"""
Paycom Weekly Hours Sync Helper
Usage:
    python paycom_hours.py week

Reads current week hours from Paycom and writes last_result.json with:
    {
      "success": bool,
      "message": str,
      "week_hours": float (when success),
      "source": str,
      "day_rows": [
        {"date_label":"Mon 02/23","hours":8.53,"clock_in":"07:35 AM","clock_out":"04:37 PM"},
        ...
      ]
    }
"""

import sys
import os
import re
import time
from datetime import datetime, timedelta

from _bootstrap import ensure_project_root_on_path

ensure_project_root_on_path()

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

from automation_audit import log_automation_event, log_automation_result
from automation_runtime import (
    SCRIPT_DIR,
    build_chrome_driver,
    configure_console_utf8,
    find_visible,
    kill_stale_chrome,
    safe_driver_quit,
    safe_get_with_partial_load,
    take_screenshot,
    write_result_payload,
)
from config import (
    PAYCOM_URL,
    PAYCOM_HOURS_URL,
    PAYCOM_WEEK_HOURS_REGEX_PATTERNS,
    WORK_CLOCK_BREAK_APPLIES_AFTER_HOURS,
    WORK_CLOCK_BREAK_MINUTES,
)
from credential_store import PAYCOM_CREDENTIAL_TARGET, read_windows_credential

configure_console_utf8()

AUDIT_AUTOMATION_NAME = "paycom_hours.week"


def write_result(success, message, week_hours=None, source="", day_rows=None):
    """Write the sync result to JSON for server.py to consume."""
    extra_fields = {}
    if week_hours is not None:
        extra_fields["week_hours"] = round(float(week_hours), 2)
    if source:
        extra_fields["source"] = source
    if isinstance(day_rows, list):
        extra_fields["day_rows"] = day_rows
    write_result_payload(
        AUDIT_AUTOMATION_NAME,
        "paycom_hours.py",
        success,
        message,
        extra_fields=extra_fields,
    )


def _normalize_text(text):
    return " ".join((text or "").replace("\xa0", " ").split())


def _is_missing_punch_marker(text):
    raw = _normalize_text(text).lower()
    if not raw:
        return True
    if raw in {"--", "??", "n/a", "na", "missing"}:
        return True
    if raw in {
        "error_outline",
        "highlight_off",
        "warning",
        "warning_amber",
        "help_outline",
        "cancel",
        "report_problem",
    }:
        return True
    if "request new punch" in raw or "forgot to clock in/out" in raw:
        return True
    return False


def _is_flex_pay_code(pay_code_text):
    text = _normalize_text(pay_code_text)
    if not text:
        return False
    return re.search(r"\bflex\b", text, flags=re.IGNORECASE) is not None


def _is_paid_leave_pay_code(pay_code_text):
    text = _normalize_text(pay_code_text)
    if not text:
        return False
    return re.search(
        r"\b(?:pto|paid\s*time\s*off|vacation|holiday|sick|personal|leave)\b",
        text,
        flags=re.IGNORECASE,
    ) is not None


def _parse_hour_like_value(text):
    raw = _normalize_text(text)
    if not raw:
        return None
    m = re.match(r"^-?\d{1,2}(?:\.\d{1,2})?$", raw)
    if not m:
        return None
    try:
        val = float(raw)
    except Exception:
        return None
    if 0.0 <= val <= 24.0:
        return round(val, 2)
    return None


def _parse_clock_minutes(text):
    raw = _normalize_text(text)
    if _is_missing_punch_marker(raw):
        return None

    candidates = [raw.upper().replace(".", "")]
    m = re.search(r"\b\d{1,2}:\d{2}\s*(?:[AP]M)?\b", candidates[0], flags=re.IGNORECASE)
    if m:
        token = m.group(0).upper().replace(".", "")
        if token and token not in candidates:
            candidates.insert(0, token)

    for cand in candidates:
        for fmt in ("%I:%M %p", "%I:%M%p", "%H:%M"):
            try:
                t = datetime.strptime(cand, fmt).time()
                return t.hour * 60 + t.minute
            except ValueError:
                continue
    return None


def _sunday_of(day):
    return day - timedelta(days=(day.weekday() + 1) % 7)


def _day_label(day):
    return day.strftime("%a %m/%d")


