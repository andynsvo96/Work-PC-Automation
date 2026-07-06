"""
CRM Shipping Cost Bypass automation worker.

This worker handles orders where CRM stock is available but the normal stock
order button is blocked by shipping-cost rules. It manually places a SanMar
order, then records the same PO in CRM under Manual Order.
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta

from _bootstrap import ensure_project_root_on_path

ensure_project_root_on_path()

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from automation_runtime import (
    SCRIPT_DIR,
    build_chrome_driver,
    configure_console_utf8,
    kill_stale_chrome,
    refresh_if_crm_challenge_attempts_exceeded,
    safe_driver_quit,
    safe_get_with_partial_load,
    safe_take_screenshot,
    write_result_payload,
    write_status_payload,
)
from config import (
    CRM_ACTION_TIMEOUT,
    CRM_PAGE_LOAD_TIMEOUT,
    CRM_PROFILE_DIR,
    CRM_SHIPPING_BYPASS_URL,
    SANMAR_CART_URL,
    SANMAR_PROFILE_DIR,
    SANMAR_URL,
)
try:
    from config import SANMAR_PASSWORD, SANMAR_USERNAME
except ImportError:
    SANMAR_USERNAME = ""
    SANMAR_PASSWORD = ""
from crm_validate_address import (
    ALLOWED_SHIPPING_LIST_ROW_DESCRIPTION,
    _batch_collection_limit,
    _batch_limit_reached,
    _build_crm_session_driver,
    _click_with_fallback,
    _clone_profile_for_worker,
    _collect_batch_order_ids,
    _collect_batch_order_ids_with_driver,
    _crm_attempt_modes,
    _is_retryable_exception,
    _normalize_requested_batch_size,
    _normalize_target_order_id,
    _open_target_order,
    _worker_profile_lock,
)
from crm_order_goods import _wait_for_order_goods_page_ready
from runtime_paths import SCREENSHOTS_DIR, state_file

configure_console_utf8()

AUTOMATION_NAME = "crm.shipping_bypasser"
PROFILE_PATH = os.path.join(SCRIPT_DIR, CRM_PROFILE_DIR)
SANMAR_PROFILE_PATH = os.path.join(SCRIPT_DIR, SANMAR_PROFILE_DIR)
RUSH_FILTER = "rush"
CONTINUOUS_ORDER_FETCH_LIMIT = 25
CRM_STATE_PATH = state_file("crm_state.json")
SHIPPING_BYPASS_PENDING_SUBMISSIONS_PATH = state_file("shipping_bypasser_pending_submissions.json")
WAREHOUSE_DISTANCE = {
    "inhouse": [
        "Robbinsville, NJ",
        "Richmond, VA",
        "Cincinnati, OH",
        "Jacksonville, FL",
        "Minneapolis, MN",
        "Dallas, TX",
        "Phoenix, AZ",
        "Reno, NV",
        "Seattle, WA",
    ],
    "mach6": [
        "Phoenix, AZ",
        "Reno, NV",
        "Seattle, WA",
        "Dallas, TX",
        "Minneapolis, MN",
        "Cincinnati, OH",
        "Jacksonville, FL",
        "Richmond, VA",
        "Robbinsville, NJ",
    ],
}
WAREHOUSE_ALIASES = {
    "robbinsville": "Robbinsville, NJ",
    "richmond": "Richmond, VA",
    "cincinnati": "Cincinnati, OH",
    "jacksonville": "Jacksonville, FL",
    "minneapolis": "Minneapolis, MN",
    "dallas": "Dallas, TX",
    "phoenix": "Phoenix, AZ",
    "reno": "Reno, NV",
    "seattle": "Seattle, WA",
}
SANMAR_WAREHOUSE_STOCK_BUFFER = 10
SIZE_TOKENS = {
    "XS", "S", "M", "L", "XL", "2XL", "3XL", "4XL", "5XL", "6XL",
    "S/M", "L/XL", "2/3X", "4/5X",
    "YXS", "YS", "YM", "YL", "YXL", "ONE SIZE", "ONESIZE", "OSFA",
    "LT", "XLT", "2XT", "3XT", "4XT",
    "NB", "3M", "6M", "12M", "18M", "24M",
    "0-3MOS", "3-6MOS", "6-12MOS", "12-18MOS", "18-24MOS",
    "2T", "3T", "4T", "5T", "6T", "7T", "5/6",
}
SANMAR_COMBO_SIZE_ALIASES = {
    "S/M": "S/M",
    "L/XL": "L/XL",
    "2/3X": "2/3X",
    "2/3XL": "2/3X",
    "2X/3X": "2/3X",
    "2XL/3XL": "2/3X",
    "4/5X": "4/5X",
    "4/5XL": "4/5X",
    "4X/5X": "4/5X",
    "4XL/5XL": "4/5X",
}
SANMAR_PRODUCT_SEARCH_OVERRIDES = {
    # Bella+Canvas product info pages can require the blue inventory button.
    # CRM 3001C maps to SanMar BC3001; searching 3001 lands on the correct result family.
    "3001C": {"search_id": "3001", "click_inventory_button": True, "expected_style_keys": ["BC3001"]},
}
SANMAR_BELLA_CANVAS_STYLE_IDS = {
    "BC100B", "BC1010", "BC1012", "BC1019", "BC108", "BC1080",
    "BC1200", "BC1201", "BC1501", "BC3001", "BC3001B",
    "BC3001CVC", "BC3001T", "BC3001Y", "BC3001YCVC", "BC3005",
    "BC3005CVC", "BC3010", "BC3010Y", "BC3200", "BC3413",
    "BC3413T", "BC3413Y", "BC3415", "BC3480", "BC3480CVC",
    "BC3480Y", "BC3483", "BC3501", "BC3501CVC", "BC3501T",
    "BC3501Y", "BC3501YCVC", "BC3511", "BC3511Y", "BC3512",
    "BC3513", "BC3650", "BC3655", "BC3719", "BC3719T",
    "BC3719Y", "BC3725", "BC3727", "BC3729", "BC373", "BC3738",
    "BC3738Y", "BC3739", "BC3739Y", "BC3787", "BC390", "BC3901",
    "BC3901Y", "BC3909", "BC391", "BC3911", "BC3945", "BC4610",
    "BC4651", "BC4711", "BC4719", "BC4737", "BC4810GD",
    "BC4851GD", "BC6003", "BC6004", "BC6008", "BC6110", "BC640",
    "BC6400", "BC6400CVC", "BC6405", "BC6405CVC", "BC6413",
    "BC648", "BC6482", "BC6500", "BC6682", "BC6824GD", "BC6882GD",
    "BC7502", "BC7505", "BC8413", "BC8800", "BC8803", "BC8804",
    "BC8882",
}
SANMAR_RABBIT_SKINS_STYLE_IDS = {
    "RS1003",
    "RS1005",
    "RS3037",
    "RS3321",
    "RS3322",
    "RS3326",
    "RS3330",
    "RS4400",
    "RS4421",
    "RS4424",
    "RS4430",
    "RS4437",
}
SANMAR_JERZEES_STYLE_IDS = {
    "21B", "21LS", "21M", "29", "29B", "29BL", "29LS", "29M",
    "29MP", "363L", "363M", "436MP", "437M", "443M", "4528M",
    "4662M", "4850MP", "560LS", "560R", "562B", "562M", "570M",
    "700M", "701M", "96C", "97", "97C", "973B", "973M", "974MP",
    "975B", "975MP", "978MP", "993B", "993M", "995M", "996M",
    "996Y", "C12", "C12M", "H12M", "IC46B", "IC46L", "IC46M",
    "IC48", "IC48M", "IC49M", "IC50M", "Z12M",
}
SANMAR_GILDAN_STYLE_IDS = {
    "64PLSMA", "64000CVC", "18000", "64200", "42400", "5000B",
    "12500",
    "H000", "2410", "64400", "18600", "64800", "G2400", "64000L",
    "19000", "SF000", "42000", "G5200", "64800L", "64220LCVC",
    "64000", "8000", "8800", "SF008", "SF600", "2000T", "75000",
    "5000L", "18000B", "SF500", "3000", "85800", "19500", "64V00",
    "64V00L", "2300", "65000L", "64200L", "2000", "2000L", "5400",
    "5700", "8300", "18500", "8000B", "8400", "65000", "64440CVC",
    "64001LCVC", "980", "2200", "5000", "2000B", "18900", "18200",
    "18400", "SF100", "5V00L", "5300", "3000B", "64000B", "5100P",
    "64000BCVC", "18600B", "8800B", "18500B", "42000B", "SF500B",
    "5400B", "65000B", "18200B",
}
SANMAR_COLOR_ALIASES = {
    "FORESTGREEN": ["Forest"],
    "KELLYGREEN": ["Kelly"],
    "SAFETYGREEN": ["S. Green"],
    "SAFETYORANGE": ["S. Orange"],
}
SANMAR_PRODUCT_COLOR_ALIASES = {
    ("4528", "TRUENAVY"): ["J. Navy"],
    ("3330", "WHITESOLIDBLACK"): ["White/ Black"],
    ("RS3330", "WHITESOLIDBLACK"): ["White/ Black"],
    ("J325", "BTLGREY"): ["Battleship Grey"],
    ("J325", "BATTLEGREY"): ["Battleship Grey"],
    ("J325", "BATTLESHIPGREY"): ["Battleship Grey"],
    ("J325", "DSBLNAVY"): ["Dress Blue Navy"],
    ("J325", "DRESSBLUENAVY"): ["Dress Blue Navy"],
    ("LST402", "BLACKTRIADSOLID"): ["Black Triad Solid"],
    ("LST402", "BLACKTRIADSLD"): ["Black Triad Solid"],
    ("LST402", "DARKGREYHTHR"): ["Dark Grey Heather"],
    ("LST402", "DKGREYHTHR"): ["Dark Grey Heather"],
    ("LST402", "LIGHTGREYHTHR"): ["Light Grey Heather"],
    ("LST402", "LTGREYHTHR"): ["Light Grey Heather"],
    ("LST402", "PINKRASPBERRYHTHR"): ["Pink Raspberry Heather"],
    ("LST402", "PINKRASPHTHR"): ["Pink Raspberry Heather"],
    ("LST402", "PNKRASPBERRYHTHR"): ["Pink Raspberry Heather"],
    ("LST402", "PONDBLUEHTHR"): ["Pond Blue Heather"],
    ("LST402", "PONDBLUHTHR"): ["Pond Blue Heather"],
}
SANMAR_KNOWN_COLOR_NAMES = (
    "Deep Red/ White",
    "Heathered Watermelon/ Heathered Charcoal",
)
SANMAR_CRM_COLOR_WORD_ALIASES = {
    "BLACK": ("BLK",),
    "CHARCOAL": ("CH", "CHAR", "CHRCL"),
    "DEEP": ("DP",),
    "HEATHER": ("HTH", "HTHR", "HTR"),
    "HEATHERED": ("HE", "HTH", "HTHD", "HTHRD", "HTR", "HTRD"),
    "RED": ("RD",),
    "WATERMELON": ("WATR", "WTRMLN"),
    "WHITE": ("WHT", "WHIT"),
}
BELLA_CANVAS_SANMAR_COLOR_NAMES = (
    "Aqua Triblend",
    "Athletic Grey Triblend",
    "Berry Triblend",
    "Black Heather Triblend",
    "Blue Storm Triblend",
    "Blue Triblend",
    "Brick Triblend",
    "Brown Triblend",
    "Cardinal Triblend",
    "Cement Triblend",
    "Charcoal-Black Triblend",
    "Charity Pink Triblend",
    "Clay Triblend",
    "Dark Lavender Triblend",
    "Denim Triblend",
    "Dusty Blue Triblend",
    "Emerald Triblend",
    "Grass Green Triblend",
    "Green Triblend",
    "Grey Triblend",
    "Heather Columbia Blue",
    "Ice Blue Triblend",
    "Kelly Triblend",
    "Lilac Triblend",
    "Maroon Triblend",
    "Mauve Triblend",
    "Military Green Triblend",
    "Mint Triblend",
    "Mustard Triblend",
    "Navy Triblend",
    "Oatmeal Triblend",
    "Olive Triblend",
    "Orange Triblend",
    "Orchid Triblend",
    "Pale Yellow Triblend",
    "Peach Triblend",
    "Pink Triblend",
    "Purple Triblend",
    "Red Triblend",
    "Sea Green Triblend",
    "Solid Asphalt Triblend",
    "Solid Black Triblend",
    "Solid Blue Triblend",
    "Solid Carolina Blue Triblend",
    "Solid Dark Grey Triblend",
    "Solid Forest Triblend",
    "Solid Kelly Triblend",
    "Solid Maroon Triblend",
    "Solid Natural Triblend",
    "Solid Navy Triblend",
    "Solid Orange Triblend",
    "Solid Red Triblend",
    "Solid Silver Triblend",
    "Solid Slate Triblend",
    "Solid Team Purple Triblend",
    "Solid True Royal Triblend",
    "Solid White Triblend",
    "Steel Blue Triblend",
    "Storm Triblend",
    "Sunset Triblend",
    "Tan Triblend",
    "Teal Triblend",
    "True Royal Triblend",
    "White Fleck Triblend",
    "Yellow Gold Triblend",
)
BELLA_CANVAS_CRM_COLOR_WORD_ALIASES = {
    "ASPHALT": ("ASPH", "ASPHLT"),
    "ATHLETIC": ("ATH", "ATHL"),
    "BLACK": ("BLK",),
    "BLUE": ("BLU",),
    "BRICK": ("BRK",),
    "BROWN": ("BRN",),
    "CARDINAL": ("CARD",),
    "CAROLINA": ("CAR", "CARO"),
    "CEMENT": ("CEM",),
    "CHARCOAL": ("CHAR", "CHRCL"),
    "CHARITY": ("CHARITY", "CHRITY"),
    "COLUMBIA": ("COLUM", "COL"),
    "DARK": ("DK",),
    "DENIM": ("DNM",),
    "DUSTY": ("DSTY",),
    "FLECK": ("FLK",),
    "FOREST": ("FOR", "FRST"),
    "GOLD": ("GLD",),
    "GRASS": ("GRS",),
    "GREEN": ("GRN",),
    "GREY": ("GRY", "GRAY"),
    "HEATHER": ("HTHR", "HTR"),
    "KELLY": ("KEL",),
    "LAVENDER": ("LAV",),
    "MAROON": ("MRN",),
    "MILITARY": ("MIL", "MLTRY"),
    "NATURAL": ("NAT",),
    "NAVY": ("NVY",),
    "ORANGE": ("ORG", "ORN"),
    "PALE": ("PL",),
    "PEACH": ("PCH",),
    "PINK": ("PNK",),
    "PURPLE": ("PURP",),
    "RED": ("RD",),
    "ROYAL": ("RYL",),
    "SILVER": ("SLVR",),
    "SLATE": ("SLT",),
    "SOLID": ("SLD",),
    "STEEL": ("STL",),
    "STORM": ("STRM",),
    "TEAM": ("TM",),
    "TRIBLEND": ("TRBLND", "TRI BLEND", "TRI-BLEND"),
    "TRUE": ("TRU", "TR"),
    "WHITE": ("WHT",),
    "YELLOW": ("YLLW", "YLW"),
}
UNIQUE_PRODUCT_ID_HANDLERS = {
    "A4": "A4 stock styles are searched with an a4 prefix, for example NW3201 -> a4NW3201.",
    "Bella+Canvas": "Bella+Canvas CRM styles can omit the SanMar BC prefix or carry a CRM-only C suffix, for example 3413C -> BC3413.",
    "Gildan": "Some Gildan CRM styles differ from SanMar styles, for example G500 -> 5000 and G640 -> 64000.",
    "3001C": "Search as 3001, choose BC3001, then click Check inventory and pricing before selecting color/quantities.",
    "Rabbit Skins": "Rabbit Skins CRM styles may omit the RS prefix, for example 4400 -> RS4400, and require the inventory/pricing button.",
    "Jerzees": "Jerzees CRM styles can omit SanMar's trailing M, for example 562 -> 562M.",
    "Next Level": "Next Level CRM styles can omit or suffix SanMar's NL prefix, for example 3600 -> NL3600 and 3933NL -> NL3933.",
}


def _publish_status(message, *, stage=None, current=None, total=None, order_id=None):
    try:
        write_status_payload(
            AUTOMATION_NAME,
            message,
            stage=stage,
            current=current,
            total=total,
            order_id=order_id,
        )
    except Exception:
        pass


def _elapsed_seconds(started_at):
    return round(max(0.0, time.monotonic() - started_at), 1)


def _validate_runtime_config(list_url=None):
    target_url = str(list_url or CRM_SHIPPING_BYPASS_URL or "").strip()
    if not target_url:
        raise RuntimeError("CRM_SHIPPING_BYPASS_URL is empty in config.py.")
    if str(list_url or "").strip():
        return target_url
    lowered_url = target_url.lower()
    if "shipping+is+too+expensive" not in lowered_url and "shipping is too expensive" not in lowered_url:
        raise RuntimeError("Shipping Bypasser list URL must target Sales Notes = Shipping is too expensive.")
    return target_url


def _normalize_order_ids(raw_ids):
    cleaned = []
    seen = set()
    values = raw_ids if isinstance(raw_ids, list) else []
    for raw in values:
        text = "".join(ch for ch in str(raw or "") if ch.isdigit())
        if len(text) != 7 or text in seen:
            continue
        seen.add(text)
        cleaned.append(text)
    return cleaned


def _load_historical_shipping_bypass_order_ids(state_path=CRM_STATE_PATH):
    try:
        with open(state_path, "r", encoding="utf-8-sig") as handle:
            state = json.load(handle)
    except FileNotFoundError:
        return set()
    except Exception as exc:
        print(f"Warning: could not read previous Shipping Bypasser history for skip list: {exc}")
        return set()
    history = state.get("run_history") if isinstance(state, dict) else []
    skipped = set()
    for entry in history if isinstance(history, list) else []:
        if not isinstance(entry, dict):
            continue
        automation_key = str(entry.get("automation_key") or "").strip().lower()
        automation_label = str(entry.get("automation_label") or "").strip().lower()
        if automation_key != "shipping_bypasser" and "shipping bypass" not in automation_label:
            continue
        if entry.get("dry_run"):
            continue
        order_results = entry.get("order_results") if isinstance(entry.get("order_results"), list) else []
        if order_results:
            for item in order_results:
                if isinstance(item, dict) and item.get("success"):
                    skipped.update(_normalize_order_ids([item.get("order_id")]))
            continue
        if entry.get("success"):
            skipped.update(_normalize_order_ids(entry.get("order_ids")))
    return skipped


def _load_historical_shipping_bypass_customer_pos(state_path=CRM_STATE_PATH):
    try:
        with open(state_path, "r", encoding="utf-8-sig") as handle:
            state = json.load(handle)
    except FileNotFoundError:
        return set()
    except Exception as exc:
        print(f"Warning: could not read previous Shipping Bypasser PO history: {exc}")
        return set()
    history = state.get("run_history") if isinstance(state, dict) else []
    customer_pos = set()
    for entry in history if isinstance(history, list) else []:
        if not isinstance(entry, dict):
            continue
        automation_key = str(entry.get("automation_key") or "").strip().lower()
        automation_label = str(entry.get("automation_label") or "").strip().lower()
        if automation_key != "shipping_bypasser" and "shipping bypass" not in automation_label:
            continue
        if entry.get("dry_run"):
            continue
        for item in entry.get("order_results") if isinstance(entry.get("order_results"), list) else []:
            if not isinstance(item, dict):
                continue
            confirmation = item.get("sanmar_confirmation") if isinstance(item.get("sanmar_confirmation"), dict) else {}
            po = str(confirmation.get("po") or "").strip()
            if po:
                customer_pos.add(po.lower())
            for detail in item.get("partial_success_details") if isinstance(item.get("partial_success_details"), list) else []:
                if isinstance(detail, dict):
                    po = str(detail.get("po") or "").strip()
                    if po:
                        customer_pos.add(po.lower())
    return customer_pos


def _load_pending_shipping_bypass_submissions(path=SHIPPING_BYPASS_PENDING_SUBMISSIONS_PATH):
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return []
    except Exception as exc:
        print(f"Warning: could not read pending Shipping Bypasser submissions: {exc}")
        return []
    submissions = payload.get("submissions") if isinstance(payload, dict) else payload
    return [item for item in submissions if isinstance(item, dict)] if isinstance(submissions, list) else []


def _write_pending_shipping_bypass_submissions(submissions, path=SHIPPING_BYPASS_PENDING_SUBMISSIONS_PATH):
    rows = [item for item in submissions if isinstance(item, dict)]
    temp_path = f"{path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump({"submissions": rows}, handle)
    os.replace(temp_path, path)


def _pending_shipping_bypass_submission(order_id, po):
    normalized_order_id = _normalize_target_order_id(order_id)
    normalized_po = str(po or "").strip().lower()
    if not normalized_order_id or not normalized_po:
        return None
    for item in reversed(_load_pending_shipping_bypass_submissions()):
        item_order_id = _normalize_target_order_id(item.get("order_id"))
        item_po = str(item.get("po") or "").strip().lower()
        if item_order_id == normalized_order_id and item_po == normalized_po and not item.get("crm_recorded_at"):
            return item
    return None


def _remember_pending_shipping_bypass_submission(order_id, po, sanmar_confirmation, vendor_name="Sanmar"):
    normalized_order_id = _normalize_target_order_id(order_id)
    normalized_po = str(po or "").strip()
    if not normalized_order_id or not normalized_po:
        return None
    submissions = _load_pending_shipping_bypass_submissions()
    replacement = {
        "order_id": normalized_order_id,
        "po": normalized_po,
        "vendor": _manual_order_vendor_label(vendor_name),
        "sanmar_confirmation": sanmar_confirmation if isinstance(sanmar_confirmation, dict) else {},
        "submitted_at": datetime.now().isoformat(),
    }
    kept = [
        item for item in submissions
        if not (
            _normalize_target_order_id(item.get("order_id")) == normalized_order_id
            and str(item.get("po") or "").strip().lower() == normalized_po.lower()
            and not item.get("crm_recorded_at")
        )
    ]
    kept.append(replacement)
    _write_pending_shipping_bypass_submissions(kept)
    return replacement


def _mark_pending_shipping_bypass_submission_recorded(order_id, po, record_state):
    normalized_order_id = _normalize_target_order_id(order_id)
    normalized_po = str(po or "").strip().lower()
    if not normalized_order_id or not normalized_po:
        return False
    submissions = _load_pending_shipping_bypass_submissions()
    changed = False
    for item in submissions:
        if (
            _normalize_target_order_id(item.get("order_id")) == normalized_order_id
            and str(item.get("po") or "").strip().lower() == normalized_po
            and not item.get("crm_recorded_at")
        ):
            item["crm_recorded_at"] = datetime.now().isoformat()
            item["crm_record_state"] = str(record_state or "")
            changed = True
    if changed:
        _write_pending_shipping_bypass_submissions(submissions)
    return changed


def _normalize_text(value):
    return " ".join(str(value or "").replace("\xa0", " ").split())


def _upper_key(value):
    return re.sub(r"[^A-Z0-9]+", "", str(value or "").upper())


def _sanmar_color_word_aliases():
    word_aliases = {}
    for source in (BELLA_CANVAS_CRM_COLOR_WORD_ALIASES, SANMAR_CRM_COLOR_WORD_ALIASES):
        for word, alias_values in source.items():
            word_key = _upper_key(word)
            if not word_key:
                continue
            existing = list(word_aliases.get(word_key, ()))
            for alias in alias_values:
                alias_key = _upper_key(alias)
                if alias_key and alias_key not in existing:
                    existing.append(alias_key)
            word_aliases[word_key] = tuple(existing)
    return word_aliases


def _color_variant_keys(color_name, word_aliases):
    tokens = re.findall(r"[A-Z0-9]+", str(color_name or "").upper())
    variants = [()]
    for token in tokens:
        options = (token,) + tuple(word_aliases.get(token, ()))
        variants = [prefix + (option,) for prefix in variants for option in options]
    return {
        _upper_key(" ".join(parts))
        for parts in variants
        if any(str(part or "").strip() for part in parts)
    }


def _bella_canvas_color_variant_keys(color_name):
    return _color_variant_keys(color_name, BELLA_CANVAS_CRM_COLOR_WORD_ALIASES)


def _build_bella_canvas_color_aliases():
    aliases = {}
    for color_name in BELLA_CANVAS_SANMAR_COLOR_NAMES:
        label = _normalize_text(color_name)
        for key in _bella_canvas_color_variant_keys(label):
            aliases.setdefault(key, []).append(label)
    return {
        key: list(dict.fromkeys(labels))
        for key, labels in aliases.items()
        if key and labels
    }


BELLA_CANVAS_COLOR_ALIASES = _build_bella_canvas_color_aliases()


def _build_known_sanmar_color_aliases():
    aliases = {}
    word_aliases = _sanmar_color_word_aliases()
    for color_name in SANMAR_KNOWN_COLOR_NAMES:
        label = _normalize_text(color_name)
        for key in _color_variant_keys(label, word_aliases):
            aliases.setdefault(key, []).append(label)
    return {
        key: list(dict.fromkeys(labels))
        for key, labels in aliases.items()
        if key and labels
    }


SANMAR_KNOWN_COLOR_ALIASES = _build_known_sanmar_color_aliases()


def _sanmar_slash_color_alias_labels(color):
    label = _normalize_text(color)
    if "/" not in label:
        return []
    parts = [_normalize_text(part) for part in re.split(r"\s*/\s*", label) if _normalize_text(part)]
    if len(parts) < 2:
        return []
    return list(dict.fromkeys((
        "/".join(parts),
        "/ ".join(parts),
        " / ".join(parts),
    )))


def _sanmar_product_color_alias_labels(product, color):
    wanted = _upper_key(color)
    if not wanted:
        return []
    product_keys = []
    if isinstance(product, dict):
        product_keys.extend(_upper_key(product.get(name)) for name in ("product_id", "search_id"))
    else:
        product_keys.append(_upper_key(product))
    labels = []
    for product_key in product_keys:
        if product_key:
            labels.extend(SANMAR_PRODUCT_COLOR_ALIASES.get((product_key, wanted), []))
    return list(dict.fromkeys(_normalize_text(label) for label in labels if _normalize_text(label)))


def _sanmar_color_alias_labels(color, product=None):
    wanted = _upper_key(color)
    labels = []
    labels.extend(_sanmar_slash_color_alias_labels(color))
    labels.extend(_sanmar_product_color_alias_labels(product, color))
    labels.extend(SANMAR_COLOR_ALIASES.get(wanted, []))
    labels.extend(SANMAR_KNOWN_COLOR_ALIASES.get(wanted, []))
    labels.extend(BELLA_CANVAS_COLOR_ALIASES.get(wanted, []))
    return list(dict.fromkeys(_normalize_text(label) for label in labels if _normalize_text(label)))


def _sanmar_color_missing_letter_limit(shorter_key, longer_key):
    shorter_len = len(shorter_key or "")
    longer_len = len(longer_key or "")
    if shorter_len < 3 or longer_len < shorter_len:
        return 0
    if longer_len <= 6:
        return 2
    if longer_len <= 12:
        return 3
    if longer_len <= 24:
        return 4
    return 5


def _is_ordered_subsequence(shorter_key, longer_key):
    if not shorter_key:
        return False
    index = 0
    for char in longer_key:
        if index < len(shorter_key) and shorter_key[index] == char:
            index += 1
    return index == len(shorter_key)


def _color_word_abbreviation_variants(word, word_aliases=None):
    token = _upper_key(word)
    if not token:
        return set()
    aliases = word_aliases if word_aliases is not None else _sanmar_color_word_aliases()
    variants = {token}
    variants.update(_upper_key(alias) for alias in aliases.get(token, ()) if _upper_key(alias))
    if len(token) >= 5:
        variants.add(token[:4])
    compact = token[0] + "".join(char for char in token[1:] if char not in "AEIOU")
    if len(compact) >= 3 or compact in variants:
        variants.add(compact)
    return {variant for variant in variants if variant}


def _color_phrase_abbreviation_keys(value, max_keys=512):
    tokens = re.findall(r"[A-Z0-9]+", str(value or "").upper())
    if not tokens:
        return set()
    if len(tokens) > 6:
        key = _upper_key(value)
        return {key} if key else set()
    word_aliases = _sanmar_color_word_aliases()
    phrase_keys = {""}
    for token in tokens:
        variants = sorted(_color_word_abbreviation_variants(token, word_aliases))
        if not variants:
            continue
        next_keys = set()
        for prefix in phrase_keys:
            for variant in variants:
                next_keys.add(prefix + variant)
                if len(next_keys) >= max_keys:
                    break
            if len(next_keys) >= max_keys:
                break
        phrase_keys = next_keys
    return {key for key in phrase_keys if key}


def _sanmar_color_keys_match(actual_key, wanted_key):
    actual = _upper_key(actual_key)
    wanted = _upper_key(wanted_key)
    if not actual or not wanted:
        return False
    if actual == wanted:
        return True
    if wanted == "NAVY" and actual.endswith("NAVY"):
        return True
    if wanted == "ROYAL" and actual.endswith("ROYAL"):
        return True
    actual_abbreviations = _color_phrase_abbreviation_keys(actual_key)
    wanted_abbreviations = _color_phrase_abbreviation_keys(wanted_key)
    if wanted in actual_abbreviations or actual in wanted_abbreviations:
        return True
    if actual_abbreviations.intersection(wanted_abbreviations):
        return True
    shorter, longer = (actual, wanted) if len(actual) <= len(wanted) else (wanted, actual)
    missing_count = len(longer) - len(shorter)
    limit = _sanmar_color_missing_letter_limit(shorter, longer)
    return bool(limit and missing_count <= limit and _is_ordered_subsequence(shorter, longer))


def _sanmar_color_match_keys(color, product=None):
    wanted = _upper_key(color)
    keys = [wanted] if wanted else []
    keys.extend(_upper_key(alias) for alias in _sanmar_color_alias_labels(color, product=product))
    return list(dict.fromkeys(key for key in keys if key))


def _sanmar_color_label_options(color, product=None):
    labels = [_normalize_text(color)] if _normalize_text(color) else []
    labels.extend(_sanmar_color_alias_labels(color, product=product))
    return list(dict.fromkeys(label for label in labels if label))


def _parse_crm_date(value):
    text = _normalize_text(value)
    for pattern in (r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b", r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b"):
        match = re.search(pattern, text)
        if not match:
            continue
        try:
            if pattern.startswith(r"\b(\d{4})"):
                year, month, day = [int(part) for part in match.groups()]
            else:
                month, day, year = [int(part) for part in match.groups()]
                if year < 100:
                    year += 2000
            return datetime(year, month, day).date()
        except ValueError:
            continue
    return None


def _next_business_day_on_or_after(day):
    if day is None:
        return None
    target = day
    while target.weekday() >= 5:
        target += timedelta(days=1)
    return target


def _next_business_day_after(day):
    return _next_business_day_on_or_after(day + timedelta(days=1))


def _shipping_bypasser_production_target_for_eta(eta):
    return _next_business_day_on_or_after(eta)


ORDER_TOTALS_SHIPPING_VALUE_PATTERN = re.compile(
    r"\bShipping:\s*(?:\S+\s+){0,2}?(?P<value>Free|\$?\s*[0-9][0-9,]*(?:\.\d{2})?)\b",
    re.IGNORECASE,
)


def _order_shipping_class_from_text(text):
    match = ORDER_TOTALS_SHIPPING_VALUE_PATTERN.search(_normalize_text(text))
    if not match:
        return ""
    value = _normalize_text(match.group("value"))
    if value.lower() == "free":
        return "free"
    amount_text = re.sub(r"[^0-9.]", "", value)
    if not amount_text:
        return ""
    try:
        return "rush" if float(amount_text) > 0 else "free"
    except ValueError:
        return ""


def _format_date_for_crm(date_value):
    return date_value.strftime("%Y-%m-%d")


def _format_date_for_crm_input(date_value):
    return date_value.strftime("%m/%d/%y")


def _json_safe(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "isoformat") and value.__class__.__name__ == "date":
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    return value


def _result(order_id, success, outcome, message, **extra):
    payload = {
        "order_id": order_id,
        "success": bool(success),
        "outcome": outcome,
        "message": str(message),
        "manual_review_required": not bool(success),
    }
    payload.update(_json_safe(extra))
    return payload


CRM_ORDER_DATA_SCRIPT = r"""
const normalize = (value) => String(value || '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
function isVisible(node) {
  if (!node) return false;
  const rect = node.getBoundingClientRect && node.getBoundingClientRect();
  if (rect && ((rect.width || 0) <= 0 || (rect.height || 0) <= 0)) return false;
  for (let current = node; current; current = current.parentElement) {
    const style = window.getComputedStyle(current);
    if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return false;
  }
  return true;
}
function ownText(node) {
  return normalize(Array.from(node.childNodes || [])
    .filter((child) => child.nodeType === Node.TEXT_NODE)
    .map((child) => child.textContent)
    .join(' '));
}
const bodyText = normalize(document.body && (document.body.innerText || document.body.textContent));
const all = Array.from(document.querySelectorAll('body *')).filter(isVisible).map((node) => {
  const rect = node.getBoundingClientRect();
  return {
    node,
    text: normalize(node.innerText || node.textContent || node.value || ''),
    own: ownText(node),
    tag: String(node.tagName || '').toLowerCase(),
    x: rect.left || 0,
    y: rect.top || 0,
    w: rect.width || 0,
    h: rect.height || 0,
  };
});
const pieces = all
  .map((item) => ({...item, text: item.own || item.text}))
  .filter((item) => item.text && item.text.length <= 160);
const heading = pieces.find((item) => /items on this design/i.test(item.text));
const headingY = heading ? heading.y : 0;
const after = pieces.filter((item) => item.y >= headingY - 20);
function activePanelTextFor(headingItem) {
  if (!headingItem || !headingItem.node) return '';
  let best = '';
  for (let node = headingItem.node; node && node !== document.body; node = node.parentElement) {
    const text = normalize(node.innerText || node.textContent || '');
    if (!/items on this design/i.test(text)) continue;
    if (!/(Alpha Stock|Stock Status|Order Goods|Manual Order)/i.test(text)) continue;
    if (!best || text.length < best.length) best = text;
  }
  return best;
}
function findLabel(label) {
  const rx = new RegExp('^' + label + ':?$', 'i');
  return after.filter((item) => rx.test(item.text)).sort((a, b) => a.y - b.y || a.x - b.x)[0] || null;
}
function detailValue(block, label) {
  const directRx = new RegExp('^' + label + '\\s*:\\s*(.+)$', 'i');
  const direct = block.find((item) => directRx.test(item.text));
  if (direct) return normalize(direct.text.match(directRx)[1]);
  const labelRx = new RegExp('^' + label + '\\s*:?$', 'i');
  const labelItem = block.find((item) => labelRx.test(item.text));
  if (!labelItem) return '';
  const sameRow = block
    .filter((item) => item !== labelItem && Math.abs(item.y - labelItem.y) < 18 && item.x > labelItem.x + 10)
    .sort((a, b) => a.x - b.x);
  return sameRow.length ? normalize(sameRow[0].text) : '';
}
function extractItemBlock(stockLine, blockEndY) {
  const block = after.filter((item) => item.y >= stockLine.y - 5 && item.y < blockEndY);
  const styleSubStyle = detailValue(block, 'Style');
  const styleSubColor = detailValue(block, 'Color');
  const styleSubDescription = detailValue(block, 'Description');
  const sizeLabel = block.find((item) => /^Size:?$/i.test(item.text) && item.y >= stockLine.y)
    || block.find((item) => /^Size:?$/i.test(item.text));
  const qtyLabel = block.find((item) => /^Quantity:?$/i.test(item.text) && item.y >= (sizeLabel ? sizeLabel.y : stockLine.y))
    || block.find((item) => /^Quantity:?$/i.test(item.text));
  const priceLabel = block.find((item) => /^Price:?$/i.test(item.text) && item.y >= (qtyLabel ? qtyLabel.y : stockLine.y))
    || block.find((item) => /^Price:?$/i.test(item.text));
  let sizes = [];
  let quantities = {};
  if (sizeLabel && qtyLabel) {
    const sizeRow = block.filter((item) =>
      Math.abs(item.y - sizeLabel.y) < 18 && item.x > sizeLabel.x + 20 && /^[A-Z0-9/ -]{1,12}$/.test(item.text)
    ).sort((a, b) => a.x - b.x);
    sizes = sizeRow.map((item) => ({ size: item.text.toUpperCase().replace(/\s+/g, ' '), x: item.x + item.w / 2 }));
    const qtyFloor = qtyLabel.y - 16;
    const qtyCeiling = priceLabel ? priceLabel.y - 4 : qtyLabel.y + 40;
    const qtyCells = block.filter((item) =>
      item.x > qtyLabel.x + 20 && item.y >= qtyFloor && item.y <= qtyCeiling && /^\d+$/.test(item.text)
    ).sort((a, b) => a.x - b.x);
    for (const cell of qtyCells) {
      if (!sizes.length) continue;
      const cx = cell.x + cell.w / 2;
      let best = sizes[0];
      let bestDistance = Math.abs(cx - best.x);
      for (const size of sizes) {
        const distance = Math.abs(cx - size.x);
        if (distance < bestDistance) {
          best = size;
          bestDistance = distance;
        }
      }
      if (best && bestDistance < 45) quantities[best.size] = Number(cell.text);
    }
  }
  let color = '';
  const total = after.find((item) => /total quantity/i.test(item.text) && Math.abs(item.y - stockLine.y) < 80);
  const colorCandidates = block.filter((item) => {
    if (item.y < stockLine.y - 20 || item.y > stockLine.y + 80) return false;
    if (item.x <= stockLine.x + 50) return false;
    if (/total quantity|quantity|subtotal|ink colors|impressions/i.test(item.text)) return false;
    if (total && item.x >= total.x - 20) return false;
    return /^[A-Z][A-Za-z0-9 /-]{1,40}$/.test(item.text);
  }).sort((a, b) => a.y - b.y || a.x - b.x);
  color = styleSubColor || (colorCandidates.length ? colorCandidates[0].text : '');
  if (!color) {
    const richLine = after.find((item) =>
      Math.abs(item.y - stockLine.y) < 40
      && item.text.includes(stockLine.text)
      && /Alpha Stock\s+(.+?)\s+Total Quantity/i.test(item.text)
    );
    const richMatch = richLine ? richLine.text.match(/Alpha Stock\s+(.+?)\s+Total Quantity/i) : null;
    if (richMatch) color = normalize(richMatch[1]);
  }
  return {
    stockLine: stockLine.text,
    color,
    styleSubStyle,
    styleSubColor,
    styleSubDescription,
    quantities,
    sizes: sizes.map((item) => item.size),
  };
}
const rawStockLines = after.filter((item) => /\b[A-Z0-9]{2,}\b.*-\s*Alpha Stock/i.test(item.text) || /\b[A-Z0-9]{2,}\b.*Alpha Stock/i.test(item.text))
  .filter((item) => !/^Items on this design\b/i.test(item.text))
  .filter((item) => !/\b(Size|Quantity|Price):/i.test(item.text))
  .sort((a, b) => a.y - b.y || a.x - b.x);
const stockLines = [];
for (const line of rawStockLines) {
  const duplicate = stockLines.find((existing) =>
    existing.text === line.text && Math.abs(existing.y - line.y) < 28
  );
  if (!duplicate) stockLines.push(line);
}
const items = [];
for (let i = 0; i < stockLines.length; i += 1) {
  const next = stockLines[i + 1];
  items.push(extractItemBlock(stockLines[i], next ? next.y - 8 : Number.POSITIVE_INFINITY));
}
const firstItem = items[0] || { stockLine: '', color: '', quantities: {}, sizes: [] };
const activeTab = pieces.find((item) => /(Design Previews|View Proofs)/i.test(item.text) && /\bQTY\s*:/i.test(item.text));
return {
  bodyText,
  stockLine: firstItem.stockLine,
  color: firstItem.color,
  quantities: firstItem.quantities,
  sizes: firstItem.sizes,
  items,
  activeTabText: activeTab ? activeTab.text : '',
  activePanelText: activePanelTextFor(heading),
};
"""


def _extract_order_data(driver, order_id, tab_context=None):
    if refresh_if_crm_challenge_attempts_exceeded(driver, f"Shipping Bypasser CRM order {order_id}", top_level=False):
        time.sleep(1)
    data = driver.execute_script(CRM_ORDER_DATA_SCRIPT)
    if not isinstance(data, dict):
        data = {}
    tab_context = tab_context if isinstance(tab_context, dict) else {}
    body_text = _normalize_text(data.get("bodyText"))
    due_match = re.search(r"Due Date:\s*([0-9/-]+)", body_text, flags=re.I)
    prod_match = re.search(r"Production Date:\s*([0-9/-]+)", body_text, flags=re.I)
    due_date = _parse_crm_date(due_match.group(1) if due_match else "")
    production_date = _parse_crm_date(prod_match.group(1) if prod_match else "")
    tab_text = _normalize_text(data.get("activeTabText"))
    context_text = _normalize_text(" ".join(str(tab_context.get(key) or "") for key in ("label", "tab_label", "stock_tab_label")))
    active_panel_text = _normalize_text(data.get("activePanelText"))
    po_match = (
        re.search(r"\b(H-[A-Za-z0-9]+)\b", context_text, flags=re.I)
        or re.search(r"\b(H-[A-Za-z0-9]+)\b", tab_text, flags=re.I)
        or re.search(r"\b(H-[A-Za-z0-9]+)\b", body_text, flags=re.I)
    )
    po = po_match.group(1) if po_match else (_po_from_tab_label(context_text) or _po_from_tab_label(tab_text))
    raw_items = data.get("items") if isinstance(data.get("items"), list) else []
    if not raw_items:
        raw_items = [
            {
                "stockLine": data.get("stockLine"),
                "color": data.get("color"),
                "quantities": data.get("quantities"),
                "sizes": data.get("sizes"),
            }
        ]
    products = []
    seen_products = set()
    for index, raw_item in enumerate(raw_items, start=1):
        if not isinstance(raw_item, dict):
            continue
        stock_line = _normalize_text(raw_item.get("stockLine"))
        product_match = re.search(r"\b([A-Z0-9]{2,})\b\s+(.*?)(?:\s*-\s*Alpha Stock|$)", stock_line, flags=re.I)
        style_sub_id = _normalize_text(raw_item.get("styleSubStyle")).upper().replace(" ", "")
        is_style_sub = bool(re.search(r"\bStyle[_\s-]*Sub\b", stock_line, flags=re.I))
        product_id = re.sub(r"[^A-Z0-9-]+", "", style_sub_id) if is_style_sub and style_sub_id else (product_match.group(1).upper() if product_match else "")
        if not product_id or product_id == "ITEMS":
            continue
        product_name = (
            _normalize_text(raw_item.get("styleSubDescription"))
            if is_style_sub and raw_item.get("styleSubDescription")
            else _normalize_text(product_match.group(2) if product_match else stock_line)
        )
        quantities = {
            str(size).upper().replace(" ", ""): int(qty)
            for size, qty in (raw_item.get("quantities") or {}).items()
            if int(qty or 0) > 0
        }
        dedupe_key = (product_id, _normalize_text(raw_item.get("color")).upper(), tuple(sorted(quantities.items())))
        if dedupe_key in seen_products:
            continue
        seen_products.add(dedupe_key)
        is_a4 = bool(
            re.search(r"\bA4\b", f"{stock_line} {product_name}", flags=re.I)
            or re.fullmatch(r"N(?:B|W)\d{4}[A-Z]?", product_id)
        )
        product = {
            "index": index,
            "stock_line": stock_line,
            "product_id": product_id,
            "product_name": product_name,
            "is_a4": is_a4,
            "color": _normalize_text(raw_item.get("styleSubColor") if is_style_sub and raw_item.get("styleSubColor") else raw_item.get("color")),
            "quantities": quantities,
        }
        product["is_gildan"] = _is_gildan_product(product)
        products.append(product)
    subcontractor_match = re.search(r"Subcontractor:\s*([^|]+?)(?:\s*Preferred|\s*$)", body_text, flags=re.I)
    subcontractor = _normalize_text(subcontractor_match.group(1) if subcontractor_match else "")
    order_type = "mach6" if "mach 6" in subcontractor.lower() else "inhouse"
    shipping_class = _order_shipping_class_from_text(body_text) or "rush"
    first_product = products[0] if products else {}
    return {
        "order_id": order_id,
        "due_date": due_date,
        "production_date": production_date,
        "po": po,
        "product_id": first_product.get("product_id", ""),
        "product_name": first_product.get("product_name", ""),
        "is_a4": bool(first_product.get("is_a4")),
        "is_gildan": bool(first_product.get("is_gildan")),
        "color": first_product.get("color", ""),
        "quantities": first_product.get("quantities", {}),
        "products": products,
        "subcontractor": subcontractor,
        "order_type": order_type,
        "shipping_class": shipping_class,
        "stock_tab_index": tab_context.get("stock_tab_index"),
        "stock_tab_count": tab_context.get("stock_tab_count"),
        "stock_tab_label": _stock_tab_summary_label(context_text or tab_text),
        "active_panel_stock_ordered": _text_indicates_stock_already_ordered(active_panel_text),
    }


def _build_sanmar_driver(visible=False):
    resolved = os.path.abspath(SANMAR_PROFILE_PATH)
    if not visible:
        kill_stale_chrome(resolved, profile_label="SanMar")
    driver = build_chrome_driver(
        resolved,
        headless_mode=not bool(visible),
        page_load_timeout=CRM_PAGE_LOAD_TIMEOUT,
        script_timeout=max(CRM_ACTION_TIMEOUT, 20),
    )
    try:
        setattr(driver, "_shipping_bypasser_visible", bool(visible))
    except Exception:
        pass
    return driver


STOCK_TAB_SCRIPT = r"""
const targetIndex = arguments.length ? arguments[0] : null;
const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim();
function isVisible(node) {
  if (!node) return false;
  const rect = node.getBoundingClientRect();
  if ((rect.width || 0) <= 8 || (rect.height || 0) <= 8) return false;
  for (let current = node; current; current = current.parentElement) {
    const style = window.getComputedStyle(current);
    if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return false;
  }
  return true;
}
function tabText(node) {
  return normalize(node && (node.innerText || node.textContent));
}
function clickableFor(node) {
  let best = node;
  for (let current = node; current && current !== document.body; current = current.parentElement) {
    const attrs = [
      current.getAttribute && current.getAttribute('ng-click'),
      current.getAttribute && current.getAttribute('onclick'),
      current.getAttribute && current.getAttribute('role'),
    ].join(' ').toLowerCase();
    const tag = String(current.tagName || '').toLowerCase();
    const style = window.getComputedStyle(current);
    if (tag === 'a' || tag === 'button' || attrs.includes('click') || attrs.includes('tab') || attrs.includes('button') || style.cursor === 'pointer') {
      best = current;
      break;
    }
    const rect = current.getBoundingClientRect();
    const text = tabText(current);
    if (text === tabText(node) && rect.width <= 320 && rect.height <= 180) {
      best = current;
    }
  }
  return best;
}
function scoreTab(node) {
  const text = tabText(node);
  if (!/\b\d+\s*-\s*QTY\s*:?\s*\d+/i.test(text) || !/(Design Previews|View Proofs)/i.test(text)) return -1;
  const rect = node.getBoundingClientRect();
  let score = 1000 - text.length;
  if (node.querySelector && node.querySelector('input')) score += 100;
  if (rect.top < 450) score += 100;
  return score;
}
const candidates = Array.from(document.querySelectorAll('div,a,button,li,span'))
  .filter(isVisible)
  .map((node) => ({ node, score: scoreTab(node), text: tabText(node), rect: node.getBoundingClientRect() }))
  .filter((item) => item.score >= 0);
const bestByNumber = new Map();
for (const item of candidates) {
  const match = item.text.match(/\b(\d+)\s*-\s*QTY\s*:?\s*(\d+)/i);
  if (!match) continue;
  const tabNumber = Number(match[1]);
  const previous = bestByNumber.get(tabNumber);
  if (!previous || item.score > previous.score) bestByNumber.set(tabNumber, item);
}
const tabs = Array.from(bestByNumber.entries()).map(([tabNumber, item]) => {
  const element = clickableFor(item.node);
  const rect = element.getBoundingClientRect();
  const label = tabText(element) || item.text;
  return { element, label, tabNumber, top: rect.top || item.rect.top || 0, left: rect.left || item.rect.left || 0 };
}).sort((a, b) => a.tabNumber - b.tabNumber || a.top - b.top || a.left - b.left);
if (targetIndex === null || targetIndex === undefined) {
  return tabs.map((tab, index) => ({ index, tab_number: tab.tabNumber, label: tab.label, top: tab.top, left: tab.left }));
}
const tab = tabs[Number(targetIndex)];
return tab ? { element: tab.element, tab_number: tab.tabNumber, label: tab.label, count: tabs.length } : null;
"""


DESIGN_TAB_NUMBER_HINT_SCRIPT = r"""
const text = String((document.body && (document.body.innerText || document.body.textContent)) || '');
const numbers = [];
const rx = /\b(\d+)\s*-\s*QTY\s*:?\s*\d+\s+(?:Design Previews|View Proofs)\b/gi;
let match;
while ((match = rx.exec(text)) !== null) {
  const number = Number(match[1]);
  if (Number.isFinite(number) && !numbers.includes(number)) numbers.push(number);
}
return numbers.sort((a, b) => a - b);
"""


def _stock_tab_summary_label(label):
    text = " ".join(str(label or "").split())
    if not text:
        return ""
    prefix = re.split(r"\b(?:Design Previews|View Proofs)\b", text, maxsplit=1, flags=re.I)[0].strip()
    parts = [part.strip() for part in prefix.splitlines() if part.strip()]
    if parts:
        return " | ".join(parts[:2])
    return prefix[:120] if prefix else text[:120]


def _find_stock_tabs(driver):
    try:
        tabs = driver.execute_script(STOCK_TAB_SCRIPT)
    except Exception:
        return []
    return tabs if isinstance(tabs, list) else []


def _visible_design_tab_number_hints(driver):
    try:
        numbers = driver.execute_script(DESIGN_TAB_NUMBER_HINT_SCRIPT)
    except Exception:
        return []
    if not isinstance(numbers, list):
        return []
    result = []
    for value in numbers:
        try:
            number = int(value)
        except Exception:
            continue
        if number > 0 and number not in result:
            result.append(number)
    return result


def _activate_stock_tab(driver, tab_index):
    try:
        tab = driver.execute_script(STOCK_TAB_SCRIPT, int(tab_index))
    except Exception:
        tab = None
    if not isinstance(tab, dict) or tab.get("element") is None:
        return None
    _click_with_fallback(driver, tab["element"])
    time.sleep(0.7)
    return tab


def _text_indicates_stock_already_ordered(text):
    normalized = " ".join(str(text or "").lower().split())
    if not normalized:
        return False
    false_value = r"(?:false|no|not\s+ordered|unordered|0)"
    true_value = r"(?:ordered|true|yes|1)"
    if re.search(rf"\bstock\s+ordered\s*[:=]\s*{false_value}\b", normalized):
        return False
    if re.search(rf"\bstock\s+status\s*[:=]\s*{true_value}\b", normalized):
        return True
    if re.search(rf"\bstock\s*:\s*{true_value}\b", normalized):
        return True
    if re.search(rf"\bstock\s+ordered\s*[:=]\s*{true_value}\b", normalized):
        return True
    return False


def _page_indicates_stock_already_ordered(driver):
    try:
        text = driver.execute_script(
            "return String((document.body && (document.body.innerText || document.body.textContent)) || '');"
        )
    except Exception:
        text = ""
    return _text_indicates_stock_already_ordered(text)


def _po_from_tab_label(label):
    text = _normalize_text(label)
    if not text:
        return ""
    h_match = re.search(r"\b(H-[A-Za-z0-9]+)\b", text, flags=re.I)
    if h_match:
        return h_match.group(1)
    qty_match = re.search(r"^(.+?)\s+\d+\s*-\s*QTY\s*:?\s*\d+\b", text, flags=re.I)
    if qty_match:
        candidate = _normalize_text(qty_match.group(1))
        if re.search(r"[A-Za-z]", candidate) and len(candidate) <= 80:
            return candidate
    return ""


def _wait_for_text(driver, pattern, timeout=15):
    deadline = time.time() + timeout
    rx = re.compile(pattern, flags=re.I)
    while time.time() < deadline:
        try:
            text = driver.execute_script("return String(document.body && (document.body.innerText || document.body.textContent) || '');")
        except Exception:
            text = ""
        if rx.search(text):
            return text
        time.sleep(0.3)
    return ""


def _sanmar_selected_color_label(driver):
    script = r"""
const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim();
function isVisible(node) {
  if (!node) return false;
  const rect = node.getBoundingClientRect();
  if ((rect.width || 0) <= 0 || (rect.height || 0) <= 0) return false;
  for (let current = node; current; current = current.parentElement) {
    const style = window.getComputedStyle(current);
    if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return false;
  }
  return true;
}
const selectors = [
  '.color-selected',
  '[class*="color-selected"]',
  '[class*="selected-color"]',
  '[class*="ColorSelected"]'
];
for (const selector of selectors) {
  for (const node of Array.from(document.querySelectorAll(selector)).filter(isVisible)) {
    const clone = node.cloneNode(true);
    for (const removable of Array.from(clone.querySelectorAll('#js-prices-info-m,[id*="price"],[class*="price"]'))) {
      removable.remove();
    }
    const text = normalize(clone.innerText || clone.textContent || node.getAttribute('aria-label') || '');
    const match = text.match(/Color\s+selected\s*:?\s*(.+?)(?:\s+Show\s+all\s+colors|\s+Show\s+less\s+colors|$)/i);
    if (match && normalize(match[1])) return normalize(match[1]);
    const imageLabel = normalize((node.querySelector('img') || {}).alt || '');
    if (imageLabel) return imageLabel;
  }
}
const bodyText = normalize(document.body && (document.body.innerText || document.body.textContent) || '');
const bodyMatch = bodyText.match(/Color\s+selected\s*:?\s*(.+?)(?:\s+Show\s+all\s+colors|\s+Show\s+less\s+colors|$)/i);
return bodyMatch ? normalize(bodyMatch[1]) : '';
"""
    try:
        return _clean_sanmar_selected_color_label(driver.execute_script(script))
    except Exception:
        return ""


def _clean_sanmar_selected_color_label(value):
    label = _normalize_text(value)
    label = re.sub(r"\s+Add\s+to\s+shopping\s+box\s*$", "", label, flags=re.IGNORECASE)
    return _normalize_text(label)


def _sanmar_auth_state(driver):
    script = r"""
function isVisible(node) {
  if (!node) return false;
  const rect = node.getBoundingClientRect();
  if ((rect.width || 0) <= 0 || (rect.height || 0) <= 0) return false;
  for (let current = node; current; current = current.parentElement) {
    const style = window.getComputedStyle(current);
    if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return false;
  }
  return true;
}
const text = String(document.body && (document.body.innerText || document.body.textContent) || '');
const visiblePasswordInputs = Array.from(document.querySelectorAll('input[type="password"]')).filter(isVisible);
const visibleInputs = Array.from(document.querySelectorAll('input')).filter(isVisible);
const visibleText = String(document.body && (document.body.innerText || '') || '');
const loginControls = Array.from(document.querySelectorAll('button,a,input[type="button"],input[type="submit"],[role="button"]'))
  .filter(isVisible)
  .map((node) => String(node.innerText || node.textContent || node.value || node.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim())
  .filter(Boolean);
return {
  text,
  url: String(location.href || ''),
  hasPasswordInput: visiblePasswordInputs.length > 0,
  passwordFilled: visiblePasswordInputs.some((node) => String(node.value || '').length > 0),
  needsTwoFactor: /let'?s verify your email|verification code|two[-\s]*factor/i.test(visibleText),
  usernameFilled: visibleInputs.some((node) => {
    const type = String(node.type || '').toLowerCase();
    if (type === 'password' || type === 'hidden' || type === 'submit' || type === 'button') return false;
    return String(node.value || '').trim().length > 0;
  }),
  loginControls,
};
"""
    try:
        state = driver.execute_script(script)
        return state if isinstance(state, dict) else {}
    except Exception:
        return {}


def _sanmar_state_confirms_login(state):
    text = str((state or {}).get("text") or "")
    normalized = _normalize_text(text)
    if re.search(r"Welcome,\s*EZONLINE1", normalized, flags=re.I):
        return True
    if (state or {}).get("hasPasswordInput"):
        return False
    if re.search(r"\b(?:Sign\s*Out|Logout|Log\s*Out|My\s+Account|EZONLINE1)\b", normalized, flags=re.I):
        return True
    if re.search(r"\b(?:My\s+Shopping\s+Box|Shopping\s+Details|Continue\s+Checkout|Proceed\s+to\s+Checkout)\b", normalized, flags=re.I):
        return True
    return False


def _wait_for_sanmar_login_confirmed(driver, timeout=20):
    deadline = time.time() + timeout
    last_state = {}
    while time.time() < deadline:
        last_state = _sanmar_auth_state(driver)
        if _sanmar_state_confirms_login(last_state):
            return True
        time.sleep(0.3)
    return _sanmar_state_confirms_login(last_state)


def _sanmar_login_url():
    base = str(SANMAR_URL or "https://www.sanmar.com/").strip() or "https://www.sanmar.com/"
    return base.rstrip("/") + "/login"


def _sanmar_login_credentials():
    username = str(os.environ.get("SANMAR_USERNAME") or SANMAR_USERNAME or "").strip()
    password = str(os.environ.get("SANMAR_PASSWORD") or SANMAR_PASSWORD or "")
    return username, password


def _click_sanmar_autofilled_login(driver, timeout=8):
    script = r"""
function isVisible(node) {
  if (!node) return false;
  const rect = node.getBoundingClientRect();
  if ((rect.width || 0) <= 0 || (rect.height || 0) <= 0) return false;
  for (let current = node; current; current = current.parentElement) {
    const style = window.getComputedStyle(current);
    if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return false;
  }
  return true;
}
function label(node) {
  return String(node.innerText || node.textContent || node.value || node.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
}
const password = Array.from(document.querySelectorAll('input[type="password"]')).filter(isVisible).find((node) => String(node.value || '').length > 0);
if (!password) return { clicked: false, reason: 'password_not_filled' };
const root = password.closest('form') || password.closest('.modal,.dropdown-menu,.login,.account-login') || document;
const submit = Array.from(root.querySelectorAll('button,input[type="button"],input[type="submit"],a,[role="button"]'))
  .filter(isVisible)
  .find((node) => /^log\s*in$/i.test(label(node)) || /sign\s*in/i.test(label(node)))
  || Array.from(document.querySelectorAll('button,input[type="button"],input[type="submit"],a,[role="button"]'))
    .filter(isVisible)
    .find((node) => /^log\s*in$/i.test(label(node)) || /sign\s*in/i.test(label(node)));
if (!submit) return { clicked: false, reason: 'login_button_not_found' };
submit.click();
return { clicked: true };
"""
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        try:
            last = driver.execute_script(script)
        except Exception as exc:
            last = {"clicked": False, "reason": str(exc)}
        if isinstance(last, dict) and last.get("clicked"):
            return True
        time.sleep(0.4)
    return False


def _submit_sanmar_login_with_credentials(driver, timeout=8):
    username, password = _sanmar_login_credentials()
    if not username or not password:
        return False
    script = r"""
const username = arguments[0];
const password = arguments[1];
function isVisible(node) {
  if (!node) return false;
  const rect = node.getBoundingClientRect();
  if ((rect.width || 0) <= 0 || (rect.height || 0) <= 0) return false;
  for (let current = node; current; current = current.parentElement) {
    const style = window.getComputedStyle(current);
    if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return false;
  }
  return true;
}
function setValue(node, value) {
  if (!node) return false;
  node.focus();
  const proto = Object.getPrototypeOf(node);
  const descriptor = proto && Object.getOwnPropertyDescriptor(proto, 'value');
  if (descriptor && descriptor.set) descriptor.set.call(node, value);
  else node.value = value;
  node.dispatchEvent(new Event('input', { bubbles: true }));
  node.dispatchEvent(new Event('change', { bubbles: true }));
  return true;
}
function label(node) {
  return String(node.innerText || node.textContent || node.value || node.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
}
const forms = Array.from(document.querySelectorAll('#login-page-form,#login-header-form,form[action*="j_spring_security_check"],form'));
for (const form of forms) {
  const user = Array.from(form.querySelectorAll('input[name="j_username"],#j_username,#username,input[autocomplete="username"],input[type="text"],input[type="email"]')).find(isVisible);
  const pass = Array.from(form.querySelectorAll('input[name="j_password"],#j_password,#password,input[type="password"]')).find(isVisible);
  if (!user || !pass) continue;
  setValue(user, username);
  setValue(pass, password);
  const remember = Array.from(form.querySelectorAll('input[name="_spring_security_remember_me"],#remember_me_login,input[type="checkbox"]')).find(isVisible);
  if (remember && !remember.checked) remember.click();
  const submit = Array.from(form.querySelectorAll('button,input[type="submit"],input[type="button"],[role="button"]'))
    .filter(isVisible)
    .find((node) => /^log\s*in$/i.test(label(node)) || /sign\s*in/i.test(label(node)))
    || form.querySelector('button[type="submit"],input[type="submit"]');
  if (submit && isVisible(submit)) submit.click();
  else if (form.requestSubmit) form.requestSubmit();
  else form.submit();
  return { submitted: true };
}
return { submitted: false, reason: 'login_form_not_found' };
"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            result = driver.execute_script(script, username, password)
        except Exception:
            result = None
        if isinstance(result, dict) and result.get("submitted"):
            return True
        time.sleep(0.4)
    return False


def _ensure_sanmar_logged_in(driver):
    safe_get_with_partial_load(driver, SANMAR_URL, label="SanMar home")
    if _wait_for_sanmar_login_confirmed(driver, timeout=5):
        return True
    text = _wait_for_text(driver, r"(Welcome,\s*EZONLINE1|Log In|Shopping Box)", timeout=15)
    if re.search(r"Welcome,\s*EZONLINE1", text, flags=re.I) or _wait_for_sanmar_login_confirmed(driver, timeout=2):
        return True
    if _submit_sanmar_login_with_credentials(driver, timeout=4):
        if _wait_for_sanmar_login_confirmed(driver, timeout=25):
            return True
    if _click_sanmar_autofilled_login(driver, timeout=8):
        if _wait_for_sanmar_login_confirmed(driver, timeout=25):
            return True
    safe_get_with_partial_load(driver, _sanmar_login_url(), label="SanMar login")
    if _wait_for_sanmar_login_confirmed(driver, timeout=3):
        return True
    if _submit_sanmar_login_with_credentials(driver, timeout=8):
        if _wait_for_sanmar_login_confirmed(driver, timeout=25):
            return True
    if _click_sanmar_autofilled_login(driver, timeout=8):
        if _wait_for_sanmar_login_confirmed(driver, timeout=25):
            return True
    confirmed = _wait_for_sanmar_login_confirmed(driver, timeout=20)
    state = _sanmar_auth_state(driver)
    if not confirmed and state.get("needsTwoFactor"):
        print("SanMar is asking for email verification. Complete the verification in the Chrome window, then the worker will continue.")
    if not confirmed and bool(getattr(driver, "_shipping_bypasser_visible", False)):
        print("SanMar login is visible. Sign in as EZONLINE1 in the Chrome window; the worker will continue after login is confirmed.")
        confirmed = _wait_for_sanmar_login_confirmed(driver, timeout=180)
    if not confirmed:
        if state.get("needsTwoFactor"):
            raise RuntimeError("SanMar login needs email verification. Use Open SanMar Cart to complete verification, then rerun Shipping Bypasser.")
        raise RuntimeError("SanMar login was not confirmed as EZONLINE1. Set SANMAR_USERNAME and SANMAR_PASSWORD in config.py or use Open SanMar Cart to sign in, then rerun Shipping Bypasser.")
    return True


def _find_clickable_by_text(driver, pattern):
    script = r"""
const pattern = new RegExp(arguments[0], 'i');
const patternText = String(arguments[0] || '').toLowerCase();
const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim();
function isVisible(node) {
  if (!node) return false;
  const rect = node.getBoundingClientRect();
  if ((rect.width || 0) <= 0 || (rect.height || 0) <= 0) return false;
  for (let current = node; current; current = current.parentElement) {
    const style = window.getComputedStyle(current);
    if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return false;
  }
  return true;
}
function labelFor(node) {
  return normalize([
    node.innerText,
    node.textContent,
    node.value,
    node.getAttribute && node.getAttribute('aria-label'),
    node.getAttribute && node.getAttribute('title')
  ].filter(Boolean).join(' '));
}
const controls = Array.from(document.querySelectorAll([
  'button',
  'a',
  'input[type="button"]',
  'input[type="submit"]',
  '[role="button"]',
  '[ng-click]',
  '[onclick]',
  '.btn',
  '[class*="btn"]',
  '[class*="button"]'
].join(','))).filter(isVisible);
const textMatch = controls.find((node) => pattern.test(labelFor(node)));
if (textMatch) return textMatch;
if (patternText.includes('edit') && patternText.includes('order')) {
  const editMode = controls.find((node) => /editModeOn\s*\(/i.test(String(node.getAttribute && node.getAttribute('ng-click') || '')));
  if (editMode) return editMode;
}
if (patternText.includes('save') && patternText.includes('order')) {
  const saveOrder = controls.find((node) => /saveOrder\s*\(/i.test(String(node.getAttribute && node.getAttribute('ng-click') || '')));
  if (saveOrder) return saveOrder;
}
return null;
"""
    def _find_in_current_context():
        try:
            element = driver.execute_script(script, pattern)
            if element is not None:
                return element
        except Exception:
            pass
        return None

    element = _find_in_current_context()
    if element is not None:
        return element
    try:
        driver.switch_to.default_content()
    except Exception:
        pass
    element = _find_in_current_context()
    if element is not None:
        return element
    try:
        frames = list(driver.find_elements(By.XPATH, "//iframe | //frame") or [])
    except Exception:
        frames = []
    for frame in frames:
        try:
            driver.switch_to.default_content()
            driver.switch_to.frame(frame)
        except Exception:
            continue
        element = _find_in_current_context()
        if element is not None:
            return element
    try:
        driver.switch_to.default_content()
    except Exception:
        pass
    return None


def _sanmar_cart_has_items(driver):
    script = r"""
const text = String(document.body && (document.body.innerText || document.body.textContent) || '');
const normalized = text.replace(/\s+/g, ' ').trim();
const checkout = /\bCheckout\b/i.test(normalized);
const amount = /\bShopping Box\b\s*\$?\s*(?!0\.00)(\d+|\d+\.\d{2})/i.test(normalized);
const badge = Array.from(document.querySelectorAll('body *')).some((node) => {
  const t = String(node.innerText || node.textContent || '').trim();
  if (!/^\d+$/.test(t) || Number(t) <= 0) return false;
  const rect = node.getBoundingClientRect();
  return rect.top < 160 && rect.left > (window.innerWidth || 1200) * 0.45;
});
return { hasItems: Boolean(checkout && (amount || badge)), text: normalized.slice(0, 1000) };
"""
    state = driver.execute_script(script)
    return state if isinstance(state, dict) else {"hasItems": False}


def _accept_sanmar_alert_if_present(driver):
    try:
        driver.switch_to.alert.accept()
        time.sleep(0.5)
        return True
    except Exception:
        return False


def _click_sanmar_cart_remove_or_update(driver):
    script = r"""
const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim();
function isVisible(node) {
  if (!node) return false;
  const rect = node.getBoundingClientRect();
  if ((rect.width || 0) <= 0 || (rect.height || 0) <= 0) return false;
  for (let current = node; current; current = current.parentElement) {
    const style = window.getComputedStyle(current);
    if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return false;
  }
  return true;
}
function labelFor(node) {
  return normalize([
    node.innerText,
    node.textContent,
    node.value,
    node.getAttribute && node.getAttribute('aria-label'),
    node.getAttribute && node.getAttribute('title'),
    node.getAttribute && node.getAttribute('alt'),
    node.getAttribute && node.getAttribute('class')
  ].filter(Boolean).join(' '));
}
const controls = Array.from(document.querySelectorAll('button,a,input[type="button"],input[type="submit"],[role="button"]'))
  .filter(isVisible)
  .map((node) => ({ node, label: labelFor(node), rect: node.getBoundingClientRect() }))
  .filter((item) => /\b(remove|delete|trash|clear)\b/i.test(item.label))
  .sort((a, b) => a.rect.top - b.rect.top || a.rect.left - b.rect.left);
if (controls.length) {
  const confirm = controls.find((item) => /\b(yes[, ]*\s*delete\s*all|yes\s*delete|delete\s*all|yes[, ]*\s*remove|confirm)\b/i.test(item.label));
  if (confirm) {
    confirm.node.click();
    return { clicked: true, action: 'confirm_remove', label: confirm.label };
  }
  const removeAll = controls.find((item) => /\b(remove\s+all|clear\s+(?:shopping\s+)?box|clear\s+cart)\b/i.test(item.label));
  const control = removeAll || controls[0];
  control.node.click();
  return { clicked: true, action: removeAll ? 'remove_all' : 'remove', label: control.label };
}
const inputs = Array.from(document.querySelectorAll('input:not([type="hidden"])')).filter((node) => {
  if (!isVisible(node)) return false;
  const value = String(node.value || '').trim();
  if (!/^\d+$/.test(value) || Number(value) <= 0) return false;
  const label = labelFor(node);
  return /qty|quantity|size|cart|shopping|box/i.test(label) || Number(value) < 1000;
});
if (inputs.length) {
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
  for (const input of inputs) {
    input.focus();
    setter.call(input, '0');
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    input.dispatchEvent(new Event('blur', { bubbles: true }));
  }
  const update = Array.from(document.querySelectorAll('button,a,input[type="button"],input[type="submit"],[role="button"]'))
    .filter(isVisible)
    .map((node) => ({ node, label: labelFor(node) }))
    .find((item) => /\b(update|recalculate|refresh)\b/i.test(item.label));
  if (update) {
    update.node.click();
    return { clicked: true, action: 'zero_update', label: update.label, inputCount: inputs.length };
  }
  return { clicked: true, action: 'zero_only', label: '', inputCount: inputs.length };
}
return { clicked: false, action: 'none', label: '' };
"""
    result = driver.execute_script(script)
    return result if isinstance(result, dict) else {"clicked": False, "action": "none"}


def _clear_sanmar_cart(driver, order_id=None):
    label = f" after failed order {order_id}" if order_id else ""
    try:
        safe_get_with_partial_load(driver, SANMAR_CART_URL, label="SanMar cart cleanup")
        time.sleep(0.8)
        if not _sanmar_cart_has_items(driver).get("hasItems"):
            return {"attempted": True, "success": True, "message": f"SanMar cart was already empty{label}."}
        last_action = None
        for _ in range(8):
            action = _click_sanmar_cart_remove_or_update(driver)
            last_action = action
            _accept_sanmar_alert_if_present(driver)
            if action.get("action") == "remove_all":
                time.sleep(0.5)
                confirm_action = _click_sanmar_cart_remove_or_update(driver)
                _accept_sanmar_alert_if_present(driver)
                if confirm_action.get("clicked"):
                    last_action = {"opened": action, "confirmed": confirm_action}
            time.sleep(1.2)
            try:
                driver.refresh()
                time.sleep(0.8)
            except Exception:
                pass
            if not _sanmar_cart_has_items(driver).get("hasItems"):
                return {
                    "attempted": True,
                    "success": True,
                    "message": f"SanMar cart was cleared{label}.",
                    "last_action": last_action,
                }
            if not action.get("clicked"):
                break
        return {
            "attempted": True,
            "success": False,
            "message": f"SanMar cart could not be cleared{label}; use Open SanMar Cart for review.",
            "last_action": last_action,
        }
    except Exception as exc:
        return {
            "attempted": True,
            "success": False,
            "message": f"SanMar cart cleanup failed{label}: {exc}. Use Open SanMar Cart for review.",
            "error_type": type(exc).__name__,
        }


def _report_needs_cart_cleanup(report_items):
    for item in report_items if isinstance(report_items, list) else []:
        if not isinstance(item, dict):
            continue
        if item.get("success") or item.get("stop_run"):
            continue
        if isinstance(item.get("sanmar_cart_cleanup"), dict):
            continue
        if str(item.get("outcome") or "") == "sanmar_cart_not_empty":
            continue
        return True
    return False


def _attach_cart_cleanup(report_items, cleanup):
    cleanup = cleanup if isinstance(cleanup, dict) else {"attempted": False, "success": False, "message": ""}
    cleaned_message = str(cleanup.get("message") or "").strip()
    for item in report_items if isinstance(report_items, list) else []:
        if not isinstance(item, dict) or item.get("success") or item.get("stop_run"):
            continue
        item["sanmar_cart_cleanup"] = cleanup
        if cleaned_message:
            base = str(item.get("message") or "").strip()
            item["message"] = f"{base} {cleaned_message}".strip()


def _cleanup_after_failed_order(sanmar_driver, order_id, report_items):
    if not _report_needs_cart_cleanup(report_items):
        return True
    cleanup = _clear_sanmar_cart(sanmar_driver, order_id=order_id)
    _attach_cart_cleanup(report_items, cleanup)
    if cleanup.get("success"):
        return True
    report_items.append(
        _result(
            order_id,
            False,
            "sanmar_cart_cleanup_failed",
            cleanup.get("message") or "SanMar cart could not be cleared after a failed Shipping Bypasser order.",
            sanmar_cart_cleanup=cleanup,
            manual_review_required=True,
            retryable=False,
            stop_run=True,
        )
    )
    return False


def _should_stop_bypasser_batch(report_items):
    for item in report_items if isinstance(report_items, list) else []:
        if not isinstance(item, dict):
            continue
        if str(item.get("outcome") or "") == "sanmar_cart_not_empty":
            return True
    return False


def _click_sanmar_text_control(driver, text_pattern, timeout=12):
    script = r"""
const pattern = new RegExp(arguments[0], 'i');
const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim();
function isVisible(node) {
  if (!node) return false;
  const rect = node.getBoundingClientRect();
  if ((rect.width || 0) <= 0 || (rect.height || 0) <= 0) return false;
  for (let current = node; current; current = current.parentElement) {
    const style = window.getComputedStyle(current);
    if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return false;
  }
  return true;
}
const controls = Array.from(document.querySelectorAll('button,a,input[type="button"],input[type="submit"],[role="button"],.btn,[class*="btn"]')).filter(isVisible);
let best = null;
for (const control of controls) {
  const text = normalize(control.innerText || control.textContent || control.value || control.getAttribute('aria-label'));
  if (!pattern.test(text)) continue;
  const rect = control.getBoundingClientRect();
  const score = Math.round(rect.top || 0) + Math.round(rect.left || 0) + text.length;
  if (!best || score < best.score) best = { control, score, text };
}
if (!best) return { success: false };
best.control.scrollIntoView({ block: 'center', inline: 'center' });
best.control.click();
return { success: true, text: best.text };
"""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            result = driver.execute_script(script, text_pattern)
        except Exception as exc:
            last = exc
            time.sleep(0.3)
            continue
        if isinstance(result, dict) and result.get("success"):
            time.sleep(1.0)
            return True
        time.sleep(0.3)
    if last is not None:
        raise RuntimeError(f"SanMar button not found: {text_pattern}: {last}")
    raise RuntimeError(f"SanMar button not found: {text_pattern}")


def _sanmar_inventory_controls_visible(driver):
    script = r"""
function isVisible(node) {
  if (!node) return false;
  const rect = node.getBoundingClientRect();
  if ((rect.width || 0) <= 0 || (rect.height || 0) <= 0) return false;
  for (let current = node; current; current = current.parentElement) {
    const style = window.getComputedStyle(current);
    if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return false;
  }
  return true;
}
const text = String(document.body && (document.body.innerText || document.body.textContent) || '');
const hasInventoryText = /Color\s+selected|Add\s+to\s+shopping\s+box|Warehouse/i.test(text);
const hasQuantityInputs = Array.from(document.querySelectorAll('table input:not([type="hidden"])')).some(isVisible);
return Boolean(hasInventoryText && hasQuantityInputs);
"""
    try:
        return bool(driver.execute_script(script))
    except Exception:
        return False


def _click_sanmar_inventory_pricing_button(driver, timeout=1):
    try:
        return bool(_click_sanmar_text_control(driver, r"Check\s+inventory\s+and\s+pricing", timeout=timeout))
    except Exception:
        return False


def _sanmar_active_style_matches(driver, expected_style_keys):
    expected = [_upper_key(key) for key in (expected_style_keys or []) if _upper_key(key)]
    if not expected:
        return {"success": True, "matched": "", "candidates": []}
    script = r"""
const expected = new Set((arguments[0] || []).map((value) => String(value || '').toUpperCase().replace(/[^A-Z0-9]+/g, '')).filter(Boolean));
function isVisible(node) {
  if (!node) return false;
  const rect = node.getBoundingClientRect();
  if ((rect.width || 0) <= 0 || (rect.height || 0) <= 0) return false;
  if ((rect.top || 0) > 760) return false;
  for (let current = node; current; current = current.parentElement) {
    const style = window.getComputedStyle(current);
    if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return false;
  }
  return true;
}
const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim();
const key = (value) => normalize(value).toUpperCase().replace(/[^A-Z0-9]+/g, '');
function hasStyleToken(text, wanted) {
  const escaped = String(wanted || '').replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return new RegExp(`(^|[^A-Z0-9])${escaped}([^A-Z0-9]|$)`, 'i').test(String(text || ''));
}
const selector = 'h1,h2,h3,[class*="product"],[class*="style"],[data-testid*="product"],a,span,p,div';
const candidates = [];
for (const node of Array.from(document.querySelectorAll(selector)).filter(isVisible)) {
  const text = normalize(node.innerText || node.textContent || node.value || node.getAttribute('aria-label'));
  if (!text || text.length > 180) continue;
  const keyed = key(text);
  if (!/[0-9]/.test(keyed)) continue;
  candidates.push({ text, key: keyed });
}
for (const candidate of candidates) {
  for (const wanted of expected) {
    if (candidate.key === wanted || hasStyleToken(candidate.text, wanted)) {
      return { success: true, matched: wanted, candidate: candidate.text, candidates: candidates.slice(0, 12).map((item) => item.text) };
    }
  }
}
return { success: false, matched: '', candidates: candidates.slice(0, 12).map((item) => item.text) };
"""
    try:
        result = driver.execute_script(script, expected)
    except Exception as exc:
        return {"success": False, "matched": "", "candidates": [], "error": str(exc)}
    return result if isinstance(result, dict) else {"success": False, "matched": "", "candidates": []}


def _assert_sanmar_active_style(driver, expected_style_keys, search_id):
    state = _sanmar_active_style_matches(driver, expected_style_keys)
    if state.get("success"):
        return True
    expected = ", ".join(str(key) for key in expected_style_keys if key)
    candidates = ", ".join(str(item) for item in (state.get("candidates") or [])[:6])
    detail = f" Visible style candidates: {candidates}." if candidates else ""
    raise RuntimeError(f"SanMar active product did not match expected style {expected or search_id}.{detail}")


def _click_sanmar_product_result(driver, expected_style_keys):
    expected = [_upper_key(key) for key in (expected_style_keys or []) if _upper_key(key)]
    if not expected:
        return False
    script = r"""
const expected = new Set((arguments[0] || []).map((value) => String(value || '').toUpperCase().replace(/[^A-Z0-9]+/g, '')).filter(Boolean));
function isVisible(node) {
  if (!node) return false;
  const rect = node.getBoundingClientRect();
  if ((rect.width || 0) <= 0 || (rect.height || 0) <= 0) return false;
  for (let current = node; current; current = current.parentElement) {
    const style = window.getComputedStyle(current);
    if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return false;
  }
  return true;
}
const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim();
const key = (value) => normalize(value).toUpperCase().replace(/[^A-Z0-9]+/g, '');
for (const node of Array.from(document.querySelectorAll('a,button,span,div,h1,h2,h3')).filter(isVisible)) {
  const text = normalize(node.innerText || node.textContent || node.value || node.getAttribute('aria-label'));
  if (!text || !expected.has(key(text))) continue;
  let clickable = node.closest('a,button,[role="button"]');
  if (!clickable) {
    for (let current = node; current && current !== document.body; current = current.parentElement) {
      const links = Array.from(current.querySelectorAll('a,button,[role="button"]')).filter(isVisible);
      if (links.length) {
        clickable = links[0];
        break;
      }
      const attrs = [
        current.getAttribute && current.getAttribute('onclick'),
        current.getAttribute && current.getAttribute('ng-click'),
        current.getAttribute && current.getAttribute('role'),
      ].join(' ').toLowerCase();
      if (attrs.includes('click') || attrs.includes('button')) {
        clickable = current;
        break;
      }
    }
  }
  clickable = clickable || node;
  clickable.scrollIntoView({ block: 'center', inline: 'center' });
  clickable.click();
  return { success: true, text };
}
return { success: false };
"""
    try:
        result = driver.execute_script(script, expected)
    except Exception:
        return False
    if isinstance(result, dict) and result.get("success"):
        time.sleep(1.0)
        return True
    return False


def _ensure_sanmar_inventory_view(driver, force_click=False):
    deadline = time.time() + 20
    clicked_inventory_gate = False
    while time.time() < deadline:
        if _sanmar_inventory_controls_visible(driver):
            return True
        try:
            text = _normalize_text(driver.execute_script("return String(document.body && (document.body.innerText || document.body.textContent) || '');"))
        except Exception:
            text = ""
        if force_click or re.search(r"Check\s+inventory\s+and\s+pricing", text, flags=re.I):
            if _click_sanmar_inventory_pricing_button(driver, timeout=1):
                clicked_inventory_gate = True
                force_click = False
                time.sleep(0.8)
                continue
        time.sleep(0.4)
    if clicked_inventory_gate:
        raise RuntimeError("SanMar inventory view did not open after clicking Check inventory and pricing.")
    raise RuntimeError("SanMar inventory view did not open.")


def _search_sanmar_product(driver, search_id, click_inventory_button=False, expected_style_keys=None):
    input_el = None
    deadline = time.time() + 12
    while time.time() < deadline and input_el is None:
        try:
            inputs = driver.find_elements(By.CSS_SELECTOR, "input")
            for item in inputs:
                placeholder = str(item.get_attribute("placeholder") or "")
                if item.is_displayed() and ("product" in placeholder.lower() or "style" in placeholder.lower() or "pms" in placeholder.lower()):
                    input_el = item
                    break
        except Exception:
            pass
        if input_el is None:
            time.sleep(0.2)
    if input_el is None:
        raise RuntimeError("SanMar search input was not found.")
    input_el.send_keys(Keys.CONTROL, "a")
    input_el.send_keys(search_id)
    input_el.send_keys(Keys.ENTER)
    deadline = time.time() + 20
    expected_keys = [_upper_key(key) for key in (expected_style_keys or [search_id]) if _upper_key(key)]
    last_product_page_error = None
    while time.time() < deadline:
        try:
            text = _normalize_text(driver.execute_script("return String(document.body && (document.body.innerText || document.body.textContent) || '');"))
            url = str(driver.current_url or "")
        except Exception:
            text = ""
            url = ""
        product_page = bool(re.search(r"Inventory\s+and\s+Pricing|Color\s+selected|Add\s+to\s+shopping\s+box|Check\s+inventory\s+and\s+pricing", text, flags=re.I))
        cart_page = bool(re.search(r"My\s+Shopping\s+Box|Shopping\s+Details|Continue\s+Checkout", text, flags=re.I))
        page_key = _upper_key(f"{text} {url}")
        expected_seen = any(key in page_key for key in expected_keys) if expected_keys else True
        if expected_seen and product_page and not (cart_page and not product_page):
            try:
                _ensure_sanmar_inventory_view(driver, force_click=click_inventory_button)
                _assert_sanmar_active_style(driver, expected_keys, search_id)
                return True
            except Exception as exc:
                last_product_page_error = exc
                if _click_sanmar_product_result(driver, expected_keys):
                    continue
        if expected_seen and not product_page and _click_sanmar_product_result(driver, expected_keys):
            continue
        time.sleep(0.3)
    if last_product_page_error is not None:
        raise RuntimeError(f"SanMar product search did not settle on the inventory page for {search_id}: {last_product_page_error}")
    raise RuntimeError(f"SanMar product search did not open the inventory page for {search_id}.")


def _select_sanmar_color(driver, color, product=None):
    wanted_keys = _sanmar_color_match_keys(color, product=product)
    if not wanted_keys:
        raise RuntimeError("CRM stock color was not detected.")
    word_aliases = _sanmar_color_word_aliases()
    script = r"""
const wantedKeys = Array.isArray(arguments[0]) ? arguments[0] : [];
const wordAliases = arguments[1] || {};
const key = (value) => String(value || '').toUpperCase().replace(/[^A-Z0-9]+/g, '');
function tokenVariants(token) {
  const base = key(token);
  if (!base) return [];
  const variants = new Set([base]);
  for (const alias of (wordAliases[base] || [])) {
    const aliasKey = key(alias);
    if (aliasKey) variants.add(aliasKey);
  }
  if (base.length >= 5) variants.add(base.slice(0, 4));
  const compact = base.charAt(0) + Array.from(base.slice(1)).filter((char) => !'AEIOU'.includes(char)).join('');
  if (compact.length >= 3 || variants.has(compact)) variants.add(compact);
  return Array.from(variants).filter(Boolean);
}
function colorAbbreviationKeys(value) {
  const tokens = String(value || '').toUpperCase().match(/[A-Z0-9]+/g) || [];
  if (!tokens.length) return [];
  if (tokens.length > 6) {
    const base = key(value);
    return base ? [base] : [];
  }
  let keys = [''];
  for (const token of tokens) {
    const variants = tokenVariants(token);
    if (!variants.length) continue;
    const nextKeys = [];
    for (const prefix of keys) {
      for (const variant of variants) {
        nextKeys.push(prefix + variant);
        if (nextKeys.length >= 512) break;
      }
      if (nextKeys.length >= 512) break;
    }
    keys = nextKeys;
  }
  return keys.filter(Boolean);
}
function abbreviationKeysMatch(actualValue, wantedValue) {
  const actual = key(actualValue);
  const wanted = key(wantedValue);
  if (!actual || !wanted) return false;
  const actualKeys = new Set(colorAbbreviationKeys(actualValue));
  if (actualKeys.has(wanted)) return true;
  const wantedKeysForValue = new Set(colorAbbreviationKeys(wantedValue));
  if (wantedKeysForValue.has(actual)) return true;
  for (const candidate of actualKeys) {
    if (wantedKeysForValue.has(candidate)) return true;
  }
  return false;
}
function missingLetterLimit(shorter, longer) {
  const shorterLength = String(shorter || '').length;
  const longerLength = String(longer || '').length;
  if (shorterLength < 3 || longerLength < shorterLength) return 0;
  if (longerLength <= 6) return 2;
  if (longerLength <= 12) return 3;
  if (longerLength <= 24) return 4;
  return 5;
}
function isOrderedSubsequence(shorter, longer) {
  if (!shorter) return false;
  let index = 0;
  for (const char of String(longer || '')) {
    if (index < shorter.length && shorter[index] === char) index += 1;
  }
  return index === shorter.length;
}
function colorMatchScore(value) {
  const actual = key(value);
  if (!actual) return null;
  let best = null;
  for (const wanted of wantedKeys) {
    if (!wanted) continue;
    let score = null;
    if (actual === wanted) score = 0;
    else if (wanted === 'NAVY' && actual.endsWith('NAVY')) score = 20;
    else if (wanted === 'ROYAL' && actual.endsWith('ROYAL')) score = 20;
    else if (abbreviationKeysMatch(value, wanted)) score = 60;
    else {
      const shorter = actual.length <= wanted.length ? actual : wanted;
      const longer = actual.length <= wanted.length ? wanted : actual;
      const missingCount = longer.length - shorter.length;
      const limit = missingLetterLimit(shorter, longer);
      if (limit && missingCount <= limit && isOrderedSubsequence(shorter, longer)) {
        score = 100 + missingCount;
      }
    }
    if (score !== null && (best === null || score < best)) best = score;
  }
  return best;
}
function isVisible(node) {
  if (!node) return false;
  const rect = node.getBoundingClientRect();
  if ((rect.width || 0) <= 0 || (rect.height || 0) <= 0) return false;
  for (let current = node; current; current = current.parentElement) {
    const style = window.getComputedStyle(current);
    if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return false;
  }
  return true;
}
const nodes = Array.from(document.querySelectorAll('button,a,label,span,div,input')).filter(isVisible);
let best = null;
for (const node of nodes) {
  const text = node.getAttribute('title') || node.getAttribute('aria-label') || node.value || node.innerText || node.textContent || '';
  const colorScore = colorMatchScore(text);
  if (colorScore === null) continue;
  const rect = node.getBoundingClientRect();
  const clickable = node.closest('button,a,label') || node;
  const tag = String(clickable.tagName || '').toLowerCase();
  const component = String(clickable.getAttribute && clickable.getAttribute('data-component') || '').toLowerCase();
  const componentScore = component.includes('colorswatch') ? -5000 : 0;
  const tagScore = tag === 'a' || tag === 'button' || tag === 'label' ? 0 : 10000;
  const area = Math.max(1, Math.round((rect.width || 1) * (rect.height || 1)));
  const score = (colorScore * 100000) + componentScore + tagScore + area + Math.round(rect.top || 0) + String(text).length;
  if (!best || score < best.score) best = { node: clickable, score };
}
if (!best) return { success: false };
best.node.scrollIntoView({ block: 'center', inline: 'center' });
const href = best.node && best.node.tagName && String(best.node.tagName).toLowerCase() === 'a'
  ? best.node.getAttribute('href')
  : '';
if (href) {
  window.location.assign(href);
  return { success: true, navigated: true, href };
}
best.node.click();
return { success: true };
"""
    clicked = False
    last_error = None
    deadline = time.time() + 12
    while time.time() < deadline and not clicked:
        try:
            result = driver.execute_script(script, wanted_keys, word_aliases)
        except Exception as exc:
            last_error = exc
            time.sleep(0.3)
            continue
        clicked = bool(isinstance(result, dict) and result.get("success"))
        if not clicked:
            time.sleep(0.3)
    if not clicked:
        if last_error is not None:
            raise RuntimeError(f"SanMar color '{color}' was not found: {last_error}")
        raise RuntimeError(f"SanMar color '{color}' was not found.")
    selected_labels = _sanmar_color_label_options(color, product=product)
    selected_pattern = "|".join(re.escape(label) for label in selected_labels)
    text = _wait_for_text(driver, rf"Color\s+selected:\s*{selected_pattern}|Color\s+selected:.*{selected_pattern}", timeout=8)
    if not text and any(key in {"NAVY", "ROYAL"} for key in wanted_keys):
        text = _wait_for_text(driver, r"Color\s+selected:.*(Navy|Royal)", timeout=3)
    if not text:
        selected_color = _sanmar_selected_color_label(driver)
        if selected_color and _cart_color_matches(selected_color, color, product=product):
            time.sleep(0.7)
            return
        if selected_color:
            raise RuntimeError(
                f"SanMar selected color '{selected_color}' did not match CRM color '{color}'."
            )
        raise RuntimeError(f"SanMar did not confirm selected color '{color}'.")
    time.sleep(0.7)


def _sanmar_inventory(driver):
    script = r"""
const normalize = (value) => String(value || '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
function isVisible(node) {
  if (!node) return false;
  const rect = node.getBoundingClientRect();
  if ((rect.width || 0) <= 0 || (rect.height || 0) <= 0) return false;
  for (let current = node; current; current = current.parentElement) {
    const style = window.getComputedStyle(current);
    if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return false;
  }
  return true;
}
const warehouseAliases = [
  ['Robbinsville, NJ', ['Robbinsville, NJ', 'Robbinsville', 'NJ']],
  ['Richmond, VA', ['Richmond, VA', 'Richmond', 'VA']],
  ['Cincinnati, OH', ['Cincinnati, OH', 'Cincinnati', 'OH']],
  ['Jacksonville, FL', ['Jacksonville, FL', 'Jacksonville', 'FL']],
  ['Minneapolis, MN', ['Minneapolis, MN', 'Minneapolis', 'MN']],
  ['Dallas, TX', ['Dallas, TX', 'Dallas', 'TX']],
  ['Phoenix, AZ', ['Phoenix, AZ', 'Phoenix', 'AZ']],
  ['Reno, NV', ['Reno, NV', 'Reno', 'NV']],
  ['Seattle, WA', ['Seattle, WA', 'Seattle', 'WA']],
];
const sizePattern = /^(XS|S|M|L|XL|[2-6]XL|S\/M|L\/XL|2\/3X|4\/5X|YXS|YS|YM|YL|YXL|LT|XLT|[2-4]XT|ONE SIZE|OSFA|NB|[0-9]{1,2}M|[0-9]{1,2}-[0-9]{1,2}MOS|0003|0306|0612|1218|1824|[2-7]T|5\/6)$/;
function escapeRegExp(value) { return String(value || '').replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }
function warehouseFromText(text) {
  const haystack = normalize(text).toUpperCase();
  for (const [canonical, aliases] of warehouseAliases) {
    for (const alias of aliases) {
      const wanted = normalize(alias).toUpperCase();
      if (!wanted) continue;
      if (wanted.includes(',') && haystack.includes(wanted)) return canonical;
      if (wanted.length <= 3) {
        if (new RegExp(`\\b${escapeRegExp(wanted)}\\b`, 'i').test(haystack)) return canonical;
        continue;
      }
      if (haystack.includes(wanted)) return canonical;
    }
  }
  return '';
}
function cleanSize(value) {
  const text = normalize(value).toUpperCase().replace(/\s+/g, ' ');
  if (/^ONE SIZE$/.test(text)) return 'ONE SIZE';
  const compact = text.replace(/\s+/g, '');
  if (compact === 'NEWBORN') return 'NB';
  const compactInfantSizes = { '0003': '3M', '0306': '6M', '0612': '12M', '1218': '18M', '1824': '24M' };
  if (compactInfantSizes[compact]) return compactInfantSizes[compact];
  const infantMatch = compact.match(/^([0-9]{1,2}-[0-9]{1,2})(?:MO|MOS|MONTH|MONTHS)$/);
  if (infantMatch) return `${Number(infantMatch[1].split('-')[1])}M`;
  const infantSingleMatch = compact.match(/^([0-9]{1,2})(?:M|MO|MOS|MONTH|MONTHS)$/);
  if (infantSingleMatch) return `${Number(infantSingleMatch[1])}M`;
  const tallMatch = compact.match(/^([2-4])XLT$/);
  if (tallMatch) return `${tallMatch[1]}XT`;
  const comboMatch = compact.match(/^([24])X?L?\/([35])X?L?$/);
  if (comboMatch) return `${comboMatch[1]}/${comboMatch[2]}X`;
  const xMatch = compact.match(/^(X{2,6})L$/);
  if (xMatch) return `${xMatch[1].length}XL`;
  return compact;
}
const warehouseRows = [];
for (const tr of Array.from(document.querySelectorAll('tr')).filter(isVisible)) {
  const text = normalize(tr.innerText || tr.textContent);
  const warehouse = warehouseFromText(text);
  if (!warehouse) continue;
  const rect = tr.getBoundingClientRect();
  warehouseRows.push({
    warehouse,
    y: rect.top + rect.height / 2,
    stock: {},
  });
}
function nearestWarehouse(y) {
  let best = null;
  let bestDistance = Infinity;
  for (const row of warehouseRows) {
    const distance = Math.abs(row.y - y);
    if (distance < bestDistance) {
      best = row;
      bestDistance = distance;
    }
  }
  return bestDistance <= 12 ? best : null;
}
for (const table of Array.from(document.querySelectorAll('table')).filter(isVisible)) {
  if (!table.querySelector('input:not([type="hidden"])')) continue;
  const headerCells = Array.from(table.querySelectorAll('thead th, tr.headings td, tr.headings th, th.size-header, td.size-header'))
    .filter(isVisible)
    .map((cell) => {
      const rect = cell.getBoundingClientRect();
      return {
        size: cleanSize(cell.innerText || cell.textContent),
        x: rect.left + rect.width / 2,
        width: rect.width || 0,
      };
    });
  const header = headerCells.filter((item) => sizePattern.test(item.size));
  if (!header.length) continue;
  function nearestCellForHeader(cells, headerCell) {
    let best = null;
    let bestDistance = Infinity;
    for (const cell of cells) {
      const rect = cell.getBoundingClientRect();
      const center = rect.left + rect.width / 2;
      const distance = Math.abs(center - headerCell.x);
      if (distance < bestDistance) {
        best = { cell, distance, width: rect.width || 0 };
        bestDistance = distance;
      }
    }
    if (!best) return null;
    const allowedDistance = Math.max(28, (Number(headerCell.width) || 0) / 2 + (Number(best.width) || 0) / 2 + 12);
    return best.distance <= allowedDistance ? best.cell : null;
  }
  for (const tr of Array.from(table.querySelectorAll('tbody tr')).filter(isVisible)) {
    const inputs = Array.from(tr.querySelectorAll('input:not([type="hidden"])')).filter(isVisible);
    if (!inputs.length) continue;
    const rect = tr.getBoundingClientRect();
    const warehouse = nearestWarehouse(rect.top + rect.height / 2);
    if (!warehouse) continue;
    const cells = Array.from(tr.children || []).filter(isVisible);
    for (const headerCell of header) {
      const size = headerCell.size;
      if (!sizePattern.test(size)) continue;
      const cell = nearestCellForHeader(cells, headerCell);
      if (!cell) continue;
      const raw = normalize(cell.innerText || cell.textContent).replace(/,/g, '');
      const numberMatch = raw.match(/\d+/);
      warehouse.stock[size] = numberMatch ? Number(numberMatch[0]) : 0;
    }
  }
}
return warehouseRows.map((row) => ({ warehouse: row.warehouse, stock: row.stock }));
"""
    rows = driver.execute_script(script)
    return rows if isinstance(rows, list) else []


def _wait_for_sanmar_inventory(driver, search_id, timeout=12):
    deadline = time.time() + timeout
    last_rows = []
    while time.time() < deadline:
        try:
            rows = _sanmar_inventory(driver)
        except Exception:
            rows = []
        if rows:
            return rows
        last_rows = rows
        try:
            _ensure_sanmar_inventory_view(driver, force_click=False)
        except Exception:
            pass
        time.sleep(0.8)
    return last_rows


def _choose_warehouse(inventory, required, order_type):
    order = WAREHOUSE_DISTANCE.get(order_type) or WAREHOUSE_DISTANCE["inhouse"]
    by_name = {}
    for row in inventory:
        if not isinstance(row, dict):
            continue
        raw_name = _normalize_text(row.get("warehouse"))
        canonical = None
        for key, alias in WAREHOUSE_ALIASES.items():
            if key in raw_name.lower():
                canonical = alias
                break
        if canonical:
            by_name[canonical] = row
    for warehouse in order:
        row = by_name.get(warehouse)
        if not row:
            continue
        stock = row.get("stock") if isinstance(row.get("stock"), dict) else {}
        if all(_stock_qty_can_cover_order(stock.get(size, 0), qty) for size, qty in required.items()):
            return warehouse
    return None


def _inventory_by_warehouse(inventory):
    by_name = {}
    for row in inventory if isinstance(inventory, list) else []:
        if not isinstance(row, dict):
            continue
        raw_name = _normalize_text(row.get("warehouse"))
        canonical = None
        for key, alias in WAREHOUSE_ALIASES.items():
            if key in raw_name.lower():
                canonical = alias
                break
        if canonical:
            by_name[canonical] = row
    return by_name


def _stock_qty(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _stock_qty_can_cover_order(available, needed):
    needed = _stock_qty(needed)
    if needed <= 0:
        return True
    return _stock_qty(available) >= needed + SANMAR_WAREHOUSE_STOCK_BUFFER


def _choose_common_warehouse(product_lines, order_type):
    order = WAREHOUSE_DISTANCE.get(order_type) or WAREHOUSE_DISTANCE["inhouse"]
    lines = product_lines if isinstance(product_lines, list) else []
    for warehouse in order:
        can_fulfill = True
        for line in lines:
            inventory = _inventory_by_warehouse(line.get("inventory"))
            row = inventory.get(warehouse)
            if not row:
                can_fulfill = False
                break
            stock = row.get("stock") if isinstance(row.get("stock"), dict) else {}
            required = line.get("quantities") if isinstance(line.get("quantities"), dict) else {}
            if not all(_stock_qty_can_cover_order(stock.get(size, 0), qty) for size, qty in required.items()):
                can_fulfill = False
                break
        if can_fulfill:
            return warehouse
    return None


def _warehouse_priority(order_type):
    return WAREHOUSE_DISTANCE.get(order_type) or WAREHOUSE_DISTANCE["inhouse"]


def _warehouse_short_name(warehouse):
    mapping = {
        "Robbinsville, NJ": "NJ",
        "Richmond, VA": "VA",
        "Cincinnati, OH": "OH",
        "Jacksonville, FL": "FL",
        "Minneapolis, MN": "MN",
        "Dallas, TX": "TX",
        "Phoenix, AZ": "AZ",
        "Reno, NV": "NV",
        "Seattle, WA": "WA",
    }
    return mapping.get(str(warehouse or "").strip(), str(warehouse or "").strip() or "warehouse")


def _warehouse_match_options(warehouse):
    text = _normalize_text(warehouse)
    options = [text] if text else []
    if "," in text:
        city, state = [_normalize_text(part) for part in text.split(",", 1)]
        options.extend([city, state])
    short_name = _warehouse_short_name(text)
    options.append(short_name)
    return list(dict.fromkeys(option for option in options if option))


def _warehouse_available_qty(line, warehouse, size):
    inventory = _inventory_by_warehouse(line.get("inventory"))
    row = inventory.get(warehouse)
    stock = row.get("stock") if isinstance(row, dict) and isinstance(row.get("stock"), dict) else {}
    return _stock_qty(stock.get(size, 0))


def _warehouse_usable_qty(line, warehouse, size):
    return max(0, _warehouse_available_qty(line, warehouse, size) - SANMAR_WAREHOUSE_STOCK_BUFFER)


def _choose_multi_warehouse_plan(product_lines, order_type):
    lines = product_lines if isinstance(product_lines, list) else []
    priority = _warehouse_priority(order_type)
    expanded_lines = []
    pieces_by_warehouse = {}
    warehouses = []
    for line in lines:
        required = line.get("quantities") if isinstance(line.get("quantities"), dict) else {}
        allocations_by_warehouse = {}
        for size, raw_qty in required.items():
            needed = _stock_qty(raw_qty)
            if needed <= 0:
                continue
            remaining = needed
            full_warehouse = None
            for warehouse in priority:
                if _stock_qty_can_cover_order(_warehouse_available_qty(line, warehouse, size), needed):
                    full_warehouse = warehouse
                    break
            allocation_targets = [full_warehouse] if full_warehouse else priority
            for warehouse in allocation_targets:
                if not warehouse:
                    continue
                available = _warehouse_usable_qty(line, warehouse, size)
                qty = min(remaining, available)
                if qty <= 0:
                    continue
                allocations_by_warehouse.setdefault(warehouse, {})[size] = allocations_by_warehouse.setdefault(warehouse, {}).get(size, 0) + qty
                pieces_by_warehouse[warehouse] = pieces_by_warehouse.get(warehouse, 0) + qty
                if warehouse not in warehouses:
                    warehouses.append(warehouse)
                remaining -= qty
                if remaining <= 0:
                    break
            if remaining > 0:
                return None
        for warehouse, quantities in allocations_by_warehouse.items():
            expanded = dict(line)
            expanded["warehouse"] = warehouse
            expanded["quantities"] = quantities
            expanded["multi_warehouse_source_line"] = line
            expanded["cart_validation_key"] = f"{(line.get('product') or {}).get('index')}:{warehouse}"
            expanded_lines.append(expanded)
    if not expanded_lines:
        return None
    return {
        "mode": "multi_warehouse" if len(warehouses) > 1 else "single_warehouse",
        "warehouses": warehouses,
        "expanded_lines": expanded_lines,
        "pieces_by_warehouse": pieces_by_warehouse,
        "box_count": len(warehouses),
    }


def _single_warehouse_plan(product_lines, warehouse):
    lines = product_lines if isinstance(product_lines, list) else []
    return {
        "mode": "single_warehouse",
        "warehouses": [warehouse],
        "expanded_lines": [dict(line, warehouse=warehouse) for line in lines],
        "pieces_by_warehouse": {
            warehouse: sum(
                sum(int(qty or 0) for qty in (line.get("quantities") or {}).values())
                for line in lines
            )
        },
        "box_count": 1,
    }


def _choose_warehouse_plan(product_lines, order_type):
    warehouse = _choose_common_warehouse(product_lines, order_type)
    if warehouse:
        return warehouse, _single_warehouse_plan(product_lines, warehouse)
    return None, _choose_multi_warehouse_plan(product_lines, order_type)


def _single_warehouse_from_plan(warehouse, plan):
    warehouse = str(warehouse or "").strip()
    if warehouse:
        return warehouse
    if str((plan or {}).get("mode") or "") != "single_warehouse":
        return None
    warehouses = [
        str(item or "").strip()
        for item in ((plan or {}).get("warehouses") or [])
        if str(item or "").strip()
    ]
    return warehouses[0] if len(warehouses) == 1 else None


def _format_multi_warehouse_production_note(order, plan):
    tab_index = order.get("stock_tab_index") or 1
    box_count = int((plan or {}).get("box_count") or len((plan or {}).get("warehouses") or []) or 0)
    lines = [f"tab {tab_index}: {box_count} box{'es' if box_count != 1 else ''} from sanmar with the same PO"]
    pieces_by_warehouse = (plan or {}).get("pieces_by_warehouse") if isinstance((plan or {}).get("pieces_by_warehouse"), dict) else {}
    for warehouse in (plan or {}).get("warehouses") or []:
        pieces = int(pieces_by_warehouse.get(warehouse, 0) or 0)
        lines.append(f"{pieces} pc from {_warehouse_short_name(warehouse)}")
    return "\n".join(lines)


def _single_warehouse_failure_message(product_lines, order_type):
    order = WAREHOUSE_DISTANCE.get(order_type) or WAREHOUSE_DISTANCE["inhouse"]
    unavailable = []
    low_buffer = []
    partial = []
    for line in product_lines if isinstance(product_lines, list) else []:
        product = line.get("product") if isinstance(line, dict) else {}
        label = str(line.get("search_id") or product.get("product_id") or "").strip() or "product"
        inventory = _inventory_by_warehouse(line.get("inventory"))
        required = line.get("quantities") if isinstance(line.get("quantities"), dict) else {}
        for size, qty in required.items():
            needed = _stock_qty(qty)
            max_available = 0
            best_warehouse = ""
            for warehouse in order:
                row = inventory.get(warehouse)
                stock = row.get("stock") if isinstance(row, dict) and isinstance(row.get("stock"), dict) else {}
                available = _stock_qty(stock.get(size, 0))
                if available > max_available:
                    max_available = available
                    best_warehouse = warehouse
            if max_available < needed:
                detail = f"{label} {size} needs {needed}, max available {max_available}"
                if best_warehouse:
                    detail = f"{detail} at {best_warehouse}"
                unavailable.append(detail)
            elif not any(
                _stock_qty_can_cover_order(((inventory.get(warehouse) or {}).get("stock") or {}).get(size, 0), needed)
                for warehouse in order
            ):
                detail = f"{label} {size} needs {needed} plus {SANMAR_WAREHOUSE_STOCK_BUFFER} buffer, max available {max_available}"
                if best_warehouse:
                    detail = f"{detail} at {best_warehouse}"
                low_buffer.append(detail)
            elif not any(
                _stock_qty(((inventory.get(warehouse) or {}).get("stock") or {}).get(size, 0)) >= needed
                for warehouse in order
            ):
                partial.append(f"{label} {size} needs {needed}")
    if unavailable:
        return "No single SanMar warehouse can fulfill every product/size/quantity on this order. Stock unavailable: " + "; ".join(unavailable[:6]) + "."
    if low_buffer:
        return f"No SanMar warehouse can fulfill every product/size/quantity while leaving the {SANMAR_WAREHOUSE_STOCK_BUFFER}-piece stock buffer. Low buffer: " + "; ".join(low_buffer[:6]) + "."
    if partial:
        return "No single SanMar warehouse can fulfill every product/size/quantity on this order. Required sizes are split across warehouses."
    return "No single SanMar warehouse can fulfill every product/size/quantity on this order."


def _sanmar_size_key_for_product(product, size):
    text = str(size or "").upper().replace(" ", "")
    combo_size = SANMAR_COMBO_SIZE_ALIASES.get(text)
    if combo_size:
        return combo_size
    if text in {"ONESIZE", "OS", "OSFA"}:
        return "OSFA"
    compact_infant_sizes = {"0003": "3M", "0306": "6M", "0612": "12M", "1218": "18M", "1824": "24M"}
    if text in compact_infant_sizes:
        return compact_infant_sizes[text]
    month_range = re.fullmatch(r"([0-9]{1,2})-([0-9]{1,2})(?:MO|MOS|MONTH|MONTHS)", text)
    if month_range:
        return f"{int(month_range.group(2))}M"
    month_single = re.fullmatch(r"([0-9]{1,2})(?:M|MO|MOS|MONTH|MONTHS)", text)
    if month_single:
        return f"{int(month_single.group(1))}M"
    product_id = str((product or {}).get("product_id") or "").strip().upper()
    product_name = str((product or {}).get("product_name") or "")
    is_youth_style = (
        product_id.startswith("Y")
        or product_id.endswith("B")
        or bool(re.search(r"\b(kid|kids|youth|child|children)\b", product_name, flags=re.I))
    )
    if is_youth_style and text in {"YXS", "YS", "YM", "YL", "YXL"}:
        return text[1:]
    return text


def _sanmar_quantities_for_product(product):
    normalized = {}
    quantities = (product or {}).get("quantities")
    for raw_size, raw_qty in (quantities if isinstance(quantities, dict) else {}).items():
        try:
            qty = int(raw_qty or 0)
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        size = _sanmar_size_key_for_product(product, raw_size)
        normalized[size] = normalized.get(size, 0) + qty
    return normalized


def _gildan_sanmar_style_id(product_id):
    raw_style = _upper_key(product_id)
    if not raw_style:
        return ""
    if raw_style in SANMAR_GILDAN_STYLE_IDS:
        return raw_style
    match = re.fullmatch(r"G([0-9]+)([A-Z0-9]*)", raw_style)
    if not match:
        return ""
    number, suffix = match.groups()
    candidates = [
        f"{number}{suffix}",
        f"{number}0{suffix}",
        f"{number}00{suffix}",
    ]
    if number == "500":
        candidates.insert(0, f"5000{suffix}")
    if number == "640":
        candidates.insert(0, f"64000{suffix}")
    for candidate in candidates:
        if candidate in SANMAR_GILDAN_STYLE_IDS:
            return candidate
    return ""


def _is_gildan_product(product):
    source_text = " ".join(
        str((product or {}).get(name) or "")
        for name in ("stock_line", "product_name", "product_id")
    )
    return bool(
        re.search(r"\bGildan\b", source_text, flags=re.I)
        or _gildan_sanmar_style_id((product or {}).get("product_id"))
    )


def _rabbit_skins_sanmar_style_id(product_id):
    raw_style = _upper_key(product_id)
    if not raw_style:
        return ""
    if raw_style in SANMAR_RABBIT_SKINS_STYLE_IDS:
        return raw_style
    prefixed = f"RS{raw_style}"
    if prefixed in SANMAR_RABBIT_SKINS_STYLE_IDS:
        return prefixed
    return ""


def _jerzees_sanmar_style_id(product_id):
    raw_style = _upper_key(product_id)
    if not raw_style:
        return ""
    if raw_style in SANMAR_JERZEES_STYLE_IDS:
        return raw_style
    suffixed = f"{raw_style}M"
    if suffixed in SANMAR_JERZEES_STYLE_IDS:
        return suffixed
    return ""


def _is_jerzees_product(product):
    source_text = " ".join(
        str((product or {}).get(name) or "")
        for name in ("stock_line", "product_name", "product_id")
    )
    return bool(re.search(r"\bJerzees\b", source_text, flags=re.I))


def _bella_canvas_sanmar_style_id(product_id):
    raw_style = _upper_key(product_id)
    if not raw_style:
        return ""
    candidates = []
    if raw_style.startswith("BC"):
        candidates.append(raw_style)
        if raw_style.endswith("C"):
            candidates.append(raw_style[:-1])
    elif re.fullmatch(r"B[0-9A-Z]+", raw_style):
        candidates.append(f"BC{raw_style[1:]}")
        candidates.append(raw_style)
        if raw_style.endswith("C"):
            candidates.append(f"BC{raw_style[1:-1]}")
    else:
        candidates.append(f"BC{raw_style}")
        if raw_style.endswith("C"):
            candidates.append(f"BC{raw_style[:-1]}")
    for candidate in candidates:
        if candidate in SANMAR_BELLA_CANVAS_STYLE_IDS:
            return candidate
    return ""


def _is_bella_canvas_product(product):
    source_text = " ".join(
        str((product or {}).get(name) or "")
        for name in ("stock_line", "product_name", "product_id")
    )
    return bool(
        _bella_canvas_sanmar_style_id((product or {}).get("product_id"))
        or re.search(r"\bBELLA\b", source_text, flags=re.I)
    )


def _next_level_sanmar_style_id(product_id, allow_bare_numeric=False):
    raw_style = _upper_key(product_id)
    if not raw_style:
        return ""
    if raw_style.startswith("NL"):
        return raw_style
    match = re.fullmatch(r"N(\d{4,})", raw_style)
    if match:
        return f"NL{match.group(1)}"
    match = re.fullmatch(r"(\d{4,})NL", raw_style)
    if match:
        return f"NL{match.group(1)}"
    if allow_bare_numeric and re.fullmatch(r"\d{4,}", raw_style):
        return f"NL{raw_style}"
    return ""


def _next_level_sanmar_style_id_requires_brand(product_id):
    raw_style = _upper_key(product_id)
    return bool(raw_style and re.fullmatch(r"\d{4,}", raw_style))


def _is_next_level_product(product):
    source_text = " ".join(
        str((product or {}).get(name) or "")
        for name in ("stock_line", "product_name", "product_id")
    )
    return bool(re.search(r"\bNEXT\s+LEVEL\b", source_text, flags=re.I))


def _sanmar_expected_style_keys(product, search_id=None):
    raw_product_id = str((product or {}).get("product_id") or "").strip().upper()
    raw_search_id = str(search_id or "").strip().upper()
    source_text = " ".join(
        str((product or {}).get(name) or "")
        for name in ("stock_line", "product_name", "product_id")
    ).lower()
    keys = []
    for value in (raw_product_id, raw_search_id):
        normalized = _upper_key(value)
        if normalized and normalized not in keys:
            keys.append(normalized)
    gildan_style_id = _gildan_sanmar_style_id(raw_product_id)
    if gildan_style_id and gildan_style_id not in keys:
        keys.append(gildan_style_id)
    rabbit_skins_style_id = _rabbit_skins_sanmar_style_id(raw_product_id)
    if rabbit_skins_style_id and rabbit_skins_style_id not in keys:
        keys.insert(0, rabbit_skins_style_id)
    jerzees_style_id = _jerzees_sanmar_style_id(raw_product_id)
    if jerzees_style_id and _is_jerzees_product(product) and jerzees_style_id not in keys:
        keys.insert(0, jerzees_style_id)
    bella_canvas_style_id = _bella_canvas_sanmar_style_id(raw_product_id)
    if bella_canvas_style_id and bella_canvas_style_id not in keys:
        keys.insert(0, bella_canvas_style_id)
    next_level_style_id = _next_level_sanmar_style_id(raw_product_id, allow_bare_numeric=_is_next_level_product(product))
    if (
        next_level_style_id
        and (
            _is_next_level_product(product)
            or not _next_level_sanmar_style_id_requires_brand(raw_product_id)
        )
        and next_level_style_id not in keys
    ):
        keys.insert(0, next_level_style_id)
    if "bella" in source_text:
        for value in (raw_product_id, raw_search_id):
            normalized = _upper_key(value)
            if normalized and not normalized.startswith("BC"):
                prefixed = f"BC{normalized}"
                if prefixed not in keys and (prefixed in SANMAR_BELLA_CANVAS_STYLE_IDS or not bella_canvas_style_id):
                    keys.insert(0, prefixed)
    return keys


def _sanmar_search_options_for_product(product):
    search_id = str((product or {}).get("product_id") or "").strip().upper()
    override = SANMAR_PRODUCT_SEARCH_OVERRIDES.get(search_id)
    if isinstance(override, dict):
        resolved_search_id = str(override.get("search_id") or search_id).strip().upper()
        return {
            "search_id": resolved_search_id,
            "click_inventory_button": bool(override.get("click_inventory_button")),
            "handler": search_id,
            "expected_style_keys": override.get("expected_style_keys") or _sanmar_expected_style_keys(product, resolved_search_id),
        }
    if (product or {}).get("is_a4"):
        resolved_search_id = f"a4{search_id}"
        return {
            "search_id": resolved_search_id,
            "click_inventory_button": False,
            "handler": "A4",
            "expected_style_keys": _sanmar_expected_style_keys(product, resolved_search_id),
        }
    jerzees_style_id = _jerzees_sanmar_style_id(search_id)
    if jerzees_style_id and _is_jerzees_product(product):
        return {
            "search_id": jerzees_style_id,
            "click_inventory_button": False,
            "handler": "Jerzees",
            "expected_style_keys": _sanmar_expected_style_keys(product, jerzees_style_id),
        }
    next_level_style_id = _next_level_sanmar_style_id(search_id, allow_bare_numeric=_is_next_level_product(product))
    if (
        next_level_style_id
        and (
            _is_next_level_product(product)
            or not _next_level_sanmar_style_id_requires_brand(search_id)
        )
    ):
        return {
            "search_id": next_level_style_id,
            "click_inventory_button": False,
            "handler": "Next Level",
            "expected_style_keys": _sanmar_expected_style_keys(product, next_level_style_id),
        }
    gildan_style_id = _gildan_sanmar_style_id(search_id)
    if gildan_style_id and _is_gildan_product(product):
        return {
            "search_id": gildan_style_id,
            "click_inventory_button": False,
            "handler": "Gildan",
            "expected_style_keys": _sanmar_expected_style_keys(product, gildan_style_id),
        }
    bella_canvas_style_id = _bella_canvas_sanmar_style_id(search_id)
    if bella_canvas_style_id and _is_bella_canvas_product(product):
        return {
            "search_id": bella_canvas_style_id,
            "click_inventory_button": True,
            "handler": "Bella+Canvas",
            "expected_style_keys": _sanmar_expected_style_keys(product, bella_canvas_style_id),
        }
    rabbit_skins_style_id = _rabbit_skins_sanmar_style_id(search_id)
    if rabbit_skins_style_id:
        return {
            "search_id": rabbit_skins_style_id,
            "click_inventory_button": True,
            "handler": "Rabbit Skins",
            "expected_style_keys": _sanmar_expected_style_keys(product, rabbit_skins_style_id),
        }
    return {
        "search_id": search_id,
        "click_inventory_button": False,
        "handler": "",
        "expected_style_keys": _sanmar_expected_style_keys(product, search_id),
    }


def _fill_sanmar_quantities(driver, warehouse, required):
    warehouse_options = _warehouse_match_options(warehouse)
    script = r"""
const warehouse = arguments[0];
const required = arguments[1] || {};
const warehouseOptions = Array.isArray(arguments[2]) ? arguments[2] : [warehouse];
const normalize = (value) => String(value || '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
function key(value) { return normalize(value).toUpperCase(); }
function escapeRegExp(value) { return String(value || '').replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }
function hasWarehouseText(text) {
  const haystack = key(text);
  for (const option of warehouseOptions) {
    const wanted = key(option);
    if (!wanted) continue;
    if (wanted.includes(',') && haystack.includes(wanted)) return true;
    if (wanted.length <= 3) {
      if (new RegExp(`\\b${escapeRegExp(wanted)}\\b`, 'i').test(haystack)) return true;
      continue;
    }
    if (haystack.includes(wanted)) return true;
  }
  return false;
}
function isVisible(node) {
  if (!node) return false;
  const rect = node.getBoundingClientRect();
  if ((rect.width || 0) <= 0 || (rect.height || 0) <= 0) return false;
  for (let current = node; current; current = current.parentElement) {
    const style = window.getComputedStyle(current);
    if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return false;
  }
  return true;
}
function dispatch(node, name) {
  node.dispatchEvent(new Event(name, { bubbles: true }));
}
const sizePattern = /^(XS|S|M|L|XL|[2-6]XL|S\/M|L\/XL|2\/3X|4\/5X|YXS|YS|YM|YL|YXL|LT|XLT|[2-4]XT|ONE SIZE|OSFA|NB|[0-9]{1,2}M|[0-9]{1,2}-[0-9]{1,2}MOS|0003|0306|0612|1218|1824|[2-7]T|5\/6)$/;
function cleanSize(value) {
  const text = normalize(value).toUpperCase().replace(/\s+/g, ' ');
  if (/^ONE SIZE$/.test(text)) return 'ONE SIZE';
  const compact = text.replace(/\s+/g, '');
  if (compact === 'NEWBORN') return 'NB';
  const compactInfantSizes = { '0003': '3M', '0306': '6M', '0612': '12M', '1218': '18M', '1824': '24M' };
  if (compactInfantSizes[compact]) return compactInfantSizes[compact];
  const infantMatch = compact.match(/^([0-9]{1,2}-[0-9]{1,2})(?:MO|MOS|MONTH|MONTHS)$/);
  if (infantMatch) return `${Number(infantMatch[1].split('-')[1])}M`;
  const infantSingleMatch = compact.match(/^([0-9]{1,2})(?:M|MO|MOS|MONTH|MONTHS)$/);
  if (infantSingleMatch) return `${Number(infantSingleMatch[1])}M`;
  const tallMatch = compact.match(/^([2-4])XLT$/);
  if (tallMatch) return `${tallMatch[1]}XT`;
  const comboMatch = compact.match(/^([24])X?L?\/([35])X?L?$/);
  if (comboMatch) return `${comboMatch[1]}/${comboMatch[2]}X`;
  const xMatch = compact.match(/^(X{2,6})L$/);
  if (xMatch) return `${xMatch[1].length}XL`;
  return compact;
}
let targetY = null;
for (const tr of Array.from(document.querySelectorAll('tr')).filter(isVisible)) {
  const rowText = normalize(tr.innerText || tr.textContent);
  if (!hasWarehouseText(rowText)) continue;
  const rect = tr.getBoundingClientRect();
  targetY = rect.top + rect.height / 2;
  break;
}
if (targetY === null) {
  for (const node of Array.from(document.querySelectorAll('body *')).filter(isVisible)) {
    const text = normalize(node.innerText || node.textContent);
    if (!hasWarehouseText(text)) continue;
    const rect = node.getBoundingClientRect();
    targetY = rect.top + rect.height / 2;
    break;
  }
}
if (targetY === null) return { success: false, message: `Warehouse row not found: ${warehouse}` };
const candidates = [];
for (const table of Array.from(document.querySelectorAll('table')).filter(isVisible)) {
  if (!table.querySelector('input:not([type="hidden"])')) continue;
  const headerCells = Array.from(table.querySelectorAll('thead th, tr.headings td, tr.headings th, th.size-header, td.size-header'))
    .filter(isVisible)
    .map((cell) => {
      const rect = cell.getBoundingClientRect();
      return {
        size: cleanSize(cell.innerText || cell.textContent),
        x: rect.left + rect.width / 2,
        width: rect.width || 0,
      };
    });
  const header = headerCells.filter((item) => sizePattern.test(item.size));
  if (!header.length) continue;
  for (const tr of Array.from(table.querySelectorAll('tbody tr')).filter(isVisible)) {
    const inputs = Array.from(tr.querySelectorAll('input:not([type="hidden"])')).filter(isVisible);
    if (!inputs.length) continue;
    const rect = tr.getBoundingClientRect();
    const distance = Math.abs((rect.top + rect.height / 2) - targetY);
    if (distance > 80) continue;
    candidates.push({ tr, table, header, distance, inputCount: inputs.length });
  }
}
if (!candidates.length) return { success: false, message: `Quantity input row not found for ${warehouse}` };
function nearestCellForHeader(cells, headerCell) {
  let best = null;
  let bestDistance = Infinity;
  for (const cell of cells) {
    const rect = cell.getBoundingClientRect();
    const center = rect.left + rect.width / 2;
    const distance = Math.abs(center - headerCell.x);
    if (distance < bestDistance) {
      best = { cell, distance, width: rect.width || 0 };
      bestDistance = distance;
    }
  }
  if (!best) return null;
  const allowedDistance = Math.max(28, (Number(headerCell.width) || 0) / 2 + (Number(best.width) || 0) / 2 + 12);
  return best.distance <= allowedDistance ? best.cell : null;
}
function rowInputs(row) {
  return Array.from(row.querySelectorAll('input:not([type="hidden"])'))
    .filter(isVisible)
    .filter((input) => !input.disabled && !input.readOnly);
}
function inputMatchesSize(input, size) {
  const wanted = cleanSize(size);
  const values = [
    input.getAttribute('aria-label'),
    input.getAttribute('title'),
    input.getAttribute('name'),
    input.getAttribute('id'),
    input.getAttribute('placeholder'),
    input.getAttribute('data-size'),
    input.getAttribute('data-color-size'),
  ];
  const label = input.closest('label');
  if (label) values.push(label.innerText || label.textContent);
  const cell = input.closest('td,th,[role="cell"],[role="gridcell"]');
  if (cell) {
    values.push(
      cell.getAttribute('data-title'),
      cell.getAttribute('data-label'),
      cell.getAttribute('aria-label')
    );
  }
  return values.some((value) => cleanSize(value) === wanted || new RegExp(`(^|[^A-Z0-9])${escapeRegExp(wanted)}([^A-Z0-9]|$)`, 'i').test(String(value || '').toUpperCase()));
}
function nearestInputForHeader(row, headerCell) {
  const inputs = rowInputs(row);
  let best = null;
  let bestDistance = Infinity;
  for (const input of inputs) {
    const rect = input.getBoundingClientRect();
    const center = rect.left + rect.width / 2;
    const distance = Math.abs(center - headerCell.x);
    if (distance < bestDistance) {
      best = { input, distance, width: rect.width || 0 };
      bestDistance = distance;
    }
  }
  if (!best) return null;
  const allowedDistance = Math.max(36, (Number(headerCell.width) || 0) / 2 + (Number(best.width) || 0) / 2 + 24);
  return best.distance <= allowedDistance ? best.input : null;
}
function ordinalInputForHeader(candidate, headerCell) {
  const inputs = rowInputs(candidate.tr);
  const index = candidate.header.indexOf(headerCell);
  if (index < 0 || !inputs.length) return null;
  const matching = inputs.find((input) => inputMatchesSize(input, headerCell.size));
  if (matching) return matching;
  if (inputs.length === candidate.header.length) return inputs[index] || null;
  if (inputs.length > candidate.header.length) {
    const offset = inputs.length - candidate.header.length;
    return inputs[offset + index] || inputs[index] || null;
  }
  return null;
}
function inputForHeader(candidate, headerCell) {
  const cells = Array.from(candidate.tr.children || []).filter(isVisible);
  const cell = nearestCellForHeader(cells, headerCell);
  const cellInput = cell ? rowInputs(cell)[0] : null;
  return cellInput || nearestInputForHeader(candidate.tr, headerCell) || ordinalInputForHeader(candidate, headerCell);
}
function coverageForCandidate(candidate) {
  let coverage = 0;
  for (const headerCell of candidate.header) {
    const size = headerCell.size;
    if (!size || required[size] === undefined) continue;
    if (inputForHeader(candidate, headerCell)) coverage += 1;
  }
  return coverage;
}
function inputDebug(input) {
  const rect = input.getBoundingClientRect();
  return [
    `x=${Math.round(rect.left + rect.width / 2)}`,
    `name=${normalize(input.getAttribute('name'))}`,
    `id=${normalize(input.getAttribute('id'))}`,
    `aria=${normalize(input.getAttribute('aria-label'))}`,
    `title=${normalize(input.getAttribute('title'))}`,
    `placeholder=${normalize(input.getAttribute('placeholder'))}`,
  ].filter((part) => !/=$/.test(part)).join('/');
}
function rowDebug(candidate) {
  const headers = candidate.header.map((item) => `${item.size}@${Math.round(item.x)}`).join(',');
  const inputs = rowInputs(candidate.tr).map(inputDebug).join(';');
  return `coverage=${candidate.coverage}; headers=${headers}; inputs=${inputs}`;
}
for (const candidate of candidates) candidate.coverage = coverageForCandidate(candidate);
candidates.sort((a, b) => b.coverage - a.coverage || a.distance - b.distance || b.inputCount - a.inputCount);
const filled = [];
const usedInputs = new Set();
function findInputForSize(size) {
  for (const candidate of candidates) {
    for (const headerCell of candidate.header) {
      if (headerCell.size !== size) continue;
      const input = inputForHeader(candidate, headerCell);
      if (!input || usedInputs.has(input)) continue;
      return { input, candidate };
    }
  }
  return null;
}
for (const size of Object.keys(required)) {
  const match = findInputForSize(size);
  if (!match) {
    const debug = candidates.slice(0, 4).map(rowDebug).join(' | ');
    return { success: false, message: `Quantity input missing for ${size}; ${debug}` };
  }
  const input = match.input;
  const value = String(required[size]);
  input.focus();
  input.value = value;
  dispatch(input, 'input');
  dispatch(input, 'change');
  dispatch(input, 'blur');
  usedInputs.add(input);
  filled.push(size);
}
const missing = Object.keys(required).filter((size) => !filled.includes(size));
if (missing.length) {
  const debug = candidates.slice(0, 4).map(rowDebug).join(' | ');
  return { success: false, message: `Quantity input missing for ${missing.join(', ')}; ${debug}` };
}
return { success: true };
"""
    deadline = time.time() + 10
    result = None
    while time.time() < deadline:
        result = driver.execute_script(script, warehouse, required, warehouse_options)
        if isinstance(result, dict) and result.get("success"):
            break
        message = str((result or {}).get("message") or "")
        if not re.search(r"Warehouse row not found|Quantity input missing", message, flags=re.I):
            break
        try:
            _ensure_sanmar_inventory_view(driver, force_click=False)
        except Exception:
            pass
        time.sleep(0.8)
    if not isinstance(result, dict) or not result.get("success"):
        raise RuntimeError((result or {}).get("message") or "SanMar quantities could not be filled.")
    time.sleep(0.7)


def _click_sanmar_button(driver, text_pattern, timeout=12):
    return _click_sanmar_text_control(driver, text_pattern, timeout=timeout)


def _add_current_product_to_box(driver):
    _click_sanmar_button(driver, r"Add\s+to\s+shopping\s+box")
    _wait_for_text(driver, r"Shopping\s+Box|Saved\s+Shopping\s+Box|Checkout|Proceed\s+to\s+checkout", timeout=15)
    time.sleep(0.8)


def _expand_sanmar_cart_rows(driver):
    script = r"""
function isVisible(node) {
  if (!node) return false;
  const rect = node.getBoundingClientRect();
  if ((rect.width || 0) <= 0 || (rect.height || 0) <= 0) return false;
  for (let current = node; current; current = current.parentElement) {
    const style = window.getComputedStyle(current);
    if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return false;
  }
  return true;
}
const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim();
function rowTexts() {
  return Array.from(document.querySelectorAll('tr')).filter(isVisible).map((row) => normalize(row.innerText || row.textContent));
}
function childRows() {
  return Array.from(document.querySelectorAll('tr[class*="child-of"], tr.collapse'));
}
function visibleChildRowCount() {
  return childRows().filter(isVisible).length;
}
const before = rowTexts().join('\n');
const hasMultiple = /\bMultiple\b/i.test(before);
let clicked = false;
let alreadyExpanded = false;
let beforeChildRows = visibleChildRowCount();
const switchLabels = Array.from(document.querySelectorAll('label,span,div,p')).filter(isVisible);
for (const node of switchLabels) {
  const text = normalize(node.innerText || node.textContent || node.getAttribute('aria-label') || node.getAttribute('title'));
  if (!/Collapse\s*\/\s*Expand\s+all\s+rows/i.test(text)) continue;
  let scope = node;
  let control = null;
  for (let i = 0; scope && i < 5 && !control; i += 1, scope = scope.parentElement) {
    control = scope.querySelector('input[type="checkbox"],button,[role="switch"]');
  }
  if (!control) {
    const labelRect = node.getBoundingClientRect();
    const candidates = Array.from(document.querySelectorAll('input[type="checkbox"],button,[role="switch"]')).filter(isVisible);
    let best = null;
    for (const candidate of candidates) {
      const rect = candidate.getBoundingClientRect();
      const distance = Math.abs(rect.top - labelRect.top) + Math.abs(rect.left - labelRect.right);
      if (!best || distance < best.distance) best = { node: candidate, distance };
    }
    control = best ? best.node : null;
  }
  if (!control) continue;
  const checked = control.checked === true || String(control.getAttribute('aria-checked') || '').toLowerCase() === 'true';
  alreadyExpanded = checked;
  if (hasMultiple && !checked && beforeChildRows <= 0) {
    control.click();
    clicked = true;
  }
  break;
}
if (!clicked && hasMultiple && beforeChildRows <= 0 && !alreadyExpanded) {
  for (const row of Array.from(document.querySelectorAll('tr')).filter(isVisible)) {
    const text = normalize(row.innerText || row.textContent);
    if (!/\bMultiple\b/i.test(text)) continue;
    const controls = Array.from(row.querySelectorAll('button,a,[role="button"],input[type="checkbox"],svg')).filter(isVisible);
    const control = controls[controls.length - 1];
    if (control) {
      control.click();
      clicked = true;
    }
  }
}
if (hasMultiple && visibleChildRowCount() <= 0) {
  for (const row of childRows()) {
    row.classList.add('show');
    row.removeAttribute('aria-hidden');
    if (row.style && row.style.display === 'none') row.style.display = '';
  }
}
return { clicked, hadMultiple: hasMultiple, alreadyExpanded, beforeChildRows, afterChildRows: visibleChildRowCount() };
"""
    try:
        result = driver.execute_script(script)
    except Exception:
        result = {}
    if isinstance(result, dict) and result.get("clicked"):
        time.sleep(1.0)
    return result if isinstance(result, dict) else {}


def _read_sanmar_cart_lines(driver):
    _expand_sanmar_cart_rows(driver)
    script = r"""
const normalize = (value) => String(value || '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
const STYLE_PATTERN = /\b(?:[A-Z]{1,6}\d{2,6}[A-Z]?|\d{3,6}[A-Z]?)\b/i;
const SIZE_PATTERN = /^(?:Y?XS|Y?S|Y?M|Y?L|Y?XL|XS|S|M|L|XL|[2-6]XL|S\/M|L\/XL|2\/3X|4\/5X|LT|XLT|[2-4]XT|ONE SIZE|OSFA|OS|NB|[0-9]{1,2}M|[0-9]{1,2}-[0-9]{1,2}MOS|[2-7]T|5\/6)$/i;
function isVisible(node) {
  if (!node) return false;
  const rect = node.getBoundingClientRect();
  if ((rect.width || 0) <= 0 || (rect.height || 0) <= 0) return false;
  for (let current = node; current; current = current.parentElement) {
    const style = window.getComputedStyle(current);
    if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return false;
  }
  return true;
}
function key(value) {
  return normalize(value).toLowerCase();
}
function cleanSize(value) {
  const text = normalize(value).toUpperCase().replace(/\s+/g, ' ');
  if (/^ONE SIZE$/.test(text)) return 'ONE SIZE';
  const compact = text.replace(/\s+/g, '');
  if (compact === 'NEWBORN') return 'NB';
  const compactInfantSizes = { '0003': '3M', '0306': '6M', '0612': '12M', '1218': '18M', '1824': '24M' };
  if (compactInfantSizes[compact]) return compactInfantSizes[compact];
  const infantMatch = compact.match(/^([0-9]{1,2}-[0-9]{1,2})(?:MO|MOS|MONTH|MONTHS)$/);
  if (infantMatch) return `${Number(infantMatch[1].split('-')[1])}M`;
  const infantSingleMatch = compact.match(/^([0-9]{1,2})(?:M|MO|MOS|MONTH|MONTHS)$/);
  if (infantSingleMatch) return `${Number(infantSingleMatch[1])}M`;
  const tallMatch = compact.match(/^([2-4])XLT$/);
  if (tallMatch) return `${tallMatch[1]}XT`;
  const comboMatch = compact.match(/^([24])X?L?\/([35])X?L?$/);
  if (comboMatch) return `${comboMatch[1]}/${comboMatch[2]}X`;
  const xMatch = compact.match(/^(X{2,6})L$/);
  if (xMatch) return `${xMatch[1].length}XL`;
  return compact;
}
function indexFor(headers, patterns) {
  for (let i = 0; i < headers.length; i += 1) {
    if (patterns.some((pattern) => pattern.test(headers[i]))) return i;
  }
  return -1;
}
function addLine(line) {
  const style = normalize(line && line.style);
  const size = cleanSize(line && line.size);
  const quantity = Number(line && line.quantity);
  if (!style || !size || !SIZE_PATTERN.test(size) || !Number.isFinite(quantity) || quantity <= 0) return;
  const warehouse = normalize(line && line.warehouse);
  const color = normalize(line && line.color);
  const dedupeKey = [style.toUpperCase(), size, quantity, color.toUpperCase(), warehouse.toUpperCase()].join('|');
  if (lines.some((existing) => existing._dedupeKey === dedupeKey)) return;
  lines.push({ style, color, size, quantity, warehouse, _dedupeKey: dedupeKey });
}
function cellLabel(cell) {
  return key(
    cell.getAttribute('data-label')
    || cell.getAttribute('data-title')
    || cell.getAttribute('aria-label')
    || cell.getAttribute('headers')
    || ''
  );
}
function quantityFromText(text) {
  const direct = normalize(text).replace(/,/g, '').match(/(?:total\s*(?:piece|pieces)|pieces?|qty|quantity)\D{0,40}(\d+)/i);
  if (direct) return Number(direct[1]);
  const numbers = normalize(text).replace(/,/g, '').match(/\b\d+\b/g) || [];
  return numbers.length === 1 ? Number(numbers[0]) : 0;
}
function styleFromText(text) {
  const labeled = normalize(text).match(/(?:style|item)\s*#?\s*:?\s*([A-Z0-9-]+)/i);
  if (labeled && STYLE_PATTERN.test(labeled[1])) return labeled[1];
  const matches = normalize(text).match(new RegExp(STYLE_PATTERN.source, 'ig')) || [];
  const match = matches.find((value) => !/^20\d{2}$/.test(value));
  return match || '';
}
function sizeFromText(text) {
  const labeled = normalize(text).match(/size\s*:?\s*(ONE\s+SIZE|OSFA|S\s*\/\s*M|L\s*\/\s*XL|2(?:X|XL)?\s*\/\s*3(?:X|XL)?|4(?:X|XL)?\s*\/\s*5(?:X|XL)?|Y?XS|Y?S|Y?M|Y?L|Y?XL|XS|S|M|L|XL|X{2,6}L|[2-6]XL|LT|XLT|[2-4]XLT|[2-4]XT|OS|NB|NEWBORN|[0-9]{1,2}M|[0-9]{1,2}-[0-9]{1,2}\s*(?:MO|MOS|MONTHS?)|[2-7]T|5\/6)\b/i);
  if (labeled) return labeled[1];
  return '';
}
function colorFromText(text) {
  const match = normalize(text).match(/color\s*:?\s*(.+?)(?=\s*(?:size\s*:?\s*(?:ONE\s+SIZE|OSFA|S\s*\/\s*M|L\s*\/\s*XL|2(?:X|XL)?\s*\/\s*3(?:X|XL)?|4(?:X|XL)?\s*\/\s*5(?:X|XL)?|Y?XS|Y?S|Y?M|Y?L|Y?XL|XS|S|M|L|XL|X{2,6}L|[2-6]XL|LT|XLT|[2-4]XLT|[2-4]XT|OS|NB|NEWBORN|[0-9]{1,2}M|[0-9]{1,2}-[0-9]{1,2}\s*(?:MO|MOS|MONTHS?)|[2-7]T|5\/6)\b|total\s*(?:piece|pieces)|pieces?|qty|quantity|warehouse|price|merchandise|weight|remove)\b|$)/i);
  return match ? match[1] : '';
}
function warehouseFromText(text) {
  const normalized = normalize(text);
  const labeled = normalized.match(/warehouse\s*:?\s*([A-Za-z ]+,\s*[A-Z]{2})/i);
  if (labeled) return labeled[1];
  const known = normalized.match(/\b(Robbinsville,\s*NJ|Richmond,\s*VA|Cincinnati,\s*OH|Jacksonville,\s*FL|Minneapolis,\s*MN|Dallas,\s*TX|Phoenix,\s*AZ|Reno,\s*NV|Seattle,\s*WA)\b/i);
  return known ? known[1] : '';
}
function textFromSelector(root, selector) {
  const node = root.querySelector(selector);
  return node ? normalize(node.innerText || node.textContent || node.value || '') : '';
}
function quantityFromCell(cell) {
  if (!cell) return 0;
  const input = cell.querySelector('input,select');
  if (input) {
    const value = normalize(input.value || input.getAttribute('value') || '').replace(/,/g, '');
    const match = value.match(/\d+/);
    if (match) return Number(match[0]);
  }
  return quantityFromText(cell.innerText || cell.textContent || '');
}
function selectedText(select) {
  if (!select) return '';
  const selected = select.selectedOptions && select.selectedOptions.length ? select.selectedOptions[0] : null;
  return normalize((selected && (selected.innerText || selected.textContent || selected.value)) || select.value || '');
}
function sizeQuantityPairsFromText(text) {
  const normalized = normalize(text);
  const pairs = [];
  const pattern = /\b(ONE\s+SIZE|OSFA|S\s*\/\s*M|L\s*\/\s*XL|2(?:X|XL)?\s*\/\s*3(?:X|XL)?|4(?:X|XL)?\s*\/\s*5(?:X|XL)?|Y?XS|Y?S|Y?M|Y?L|Y?XL|XS|S|M|L|XL|X{2,6}L|[2-6]XL|LT|XLT|[2-4]XLT|[2-4]XT|OS|NB|NEWBORN|[0-9]{1,2}M|[0-9]{1,2}-[0-9]{1,2}\s*(?:MO|MOS|MONTHS?)|[2-7]T|5\/6)\s*:\s*(\d+)\b/ig;
  let match = null;
  while ((match = pattern.exec(normalized)) !== null) {
    pairs.push({ size: cleanSize(match[1]), quantity: Number(match[2]) });
  }
  return pairs;
}
const lines = [];
for (const table of Array.from(document.querySelectorAll('table')).filter(isVisible)) {
  const rows = Array.from(table.querySelectorAll('tr')).filter(isVisible);
  if (rows.length < 2) continue;
  for (const row of rows) {
    const styleCell = row.querySelector('.column-style');
    const sizeCell = row.querySelector('.column-size');
    const qtyCell = row.querySelector('.column-merch-qty');
    if (!styleCell || !sizeCell || !qtyCell) continue;
    const mobileSizeQty = textFromSelector(row, '.column-size-qty');
    const mobileMatch = mobileSizeQty.match(/\b(ONE\s+SIZE|OSFA|S\s*\/\s*M|L\s*\/\s*XL|2(?:X|XL)?\s*\/\s*3(?:X|XL)?|4(?:X|XL)?\s*\/\s*5(?:X|XL)?|Y?XS|Y?S|Y?M|Y?L|Y?XL|XS|S|M|L|XL|X{2,6}L|[2-6]XL|LT|XLT|[2-4]XLT|[2-4]XT|OS|NB|NEWBORN|[0-9]{1,2}M|[0-9]{1,2}-[0-9]{1,2}\s*(?:MO|MOS|MONTHS?)|[2-7]T|5\/6)\s*:\s*(\d+)\b/i);
    const warehouseCell = row.querySelector('.column-warehouse');
    const warehouseSelect = warehouseCell ? warehouseCell.querySelector('select') : null;
    const style = textFromSelector(styleCell, '.style-number') || styleFromText(styleCell.innerText || styleCell.textContent || '');
    const color = textFromSelector(row, '.column-color .name') || textFromSelector(row, '.column-color img') || textFromSelector(row, '.column-color');
    const rawSize = normalize(sizeCell.innerText || sizeCell.textContent || '');
    const warehouse = selectedText(warehouseSelect) || (warehouseCell ? warehouseFromText(warehouseCell.innerText || warehouseCell.textContent || '') : '');
    if (/^multiple$/i.test(rawSize)) {
      for (const pair of sizeQuantityPairsFromText(mobileSizeQty)) {
        addLine({ style, color, size: pair.size, quantity: pair.quantity, warehouse });
      }
      continue;
    }
    addLine({
      style,
      color,
      size: rawSize || (mobileMatch ? cleanSize(mobileMatch[1]) : ''),
      quantity: quantityFromCell(qtyCell) || (mobileMatch ? Number(mobileMatch[2]) : 0),
      warehouse,
    });
  }
  let headerIndex = -1;
  let headers = [];
  for (let i = 0; i < rows.length; i += 1) {
    const cells = Array.from(rows[i].children || []).filter(isVisible).map((cell) => key(cell.innerText || cell.textContent));
    if (cells.some((text) => /style/.test(text)) && cells.some((text) => /size/.test(text))) {
      headerIndex = i;
      headers = cells;
      break;
    }
  }
  if (headerIndex < 0) continue;
  const styleIndex = indexFor(headers, [/style/]);
  const colorIndex = indexFor(headers, [/color/]);
  const sizeIndex = indexFor(headers, [/size/]);
  const qtyIndex = indexFor(headers, [/total.*piece/, /\bpiece/, /\bqty/, /quantity/]);
  const warehouseIndex = indexFor(headers, [/warehouse/]);
  if (styleIndex < 0 || sizeIndex < 0 || qtyIndex < 0) continue;
  let currentStyle = '';
  for (const row of rows.slice(headerIndex + 1)) {
    const cells = Array.from(row.children || []).filter(isVisible).map((cell) => normalize(cell.innerText || cell.textContent));
    if (cells.length <= Math.max(styleIndex, sizeIndex, qtyIndex)) continue;
    const rawStyle = cells[styleIndex];
    const style = rawStyle || currentStyle;
    const size = cells[sizeIndex];
    if (rawStyle) currentStyle = rawStyle;
    if (/^multiple$/i.test(size)) continue;
    const qtyMatch = String(cells[qtyIndex] || '').replace(/,/g, '').match(/\d+/);
    const quantity = qtyMatch ? Number(qtyMatch[0]) : 0;
    if (!style || !size || quantity <= 0) continue;
    addLine({
      style,
      color: colorIndex >= 0 ? cells[colorIndex] || '' : '',
      size,
      quantity,
      warehouse: warehouseIndex >= 0 ? cells[warehouseIndex] || '' : '',
    });
  }
}
for (const row of Array.from(document.querySelectorAll('tr')).filter(isVisible)) {
  const labeled = {};
  for (const cell of Array.from(row.children || []).filter(isVisible)) {
    const label = cellLabel(cell);
    if (!label) continue;
    const text = normalize(cell.innerText || cell.textContent);
    if (/style|item/.test(label)) labeled.style = text;
    else if (/color/.test(label)) labeled.color = text;
    else if (/size/.test(label)) labeled.size = text;
    else if (/total.*piece|piece|qty|quantity/.test(label)) labeled.quantity = text;
    else if (/warehouse/.test(label)) labeled.warehouse = text;
  }
  if (labeled.style && labeled.size && labeled.quantity) {
    addLine({
      style: styleFromText(labeled.style),
      color: labeled.color || '',
      size: labeled.size,
      quantity: quantityFromText(labeled.quantity),
      warehouse: labeled.warehouse || warehouseFromText(normalize(row.innerText || row.textContent)),
    });
  }
}
function looksLikeCartLineText(text) {
  const normalized = normalize(text);
  return STYLE_PATTERN.test(normalized)
    && /(?:total\s*(?:piece|pieces)|pieces?|qty|quantity)\D{0,40}\d/i.test(normalized)
    && /(?:\bsize\b\s*:?\s*)?(?:ONE\s+SIZE|OSFA|S\s*\/\s*M|L\s*\/\s*XL|2(?:X|XL)?\s*\/\s*3(?:X|XL)?|4(?:X|XL)?\s*\/\s*5(?:X|XL)?|Y?XS|Y?S|Y?M|Y?L|Y?XL|XS|S|M|L|XL|X{2,6}L|[2-6]XL|LT|XLT|[2-4]XLT|[2-4]XT|OS)\b/i.test(normalized);
}
const blockNodes = Array.from(document.querySelectorAll('tr,li,[class*="cart"],[class*="shopping"],[class*="line"],[class*="item"],[class*="product"],div'))
  .filter(isVisible)
  .filter((node) => {
    const text = normalize(node.innerText || node.textContent);
    if (!looksLikeCartLineText(text)) return false;
    return !Array.from(node.children || []).some((child) => isVisible(child) && looksLikeCartLineText(child.innerText || child.textContent));
  });
for (const node of blockNodes) {
  const text = normalize(node.innerText || node.textContent);
  addLine({
    style: styleFromText(text),
    color: colorFromText(text),
    size: sizeFromText(text),
    quantity: quantityFromText(text),
    warehouse: warehouseFromText(text),
  });
}
const filteredLines = lines.filter((line, index) => {
  if (line.warehouse) return true;
  return !lines.some((other, otherIndex) => (
    otherIndex !== index
    && other.warehouse
    && String(other.style || '').toUpperCase() === String(line.style || '').toUpperCase()
    && String(other.color || '').toUpperCase() === String(line.color || '').toUpperCase()
    && String(other.size || '').toUpperCase() === String(line.size || '').toUpperCase()
    && Number(other.quantity || 0) === Number(line.quantity || 0)
  ));
});
return filteredLines.map((line) => {
  const copy = Object.assign({}, line);
  delete copy._dedupeKey;
  return copy;
});
"""
    try:
        lines = driver.execute_script(script)
    except Exception:
        lines = []
    return lines if isinstance(lines, list) else []


def _cart_style_matches(style, expected_keys):
    style_key = _upper_key(style)
    return any(key and style_key == key for key in expected_keys or [])


def _cart_color_matches(color, expected_color, product=None):
    wanted_keys = _sanmar_color_match_keys(expected_color, product=product)
    if not wanted_keys:
        return True
    return any(_sanmar_color_keys_match(color, key) for key in wanted_keys)


def _validate_sanmar_cart_contents(driver, product_lines, warehouse=None):
    cart_lines = _read_sanmar_cart_lines(driver)
    expected_lines = []
    for line in product_lines if isinstance(product_lines, list) else []:
        product = line.get("product") if isinstance(line, dict) else {}
        quantities = line.get("quantities") if isinstance(line.get("quantities"), dict) else {}
        expected_keys = line.get("expected_style_keys") or _sanmar_expected_style_keys(product, line.get("search_id"))
        expected_lines.append(
            {
                "index": line.get("cart_validation_key") or product.get("index"),
                "product_id": product.get("product_id"),
                "search_id": line.get("search_id"),
                "expected_style_keys": [_upper_key(key) for key in expected_keys if _upper_key(key)],
                "product": product,
                "color": product.get("color"),
                "quantities": {str(size).upper().replace(" ", ""): int(qty) for size, qty in quantities.items()},
                "warehouse": line.get("warehouse"),
            }
        )

    issues = []
    actual_by_index = {item["index"]: {} for item in expected_lines}
    matched_line_indexes = set()
    for cart_line in cart_lines:
        style = str(cart_line.get("style") or "")
        size = str(cart_line.get("size") or "").upper().replace(" ", "")
        quantity = int(cart_line.get("quantity") or 0)
        matches = [
            item
            for item in expected_lines
            if _cart_style_matches(style, item["expected_style_keys"])
            and _cart_color_matches(cart_line.get("color"), item.get("color"), item.get("product"))
            and size in item.get("quantities", {})
            and (
                not item.get("warehouse")
                or not cart_line.get("warehouse")
                or str(item.get("warehouse")).lower() in str(cart_line.get("warehouse")).lower()
            )
        ]
        if len(matches) != 1:
            color = str(cart_line.get("color") or "").strip()
            color_detail = f" color {color}" if color else ""
            issues.append(f"Unexpected SanMar cart line: style {style or '-'}{color_detail} size {size or '-'} qty {quantity}.")
            continue
        expected = matches[0]
        matched_line_indexes.add(expected["index"])
        actual_by_index.setdefault(expected["index"], {})
        actual_by_index[expected["index"]][size] = actual_by_index[expected["index"]].get(size, 0) + quantity
        expected_warehouse = expected.get("warehouse") or warehouse
        if expected_warehouse:
            line_warehouse = str(cart_line.get("warehouse") or "")
            if line_warehouse and str(expected_warehouse).lower() not in line_warehouse.lower():
                issues.append(f"Cart line {style} {size} used warehouse {line_warehouse}, expected {expected_warehouse}.")

    for expected in expected_lines:
        actual = actual_by_index.get(expected["index"], {})
        for size, quantity in expected["quantities"].items():
            if int(actual.get(size, 0) or 0) != int(quantity):
                issues.append(
                    f"Expected {expected['product_id']} size {size} qty {quantity}, "
                    f"but cart has {int(actual.get(size, 0) or 0)}."
                )
        for size, quantity in actual.items():
            if size not in expected["quantities"]:
                issues.append(f"Cart has extra {expected['product_id']} size {size} qty {quantity}.")

    expected_total = sum(sum(item["quantities"].values()) for item in expected_lines)
    actual_total = sum(int(line.get("quantity") or 0) for line in cart_lines)
    if expected_total != actual_total:
        issues.append(f"Expected {expected_total} total piece(s), but cart has {actual_total}.")
    if not cart_lines:
        issues.append("No SanMar cart lines could be read before checkout.")

    return {
        "success": not issues,
        "issues": issues,
        "cart_lines": cart_lines,
        "expected_lines": expected_lines,
    }


def _select_shipping_destination(driver, order_type, warehouse=None, multi_warehouse=False):
    if multi_warehouse:
        _click_radio_near_text(driver, "Ship to an address")
        target = "Mach 6 Manufacturing" if order_type == "mach6" else "123 EZ TEES INC"
        _click_radio_near_text(driver, target)
        _wait_for_text(driver, re.escape(target), timeout=5)
        _click_sanmar_button(driver, r"Confirm\s+Address")
        time.sleep(2.0)
        return {"ship_mode": "ship", "address": target}
    if order_type == "inhouse" and warehouse == "Robbinsville, NJ":
        _click_radio_near_text(driver, "Pick Up at warehouse")
        _select_dropdown_option_containing(driver, "Robbinsville")
        _click_sanmar_button(driver, r"Proceed\s+To\s+Payment")
        return {"ship_mode": "pickup", "address": "Robbinsville, NJ"}
    _click_radio_near_text(driver, "Ship to an address")
    target = "Mach 6 Manufacturing" if order_type == "mach6" else "123 EZ TEES INC"
    _click_radio_near_text(driver, target)
    _wait_for_text(driver, re.escape(target), timeout=5)
    _click_sanmar_button(driver, r"Confirm\s+Address")
    time.sleep(2.0)
    return {"ship_mode": "ship", "address": target}


def _click_radio_near_text(driver, text):
    script = r"""
const wanted = String(arguments[0] || '').toLowerCase();
const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim();
function isVisible(node) {
  if (!node) return false;
  const rect = node.getBoundingClientRect();
  if ((rect.width || 0) <= 0 || (rect.height || 0) <= 0) return false;
  for (let current = node; current; current = current.parentElement) {
    const style = window.getComputedStyle(current);
    if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return false;
  }
  return true;
}
const radios = Array.from(document.querySelectorAll('input[type="radio"]')).filter(isVisible);
let best = null;
for (const radio of radios) {
  for (let current = radio.parentElement, depth = 0; current && depth < 4; current = current.parentElement, depth += 1) {
    const scopeText = normalize(current.innerText || current.textContent);
    if (!scopeText.toLowerCase().includes(wanted)) continue;
    const rect = radio.getBoundingClientRect();
    const currentRect = current.getBoundingClientRect();
    const area = Math.max(1, (currentRect.width || 1) * (currentRect.height || 1));
    const score = (depth * 100000000) + area + Math.round(rect.top || 0);
    if (!best || score < best.score) best = { radio, score };
    break;
  }
}
return best ? best.radio : null;
"""
    radio = driver.execute_script(script, text)
    if radio is None:
        raise RuntimeError(f"Radio option not found: {text}")
    _click_with_fallback(driver, radio)
    time.sleep(0.5)


def _select_dropdown_option_containing(driver, text):
    script = r"""
const wanted = String(arguments[0] || '').toLowerCase();
const selects = Array.from(document.querySelectorAll('select')).filter((node) => {
  const rect = node.getBoundingClientRect();
  return (rect.width || 0) > 0 && (rect.height || 0) > 0;
});
for (const select of selects) {
  const option = Array.from(select.options || []).find((item) => String(item.textContent || item.value || '').toLowerCase().includes(wanted));
  if (!option) continue;
  select.value = option.value;
  option.selected = true;
  select.dispatchEvent(new Event('input', { bubbles: true }));
  select.dispatchEvent(new Event('change', { bubbles: true }));
  return true;
}
return false;
"""
    if driver.execute_script(script, text) is not True:
        raise RuntimeError(f"Dropdown option not found: {text}")
    time.sleep(0.5)


def _select_ups_and_eta(driver, order_type):
    if order_type == "inhouse":
        text = _normalize_text(driver.execute_script("return String(document.body && (document.body.innerText || document.body.textContent) || '');"))
        if "Pick Up at warehouse" in text and "Shipping Method" not in text:
            return None
    script = r"""
const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim();
function isVisible(node) {
  if (!node) return false;
  const rect = node.getBoundingClientRect();
  if ((rect.width || 0) <= 0 || (rect.height || 0) <= 0) return false;
  for (let current = node; current; current = current.parentElement) {
    const style = window.getComputedStyle(current);
    if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return false;
  }
  return true;
}
function dateFrom(text) {
  return String(text || '').match(/(?:Jan|Feb|Mar|Apr|May|Jun|June|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}(?:,\s*\d{4})?|\d{1,2}\/\d{1,2}\/\d{2,4}/i);
}
function rowFor(radio) {
  const tableRow = radio.closest('tr');
  if (tableRow && isVisible(tableRow)) return tableRow;
  let scope = radio.parentElement;
  for (let depth = 0; scope && depth < 5; scope = scope.parentElement, depth += 1) {
    const text = normalize(scope.innerText || scope.textContent);
    const radioCount = Array.from(scope.querySelectorAll('input[type="radio"]')).filter(isVisible).length;
    if (dateFrom(text) && radioCount <= 1) return scope;
  }
  return radio.parentElement;
}
const radios = Array.from(document.querySelectorAll('input[type="radio"]')).filter(isVisible);
for (const radio of radios) {
  const text = normalize(rowFor(radio).innerText || rowFor(radio).textContent);
  if (!/\bUPS\b/i.test(text)) continue;
  const selected = Boolean(radio.checked);
  radio.click();
  radio.dispatchEvent(new Event('change', { bubbles: true }));
  const dateMatch = dateFrom(text);
  return { success: true, etaText: dateMatch ? dateMatch[0] : '', selected, text: text.slice(0, 300) };
}
return { success: false };
"""
    result = driver.execute_script(script)
    if not isinstance(result, dict) or not result.get("success"):
        raise RuntimeError("UPS shipping option was not available.")
    time.sleep(0.5)
    eta = _parse_sanmar_eta(result.get("etaText"))
    if eta is None:
        text = _normalize_text(driver.execute_script("return String(document.body && (document.body.innerText || document.body.textContent) || '');"))
        ups_area = re.search(r"\bUPS\b(.{0,160})", text, flags=re.I)
        eta = _parse_sanmar_eta(ups_area.group(1) if ups_area else text)
    if eta is None:
        raise RuntimeError("UPS estimated delivery date could not be read.")
    return eta


def _select_ups_and_latest_eta_by_warehouse(driver, expected_warehouses=None):
    expected = [str(item or "").strip() for item in (expected_warehouses or []) if str(item or "").strip()]
    script = r"""
const expected = arguments[0] || [];
const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim();
function isVisible(node) {
  if (!node) return false;
  const rect = node.getBoundingClientRect();
  if ((rect.width || 0) <= 0 || (rect.height || 0) <= 0) return false;
  for (let current = node; current; current = current.parentElement) {
    const style = window.getComputedStyle(current);
    if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return false;
  }
  return true;
}
const warehousePattern = /(Robbinsville,\s*NJ|Richmond,\s*VA|Cincinnati,\s*OH|Jacksonville,\s*FL|Minneapolis,\s*MN|Dallas,\s*TX|Phoenix,\s*AZ|Reno,\s*NV|Seattle,\s*WA)/i;
const headings = Array.from(document.querySelectorAll('body *')).filter(isVisible).map((node) => {
  const text = normalize(node.innerText || node.textContent);
  const match = text.match(new RegExp('^\\\\s*Warehouse\\\\s*:\\\\s*' + warehousePattern.source, 'i')) || text.match(/^Warehouse\s*:\s*(.+)$/i);
  if (!match) return null;
  const warehouseMatch = text.match(warehousePattern);
  if (!warehouseMatch) return null;
  const rect = node.getBoundingClientRect();
  return { warehouse: normalize(warehouseMatch[1]).replace(/\s*,\s*/, ', '), y: rect.top };
}).filter(Boolean).sort((a, b) => a.y - b.y);
function warehouseForY(y) {
  let selected = null;
  for (const heading of headings) {
    if (heading.y <= y + 4) selected = heading;
    else break;
  }
  return selected ? selected.warehouse : '';
}
function dateFrom(text) {
  return String(text || '').match(/(?:Jan|Feb|Mar|Apr|May|Jun|June|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}(?:,\s*\d{4})?|\d{1,2}\/\d{1,2}\/\d{2,4}/i);
}
function rowFor(radio) {
  const tableRow = radio.closest('tr');
  if (tableRow && isVisible(tableRow)) return tableRow;
  let scope = radio.parentElement;
  for (let depth = 0; scope && depth < 8; scope = scope.parentElement, depth += 1) {
    const text = normalize(scope.innerText || scope.textContent);
    const radioCount = Array.from(scope.querySelectorAll('input[type="radio"]')).filter(isVisible).length;
    if ((dateFrom(text) || warehousePattern.test(text)) && radioCount <= 1) return scope;
  }
  return radio.parentElement;
}
const results = [];
for (const radio of Array.from(document.querySelectorAll('input[type="radio"]')).filter(isVisible)) {
  const rect = radio.getBoundingClientRect();
  const text = normalize(rowFor(radio).innerText || rowFor(radio).textContent);
  const warehouseMatch = text.match(warehousePattern);
  const warehouse = warehouseForY(rect.top) || (warehouseMatch ? normalize(warehouseMatch[1]).replace(/\s*,\s*/, ', ') : '');
  if (!warehouse) continue;
  if (!/\bUPS\b/i.test(text)) continue;
  if (expected.length && !expected.some((item) => item.toLowerCase() === warehouse.toLowerCase())) continue;
  const selected = Boolean(radio.checked);
  radio.click();
  radio.dispatchEvent(new Event('change', { bubbles: true }));
  const dateMatch = dateFrom(text);
  results.push({ warehouse, etaText: dateMatch ? dateMatch[0] : '', selected, text: text.slice(0, 300) });
}
return results;
"""
    rows = driver.execute_script(script, expected)
    rows = rows if isinstance(rows, list) else []
    by_warehouse = {}
    selected_by_warehouse = {}
    missing = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        warehouse = _normalize_text(row.get("warehouse"))
        eta = _parse_sanmar_eta(row.get("etaText"))
        if eta is None:
            eta = _parse_sanmar_eta(row.get("text"))
        if not warehouse or eta is None:
            continue
        if warehouse in by_warehouse and not row.get("selected") and selected_by_warehouse.get(warehouse):
            continue
        by_warehouse[warehouse] = eta
        selected_by_warehouse[warehouse] = bool(row.get("selected"))
    for warehouse in expected:
        if warehouse not in by_warehouse:
            missing.append(warehouse)
    if missing:
        raise RuntimeError("UPS estimated delivery date could not be read for warehouse(s): " + ", ".join(missing))
    if not by_warehouse:
        raise RuntimeError("UPS shipping options were not available for the SanMar warehouses.")
    latest_eta = max(by_warehouse.values())
    return {"eta_by_warehouse": by_warehouse, "latest_eta": latest_eta}


def _sanmar_checkout_shipping_state(driver):
    script = r"""
const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim();
function isVisible(node) {
  if (!node) return false;
  const rect = node.getBoundingClientRect();
  if ((rect.width || 0) <= 0 || (rect.height || 0) <= 0) return false;
  for (let current = node; current; current = current.parentElement) {
    const style = window.getComputedStyle(current);
    if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return false;
    if (Number(style.opacity || 1) <= 0.03) return false;
  }
  return true;
}
function loaderDetails(node) {
  const rect = node.getBoundingClientRect();
  const style = window.getComputedStyle(node);
  const text = normalize(node.innerText || node.textContent || node.getAttribute('aria-label') || node.getAttribute('title'));
  return {
    tag: String(node.tagName || '').toLowerCase(),
    className: String(node.className || ''),
    id: String(node.id || ''),
    role: String(node.getAttribute('role') || ''),
    text: text.slice(0, 160),
    position: style.position,
    zIndex: style.zIndex,
    width: Math.round(rect.width || 0),
    height: Math.round(rect.height || 0),
  };
}
const text = normalize(document.body && (document.body.innerText || document.body.textContent) || '');
const radios = Array.from(document.querySelectorAll('input[type="radio"]')).filter(isVisible);
const upsRadios = radios.filter((radio) => {
  let scope = radio.parentElement;
  for (let depth = 0; scope && depth < 8; scope = scope.parentElement, depth += 1) {
    const scopeText = normalize(scope.innerText || scope.textContent);
    if (/\bUPS\b/i.test(scopeText)) return true;
  }
  return false;
});
const loaderSelector = [
  '[aria-busy="true"]',
  '[role="progressbar"]',
  '.blockUI',
  '.modal-backdrop',
  '.loading',
  '.loader',
  '.spinner',
  '[class*="loading"]',
  '[class*="loader"]',
  '[class*="spinner"]',
  '[class*="preloader"]',
  '[class*="progress"]',
  '[class*="busy"]',
  '[id*="loading"]',
  '[id*="loader"]',
  '[id*="spinner"]'
].join(',');
const viewportArea = Math.max(1, window.innerWidth * window.innerHeight);
const loadingNodes = Array.from(document.querySelectorAll(loaderSelector))
  .filter(isVisible)
  .filter((node) => {
    const rect = node.getBoundingClientRect();
    const style = window.getComputedStyle(node);
    const areaRatio = ((rect.width || 0) * (rect.height || 0)) / viewportArea;
    const classAndId = `${node.className || ''} ${node.id || ''}`;
    const meaningfulLoaderName = /loading|loader|spinner|preloader|progress|busy|blockUI|modal-backdrop/i.test(classAndId);
    const overlayLike = ['fixed', 'absolute', 'sticky'].includes(style.position)
      && (areaRatio >= 0.02 || Number.parseInt(style.zIndex || '0', 10) >= 10);
    return node.getAttribute('aria-busy') === 'true'
      || node.getAttribute('role') === 'progressbar'
      || meaningfulLoaderName && overlayLike;
  })
  .map(loaderDetails);
return {
  readyState: document.readyState,
  hasShippingMethodText: /Shipping\s+Method/i.test(text),
  hasUpsText: /\bUPS\b/i.test(text),
  upsRadioCount: upsRadios.length,
  radioCount: radios.length,
  loading: loadingNodes.length > 0,
  loadingNodes,
  textSample: text.slice(0, 500),
};
"""
    try:
        state = driver.execute_script(script)
    except Exception:
        return {"readyState": "complete", "loading": False, "hasShippingMethodText": True, "upsRadioCount": 0, "radioCount": 0, "syntheticReady": True}
    return state if isinstance(state, dict) else {"readyState": "complete", "loading": False, "hasShippingMethodText": True, "upsRadioCount": 0, "radioCount": 0, "syntheticReady": True}


def _wait_for_sanmar_checkout_shipping_methods(driver, timeout=75, settle_seconds=1.0):
    deadline = time.time() + max(1, float(timeout or 0))
    stable_ready_since = None
    last_state = {}
    while time.time() <= deadline:
        state = _sanmar_checkout_shipping_state(driver)
        last_state = state
        loading = bool(state.get("loading"))
        has_ups = int(state.get("upsRadioCount") or 0) > 0 or bool(state.get("hasUpsText"))
        has_shipping_method = bool(state.get("hasShippingMethodText"))
        ready_state = str(state.get("readyState") or "")
        ready = not loading and ready_state in {"interactive", "complete"} and (has_ups or has_shipping_method)
        if ready:
            if state.get("syntheticReady"):
                return state
            if stable_ready_since is None:
                stable_ready_since = time.time()
            if time.time() - stable_ready_since >= max(0, float(settle_seconds or 0)):
                return state
        else:
            stable_ready_since = None
        time.sleep(0.5)
    loader_detail = ""
    loading_nodes = last_state.get("loadingNodes") if isinstance(last_state.get("loadingNodes"), list) else []
    if loading_nodes:
        first = loading_nodes[0]
        loader_detail = f" Last visible loader: {first}."
    raise RuntimeError(f"SanMar checkout shipping methods did not finish loading within {int(timeout)} seconds.{loader_detail}")


def _capture_sanmar_checkout_diagnostic(driver, order_id=None, reason="ups_unavailable"):
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_order_id = re.sub(r"[^0-9A-Za-z_-]+", "_", str(order_id or "order"))
    safe_reason = re.sub(r"[^0-9A-Za-z_-]+", "_", str(reason or "checkout"))
    os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
    base = f"sanmar_checkout_{safe_reason}_{safe_order_id}_{stamp}"
    screenshot_path = os.path.join(SCREENSHOTS_DIR, f"{base}.png")
    text_path = os.path.join(SCREENSHOTS_DIR, f"{base}.txt")
    diagnostic = {
        "screenshot": screenshot_path,
        "text_snapshot": text_path,
        "url": str(getattr(driver, "current_url", "") or ""),
    }
    try:
        driver.save_screenshot(screenshot_path)
    except Exception as exc:
        diagnostic["screenshot_error"] = str(exc)
        diagnostic["screenshot"] = ""
    try:
        text = _normalize_text(
            driver.execute_script("return String(document.body && (document.body.innerText || document.body.textContent) || '');")
        )
        with open(text_path, "w", encoding="utf-8") as handle:
            handle.write(text[:20000])
    except Exception as exc:
        diagnostic["text_snapshot_error"] = str(exc)
        diagnostic["text_snapshot"] = ""
    return diagnostic


def _select_ups_eta_for_shipping_plan(driver, order_type, warehouse=None, selected_warehouses=None, multi_warehouse=False, order_id=None):
    try:
        _wait_for_sanmar_checkout_shipping_methods(driver)
    except RuntimeError as exc:
        if order_id:
            diagnostic = _capture_sanmar_checkout_diagnostic(driver, order_id=order_id, reason="shipping_methods_loading")
            setattr(exc, "sanmar_checkout_diagnostic", diagnostic)
        raise
    if multi_warehouse:
        try:
            eta_state = _select_ups_and_latest_eta_by_warehouse(driver, selected_warehouses)
        except RuntimeError as exc:
            if order_id:
                diagnostic = _capture_sanmar_checkout_diagnostic(driver, order_id=order_id, reason="ups_multi_warehouse")
                setattr(exc, "sanmar_checkout_diagnostic", diagnostic)
            raise
        return {
            "eta": eta_state.get("latest_eta"),
            "eta_by_warehouse": {
                name: str(value)
                for name, value in (eta_state.get("eta_by_warehouse") or {}).items()
            },
        }
    warehouse = str(warehouse or "").strip()
    scoped_error = None
    if warehouse:
        try:
            eta_state = _select_ups_and_latest_eta_by_warehouse(driver, [warehouse])
            return {
                "eta": eta_state.get("latest_eta"),
                "eta_by_warehouse": {
                    name: str(value)
                    for name, value in (eta_state.get("eta_by_warehouse") or {}).items()
                },
            }
        except RuntimeError as exc:
            scoped_error = exc
    try:
        return {"eta": _select_ups_and_eta(driver, order_type), "eta_by_warehouse": None}
    except RuntimeError as exc:
        diagnostic = _capture_sanmar_checkout_diagnostic(driver, order_id=order_id, reason="ups_unavailable") if order_id else None
        if scoped_error is not None:
            combined = RuntimeError(
                f"{exc} Selected warehouse: {warehouse}. Warehouse-scoped UPS lookup also failed: {scoped_error}"
            )
            if diagnostic is not None:
                setattr(combined, "sanmar_checkout_diagnostic", diagnostic)
            raise combined from exc
        if diagnostic is not None:
            setattr(exc, "sanmar_checkout_diagnostic", diagnostic)
        raise


def _parse_sanmar_eta(value):
    text = _normalize_text(value)
    direct = _parse_crm_date(text)
    if direct:
        return direct
    months = {
        "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
        "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
        "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
        "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
    }
    match = re.search(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:,\s*(\d{4}))?\b", text)
    if not match:
        return None
    month = months.get(match.group(1).lower())
    if not month:
        return None
    year = int(match.group(3) or datetime.now().year)
    try:
        return datetime(year, month, int(match.group(2))).date()
    except ValueError:
        return None


def _set_crm_edit_date_field(driver, label_text, target_date, allow_previous_days=0):
    target = _format_date_for_crm_input(target_date)
    iso_target = _format_date_for_crm(target_date)
    script = r"""
const labelText = String(arguments[0] || '');
const target = String(arguments[1] || '');
const isoTarget = String(arguments[2] || '');
const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim();
function isVisible(node) {
  if (!node) return false;
  const rect = node.getBoundingClientRect();
  if ((rect.width || 0) <= 0 || (rect.height || 0) <= 0) return false;
  for (let current = node; current; current = current.parentElement) {
    const style = window.getComputedStyle(current);
    if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return false;
  }
  return true;
}
function ownText(node) {
  return normalize(Array.from(node.childNodes || [])
    .filter((child) => child.nodeType === Node.TEXT_NODE)
    .map((child) => child.textContent || '')
    .join(' '));
}
const labelRx = new RegExp(labelText.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\s*:?$', 'i');
const labels = Array.from(document.querySelectorAll('body *'))
  .filter(isVisible)
  .map((node) => ({ node, text: ownText(node) || normalize(node.innerText || node.textContent) }))
  .filter((item) => labelRx.test(item.text) || normalize(item.text).toLowerCase() === labelText.toLowerCase())
  .sort((a, b) => a.text.length - b.text.length);
let best = null;
for (const item of labels) {
  const labelRect = item.node.getBoundingClientRect();
  for (let scope = item.node.parentElement, depth = 0; scope && scope !== document.body && depth < 5; scope = scope.parentElement, depth += 1) {
    const inputs = Array.from(scope.querySelectorAll('input:not([type="hidden"])')).filter(isVisible);
    for (const input of inputs) {
      const rect = input.getBoundingClientRect();
      const horizontalPenalty = rect.left >= labelRect.left ? Math.max(0, rect.left - labelRect.right) : 1000 + Math.abs(rect.left - labelRect.left);
      const verticalPenalty = Math.abs(rect.top - labelRect.top) * 8;
      const valuePenalty = /\d{1,4}[\/-]\d{1,2}[\/-]\d{1,4}/.test(input.value || '') ? 0 : 100;
      const score = (depth * 5000) + horizontalPenalty + verticalPenalty + valuePenalty + Math.abs((rect.width || 0) - 130);
      if (!best || score < best.score) best = { input, score };
    }
    if (best && depth <= 1) break;
  }
}
if (!best) return { success: false, message: labelText + ' input not found' };
const input = best.input;
const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
const modelName = normalize(input.getAttribute('ng-model'));
const dateControlHint = [
  input.type,
  modelName,
  input.getAttribute('name'),
  input.getAttribute('id'),
  input.getAttribute('placeholder'),
  input.value,
].join(' ');
const useIsoTarget = String(input.type || '').toLowerCase() === 'date'
  || /scheduledPrintDate|production|fulfillmentDate/i.test(dateControlHint)
  || /^\d{4}-\d{2}-\d{2}$/.test(normalize(input.value));
const desired = useIsoTarget ? isoTarget : target;
const dateParts = isoTarget.match(/^(\d{4})-(\d{2})-(\d{2})$/);
const localDateValue = dateParts
  ? new Date(Number(dateParts[1]), Number(dateParts[2]) - 1, Number(dateParts[3]), 12, 0, 0)
  : null;
input.scrollIntoView({ block: 'center', inline: 'center' });
input.focus();
if (setter) setter.call(input, desired);
else input.value = desired;
input.dispatchEvent(new Event('input', { bubbles: true }));
input.dispatchEvent(new Event('change', { bubbles: true }));
input.dispatchEvent(new Event('blur', { bubbles: true }));
if (window.angular && window.angular.element) {
  try {
    const ngElement = window.angular.element(input);
    const ngModel = ngElement.controller && ngElement.controller('ngModel');
    if (ngModel && ngModel.$setViewValue) {
      const angularValue = /scheduledPrintDate|production|fulfillmentDate/i.test(dateControlHint) && localDateValue
        ? localDateValue
        : desired;
      ngModel.$setViewValue(angularValue);
      if (ngModel.$commitViewValue) ngModel.$commitViewValue();
      if (ngModel.$validate) ngModel.$validate();
      if (ngModel.$render) ngModel.$render();
      const scope = ngElement.scope && ngElement.scope();
      if (scope && scope.$evalAsync) {
        scope.$evalAsync(() => {
          if (/scheduledPrintDate/i.test(modelName) && typeof scope.printDateChange === 'function') {
            scope.printDateChange();
          }
        });
      } else if (scope && scope.$applyAsync) {
        scope.$applyAsync();
      }
    }
  } catch (err) {}
}
if (normalize(input.value) !== target && normalize(input.value) !== isoTarget) {
  if (setter) setter.call(input, desired);
  else input.value = desired;
}
const value = normalize(input.value);
return { success: value === target || value === isoTarget, value, score: best.score, desired, modelName };
"""
    result = driver.execute_script(script, label_text, target, iso_target)
    if not isinstance(result, dict) or not result.get("success"):
        value = result.get("value") if isinstance(result, dict) else ""
        actual_date = _parse_crm_date(value)
        if actual_date is not None and allow_previous_days:
            delta_days = (target_date - actual_date).days
            if 0 <= delta_days <= int(allow_previous_days):
                return actual_date
        detail = f" Actual value: {value!r}." if value else ""
        raise RuntimeError(f"CRM {label_text.lower()} input was not set.{detail}")
    return target_date


def _acknowledge_crm_production_date_warning(driver, timeout=8):
    deadline = time.time() + max(0, float(timeout or 0))
    script = r"""
const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim();
function isVisible(node) {
  if (!node) return false;
  const rect = node.getBoundingClientRect();
  if ((rect.width || 0) <= 0 || (rect.height || 0) <= 0) return false;
  for (let current = node; current; current = current.parentElement) {
    const style = window.getComputedStyle(current);
    if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return false;
  }
  return true;
}
const warningRx = /ground\s+shipping\s+is\s+not\s+available|another\s+production\s+date|shipping\s+options/i;
const modalSelectors = '.modal.in,.modal.show,.modal[style*="display: block"],.modal-content,[role="dialog"],.bootbox,.swal2-popup';
const seeded = Array.from(document.querySelectorAll(modalSelectors)).filter(isVisible);
const textMatches = Array.from(document.querySelectorAll('body *'))
  .filter(isVisible)
  .filter((node) => {
    const text = normalize(node.innerText || node.textContent);
    return text && text.length <= 2500 && warningRx.test(text);
  });
const candidates = seeded.concat(textMatches)
  .sort((a, b) => normalize(a.innerText || a.textContent).length - normalize(b.innerText || b.textContent).length);
for (const candidate of candidates) {
  let modal = candidate;
  for (let current = candidate; current && current !== document.body; current = current.parentElement) {
    const text = normalize(current.innerText || current.textContent);
    const ok = Array.from(current.querySelectorAll('button,input[type="button"],input[type="submit"],a,[role="button"]'))
      .filter(isVisible)
      .find((node) => /^ok$/i.test(normalize(node.innerText || node.textContent || node.value || node.getAttribute('aria-label'))));
    if (warningRx.test(text) && ok) {
      modal = current;
      break;
    }
  }
  const text = normalize(modal.innerText || modal.textContent);
  if (!/warning/i.test(text)) continue;
  if (!warningRx.test(text)) continue;
  const ok = Array.from(modal.querySelectorAll('button,input[type="button"],input[type="submit"],a,[role="button"]'))
    .filter(isVisible)
    .find((node) => /^ok$/i.test(normalize(node.innerText || node.textContent || node.value || node.getAttribute('aria-label'))));
  if (!ok) return { success: false, found: true, message: 'warning ok button not found' };
  ok.click();
  return { success: true, found: true };
}
    return { success: false, found: false };
"""
    clicked = False
    first_check = True
    while first_check or time.time() <= deadline:
        first_check = False
        try:
            result = driver.execute_script(script)
        except Exception:
            result = None
        if isinstance(result, dict) and result.get("success"):
            clicked = True
            time.sleep(0.5)
            continue
        if isinstance(result, dict) and result.get("found"):
            time.sleep(0.5)
        else:
            if clicked:
                return True
            time.sleep(0.3)
    return clicked


def _wait_for_crm_shipping_method_selection(driver, timeout=25, initial_grace=3):
    deadline = time.time() + max(0, float(timeout or 0))
    grace_deadline = time.time() + max(0, float(initial_grace or 0))
    seen_loading = False
    script = r"""
const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim();
function isVisible(node) {
  if (!node) return false;
  const rect = node.getBoundingClientRect();
  if ((rect.width || 0) <= 0 || (rect.height || 0) <= 0) return false;
  for (let current = node; current; current = current.parentElement) {
    const style = window.getComputedStyle(current);
    if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return false;
  }
  return true;
}
const visibleText = Array.from(document.querySelectorAll('body *'))
  .filter(isVisible)
  .map((node) => node.innerText || node.textContent || node.value || '')
  .join('\n');
const text = normalize(visibleText);
return {
  selecting: /selecting\s+shipping\s+method/i.test(text),
  warning: /ground\s+shipping\s+is\s+not\s+available|another\s+production\s+date|shipping\s+options/i.test(text),
};
"""
    while time.time() <= deadline:
        try:
            state = driver.execute_script(script)
        except Exception:
            state = {}
        if isinstance(state, dict) and state.get("warning"):
            return seen_loading
        if isinstance(state, dict) and state.get("selecting"):
            seen_loading = True
            time.sleep(0.5)
            continue
        if seen_loading:
            time.sleep(0.5)
            return True
        if time.time() >= grace_deadline:
            return False
        time.sleep(0.25)
    return seen_loading


def _reload_crm_order_for_production_verify(driver, order_id):
    try:
        driver.refresh()
    except Exception:
        pass
    if _wait_for_order_goods_page_ready(driver, order_id):
        return True
    try:
        _open_target_order(driver, order_id, shipping_filter=RUSH_FILTER, list_url_override=None)
    except Exception:
        return False
    return bool(_wait_for_order_goods_page_ready(driver, order_id, timeout=max(8, CRM_ACTION_TIMEOUT)))


def _change_crm_production_date(driver, order_id, target_date):
    edit = _find_clickable_by_text(driver, r"edit\s+order")
    if edit is None:
        raise RuntimeError("CRM edit order button was not found for production date update.")
    _click_with_fallback(driver, edit)
    time.sleep(1.0)
    saved_target_date = _set_crm_edit_date_field(driver, "Production Date", target_date)
    _wait_for_crm_shipping_method_selection(driver, timeout=25)
    _acknowledge_crm_production_date_warning(driver, timeout=12)
    _wait_for_crm_shipping_method_selection(driver, timeout=6, initial_grace=1)
    save = _find_clickable_by_text(driver, r"save\s+order")
    if save is None:
        raise RuntimeError("CRM save order button was not found after production date update.")
    _click_with_fallback(driver, save)
    if _acknowledge_crm_production_date_warning(driver, timeout=5):
        save = _find_clickable_by_text(driver, r"save\s+order")
        if save is not None:
            _click_with_fallback(driver, save)
    _wait_for_text(driver, r"edit\s+order", timeout=25)
    seen_values = []
    for _attempt in range(2):
        _reload_crm_order_for_production_verify(driver, order_id)
        refreshed = _extract_order_data(driver, order_id)
        actual_date = refreshed.get("production_date")
        if actual_date == saved_target_date:
            return saved_target_date
        seen_values.append(actual_date.isoformat() if hasattr(actual_date, "isoformat") else str(actual_date or "blank"))
        time.sleep(1.0)
    expected = saved_target_date.isoformat() if hasattr(saved_target_date, "isoformat") else str(saved_target_date)
    detail = ", ".join(seen_values) if seen_values else "blank"
    raise RuntimeError(f"CRM production date did not persist after save and refresh. Expected {expected}; saw {detail}.")


def _change_crm_due_date(driver, order_id, target_date):
    edit = _find_clickable_by_text(driver, r"edit\s+order")
    if edit is None:
        raise RuntimeError("CRM edit order button was not found for due date update.")
    _click_with_fallback(driver, edit)
    time.sleep(1.0)
    saved_target_date = _set_crm_edit_date_field(driver, "Due Date", target_date)
    save = _find_clickable_by_text(driver, r"save\s+order")
    if save is None:
        raise RuntimeError("CRM save order button was not found after due date update.")
    _click_with_fallback(driver, save)
    _wait_for_text(driver, r"edit\s+order", timeout=25)
    driver.refresh()
    _wait_for_order_goods_page_ready(driver, order_id)
    refreshed = _extract_order_data(driver, order_id)
    if refreshed.get("due_date") != saved_target_date:
        raise RuntimeError("CRM due date did not persist after save.")
    return saved_target_date


def _append_crm_production_note(driver, order_id, note):
    note = str(note or "").strip()
    if not note:
        return "no_note"
    edit = _find_clickable_by_text(driver, r"edit\s+order")
    if edit is None:
        raise RuntimeError("CRM edit order button was not found for production note update.")
    _click_with_fallback(driver, edit)
    time.sleep(1.0)
    script = r"""
const note = arguments[0];
function normalize(value) { return String(value || '').replace(/\s+/g, ' ').trim(); }
function isVisible(node) {
  if (!node) return false;
  const rect = node.getBoundingClientRect();
  if ((rect.width || 0) <= 0 || (rect.height || 0) <= 0) return false;
  for (let current = node; current; current = current.parentElement) {
    const style = window.getComputedStyle(current);
    if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return false;
  }
  return true;
}
const labels = Array.from(document.querySelectorAll('body *')).filter((node) => /Production Notes/i.test(node.innerText || node.textContent || ''));
let textarea = null;
for (const label of labels) {
  for (let scope = label; scope && scope !== document.body; scope = scope.parentElement) {
    const areas = Array.from(scope.querySelectorAll('textarea')).filter(isVisible);
    if (areas.length) {
      textarea = areas.sort((a, b) => {
        const ar = a.getBoundingClientRect();
        const br = b.getBoundingClientRect();
        return (ar.top - br.top) || (ar.left - br.left);
      })[0];
      break;
    }
  }
  if (textarea) break;
}
if (!textarea) {
  textarea = Array.from(document.querySelectorAll('textarea')).filter(isVisible).sort((a, b) => {
    const ar = a.getBoundingClientRect();
    const br = b.getBoundingClientRect();
    return ((br.width * br.height) - (ar.width * ar.height));
  })[0] || null;
}
if (!textarea) return { success: false, message: 'Production Notes textarea was not found.' };
const current = String(textarea.value || textarea.textContent || '').trim();
if (normalize(current).includes(normalize(note))) return { success: true, alreadyPresent: true };
const next = current ? `${current}\n${note}` : note;
textarea.focus();
textarea.value = next;
textarea.dispatchEvent(new Event('input', { bubbles: true }));
textarea.dispatchEvent(new Event('change', { bubbles: true }));
return { success: true, alreadyPresent: false };
"""
    result = driver.execute_script(script, note)
    if not isinstance(result, dict) or not result.get("success"):
        raise RuntimeError((result or {}).get("message") or "CRM production note could not be filled.")
    save = _find_clickable_by_text(driver, r"save\s+order")
    if save is None:
        raise RuntimeError("CRM save order button was not found after production note update.")
    _click_with_fallback(driver, save)
    _wait_for_text(driver, r"edit\s+order", timeout=25)
    driver.refresh()
    _wait_for_order_goods_page_ready(driver, order_id)
    return "already_present" if result.get("alreadyPresent") else "recorded"


def _fill_review_and_submit(driver, po, dry_run=False):
    normalized_po = _normalize_text(po)
    if not normalized_po:
        raise RuntimeError("Customer PO is empty; refusing to submit SanMar order.")
    text = _normalize_text(driver.execute_script("return String(document.body && (document.body.innerText || document.body.textContent) || '');"))
    if not re.search(r"\bNet\b", text, flags=re.I):
        raise RuntimeError("SanMar payment method NET was not visible on review page.")
    script = r"""
const po = arguments[0];
const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim();
function isVisible(node) {
  if (!node) return false;
  const rect = node.getBoundingClientRect();
  if ((rect.width || 0) <= 0 || (rect.height || 0) <= 0) return false;
  for (let current = node; current; current = current.parentElement) {
    const style = window.getComputedStyle(current);
    if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return false;
  }
  return true;
}
function ownText(node) {
  return normalize(Array.from(node.childNodes || [])
    .filter((child) => child.nodeType === Node.TEXT_NODE)
    .map((child) => child.textContent || '')
    .join(' '));
}
const labels = Array.from(document.querySelectorAll('label,span,div,p,strong,b')).filter((node) => {
  if (!isVisible(node)) return false;
  const text = ownText(node) || normalize(node.innerText || node.textContent);
  return /^Customer\s+PO\b/i.test(text) && text.length <= 80;
});
const inputs = Array.from(document.querySelectorAll('input:not([type="hidden"])')).filter(isVisible);
let input = null;
let best = null;
for (const label of labels) {
  const labelRect = label.getBoundingClientRect();
  for (const candidate of inputs) {
    const rect = candidate.getBoundingClientRect();
    const dy = Math.abs((rect.top + rect.height / 2) - (labelRect.top + labelRect.height / 2));
    const toRight = rect.left >= labelRect.left - 20 ? 0 : 10000;
    const distance = Math.abs(rect.left - labelRect.left) + dy * 4 + toRight;
    if (!best || distance < best.distance) best = { input: candidate, distance };
  }
}
input = best ? best.input : null;
if (!input) {
  input = inputs.find((node) => {
    const rect = node.getBoundingClientRect();
    const label = String(node.getAttribute('aria-label') || node.getAttribute('name') || node.getAttribute('id') || node.placeholder || '');
    return (rect.width || 0) > 160 && (rect.height || 0) > 0 && /po|purchase/i.test(label);
  });
}
if (!input) return { success: false, value: '', message: 'Customer PO input not found' };
const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
input.focus();
setter.call(input, po);
input.dispatchEvent(new Event('input', { bubbles: true }));
input.dispatchEvent(new Event('change', { bubbles: true }));
input.dispatchEvent(new Event('blur', { bubbles: true }));
return { success: String(input.value || '').trim() === String(po || '').trim(), value: String(input.value || '').trim() };
"""
    result = driver.execute_script(script, normalized_po)
    if not isinstance(result, dict) or not result.get("success"):
        actual = result.get("value") if isinstance(result, dict) else ""
        detail = f" Actual value: {actual!r}." if actual else ""
        raise RuntimeError(f"Customer PO was not filled and verified on SanMar review page; refusing to submit order.{detail}")
    time.sleep(0.5)
    verify_script = r"""
const po = String(arguments[0] || '').trim();
const inputs = Array.from(document.querySelectorAll('input:not([type="hidden"])')).filter((node) => {
  const rect = node.getBoundingClientRect();
  return (rect.width || 0) > 0 && (rect.height || 0) > 0;
});
return inputs.some((node) => String(node.value || '').trim() === po);
"""
    if driver.execute_script(verify_script, normalized_po) is not True:
        raise RuntimeError("Customer PO disappeared before submit; refusing to submit SanMar order.")
    if dry_run:
        return "dry_run_review_ready"
    _click_sanmar_button(driver, r"Submit\s+Order")
    success_text = _wait_for_text(driver, r"Thank You For Your Order|Web Reference", timeout=30)
    if not success_text:
        raise RuntimeError("SanMar submit did not reach the thank-you page.")
    return "submitted"


def _capture_sanmar_confirmation(driver, order_id, po):
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_order_id = re.sub(r"[^0-9A-Za-z_-]+", "_", str(order_id or "order"))
    screenshot_dir = SCREENSHOTS_DIR
    os.makedirs(screenshot_dir, exist_ok=True)
    screenshot_path = os.path.join(screenshot_dir, f"sanmar_shipping_bypass_{safe_order_id}_{stamp}.png")
    normalized_po = str(po or "").strip()
    result = {
        "url": str(getattr(driver, "current_url", "") or ""),
        "screenshot": screenshot_path,
        "web_reference": "",
        "po": po,
        "po_confirmed": False,
        "order_summary_opened": False,
    }

    def _page_text():
        return _normalize_text(
            driver.execute_script("return String(document.body && (document.body.innerText || document.body.textContent) || '');")
        )

    def _read_confirmation_text(text):
        web_reference = re.search(r"Web Reference\s*#?\s*:?\s*([A-Za-z0-9-]+)", text, flags=re.I)
        if web_reference:
            result["web_reference"] = web_reference.group(1)
        result["po_confirmed"] = bool(normalized_po and re.search(re.escape(normalized_po), text, flags=re.I))

    try:
        text = _page_text()
        _read_confirmation_text(text)
        if not result["po_confirmed"] and re.search(r"View\s+Order\s+Summary", text, flags=re.I):
            try:
                _click_sanmar_text_control(driver, r"View\s+Order\s+Summary", timeout=8)
                result["order_summary_opened"] = True
                _wait_for_text(
                    driver,
                    rf"{re.escape(normalized_po)}|Customer\s+PO|Order\s+Summary|Web\s+Reference",
                    timeout=20,
                )
                result["url"] = str(getattr(driver, "current_url", "") or result["url"])
                _read_confirmation_text(_page_text())
            except Exception as exc:
                result["order_summary_error"] = str(exc)
    except Exception:
        pass
    try:
        driver.save_screenshot(screenshot_path)
    except Exception as exc:
        result["screenshot_error"] = str(exc)
        result["screenshot"] = ""
    return result


def _manual_order_vendor_label(vendor_name):
    vendor = _normalize_text(vendor_name) or "Sanmar"
    normalized = vendor.lower().replace("&", "and")
    normalized = re.sub(r"\s+", " ", normalized)
    if "s and s" in normalized or "ss activewear" in normalized:
        return "S&S Activewear"
    if "sanmar" in normalized:
        return "Sanmar"
    return vendor


def _reopen_crm_manual_order_target(driver, order_id, order_url=None):
    if order_url:
        safe_get_with_partial_load(driver, order_url, f"CRM order {order_id} manual order retry")
        return
    _open_target_order(driver, order_id, shipping_filter=RUSH_FILTER, list_url_override=None)


def _record_crm_manual_order(driver, order_id, po, dry_run=False, stock_tab_index=None, vendor_name=None, order_url=None):
    vendor_label = _manual_order_vendor_label(vendor_name)
    if dry_run:
        return "dry_run_manual_order_ready"
    script = r"""
function normalize(value) { return String(value || '').replace(/\s+/g, ' ').trim(); }
function isVisible(node) {
  if (!node) return false;
  const rect = node.getBoundingClientRect();
  if ((rect.width || 0) <= 0 || (rect.height || 0) <= 0) return false;
  for (let current = node; current; current = current.parentElement) {
    const style = window.getComputedStyle(current);
    if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return false;
  }
  return true;
}
const controls = Array.from(document.querySelectorAll('button,input[type="button"],input[type="submit"],a,[role="button"],[ng-click],[onclick],.btn,[class*="btn"]')).filter(isVisible);
let best = null;
for (const control of controls) {
  const text = normalize(control.innerText || control.textContent || control.value || control.getAttribute('aria-label')).toLowerCase();
  if (!text.includes('order goods') && !text.includes('add box')) continue;
  let score = 999999;
  for (let scope = control; scope && scope !== document.body; scope = scope.parentElement) {
    const scopeText = normalize(scope.innerText || scope.textContent).toLowerCase();
    if (scopeText.includes('manual order')) {
      score = normalize(scope.innerText || scope.textContent).length;
      break;
    }
  }
  if (!best || score < best.score) best = { control, score };
}
return best ? best.control : null;
"""
    button = None
    last_text = ""
    for attempt in range(4):
        if attempt == 0:
            try:
                driver.refresh()
            except Exception:
                pass
        else:
            try:
                _reopen_crm_manual_order_target(driver, order_id, order_url=order_url)
            except Exception:
                safe_get_with_partial_load(driver, f"https://crm2.legacy.printfly.com/order/{order_id}", f"CRM order {order_id} manual order retry")
        _wait_for_order_goods_page_ready(driver, order_id, timeout=max(10, CRM_ACTION_TIMEOUT))
        if stock_tab_index is not None:
            _activate_stock_tab(driver, int(stock_tab_index) - 1)
        time.sleep(0.8)
        try:
            button = driver.execute_script(script)
        except Exception:
            button = None
        if button is not None:
            break
        try:
            last_text = _normalize_text(driver.execute_script("return String(document.body && (document.body.innerText || document.body.textContent) || '');"))
        except Exception:
            last_text = ""
        time.sleep(1.0)
    if button is None:
        if _crm_manual_order_row_exists(driver, po, vendor_name=vendor_label):
            return "already_recorded"
        raise RuntimeError("CRM Manual Order order goods/add box button was not found after reopening the order.")
    _click_with_fallback(driver, button)
    time.sleep(1.0)
    script = r"""
const po = arguments[0];
const vendorName = String(arguments[1] || 'Sanmar').replace(/\s+/g, ' ').trim();
function isVisible(node) {
  if (!node) return false;
  const rect = node.getBoundingClientRect();
  return (rect.width || 0) > 0 && (rect.height || 0) > 0;
}
function setNativeValue(node, value) {
  const proto = node.tagName === 'SELECT' ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
  if (setter) setter.call(node, value);
  else node.value = value;
}
function emit(node, name) {
  node.dispatchEvent(new Event(name, { bubbles: true }));
  if (window.angular) {
    try { window.angular.element(node).triggerHandler(name); } catch (error) {}
  }
}
function digest(node) {
  if (!window.angular) return;
  try {
    const scope = window.angular.element(node).scope() || window.angular.element(node).isolateScope();
    if (scope && !scope.$$phase) scope.$applyAsync ? scope.$applyAsync() : scope.$apply();
  } catch (error) {}
}
function vendorAliases(value) {
  const normalized = String(value || '').replace(/&/g, 'and').replace(/\s+/g, ' ').trim().toLowerCase();
  if (/^(?:s\s*and\s*s|ss)\s+activewear$/.test(normalized) || normalized.includes('s and s activewear') || normalized.includes('ss activewear')) {
    return ['S&S Activewear', 'S and S Activewear', 'SS Activewear'];
  }
  if (normalized.includes('sanmar')) return ['Sanmar'];
  return [value];
}
function vendorMatches(text, wanted) {
  const haystack = String(text || '').replace(/&/g, 'and').replace(/\s+/g, ' ').trim().toLowerCase();
  return vendorAliases(wanted).some((alias) => {
    const needle = String(alias || '').replace(/&/g, 'and').replace(/\s+/g, ' ').trim().toLowerCase();
    return needle && haystack.includes(needle);
  });
}
const modal = Array.from(document.querySelectorAll('.modal.in .modal-content,.modal[style*="display: block"] .modal-content,.modal-content'))
  .filter(isVisible)
  .find((node) => /Manual Order/i.test(node.innerText || node.textContent || ''))
  || document;
const inputs = Array.from(modal.querySelectorAll('input:not([type="hidden"])')).filter(isVisible);
if (!inputs.length) return { success: false, message: 'PO input not found.' };
inputs[0].focus();
setNativeValue(inputs[0], po);
emit(inputs[0], 'input');
emit(inputs[0], 'change');
digest(inputs[0]);
const selects = Array.from(modal.querySelectorAll('select')).filter(isVisible);
if (selects.length) {
  const option = Array.from(selects[0].options || []).find((item) => vendorMatches(`${item.textContent || ''} ${item.value || ''}`, vendorName));
  if (!option) return { success: false, message: `${vendorName} vendor option not found.` };
  setNativeValue(selects[0], option.value);
  option.selected = true;
  emit(selects[0], 'input');
  emit(selects[0], 'change');
  digest(selects[0]);
} else {
  const dropdown = Array.from(modal.querySelectorAll('[role="combobox"],[aria-haspopup="listbox"],button,div')).filter(isVisible).find((node) => {
    const text = String(node.innerText || node.textContent || '').toLowerCase();
    return text === '' || text.includes('vendor') || text.includes('select');
  });
  if (dropdown) dropdown.click();
  const dropdownRect = dropdown ? dropdown.getBoundingClientRect() : { top: 0, bottom: window.innerHeight, left: 0, right: window.innerWidth };
  const option = Array.from(document.querySelectorAll('[role="option"],[role="listbox"] *,li,a,button,span,div'))
    .filter(isVisible)
    .find((node) => {
      const text = String(node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim();
      if (!vendorMatches(text, vendorName)) return false;
      const rect = node.getBoundingClientRect();
      const className = String(node.className || '').toLowerCase();
      const optionish = node.getAttribute('role') === 'option' || node.closest('[role="listbox"]') || /option|select|dropdown|ui-select/.test(className);
      const nearDropdown = rect.top >= dropdownRect.top - 20
        && rect.top <= dropdownRect.bottom + 500
        && rect.left >= dropdownRect.left - 80
        && rect.left <= dropdownRect.right + 180;
      return optionish || nearDropdown;
    });
  if (!option) return { success: false, message: `${vendorName} dropdown option not found.` };
  option.click();
}
return { success: true };
"""
    result = driver.execute_script(script, po, vendor_label)
    if not isinstance(result, dict) or not result.get("success"):
        raise RuntimeError((result or {}).get("message") or "CRM manual order popup could not be filled.")
    time.sleep(1.0)
    script = r"""
function isVisible(node) {
  if (!node) return false;
  const rect = node.getBoundingClientRect();
  return (rect.width || 0) > 0 && (rect.height || 0) > 0;
}
const modal = Array.from(document.querySelectorAll('.modal.in,.modal[style*="display: block"],.modal-content')).filter(isVisible)[0] || document;
let save = Array.from(modal.querySelectorAll('button,input[type="button"],input[type="submit"],a,[role="button"]')).filter(isVisible).find((node) => {
  const text = String(node.innerText || node.textContent || node.value || '').replace(/\s+/g, ' ').trim();
  return /^save$/i.test(text);
});
if (!save) {
  save = Array.from(document.querySelectorAll('button,input[type="button"],input[type="submit"],a,[role="button"]')).filter(isVisible).find((node) => {
    const text = String(node.innerText || node.textContent || node.value || '').replace(/\s+/g, ' ').trim();
    return /^save$/i.test(text);
  });
}
if (!save) return { success: false, message: 'Manual order save button not found.' };
if (save.disabled || save.getAttribute('disabled') !== null || save.getAttribute('aria-disabled') === 'true') return { success: false, message: 'Manual order save button is disabled after filling PO/vendor.' };
save.click();
return { success: true };
"""
    result = driver.execute_script(script)
    if not isinstance(result, dict) or not result.get("success"):
        raise RuntimeError((result or {}).get("message") or "CRM manual order popup could not be saved.")
    deadline = time.time() + max(18, min(30, CRM_ACTION_TIMEOUT * 2))
    while time.time() < deadline:
        if _crm_manual_order_row_exists(driver, po, vendor_name=vendor_label):
            time.sleep(1.0)
            try:
                _wait_for_order_goods_page_ready(driver, order_id, timeout=max(8, CRM_ACTION_TIMEOUT))
            except Exception:
                pass
            return "recorded"
        time.sleep(0.8)
    for _attempt in range(2):
        _reopen_crm_manual_order_target(driver, order_id, order_url=order_url)
        _wait_for_order_goods_page_ready(driver, order_id, timeout=max(8, CRM_ACTION_TIMEOUT))
        if stock_tab_index is not None:
            _activate_stock_tab(driver, int(stock_tab_index) - 1)
        if _crm_manual_order_row_exists(driver, po, vendor_name=vendor_label):
            return "recorded"
        time.sleep(1.0)
    raise RuntimeError(f"CRM Manual Order save did not produce a visible {vendor_label} manual-order row.")


def _crm_manual_order_row_exists(driver, po, vendor_name=None):
    script = r"""
const po = String(arguments[0] || '').toLowerCase();
const vendorName = String(arguments[1] || '').replace(/\s+/g, ' ').trim();
const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim().toLowerCase();
const escapedPo = po.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
const poPattern = new RegExp(escapedPo + '(?:\\b|-[a-z0-9]+\\b)', 'i');
function escapeRegex(value) {
  return String(value || '').replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}
function vendorPatternFor(value) {
  const normalized = String(value || '').replace(/&/g, 'and').replace(/\s+/g, ' ').trim().toLowerCase();
  if (!normalized) return /\b(?:sanmar|s\s*&\s*s(?:\s+activewear)?|ssactivewear|s\s+and\s+s(?:\s+activewear)?)\b/i;
  if (normalized.includes('sanmar')) return /\bsanmar\b/i;
  if (normalized.includes('s and s activewear') || normalized.includes('ss activewear')) {
    return /\b(?:s\s*&\s*s(?:\s+activewear)?|ssactivewear|s\s+and\s+s(?:\s+activewear)?)\b/i;
  }
  return new RegExp(escapeRegex(value).replace(/\\\s+/g, '\\s+'), 'i');
}
const stockVendorPattern = vendorPatternFor(vendorName);
function isVisible(node) {
  if (!node) return false;
  const rect = node.getBoundingClientRect();
  if ((rect.width || 0) <= 0 || (rect.height || 0) <= 0) return false;
  for (let current = node; current; current = current.parentElement) {
    const style = window.getComputedStyle(current);
    if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return false;
  }
  return true;
}
function textMatches(node) {
  if (!node || node.closest('.modal')) return false;
  const text = normalize(node.innerText || node.textContent);
  if (text.length > 700) return false;
  return poPattern.test(text) && stockVendorPattern.test(text);
}
return Array.from(document.querySelectorAll('tr,[role="row"],li')).filter(isVisible).some(textMatches);
"""
    try:
        if vendor_name:
            return driver.execute_script(script, po, _manual_order_vendor_label(vendor_name)) is True
        return driver.execute_script(script, po) is True
    except Exception:
        return False


def _crm_stock_order_yellow_visual_exists(driver, po):
    script = r"""
const po = String(arguments[0] || '').toLowerCase();
const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim().toLowerCase();
const escapedPo = po.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
const poPattern = new RegExp(escapedPo + '(?:\\b|-[a-z0-9]+\\b)', 'i');
const stockVendorPattern = /\b(?:sanmar|s\s*&\s*s(?:\s+activewear)?|ssactivewear|s\s+and\s+s(?:\s+activewear)?)\b/i;
function isVisible(node) {
  if (!node) return false;
  const rect = node.getBoundingClientRect();
  if ((rect.width || 0) <= 0 || (rect.height || 0) <= 0) return false;
  for (let current = node; current; current = current.parentElement) {
    const style = window.getComputedStyle(current);
    if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return false;
  }
  return true;
}
function yellowish(node) {
  const values = [node, ...Array.from(node.children || [])].map((item) => window.getComputedStyle(item).backgroundColor || '');
  return values.some((value) => {
    const match = String(value).match(/rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([0-9.]+))?\)/i);
    if (!match) return /yellow/i.test(value);
    const r = Number(match[1]);
    const g = Number(match[2]);
    const b = Number(match[3]);
    const a = match[4] === undefined ? 1 : Number(match[4]);
    if (a <= 0.05) return false;
    return r >= 220 && g >= 220 && b <= 210 && Math.min(r, g) - b >= 25;
  });
}
return Array.from(document.querySelectorAll('tr')).filter(isVisible).some((row) => {
  if (row.closest('.modal')) return false;
  const text = normalize(row.innerText || row.textContent);
  return poPattern.test(text) && stockVendorPattern.test(text) && yellowish(row);
});
"""
    try:
        return driver.execute_script(script, po) is True
    except Exception:
        return False


def _historical_shipping_bypass_po_exists(po, state_path=CRM_STATE_PATH):
    po_text = str(po or "").strip().lower()
    if not po_text:
        return False
    return po_text in _load_historical_shipping_bypass_customer_pos(state_path=state_path)


def _attach_stock_tab_context(result, stock_tab_index=None, stock_tab_count=None, stock_tab_label=None):
    if not isinstance(result, dict):
        return result
    if stock_tab_index is not None:
        result["stock_tab_index"] = int(stock_tab_index)
    if stock_tab_count is not None:
        result["stock_tab_count"] = int(stock_tab_count)
    stock_tab_label = _stock_tab_summary_label(stock_tab_label)
    if stock_tab_label:
        result["stock_tab_label"] = stock_tab_label
    return result


def _shipping_bypasser_actionable_stock_tabs(driver, order_id, tabs):
    actionable = []
    skipped = []
    for tab_index, tab in enumerate(tabs if isinstance(tabs, list) else []):
        tab_number = tab_index + 1
        tab_label = _stock_tab_summary_label((tab or {}).get("label"))
        try:
            activated = _activate_stock_tab(driver, tab_index)
            if activated is None:
                actionable.append((tab_index, tab, "stock_tab_not_found"))
                continue
            effective_label = _stock_tab_summary_label((activated or {}).get("label") or tab_label)
            order = _extract_order_data(driver, order_id, tab_context=tab)
            po = order.get("po")
            if po and _crm_stock_order_yellow_visual_exists(driver, po):
                skipped.append({"tab_number": tab_number, "tab_label": effective_label, "po": po, "reason": "crm_yellow_manual_order"})
            elif po and _crm_manual_order_row_exists(driver, po):
                skipped.append({"tab_number": tab_number, "tab_label": effective_label, "po": po, "reason": "crm_manual_order"})
            elif po and _historical_shipping_bypass_po_exists(po):
                skipped.append({"tab_number": tab_number, "tab_label": effective_label, "po": po, "reason": "shipping_bypasser_history"})
            elif order.get("active_panel_stock_ordered"):
                skipped.append({"tab_number": tab_number, "tab_label": effective_label, "po": po, "reason": "active_panel_stock_ordered"})
            else:
                actionable.append((tab_index, tab, "needs_shipping_bypass"))
        except Exception as exc:
            actionable.append((tab_index, tab, f"preflight_error:{type(exc).__name__}:{exc}"))
    return actionable, skipped


def _process_open_order(crm_driver, sanmar_driver, order_id, dry_run=False, stock_tab_index=None, stock_tab_count=None, stock_tab_label=None, stock_tab_context=None):
    _wait_for_order_goods_page_ready(crm_driver, order_id)
    stock_tab_label = _stock_tab_summary_label(stock_tab_label)
    stock_tab_context = stock_tab_context if isinstance(stock_tab_context, dict) else {}
    stock_tab_context.update(
        {
            "stock_tab_index": stock_tab_index,
            "stock_tab_count": stock_tab_count,
            "stock_tab_label": stock_tab_label,
            "label": stock_tab_context.get("label") or stock_tab_label,
        }
    )

    def done(result):
        return _attach_stock_tab_context(result, stock_tab_index, stock_tab_count, stock_tab_label)

    tab_suffix = f" stock tab {stock_tab_index}/{stock_tab_count}" if stock_tab_count > 1 else ""
    _publish_status(
        f"Reading CRM stock/order data for Shipping Bypasser order {order_id}{tab_suffix}.",
        stage="reading_crm_order",
        order_id=order_id,
    )
    order = _extract_order_data(crm_driver, order_id, tab_context=stock_tab_context)
    already_ordered_source = ""
    if order.get("po") and _crm_stock_order_yellow_visual_exists(crm_driver, order["po"]):
        already_ordered_source = "crm_yellow_manual_order"
    elif order.get("po") and _crm_manual_order_row_exists(crm_driver, order["po"]):
        already_ordered_source = "crm_manual_order"
    if already_ordered_source:
        if already_ordered_source == "crm_yellow_manual_order":
            message = f"Skipped because CRM shows a highlighted Manual Order stock row for PO {order['po']}."
        else:
            message = f"Skipped because Sanmar Manual Order PO {order['po']} is already recorded for this tab."
        if stock_tab_label:
            message = f"{message} Stock tab: {stock_tab_label}."
        return done(
            _result(
                order_id,
                True,
                "already_stock_ordered",
                message,
                manual_review_required=False,
                order=order,
                duplicate_guard=already_ordered_source,
            )
        )
    pending_submission = _pending_shipping_bypass_submission(order_id, order.get("po"))
    if pending_submission:
        try:
            _publish_status(
                f"Recording CRM Manual Order from previous SanMar confirmation for Shipping Bypasser order {order_id}.",
                stage="recording_crm_manual_order",
                order_id=order_id,
            )
            record_state = _record_crm_manual_order(
                crm_driver,
                order_id,
                order["po"],
                dry_run=dry_run,
                stock_tab_index=stock_tab_index,
                vendor_name=pending_submission.get("vendor") or "Sanmar",
            )
            if not dry_run:
                _mark_pending_shipping_bypass_submission_recorded(order_id, order["po"], record_state)
            message = "SanMar stock was already submitted previously and was recorded in CRM Manual Order."
            if stock_tab_label:
                message = f"{message} Stock tab: {stock_tab_label}."
            return done(_result(
                order_id,
                True,
                "shipping_bypass_ordered",
                message,
                manual_review_required=False,
                order=order,
                sanmar_confirmation=pending_submission.get("sanmar_confirmation") or {},
                sanmar_submit_state="previously_submitted",
                crm_record_state=record_state,
                duplicate_guard="pending_sanmar_confirmation",
            ))
        except Exception as exc:
            return done(_result(
                order_id,
                False,
                "pending_sanmar_submitted_crm_record_failed",
                f"SanMar order was previously submitted, but CRM Manual Order could not be recorded: {exc}",
                order=order,
                sanmar_confirmation=pending_submission.get("sanmar_confirmation") or {},
                sanmar_submit_state="previously_submitted",
                manual_review_required=True,
                retryable=False,
            ))
    if order.get("po") and _historical_shipping_bypass_po_exists(order["po"]):
        message = f"Skipped because SanMar customer PO {order['po']} was already confirmed by a previous Shipping Bypasser run."
        if stock_tab_label:
            message = f"{message} Stock tab: {stock_tab_label}."
        return done(
            _result(
                order_id,
                True,
                "already_stock_ordered",
                message,
                manual_review_required=False,
                order=order,
                duplicate_guard="shipping_bypasser_history",
            )
        )
    if order.get("active_panel_stock_ordered"):
        message = "Skipped because stock is already ordered for this tab."
        if stock_tab_label:
            message = f"{message} Stock tab: {stock_tab_label}."
        return done(
            _result(
                order_id,
                True,
                "already_stock_ordered",
                message,
                manual_review_required=False,
            )
        )

    missing = [name for name in ("po", "product_id", "color", "production_date", "due_date") if not order.get(name)]
    if missing:
        return done(_result(order_id, False, "crm_data_missing", f"Missing CRM order data: {', '.join(missing)}.", order=order))
    if not order.get("quantities"):
        return done(_result(order_id, False, "crm_quantities_missing", "Could not map CRM size quantities from the order.", order=order))
    if order["order_type"] == "mach6" and "mach 6" not in order.get("subcontractor", "").lower():
        return done(_result(order_id, False, "subcontractor_mismatch", f"Unsupported subcontractor: {order.get('subcontractor')}.", order=order))

    _publish_status(f"Loading SanMar for order {order_id}.", stage="loading_sanmar", order_id=order_id)
    safe_get_with_partial_load(sanmar_driver, SANMAR_URL, label="SanMar home")
    _ensure_sanmar_logged_in(sanmar_driver)
    _publish_status(f"Checking SanMar cart before order {order_id}.", stage="checking_sanmar_cart", order_id=order_id)
    cart = _sanmar_cart_has_items(sanmar_driver)
    if cart.get("hasItems"):
        safe_get_with_partial_load(sanmar_driver, SANMAR_CART_URL, label="SanMar cart")
        return done(_result(order_id, False, "sanmar_cart_not_empty", "SanMar shopping box already has items. Use Open SanMar Cart for review.", order=order, stop_run=True))

    products = order.get("products") if isinstance(order.get("products"), list) else []
    if not products:
        return done(_result(order_id, False, "crm_products_missing", "Could not map any CRM stock products from the order.", order=order))
    invalid_products = [
        product for product in products
        if not product.get("product_id") or not product.get("color") or not product.get("quantities")
    ]
    if invalid_products:
        labels = ", ".join(str(product.get("index") or "?") for product in invalid_products)
        return done(_result(order_id, False, "crm_product_data_missing", f"Missing product/color/size data for CRM product block(s): {labels}.", order=order))

    product_lines = []
    for product_index, product in enumerate(products, start=1):
        search_options = _sanmar_search_options_for_product(product)
        search_id = search_options["search_id"]
        sanmar_quantities = _sanmar_quantities_for_product(product)
        if not sanmar_quantities:
            return done(_result(order_id, False, "crm_quantities_missing", f"Could not normalize CRM size quantities for SanMar product {product.get('product_id')}.", order=order, product=product))
        try:
            _publish_status(
                f"Searching SanMar product {search_id} ({product_index}/{len(products)}) for order {order_id}.",
                stage="searching_sanmar_product",
                order_id=order_id,
            )
            _search_sanmar_product(
                sanmar_driver,
                search_id,
                click_inventory_button=search_options.get("click_inventory_button"),
                expected_style_keys=search_options.get("expected_style_keys"),
            )
        except Exception as exc:
            return done(_result(order_id, False, "sanmar_product_not_found", f"SanMar product could not be found for {search_id}: {exc}", order=order, product=product))
        try:
            _publish_status(
                f"Selecting SanMar color {product['color']} for product {search_id}.",
                stage="selecting_sanmar_color",
                order_id=order_id,
            )
            _select_sanmar_color(sanmar_driver, product["color"], product=product)
        except Exception as exc:
            return done(_result(order_id, False, "sanmar_color_not_found", f"SanMar color could not be selected for {search_id} / {product['color']}: {exc}", order=order, product=product))
        _publish_status(f"Reading SanMar inventory for product {search_id}.", stage="reading_sanmar_inventory", order_id=order_id)
        inventory = _wait_for_sanmar_inventory(sanmar_driver, search_id)
        if not inventory:
            return done(_result(order_id, False, "sanmar_inventory_missing", f"SanMar inventory table was not found for {search_id}.", order=order, product=product))
        product_lines.append(
            {
                "product": product,
                "search_id": search_id,
                "quantities": sanmar_quantities,
                "search_handler": search_options.get("handler") or "",
                "click_inventory_button": bool(search_options.get("click_inventory_button")),
                "expected_style_keys": search_options.get("expected_style_keys") or [],
                "inventory": inventory,
            }
        )

    warehouse, warehouse_plan = _choose_warehouse_plan(product_lines, order["order_type"])
    if not warehouse_plan:
        return done(_result(order_id, False, "no_single_warehouse", _single_warehouse_failure_message(product_lines, order["order_type"]), order=order, products=product_lines))

    multi_warehouse = str(warehouse_plan.get("mode") or "") == "multi_warehouse"
    selected_warehouses = warehouse_plan.get("warehouses") or ([warehouse] if warehouse else [])
    warehouse = _single_warehouse_from_plan(warehouse, warehouse_plan)
    cart_product_lines = warehouse_plan.get("expanded_lines") or []

    for line_index, line in enumerate(cart_product_lines, start=1):
        product = line["product"]
        search_id = line["search_id"]
        _publish_status(
            f"Adding SanMar product {search_id} to cart ({line_index}/{len(cart_product_lines)}) for order {order_id}.",
            stage="adding_sanmar_cart",
            order_id=order_id,
        )
        _search_sanmar_product(
            sanmar_driver,
            search_id,
            click_inventory_button=bool(line.get("click_inventory_button")),
            expected_style_keys=line.get("expected_style_keys"),
        )
        _select_sanmar_color(sanmar_driver, product["color"], product=product)
        _fill_sanmar_quantities(sanmar_driver, line.get("warehouse") or warehouse, line["quantities"])
        _add_current_product_to_box(sanmar_driver)

    _publish_status(f"Validating SanMar cart for order {order_id}.", stage="validating_sanmar_cart", order_id=order_id)
    safe_get_with_partial_load(sanmar_driver, SANMAR_CART_URL, label="SanMar cart")
    _wait_for_text(sanmar_driver, r"Continue\s+Checkout|Warehouse", timeout=20)
    cart_validation = _validate_sanmar_cart_contents(sanmar_driver, cart_product_lines, warehouse=None if multi_warehouse else warehouse)
    if not cart_validation.get("success"):
        issues = cart_validation.get("issues") or ["SanMar cart did not match the CRM products."]
        return done(_result(
            order_id,
            False,
            "sanmar_cart_mismatch",
            "SanMar cart does not match CRM product/size quantities: " + " ".join(issues[:4]),
            order=order,
            warehouse=warehouse,
            warehouses=selected_warehouses,
            products=product_lines,
            warehouse_plan=warehouse_plan,
            sanmar_cart_validation=cart_validation,
            manual_review_required=True,
            retryable=False,
        ))
    _click_sanmar_button(sanmar_driver, r"Continue\s+Checkout")
    _wait_for_text(sanmar_driver, r"Shipping\s+Details|Shipping\s+Address", timeout=20)
    _publish_status(f"Selecting SanMar shipping for order {order_id}.", stage="selecting_sanmar_shipping", order_id=order_id)
    shipping = _select_shipping_destination(sanmar_driver, order["order_type"], warehouse, multi_warehouse=multi_warehouse)
    eta = None
    eta_by_warehouse = None
    if shipping.get("ship_mode") == "ship":
        try:
            eta_state = _select_ups_eta_for_shipping_plan(
                sanmar_driver,
                order["order_type"],
                warehouse=warehouse,
                selected_warehouses=selected_warehouses,
                multi_warehouse=multi_warehouse,
                order_id=order_id,
            )
        except RuntimeError as exc:
            diagnostic = getattr(exc, "sanmar_checkout_diagnostic", None)
            detail = ""
            if isinstance(diagnostic, dict):
                screenshot = diagnostic.get("screenshot")
                text_snapshot = diagnostic.get("text_snapshot")
                artifacts = ", ".join(path for path in (screenshot, text_snapshot) if path)
                if artifacts:
                    detail = f" Diagnostic saved: {artifacts}."
            return done(_result(
                order_id,
                False,
                "sanmar_ups_unavailable",
                f"SanMar UPS shipping option could not be selected/read: {exc}.{detail}",
                order=order,
                warehouse=warehouse,
                warehouses=selected_warehouses,
                products=product_lines,
                warehouse_plan=warehouse_plan,
                shipping=shipping,
                sanmar_checkout_diagnostic=diagnostic if isinstance(diagnostic, dict) else None,
                manual_review_required=True,
                retryable=False,
            ))
        eta = eta_state.get("eta")
        eta_by_warehouse = eta_state.get("eta_by_warehouse")
        production_target = _shipping_bypasser_production_target_for_eta(eta)
        if eta > order["due_date"]:
            if order.get("shipping_class") == "free":
                # Free-ship orders may be moved out to receive stock; paid/rush orders may not.
                due_target = _next_business_day_on_or_after(eta)
                if due_target <= production_target:
                    due_target = _next_business_day_after(production_target)
                _change_crm_due_date(crm_driver, order_id, due_target)
                order["due_date"] = due_target
            else:
                return done(_result(order_id, False, "eta_after_due_date", f"SanMar ETA {eta.isoformat()} is after due date {order['due_date'].isoformat()}.", order=order, warehouse=warehouse, warehouses=selected_warehouses, eta=str(eta), eta_by_warehouse=eta_by_warehouse))
        if production_target >= order["due_date"]:
            return done(_result(
                order_id,
                False,
                "eta_after_due_date",
                f"SanMar ETA {eta.isoformat()} pushes production date to {production_target.isoformat()}, which is not before due date {order['due_date'].isoformat()}.",
                order=order,
                warehouse=warehouse,
                warehouses=selected_warehouses,
                eta=str(eta),
                eta_by_warehouse=eta_by_warehouse,
            ))
        if production_target < order["due_date"] and production_target > order["production_date"]:
            try:
                order["production_date"] = _change_crm_production_date(crm_driver, order_id, production_target)
            except RuntimeError as exc:
                if "production date did not persist" in str(exc).lower():
                    return done(_result(
                        order_id,
                        False,
                        "crm_production_date_not_persisted",
                        f"{exc} Skipped before submitting the SanMar order.",
                        order=order,
                        warehouse=warehouse,
                        warehouses=selected_warehouses,
                        products=product_lines,
                        warehouse_plan=warehouse_plan,
                        eta=str(eta) if eta else None,
                        eta_by_warehouse=eta_by_warehouse,
                        shipping=shipping,
                        manual_review_required=True,
                        retryable=False,
                    ))
                raise
        _click_sanmar_button(sanmar_driver, r"Proceed\s+To\s+Payment")
    _wait_for_text(sanmar_driver, r"Review\s+&\s+Submit|Review\s+and\s+Submit|Customer\s+PO", timeout=20)
    _publish_status(f"Reviewing SanMar order {order_id} before submit.", stage="reviewing_sanmar_order", order_id=order_id)
    submit_state = _fill_review_and_submit(sanmar_driver, order["po"], dry_run=dry_run)
    sanmar_confirmation = None
    if submit_state == "submitted":
        sanmar_confirmation = _capture_sanmar_confirmation(sanmar_driver, order_id, order["po"])
        if sanmar_confirmation.get("po_confirmed"):
            _remember_pending_shipping_bypass_submission(order_id, order["po"], sanmar_confirmation, vendor_name="Sanmar")
        if not sanmar_confirmation.get("po_confirmed"):
            return done(_result(
                order_id,
                False,
                "sanmar_submitted_po_not_confirmed",
                f"SanMar order was submitted, but Customer PO {order['po']} was not visible on the confirmation/order summary page. Manual review required.",
                order=order,
                warehouse=warehouse,
                products=product_lines,
                eta=str(eta) if eta else None,
                shipping=shipping,
                sanmar_submit_state=submit_state,
                sanmar_confirmation=sanmar_confirmation,
                manual_review_required=True,
                retryable=False,
            ))
    try:
        _publish_status(f"Recording CRM Manual Order for Shipping Bypasser order {order_id}.", stage="recording_crm_manual_order", order_id=order_id)
        record_state = _record_crm_manual_order(crm_driver, order_id, order["po"], dry_run=dry_run, stock_tab_index=stock_tab_index)
        if submit_state == "submitted" and not dry_run:
            _mark_pending_shipping_bypass_submission_recorded(order_id, order["po"], record_state)
    except Exception as exc:
        if submit_state == "submitted":
            return done(_result(
                order_id,
                False,
                "sanmar_submitted_crm_record_failed",
                f"SanMar order was submitted, but CRM Manual Order could not be recorded: {exc}",
                order=order,
                warehouse=warehouse,
                products=product_lines,
                eta=str(eta) if eta else None,
                shipping=shipping,
                sanmar_submit_state=submit_state,
                sanmar_confirmation=sanmar_confirmation,
                manual_review_required=True,
                retryable=False,
            ))
        raise
    production_note = _format_multi_warehouse_production_note(order, warehouse_plan) if multi_warehouse else ""
    production_note_state = None
    if production_note:
        if dry_run:
            production_note_state = "dry_run_production_note_ready"
        else:
            production_note_state = _append_crm_production_note(crm_driver, order_id, production_note)
    message = "Shipping Bypasser dry run reached SanMar review and CRM manual-order readiness." if dry_run else "SanMar stock was ordered and recorded in CRM Manual Order."
    if production_note:
        message = f"{message} Production note {'ready' if dry_run else 'saved'}."
    if stock_tab_label:
        message = f"{message} Stock tab: {stock_tab_label}."
    return done(_result(
        order_id,
        True,
        "shipping_bypass_ready" if dry_run else "shipping_bypass_ordered",
        message,
        manual_review_required=False,
        order=order,
        warehouse=warehouse,
        warehouses=selected_warehouses,
        products=product_lines,
        warehouse_plan=warehouse_plan,
        eta=str(eta) if eta else None,
        eta_by_warehouse=eta_by_warehouse,
        shipping=shipping,
        sanmar_submit_state=submit_state,
        sanmar_confirmation=sanmar_confirmation,
        crm_record_state=record_state,
        production_note=production_note or None,
        production_note_state=production_note_state,
    ))


def _run_order_with_drivers(crm_driver, sanmar_driver, order_id, dry_run=False):
    normalized_order_id = _normalize_target_order_id(order_id)
    if not normalized_order_id:
        raise RuntimeError("Order ID must be a 7-digit value or CRM order URL.")
    _publish_status(f"Opening CRM order {normalized_order_id} for Shipping Bypasser.", stage="opening_order", order_id=normalized_order_id)
    _open_target_order(crm_driver, normalized_order_id, shipping_filter=RUSH_FILTER, list_url_override=None)
    _publish_status(f"Checking Shipping Bypasser page for order {normalized_order_id}.", stage="checking_order_goods", order_id=normalized_order_id)
    _wait_for_order_goods_page_ready(crm_driver, normalized_order_id)
    _publish_status(f"Finding stock tabs for Shipping Bypasser order {normalized_order_id}.", stage="finding_stock_tabs", order_id=normalized_order_id)
    tabs = _find_stock_tabs(crm_driver)
    tab_number_hints = _visible_design_tab_number_hints(crm_driver)
    tab_count = len(tabs)
    if tab_number_hints and len(tabs) < len(tab_number_hints):
        return [
            _result(
                normalized_order_id,
                False,
                "stock_tab_detection_incomplete",
                (
                    f"Detected {len(tab_number_hints)} design preview tab label(s) in CRM text "
                    f"({', '.join(str(value) for value in tab_number_hints)}), but only mapped "
                    f"{len(tabs)} clickable stock tab(s). Manual review required before Shipping Bypasser orders stock."
                ),
                manual_review_required=True,
                retryable=False,
                stock_tab_count=max(1, len(tab_number_hints)),
                detected_stock_tabs=tabs,
                detected_design_tab_numbers=tab_number_hints,
            )
        ]
    if tab_count <= 1:
        tab_label = _stock_tab_summary_label((tabs[0] or {}).get("label")) if tabs else ""
        result = _process_open_order(
            crm_driver,
            sanmar_driver,
            normalized_order_id,
            dry_run=dry_run,
            stock_tab_index=1,
            stock_tab_count=max(1, tab_count),
            stock_tab_label=tab_label,
            stock_tab_context=tabs[0] if tabs else None,
        )
        if dry_run and result.get("success") and str(result.get("outcome") or "") == "shipping_bypass_ready":
            result["sanmar_cart_cleanup"] = _clear_sanmar_cart(sanmar_driver, order_id=normalized_order_id)
        return [result]

    results = []
    skipped_tabs = []
    for tab_index, tab_context in enumerate(tabs if isinstance(tabs, list) else []):
        tab_number = tab_index + 1
        tab_label = _stock_tab_summary_label((tab_context or {}).get("label"))
        try:
            if tab_index:
                _publish_status(
                    f"Reloading CRM order {normalized_order_id} for stock tab {tab_number}/{tab_count}.",
                    stage="reloading_order",
                    order_id=normalized_order_id,
                )
                _open_target_order(crm_driver, normalized_order_id, shipping_filter=RUSH_FILTER, list_url_override=None)
                _wait_for_order_goods_page_ready(crm_driver, normalized_order_id)
            tab = _activate_stock_tab(crm_driver, tab_index)
            tab_label = _stock_tab_summary_label((tab or {}).get("label") or tab_label)
            if tab is None:
                result = _result(
                    normalized_order_id,
                    False,
                    "stock_tab_not_found",
                    f"Stock tab {tab_number} of {tab_count} could not be activated.",
                    manual_review_required=True,
                    retryable=False,
                )
            else:
                order = _extract_order_data(crm_driver, normalized_order_id, tab_context=tab)
                po = order.get("po")
                skip_reason = ""
                if po and _crm_stock_order_yellow_visual_exists(crm_driver, po):
                    skip_reason = "crm_yellow_manual_order"
                elif po and _crm_manual_order_row_exists(crm_driver, po):
                    skip_reason = "crm_manual_order"
                elif po and _historical_shipping_bypass_po_exists(po):
                    skip_reason = "shipping_bypasser_history"
                elif order.get("active_panel_stock_ordered"):
                    skip_reason = "active_panel_stock_ordered"

                if skip_reason:
                    skipped_tabs.append({"tab_number": tab_number, "tab_label": tab_label, "po": po, "reason": skip_reason})
                    result = _result(
                        normalized_order_id,
                        True,
                        "already_stock_ordered",
                        f"Skipped stock tab {tab_number} of {tab_count} because it is already ordered or recorded.",
                        manual_review_required=False,
                        detected_stock_tabs=tabs,
                        skipped_stock_tabs=skipped_tabs,
                    )
                else:
                    print(f"Shipping Bypasser ordering stock tab {tab_number}/{tab_count}: {tab_label or 'untitled tab'}...")
                    _publish_status(
                        f"Processing Shipping Bypasser order {normalized_order_id} stock tab {tab_number}/{tab_count}.",
                        stage="processing_stock_tab",
                        order_id=normalized_order_id,
                    )
                    result = _process_open_order(
                        crm_driver,
                        sanmar_driver,
                        normalized_order_id,
                        dry_run=dry_run,
                        stock_tab_index=tab_number,
                        stock_tab_count=tab_count,
                        stock_tab_label=tab_label,
                        stock_tab_context=tab,
                    )
                    result["stock_tab_preflight_reason"] = "needs_shipping_bypass"
        except Exception as exc:
            safe_take_screenshot(crm_driver, "crm_shipping_bypass_tab_error")
            result = _result(
                normalized_order_id,
                False,
                "worker_exception",
                str(exc),
                manual_review_required=True,
                retryable=_is_retryable_exception(exc),
                error_type=type(exc).__name__,
            )
        result = _attach_stock_tab_context(result, tab_number, tab_count, tab_label)
        results.append(result)
        if dry_run and result.get("success") and str(result.get("outcome") or "") == "shipping_bypass_ready":
            result["sanmar_cart_cleanup"] = _clear_sanmar_cart(sanmar_driver, order_id=normalized_order_id)
        if result.get("stop_run"):
            break
        if not result.get("success"):
            cleanup_report = [result]
            cleanup_ok = _cleanup_after_failed_order(sanmar_driver, normalized_order_id, cleanup_report)
            if len(cleanup_report) > 1:
                results.extend(cleanup_report[1:])
            if not cleanup_ok:
                break
    if results and len(skipped_tabs) == len(tabs) and all(str(item.get("outcome") or "") == "already_stock_ordered" for item in results):
        skipped_labels = ", ".join(
            f"{item.get('po') or item.get('tab_label') or item.get('tab_number')} ({item.get('reason')})"
            for item in skipped_tabs[:8]
        )
        suffix = f": {skipped_labels}" if skipped_labels else "."
        return [
            _result(
                normalized_order_id,
                True,
                "already_stock_ordered",
                f"Skipped because all detected stock tabs are already ordered or recorded{suffix}",
                manual_review_required=False,
                detected_stock_tabs=tabs,
                skipped_stock_tabs=skipped_tabs,
            )
        ]
    return results


STOCK_ORDER_SUCCESS_OUTCOMES = {"shipping_bypass_ordered"}


def _stock_tab_descriptor(item):
    if not isinstance(item, dict):
        return ""
    tab_index = item.get("stock_tab_index")
    tab_count = item.get("stock_tab_count")
    label = _stock_tab_summary_label(item.get("stock_tab_label"))
    try:
        tab_index = int(tab_index)
    except Exception:
        tab_index = None
    try:
        tab_count = int(tab_count)
    except Exception:
        tab_count = None
    if tab_index and tab_count and tab_count > 1:
        base = f"tab {tab_index} of {tab_count}"
    elif tab_index:
        base = f"tab {tab_index}"
    else:
        base = "tab"
    return f"{base} ({label})" if label else base


def _report_item_customer_po(item):
    if not isinstance(item, dict):
        return ""
    confirmation = item.get("sanmar_confirmation") if isinstance(item.get("sanmar_confirmation"), dict) else {}
    po = str(confirmation.get("po") or "").strip()
    if po:
        return po
    order = item.get("order") if isinstance(item.get("order"), dict) else {}
    return str(order.get("po") or item.get("po") or "").strip()


def _report_item_sanmar_confirmation_url(item):
    if not isinstance(item, dict):
        return ""
    confirmation = item.get("sanmar_confirmation") if isinstance(item.get("sanmar_confirmation"), dict) else {}
    return str(confirmation.get("url") or "").strip()


def _report_item_ordered_stock_success_detail(item):
    if not isinstance(item, dict) or not item.get("success"):
        return ""
    if str(item.get("outcome") or "") not in STOCK_ORDER_SUCCESS_OUTCOMES:
        return ""
    descriptor = _stock_tab_descriptor(item)
    po = _report_item_customer_po(item)
    url = _report_item_sanmar_confirmation_url(item)
    if not descriptor or not po or not url:
        return ""
    return f"{descriptor}, customer PO {po}, SanMar confirmation {url}"


def _report_item_ordered_stock_successfully(item):
    return bool(_report_item_ordered_stock_success_detail(item))


def _ordered_stock_success_details(items):
    details = []
    seen = set()
    for item in items if isinstance(items, list) else []:
        detail = _report_item_ordered_stock_success_detail(item)
        if not detail or detail in seen:
            continue
        seen.add(detail)
        details.append(detail)
    return details


def _stock_order_report_rows(report_items):
    rows = [item for item in (report_items if isinstance(report_items, list) else []) if isinstance(item, dict)]
    non_cleanup_rows = [item for item in rows if str(item.get("outcome") or "") != "sanmar_cart_cleanup_failed"]
    return non_cleanup_rows or rows


def _summary_message(report_items, refresh_passes=1, order_count=0):
    rows = _stock_order_report_rows(report_items)
    total = len(rows)
    order_groups = {}
    for item in rows:
        order_id = str(item.get("order_id") or "").strip()
        if order_id:
            order_groups.setdefault(order_id, []).append(item)
    successful_orders = sum(1 for items in order_groups.values() if items and all(bool(item.get("success")) for item in items))
    partial_orders = []
    for order_id, items in order_groups.items():
        success_details = _ordered_stock_success_details(items)
        failed_items = [item for item in items if not bool(item.get("success"))]
        if failed_items and success_details:
            details = (
                success_details
                if any(str(item.get("outcome") or "") == "worker_exception" for item in failed_items)
                else []
            )
            partial_orders.append((order_id, details))
    failed_order_ids = [
        order_id
        for order_id, items in order_groups.items()
        if any(not bool(item.get("success")) for item in items) and not _ordered_stock_success_details(items)
    ]
    if total == 0:
        return f"No {ALLOWED_SHIPPING_LIST_ROW_DESCRIPTION} Shipping Bypasser orders were detected in the CRM list."
    parts = [
        f"Shipping Bypasser processed {max(1, int(order_count or len(order_groups) or 0))} order(s) and {total} stock tab(s) across {max(1, int(refresh_passes or 1))} CRM list refresh pass(es).",
        f"{successful_orders} order(s) succeeded.",
    ]
    if partial_orders:
        detail_text = "; ".join(
            f"{order_id} {'; '.join(details)}"
            for order_id, details in partial_orders
            if details
        )
        suffix = f": {detail_text}" if detail_text else ""
        parts.append(f"{len(partial_orders)} order(s) partially succeeded{suffix}.")
    if failed_order_ids:
        displayed_ids = ", ".join(failed_order_ids[:10])
        suffix = "..." if len(failed_order_ids) > 10 else ""
        parts.append(f"{len(failed_order_ids)} order(s) need attention: {displayed_ids}{suffix}.")
    return " ".join(parts)


def _report_orders_succeeded_or_partially_succeeded(report_items):
    rows = _stock_order_report_rows(report_items)
    if not rows:
        return True
    order_groups = {}
    anonymous_rows = []
    for item in rows:
        order_id = str(item.get("order_id") or "").strip()
        if order_id:
            order_groups.setdefault(order_id, []).append(item)
        else:
            anonymous_rows.append(item)
    if anonymous_rows and not all(bool(item.get("success")) for item in anonymous_rows):
        return False
    for items in order_groups.values():
        if all(bool(item.get("success")) for item in items):
            continue
        if any(_report_item_ordered_stock_successfully(item) for item in items):
            continue
        return False
    return True


def _report_has_fully_failed_order(report_items):
    return not _report_orders_succeeded_or_partially_succeeded(report_items)


def _run_single_with_mode(headless_mode, order_id, dry_run=False, profile_path=None, sanmar_visible=False):
    started_at = time.monotonic()
    normalized_order_id = _normalize_target_order_id(order_id)
    if not normalized_order_id:
        raise RuntimeError("Order ID must be a 7-digit value or CRM order URL.")
    resolved_profile_path = os.path.abspath(profile_path or PROFILE_PATH)
    crm_driver = None
    sanmar_driver = None
    report_items = []
    try:
        _publish_status(
            f"Loading CRM session for Shipping Bypasser order {normalized_order_id}.",
            stage="loading_crm",
            current=0,
            total=1,
            order_id=normalized_order_id,
        )
        crm_driver = _build_crm_session_driver(
            resolved_profile_path,
            headless_mode=headless_mode,
            profile_label=f"CRM shipping bypasser single {normalized_order_id}",
        )
        _publish_status(
            f"Loading SanMar session for Shipping Bypasser order {normalized_order_id}.",
            stage="loading_sanmar",
            current=0,
            total=1,
            order_id=normalized_order_id,
        )
        sanmar_driver = _build_sanmar_driver(visible=sanmar_visible or dry_run)
        report_items = _run_order_with_drivers(crm_driver, sanmar_driver, normalized_order_id, dry_run=dry_run)
        if not any(isinstance(item, dict) and item.get("stop_run") for item in report_items):
            _cleanup_after_failed_order(sanmar_driver, normalized_order_id, report_items)
        _publish_status(
            f"Finished Shipping Bypasser order {normalized_order_id} (1/1 done).",
            stage="finished_order",
            current=1,
            total=1,
            order_id=normalized_order_id,
        )
    finally:
        safe_driver_quit(crm_driver, profile_path=resolved_profile_path)
        safe_driver_quit(sanmar_driver, profile_path=os.path.abspath(SANMAR_PROFILE_PATH))
    success = _report_orders_succeeded_or_partially_succeeded(report_items)
    return {
        "action": "shipping_bypass_single",
        "success": success,
        "message": _summary_message(report_items, refresh_passes=1, order_count=1),
        "target_order_id": normalized_order_id,
        "order_count": 1,
        "order_ids": [normalized_order_id],
        "report": report_items,
        "dry_run": bool(dry_run),
        "headless": bool(headless_mode),
        "shipping_filter": RUSH_FILTER,
        "batch_size": 1,
        "parallel_workers": 1,
        "refresh_passes": 1,
        "duration_seconds": _elapsed_seconds(started_at),
        "manual_review_required": _report_has_fully_failed_order(report_items),
        "resolution": "single",
    }


def _run_single(order_id, dry_run=False, profile_path=None, visible=False):
    started_at = time.monotonic()
    normalized_order_id = _normalize_target_order_id(order_id)
    if not normalized_order_id:
        return {
            "action": "shipping_bypass_single",
            "success": False,
            "message": "Order ID must be a 7-digit value or CRM order URL.",
            "target_order_id": None,
            "order_count": 0,
            "order_ids": [],
            "report": [],
            "dry_run": bool(dry_run),
            "shipping_filter": RUSH_FILTER,
            "duration_seconds": _elapsed_seconds(started_at),
            "manual_review_required": True,
            "resolution": "invalid_order_id",
        }
    modes = [False] if visible else _crm_attempt_modes()
    last_payload = None
    for index, headless_mode in enumerate(modes, start=1):
        try:
            payload = _run_single_with_mode(
                headless_mode,
                normalized_order_id,
                dry_run=dry_run,
                profile_path=profile_path,
                sanmar_visible=visible,
            )
            payload["headless"] = bool(headless_mode)
            return payload
        except Exception as exc:
            last_payload = {
                "action": "shipping_bypass_single",
                "success": False,
                "message": str(exc),
                "target_order_id": normalized_order_id,
                "order_count": 1,
                "order_ids": [normalized_order_id],
                "report": [_result(normalized_order_id, False, "worker_exception", str(exc), retryable=_is_retryable_exception(exc), error_type=type(exc).__name__)],
                "dry_run": bool(dry_run),
                "headless": bool(headless_mode),
                "shipping_filter": RUSH_FILTER,
                "duration_seconds": _elapsed_seconds(started_at),
                "manual_review_required": True,
                "resolution": "single",
            }
            if not headless_mode or index == len(modes) or not _is_retryable_exception(exc):
                break
            print("Headless Shipping Bypasser single run failed with a retryable error; retrying with visible Chrome...")
            time.sleep(1)
    return last_payload


def _run_batch_with_mode(headless_mode, dry_run=False, batch_size=None, profile_path=None, list_url=None):
    batch_started_at = time.monotonic()
    target_url = _validate_runtime_config(list_url)
    requested_batch_size = _normalize_requested_batch_size(batch_size)
    resolved_profile_path = os.path.abspath(profile_path or PROFILE_PATH)

    def _launch_crm_driver():
        return _build_crm_session_driver(
            resolved_profile_path,
            headless_mode=headless_mode,
            profile_label="CRM shipping bypasser",
        )

    def _launch_sanmar_driver():
        return _build_sanmar_driver(visible=dry_run)

    _publish_status("Loading CRM session for Shipping Bypasser batch.", stage="loading_crm")
    crm_driver = _launch_crm_driver()
    sanmar_driver = None
    report_items = []
    attempted_order_ids = []
    historical_order_id_set = _load_historical_shipping_bypass_order_ids()
    attempted_order_id_set = set(historical_order_id_set)
    refresh_passes = 0
    completed_order_count = 0
    total_scanned_count = 0
    stop_batch = False

    def _relaunch_crm_driver(reason):
        nonlocal crm_driver
        print(reason)
        safe_driver_quit(crm_driver, profile_path=resolved_profile_path)
        time.sleep(1)
        crm_driver = _launch_crm_driver()

    def _relaunch_sanmar_driver(reason):
        nonlocal sanmar_driver
        print(reason)
        safe_driver_quit(sanmar_driver, profile_path=os.path.abspath(SANMAR_PROFILE_PATH))
        time.sleep(1)
        sanmar_driver = _launch_sanmar_driver()

    try:
        _publish_status("Loading SanMar session for Shipping Bypasser batch.", stage="loading_sanmar")
        sanmar_driver = _launch_sanmar_driver()
        while not stop_batch and not _batch_limit_reached(len(attempted_order_ids), requested_batch_size):
            refresh_passes += 1
            remaining = _batch_collection_limit(
                requested_batch_size,
                len(attempted_order_ids),
                worker_limit=CONTINUOUS_ORDER_FETCH_LIMIT,
            )
            _publish_status(
                f"Loading CRM Shipping Bypasser list pass {refresh_passes} to scan for eligible orders.",
                stage="loading_crm_list",
                current=completed_order_count if total_scanned_count else None,
                total=total_scanned_count or None,
            )
            list_scan_attempt = 0
            while True:
                try:
                    order_ids = _collect_batch_order_ids_with_driver(
                        crm_driver,
                        RUSH_FILTER,
                        remaining,
                        list_url_override=target_url,
                        exclude_order_ids=attempted_order_id_set,
                    )
                    break
                except Exception as exc:
                    list_scan_attempt += 1
                    if list_scan_attempt <= 1 and _is_retryable_exception(exc):
                        _relaunch_crm_driver(
                            "CRM Shipping Bypasser list scan lost its browser session; relaunching CRM Chrome and retrying the scan..."
                        )
                        continue
                    raise
            if not order_ids:
                _publish_status(
                    "No more eligible Shipping Bypasser orders were found in the CRM list.",
                    stage="scan_complete",
                    current=completed_order_count if total_scanned_count else None,
                    total=total_scanned_count or None,
                )
                break
            total_scanned_count += len(order_ids)
            _publish_status(
                f"Scanned {len(order_ids)} eligible Shipping Bypasser order(s) on pass {refresh_passes}; processing orders.",
                stage="processing_orders",
                current=completed_order_count,
                total=total_scanned_count,
            )
            for order_id in order_ids:
                if _batch_limit_reached(len(attempted_order_ids), requested_batch_size):
                    break
                attempted_order_id_set.add(order_id)
                attempted_order_ids.append(order_id)
                print(f"Processing Shipping Bypasser order {len(attempted_order_ids)}: {order_id}...")
                _publish_status(
                    f"Processing Shipping Bypasser order {order_id} ({completed_order_count}/{total_scanned_count} done).",
                    stage="processing_order",
                    current=completed_order_count,
                    total=total_scanned_count,
                    order_id=order_id,
                )
                order_started_at = time.monotonic()
                try:
                    order_report = _run_order_with_drivers(crm_driver, sanmar_driver, order_id, dry_run=dry_run)
                    order_duration = _elapsed_seconds(order_started_at)
                    for item in order_report:
                        if isinstance(item, dict):
                            item["duration_seconds"] = order_duration
                            item["session_duration_seconds"] = order_duration
                    stop_batch = _should_stop_bypasser_batch(order_report)
                    if not stop_batch:
                        _cleanup_after_failed_order(sanmar_driver, order_id, order_report)
                    report_items.extend(order_report)
                except Exception as exc:
                    safe_take_screenshot(crm_driver, "crm_shipping_bypass_error")
                    order_duration = _elapsed_seconds(order_started_at)
                    retryable = _is_retryable_exception(exc)
                    failure = _result(order_id, False, "worker_exception", str(exc), retryable=retryable, error_type=type(exc).__name__, duration_seconds=order_duration)
                    failure_report = [failure]
                    if "cart already had items" in str(exc).lower():
                        stop_batch = True
                    else:
                        _cleanup_after_failed_order(sanmar_driver, order_id, failure_report)
                    report_items.extend(failure_report)
                    if retryable:
                        try:
                            _relaunch_crm_driver(
                                "CRM Shipping Bypasser order processing lost its CRM browser session; relaunching before the next order..."
                            )
                            _relaunch_sanmar_driver(
                                "CRM Shipping Bypasser order processing hit a retryable browser error; relaunching SanMar before the next order..."
                            )
                        except Exception as relaunch_exc:
                            report_items.append(
                                _result(
                                    order_id,
                                    False,
                                    "browser_relaunch_failed",
                                    f"Browser session relaunch failed after retryable Shipping Bypasser error: {relaunch_exc}",
                                    retryable=_is_retryable_exception(relaunch_exc),
                                    error_type=type(relaunch_exc).__name__,
                                    duration_seconds=order_duration,
                                    stop_run=True,
                                )
                            )
                            stop_batch = True
                completed_order_count += 1
                _publish_status(
                    f"Finished Shipping Bypasser order {order_id} ({completed_order_count}/{total_scanned_count} done).",
                    stage="finished_order",
                    current=completed_order_count,
                    total=total_scanned_count,
                    order_id=order_id,
                )
                if stop_batch:
                    break
            if _batch_limit_reached(len(attempted_order_ids), requested_batch_size):
                break
            if stop_batch:
                break
            print("Finished Shipping Bypasser list pass; reopening the list to look for more eligible orders...")
    finally:
        safe_driver_quit(crm_driver, profile_path=resolved_profile_path)
        safe_driver_quit(sanmar_driver, profile_path=os.path.abspath(SANMAR_PROFILE_PATH))
    success = _report_orders_succeeded_or_partially_succeeded(report_items)
    skipped_historical_count = len(historical_order_id_set)
    message = _summary_message(report_items, refresh_passes=refresh_passes, order_count=len(attempted_order_ids))
    if skipped_historical_count:
        message = f"{message} Skipped {skipped_historical_count} previously logged Shipping Bypasser order(s); clear Stock Tools history to make them eligible again."
    return {
        "action": "shipping_bypass_batch",
        "success": success,
        "message": message,
        "order_count": len(attempted_order_ids),
        "order_ids": attempted_order_ids,
        "report": report_items,
        "dry_run": bool(dry_run),
        "headless": bool(headless_mode),
        "shipping_filter": RUSH_FILTER,
        "list_url": target_url,
        "batch_size": requested_batch_size,
        "parallel_workers": 1,
        "refresh_passes": refresh_passes,
        "duration_seconds": _elapsed_seconds(batch_started_at),
        "manual_review_required": _report_has_fully_failed_order(report_items),
        "resolution": "batch" if attempted_order_ids else "no_orders",
        "skipped_historical_order_count": skipped_historical_count,
    }


def _run_batch(dry_run=False, batch_size=None, profile_path=None, list_url=None, visible=False):
    batch_started_at = time.monotonic()
    modes = [False] if visible else _crm_attempt_modes()
    last_payload = None
    for index, headless_mode in enumerate(modes, start=1):
        try:
            payload = _run_batch_with_mode(
                headless_mode,
                dry_run=dry_run,
                batch_size=batch_size,
                profile_path=profile_path,
                list_url=list_url,
            )
            payload["headless"] = bool(headless_mode)
            return payload
        except Exception as exc:
            last_payload = {
                "action": "shipping_bypass_batch",
                "success": False,
                "message": str(exc),
                "order_count": 0,
                "order_ids": [],
                "report": [],
                "dry_run": bool(dry_run),
                "headless": bool(headless_mode),
                "shipping_filter": RUSH_FILTER,
                "manual_review_required": True,
                "retryable": _is_retryable_exception(exc),
                "error_type": type(exc).__name__,
                "duration_seconds": _elapsed_seconds(batch_started_at),
            }
            if not headless_mode or index == len(modes) or not _is_retryable_exception(exc):
                break
            print("Headless Shipping Bypasser failed with a retryable error; retrying with visible Chrome...")
            time.sleep(1)
    return last_payload


def run(action="shipping_bypass_batch", dry_run=False, batch_size=None, profile_path=None, result_file=None, list_url=None, visible=False, order_id=None):
    if action not in {"shipping_bypass_batch", "shipping_bypass_single"}:
        raise RuntimeError("Unsupported CRM Shipping Bypasser action.")
    if action == "shipping_bypass_single" or order_id:
        payload = _run_single(order_id, dry_run=dry_run, profile_path=profile_path, visible=visible)
    else:
        payload = _run_batch(dry_run=dry_run, batch_size=batch_size, profile_path=profile_path, list_url=list_url, visible=visible)
    write_result_payload(
        AUTOMATION_NAME,
        "crm_shipping_bypasser.py",
        bool(payload.get("success")),
        payload.get("message") or "CRM Shipping Bypasser completed.",
        extra_fields=payload,
        result_file=result_file,
    )
    return 0 if payload.get("success") else 1


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Manually order SanMar stock for Shipping Cost Bypass CRM orders.")
    parser.add_argument("--action", choices=["shipping_bypass_batch", "shipping_bypass_single"], default="shipping_bypass_batch")
    parser.add_argument("--order-id", required=False, help="Optional single 7-digit CRM order ID or CRM order URL.")
    parser.add_argument("--batch-size", type=int, default=None, help="Process up to this many orders; 0/unset means run until no eligible orders remain.")
    parser.add_argument("--profile-path", required=False, help="Optional CRM Chrome user-data-dir override.")
    parser.add_argument("--result-file", required=False, help="Optional path for the JSON result payload.")
    parser.add_argument("--list-url", required=False, help="Optional Shipping Bypasser CRM report URL override.")
    parser.add_argument("--visible", action="store_true", help="Run Chrome visibly instead of headless for testing.")
    parser.add_argument("--dry-run", action="store_true", help="Stop before SanMar submit and CRM Manual Order save.")
    return parser.parse_args(argv)


if __name__ == "__main__":
    options = parse_args(sys.argv[1:])
    sys.exit(
        run(
            action=options.action,
            dry_run=bool(options.dry_run),
            batch_size=options.batch_size,
            profile_path=options.profile_path,
            result_file=options.result_file,
            list_url=options.list_url,
            visible=bool(options.visible),
            order_id=options.order_id,
        )
    )