def _parse_paycom_punch_datetime(date_text, time_text):
    raw_date = _normalize_text(date_text)
    raw_time = _normalize_text(time_text).upper().replace(".", "")
    if not raw_date or not raw_time:
        return None

    date_value = None
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            date_value = datetime.strptime(raw_date, fmt).date()
            break
        except ValueError:
            continue
    if date_value is None:
        return None

    extracted = re.search(r"\b\d{1,2}:\d{2}\s*(?:[AP]M)?\b", raw_time, flags=re.IGNORECASE)
    if extracted:
        raw_time = extracted.group(0).upper().replace(".", "")

    for fmt in ("%I:%M %p", "%I:%M%p", "%H:%M"):
        try:
            time_value = datetime.strptime(raw_time, fmt).time()
            return datetime.combine(date_value, time_value)
        except ValueError:
            continue
    return None


def _paid_hours_for_gross(gross_hours):
    gross = max(0.0, float(gross_hours or 0.0))
    try:
        threshold = float(WORK_CLOCK_BREAK_APPLIES_AFTER_HOURS)
    except Exception:
        threshold = 4.0
    try:
        break_hours = max(0.0, float(WORK_CLOCK_BREAK_MINUTES) / 60.0)
    except Exception:
        break_hours = 0.5
    if gross > threshold:
        return max(0.0, gross - break_hours)
    return gross


def extract_week_hours(body_text):
    """Extract weekly hours from Paycom page text using regex patterns."""
    normalized = _normalize_text(body_text)
    if not normalized:
        return None, ""

    candidates = []

    patterns = PAYCOM_WEEK_HOURS_REGEX_PATTERNS or []
    for pattern in patterns:
        for match in re.finditer(pattern, normalized, flags=re.IGNORECASE):
            try:
                value = float(match.group(1))
            except Exception:
                continue

            if 0.0 <= value <= 120.0:
                candidates.append((value, match.group(0)))

    # Fallback heuristic if config patterns miss the text format.
    if not candidates:
        fallback_pattern = r"(?:this\s*week|week\s*total|total\s*hours?).{0,24}?(\d{1,2}(?:\.\d{1,2})?)"
        for match in re.finditer(fallback_pattern, normalized, flags=re.IGNORECASE):
            try:
                value = float(match.group(1))
            except Exception:
                continue
            if 0.0 <= value <= 120.0:
                candidates.append((value, match.group(0)))

    if not candidates:
        return None, ""

    # Choose the largest plausible value; this usually matches week-total over daily numbers.
    candidates.sort(key=lambda c: c[0], reverse=True)
    return round(candidates[0][0], 2), candidates[0][1]


def _get_body_text(driver):
    try:
        return driver.find_element(By.TAG_NAME, "body").text or ""
    except Exception:
        return ""


def extract_day_rows_from_timesheet(driver):
    """
    Scrape per-day rows from Paycom Web Time Sheet Read-Only table.
    Returns a list of:
      {
        "date_label":"Mon 02/23",
        "hours":8.53,
        "clock_in":"07:35 AM",
        "clock_out":"04:37 PM",
        "pay_code":"[FXX] Flex Time",
        "is_flex":true
      }
    """
    script = r"""
const clean = (v) => String(v || '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
const dayRe = /^(Sun|Mon|Tue|Wed|Thu|Fri|Sat)\s+\d{2}\/\d{2}$/i;
const parseNum = (v) => {
  const m = clean(v).match(/-?\d+(?:\.\d+)?/);
  return m ? parseFloat(m[0]) : null;
};
const looksLikeClock = (v) => /\b\d{1,2}:\d{2}\s*(?:[AP]M)?\b/i.test(clean(v));
const rowHourCandidates = (cells) => {
  const out = [];
  for (const cell of cells) {
    const txt = clean(cell);
    if (!/^-?\d{1,2}(?:\.\d{1,2})?$/.test(txt)) continue;
    const n = parseFloat(txt);
    if (!Number.isFinite(n)) continue;
    if (n < 0 || n > 24) continue;
    out.push(n);
  }
  return out;
};
const isMissingPunch = (v) => {
  const low = clean(v).toLowerCase();
  if (!low || low === '??' || low === '--' || low === 'n/a' || low === 'na' || low === 'missing') return true;
  if ([
    'error_outline',
    'highlight_off',
    'warning',
    'warning_amber',
    'help_outline',
    'cancel',
    'report_problem'
  ].includes(low)) return true;
  if (low.includes('request new punch') || low.includes('forgot to clock in/out')) return true;
  return false;
};
const outByDate = new Map();
const ensureEntry = (dateText) => {
  if (!outByDate.has(dateText)) {
    outByDate.set(dateText, {
      date_label: dateText,
      hours: null,
      clock_in: null,
      clock_out: null,
      pay_code: null,
      shift_hours_sum: 0,
      has_total_hours: false,
    });
  }
  return outByDate.get(dateText);
};
for (const table of Array.from(document.querySelectorAll('table'))) {
  const rows = Array.from(table.querySelectorAll('tr'));
  if (!rows.length) continue;

  let hoursHeaderIndex = -1;
  let hoursHeaders = [];
  let payCodeHeaderIndex = -1;
  let payCodeHeaders = [];
  for (let i = 0; i < Math.min(rows.length, 8); i++) {
    const cells = Array.from(rows[i].querySelectorAll('th,td')).map(c => clean(c.innerText || c.textContent));
    const low = cells.map(c => c.toLowerCase());
    const hasDate = low.includes('date');
    const hasHours = low.includes('hours') || low.includes('total hours');
    const hasPayCode = low.includes('pay code') || low.includes('paycode') || low.some(h => h.includes('pay code'));
    if (hoursHeaderIndex < 0 && hasDate && hasHours) {
      hoursHeaderIndex = i;
      hoursHeaders = cells;
    }
    if (payCodeHeaderIndex < 0 && hasDate && hasPayCode) {
      payCodeHeaderIndex = i;
      payCodeHeaders = cells;
    }
  }

  if (hoursHeaderIndex >= 0) {
    const lowHeaders = hoursHeaders.map(h => h.toLowerCase());
    const dateIdx = lowHeaders.indexOf('date');
    const totalHoursIdx = lowHeaders.indexOf('total hours');
    const hoursIdx = lowHeaders.indexOf('hours');
    const payCodeIdx = lowHeaders.findIndex(h => h === 'pay code' || h === 'paycode' || h.includes('pay code'));
    if (dateIdx >= 0 && (totalHoursIdx >= 0 || hoursIdx >= 0)) {
      const inIdxs = [];
      const outIdxs = [];
      for (let i = 0; i < lowHeaders.length; i++) {
        if (lowHeaders[i] === 'in') inIdxs.push(i);
        if (lowHeaders[i] === 'out') outIdxs.push(i);
      }

      let currentDateText = '';
      for (let r = hoursHeaderIndex + 1; r < rows.length; r++) {
        const cells = Array.from(rows[r].querySelectorAll('td,th')).map(c => clean(c.innerText || c.textContent));
        if (!cells.length) continue;
        const dateText = clean(cells[dateIdx] || '');
        if (dayRe.test(dateText)) {
          currentDateText = dateText;
        } else if (!currentDateText) {
          continue;
        }

        let firstIn = null;
        for (const idx of inIdxs) {
          const v = clean(cells[idx] || '');
          if (!isMissingPunch(v) && looksLikeClock(v)) {
            firstIn = v;
            break;
          }
        }

        let lastOut = null;
        for (let i = outIdxs.length - 1; i >= 0; i--) {
          const v = clean(cells[outIdxs[i]] || '');
          if (!isMissingPunch(v) && looksLikeClock(v)) {
            lastOut = v;
            break;
          }
        }

        const pc = payCodeIdx >= 0 ? clean(cells[payCodeIdx] || '') : '';
        let totalHrs = totalHoursIdx >= 0 ? parseNum(cells[totalHoursIdx] || '') : null;
        let shiftHrs = hoursIdx >= 0 ? parseNum(cells[hoursIdx] || '') : null;
        const hourCandidates = rowHourCandidates(cells);
        if (shiftHrs === null && hourCandidates.length > 0) {
          shiftHrs = Math.min(...hourCandidates);
        }
        if (totalHrs === null && hourCandidates.length > 1) {
          const minH = Math.min(...hourCandidates);
          const maxH = Math.max(...hourCandidates);
          if (maxH > minH + 0.005) {
            totalHrs = maxH;
          }
        }
        const hasCarryData = Boolean(firstIn || lastOut || pc);
        if (!dayRe.test(dateText) && !hasCarryData) continue;

        const rec = ensureEntry(currentDateText);

        if (totalHrs !== null) {
          rec.hours = totalHrs;
          rec.has_total_hours = true;
        } else if (!rec.has_total_hours && shiftHrs !== null) {
          rec.shift_hours_sum = Math.round((rec.shift_hours_sum + shiftHrs) * 100) / 100;
          rec.hours = rec.shift_hours_sum;
        }

        if (firstIn && !rec.clock_in) {
          rec.clock_in = firstIn;
        }
        if (lastOut) {
          rec.clock_out = lastOut;
        }

        if (payCodeIdx >= 0) {
          if (pc) rec.pay_code = pc;
        }
      }
    }
  }

  if (payCodeHeaderIndex >= 0) {
    const lowHeaders = payCodeHeaders.map(h => h.toLowerCase());
    const dateIdx = lowHeaders.indexOf('date');
    const payCodeIdx = lowHeaders.findIndex(h => h === 'pay code' || h === 'paycode' || h.includes('pay code'));
    if (dateIdx >= 0 && payCodeIdx >= 0) {
      for (let r = payCodeHeaderIndex + 1; r < rows.length; r++) {
        const cells = Array.from(rows[r].querySelectorAll('td,th')).map(c => clean(c.innerText || c.textContent));
        if (!cells.length) continue;
        const dateText = clean(cells[dateIdx] || '');
        if (!dayRe.test(dateText)) continue;
        const rec = ensureEntry(dateText);
        const pc = clean(cells[payCodeIdx] || '');
        if (pc) rec.pay_code = pc;
      }
    }
  }
}
const output = [];
for (const rec of outByDate.values()) {
  output.push({
    date_label: rec.date_label,
    hours: rec.hours,
    clock_in: rec.clock_in,
    clock_out: rec.clock_out,
    pay_code: rec.pay_code,
  });
}
return output;
"""
    rows = []
    try:
        raw = driver.execute_script(script) or []
        if isinstance(raw, list):
            rows = raw
    except Exception as e:
        print(f"Warning: could not scrape day rows from timesheet table: {e}")
        return []

    day_re = re.compile(r"^(Sun|Mon|Tue|Wed|Thu|Fri|Sat)\s+\d{2}/\d{2}$", flags=re.IGNORECASE)
    dedup = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        date_label = _normalize_text(row.get("date_label", ""))
        if not day_re.match(date_label):
            continue

        hours = row.get("hours")
        try:
            hours = float(hours) if hours is not None else None
        except Exception:
            hours = None
        if hours is not None:
            hours = round(hours, 2)

        pay_code = _normalize_text(row.get("pay_code", "")) or None
        if pay_code and re.match(r"^-?\d+(?:\.\d+)?$", pay_code):
            pay_code = None
        is_flex = _is_flex_pay_code(pay_code)

        clock_in = _normalize_text(row.get("clock_in", "")) or None
        clock_out = _normalize_text(row.get("clock_out", "")) or None
        if _is_missing_punch_marker(clock_in):
            clock_in = None
        if _is_missing_punch_marker(clock_out):
            clock_out = None

        # Defensive cleanup for misaligned table rows where hour decimals can land in IN/OUT columns
        # (e.g., clock_out="3.78"). Treat those as additional segment-hour values, not punch times.
        extra_hour_values = []
        if clock_in and ":" not in clock_in:
            in_as_hours = _parse_hour_like_value(clock_in)
            if in_as_hours is not None:
                extra_hour_values.append(in_as_hours)
                clock_in = None
        if clock_out and ":" not in clock_out:
            out_as_hours = _parse_hour_like_value(clock_out)
            if out_as_hours is not None:
                extra_hour_values.append(out_as_hours)
                clock_out = None
        for extra_h in extra_hour_values:
            if hours is None:
                hours = round(extra_h, 2)
                continue
            combined_h = round(hours + extra_h, 2)
            if 0.0 <= combined_h <= 24.0:
                hours = combined_h

        # Some Paycom layouts place day-hour numbers in a non-hour column.
        # Recover numeric hour values for flex rows when possible.
        if is_flex and hours is None:
            out_as_hours = _parse_hour_like_value(clock_out)
            in_as_hours = _parse_hour_like_value(clock_in)
            if out_as_hours is not None:
                hours = out_as_hours
                clock_out = None
            elif in_as_hours is not None:
                hours = in_as_hours
                clock_in = None

        entry = {
            "date_label": date_label,
            "hours": hours,
            "clock_in": clock_in,
            "clock_out": clock_out,
            "pay_code": pay_code,
            "is_flex": bool(is_flex),
            "is_possible_pto": bool(
                not is_flex
                and hours is not None
                and hours > 0
                and not clock_in
                and not clock_out
            ),
            "is_paid_leave_code": bool(_is_paid_leave_pay_code(pay_code)),
        }

        existing = dedup.get(date_label)
        if not existing:
            dedup[date_label] = entry
            continue

        existing_clock_in = existing.get("clock_in")
        existing_clock_out = existing.get("clock_out")
        incoming_clock_in = entry.get("clock_in")
        incoming_clock_out = entry.get("clock_out")
        existing_in_mins = _parse_clock_minutes(existing_clock_in)
        existing_out_mins = _parse_clock_minutes(existing_clock_out)
        incoming_in_mins = _parse_clock_minutes(incoming_clock_in)
        incoming_out_mins = _parse_clock_minutes(incoming_clock_out)

        existing_hours = existing.get("hours")
        incoming_hours = entry.get("hours")
        if existing_hours is not None and incoming_hours is not None:
            combined = round(existing_hours + incoming_hours, 2)
            has_distinct_intervals = (
                existing_in_mins is not None
                and existing_out_mins is not None
                and incoming_in_mins is not None
                and incoming_out_mins is not None
                and (existing_in_mins, existing_out_mins) != (incoming_in_mins, incoming_out_mins)
            )
            existing_contains_incoming = (
                existing_in_mins is not None
                and existing_out_mins is not None
                and incoming_in_mins is not None
                and incoming_out_mins is not None
                and existing_in_mins <= incoming_in_mins
                and existing_out_mins >= incoming_out_mins
            )
            incoming_contains_existing = (
                existing_in_mins is not None
                and existing_out_mins is not None
                and incoming_in_mins is not None
                and incoming_out_mins is not None
                and incoming_in_mins <= existing_in_mins
                and incoming_out_mins >= existing_out_mins
            )
            if (
                has_distinct_intervals
                and not existing_contains_incoming
                and not incoming_contains_existing
                and combined <= 24.0
            ):
                existing["hours"] = combined
            elif incoming_hours > existing_hours:
                existing["hours"] = incoming_hours
        elif incoming_hours is not None and existing_hours is None:
            existing["hours"] = incoming_hours

        if incoming_clock_in:
            if (
                not existing_clock_in
                or (
                    incoming_in_mins is not None
                    and (existing_in_mins is None or incoming_in_mins < existing_in_mins)
                )
            ):
                existing["clock_in"] = incoming_clock_in

        if incoming_clock_out:
            if (
                not existing_clock_out
                or (
                    incoming_out_mins is not None
                    and (existing_out_mins is None or incoming_out_mins > existing_out_mins)
                )
            ):
                existing["clock_out"] = incoming_clock_out

        incoming_pay_code = entry.get("pay_code")
        if incoming_pay_code and (not existing.get("pay_code") or (entry.get("is_flex") and not existing.get("is_flex"))):
            existing["pay_code"] = incoming_pay_code
        existing["is_flex"] = bool(existing.get("is_flex")) or bool(entry.get("is_flex"))
        existing["is_paid_leave_code"] = bool(existing.get("is_paid_leave_code")) or bool(entry.get("is_paid_leave_code"))
        existing["is_possible_pto"] = bool(
            not existing.get("is_flex")
            and existing.get("hours") is not None
            and float(existing.get("hours") or 0) > 0
            and not existing.get("clock_in")
            and not existing.get("clock_out")
        )
        dedup[date_label] = existing

    ordered_days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
    result = list(dedup.values())
    result.sort(key=lambda e: ordered_days.index(e["date_label"][:3]) if e.get("date_label", "")[:3] in ordered_days else 99)
    return result


def extract_recent_punch_rows_from_timeclock(driver):
    """
    Scrape Paycom Time Clock's Recent Punches table.
    This view does not expose a week total, but it can still provide enough
    punch data to compute completed current-week shifts.
    """
    script = r"""
const clean = (v) => String(v || '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
const output = [];
for (const table of Array.from(document.querySelectorAll('table'))) {
  const rows = Array.from(table.querySelectorAll('tr'));
  if (!rows.length) continue;

  let headerIndex = -1;
  let headers = [];
  for (let i = 0; i < Math.min(rows.length, 4); i++) {
    const cells = Array.from(rows[i].querySelectorAll('th,td')).map(c => clean(c.innerText || c.textContent));
    const low = cells.map(c => c.toLowerCase());
    if (low.includes('type') && low.includes('date') && low.includes('time')) {
      headerIndex = i;
      headers = low;
      break;
    }
  }
  if (headerIndex < 0) continue;

  const typeIdx = headers.indexOf('type');
  const dateIdx = headers.indexOf('date');
  const timeIdx = headers.indexOf('time');
  const roundedIdx = headers.indexOf('rounded time');
  for (let r = headerIndex + 1; r < rows.length; r++) {
    const cells = Array.from(rows[r].querySelectorAll('td,th')).map(c => clean(c.innerText || c.textContent));
    if (!cells.length) continue;
    const punchType = clean(cells[typeIdx] || '');
    const punchDate = clean(cells[dateIdx] || '');
    const punchTime = clean(cells[(roundedIdx >= 0 ? roundedIdx : timeIdx)] || cells[timeIdx] || '');
    if (!punchType || !punchDate || !punchTime) continue;
    output.push({type: punchType, date: punchDate, time: punchTime});
  }
}
return output;
"""
    try:
        raw_rows = driver.execute_script(script) or []
    except Exception as e:
        print(f"Warning: could not scrape recent punch rows from time clock table: {e}")
        return []

    if not isinstance(raw_rows, list):
        return []

    rows = []
    for raw in raw_rows:
        if not isinstance(raw, dict):
            continue
        punch_type = _normalize_text(raw.get("type", ""))
        punch_dt = _parse_paycom_punch_datetime(raw.get("date", ""), raw.get("time", ""))
        if not punch_type or punch_dt is None:
            continue
        kind = None
        if re.search(r"\bin\b", punch_type, flags=re.IGNORECASE):
            kind = "in"
        elif re.search(r"\bout\b", punch_type, flags=re.IGNORECASE):
            kind = "out"
        if not kind:
            continue
        rows.append(
            {
                "kind": kind,
                "type": punch_type,
                "dt": punch_dt,
                "time": punch_dt.strftime("%I:%M %p").lstrip("0"),
            }
        )
    rows.sort(key=lambda row: row["dt"])
    return rows


def _compute_week_from_recent_punches(punch_rows, today=None):
    today = today or datetime.now().date()
    week_start = _sunday_of(today)
    week_end = week_start + timedelta(days=7)
    current_week_rows = [
        row for row in punch_rows
        if week_start <= row.get("dt", datetime.min).date() < week_end
    ]
    if not current_week_rows:
        return None, []

    days = {}
    open_in = None
    for row in current_week_rows:
        punch_dt = row["dt"]
        label = _day_label(punch_dt)
        entry = days.setdefault(
            label,
            {
                "date_label": label,
                "hours": None,
                "clock_in": None,
                "clock_out": None,
                "pay_code": None,
                "is_flex": False,
                "is_possible_pto": False,
                "is_paid_leave_code": False,
            },
        )
        if row["kind"] == "in":
            if not entry["clock_in"]:
                entry["clock_in"] = row["time"]
            open_in = row
            continue

        if row["kind"] != "out":
            continue
        if not entry["clock_out"]:
            entry["clock_out"] = row["time"]
        elif _parse_clock_minutes(row["time"]) is not None and _parse_clock_minutes(row["time"]) > _parse_clock_minutes(entry["clock_out"]):
            entry["clock_out"] = row["time"]

        if not open_in or open_in["dt"] >= punch_dt:
            continue
        gross_hours = (punch_dt - open_in["dt"]).total_seconds() / 3600.0
        if gross_hours <= 0 or gross_hours > 24:
            open_in = None
            continue
        paid_hours = round(_paid_hours_for_gross(gross_hours), 2)
        in_label = _day_label(open_in["dt"])
        in_entry = days.setdefault(
            in_label,
            {
                "date_label": in_label,
                "hours": None,
                "clock_in": open_in["time"],
                "clock_out": None,
                "pay_code": None,
                "is_flex": False,
                "is_possible_pto": False,
                "is_paid_leave_code": False,
            },
        )
        if not in_entry["clock_in"]:
            in_entry["clock_in"] = open_in["time"]
        in_entry["clock_out"] = row["time"]
        in_entry["hours"] = round(float(in_entry["hours"] or 0.0) + paid_hours, 2)
        open_in = None

    ordered_days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
    day_rows = list(days.values())
    day_rows.sort(key=lambda e: ordered_days.index(e["date_label"][:3]) if e.get("date_label", "")[:3] in ordered_days else 99)
    total = round(sum(float(row.get("hours") or 0.0) for row in day_rows), 2)
    return total, day_rows


def extract_week_hours_from_recent_punches(driver):
    punch_rows = extract_recent_punch_rows_from_timeclock(driver)
    week_hours, day_rows = _compute_week_from_recent_punches(punch_rows)
    if week_hours is None:
        return None, []
    return week_hours, day_rows


def adjust_week_hours_for_flex_days(week_hours, day_rows):
    """
    Subtract flex-day hours from parsed weekly totals so unpaid Flex Time does not count.
    Returns (adjusted_week_hours, flex_days_found, flex_hours_removed, flex_days_with_hours).
    """
    if week_hours is None:
        return None, 0, 0.0, 0
    if not isinstance(day_rows, list):
        return round(float(week_hours), 2), 0, 0.0, 0

    flex_days_found = 0
    flex_days_with_hours = 0
    flex_hours_removed = 0.0
    for row in day_rows:
        if not isinstance(row, dict):
            continue
        if not bool(row.get("is_flex")):
            continue
        flex_days_found += 1
        raw = row.get("hours")
        try:
            h = float(raw) if raw is not None else None
        except Exception:
            h = None
        if h is None:
            continue
        if h < 0:
            continue
        flex_days_with_hours += 1
        flex_hours_removed += h

    adjusted = max(0.0, float(week_hours) - flex_hours_removed)
    return round(adjusted, 2), flex_days_found, round(flex_hours_removed, 2), flex_days_with_hours


def _try_extract_from_current_page(driver):
    body_text = _get_body_text(driver)
    week_hours, match_text = extract_week_hours(body_text)
    try:
        page_url = driver.current_url
    except Exception:
        page_url = ""
    return week_hours, match_text, body_text, page_url


def _click_first_text_match(driver, target_text):
    """Click the first visible element whose text contains target_text."""
    needle = target_text.lower()
    try:
        elements = driver.find_elements(
            By.XPATH,
            "//*[self::a or self::button or @role='button' or @tabindex]",
        )
    except Exception:
        return False

    for el in elements:
        try:
            text = (el.text or "").strip()
            if not text or needle not in text.lower():
                continue
            if not el.is_displayed():
                continue
            try:
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
            except Exception:
                pass
            try:
                el.click()
            except Exception:
                driver.execute_script("arguments[0].click();", el)
            return True
        except Exception:
            continue

    return False


def _collect_candidate_time_links(driver):
    candidates = []
    seen = set()
    keywords = ("timesheet", "timeclock", "timemanagement", "time-management", "web.php/timeclock")

    try:
        links = driver.find_elements(By.TAG_NAME, "a")
    except Exception:
        links = []

    for link in links:
        try:
            href = (link.get_attribute("href") or "").strip()
            text = (link.text or "").strip().lower()
            href_lower = href.lower()
            if not href:
                continue
            if any(k in href_lower for k in keywords) or any(k in text for k in keywords):
                if href not in seen:
                    seen.add(href)
                    candidates.append(href)
        except Exception:
            continue

    return candidates[:10]


def find_week_hours_with_fallback_navigation(driver):
    """Try current page first, then navigate to likely timesheet pages and retry."""
    week_hours, match_text, body_text, source_url = _try_extract_from_current_page(driver)
    if week_hours is not None:
        return week_hours, match_text, body_text, source_url

    print("Weekly hours not found on current page. Trying timesheet navigation fallbacks...")

    # Try menu/cmd links visible on dashboard-like pages.
    nav_labels = [
        "web time sheet",
        "read-only time sheet",
        "time sheet",
        "time management",
    ]
    for label in nav_labels:
        if _click_first_text_match(driver, label):
            print(f"Clicked navigation item containing '{label}'.")
            time.sleep(2.5)
            week_hours, match_text, body_text, source_url = _try_extract_from_current_page(driver)
            if week_hours is not None:
                return week_hours, match_text, body_text, source_url

    # Try direct links discovered from current page.
    for url in _collect_candidate_time_links(driver):
        try:
            print(f"Trying discovered time link: {url}")
            safe_get_with_partial_load(driver, url, "discovered time link")
            time.sleep(2.5)
        except Exception:
            continue

        week_hours, match_text, body_text, source_url = _try_extract_from_current_page(driver)
        if week_hours is not None:
            return week_hours, match_text, body_text, source_url

    # Final retry on default Paycom time-clock URL.
    try:
        print(f"Trying fallback Paycom URL: {PAYCOM_URL}")
        safe_get_with_partial_load(driver, PAYCOM_URL, "fallback Paycom URL")
        time.sleep(2.5)
        week_hours, match_text, body_text, source_url = _try_extract_from_current_page(driver)
        if week_hours is not None:
            return week_hours, match_text, body_text, source_url
    except Exception:
        pass

    return None, "", body_text, source_url


def _build_driver(profile_path, headless_mode):
    return build_chrome_driver(
        profile_path,
        headless_mode=headless_mode,
        page_load_strategy="eager",
        page_load_timeout=45,
        script_timeout=30,
    )


def _run_once(headless_mode):
    driver = None
    profile_path = os.path.join(SCRIPT_DIR, "chrome_profile")
    try:
        start_time = time.time()
        mode_label = "headless" if headless_mode else "visible"
        print(f"Starting Paycom weekly-hours sync ({mode_label} mode)...")

        kill_stale_chrome(profile_path, profile_label="Paycom-hours automation")

        driver = _build_driver(profile_path, headless_mode)

        target_url = PAYCOM_HOURS_URL or PAYCOM_URL
        print(f"Navigating to hours source page: {target_url}")
        safe_get_with_partial_load(driver, target_url, "hours source page")

        # Fill PIN when login page appears.
        pin_field = find_visible(
            driver,
            [
                "input[name='pin']",
                "input[id*='pin']",
                "input[placeholder*='PIN']",
                "input[type='password'][maxlength='4']",
            ],
            timeout=3,
        )
        if pin_field:
            print("Entering PIN for hours sync...")
            pin = read_windows_credential(PAYCOM_CREDENTIAL_TARGET).secret
            pin_field.clear()
            pin_field.send_keys(pin)

        login_btn = find_visible(
            driver,
            [
                "button[type='submit']",
                "input[type='submit']",
            ],
            timeout=2,
        )
        if login_btn:
            login_btn.click()
            print("Clicked Log In for hours sync.")
            try:
                WebDriverWait(driver, 8).until(EC.staleness_of(login_btn))
            except TimeoutException:
                pass

        # Give the page a moment to hydrate numbers.
        time.sleep(2)

        week_hours, match_text, body_text, parsed_from_url = find_week_hours_with_fallback_navigation(driver)
        day_rows = extract_day_rows_from_timesheet(driver)
        if week_hours is None:
            recent_week_hours, recent_day_rows = extract_week_hours_from_recent_punches(driver)
            if recent_week_hours is not None:
                week_hours = recent_week_hours
                match_text = "computed from current-week recent punches"
                if recent_day_rows:
                    day_rows = recent_day_rows
                try:
                    parsed_from_url = driver.current_url
                except Exception:
                    pass
        if week_hours is None:
            take_screenshot(driver, "paycom_hours_not_found")
            snippet = _normalize_text(body_text)[:300]
            msg = (
                "Could not parse weekly hours from Paycom page text. "
                "Try setting PAYCOM_HOURS_URL to your direct Web Time Sheet Read-Only link."
            )
            if snippet:
                msg += f" Text snippet: {snippet}"
            return False, msg, None, parsed_from_url or target_url, day_rows

        adjusted_week_hours, flex_days_found, flex_hours_removed, flex_days_with_hours = (
            adjust_week_hours_for_flex_days(week_hours, day_rows)
        )

        elapsed = time.time() - start_time
        msg = (
            f"Paycom weekly hours parsed: {week_hours:.2f} "
            f"(match: '{match_text}') ({elapsed:.1f}s)"
        )
        if flex_days_found > 0:
            if flex_hours_removed > 0:
                msg += (
                    f" Flex days excluded: {flex_days_found} "
                    f"(-{flex_hours_removed:.2f}h -> {adjusted_week_hours:.2f}h)."
                )
            else:
                msg += (
                    f" Flex days detected: {flex_days_found}, "
                    "but no numeric flex hours were found to subtract."
                )
        if day_rows:
            msg += f" Day rows scraped: {len(day_rows)}."
            possible_pto_days = [
                str(row.get("date_label"))
                for row in day_rows
                if isinstance(row, dict) and bool(row.get("is_possible_pto"))
            ]
            if possible_pto_days:
                msg += f" Possible PTO/paid leave rows: {', '.join(possible_pto_days)}."
        return True, msg, adjusted_week_hours, parsed_from_url or target_url, day_rows

    except Exception as e:
        try:
            if driver:
                take_screenshot(driver, "paycom_hours_error")
        except Exception:
            pass

        error_msg = f"Paycom hours sync failed: {type(e).__name__}: {e}"
        return False, error_msg, None, "", []
    finally:
        if driver:
            safe_driver_quit(driver, profile_path=profile_path)


def run():
    log_automation_event(
        AUDIT_AUTOMATION_NAME,
        "STARTED",
        "Requested action: week",
        source="paycom_hours.py",
    )
    success, msg, week_hours, source, day_rows = _run_once(headless_mode=True)
    if success:
        write_result(True, msg, week_hours=week_hours, source=source, day_rows=day_rows)
        print(f"RESULT:SUCCESS:{msg}")
        return

    # If the headless renderer hangs, retry once in visible mode.
    if "timed out receiving message from renderer" in msg.lower():
        print("Headless sync hit renderer timeout. Retrying once in visible mode...")
        success2, msg2, week_hours2, source2, day_rows2 = _run_once(headless_mode=False)
        if success2:
            write_result(True, msg2, week_hours=week_hours2, source=source2, day_rows=day_rows2)
            print(f"RESULT:SUCCESS:{msg2}")
            return
        msg = msg2
        day_rows = day_rows2

    write_result(False, msg, source=source, day_rows=day_rows)
    print(f"RESULT:FAIL:{msg}")
    sys.exit(1)


if __name__ == "__main__":
    # Optional argument is kept for future extension.
    if len(sys.argv) > 2:
        log_automation_result(
            "paycom_hours.invalid_invocation",
            False,
            "Invalid command-line arguments.",
            source="paycom_hours.py",
        )
        print("Usage: python paycom_hours.py [week]")
        sys.exit(1)

    run()
