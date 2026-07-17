
"""
CRM single-order address validation automation worker.
Usage:
    python crm_validate_address.py --action validate_order
    python crm_validate_address.py --action validate_order --dry-run
    python crm_validate_address.py --action validate_order --order-id 4357285
"""

import argparse
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from difflib import SequenceMatcher

from _bootstrap import ensure_project_root_on_path

ensure_project_root_on_path()

from selenium.common.exceptions import (
    ElementClickInterceptedException,
    ElementNotInteractableException,
    StaleElementReferenceException,
    TimeoutException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import Select

from automation_runtime import (
    SCRIPT_DIR,
    build_chrome_driver,
    configure_console_utf8,
    kill_stale_chrome,
    safe_driver_quit,
    safe_get_with_partial_load,
    safe_take_screenshot,
    write_result_payload,
)
from config import (
    CRM_813_VALIDATOR_URL,
    CRM_ACTION_TIMEOUT,
    CRM_ALLOW_VISIBLE_FALLBACK,
    CRM_HEADLESS,
    CRM_SHIPPING_813_URL,
    CRM_SHIPPING_ALL_URL,
    CRM_SHIPPING_FILTER_DEFAULT,
    CRM_SHIPPING_FREE_URL,
    CRM_SHIPPING_HIGH_VALUE_URL,
    CRM_SHIPPING_RUSH_URL,
    CRM_SHIPPING_URL,
    CRM_LOGIN_URL,
    CRM_PAGE_LOAD_TIMEOUT,
    CRM_PROFILE_DIR,
)
from runtime_paths import GENERATED_PROFILES_DIR
from credential_store import CRM_CREDENTIAL_TARGET, read_windows_credential

configure_console_utf8()

AUTOMATION_NAME = "crm.address_validator"
PROFILE_PATH = os.path.join(SCRIPT_DIR, CRM_PROFILE_DIR)
CONTINUOUS_BATCH_FETCH_LIMIT = 25
PROFILE_CLONE_IGNORE_NAMES = (
    "ActorSafetyLists",
    "AmountExtractionHeuristicRegexes",
    "BrowserMetrics-spare.pma",
    "CaptchaProviders",
    "component_crx_cache",
    "SingletonLock",
    "SingletonCookie",
    "SingletonSocket",
    "DevToolsActivePort",
    "Crashpad",
    "Crowd Deny",
    "extensions_crx_cache",
    "FileTypePolicies",
    "First Run",
    "FirstPartySetsPreloaded",
    "first_party_sets.db",
    "first_party_sets.db-journal",
    "GraphiteDawnCache",
    "hyphen-data",
    "Last Browser",
    "Last Version",
    "MEIPreload",
    "OnDeviceHeadSuggestModel",
    "OptimizationHints",
    "optimization_guide_model_store",
    "OriginTrials",
    "PKIMetadata",
    "PrivacySandboxAttestationsPreloaded",
    "RecoveryImproved",
    "SafetyTips",
    "segmentation_platform",
    "Service Worker",
    "Sessions",
    "Session Storage",
    "Shared Dictionary",
    "BrowserMetrics",
    "Cache",
    "Code Cache",
    "GPUCache",
    "ShaderCache",
    "GrShaderCache",
    "DawnGraphiteCache",
    "SharedStorage",
    "SSLErrorAssistant",
    "Subresource Filter",
    "TrustTokenKeyCommitments",
    "Variations",
    "WasmTtsEngine",
    "WidevineCdm",
    "ZxcvbnData",
)
PROFILE_CLONE_IGNORE_LOOKUPS = {name.lower() for name in PROFILE_CLONE_IGNORE_NAMES}
WORKER_PROFILE_POOL_DIR = os.path.join(GENERATED_PROFILES_DIR, "chrome_profile_crm_worker_pool")
WORKER_PROFILE_LOCKS = {}
WORKER_PROFILE_LOCKS_LOCK = threading.Lock()
WORKER_PROFILE_LOCK_CLEANUP_NAMES = (
    "SingletonLock",
    "SingletonCookie",
    "SingletonSocket",
    "DevToolsActivePort",
)
WORKER_PROFILE_CACHE_DIR_NAMES = (
    "Cache",
    "Code Cache",
    "GPUCache",
    "ShaderCache",
    "GrShaderCache",
)


def _elapsed_seconds(started_at):
    return round(max(0.0, time.monotonic() - started_at), 1)


def _record_stage_timing(stage_timings, stage, started_at, **extra):
    if not isinstance(stage_timings, list):
        return
    item = {
        "stage": str(stage or ""),
        "duration_seconds": _elapsed_seconds(started_at),
    }
    for key, value in extra.items():
        if value is not None:
            item[str(key)] = value
    stage_timings.append(item)


def _attach_duration(payload, duration_seconds):
    if not isinstance(payload, dict):
        return payload
    duration = round(max(0.0, float(duration_seconds or 0.0)), 1)
    payload["duration_seconds"] = duration
    payload["session_duration_seconds"] = duration
    report = payload.get("report")
    if isinstance(report, list):
        for item in report:
            if isinstance(item, dict):
                item["duration_seconds"] = duration
                item["session_duration_seconds"] = duration
    return payload

UNIT_LABEL_PATTERN = r"(?:APT|APARTMENT|UNIT|STE|SUITE|FLOOR|FL|ROOM|RM|BLDG|BUILDING|LOT|ATTN|DEPT|DEPARTMENT|MAILSTOP|MAIL STOP|PMB|TRLR|TRAILER|SPC|SPACE|BOX|CARE OF|C/O)"
UNIT_TRAILER_PATTERNS = (
    re.compile(
        rf"^(?P<street>.*?)(?:,?\s+)(?P<unit>{UNIT_LABEL_PATTERN}\.?\s*#?\s*[A-Z0-9][A-Z0-9-]*(?:\s+[A-Z0-9-]+)*)$"
    ),
    re.compile(
        r"^(?P<street>.+?)(?:,?\s*)(?P<unit>#\s*[A-Z0-9][A-Z0-9-]*(?:\s+[A-Z0-9-]+)*)$"
    ),
)
EMAIL_ADDRESS_PATTERN = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
LEADING_UNIT_PREFIX_PATTERNS = (
    re.compile(
        rf"^(?P<unit>#\s*[A-Z0-9][A-Z0-9-]*|{UNIT_LABEL_PATTERN}\.?\s*#?\s*[A-Z0-9][A-Z0-9-]*)\s+(?P<street>.+)$"
    ),
)
RETRYABLE_EXCEPTION_SIGNALS = (
    "session not created",
    "devtoolsactiveport",
    "chrome failed to start",
    "disconnected: not connected to devtools",
    "timed out receiving message from renderer",
    "timeout",
    "invalid session id",
    "unable to discover open pages",
)
CSS_RGB_PATTERN = re.compile(r"rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})")
ORDINAL_STREET_TOKEN_PATTERN = re.compile(r"^(\d+)(ST|ND|RD|TH)$")
LOGIN_USERNAME_SELECTORS = [
    (By.NAME, "email"),
    (By.NAME, "username"),
    (By.NAME, "login"),
    (By.CSS_SELECTOR, "input[type='email']"),
    (By.CSS_SELECTOR, "input[name='email']"),
    (By.CSS_SELECTOR, "input[name='username']"),
    (By.CSS_SELECTOR, "input[id='username']"),
    (By.CSS_SELECTOR, "input[name='login']"),
    (By.CSS_SELECTOR, "input[id='login']"),
]
LOGIN_PASSWORD_SELECTORS = [
    (By.NAME, "password"),
    (By.CSS_SELECTOR, "input[type='password']"),
    (By.CSS_SELECTOR, "input[name='password']"),
]
LOGIN_BUTTON_SELECTORS = [
    (By.XPATH, "//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'login') or contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'sign in')]"),
    (By.XPATH, "//input[@type='submit' and (contains(translate(@value, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'login') or contains(translate(@value, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'sign in'))]"),
]
EDIT_BUTTON_SELECTORS = [
    (By.XPATH, ".//button[translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')='edit']"),
    (By.XPATH, ".//a[translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')='edit']"),
]
SAVE_VERIFY_BUTTON_SELECTORS = [
    (By.XPATH, ".//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'save & verify address')]"),
    (By.XPATH, ".//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'save and verify address')]"),
]
FINAL_SAVE_BUTTON_SELECTORS = [
    (By.XPATH, ".//button[translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')='save']"),
    (By.XPATH, ".//input[@type='submit' and translate(@value, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')='save']"),
]
GLOBAL_FINAL_SAVE_BUTTON_SELECTORS = [
    (By.XPATH, "//button[translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')='save']"),
    (By.XPATH, "//input[@type='submit' and translate(@value, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')='save']"),
]
RECIPIENT_INPUT_SELECTORS = [
    (By.NAME, 'shipTo'),
    (By.CSS_SELECTOR, "input[ng-model='address.shipTo']"),
]
ADDRESS_LINE_INPUT_SELECTORS = [
    (By.NAME, 'address1'),
    (By.CSS_SELECTOR, "input[ng-model='address.address1']"),
]
ADDRESS_CONT_INPUT_SELECTORS = [
    (By.NAME, 'address2'),
    (By.CSS_SELECTOR, "input[ng-model='address.address2']"),
]
CITY_INPUT_SELECTORS = [
    (By.NAME, 'city'),
    (By.CSS_SELECTOR, "input[ng-model='address.city']"),
]
STATE_INPUT_SELECTORS = [
    (By.CSS_SELECTOR, "select[ng-model='address.stateId']"),
    (By.CSS_SELECTOR, "input[ng-model='address.stateName']"),
]
ZIP_INPUT_SELECTORS = [
    (By.NAME, 'zip'),
    (By.CSS_SELECTOR, "input[ng-model='address.zip']"),
]
GENERIC_CLOSE_BUTTON_SELECTORS = [
    (By.XPATH, ".//button[normalize-space()='close' or normalize-space()='Close']"),
    (By.XPATH, ".//button[normalize-space()='cancel' or normalize-space()='Cancel']"),
]
BUSINESS_NO_BUTTON_SELECTORS = [
    (By.XPATH, ".//button[normalize-space()='no' or normalize-space()='No']"),
]
FOLLOWUP_NO_PROMPT_KEYWORDS = (
    "is this a business address",
    "does this address have a unit",
    "does this have a unit",
    "apartment",
    "suite",
    "unit",
    "address line 2",
    "address line two",
    "secondary address",
)
EMBEDDED_PO_BOX_TRAILER_PATTERN = re.compile(
    r"^(?P<street>.+?)(?:\s*[,;]\s*|\s+)(?P<po_box>(?:P\.?\s*O\.?\s*BOX|POST\s+OFFICE\s+BOX)\.?\s*#?\s*[A-Z0-9][A-Z0-9-]*(?:\s+[A-Z0-9-]+)*)$",
    re.IGNORECASE,
)
ORDER_TOTALS_SHIPPING_VALUE_PATTERN = re.compile(
    r"\bShipping:\s*(?:\S+\s+){0,2}?(?P<value>Free|\$?\s*[0-9][0-9,]*(?:\.\d{2})?)\b",
    re.IGNORECASE,
)
ORDER_ID_PATTERN = re.compile(r"\b\d{7}\b")
ADDRESS_VALID_TEXT = "Address is valid"
NO_CANDIDATES_TEXT = "No Address Candidates found"
INVALID_FIELD_TEXT = "Please tell a manager"
MILITARY_STATE_CODES = {"AE", "AP", "AA", "ARMED FORCES EUROPE", "ARMED FORCES PACIFIC", "ARMED FORCES AMERICAS"}
ALLOWED_SHIPPING_LIST_ROW_LABELS = ("tan", "purple", "lime_green")
ALLOWED_SHIPPING_LIST_ROW_DESCRIPTION = "tan, natural, purple, or lime green"
ALLOWED_813_ORDER_GOODS_ROW_LABELS = ("bright_red", "purple")
ALLOWED_813_ORDER_GOODS_ROW_DESCRIPTION = "bright red or purple"

_STATE_ROWS = """
AL:ALABAMA
AK:ALASKA
AZ:ARIZONA
AR:ARKANSAS
CA:CALIFORNIA
CO:COLORADO
CT:CONNECTICUT
DE:DELAWARE
FL:FLORIDA
GA:GEORGIA
HI:HAWAII
ID:IDAHO
IL:ILLINOIS
IN:INDIANA
IA:IOWA
KS:KANSAS
KY:KENTUCKY
LA:LOUISIANA
ME:MAINE
MD:MARYLAND
MA:MASSACHUSETTS
MI:MICHIGAN
MN:MINNESOTA
MS:MISSISSIPPI
MO:MISSOURI
MT:MONTANA
NE:NEBRASKA
NV:NEVADA
NH:NEW HAMPSHIRE
NJ:NEW JERSEY
NM:NEW MEXICO
NY:NEW YORK
NC:NORTH CAROLINA
ND:NORTH DAKOTA
OH:OHIO
OK:OKLAHOMA
OR:OREGON
PA:PENNSYLVANIA
RI:RHODE ISLAND
SC:SOUTH CAROLINA
SD:SOUTH DAKOTA
TN:TENNESSEE
TX:TEXAS
UT:UTAH
VT:VERMONT
VA:VIRGINIA
WA:WASHINGTON
WV:WEST VIRGINIA
WI:WISCONSIN
WY:WYOMING
DC:DISTRICT OF COLUMBIA
AB:ALBERTA
BC:BRITISH COLUMBIA
MB:MANITOBA
NB:NEW BRUNSWICK
NL:NEWFOUNDLAND AND LABRADOR
NS:NOVA SCOTIA
NT:NORTHWEST TERRITORIES
NU:NUNAVUT
ON:ONTARIO
PE:PRINCE EDWARD ISLAND
QC:QUEBEC
SK:SASKATCHEWAN
YT:YUKON
AE:ARMED FORCES EUROPE
AP:ARMED FORCES PACIFIC
AA:ARMED FORCES AMERICAS
""".strip().splitlines()
STATE_ALIASES = {}
STATE_CODES = set()
for row in _STATE_ROWS:
    code, full = row.split(":", 1)
    STATE_CODES.add(code)
    STATE_ALIASES[code] = full
    STATE_ALIASES[full] = full

TEXT_TOKEN_MAP = {
    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
    "NORTHEAST": "NE", "NORTHWEST": "NW", "SOUTHEAST": "SE", "SOUTHWEST": "SW",
    "STREET": "ST", "AVENUE": "AVE", "ROAD": "RD", "DRIVE": "DR",
    "COURT": "CT", "LANE": "LN", "BOULEVARD": "BLVD", "TERRACE": "TER",
    "PLACE": "PL", "PARKWAY": "PKWY", "HIGHWAY": "HWY", "ROUTE": "RT",
    "RTE": "RT", "MOUNT": "MT", "FORT": "FT", "SAINT": "ST",
    "AV": "AVE", "WY": "WAY", "WAY": "WAY", "STR": "ST", "STRT": "ST",
    "DRV": "DR", "CRT": "CT", "BLV": "BLVD", "BOUL": "BLVD", "TERR": "TER",
    "PKY": "PKWY", "PKWY": "PKWY", "HWY": "HWY", "HIWAY": "HWY",
    "CIRCLE": "CIR", "CIR": "CIR", "CENTER": "CTR", "CENTRE": "CTR", "CTR": "CTR",
    "TRAIL": "TRL", "TRL": "TRL", "SQUARE": "SQ", "SQ": "SQ",
    "ZERO": "0", "ONE": "1", "TWO": "2", "THREE": "3", "FOUR": "4", "FIVE": "5",
    "SIX": "6", "SEVEN": "7", "EIGHT": "8", "NINE": "9", "TEN": "10",
    "ELEVEN": "11", "TWELVE": "12",
}

STREET_SUFFIX_TOKENS = {
    "ST", "AVE", "RD", "DR", "CT", "LN", "BLVD", "TER", "PL", "PKWY", "HWY", "RT",
    "WAY", "CIR", "CTR", "TRL", "SQ",
}
SECONDARY_ADDRESS_UNIT_TOKENS = {
    "APT", "APARTMENT", "UNIT", "STE", "SUITE", "FLOOR", "FL", "ROOM", "RM",
    "BLDG", "BUILDING", "LOT", "ATTN", "DEPT", "DEPARTMENT", "MAILSTOP", "MAIL",
    "STOP", "PMB", "TRLR", "TRAILER", "SPC", "SPACE", "CARE", "OF", "C", "O",
}
SECONDARY_ADDRESS_BOX_TOKENS = {"BOX"}


def _is_retryable_exception(err):
    text = f"{type(err).__name__}: {err}".lower()
    return any(signal in text for signal in RETRYABLE_EXCEPTION_SIGNALS)


def _payload_has_retryable_worker_exception(payload):
    if not isinstance(payload, dict) or payload.get("success"):
        return False
    if payload.get("retryable"):
        return True
    report = payload.get("report")
    if not isinstance(report, list):
        return False
    for item in report:
        if not isinstance(item, dict) or item.get("success"):
            continue
        if item.get("outcome") not in {"worker_exception", "batch_worker_exception"}:
            continue
        if _is_retryable_exception(item.get("message") or payload.get("message") or ""):
            return True
    return False


def _mark_transient_retry_attempted(payload):
    if not isinstance(payload, dict):
        return payload
    payload["transient_retry_attempted"] = True
    report = payload.get("report")
    if isinstance(report, list):
        for item in report:
            if isinstance(item, dict):
                item["retry_attempted"] = True
                item["transient_retry_attempted"] = True
    return payload


def _crm_attempt_modes():
    modes = [bool(CRM_HEADLESS)]
    if modes[0] and bool(CRM_ALLOW_VISIBLE_FALLBACK):
        modes.append(False)
    return modes


def _normalize_shipping_filter(value):
    key = str(value or "").strip().lower()
    key = key.replace("-", "_").replace(" ", "_")
    return key if key in {"all", "free", "rush", "813", "high_value"} else "free"


def _normalize_requested_batch_size(batch_size):
    if batch_size is None:
        return None
    text = str(batch_size).strip()
    if not text:
        return None
    if text.lower() in {"all", "continuous", "unlimited"}:
        return None
    try:
        number = int(float(text))
    except (TypeError, ValueError):
        return 1
    if number <= 0:
        return None
    return max(1, number)


def _batch_limit_reached(attempted_count, requested_batch_size):
    return requested_batch_size is not None and attempted_count >= requested_batch_size


def _batch_collection_limit(requested_batch_size, attempted_count, worker_limit=1):
    if requested_batch_size is None:
        return max(CONTINUOUS_BATCH_FETCH_LIMIT, int(worker_limit or 1))
    return max(1, requested_batch_size - attempted_count)


def _shipping_filter_label(value):
    key = _normalize_shipping_filter(value)
    if key == "813":
        return "813 orders"
    if key == "high_value":
        return "high value orders"
    if key == "all":
        return "all invalid-address orders"
    return "rush orders" if key == "rush" else "free ship orders"


def _normalized_list_url_override(list_url):
    return str(list_url or "").strip()


def _shipping_list_label(value, list_url_override=None):
    if _normalized_list_url_override(list_url_override):
        return "custom CRM list"
    return _shipping_filter_label(value)


def _shipping_list_url_for_filter(value, list_url_override=None):
    custom_url = _normalized_list_url_override(list_url_override)
    if custom_url:
        return custom_url
    key = _normalize_shipping_filter(value)
    if key == "813":
        return str(CRM_813_VALIDATOR_URL or CRM_SHIPPING_813_URL or "").strip()
    if key == "all":
        return str(CRM_SHIPPING_ALL_URL or CRM_SHIPPING_URL or CRM_SHIPPING_FREE_URL or CRM_SHIPPING_RUSH_URL or "").strip()
    if key == "high_value":
        return str(CRM_SHIPPING_HIGH_VALUE_URL or CRM_SHIPPING_RUSH_URL or CRM_SHIPPING_URL or CRM_SHIPPING_FREE_URL or "").strip()
    if key == "rush":
        return str(CRM_SHIPPING_RUSH_URL or CRM_SHIPPING_URL or CRM_SHIPPING_FREE_URL or "").strip()
    return str(CRM_SHIPPING_FREE_URL or CRM_SHIPPING_URL or CRM_SHIPPING_RUSH_URL or "").strip()


def _validate_runtime_config(shipping_filter=None, list_url_override=None):
    normalized_filter = _normalize_shipping_filter(shipping_filter or CRM_SHIPPING_FILTER_DEFAULT)
    if not str(_shipping_list_url_for_filter(normalized_filter, list_url_override=list_url_override) or "").strip():
        raise RuntimeError("A CRM shipping filter URL is empty in config.py.")
    if not str(CRM_LOGIN_URL or "").strip():
        raise RuntimeError("CRM_LOGIN_URL is empty in config.py.")
    read_windows_credential(CRM_CREDENTIAL_TARGET)


def _order_ids_from_text(text):
    ids = []
    seen = set()
    for match in ORDER_ID_PATTERN.findall(str(text or "")):
        if match in seen:
            continue
        seen.add(match)
        ids.append(match)
    return ids


def _normalize_target_order_id(raw):
    text = str(raw or "").strip()
    if not text:
        return None
    direct_match = re.search(r"/order/(\d{7})(?:\D|$)", text)
    if direct_match:
        return direct_match.group(1)
    matches = ORDER_ID_PATTERN.findall(text)
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    return matches[-1]


def _parse_css_rgb(value):
    match = CSS_RGB_PATTERN.search(str(value or ""))
    if not match:
        return None
    try:
        return tuple(max(0, min(255, int(match.group(index)))) for index in range(1, 4))
    except Exception:
        return None


def _classify_shipping_list_row_color(rgb):
    if not rgb or len(rgb) != 3:
        return None
    red, green, blue = rgb

    if red >= 120 and blue >= 120 and green <= 150 and (red - green) >= 30 and (blue - green) >= 30:
        return "purple"

    if blue >= 110 and red >= 80 and green <= 135 and (blue - green) >= 35 and (red - green) >= 20:
        return "purple"

    if red >= 180 and green <= 90 and blue <= 90 and (red - green) >= 80 and (red - blue) >= 80:
        return "bright_red"

    if red >= 90 and green <= 110 and blue <= 110 and (red - green) >= 30 and (red - blue) >= 30:
        return "dark_red"

    if red >= 220 and green >= 170 and blue >= 110 and red >= green >= blue - 15 and (red - blue) >= 25:
        return "tan"

    if green >= 170 and red <= 120 and blue <= 120 and (green - red) >= 60 and (green - blue) >= 60:
        return "lime_green"

    return None


def _classify_shipping_list_row_candidate(candidate):
    candidate = candidate if isinstance(candidate, dict) else {}
    rgb = _parse_css_rgb(candidate.get("backgroundColor"))
    label = _classify_shipping_list_row_color(rgb)
    if label:
        return label, rgb

    marker = " ".join(
        str(candidate.get(key) or "")
        for key in ("id", "className", "tag", "backgroundColor")
    ).lower()
    if "purple" in marker or "violet" in marker:
        return "purple", rgb
    if "lime" in marker or "green" in marker:
        return "lime_green", rgb
    if "tan" in marker or "natural" in marker:
        return "tan", rgb
    if "bright" in marker and "red" in marker:
        return "bright_red", rgb
    return None, rgb


def _describe_shipping_list_order_row(driver, link):
    details = driver.execute_script(
        r"""
const link = arguments[0];
function isTransparent(color){
  if(!color){ return true; }
  const normalized = String(color).replace(/\s+/g, '').toLowerCase();
  return normalized === 'transparent' || normalized === 'rgba(0,0,0,0)';
}
const rect = link.getBoundingClientRect();
const colors = [];
const seen = new Set();
function addNodeColor(node){
  if(!node || colors.length >= 40){ return; }
  const style = window.getComputedStyle(node);
  const bg = style.backgroundColor;
  if(!isTransparent(bg)){
    const nodeRect = node.getBoundingClientRect();
    const key = [bg, node.tagName, Math.round(nodeRect.width), Math.round(nodeRect.height)].join('|');
    if(!seen.has(key)){
      seen.add(key);
      colors.push({
        backgroundColor: bg,
        tag: node.tagName,
        id: node.id || '',
        className: String(node.className || ''),
        width: nodeRect.width || 0,
        height: nodeRect.height || 0
      });
    }
  }
}
function addAncestors(node, maxDepth){
  let depth = 0;
  for(let current = node; current && colors.length < 40 && depth < maxDepth; current = current.parentElement, depth++){
    addNodeColor(current);
  }
}
function addPointColors(x, y){
  if(!Number.isFinite(x) || !Number.isFinite(y)){ return; }
  if(x < 0 || y < 0 || x > window.innerWidth || y > window.innerHeight){ return; }
  const elements = document.elementsFromPoint(x, y);
  for(const node of elements){
    addAncestors(node, 8);
    if(colors.length >= 40){ return; }
  }
}
function addChildColors(node){
  for(const child of Array.from((node && node.children) || [])){
    addNodeColor(child);
    if(colors.length >= 40){ return; }
  }
}
const row = link.closest ? link.closest('tr,[role="row"],li,.row,.report-row') : null;
addAncestors(link, 16);
if(row){
  const rowRect = row.getBoundingClientRect();
  addAncestors(row, 8);
  addChildColors(row);
  const y = rowRect.top + Math.max(1, rowRect.height || rect.height || 1) / 2;
  const left = Math.max(0, rowRect.left + 4);
  const right = Math.min(window.innerWidth - 1, rowRect.right - 4);
  const middle = rowRect.left + Math.max(1, rowRect.width || rect.width || 1) / 2;
  for(const x of [rect.left + 2, rect.left + rect.width / 2, left, middle, right]){
    addPointColors(x, y);
  }
} else {
  const y = rect.top + Math.max(1, rect.height || 1) / 2;
  for(const x of [rect.left + 2, rect.left + rect.width / 2, rect.right + 24]){
    addPointColors(x, y);
  }
}
return {
  top: rect.top || 0,
  left: rect.left || 0,
  width: rect.width || 0,
  height: rect.height || 0,
  colors: colors
};
""",
        link,
    ) or {}

    for candidate in details.get("colors") or []:
        label, rgb = _classify_shipping_list_row_candidate(candidate)
        if label:
            return {
                "label": label,
                "rgb": rgb,
                "rect": (
                    float(details.get("top") or 0),
                    float(details.get("left") or 0),
                    float(details.get("width") or 0),
                    float(details.get("height") or 0),
                ),
                "element": candidate,
            }

    return {
        "label": None,
        "rgb": None,
        "rect": (
            float(details.get("top") or 0),
            float(details.get("left") or 0),
            float(details.get("width") or 0),
            float(details.get("height") or 0),
        ),
        "element": None,
    }


def _describe_shipping_list_order_rows(driver):
    details_list = driver.execute_script(
        r"""
const links = Array.from(document.querySelectorAll('a'));
function isTransparent(color){
  if(!color){ return true; }
  const normalized = String(color).replace(/\s+/g, '').toLowerCase();
  return normalized === 'transparent' || normalized === 'rgba(0,0,0,0)';
}
function isDisplayed(node){
  if(!node){ return false; }
  const rect = node.getBoundingClientRect();
  if((rect.width || 0) <= 0 && (rect.height || 0) <= 0){ return false; }
  for(let current = node; current; current = current.parentElement){
    const style = window.getComputedStyle(current);
    if(style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse'){
      return false;
    }
  }
  return true;
}
function addNodeColor(colors, seen, node){
  if(!node || colors.length >= 40){ return; }
  const style = window.getComputedStyle(node);
  const bg = style.backgroundColor;
  if(!isTransparent(bg)){
    const nodeRect = node.getBoundingClientRect();
    const key = [bg, node.tagName, Math.round(nodeRect.width), Math.round(nodeRect.height)].join('|');
    if(!seen.has(key)){
      seen.add(key);
      colors.push({
        backgroundColor: bg,
        tag: node.tagName,
        id: node.id || '',
        className: String(node.className || ''),
        width: nodeRect.width || 0,
        height: nodeRect.height || 0
      });
    }
  }
}
function addAncestors(colors, seen, node, maxDepth){
  let depth = 0;
  for(let current = node; current && colors.length < 40 && depth < maxDepth; current = current.parentElement, depth++){
    addNodeColor(colors, seen, current);
  }
}
function addPointColors(colors, seen, x, y){
  if(!Number.isFinite(x) || !Number.isFinite(y)){ return; }
  if(x < 0 || y < 0 || x > window.innerWidth || y > window.innerHeight){ return; }
  const elements = document.elementsFromPoint(x, y);
  for(const node of elements){
    addAncestors(colors, seen, node, 8);
    if(colors.length >= 40){ return; }
  }
}
function addChildColors(colors, seen, node){
  for(const child of Array.from((node && node.children) || [])){
    addNodeColor(colors, seen, child);
    if(colors.length >= 40){ return; }
  }
}
return links.map((link) => {
  const rect = link.getBoundingClientRect();
  const colors = [];
  const seen = new Set();
  const row = link.closest ? link.closest('tr,[role="row"],li,.row,.report-row') : null;
  addAncestors(colors, seen, link, 16);
  if(row){
    const rowRect = row.getBoundingClientRect();
    addAncestors(colors, seen, row, 8);
    addChildColors(colors, seen, row);
    const y = rowRect.top + Math.max(1, rowRect.height || rect.height || 1) / 2;
    const left = Math.max(0, rowRect.left + 4);
    const right = Math.min(window.innerWidth - 1, rowRect.right - 4);
    const middle = rowRect.left + Math.max(1, rowRect.width || rect.width || 1) / 2;
    for(const x of [rect.left + 2, rect.left + rect.width / 2, left, middle, right]){
      addPointColors(colors, seen, x, y);
    }
  } else {
    const y = rect.top + Math.max(1, rect.height || 1) / 2;
    for(const x of [rect.left + 2, rect.left + rect.width / 2, rect.right + 24]){
      addPointColors(colors, seen, x, y);
    }
  }
  return {
    text: link.innerText || link.textContent || '',
    displayed: isDisplayed(link),
    top: rect.top || 0,
    left: rect.left || 0,
    width: rect.width || 0,
    height: rect.height || 0,
    colors: colors
  };
});
""",
    ) or []
    if not isinstance(details_list, list):
        return None

    rows = []
    for details in details_list:
        row_info = {
            **_describe_shipping_list_order_row_from_details(details),
            "displayed": bool(details.get("displayed")),
            "text": str(details.get("text") or ""),
        }
        rows.append(row_info)
    return rows


def _describe_shipping_list_order_row_from_details(details):
    for candidate in details.get("colors") or []:
        label, rgb = _classify_shipping_list_row_candidate(candidate)
        if label:
            return {
                "label": label,
                "rgb": rgb,
                "rect": (
                    float(details.get("top") or 0),
                    float(details.get("left") or 0),
                    float(details.get("width") or 0),
                    float(details.get("height") or 0),
                ),
                "element": candidate,
            }

    return {
        "label": None,
        "rgb": None,
        "rect": (
            float(details.get("top") or 0),
            float(details.get("left") or 0),
            float(details.get("width") or 0),
            float(details.get("height") or 0),
        ),
        "element": None,
    }


def _find_shipping_list_orders_legacy(driver, limit=1, timeout=None, exclude_order_ids=None, allowed_row_labels=None, allowed_row_description=None):
    timeout = timeout or max(CRM_ACTION_TIMEOUT, 12)
    deadline = time.time() + timeout
    excluded = {str(order_id).strip() for order_id in (exclude_order_ids or []) if str(order_id).strip()}
    allowed_labels = tuple(allowed_row_labels or ALLOWED_SHIPPING_LIST_ROW_LABELS)
    allowed_description = str(allowed_row_description or ALLOWED_SHIPPING_LIST_ROW_DESCRIPTION)
    while time.time() < deadline:
        candidates = {}
        for link in driver.find_elements(By.XPATH, "//a"):
            try:
                if not link.is_displayed():
                    continue
                order_ids = _order_ids_from_text(link.text)
                if not order_ids:
                    continue
                order_id = order_ids[0]
                if order_id in excluded:
                    continue
                row_info = _describe_shipping_list_order_row(driver, link)
                if row_info.get("label") not in allowed_labels:
                    continue
                rect = row_info.get("rect") or (0.0, 0.0, 0.0, 0.0)
                sort_key = (float(rect[0]), float(rect[1]))
                current = candidates.get(order_id)
                if current is None or sort_key < current["sort_key"]:
                    candidates[order_id] = {
                        "sort_key": sort_key,
                        "order_id": order_id,
                        "link": link,
                        "row_info": row_info,
                    }
            except Exception:
                continue
        if candidates:
            ordered = sorted(candidates.values(), key=lambda item: item["sort_key"])
            selected = ordered[:max(1, int(limit or 1))]
            for item in selected:
                row_info = item.get("row_info") or {}
                color_label = row_info.get("label") or "unknown"
                color_rgb = row_info.get("rgb") or ()
                rgb_text = ",".join(str(part) for part in color_rgb) if color_rgb else "?"
                print(f"Selected shipping-list order {item['order_id']} from an allowed {color_label} row ({rgb_text}); allowed colors: {allowed_description}.")
            return selected
        time.sleep(0.25)
    return []


def _find_shipping_list_orders(driver, limit=1, timeout=None, exclude_order_ids=None, allowed_row_labels=None, allowed_row_description=None):
    timeout = timeout or max(CRM_ACTION_TIMEOUT, 12)
    deadline = time.time() + timeout
    excluded = {str(order_id).strip() for order_id in (exclude_order_ids or []) if str(order_id).strip()}
    allowed_labels = tuple(allowed_row_labels or ALLOWED_SHIPPING_LIST_ROW_LABELS)
    allowed_description = str(allowed_row_description or ALLOWED_SHIPPING_LIST_ROW_DESCRIPTION)
    while time.time() < deadline:
        try:
            links = driver.find_elements(By.XPATH, "//a")
            row_details = _describe_shipping_list_order_rows(driver)
        except Exception:
            row_details = None
            links = []

        if not row_details or len(row_details) != len(links):
            remaining = max(0.5, deadline - time.time())
            return _find_shipping_list_orders_legacy(
                driver,
                limit=limit,
                timeout=remaining,
                exclude_order_ids=excluded,
                allowed_row_labels=allowed_labels,
                allowed_row_description=allowed_description,
            )

        candidates = {}
        for link, details in zip(links, row_details):
            try:
                if not details.get("displayed"):
                    continue
                order_ids = _order_ids_from_text(details.get("text"))
                if not order_ids:
                    continue
                order_id = order_ids[0]
                if order_id in excluded:
                    continue
                row_info = {
                    "label": details.get("label"),
                    "rgb": details.get("rgb"),
                    "rect": details.get("rect"),
                    "element": details.get("element"),
                }
                if row_info.get("label") not in allowed_labels:
                    continue
                rect = row_info.get("rect") or (0.0, 0.0, 0.0, 0.0)
                sort_key = (float(rect[0]), float(rect[1]))
                current = candidates.get(order_id)
                if current is None or sort_key < current["sort_key"]:
                    candidates[order_id] = {
                        "sort_key": sort_key,
                        "order_id": order_id,
                        "link": link,
                        "row_info": row_info,
                    }
            except Exception:
                continue
        if candidates:
            ordered = sorted(candidates.values(), key=lambda item: item["sort_key"])
            selected = ordered[:max(1, int(limit or 1))]
            for item in selected:
                row_info = item.get("row_info") or {}
                color_label = row_info.get("label") or "unknown"
                color_rgb = row_info.get("rgb") or ()
                rgb_text = ",".join(str(part) for part in color_rgb) if color_rgb else "?"
                print(f"Selected shipping-list order {item['order_id']} from an allowed {color_label} row ({rgb_text}); allowed colors: {allowed_description}.")
            return selected
        time.sleep(0.25)
    return []


def _find_first_shipping_list_order(driver, timeout=None):
    matches = _find_shipping_list_orders(driver, limit=1, timeout=timeout)
    if not matches:
        return None, None
    first = matches[0]
    return first["order_id"], first["link"]


def _wait_for_any(root, selectors, timeout=None, condition="visible"):
    timeout = timeout or CRM_ACTION_TIMEOUT
    deadline = time.time() + max(1, timeout)
    last_error = None
    while time.time() < deadline:
        for by, value in selectors:
            try:
                element = root.find_element(by, value)
                if condition == "presence":
                    return element
                if condition == "visible" and element.is_displayed():
                    return element
                if condition == "clickable" and element.is_displayed() and element.is_enabled():
                    return element
            except Exception as exc:
                last_error = exc
        time.sleep(0.2)
    selector_text = " | ".join(f"{by}={value}" for by, value in selectors[:4])
    if last_error:
        raise TimeoutException(f"Timed out waiting for {condition} element: {selector_text} ({last_error})")
    raise TimeoutException(f"Timed out waiting for {condition} element: {selector_text}")


def _visible_elements(root, selectors):
    for by, value in selectors:
        try:
            elements = root.find_elements(by, value)
        except Exception:
            continue
        visible = []
        for element in elements:
            try:
                if element.is_displayed():
                    visible.append(element)
            except StaleElementReferenceException:
                continue
        if visible:
            return visible
    return []


def _click_with_fallback(driver, element):
    try:
        element.click()
        return
    except (ElementClickInterceptedException, ElementNotInteractableException, StaleElementReferenceException):
        pass
    try:
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
    except Exception:
        pass
    try:
        element.click()
        return
    except Exception:
        pass
    driver.execute_script("arguments[0].click();", element)


def _body_text(driver):
    try:
        return " ".join((driver.find_element(By.TAG_NAME, "body").text or "").split())
    except Exception:
        return ""


def _normalize_space(text):
    return " ".join(str(text or "").split())


def _address_fields_contain_email(address_fields):
    address_fields = address_fields or {}
    for key in ("address", "address_cont", "city", "state", "zip"):
        value = _normalize_space(address_fields.get(key))
        if not value:
            continue
        if "@" in value or EMAIL_ADDRESS_PATTERN.search(value):
            return True
    return False


def _merge_address_fields(primary, fallback):
    merged = dict(primary or {})
    for key in ("recipient", "address", "address_cont", "city", "state", "zip"):
        if not _normalize_space(merged.get(key)) and _normalize_space((fallback or {}).get(key)):
            merged[key] = (fallback or {}).get(key)
    return merged


def _normalize_selected_address_text(text):
    normalized = _normalize_space(text)
    return re.sub(r"\b([A-Z]{2}),\s+(\d{5}(?:-\d{4})?)\b", r"\1 \2", normalized)


def _extract_modal_recipient_name(modal):
    selectors = [
        (By.CSS_SELECTOR, '.modal-title'),
        (By.XPATH, ".//*[contains(normalize-space(.), 'Shipping Transaction for')]"),
        (By.XPATH, ".//h3[contains(normalize-space(.), 'Shipping Transaction for')]"),
    ]
    for by, value in selectors:
        try:
            elements = modal.find_elements(by, value)
        except Exception:
            continue
        for element in elements:
            text = _normalize_space(element.text)
            if not text:
                continue
            match = re.search(r"Shipping Transaction for\s+(.+)", text, re.IGNORECASE)
            if match:
                name = _normalize_space(match.group(1))
                if name:
                    return name
    return ""

def _tokenize(text):
    normalized = re.sub(r"[^A-Z0-9#]+", " ", str(text or "").upper())
    return [TEXT_TOKEN_MAP.get(token, token) for token in normalized.split()]


def _canonical_text(text):
    return " ".join(_tokenize(text))


STATE_CANONICAL_ALIASES = {}
for row in _STATE_ROWS:
    code, full = row.split(":", 1)
    for value in (code, full):
        canonical = _canonical_text(value)
        if canonical:
            STATE_CANONICAL_ALIASES[canonical] = full


def _normalize_state_text(state_text):
    raw = _canonical_text(state_text)
    return STATE_ALIASES.get(raw) or STATE_CANONICAL_ALIASES.get(raw, raw)


def _state_variants(state_text):
    raw = _normalize_state_text(state_text)
    variants = {raw}
    for code, full in STATE_ALIASES.items():
        if raw == full:
            variants.add(code)
        if raw == code:
            variants.add(full)
    return {value for value in variants if value}


def _state_matches(current_state, comparison_text):
    comparison = f" {_canonical_text(comparison_text)} "
    for variant in _state_variants(current_state):
        canonical_variant = _canonical_text(variant)
        if canonical_variant and f" {canonical_variant} " in comparison:
            return True
    return False


def _normalize_postal(postal_text):
    return re.sub(r"[^A-Z0-9]+", "", str(postal_text or "").upper())


def _postal_base(postal_text):
    normalized = _normalize_postal(postal_text)
    if re.match(r"^\d{5}", normalized):
        return normalized[:5]
    return normalized


def _postal_digit_distance(left, right):
    left = _normalize_postal(left)
    right = _normalize_postal(right)
    if len(left) != len(right) or not left or not right:
        return None
    return sum(1 for left_digit, right_digit in zip(left, right) if left_digit != right_digit)


def _postal_is_simple_transposition(left, right):
    left = _normalize_postal(left)
    right = _normalize_postal(right)
    if len(left) != len(right) or len(left) < 2:
        return False
    mismatch_indexes = [index for index, (left_digit, right_digit) in enumerate(zip(left, right)) if left_digit != right_digit]
    if len(mismatch_indexes) != 2:
        return False
    first, second = mismatch_indexes
    return left[first] == right[second] and left[second] == right[first]


def _is_us_postal(postal_text):
    return bool(re.match(r"^\d{5}", _normalize_postal(postal_text)))


def _is_base_only_us_postal(postal_text):
    return bool(re.match(r"^\d{5}$", _normalize_postal(postal_text)))


def _leading_house_parts(address_line):
    raw = _normalize_space(address_line)
    if raw:
        house_number_piece = r"(?:\d+[A-Z]?|[NSEW]\d+[A-Z]?)"
        match = re.match(
            rf"^(?P<number>{house_number_piece}(?:\s*-\s*{house_number_piece})?)(?:\s+(?P<rest>.+))?$",
            raw,
            flags=re.IGNORECASE,
        )
        if match:
            house = _normalize_postal(match.group("number"))
            if house:
                return house, _normalize_space(match.group("rest"))

    canonical = _canonical_text(address_line)
    tokens = canonical.split()
    if tokens and re.match(r"^\d", tokens[0]):
        return tokens[0], " ".join(tokens[1:])
    return "", canonical


def _house_token(address_line):
    house, _ = _leading_house_parts(address_line)
    return house


def _street_core(address_line):
    _, remainder = _leading_house_parts(address_line)
    return _canonical_text(remainder)


def _route_signature(text):
    tokens = _canonical_text(text).split()
    route_number_pattern = re.compile(r"^\d+[A-Z]?$")
    for idx, token in enumerate(tokens):
        next_token = tokens[idx + 1] if idx + 1 < len(tokens) else ""
        third_token = tokens[idx + 2] if idx + 2 < len(tokens) else ""
        if token == "STATE" and next_token == "RT" and route_number_pattern.match(third_token):
            return ("STATE_RT", third_token)
        if token in {"RT", "HWY", "SR", "SH", "FM", "RR", "US"} and route_number_pattern.match(next_token):
            return ("ROUTE", next_token)
        if token in STATE_CODES and route_number_pattern.match(next_token):
            return ("STATE_CODE_ROUTE", next_token)
    return None


def _looks_like_street_portion(address_line):
    if not _house_token(address_line):
        return False
    return bool(re.search(r"[A-Z]", _street_core(address_line)))


def _city_matches(current_city, comparison_text):
    city = _canonical_text(current_city)
    comparison = _canonical_text(comparison_text)
    if not city:
        return False
    city_variants = {city}
    if city == "WAIMEA":
        city_variants.add("KAMUELA")
    elif city == "KAMUELA":
        city_variants.add("WAIMEA")
    if any(variant in comparison for variant in city_variants):
        return True
    city_tokens = city.split()
    comparison_tokens = comparison.split()
    if len(city_tokens) == 1:
        return any(variant in comparison_tokens for variant in city_variants)
    if city.endswith(" OGDEN") and "OGDEN" in comparison_tokens:
        return True
    if city.startswith("NORTH ") and city.split(" ", 1)[1] in comparison:
        return True
    return city_tokens[0] in comparison_tokens and city_tokens[-1] in comparison_tokens


def _house_number_matches(address_line, comparison_text):
    house = _house_token(address_line)
    if not house:
        return False
    comparison_canonical = _canonical_text(comparison_text)
    if house in comparison_canonical:
        return True

    match = re.match(
        r"^\s*(?P<number>\d+[A-Z]?(?:\s*-\s*\d+[A-Z]?)?)\b",
        str(address_line or ""),
        flags=re.IGNORECASE,
    )
    if not match or "-" not in match.group("number"):
        return False

    house_parts = [
        _normalize_postal(part)
        for part in re.split(r"\s*-\s*", match.group("number"))
        if _normalize_postal(part)
    ]
    comparison_tokens = comparison_canonical.split()
    if not house_parts or len(comparison_tokens) < len(house_parts):
        return False
    for index in range(0, len(comparison_tokens) - len(house_parts) + 1):
        if comparison_tokens[index : index + len(house_parts)] == house_parts:
            return True
    return False


def _street_matches(address_line, comparison_text):
    street_core = _street_core(address_line)
    comparison = _canonical_text(comparison_text)
    if not street_core:
        return False
    current_route = _route_signature(street_core)
    comparison_route = _route_signature(comparison)
    if current_route and comparison_route and current_route[1] == comparison_route[1]:
        return True
    if street_core in comparison:
        return True
    compact_street_core = re.sub(r"[^A-Z0-9]+", "", street_core)
    compact_comparison = re.sub(r"[^A-Z0-9]+", "", comparison)
    if len(compact_street_core) >= 5 and compact_street_core in compact_comparison:
        return True
    current_tokens = [token for token in street_core.split() if token != "NEW"]
    comparison_tokens = comparison.split()
    if not current_tokens:
        return False
    overlap = sum(1 for token in current_tokens if token in comparison_tokens)
    if overlap / max(1, len(current_tokens)) >= 0.75:
        return True
    return SequenceMatcher(None, " ".join(current_tokens), comparison).ratio() >= 0.82


def _compact_runon_address_signature(address_line):
    canonical = _canonical_text(address_line)
    if not canonical or " " in canonical:
        return ""
    if _is_po_box(address_line):
        return ""
    if not re.match(r"^\d+[A-Z0-9]+$", canonical):
        return ""
    return canonical


def _compact_runon_address_matches(address_line, comparison_text):
    signature = _compact_runon_address_signature(address_line)
    if not signature:
        return False
    comparison_canonical = re.sub(r"[^A-Z0-9]+", "", str(comparison_text or "").upper())
    if not comparison_canonical:
        return False
    return signature in comparison_canonical


def _normalize_street_root_token(token):
    token = str(token or "").upper()
    match = ORDINAL_STREET_TOKEN_PATTERN.match(token)
    if match:
        return match.group(1)
    return token


def _street_tokens_from_validation_candidate(address_fields, candidate_text):
    tokens = _canonical_text(candidate_text).split()
    if tokens and any(ch.isdigit() for ch in tokens[0]):
        tokens = tokens[1:]
    city_tokens = set(_canonical_text(address_fields.get("city")).split())
    state_tokens = _state_variants(address_fields.get("state"))
    postal_tokens = {
        _postal_base(address_fields.get("zip")),
        _normalize_postal(address_fields.get("zip")),
    }
    filtered = []
    for token in tokens:
        if not token or token.isdigit():
            continue
        if token in city_tokens or token in state_tokens or token in postal_tokens:
            continue
        filtered.append(token)
    return filtered


def _validation_candidate_primary_address_line(address_fields, candidate_text):
    candidate_text = _normalize_space(candidate_text)
    city_pattern = _flexible_value_pattern((address_fields or {}).get("city"))
    postal_base = _postal_base((address_fields or {}).get("zip"))
    if not candidate_text:
        return ""

    separator = r"(?:\s*,\s*|\s+)"
    candidate_patterns = []
    if city_pattern:
        for state_variant in sorted({variant for variant in _state_variants((address_fields or {}).get("state")) if variant}, key=len, reverse=True):
            state_pattern = _flexible_value_pattern(state_variant)
            if not state_pattern:
                continue
            if postal_base:
                candidate_patterns.append(
                    rf"^(?P<address>.+?){separator}{city_pattern}{separator}{state_pattern}{separator}{re.escape(postal_base)}(?:-\d{{4}})?(?:\s*[,.;]*)$"
                )
            candidate_patterns.append(
                rf"^(?P<address>.+?){separator}{city_pattern}{separator}{state_pattern}(?:\s*[,.;]*)$"
            )
        if postal_base:
            candidate_patterns.append(
                rf"^(?P<address>.+?){separator}{city_pattern}{separator}{re.escape(postal_base)}(?:-\d{{4}})?(?:\s*[,.;]*)$"
            )
        candidate_patterns.append(
            rf"^(?P<address>.+?){separator}{city_pattern}(?:\s*[,.;]*)$"
        )
    if postal_base:
        candidate_patterns.append(
            rf"^(?P<address>.+?){separator}{re.escape(postal_base)}(?:-\d{{4}})?(?:\s*[,.;]*)$"
        )

    for pattern in candidate_patterns:
        match = re.match(pattern, candidate_text, flags=re.IGNORECASE)
        if not match:
            continue
        address_line = _normalize_space(match.group("address"))
        if address_line:
            return address_line
    return ""


def _validation_street_matches(address_fields, comparison_text):
    address_line = address_fields.get("address")
    if _street_matches(address_line, comparison_text):
        return True

    current_root_tokens = _street_root_tokens(address_line)
    option_tokens = _street_tokens_from_validation_candidate(address_fields, comparison_text)
    option_root_tokens = [token for token in option_tokens if token not in STREET_SUFFIX_TOKENS]
    if not current_root_tokens or not option_root_tokens:
        return False

    for current_token in current_root_tokens[:2]:
        for option_token in option_root_tokens[:2]:
            if _tokens_share_street_root(current_token, option_token):
                return True

    overlap = sum(1 for token in current_root_tokens if token in option_root_tokens)
    return overlap / max(1, len(current_root_tokens)) >= 0.6


def _street_root_tokens(address_line):
    return [
        _normalize_street_root_token(token)
        for token in _street_core(address_line).split()
        if token and token != "NEW" and token not in STREET_SUFFIX_TOKENS
    ]


def _street_suffix_tokens(address_line):
    return {token for token in _street_core(address_line).split() if token in STREET_SUFFIX_TOKENS}


def _street_tokens_from_existing_option(address_fields, option_text):
    address_part = _existing_address_text_after_name(option_text)
    tokens = _canonical_text(address_part).split()
    if tokens and any(ch.isdigit() for ch in tokens[0]):
        tokens = tokens[1:]
    city_tokens = set(_canonical_text(address_fields.get("city")).split())
    state_tokens = _state_variants(address_fields.get("state"))
    postal_tokens = {
        _postal_base(address_fields.get("zip")),
        _normalize_postal(address_fields.get("zip")),
    }
    filtered = []
    for token in tokens:
        if not token or token.isdigit():
            continue
        if token in city_tokens or token in state_tokens or token in postal_tokens:
            continue
        filtered.append(token)
    return filtered


def _tokens_share_street_root(left, right):
    left = _normalize_street_root_token(left)
    right = _normalize_street_root_token(right)
    if not left or not right:
        return False
    if left == right:
        return True
    shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
    if len(shorter) >= 4 and longer.startswith(shorter):
        return True
    return SequenceMatcher(None, left, right).ratio() >= 0.76


def _existing_street_matches(address_fields, option_text):
    address_line = address_fields.get("address")
    if _street_matches(address_line, option_text):
        return True

    current_root_tokens = _street_root_tokens(address_line)
    option_tokens = _street_tokens_from_existing_option(address_fields, option_text)
    option_root_tokens = [token for token in option_tokens if token not in STREET_SUFFIX_TOKENS]
    if not current_root_tokens or not option_root_tokens:
        return False

    current_suffixes = _street_suffix_tokens(address_line)
    option_suffixes = {token for token in option_tokens if token in STREET_SUFFIX_TOKENS}
    suffix_compatible = not current_suffixes or not option_suffixes or bool(current_suffixes & option_suffixes)
    if not suffix_compatible:
        return False

    for current_token in current_root_tokens[:2]:
        for option_token in option_root_tokens[:2]:
            if _tokens_share_street_root(current_token, option_token):
                return True

    overlap = sum(1 for token in current_root_tokens if token in option_root_tokens)
    return overlap / max(1, len(current_root_tokens)) >= 0.6


def _merge_address_cont_value(existing_value, new_value):
    existing_value = _normalize_space(existing_value)
    new_value = _normalize_space(new_value)
    if not existing_value:
        return new_value
    if not new_value:
        return existing_value
    existing_canonical = _canonical_text(existing_value)
    new_canonical = _canonical_text(new_value)
    if not existing_canonical:
        return new_value
    if not new_canonical:
        return existing_value
    if new_canonical in existing_canonical:
        return existing_value
    if existing_canonical in new_canonical:
        return new_value
    return _normalize_space(f"{existing_value} {new_value}")


def _clean_split_street_line(address_line):
    return _normalize_display_address_line(_normalize_space(address_line).rstrip(" ,.;"))


def _split_embedded_po_box_indicator(address_line):
    address_line = _normalize_space(address_line)
    if not address_line:
        return "", ""
    match = EMBEDDED_PO_BOX_TRAILER_PATTERN.match(address_line)
    if not match:
        return "", ""
    street_line = _clean_split_street_line(match.group("street"))
    po_box_line = _normalize_space(match.group("po_box"))
    if not street_line or not po_box_line:
        return "", ""
    if not _looks_like_street_portion(street_line):
        return "", ""
    return street_line, po_box_line


def _secondary_address_profile(text):
    tokens = _tokenize(text)
    collapsed_tokens = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        next_token = tokens[index + 1] if index + 1 < len(tokens) else ""
        if token == "R" and next_token == "R":
            collapsed_tokens.append("RR")
            index += 2
            continue
        collapsed_tokens.append(token)
        index += 1
    canonical = " ".join(collapsed_tokens)
    categories = set()
    values = []
    seen_values = set()
    for token in collapsed_tokens:
        stripped = token.lstrip("#")
        if not stripped:
            continue
        if token.startswith("#"):
            categories.add("HASH")
        if token in SECONDARY_ADDRESS_BOX_TOKENS:
            categories.add("BOX")
            continue
        if token in SECONDARY_ADDRESS_UNIT_TOKENS:
            categories.add("UNIT")
            continue
        if stripped not in seen_values:
            values.append(stripped)
            seen_values.add(stripped)
    return {
        "canonical": canonical,
        "categories": categories,
        "values": tuple(values),
    }


def _secondary_address_profiles_compatible(left_text, right_text):
    left = _secondary_address_profile(left_text)
    right = _secondary_address_profile(right_text)
    if not left["canonical"] or not right["canonical"]:
        return False
    if left["canonical"] in right["canonical"] or right["canonical"] in left["canonical"]:
        return True
    left_values = set(left["values"])
    right_values = set(right["values"])
    if not left_values or not right_values or left_values != right_values:
        return False
    if left["categories"] == right["categories"]:
        return True
    if not left["categories"] or not right["categories"]:
        return True
    if left["categories"] == {"HASH"} or right["categories"] == {"HASH"}:
        return True
    return bool(left["categories"] & right["categories"])


def _address_cont_value_preserved(required_cont, actual_cont):
    required_cont = _normalize_space(required_cont)
    actual_cont = _normalize_space(actual_cont)
    if not required_cont:
        return True
    if not actual_cont:
        return False

    required_canonical = _canonical_text(required_cont)
    actual_canonical = _canonical_text(actual_cont)
    if required_canonical and required_canonical in actual_canonical:
        return True
    if _secondary_address_matches(required_cont, actual_cont):
        return True
    return _secondary_address_profiles_compatible(required_cont, actual_cont)


def _secondary_address_matches(required_cont, comparison_text):
    required = _secondary_address_profile(required_cont)
    if not required["values"]:
        return not _normalize_space(required_cont)

    comparison = _secondary_address_profile(comparison_text)
    comparison_values = set(comparison["values"])
    if not set(required["values"]).issubset(comparison_values):
        return False
    if not required["categories"]:
        return True
    if required["categories"] == {"HASH"}:
        return True
    if "HASH" in comparison["categories"]:
        return True
    return bool(required["categories"] & comparison["categories"])


def _secondary_address_can_be_preserved(required_cont, comparison_text, *, allow_any_missing_secondary=False):
    required = _secondary_address_profile(required_cont)
    if not required["values"]:
        return False
    comparison = _secondary_address_profile(comparison_text)
    if comparison["categories"]:
        return False
    if allow_any_missing_secondary:
        return True
    required_tokens = set(required["canonical"].split())
    return bool(required_tokens & {"BLDG", "BUILDING"})


def _address_cont_looks_like_locality_overflow(address_fields):
    address_cont = _normalize_space((address_fields or {}).get("address_cont"))
    if not address_cont:
        return False
    city = _normalize_space((address_fields or {}).get("city"))
    state = _normalize_space((address_fields or {}).get("state"))
    postal = _postal_base((address_fields or {}).get("zip"))
    if not postal:
        return False
    has_city = _city_matches(city, address_cont) if city else False
    has_state = _state_matches(state, address_cont) if state else False
    has_postal = postal in _normalize_postal(address_cont)
    return has_postal and (has_city or has_state)


def _effective_address_cont(address_fields):
    if _address_cont_looks_like_locality_overflow(address_fields):
        return ""
    return _normalize_space((address_fields or {}).get("address_cont"))


def _normalize_display_address_line(address_line):
    tokens = _normalize_space(address_line).split()
    if not tokens:
        return ""
    mapped = TEXT_TOKEN_MAP.get(tokens[0].upper())
    if mapped and mapped.isdigit():
        tokens[0] = mapped
    return _normalize_space(" ".join(tokens))


def _clean_city_field_value(city_text, state_text, postal_text):
    del postal_text
    cleaned = _normalize_space(city_text)
    if not cleaned:
        return ""
    cleaned = re.sub(r"\b\d{5}(?:-\d{4})?\b", "", cleaned).strip(" ,")
    variants = sorted({variant for variant in _state_variants(state_text) if variant}, key=len, reverse=True)
    for variant in variants:
        cleaned = re.sub(
            rf"(?:,\s*|\s+){re.escape(variant)}\.?$",
            "",
            cleaned,
            flags=re.IGNORECASE,
        ).strip(" ,")
    return _normalize_space(cleaned)


def _flexible_value_pattern(text):
    tokens = [re.escape(token) for token in re.findall(r"[A-Za-z0-9]+", str(text or ""))]
    if not tokens:
        return ""
    return r"(?:[\s,.-]+)".join(tokens)


def _clean_address_line_locality_suffix(address_line, city_text, state_text, postal_text):
    address_line = _normalize_space(address_line)
    postal_base = _postal_base(postal_text)
    city_pattern = _flexible_value_pattern(city_text)
    if not address_line:
        return address_line, ""

    separator = r"(?:\s*,\s*|\s+)"
    postal_pattern = rf"{re.escape(postal_base)}(?:-\d{{4}})?"
    candidate_patterns = []
    if city_pattern:
        for state_variant in sorted({variant for variant in _state_variants(state_text) if variant}, key=len, reverse=True):
            state_pattern = _flexible_value_pattern(state_variant)
            if not state_pattern:
                continue
            if postal_base:
                candidate_patterns.append(
                    rf"^(?P<street>.+?){separator}(?P<suffix>{city_pattern}{separator}{state_pattern}{separator}{postal_pattern})(?:\s*[,.;]*)$"
                )
            candidate_patterns.append(
                rf"^(?P<street>.+?){separator}(?P<suffix>{city_pattern}{separator}{state_pattern})(?:\s*[,.;]*)$"
            )
        if postal_base:
            candidate_patterns.append(
                rf"^(?P<street>.+?){separator}(?P<suffix>{city_pattern}{separator}{postal_pattern})(?:\s*[,.;]*)$"
            )
        candidate_patterns.append(
            rf"^(?P<street>.+?){separator}(?P<suffix>{city_pattern})(?:\s*[,.;]*)$"
        )
    if postal_base:
        candidate_patterns.append(
            rf"^(?P<street>.+?){separator}(?P<suffix>{postal_pattern})(?:\s*[,.;]*)$"
        )

    for pattern in candidate_patterns:
        match = re.match(pattern, address_line, flags=re.IGNORECASE)
        if not match:
            continue
        street = _normalize_space(match.group("street"))
        suffix = _normalize_space(match.group("suffix"))
        if not street or not suffix:
            continue
        if not _looks_like_street_portion(street):
            continue
        return street, suffix
    return address_line, ""


def _extract_embedded_street_candidate(text, city_text, state_text, postal_text):
    raw = _normalize_space(text)
    if not raw:
        return "", "", ""

    search_offsets = []
    if _house_token(raw):
        search_offsets.append(0)
    else:
        for match in re.finditer(r"(?<![A-Z0-9])\d+[A-Z]?(?:\s*-\s*\d+[A-Z]?)?\b", raw, flags=re.IGNORECASE):
            search_offsets.append(match.start())

    seen_offsets = set()
    for offset in search_offsets:
        if offset in seen_offsets:
            continue
        seen_offsets.add(offset)
        prefix = _normalize_space(raw[:offset])
        candidate_text = _normalize_space(raw[offset:])
        cleaned_candidate, removed_locality_suffix = _clean_address_line_locality_suffix(
            candidate_text,
            city_text,
            state_text,
            postal_text,
        )
        cleaned_candidate = _normalize_display_address_line(cleaned_candidate)
        if not _looks_like_street_portion(cleaned_candidate):
            continue
        if not (_street_suffix_tokens(cleaned_candidate) or _is_highway_address(cleaned_candidate)):
            continue
        return prefix, cleaned_candidate, removed_locality_suffix
    return "", "", ""


def _recover_misaligned_street_address(address_fields):
    address_fields = dict(address_fields or {})
    address_line = _normalize_space(address_fields.get("address"))
    address_cont = _effective_address_cont(address_fields)
    city = address_fields.get("city")
    state = address_fields.get("state")
    postal = address_fields.get("zip")

    if not _house_token(address_line):
        prefix, street_line, removed_locality_suffix = _extract_embedded_street_candidate(
            address_line,
            city,
            state,
            postal,
        )
        if street_line:
            return {
                "source_field": "address",
                "address": street_line,
                "address_cont": _merge_address_cont_value(prefix, address_cont),
                "removed_locality_suffix": removed_locality_suffix,
            }

    if not _house_token(address_line) and address_cont:
        prefix, street_line, removed_locality_suffix = _extract_embedded_street_candidate(
            address_cont,
            city,
            state,
            postal,
        )
        if street_line:
            return {
                "source_field": "address_cont",
                "address": street_line,
                "address_cont": _merge_address_cont_value(address_line, prefix),
                "removed_locality_suffix": removed_locality_suffix,
            }
    return None


def _address_cont_looks_like_street_fragment(address_fields):
    address_line = _normalize_space((address_fields or {}).get("address"))
    address_cont = _effective_address_cont(address_fields)
    if not address_line or not address_cont:
        return False
    if not _house_token(address_line):
        return False
    if not _is_missing_street_name(address_line):
        return False
    if _house_token(address_cont):
        return False
    if _is_po_box(address_cont):
        return False
    if _secondary_address_profile(address_cont).get("categories"):
        return False
    return bool(re.search(r"[A-Z]", _canonical_text(address_cont)))


def _shipping_address_needs_split_street_normalization(address_fields):
    return _address_cont_looks_like_street_fragment(address_fields)


def _dedupe_address_identifier(address_line, address_cont):
    address_line = _normalize_space(address_line)
    address_cont = _normalize_space(address_cont)
    embedded_street, embedded_po_box = _split_embedded_po_box_indicator(address_line)
    if embedded_street and embedded_po_box:
        merged_address_cont = _merge_address_cont_value(embedded_po_box, address_cont) if address_cont else embedded_po_box
        return embedded_street, merged_address_cont, embedded_po_box
    original_match = None
    for pattern in UNIT_TRAILER_PATTERNS:
        original_match = pattern.match(address_line.upper()) if address_line else None
        if original_match and _normalize_space(original_match.group("street")):
            break
        original_match = None
    if not original_match:
        for pattern in LEADING_UNIT_PREFIX_PATTERNS:
            original_match = pattern.match(address_line.upper()) if address_line else None
            if original_match:
                break
    if not original_match:
        return address_line, address_cont, ""
    extracted = _normalize_space(original_match.group("unit"))
    cleaned = _normalize_display_address_line(_normalize_space(original_match.group("street")))
    if not cleaned:
        return address_line, address_cont, ""
    if not _looks_like_street_portion(cleaned):
        return address_line, address_cont, ""
    if address_cont and not _secondary_address_profiles_compatible(address_cont, extracted):
        return address_line, address_cont, ""
    merged_address_cont = _merge_address_cont_value(extracted, address_cont) if address_cont else extracted
    return cleaned, merged_address_cont, extracted


def _is_po_box(address_line):
    canonical = _canonical_text(address_line)
    if not canonical:
        return False
    if "PO BOX" in canonical or "P O BOX" in canonical or canonical.startswith("BOX "):
        return True
    return bool(re.search(r"\bPOST OFFICE BOX\b", canonical))


def _classify_po_box_address(address_fields):
    address_line = _normalize_space(address_fields.get("address"))
    address_cont = _normalize_space(address_fields.get("address_cont"))
    embedded_street, embedded_po_box = _split_embedded_po_box_indicator(address_line)
    if not embedded_po_box:
        deduped_street, deduped_cont, extracted_identifier = _dedupe_address_identifier(address_line, address_cont)
        if extracted_identifier and "BOX" in _secondary_address_profile(extracted_identifier).get("categories", set()):
            embedded_street = deduped_street
            embedded_po_box = extracted_identifier
    address_is_po_box = bool(_is_po_box(address_line) and not embedded_street)
    address_cont_is_po_box = _is_po_box(address_cont)
    address_is_street = bool(
        embedded_street
        or (address_line and not address_is_po_box and _looks_like_street_portion(address_line))
    )
    effective_po_box_line = embedded_po_box or (address_cont if address_cont_is_po_box else (address_line if address_is_po_box else ""))
    address_cont_is_street = bool(address_cont and not address_cont_is_po_box and _looks_like_street_portion(address_cont))
    has_po_box = address_is_po_box or address_cont_is_po_box or bool(embedded_po_box)
    has_street = address_is_street or address_cont_is_street
    return {
        "address_is_po_box": address_is_po_box,
        "address_cont_is_po_box": address_cont_is_po_box,
        "address_has_embedded_po_box": bool(embedded_po_box),
        "address_is_street": address_is_street,
        "address_cont_is_street": address_cont_is_street,
        "has_po_box": has_po_box,
        "has_street": has_street,
        "po_box_only": has_po_box and not has_street,
        "mixed_po_box_and_street": has_po_box and has_street,
        "needs_swap": address_is_po_box and address_cont_is_street,
        "needs_embedded_po_box_split": bool(embedded_po_box),
        "street_already_primary": address_is_street and (address_cont_is_po_box or bool(embedded_po_box)),
        "street_line": embedded_street if embedded_street else (address_line if address_is_street else (address_cont if address_cont_is_street else "")),
        "po_box_line": effective_po_box_line,
    }


def _swap_street_and_po_box_lines(modal, warnings):
    current = _extract_current_address(modal)
    profile = _classify_po_box_address(current)
    if not profile["needs_swap"]:
        return current, profile

    address_field = _find_address_form_input(modal, ADDRESS_LINE_INPUT_SELECTORS, "* Address:")
    if address_field is None:
        address_field = _find_address_form_input(modal, ADDRESS_LINE_INPUT_SELECTORS, "Address:")
    address_cont_field = _find_address_form_input(modal, ADDRESS_CONT_INPUT_SELECTORS, "Address (cont):")
    if address_field is None or address_cont_field is None:
        return current, profile

    street_line = profile["street_line"]
    po_box_line = profile["po_box_line"]
    _set_input_value(address_field, street_line)
    _set_input_value(address_cont_field, po_box_line)
    time.sleep(0.2)
    updated = _extract_current_address(modal)
    warnings.append(f"Moved street address '{street_line}' into Address and preserved '{po_box_line}' in Address (cont).")
    return updated, _classify_po_box_address(updated)


def _is_missing_street_name(address_line):
    canonical = _canonical_text(address_line)
    if not canonical:
        return True
    return not bool(re.search(r"[A-Z]", canonical))


def _is_missing_street_number(address_line):
    canonical = _canonical_text(address_line)
    if not canonical:
        return True
    if _is_po_box(address_line):
        return False
    if _is_highway_address(address_line):
        return False
    return not bool(_house_token(address_line))


def _address_cont_street_number_candidate(address_fields):
    address_fields = address_fields or {}
    address_line = _normalize_space(address_fields.get("address"))
    if not address_line or not _is_missing_street_number(address_line) or _is_missing_street_name(address_line):
        return ""
    address_cont = _effective_address_cont(address_fields)
    if not address_cont or _is_po_box(address_cont):
        return ""
    categories = _secondary_address_profile(address_cont).get("categories") or set()
    if categories & {"UNIT", "BOX"}:
        return ""
    candidate = _normalize_space(address_cont).strip(" #,.;")
    if not re.fullmatch(r"\d+[A-Z]?(?:\s*-\s*\d+[A-Z]?)?", candidate, flags=re.IGNORECASE):
        return ""
    return re.sub(r"\s*-\s*", "-", candidate).upper()


def _move_address_cont_number_to_primary_address(modal, warnings, current_address):
    street_number = _address_cont_street_number_candidate(current_address)
    if not street_number:
        return current_address, ""
    address_field = _find_address_form_input(modal, ADDRESS_LINE_INPUT_SELECTORS, "* Address:")
    if address_field is None:
        address_field = _find_address_form_input(modal, ADDRESS_LINE_INPUT_SELECTORS, "Address:")
    address_cont_field = _find_address_form_input(modal, ADDRESS_CONT_INPUT_SELECTORS, "Address (cont):")
    if address_field is None or address_cont_field is None:
        return current_address, ""

    original_address = _normalize_space(current_address.get("address"))
    original_address_cont = _normalize_space(current_address.get("address_cont"))
    _set_input_value(address_field, f"{street_number} {original_address}")
    _set_input_value(address_cont_field, "")
    time.sleep(0.2)
    updated = _extract_current_address(modal)
    if _house_token(updated.get("address")) != _normalize_postal(street_number):
        return updated, ""
    warnings.append(
        f"Moved '{original_address_cont}' from Address (cont) into Address as the missing street number before validation."
    )
    return updated, street_number


def _is_highway_address(address_line):
    canonical = _canonical_text(address_line)
    if not canonical:
        return False
    tokens = canonical.split()
    route_number_pattern = re.compile(r"^\d+[A-Z]?$")

    for idx, token in enumerate(tokens):
        next_token = tokens[idx + 1] if idx + 1 < len(tokens) else ""
        third_token = tokens[idx + 2] if idx + 2 < len(tokens) else ""

        if token in {"HWY", "RT", "SR", "SH", "FM", "RR"} and route_number_pattern.match(next_token):
            return True
        if token == "US" and route_number_pattern.match(next_token):
            return True
        if token in {"COUNTY", "CO"} and next_token in {"RD", "RT"} and route_number_pattern.match(third_token):
            return True
        if token == "STATE" and next_token == "RT" and route_number_pattern.match(third_token):
            return True
        if token in STATE_CODES and route_number_pattern.match(next_token):
            if idx == 0 or (idx == 1 and _house_token(tokens[0])):
                return True

    return False


def _is_military_address(address_fields):
    return _normalize_state_text(address_fields.get("state")) in MILITARY_STATE_CODES


def _is_canadian_postal(postal_text):
    return bool(re.match(r"^[A-Z]\d[A-Z]\d[A-Z]\d$", _normalize_postal(postal_text)))


def _postal_tokens_from_text(text):
    text = str(text or "")
    us_tokens = [
        _normalize_postal(match.group(0))
        for match in re.finditer(r"\b\d{5}(?:-\d{4})?\b", text)
    ]
    canadian_tokens = [
        _normalize_postal(match.group(0))
        for match in re.finditer(r"\b[A-Za-z]\d[A-Za-z][ -]?\d[A-Za-z]\d\b", text)
    ]
    return [token for token in us_tokens + canadian_tokens if token]


def _postal_matches(current_postal, comparison_text):
    current_norm = _normalize_postal(current_postal)
    if not current_norm:
        return False, False, False
    if _is_us_postal(current_postal):
        base = _postal_base(current_postal)
        comparison_tokens = [token for token in _postal_tokens_from_text(comparison_text) if re.match(r"^\d{5}(?:\d{4})?$", token)]
        if comparison_tokens:
            near_match = any(
                len(token) >= 5
                and token[:5] != base
                and (
                    _postal_digit_distance(base, token[:5]) == 1
                    or _postal_is_simple_transposition(base, token[:5])
                )
                for token in comparison_tokens
            )
            return (
                bool(base and any(token.startswith(base) for token in comparison_tokens)),
                any(token == current_norm for token in comparison_tokens),
                bool(base and near_match),
            )
        comparison_norm = _normalize_postal(comparison_text)
        comparison_base = comparison_norm[:5] if len(comparison_norm) >= 5 else comparison_norm
        return (
            bool(base and base in comparison_norm),
            False,
            bool(
                base
                and comparison_base
                and comparison_base != base
                and (
                    _postal_digit_distance(base, comparison_base) == 1
                    or _postal_is_simple_transposition(base, comparison_base)
                )
            ),
        )
    if _is_canadian_postal(current_postal):
        comparison_tokens = [token for token in _postal_tokens_from_text(comparison_text) if re.match(r"^[A-Z]\d[A-Z]\d[A-Z]\d$", token)]
        if comparison_tokens:
            near_match = any(
                token != current_norm
                and (
                    _postal_digit_distance(current_norm, token) == 1
                    or _postal_is_simple_transposition(current_norm, token)
                )
                for token in comparison_tokens
            )
            full_match = any(token == current_norm for token in comparison_tokens)
            return full_match, full_match, near_match
        comparison_norm = _normalize_postal(comparison_text)
        return (
            current_norm == comparison_norm,
            current_norm == comparison_norm,
            bool(
                comparison_norm
                and comparison_norm != current_norm
                and len(comparison_norm) == len(current_norm)
                and (
                    _postal_digit_distance(current_norm, comparison_norm) == 1
                    or _postal_is_simple_transposition(current_norm, comparison_norm)
                )
            ),
        )
    comparison_norm = _normalize_postal(comparison_text)
    return current_norm in comparison_norm, current_norm in comparison_norm, False


def _postal_same_prefix_match(current_postal, comparison_text, prefix_length=2):
    base = _postal_base(current_postal)
    if _is_canadian_postal(current_postal):
        current_norm = _normalize_postal(current_postal)
        canadian_prefix_length = 4
        comparison_tokens = [
            token
            for token in _postal_tokens_from_text(comparison_text)
            if re.match(r"^[A-Z]\d[A-Z]\d[A-Z]\d$", token)
        ]
        if comparison_tokens:
            for token in comparison_tokens:
                if token == current_norm or len(token) < canadian_prefix_length:
                    continue
                if token[:canadian_prefix_length] == current_norm[:canadian_prefix_length]:
                    return True
            return False
        comparison_norm = _normalize_postal(comparison_text)
        return bool(
            len(comparison_norm) >= canadian_prefix_length
            and comparison_norm != current_norm
            and comparison_norm[:canadian_prefix_length] == current_norm[:canadian_prefix_length]
        )
    if not re.match(r"^\d{5}$", str(base or "")):
        return False
    comparison_tokens = [
        _normalize_postal(match.group(0))
        for match in re.finditer(r"\b\d{5}(?:-\d{4})?\b", str(comparison_text or ""))
    ]
    if comparison_tokens:
        for token in comparison_tokens:
            candidate_base = token[:5]
            if candidate_base == base or len(candidate_base) != 5:
                continue
            if candidate_base[:prefix_length] == base[:prefix_length]:
                return True
        return False
    comparison_norm = _normalize_postal(comparison_text)
    comparison_base = comparison_norm[:5] if len(comparison_norm) >= 5 else comparison_norm
    return bool(
        len(comparison_base) == 5
        and comparison_base != base
        and comparison_base[:prefix_length] == base[:prefix_length]
    )


def _assessment_can_be_used(assessment):
    assessment = assessment or {}
    return bool(
        assessment.get("required_match")
        or assessment.get("missing_number_rescue")
        or assessment.get("safe_postal_near_match")
        or assessment.get("safe_postal_prefix_match")
    )


def _resolution_from_assessment(base_resolution, label, assessment, warnings):
    assessment = assessment or {}
    resolution = base_resolution
    if assessment.get("city_only_mismatch"):
        warnings.append(f"{label} was accepted with a city-only difference.")
        resolution = f"{base_resolution}_city_difference"
    if assessment.get("missing_number_rescue"):
        warnings.append(f"{label} was accepted because it restored the missing street number while the rest of the address still matched.")
        if resolution == base_resolution:
            resolution = f"{base_resolution}_missing_number_rescue"
    if assessment.get("postal_near_match"):
        warnings.append(f"{label} was accepted with a small ZIP difference because the rest of the address matched closely.")
        if resolution == base_resolution:
            resolution = f"{base_resolution}_zip_near_match"
    if assessment.get("postal_prefix_match") and not assessment.get("postal_base_match") and not assessment.get("postal_near_match"):
        if assessment.get("canadian_postal_prefix_match"):
            warnings.append(f"{label} was accepted because the postal code shared the same first 4 characters and the rest of the address matched closely.")
        else:
            warnings.append(f"{label} was accepted because the ZIP shared the same first 2 digits and the rest of the address matched closely.")
        if resolution == base_resolution:
            resolution = f"{base_resolution}_zip_prefix_match"
    return resolution


def _assess_address_text(address_fields, comparison_text):
    comparison = _normalize_space(comparison_text)
    compact_runon_match = _compact_runon_address_matches(address_fields.get("address"), comparison_text)
    house_match = _house_number_matches(address_fields.get("address"), comparison) or compact_runon_match
    street_match = _validation_street_matches(address_fields, comparison) or compact_runon_match
    state_match = _state_matches(address_fields.get("state"), comparison)
    postal_base_match, postal_full_match, postal_near_match = _postal_matches(address_fields.get("zip"), comparison)
    postal_prefix_match = _postal_same_prefix_match(address_fields.get("zip"), comparison)
    city_match = _city_matches(address_fields.get("city"), comparison)
    secondary_required = bool(_secondary_address_profile(address_fields.get("address_cont"))["values"])
    secondary_match = _secondary_address_matches(address_fields.get("address_cont"), comparison_text)
    secondary_preserved = _secondary_address_can_be_preserved(
        address_fields.get("address_cont"),
        comparison_text,
        allow_any_missing_secondary=True,
    )
    secondary_ok = (not secondary_required) or secondary_match or secondary_preserved
    missing_number_rescue = (
        not bool(_house_token(address_fields.get("address")))
        and bool(_house_token(comparison_text))
        and street_match
        and state_match
        and city_match
        and (postal_base_match or postal_near_match or postal_prefix_match)
        and secondary_ok
    )
    safe_postal_near_match = house_match and street_match and state_match and city_match and postal_near_match and secondary_ok
    safe_postal_prefix_match = house_match and street_match and state_match and city_match and postal_prefix_match and secondary_ok
    mismatch_fields = []
    if not house_match:
        mismatch_fields.append("address_number")
    if not street_match:
        mismatch_fields.append("street")
    if not state_match:
        mismatch_fields.append("state")
    if not postal_base_match and not postal_near_match and not postal_prefix_match:
        mismatch_fields.append("zip")
    if not city_match:
        mismatch_fields.append("city")
    if secondary_required and not secondary_ok:
        mismatch_fields.append("address_cont")
    return {
        "required_match": house_match and street_match and state_match and postal_base_match,
        "exact_match": house_match and street_match and state_match and postal_full_match and city_match and secondary_ok,
        "city_only_mismatch": house_match and street_match and state_match and postal_base_match and not city_match and secondary_ok,
        "zip_plus4_only": house_match and street_match and state_match and city_match and postal_base_match and not postal_full_match and _is_us_postal(address_fields.get("zip")) and secondary_ok,
        "mismatch_fields": mismatch_fields,
        "postal_base_match": postal_base_match,
        "postal_full_match": postal_full_match,
        "postal_near_match": postal_near_match,
        "postal_prefix_match": postal_prefix_match,
        "canadian_postal_prefix_match": _is_canadian_postal(address_fields.get("zip")) and postal_prefix_match and not postal_base_match,
        "city_match": city_match,
        "secondary_required": secondary_required,
        "secondary_match": secondary_match,
        "secondary_preserved": secondary_preserved,
        "missing_number_rescue": missing_number_rescue,
        "safe_postal_near_match": safe_postal_near_match,
        "safe_postal_prefix_match": safe_postal_prefix_match,
        "compact_runon_match": compact_runon_match,
        "house_match": house_match,
        "street_match": street_match,
        "state_match": state_match,
    }


def _assess_existing_address_text(address_fields, comparison_text):
    comparison = _normalize_space(comparison_text)
    comparison_address = _existing_address_text_after_name(comparison_text)
    comparison_house = _house_token(comparison_address) or _house_token(comparison_text)
    house = _house_token(address_fields.get("address"))
    compact_runon_match = _compact_runon_address_matches(address_fields.get("address"), comparison_text)
    house_match = _house_number_matches(address_fields.get("address"), comparison) or compact_runon_match
    street_match = _existing_street_matches(address_fields, comparison_text) or compact_runon_match
    state_match = _state_matches(address_fields.get("state"), comparison)
    postal_base_match, postal_full_match, postal_near_match = _postal_matches(address_fields.get("zip"), comparison)
    postal_prefix_match = _postal_same_prefix_match(address_fields.get("zip"), comparison)
    city_match = _city_matches(address_fields.get("city"), comparison)
    secondary_required = bool(_secondary_address_profile(address_fields.get("address_cont"))["values"])
    secondary_match = _secondary_address_matches(address_fields.get("address_cont"), comparison_text)
    secondary_preserved = _secondary_address_can_be_preserved(address_fields.get("address_cont"), comparison_text)
    secondary_ok = (not secondary_required) or secondary_match or secondary_preserved
    military_without_house_match = (
        _is_military_address(address_fields)
        and not bool(house)
        and not bool(comparison_house)
        and street_match
        and state_match
        and city_match
        and postal_base_match
        and secondary_ok
    )
    effective_house_match = house_match or military_without_house_match
    missing_number_rescue = (
        not bool(house)
        and bool(comparison_house)
        and street_match
        and state_match
        and city_match
        and (postal_base_match or postal_near_match or postal_prefix_match)
        and secondary_ok
    )
    safe_postal_near_match = effective_house_match and street_match and state_match and city_match and postal_near_match and secondary_ok
    safe_postal_prefix_match = effective_house_match and street_match and state_match and city_match and postal_prefix_match and secondary_ok
    mismatch_fields = []
    if not effective_house_match:
        mismatch_fields.append("address_number")
    if not street_match:
        mismatch_fields.append("street")
    if not state_match:
        mismatch_fields.append("state")
    if not postal_base_match and not postal_near_match and not postal_prefix_match:
        mismatch_fields.append("zip")
    if not city_match:
        mismatch_fields.append("city")
    if secondary_required and not secondary_ok:
        mismatch_fields.append("address_cont")
    return {
        "required_match": effective_house_match and street_match and state_match and postal_base_match,
        "exact_match": effective_house_match and street_match and state_match and postal_full_match and city_match and secondary_ok,
        "city_only_mismatch": effective_house_match and street_match and state_match and postal_base_match and not city_match and secondary_ok,
        "zip_plus4_only": effective_house_match and street_match and state_match and city_match and postal_base_match and not postal_full_match and _is_us_postal(address_fields.get("zip")) and secondary_ok,
        "mismatch_fields": mismatch_fields,
        "postal_base_match": postal_base_match,
        "postal_full_match": postal_full_match,
        "postal_near_match": postal_near_match,
        "postal_prefix_match": postal_prefix_match,
        "canadian_postal_prefix_match": _is_canadian_postal(address_fields.get("zip")) and postal_prefix_match and not postal_base_match,
        "city_match": city_match,
        "secondary_required": secondary_required,
        "secondary_match": secondary_match,
        "secondary_preserved": secondary_preserved,
        "missing_number_rescue": missing_number_rescue,
        "safe_postal_near_match": safe_postal_near_match,
        "safe_postal_prefix_match": safe_postal_prefix_match,
        "compact_runon_match": compact_runon_match,
        "house_match": effective_house_match,
        "military_without_house_match": military_without_house_match,
        "street_match": street_match,
        "state_match": state_match,
    }


def _find_labeled_input(modal, label_text):
    selectors = [
        (By.XPATH, f".//*[contains(normalize-space(.), '{label_text}')]/following::input[1]"),
        (By.XPATH, f".//*[contains(normalize-space(.), '{label_text}')]/following::textarea[1]"),
    ]
    return _wait_for_any(modal, selectors, timeout=4, condition="visible")


def _find_visible_element(root, selectors):
    for by, value in selectors:
        try:
            elements = root.find_elements(by, value)
        except Exception:
            continue
        for element in elements:
            try:
                if element.is_displayed():
                    return element
            except Exception:
                continue
    return None


def _find_address_form_input(modal, selectors, fallback_label_text=None):
    element = _find_visible_element(modal, selectors)
    if element is not None:
        return element
    if fallback_label_text:
        try:
            return _find_labeled_input(modal, fallback_label_text)
        except Exception:
            return None
    return None


def _find_state_field(modal):
    selectors = [
        (By.XPATH, ".//*[contains(normalize-space(.), 'State:')]/following::select[1]"),
        (By.XPATH, ".//*[contains(normalize-space(.), 'State:')]/following::*[self::input or self::select][1]"),
    ]
    return _wait_for_any(modal, selectors, timeout=4, condition="visible")


def _string_value(element):
    try:
        return _normalize_space(element.get_attribute("value") or element.text or "")
    except Exception:
        return ""


def _extract_current_address_once(modal):
    state_field = _find_address_form_input(modal, STATE_INPUT_SELECTORS)
    if state_field is None:
        state_field = _find_state_field(modal)
    try:
        state_text = _normalize_space(Select(state_field).first_selected_option.text)
    except Exception:
        state_text = _string_value(state_field) if state_field is not None else ""
    recipient_field = _find_address_form_input(modal, RECIPIENT_INPUT_SELECTORS, "* To:")
    address_field = _find_address_form_input(modal, ADDRESS_LINE_INPUT_SELECTORS, "* Address:")
    if address_field is None:
        address_field = _find_address_form_input(modal, ADDRESS_LINE_INPUT_SELECTORS, "Address:")
    address_cont_field = _find_address_form_input(modal, ADDRESS_CONT_INPUT_SELECTORS, "Address (cont):")
    city_field = _find_address_form_input(modal, CITY_INPUT_SELECTORS, "City:")
    zip_field = _find_address_form_input(modal, ZIP_INPUT_SELECTORS, "Zip:")
    return {
        "recipient": _string_value(recipient_field),
        "address": _string_value(address_field),
        "address_cont": _string_value(address_cont_field),
        "city": _string_value(city_field),
        "state": state_text,
        "zip": _string_value(zip_field),
    }


def _extract_current_address(modal, timeout=2.5):
    deadline = time.time() + max(0.2, float(timeout or 0))
    last = _extract_current_address_once(modal)
    while time.time() < deadline:
        if any(_normalize_space(last.get(key)) for key in ("address", "city", "zip", "state")):
            return last
        time.sleep(0.2)
        last = _extract_current_address_once(modal)
    return last


def _set_input_value(field, value):
    driver = getattr(field, "parent", None) or getattr(field, "_parent", None)
    target_value = _normalize_space(value)
    try:
        if driver is not None:
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", field)
    except Exception:
        pass
    try:
        field.click()
    except Exception:
        try:
            if driver is not None:
                driver.execute_script("arguments[0].focus();", field)
        except Exception:
            pass
    try:
        select_all_key = Keys.COMMAND if sys.platform == "darwin" else Keys.CONTROL
        field.send_keys(select_all_key, "a")
        field.send_keys(Keys.BACKSPACE)
        field.send_keys(Keys.DELETE)
        if target_value:
            field.send_keys(target_value)
        time.sleep(0.05)
        current_value = _normalize_space(field.get_attribute("value") or field.text or "")
        if current_value == target_value:
            return
    except Exception:
        if driver is None:
            raise
    driver.execute_script(
        """
        const el = arguments[0];
        const nextValue = arguments[1] || '';
        const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), 'value')
          || Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')
          || Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value');
        if (setter && setter.set) {
          setter.set.call(el, nextValue);
        } else {
          el.value = nextValue;
        }
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        el.dispatchEvent(new Event('blur', { bubbles: true }));
        """,
        field,
        target_value,
    )


def _ensure_recipient_present(modal, original_recipient, warnings):
    current = _extract_current_address(modal)
    if _normalize_space(current.get("recipient")):
        return True, current

    expected = _normalize_space(original_recipient) or _extract_modal_recipient_name(modal)
    if expected:
        try:
            recipient_field = _find_labeled_input(modal, "* To:")
            if recipient_field is not None:
                _set_input_value(recipient_field, expected)
                time.sleep(0.25)
        except Exception:
            pass
        current = _extract_current_address(modal)
        if _normalize_space(current.get("recipient")):
            warnings.append("Restored the customer name in the To field after CRM cleared it.")
            return True, current

    return False, current


def _recipient_missing_result(order_id, warnings, original_address, final_address):
    return _result_for(
        order_id,
        "recipient_cleared",
        "Skipped because the To field was cleared and the customer name could not be restored.",
        success=False,
        resolution="manual_review",
        manual_review=True,
        warnings=warnings,
        original_address=original_address,
        final_address=final_address,
    )


def _email_in_shipping_address_result(order_id, warnings, original_address, final_address):
    return _result_for(
        order_id,
        "email_in_shipping_address",
        "Skipped because the shipping address fields contain an email address and need manual correction.",
        success=False,
        resolution="manual_review",
        manual_review=True,
        warnings=warnings,
        original_address=original_address,
        final_address=final_address,
    )


def _address_cont_preservation_failed_result(order_id, warnings, original_address, final_address, required_cont):
    return _result_for(
        order_id,
        "address_cont_not_preserved",
        f"Skipped because '{required_cont}' could not be preserved in Address (cont) before saving.",
        success=False,
        resolution="manual_review",
        manual_review=True,
        warnings=warnings,
        original_address=original_address,
        final_address=final_address,
    )


def _ensure_address_cont_preserved(modal, required_cont, warnings):
    required_cont = _normalize_space(required_cont)
    current = _extract_current_address(modal)
    if not required_cont:
        return True, current
    if _address_cont_value_preserved(required_cont, current.get("address_cont")):
        return True, current
    address_cont_field = _find_address_form_input(modal, ADDRESS_CONT_INPUT_SELECTORS, "Address (cont):")
    if address_cont_field is None:
        return False, current
    merged_cont = _merge_address_cont_value(current.get("address_cont"), required_cont)
    _set_input_value(address_cont_field, merged_cont)
    time.sleep(0.2)
    updated = _extract_current_address(modal)
    if not _address_cont_value_preserved(required_cont, updated.get("address_cont")):
        return False, updated
    if _canonical_text(current.get("address_cont")) != _canonical_text(updated.get("address_cont")):
        warnings.append(f"Preserved '{required_cont}' in Address (cont).")
    return True, updated


def _prepare_shipping_form_for_save(order_id, shipping_modal, original_address, preserved_address_cont, warnings, allow_rewrite=True):
    recipient_ok, final_address = _ensure_recipient_present(shipping_modal, original_address.get("recipient"), warnings)
    if not recipient_ok:
        return _recipient_missing_result(order_id, warnings, original_address, final_address), None, preserved_address_cont
    if _address_fields_contain_email(final_address):
        warnings.append("Detected an email address in the shipping address fields before saving.")
        return _email_in_shipping_address_result(order_id, warnings, original_address, final_address), None, preserved_address_cont
    if allow_rewrite:
        final_address, preserved_address_cont = _rewrite_address_fields_if_needed(shipping_modal, warnings, preserved_address_cont)
        if _address_fields_contain_email(final_address):
            warnings.append("Detected an email address in the shipping address fields after cleanup.")
            return _email_in_shipping_address_result(order_id, warnings, original_address, final_address), None, preserved_address_cont
        preserved_ok, final_address = _ensure_address_cont_preserved(shipping_modal, preserved_address_cont, warnings)
        if not preserved_ok:
            return _address_cont_preservation_failed_result(order_id, warnings, original_address, final_address, preserved_address_cont), None, preserved_address_cont
        return None, final_address, preserved_address_cont

    preserved_token = _canonical_text(preserved_address_cont)
    if preserved_token:
        in_cont = _address_cont_value_preserved(preserved_address_cont, final_address.get("address_cont"))
        in_main = preserved_token in _canonical_text(final_address.get("address"))
        if not in_cont and not in_main:
            return _address_cont_preservation_failed_result(order_id, warnings, original_address, final_address, preserved_address_cont), None, preserved_address_cont
        if in_main and not in_cont:
            warnings.append(f"Preserved '{preserved_address_cont}' in the main address line because CRM already considered the saved address valid.")
    return None, final_address, preserved_address_cont


def _format_address_fields(address_fields):
    parts = [
        address_fields.get("address"),
        address_fields.get("address_cont"),
        address_fields.get("city"),
        address_fields.get("state"),
        address_fields.get("zip"),
    ]
    return ", ".join([part for part in parts if part])


def _result_for(order_id, outcome, message, success=False, resolution="", warnings=None, manual_review=True, original_address=None, final_address=None, retry_attempted=False):
    normalized_order_id = _normalize_target_order_id(order_id) if order_id else None
    item = {
        "order_id": normalized_order_id,
        "success": bool(success),
        "outcome": outcome,
        "message": message,
        "resolution": resolution,
        "manual_review_required": bool(manual_review),
        "warnings": warnings or [],
        "retry_attempted": bool(retry_attempted),
    }
    if original_address:
        item["original_address"] = original_address
        item["original_address_text"] = _format_address_fields(original_address)
    if final_address:
        item["final_address"] = final_address
        item["final_address_text"] = _format_address_fields(final_address)
    return item

def _extract_option_text(input_element):
    candidates = [input_element]
    for xpath in ('./ancestor::label[1]', './ancestor::div[1]', './..'):
        try:
            candidates.append(input_element.find_element(By.XPATH, xpath))
        except Exception:
            pass
    for element in candidates:
        text = _normalize_space(element.text)
        if text:
            return text
    return ""


def _existing_address_text_after_name(option_text):
    text = _normalize_space(option_text)
    if text.startswith('- '):
        return text[2:].strip()
    if ' - ' in text:
        return text.rsplit(' - ', 1)[1]
    return text


def _is_mostly_all_caps_text(text):
    letters = [ch for ch in _normalize_space(text) if ch.isalpha()]
    if len(letters) < 4:
        return False
    uppercase_count = sum(1 for ch in letters if ch.isupper())
    return (uppercase_count / len(letters)) >= 0.9


def _shipping_address_needs_caps_normalization(address_fields):
    if not address_fields:
        return False
    normalized_shipping_text = _normalize_space(
        " ".join(
            part
            for part in (
                address_fields.get("address"),
                address_fields.get("city"),
            )
            if _normalize_space(part)
        )
    )
    if not normalized_shipping_text:
        return False
    return not _is_mostly_all_caps_text(normalized_shipping_text)


def _is_all_caps_existing_address(option_text):
    return _is_mostly_all_caps_text(_existing_address_text_after_name(option_text))


def _get_existing_address_options(modal):
    options = []
    seen = set()
    for element in modal.find_elements(By.XPATH, ".//input[@type='radio']"):
        text = _extract_option_text(element)
        normalized_text = _normalize_space(text)
        if not normalized_text:
            continue
        if " - " not in normalized_text and not normalized_text.startswith('- '):
            continue
        if normalized_text in seen:
            continue
        seen.add(normalized_text)
        options.append({
            "text": normalized_text,
            "preferred_all_caps": _is_all_caps_existing_address(normalized_text),
        })
    return options


def _find_existing_address_scroll_container(driver, modal):
    try:
        container = driver.execute_script(
            """
            const modal = arguments[0];
            const nodes = [modal, ...modal.querySelectorAll('*')];
            let best = null;
            let bestScore = -1;
            for (const node of nodes) {
              if (!node.querySelector || !node.querySelector('input[type="radio"]')) continue;
              const style = window.getComputedStyle(node);
              const overflowY = (style.overflowY || '').toLowerCase();
              const scrollable = node.scrollHeight > node.clientHeight + 24;
              if (!scrollable) continue;
              if (node !== modal && overflowY !== 'auto' && overflowY !== 'scroll') continue;
              const radios = node.querySelectorAll('input[type="radio"]').length;
              const score = radios * 10 + Math.min(node.scrollHeight - node.clientHeight, 1000);
              if (score > bestScore) {
                best = node;
                bestScore = score;
              }
            }
            return best || modal;
            """,
            modal,
        )
        return container or modal
    except Exception:
        return modal


def _scroll_existing_address_container(driver, container, amount=None):
    try:
        return driver.execute_script(
            """
            const node = arguments[0];
            const amount = arguments[1];
            const before = Number(node.scrollTop || 0);
            const delta = amount || Math.max(220, Math.floor((node.clientHeight || 0) * 0.75));
            node.scrollTop = Math.min(before + delta, Math.max(0, (node.scrollHeight || 0) - (node.clientHeight || 0)));
            return { before, after: Number(node.scrollTop || 0) };
            """,
            container,
            amount,
        )
    except Exception:
        return {"before": 0, "after": 0}


def _collect_radio_option_texts_batched(driver, modal, max_scrolls=12, require_displayed=False, require_existing_format=False):
    try:
        texts = driver.execute_async_script(
            r"""
            const modal = arguments[0];
            const maxScrolls = Math.max(0, Number(arguments[1] || 0));
            const requireDisplayed = !!arguments[2];
            const requireExistingFormat = !!arguments[3];
            const done = arguments[arguments.length - 1];

            function normalizeSpace(text) {
              return String(text || '').replace(/\s+/g, ' ').trim();
            }

            function isVisible(node) {
              if (!node) return false;
              const rect = node.getBoundingClientRect();
              if ((rect.width || 0) <= 0 && (rect.height || 0) <= 0) return false;
              for (let current = node; current; current = current.parentElement) {
                const style = window.getComputedStyle(current);
                if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') {
                  return false;
                }
              }
              return true;
            }

            function optionText(input) {
              const candidates = [input];
              const label = input.closest ? input.closest('label') : null;
              const div = input.closest ? input.closest('div') : null;
              const parent = input.parentElement || null;
              for (const candidate of [label, div, parent]) {
                if (candidate && !candidates.includes(candidate)) {
                  candidates.push(candidate);
                }
              }
              for (const candidate of candidates) {
                const text = normalizeSpace((candidate && (candidate.innerText || candidate.textContent)) || '');
                if (text) return text;
              }
              return '';
            }

            function findContainer(modalRoot) {
              const nodes = [modalRoot, ...modalRoot.querySelectorAll('*')];
              let best = modalRoot;
              let bestScore = -1;
              for (const node of nodes) {
                if (!node.querySelector || !node.querySelector('input[type="radio"]')) continue;
                const style = window.getComputedStyle(node);
                const overflowY = (style.overflowY || '').toLowerCase();
                const scrollable = node.scrollHeight > node.clientHeight + 24;
                if (!scrollable) continue;
                if (node !== modalRoot && overflowY !== 'auto' && overflowY !== 'scroll') continue;
                const radios = node.querySelectorAll('input[type="radio"]').length;
                const score = radios * 10 + Math.min(node.scrollHeight - node.clientHeight, 1000);
                if (score > bestScore) {
                  best = node;
                  bestScore = score;
                }
              }
              return best || modalRoot;
            }

            function collect(collected, seen) {
              const radios = modal.querySelectorAll('input[type="radio"]');
              for (const radio of radios) {
                if (requireDisplayed && !isVisible(radio)) continue;
                const text = normalizeSpace(optionText(radio));
                if (!text) continue;
                if (requireExistingFormat && text.indexOf(' - ') === -1 && !text.startsWith('- ')) continue;
                if (seen.has(text)) continue;
                seen.add(text);
                collected.push(text);
              }
            }

            const container = findContainer(modal);
            const collected = [];
            const seen = new Set();
            const start = Date.now();
            const firstWaitMs = 4000;
            let scrollCount = 0;

            try {
              container.scrollTop = 0;
            } catch (err) {}

            function finish() {
              try {
                container.scrollTop = 0;
              } catch (err) {}
              done(collected);
            }

            function step() {
              collect(collected, seen);

              if (!collected.length && (Date.now() - start) < firstWaitMs) {
                setTimeout(step, 100);
                return;
              }

              if (scrollCount >= maxScrolls) {
                finish();
                return;
              }

              const before = Number(container.scrollTop || 0);
              const delta = Math.max(220, Math.floor((container.clientHeight || 0) * 0.75));
              const maxTop = Math.max(0, Number((container.scrollHeight || 0) - (container.clientHeight || 0)));
              const after = Math.min(before + delta, maxTop);
              if (after <= before + 1) {
                finish();
                return;
              }

              container.scrollTop = after;
              scrollCount += 1;
              setTimeout(step, 60);
            }

            step();
            """,
            modal,
            int(max_scrolls or 0),
            bool(require_displayed),
            bool(require_existing_format),
        )
    except Exception:
        return None

    if not isinstance(texts, list):
        return None
    return [_normalize_space(text) for text in texts if _normalize_space(text)]


def _collect_existing_address_options_legacy(driver, modal, max_scrolls=12):
    collected = {}
    container = _find_existing_address_scroll_container(driver, modal)
    try:
        driver.execute_script("arguments[0].scrollTop = 0;", container)
    except Exception:
        pass

    initial_deadline = time.time() + 4.0
    while time.time() < initial_deadline:
        for option in _get_existing_address_options(modal):
            collected[option["text"]] = option
        if collected:
            break
        time.sleep(0.2)

    time.sleep(0.15)
    for _ in range(max_scrolls + 1):
        for option in _get_existing_address_options(modal):
            collected[option["text"]] = option
        moved = _scroll_existing_address_container(driver, container)
        if float(moved.get("after", 0)) <= float(moved.get("before", 0)) + 1:
            break
        time.sleep(0.15)

    if not collected:
        retry_deadline = time.time() + 2.0
        while time.time() < retry_deadline:
            for option in _get_existing_address_options(modal):
                collected[option["text"]] = option
            if collected:
                break
            time.sleep(0.2)

    try:
        driver.execute_script("arguments[0].scrollTop = 0;", container)
    except Exception:
        pass
    return list(collected.values())


def _collect_existing_address_options(driver, modal, max_scrolls=12):
    texts = _collect_radio_option_texts_batched(
        driver,
        modal,
        max_scrolls=max_scrolls,
        require_displayed=False,
        require_existing_format=True,
    )
    if texts is None:
        return _collect_existing_address_options_legacy(driver, modal, max_scrolls=max_scrolls)
    return [
        {
            "text": text,
            "preferred_all_caps": _is_all_caps_existing_address(text),
        }
        for text in texts
    ]


def _select_existing_address_option_by_text(driver, modal, target_text, max_scrolls=12):
    target_text = _normalize_space(target_text)
    container = _find_existing_address_scroll_container(driver, modal)
    try:
        driver.execute_script("arguments[0].scrollTop = 0;", container)
    except Exception:
        pass
    time.sleep(0.15)

    def _try_select_in_current_view():
        try:
            selected = driver.execute_script(
                r"""
                const modal = arguments[0];
                const target = String(arguments[1] || '').replace(/\s+/g, ' ').trim();
                function normalize(value){
                  return String(value || '').replace(/\s+/g, ' ').trim();
                }
                function textFor(input){
                  const candidates = [];
                  if (input.labels) candidates.push(...input.labels);
                  let node = input;
                  for (let i = 0; node && i < 4; i += 1){
                    node = node.parentElement;
                    if (node) candidates.push(node);
                  }
                  for (const candidate of candidates){
                    const text = normalize(candidate && candidate.textContent);
                    if (text) return text;
                  }
                  return '';
                }
                const radios = Array.from(modal.querySelectorAll("input[type='radio']"));
                for (const radio of radios){
                  const text = textFor(radio);
                  if (!text || !text.includes(' - ')) continue;
                  if (normalize(text) !== target) continue;
                  try {
                    radio.scrollIntoView({block: 'center', inline: 'nearest'});
                  } catch (error) {}
                  try {
                    radio.click();
                  } catch (error) {
                    const label = radio.labels && radio.labels.length ? radio.labels[0] : radio.parentElement;
                    if (label && typeof label.click === 'function') {
                      label.click();
                    } else {
                      radio.checked = true;
                    }
                  }
                  radio.dispatchEvent(new Event('input', { bubbles: true }));
                  radio.dispatchEvent(new Event('change', { bubbles: true }));
                  return true;
                }
                return false;
                """,
                modal,
                target_text,
            )
            return bool(selected)
        except Exception:
            return False

    for _ in range(max_scrolls + 1):
        if _try_select_in_current_view():
            time.sleep(0.4)
            return True
        moved = _scroll_existing_address_container(driver, container)
        if float(moved.get("after", 0)) <= float(moved.get("before", 0)) + 1:
            break
        time.sleep(0.15)
    return False


def _pick_existing_address_option(address_fields, options):
    best = None
    best_score = None
    for option in options:
        assessment = _assess_existing_address_text(address_fields, option["text"])
        if _existing_address_would_drop_secondary({"assessment": assessment}):
            continue
        mismatch_count = len([field for field in assessment["mismatch_fields"] if field in {"address_number", "street", "state", "zip", "city"}])
        if not _assessment_can_be_used(assessment) or mismatch_count > 1:
            continue
        score = (
            1 if assessment.get("missing_number_rescue") else 0,
            1 if assessment.get("safe_postal_near_match") else 0,
            1 if assessment.get("safe_postal_prefix_match") else 0,
            1 if assessment.get("secondary_match") else 0,
            1 if option.get("preferred_all_caps") else 0,
            1 if assessment["exact_match"] else 0,
            1 if assessment["city_match"] else 0,
            1 if assessment["postal_full_match"] else 0,
            -mismatch_count,
            len(option["text"]),
        )
        if best_score is None or score > best_score:
            best_score = score
            best = {"option": option, "assessment": assessment}
    return best


def _find_best_existing_address_option(address_fields, existing_options):
    preview_address, preview_preserved_address_cont, extracted_identifier = _dedupe_address_identifier(
        address_fields.get("address"),
        address_fields.get("address_cont"),
    )
    if extracted_identifier:
        rewritten_preview_address = dict(address_fields)
        rewritten_preview_address["address"] = preview_address
        rewritten_preview_address["address_cont"] = preview_preserved_address_cont
        return _pick_existing_address_option(rewritten_preview_address, existing_options)
    return _pick_existing_address_option(address_fields, existing_options)


def _text_has_zip_plus4(text):
    normalized = _normalize_postal(text)
    return bool(re.match(r"^\d{9}$", normalized[-9:])) if len(normalized) >= 9 else False


def _existing_address_looks_like_weak_duplicate(best_existing):
    if not best_existing:
        return False
    option = best_existing.get("option") or {}
    assessment = best_existing.get("assessment") or {}
    if option.get("preferred_all_caps"):
        return False
    return bool(
        assessment.get("exact_match")
        and assessment.get("city_match")
        and assessment.get("postal_full_match")
    )


def _existing_address_would_drop_secondary(best_existing):
    assessment = (best_existing or {}).get("assessment") or {}
    return bool(
        assessment.get("secondary_required")
        and not assessment.get("secondary_match")
        and not assessment.get("secondary_preserved")
    )


def _try_resolve_with_existing_address(driver, shipping_modal, order_id, dry_run, original_address, current_address, preserved_address_cont, warnings, allow_rewrite=False, max_scrolls=12, existing_options=None, best_existing=None, accept_save_button_ready=True, allow_prevalidated_selection=False, allow_assessed_current_address=False):
    existing_options = existing_options or _collect_existing_address_options(driver, shipping_modal, max_scrolls=max_scrolls)
    best_existing = best_existing or _find_best_existing_address_option(current_address, existing_options)
    if best_existing is None:
        return None
    if _existing_address_would_drop_secondary(best_existing):
        required_cont = _normalize_space(current_address.get("address_cont") or preserved_address_cont)
        if required_cont:
            warnings.append(
                f"Skipped the matching saved address because it did not include '{required_cont}' in Address (cont); running Save & Verify Address instead."
            )
        return None

    print("Selecting the closest existing address option...")
    selected_existing = _select_existing_address_option_by_text(driver, shipping_modal, best_existing["option"]["text"])
    if not selected_existing and (_address_is_valid(shipping_modal) or _final_save_ready(driver, shipping_modal, timeout=1.5)):
        warnings.append("Existing address option was already selected when the shipping editor opened.")
        selected_existing = True
    if not selected_existing:
        raise TimeoutException("The matching existing address option could not be selected from the saved-address list.")

    recipient_ok, current_address = _ensure_recipient_present(shipping_modal, original_address.get("recipient"), warnings)
    if not recipient_ok:
        return _recipient_missing_result(order_id, warnings, original_address, current_address)

    if allow_rewrite:
        current_address, preserved_address_cont = _rewrite_address_fields_if_needed(
            shipping_modal,
            warnings,
            preserved_address_cont,
        )
        recipient_ok, current_address = _ensure_recipient_present(
            shipping_modal,
            original_address.get("recipient"),
            warnings,
        )
        if not recipient_ok:
            return _recipient_missing_result(order_id, warnings, original_address, current_address)

    time.sleep(0.2)
    existing_ready = _wait_for_address_valid(shipping_modal, timeout=3)
    if accept_save_button_ready and not existing_ready and _final_save_ready(driver, shipping_modal, timeout=2.5):
        warnings.append("Existing address did not render the green valid-address text, but the final Save button became available.")
        existing_ready = True
    if allow_prevalidated_selection and not existing_ready:
        warnings.append("Reused a previously valid matching saved address after CRM returned no candidates.")
        existing_ready = True
    if allow_assessed_current_address and not existing_ready:
        selected_address = _extract_current_address(shipping_modal)
        selected_assessment = _assess_address_text(original_address, _format_address_fields(selected_address))
        if _assessment_can_be_used(selected_assessment):
            warnings.append("CRM did not restore the green valid-address text after reselecting the existing address, but the saved address fields still matched safely.")
            existing_ready = True

    fallback_persisted = False
    if existing_ready and not dry_run:
        persisted = _persist_validated_address_via_modal_scope(
            driver,
            shipping_modal,
            use_override=False,
        )
        if not persisted.get("ok"):
            if _address_is_valid(shipping_modal):
                warnings.append(
                    "CRM reported the selected existing address as ready, but the modal service could not pre-persist it before the final Save."
                )
            else:
                raise TimeoutException(
                    "CRM failed to persist the validated address before saving the shipping transaction: "
                    + str(persisted.get("error") or persisted.get("state") or persisted)
                )
        else:
            fallback_persisted = True
            if _address_is_valid(shipping_modal):
                warnings.append("Persisted the selected existing address through the CRM modal service before saving the shipping transaction.")
            else:
                warnings.append("Persisted the existing address through the CRM modal service because the validator controls did not surface a normal valid-address state.")
            if persisted.get("scheduledShipDateAdjusted"):
                warnings.append("Updated the stale scheduled ship date to CRM's current ship-block date so the existing shipping method could be preserved.")

    if not existing_ready:
        return None

    failure_result, final_address, preserved_address_cont = _prepare_shipping_form_for_save(
        order_id,
        shipping_modal,
        original_address,
        preserved_address_cont,
        warnings,
        allow_rewrite=allow_rewrite,
    )
    if failure_result:
        return failure_result

    resolution = _resolution_from_assessment(
        "existing_address",
        "Existing address",
        best_existing.get("assessment"),
        warnings,
    )
    if best_existing["option"].get("preferred_all_caps"):
        warnings.append("Preferred the all-caps existing address option because it appears previously validated.")

    _save_shipping_transaction(
        driver,
        shipping_modal,
        order_id,
        dry_run,
        use_scope_send=fallback_persisted,
    )
    result = _result_for(
        order_id,
        "existing_address_saved" if not dry_run else "existing_address_ready",
        "Used an existing saved address and reached a valid-address state.",
        success=True,
        resolution=resolution,
        manual_review=False,
        warnings=warnings,
        original_address=original_address,
        final_address=final_address,
    )
    selected_existing_text = _normalize_selected_address_text(_existing_address_text_after_name(best_existing["option"]["text"]))
    if selected_existing_text:
        result["selected_existing_address_text"] = selected_existing_text
    selected_existing_option_text = _normalize_selected_address_text(best_existing["option"]["text"])
    if selected_existing_option_text:
        result["selected_existing_option_text"] = selected_existing_option_text
    return result


def _address_is_valid(modal):
    return ADDRESS_VALID_TEXT.lower() in _normalize_space(modal.text).lower()


def _wait_for_address_valid(modal, timeout=8):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _address_is_valid(modal):
            return True
        time.sleep(0.2)
    return False


def _dismiss_business_warning_if_present(driver):
    selectors = [
        (By.XPATH, "//*[self::div or self::section or self::form][.//button[normalize-space()='No' or normalize-space()='no'] and .//button[normalize-space()='Yes' or normalize-space()='yes']]"),
    ]

    def _dismiss_in_current_context():
        dialogs = _visible_elements(driver, selectors)
        if not dialogs:
            return False
        for dialog in dialogs:
            dialog_text = _normalize_space(dialog.text).lower()
            if not any(keyword in dialog_text for keyword in FOLLOWUP_NO_PROMPT_KEYWORDS):
                continue
            for button in _visible_elements(dialog, BUSINESS_NO_BUTTON_SELECTORS):
                _click_with_fallback(driver, button)
                time.sleep(0.3)
                return True
        return False

    deadline = time.time() + 4
    while time.time() < deadline:
        if _dismiss_in_current_context():
            return True
        try:
            driver.switch_to.default_content()
        except Exception:
            pass
        if _dismiss_in_current_context():
            return True
        try:
            frames = driver.find_elements(By.XPATH, "//iframe | //frame")
        except Exception:
            frames = []
        for frame in frames:
            try:
                driver.switch_to.default_content()
                driver.switch_to.frame(frame)
                if _dismiss_in_current_context():
                    return True
            except Exception:
                continue
        time.sleep(0.2)
    return False


def _get_checkbox_near_text(modal, label_text):
    selectors = [
        (By.XPATH, f".//*[contains(normalize-space(.), '{label_text}')]/preceding::input[@type='checkbox'][1]"),
        (By.XPATH, f".//*[contains(normalize-space(.), '{label_text}')]/following::input[@type='checkbox'][1]"),
    ]
    return _wait_for_any(modal, selectors, timeout=4, condition="visible")


def _apply_override(driver, modal):
    checkbox = _get_checkbox_near_text(modal, "Override:")
    if not checkbox.is_selected():
        _click_with_fallback(driver, checkbox)
        time.sleep(0.3)
    _dismiss_business_warning_if_present(driver)


def _switch_to_order_app_frame(driver, timeout=2.0):
    deadline = time.time() + max(0.5, timeout)
    while time.time() < deadline:
        try:
            driver.switch_to.default_content()
        except Exception:
            pass
        try:
            frames = driver.find_elements(
                By.XPATH,
                "//iframe[contains(@src, '/app#/order/') or contains(@src, '/app#')] | //frame[contains(@src, '/app#/order/') or contains(@src, '/app#')]",
            )
        except Exception:
            frames = []
        for frame in frames:
            try:
                driver.switch_to.default_content()
                driver.switch_to.frame(frame)
                body = _body_text(driver).lower()
                if (
                    "shipping info" in body
                    or "payments and credits" in body
                    or "edit order" in body
                    or "shipping transaction" in body
                ):
                    return True
            except Exception:
                continue
        time.sleep(0.2)
    try:
        driver.switch_to.default_content()
    except Exception:
        pass
    return False


def _is_order_detail_page(driver, order_id=None, list_url=None):
    body = _body_text(driver)
    lowered = body.lower()
    current_url = str(getattr(driver, "current_url", "") or "").strip()
    try:
        title = str(driver.title or "")
    except Exception:
        title = ""
    page_text = f"{body}\n{title}".lower()
    if "shipping info" in page_text or "payments and credits" in page_text or "edit order" in page_text:
        return True
    if order_id and order_id in page_text and ("shipping transaction" in page_text or "purchase orders" in page_text or "designstudio order" in page_text):
        return True
    return False


def _wait_for_target_order_open(driver, resolved_order_id, list_url, handles_before, original_handle, timeout_seconds):
    deadline = time.time() + max(0, float(timeout_seconds or 0))
    while time.time() < deadline:
        try:
            handles_now = driver.window_handles
        except Exception:
            handles_now = []
        new_handles = [handle for handle in handles_now if handle not in handles_before]
        if new_handles:
            try:
                driver.switch_to.window(new_handles[-1])
            except Exception:
                pass
        elif original_handle and original_handle in handles_now:
            try:
                driver.switch_to.window(original_handle)
            except Exception:
                pass

        switched_into_app = _switch_to_order_app_frame(driver, timeout=0.8)
        if switched_into_app and _is_order_detail_page(driver, resolved_order_id, list_url=list_url):
            return True
        if switched_into_app and _find_shipping_edit_button(driver, timeout=1.0, raise_on_timeout=False) is not None:
            return True
        if not switched_into_app and _is_order_detail_page(driver, resolved_order_id, list_url=list_url):
            return True
        time.sleep(0.25)
    return False


def _open_target_order(driver, order_id=None, shipping_filter=None, list_url_override=None):
    resolved_order_id = None
    normalized_filter = _normalize_shipping_filter(shipping_filter or CRM_SHIPPING_FILTER_DEFAULT)
    list_url = _shipping_list_url_for_filter(normalized_filter, list_url_override=list_url_override)
    list_label = _shipping_list_label(normalized_filter, list_url_override=list_url_override)

    if order_id:
        resolved_order_id = _normalize_target_order_id(order_id)
        if not resolved_order_id:
            raise RuntimeError("Target order ID must be a 7-digit value or a CRM order URL ending in a 7-digit order ID.")
        target_order_url = f"https://crm2.legacy.printfly.com/order/{resolved_order_id}"
        print(f"Opening order {resolved_order_id} directly...")
        safe_get_with_partial_load(driver, target_order_url, f"CRM order {resolved_order_id}")
        if login_if_needed(driver):
            safe_get_with_partial_load(driver, target_order_url, f"CRM order {resolved_order_id} after login")
        handles_before = set(driver.window_handles)
        try:
            original_handle = driver.current_window_handle
        except Exception:
            original_handle = None
    else:
        print(f"Opening shipping-address CRM report for {list_label}...")
        safe_get_with_partial_load(driver, list_url, f"CRM shipping-address report ({list_label})")
        if login_if_needed(driver):
            safe_get_with_partial_load(driver, list_url, f"CRM shipping-address report after login ({list_label})")

        list_url = str(getattr(driver, "current_url", "") or list_url)
        order_link = None
        resolved_order_id, order_link = _find_first_shipping_list_order(driver, timeout=max(CRM_ACTION_TIMEOUT, 12))
        if not resolved_order_id or order_link is None:
            print("No eligible CRM list row appeared on the first pass; reloading the report once to confirm.")
            safe_get_with_partial_load(driver, list_url, f"CRM shipping-address report recovery reload ({list_label})")
            if login_if_needed(driver):
                safe_get_with_partial_load(driver, list_url, f"CRM shipping-address report recovery after login ({list_label})")
            resolved_order_id, order_link = _find_first_shipping_list_order(driver, timeout=max(CRM_ACTION_TIMEOUT, 12))
            if not resolved_order_id or order_link is None:
                return None

        handles_before = set(driver.window_handles)
        try:
            original_handle = driver.current_window_handle
        except Exception:
            original_handle = None

        print(f"Opening order {resolved_order_id} from the shipping-address report...")
        _click_with_fallback(driver, order_link)

    open_timeout = max(CRM_ACTION_TIMEOUT * 3, 45)
    if _wait_for_target_order_open(driver, resolved_order_id, list_url, handles_before, original_handle, open_timeout):
        return resolved_order_id
    print(f"Order {resolved_order_id} did not open before timeout; refreshing once and retrying.")
    try:
        driver.refresh()
    except Exception:
        pass
    time.sleep(1.0)
    if _wait_for_target_order_open(driver, resolved_order_id, list_url, handles_before, original_handle, open_timeout):
        return resolved_order_id
    raise TimeoutException(f"Order {resolved_order_id} did not open before the timeout expired.")


def _find_shipping_section(driver):
    selectors = [
        (By.ID, "order-shipping"),
        (By.XPATH, "//div[@id='order-shipping' and not(contains(@class, 'ng-hide'))]"),
        (By.XPATH, "//aside[@id='right-column']//div[@id='order-shipping']"),
        (By.XPATH, "//div[contains(@id, 'shipping-transactions')]/ancestor::div[contains(@class, 'panel panel-default')][1]"),
        (By.XPATH, "//strong[contains(normalize-space(.), 'Shipping transaction')]/ancestor::div[contains(@class, 'panel panel-default')][1]"),
    ]
    best = None
    best_score = None
    for by, value in selectors:
        try:
            elements = driver.find_elements(by, value)
        except Exception:
            continue
        for element in elements:
            try:
                if not element.is_displayed():
                    continue
                rect = driver.execute_script(
                    "const r = arguments[0].getBoundingClientRect(); return {top:r.top,left:r.left,width:r.width,height:r.height};",
                    element,
                ) or {}
                section_text = _normalize_space(element.text).lower()
                score = (
                    1 if (element.get_attribute("id") or "") == "order-shipping" else 0,
                    1 if "shipping transaction" in section_text else 0,
                    1 if "shipping info" in section_text else 0,
                    float(rect.get("left") or 0),
                    -float(rect.get("top") or 0),
                )
                if best_score is None or score > best_score:
                    best_score = score
                    best = element
            except Exception:
                continue
    return best


def _ancestor_text(driver, element, depth=6):
    try:
        return _normalize_space(
            driver.execute_script(
                "let el=arguments[0], out=[]; let d=0; while(el && d<arguments[1]){ out.push((el.innerText||'').trim()); el=el.parentElement; d++; } return out.join(' ');",
                element,
                depth,
            ) or ""
        ).lower()
    except Exception:
        return ""


def _find_shipping_edit_button(driver, timeout=None, raise_on_timeout=True):
    timeout = timeout or max(CRM_ACTION_TIMEOUT, 16)
    deadline = time.time() + timeout
    while time.time() < deadline:
        shipping_section = _find_shipping_section(driver)
        if shipping_section is not None:
            try:
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", shipping_section)
                time.sleep(0.25)
            except Exception:
                pass
            scoped_selectors = [
                (By.XPATH, ".//a[contains(@ng-click, 'openShippingTransactionModal') and not(contains(@class, 'ng-hide'))]"),
                (By.XPATH, ".//button[contains(@ng-click, 'openShippingTransactionModal') and not(contains(@class, 'ng-hide'))]"),
                (By.XPATH, ".//div[contains(@id, 'shipping-transactions')]//a[translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')='edit' and not(contains(@class, 'ng-hide'))]"),
                (By.XPATH, ".//a[translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')='edit' and not(contains(@class, 'ng-hide'))]"),
                (By.XPATH, ".//button[translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')='edit' and not(contains(@class, 'ng-hide'))]"),
            ]
            scoped_buttons = _visible_elements(shipping_section, scoped_selectors)
            if scoped_buttons:
                return scoped_buttons[0]

        candidates = []
        fallback_selectors = [
            (By.XPATH, "//a[contains(@ng-click, 'openShippingTransactionModal') and not(contains(@class, 'ng-hide'))]"),
            (By.XPATH, "//button[contains(@ng-click, 'openShippingTransactionModal') and not(contains(@class, 'ng-hide'))]"),
            (By.XPATH, "//a[translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')='edit' and not(contains(@class, 'ng-hide'))]"),
            (By.XPATH, "//button[translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')='edit' and not(contains(@class, 'ng-hide'))]"),
        ]
        for button in _visible_elements(driver, fallback_selectors):
            rect = {}
            try:
                rect = driver.execute_script(
                    "const r = arguments[0].getBoundingClientRect(); return {left:r.left, top:r.top};",
                    button,
                ) or {}
            except Exception:
                pass
            ancestor_text = _ancestor_text(driver, button)
            has_modal_hook = "openshippingtransactionmodal" in ((button.get_attribute('ng-click') or '').lower())
            shipping_context = (
                "shipping transaction" in ancestor_text
                or "shipping info" in ancestor_text
                or "shipping" in ancestor_text
            )
            if not has_modal_hook and not shipping_context:
                continue
            score = (
                2 if has_modal_hook else 0,
                2 if "shipping transaction" in ancestor_text else 0,
                1 if shipping_context else 0,
                float(rect.get("left") or 0),
                -float(rect.get("top") or 0),
            )
            candidates.append((score, button))
        if candidates:
            candidates.sort(reverse=True, key=lambda item: item[0])
            return candidates[0][1]
        try:
            driver.execute_script("window.scrollBy(0, Math.max(520, window.innerHeight * 0.95));")
        except Exception:
            pass
        time.sleep(0.35)
    if raise_on_timeout:
        raise TimeoutException("The Shipping transaction edit button did not become available.")
    return None


def _wait_for_shipping_modal(driver, timeout=None):
    selectors = [
        (By.XPATH, "//*[contains(normalize-space(.), 'Shipping Transaction for')]/ancestor::div[1]"),
        (By.XPATH, "//div[contains(@class, 'modal') and .//*[contains(normalize-space(.), 'Shipping Transaction for')]]"),
    ]

    def _find_modal_in_current_context():
        modals = _visible_elements(driver, selectors)
        if modals:
            return modals[0]
        return None

    deadline = time.time() + max(float(timeout or 0), CRM_ACTION_TIMEOUT if timeout is None else 0.5, 0.5)
    while time.time() < deadline:
        modal = _find_modal_in_current_context()
        if modal is not None:
            return modal

        try:
            driver.switch_to.default_content()
        except Exception:
            pass

        modal = _find_modal_in_current_context()
        if modal is not None:
            return modal

        try:
            frames = driver.find_elements(By.XPATH, "//iframe | //frame")
        except Exception:
            frames = []
        for frame in frames:
            try:
                driver.switch_to.default_content()
                driver.switch_to.frame(frame)
                modal = _find_modal_in_current_context()
                if modal is not None:
                    return modal
            except Exception:
                continue

        time.sleep(0.25)
    raise TimeoutException("The Shipping Transaction modal did not appear before the timeout expired.")


def _open_shipping_editor(driver):
    last_error = None
    for attempt in range(3):
        _switch_to_order_app_frame(driver, timeout=1.5)
        edit_button = _find_shipping_edit_button(driver, timeout=4 if attempt else None)
        print("Opening the shipping transaction editor...")
        _click_with_fallback(driver, edit_button)
        try:
            return _wait_for_shipping_modal(driver, timeout=4 if attempt < 2 else None)
        except TimeoutException as exc:
            last_error = exc
            print(f"Shipping edit click attempt {attempt + 1} did not open the modal yet; retrying...")
            time.sleep(0.5)
    raise last_error or TimeoutException("The Shipping Transaction modal did not appear before the timeout expired.")


def _click_save_verify(driver, shipping_modal):
    button = _wait_for_any(shipping_modal, SAVE_VERIFY_BUTTON_SELECTORS, timeout=8, condition="clickable")
    print("Running Save & Verify Address...")
    _click_with_fallback(driver, button)


def _close_modal_with_generic_button(driver, modal):
    for button in _visible_elements(modal, GENERIC_CLOSE_BUTTON_SELECTORS):
        _click_with_fallback(driver, button)
        time.sleep(0.4)
        return True
    return False


def _wait_for_validation_result(driver, shipping_modal, timeout=8):
    validation_selectors = [
        (By.XPATH, "//*[contains(normalize-space(.), 'Address Validation')]/ancestor::div[1]"),
        (By.XPATH, "//div[contains(@class, 'modal') and .//*[contains(normalize-space(.), 'Address Validation')]]"),
    ]
    error_selectors = [
        (By.XPATH, "//*[contains(normalize-space(.), 'There were some errors:')]/ancestor::div[1]"),
        (By.XPATH, "//*[contains(normalize-space(.), 'Please tell a manager')]/ancestor::div[1]"),
        (By.XPATH, "//*[contains(normalize-space(.), 'No Address Candidates found')]/ancestor::div[1]"),
    ]
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _dismiss_business_warning_if_present(driver):
            continue
        if _address_is_valid(shipping_modal):
            return {"kind": "address_valid"}
        error_modal = _visible_elements(driver, error_selectors)
        if error_modal:
            text = _normalize_space(error_modal[0].text)
            lowered = text.lower()
            if NO_CANDIDATES_TEXT.lower() in lowered:
                return {"kind": "no_candidates", "modal": error_modal[0], "message": text}
            if INVALID_FIELD_TEXT.lower() in lowered:
                return {"kind": "invalid_field", "modal": error_modal[0], "message": text}
            return {"kind": "error", "modal": error_modal[0], "message": text}
        validation_modal = _visible_elements(driver, validation_selectors)
        if validation_modal:
            return {"kind": "validation_popup", "modal": validation_modal[0]}
        time.sleep(0.2)
    raise TimeoutException("The address validator did not return a result before the timeout expired.")


def _extract_validation_candidates(validation_modal):
    candidates = []
    seen = set()
    for radio in validation_modal.find_elements(By.XPATH, ".//input[@type='radio']"):
        try:
            if not radio.is_displayed():
                continue
        except Exception:
            continue
        text = _extract_option_text(radio)
        if not text or text in seen:
            continue
        seen.add(text)
        candidates.append({"input": radio, "text": text, "preferred_all_caps": _is_mostly_all_caps_text(text)})
    return candidates


def _collect_validation_candidates_legacy(driver, validation_modal, max_scrolls=12):
    collected = {}
    container = _find_existing_address_scroll_container(driver, validation_modal)
    try:
        driver.execute_script("arguments[0].scrollTop = 0;", container)
    except Exception:
        pass

    initial_deadline = time.time() + 4.0
    while time.time() < initial_deadline:
        for candidate in _extract_validation_candidates(validation_modal):
            collected[candidate["text"]] = candidate
        if collected:
            break
        time.sleep(0.2)

    time.sleep(0.15)
    for _ in range(max_scrolls + 1):
        for candidate in _extract_validation_candidates(validation_modal):
            collected[candidate["text"]] = candidate
        moved = _scroll_existing_address_container(driver, container)
        if float(moved.get("after", 0)) <= float(moved.get("before", 0)) + 1:
            break
        time.sleep(0.15)

    try:
        driver.execute_script("arguments[0].scrollTop = 0;", container)
    except Exception:
        pass
    return list(collected.values())


def _collect_validation_candidates(driver, validation_modal, max_scrolls=12):
    texts = _collect_radio_option_texts_batched(
        driver,
        validation_modal,
        max_scrolls=max_scrolls,
        require_displayed=True,
        require_existing_format=False,
    )
    if texts is None:
        return _collect_validation_candidates_legacy(driver, validation_modal, max_scrolls=max_scrolls)
    return [{"input": None, "text": text, "preferred_all_caps": _is_mostly_all_caps_text(text)} for text in texts]


def _select_validation_candidate_by_text(driver, validation_modal, target_text, max_scrolls=12):
    target_text = _normalize_space(target_text)
    container = _find_existing_address_scroll_container(driver, validation_modal)
    try:
        driver.execute_script("arguments[0].scrollTop = 0;", container)
    except Exception:
        pass
    time.sleep(0.15)

    def _try_select_in_current_view():
        try:
            selected = driver.execute_script(
                r"""
                const modal = arguments[0];
                const target = String(arguments[1] || '').replace(/\s+/g, ' ').trim();
                function normalize(value){
                  return String(value || '').replace(/\s+/g, ' ').trim();
                }
                function textFor(input){
                  const candidates = [];
                  if (input.labels) candidates.push(...input.labels);
                  let node = input;
                  for (let i = 0; node && i < 4; i += 1){
                    node = node.parentElement;
                    if (node) candidates.push(node);
                  }
                  for (const candidate of candidates){
                    const text = normalize(candidate && candidate.textContent);
                    if (text) return text;
                  }
                  return '';
                }
                const radios = Array.from(modal.querySelectorAll("input[type='radio']"));
                for (const radio of radios){
                  const text = textFor(radio);
                  if (!text) continue;
                  if (normalize(text) !== target) continue;
                  try {
                    radio.scrollIntoView({block: 'center', inline: 'nearest'});
                  } catch (error) {}
                  try {
                    radio.click();
                  } catch (error) {
                    const label = radio.labels && radio.labels.length ? radio.labels[0] : radio.parentElement;
                    if (label && typeof label.click === 'function') {
                      label.click();
                    } else {
                      radio.checked = true;
                    }
                  }
                  radio.dispatchEvent(new Event('input', { bubbles: true }));
                  radio.dispatchEvent(new Event('change', { bubbles: true }));
                  return true;
                }
                return false;
                """,
                validation_modal,
                target_text,
            )
            return bool(selected)
        except Exception:
            return False

    for _ in range(max_scrolls + 1):
        if _try_select_in_current_view():
            time.sleep(0.4)
            return True
        moved = _scroll_existing_address_container(driver, container)
        if float(moved.get("after", 0)) <= float(moved.get("before", 0)) + 1:
            break
        time.sleep(0.15)
    return False


def _pick_validation_candidate(address_fields, candidates):
    best = None
    best_score = None
    saw_zip_plus4_only = False
    for candidate in candidates:
        assessment = _assess_address_text(address_fields, candidate["text"])
        if assessment["zip_plus4_only"]:
            saw_zip_plus4_only = True
        if not _assessment_can_be_used(assessment):
            continue
        score = (
            1 if assessment.get("missing_number_rescue") else 0,
            1 if assessment.get("safe_postal_near_match") else 0,
            1 if assessment.get("safe_postal_prefix_match") else 0,
            1 if assessment.get("secondary_match") else 0,
            1 if candidate.get("preferred_all_caps") else 0,
            1 if assessment["exact_match"] else 0,
            1 if assessment["postal_full_match"] else 0,
            1 if assessment["city_match"] else 0,
            -len(assessment["mismatch_fields"]),
            len(candidate["text"]),
        )
        if best_score is None or score > best_score:
            best_score = score
            best = {"candidate": candidate, "assessment": assessment}
    return best, saw_zip_plus4_only


def _assessed_validation_candidates(address_fields, candidates):
    assessed = []
    for candidate in candidates:
        assessment = _assess_address_text(address_fields, candidate["text"])
        assessed.append({"candidate": candidate, "assessment": assessment})
    return assessed


def _postal_extension_bug_candidates(address_fields, assessed_candidates):
    if not _is_base_only_us_postal(address_fields.get("zip")):
        return []
    bug_candidates = []
    for item in assessed_candidates:
        assessment = item.get("assessment") or {}
        candidate = item.get("candidate") or {}
        if not assessment.get("required_match"):
            continue
        if assessment.get("postal_full_match"):
            continue
        if not _text_has_zip_plus4(candidate.get("text")):
            continue
        bug_candidates.append(item)
    return bug_candidates


def _compact_runon_zip_plus4_override_line(address_fields, assessed_candidates):
    if not _compact_runon_address_signature((address_fields or {}).get("address")):
        return ""

    lines = {}
    for item in assessed_candidates or []:
        candidate = item.get("candidate") or {}
        address_line = _validation_candidate_primary_address_line(address_fields, candidate.get("text"))
        if not address_line:
            return ""
        normalized_line = _normalize_space(address_line)
        if not _looks_like_street_portion(normalized_line):
            return ""
        if not _compact_runon_address_matches((address_fields or {}).get("address"), normalized_line):
            return ""
        lines.setdefault(_canonical_text(normalized_line), normalized_line)

    if len(lines) != 1:
        return ""
    return next(iter(lines.values()))


def _has_zip_plus4_bug(address_fields, assessed_candidates):
    bug_candidates = _postal_extension_bug_candidates(address_fields, assessed_candidates)
    if len(bug_candidates) <= 1:
        return False
    zip_tokens = {
        _normalize_postal(item.get("candidate", {}).get("text"))
        for item in bug_candidates
        if _normalize_postal(item.get("candidate", {}).get("text"))
    }
    return len(zip_tokens) > 1


def _rewrite_address_fields_if_needed(modal, warnings, preserved_address_cont=""):
    current = _extract_current_address(modal)
    address_field = _find_address_form_input(modal, ADDRESS_LINE_INPUT_SELECTORS, "* Address:")
    if address_field is None:
        address_field = _find_address_form_input(modal, ADDRESS_LINE_INPUT_SELECTORS, "Address:")
    address_cont_field = _find_address_form_input(modal, ADDRESS_CONT_INPUT_SELECTORS, "Address (cont):")
    city_field = _find_address_form_input(modal, CITY_INPUT_SELECTORS, "City:")
    normalized_address = _normalize_display_address_line(current.get("address"))
    if normalized_address and normalized_address != _normalize_space(current.get("address")) and address_field is not None:
        _set_input_value(address_field, normalized_address)
        time.sleep(0.2)
        current = _extract_current_address(modal)
        warnings.append("Normalized the spelled-out street number in Address.")

    embedded_street, embedded_po_box = _split_embedded_po_box_indicator(current.get("address"))
    if embedded_street and embedded_po_box and address_field is not None and address_cont_field is not None:
        merged_address_cont = _merge_address_cont_value(embedded_po_box, _effective_address_cont(current))
        _set_input_value(address_field, embedded_street)
        _set_input_value(address_cont_field, merged_address_cont)
        time.sleep(0.2)
        current = _extract_current_address(modal)
        preserved_address_cont = _merge_address_cont_value(preserved_address_cont, embedded_po_box)
        warnings.append(f"Moved '{embedded_po_box}' into Address (cont) and kept '{embedded_street}' in Address.")

    cleaned_address_line, removed_locality_suffix = _clean_address_line_locality_suffix(
        current.get("address"),
        current.get("city"),
        current.get("state"),
        current.get("zip"),
    )
    if removed_locality_suffix and cleaned_address_line != _normalize_space(current.get("address")) and address_field is not None:
        _set_input_value(address_field, cleaned_address_line)
        time.sleep(0.2)
        current = _extract_current_address(modal)
        warnings.append(
            f"Removed duplicated locality text '{removed_locality_suffix}' from Address because City/State/ZIP already populate their own fields."
        )

    recovered_street = _recover_misaligned_street_address(current)
    if recovered_street is not None and address_field is not None and address_cont_field is not None:
        _set_input_value(address_field, recovered_street["address"])
        _set_input_value(address_cont_field, recovered_street["address_cont"])
        time.sleep(0.2)
        current = _extract_current_address(modal)
        if recovered_street["source_field"] == "address":
            warnings.append(
                f"Moved the embedded street address '{recovered_street['address']}' into Address and kept the remaining identifier text in Address (cont)."
            )
        else:
            warnings.append(
                f"Moved '{recovered_street['address']}' from Address (cont) into Address and preserved the venue or identifier text in Address (cont)."
            )
        if recovered_street["removed_locality_suffix"]:
            warnings.append(
                f"Removed duplicated locality text '{recovered_street['removed_locality_suffix']}' from the recovered street address."
            )

    if _address_cont_looks_like_street_fragment(current) and address_field is not None and address_cont_field is not None:
        street_fragment = _effective_address_cont(current)
        merged_address = _normalize_display_address_line(f"{current.get('address')} {street_fragment}")
        _set_input_value(address_field, merged_address)
        _set_input_value(address_cont_field, "")
        time.sleep(0.2)
        current = _extract_current_address(modal)
        preserved_address_cont = ""
        warnings.append(f"Moved '{street_fragment}' from Address (cont) into Address because the main address only contained the house number.")

    original_city = _normalize_space(current.get("city"))
    cleaned_city = _clean_city_field_value(current.get("city"), current.get("state"), current.get("zip"))
    if cleaned_city and cleaned_city != _normalize_space(current.get("city")) and city_field is not None:
        _set_input_value(city_field, cleaned_city)
        time.sleep(0.2)
        current = _extract_current_address(modal)
        warnings.append(f"Cleaned the City field from '{original_city}' to '{cleaned_city}'.")

    effective_address_cont = _effective_address_cont(current)
    if effective_address_cont:
        preserved_address_cont = _merge_address_cont_value(preserved_address_cont, effective_address_cont)
    elif _normalize_space(current.get("address_cont")):
        warnings.append("Ignored duplicate city/state/ZIP text that CRM had placed in Address (cont).")
    cleaned_address, new_address_cont, extracted = _dedupe_address_identifier(current.get("address"), effective_address_cont)
    if not extracted:
        return current, preserved_address_cont
    _set_input_value(address_field, cleaned_address)
    _set_input_value(address_cont_field, new_address_cont)
    time.sleep(0.2)
    warnings.append(f"Moved '{extracted}' into Address (cont).")
    preserved_address_cont = _merge_address_cont_value(preserved_address_cont, extracted)
    return _extract_current_address(modal), preserved_address_cont


def _set_primary_address_line(modal, address_line):
    address_field = _find_address_form_input(modal, ADDRESS_LINE_INPUT_SELECTORS, "* Address:")
    if address_field is None:
        address_field = _find_address_form_input(modal, ADDRESS_LINE_INPUT_SELECTORS, "Address:")
    if address_field is None:
        return _extract_current_address(modal)
    _set_input_value(address_field, address_line)
    time.sleep(0.2)
    return _extract_current_address(modal)


def _looks_clearly_valid_for_override(address_fields):
    if _is_po_box(address_fields.get("address")):
        return False
    if _is_military_address(address_fields):
        return True
    if _is_highway_address(address_fields.get("address")):
        return True
    return bool(_house_token(address_fields.get("address")) and address_fields.get("city") and address_fields.get("state") and address_fields.get("zip"))


def _allow_override_after_no_candidates(address_fields):
    return _is_military_address(address_fields) or _is_highway_address(address_fields.get("address"))


def _close_validation_popup_without_selecting(driver, validation_modal):
    validation_selectors = [
        (By.XPATH, "//*[contains(normalize-space(.), 'Address Validation')]/ancestor::div[1]"),
        (By.XPATH, "//div[contains(@class, 'modal') and .//*[contains(normalize-space(.), 'Address Validation')]]"),
    ]
    close_selectors = list(GENERIC_CLOSE_BUTTON_SELECTORS) + [
        (By.XPATH, ".//button[contains(@class, 'close')]"),
        (By.XPATH, ".//*[self::button or self::a or self::span][normalize-space()='×' or normalize-space()='x']"),
        (By.XPATH, ".//*[self::button or self::a][@aria-label='Close' or @aria-label='close']"),
    ]

    def _wait_until_closed():
        deadline = time.time() + 2.5
        while time.time() < deadline:
            if not _visible_elements(driver, validation_selectors):
                return True
            try:
                if not validation_modal.is_displayed():
                    return True
            except StaleElementReferenceException:
                return True
            except Exception:
                return True
            time.sleep(0.2)
        return not _visible_elements(driver, validation_selectors)

    for root in (validation_modal, driver):
        for button in _visible_elements(root, close_selectors):
            text = _normalize_space(button.text).lower()
            if text and text not in {"cancel", "close", "×", "x"}:
                continue
            _click_with_fallback(driver, button)
            if _wait_until_closed():
                return True

    try:
        body = driver.find_element(By.TAG_NAME, "body")
        body.send_keys(Keys.ESCAPE)
        if _wait_until_closed():
            return True
    except Exception:
        pass

    try:
        closed = driver.execute_script(
            """
            const modal = arguments[0];
            const candidates = modal
              ? Array.from(modal.querySelectorAll('button, a, span, [aria-label]'))
              : [];
            for (const node of candidates) {
              const text = (node.innerText || node.textContent || '').trim().toLowerCase();
              const aria = String(node.getAttribute('aria-label') || '').trim().toLowerCase();
              const cls = String(node.className || '').toLowerCase();
              if (text === 'cancel' || text === 'close' || text === '×' || text === 'x' || aria === 'close' || cls.includes('close')) {
                node.click();
                return true;
              }
            }
            return false;
            """,
            validation_modal,
        )
        if closed and _wait_until_closed():
            return True
    except Exception:
        pass

    return False


def _final_save_ready(driver, shipping_modal, timeout=3):
    deadline = time.time() + timeout
    while time.time() < deadline:
        buttons = _visible_elements(shipping_modal, FINAL_SAVE_BUTTON_SELECTORS)
        buttons.extend(_visible_elements(driver, GLOBAL_FINAL_SAVE_BUTTON_SELECTORS))
        for button in buttons:
            try:
                if button.is_enabled():
                    return True
            except Exception:
                continue
        time.sleep(0.2)
    return False


def _collect_shipping_blocker_warnings(driver, shipping_modal):
    combined_parts = []
    try:
        combined_parts.append(str(shipping_modal.text or ""))
    except Exception:
        pass
    try:
        combined_parts.append(str(_body_text(driver) or ""))
    except Exception:
        pass

    combined_text = _normalize_space(" ".join(combined_parts)).lower()
    warnings = []
    if "address needs to be validated" in combined_text:
        warnings.append("CRM still showed 'Address needs to be validated' after the attempted override.")
    if "no ship / pick-up date set" in combined_text:
        warnings.append("CRM still showed 'No Ship / Pick-Up Date Set' in the shipping editor.")
    if "cannot ship until production is complete" in combined_text:
        warnings.append("CRM still showed 'Cannot ship until production is complete' on the order page.")
    return warnings


def _wait_for_final_save_button(driver, shipping_modal, timeout=8, condition="clickable"):
    last_error = None
    deadline = time.time() + max(0.5, float(timeout or 0))
    while time.time() < deadline:
        remaining = max(0.3, min(1.0, deadline - time.time()))
        try:
            return _wait_for_any(shipping_modal, FINAL_SAVE_BUTTON_SELECTORS, timeout=remaining, condition=condition)
        except Exception as exc:
            last_error = exc
        try:
            return _wait_for_any(driver, GLOBAL_FINAL_SAVE_BUTTON_SELECTORS, timeout=remaining, condition=condition)
        except Exception as exc:
            last_error = exc
        time.sleep(0.15)
    raise TimeoutException(str(last_error) if last_error else "The final Save button did not become available.")


def _shipping_panel_has_valid_address(driver):
    try:
        _switch_to_order_app_frame(driver, timeout=1.0)
    except Exception:
        pass
    try:
        panel = driver.find_element(By.ID, "order-shipping")
        return "valid address" in _normalize_space(panel.text).lower()
    except Exception:
        pass
    try:
        return "valid address" in _body_text(driver).lower()
    except Exception:
        return False



def _extract_shipping_panel_address(driver):
    try:
        _switch_to_order_app_frame(driver, timeout=1.0)
    except Exception:
        pass
    shipping_section = _find_shipping_section(driver)
    if shipping_section is None:
        return {}
    try:
        raw_lines = [line.strip() for line in str(shipping_section.text or "").splitlines()]
    except Exception:
        raw_lines = []
    lines = [_normalize_space(line) for line in raw_lines if _normalize_space(line)]
    if not lines:
        return {}

    stop_markers = (
        "find on google maps",
        "notes:",
        "tracking:",
        "ship method:",
        "ship date:",
        "quantity:",
        "packages:",
        "package weight",
        "valid address",
        "cannot ship until production is complete",
        "edit",
        "duplicate transaction",
    )
    transaction_start = None
    for index, line in enumerate(lines):
        if line.lower().startswith("shipping transaction"):
            transaction_start = index + 1
            break
    if transaction_start is None:
        return {}

    address_lines = []
    for line in lines[transaction_start:]:
        lowered = line.lower()
        if lowered.startswith("shipping transaction"):
            continue
        if not address_lines and ("ship cheapest" in lowered or "ignore due date" in lowered):
            continue
        if any(marker in lowered for marker in stop_markers):
            break
        address_lines.append(line)

    if len(address_lines) < 2:
        return {}

    city = ""
    state = ""
    postal = ""
    city_line = address_lines[-1]
    city_match = re.match(
        r"^(?P<city>.+?),\s*(?P<state>[A-Za-z][A-Za-z .'-]*?)\s*,?\s*(?P<zip>\d{5}(?:-\d{4})?)$",
        city_line,
    )
    if city_match:
        city = _normalize_space(city_match.group("city"))
        state = _normalize_space(city_match.group("state"))
        postal = _normalize_space(city_match.group("zip"))
    else:
        parts = [part.strip() for part in city_line.split(",") if part.strip()]
        if len(parts) >= 3:
            city = _normalize_space(parts[0])
            state = _normalize_space(parts[1])
            postal = _normalize_space(parts[2])

    street_lines = address_lines[1:-1]
    for index, line in enumerate(street_lines):
        if (_looks_like_street_portion(line) and re.match(r"^\s*\d", line)) or _is_po_box(line):
            street_lines = street_lines[index:]
            break
    return {
        "recipient": address_lines[0],
        "address": street_lines[0] if street_lines else "",
        "address_cont": ", ".join(street_lines[1:]) if len(street_lines) > 1 else "",
        "city": city,
        "state": state,
        "zip": postal,
    }


def _order_totals_shipping_value_from_text(text):
    match = ORDER_TOTALS_SHIPPING_VALUE_PATTERN.search(_normalize_space(text))
    if not match:
        return ""
    return _normalize_space(match.group("value"))


def _order_totals_shipping_class_from_value(value):
    value = _normalize_space(value)
    if not value:
        return ""
    if value.lower() == "free":
        return "free"
    amount_text = re.sub(r"[^0-9.]", "", value)
    if not amount_text:
        return ""
    try:
        return "rush" if float(amount_text) > 0 else "free"
    except ValueError:
        return ""


def _order_totals_shipping_class_from_text(text):
    return _order_totals_shipping_class_from_value(_order_totals_shipping_value_from_text(text))


def _read_order_totals_shipping_class(driver):
    def scan_current_context():
        selectors = (
            (By.XPATH, "//*[normalize-space(.)='Order Totals']/ancestor::*[contains(@class, 'panel')][1]"),
            (By.XPATH, "//*[contains(normalize-space(.), 'Order Totals') and contains(normalize-space(.), 'Shipping:')]"),
        )
        for by, value in selectors:
            try:
                elements = driver.find_elements(by, value)
            except Exception:
                elements = []
            for element in elements:
                try:
                    shipping_class = _order_totals_shipping_class_from_text(element.text)
                except Exception:
                    shipping_class = ""
                if shipping_class:
                    return shipping_class
        try:
            return _order_totals_shipping_class_from_text(_body_text(driver))
        except Exception:
            return ""

    shipping_class = scan_current_context()
    if shipping_class:
        return shipping_class
    try:
        driver.switch_to.default_content()
        shipping_class = scan_current_context()
        if shipping_class:
            return shipping_class
    except Exception:
        pass
    try:
        _switch_to_order_app_frame(driver, timeout=1.0)
        return scan_current_context()
    except Exception:
        return ""


def _po_box_shipping_policy_filter(driver, shipping_filter_key, warnings, detected_shipping_class=""):
    shipping_filter_key = _normalize_shipping_filter(shipping_filter_key)
    if shipping_filter_key != "all":
        return shipping_filter_key
    shipping_class = detected_shipping_class or _read_order_totals_shipping_class(driver)
    if shipping_class in {"free", "rush"}:
        warnings.append(
            f"Detected this all-list order as {_shipping_filter_label(shipping_class)} from the Order Totals shipping charge."
        )
        return shipping_class
    warnings.append("Could not determine the Order Totals shipping charge in all-list mode, so PO Box-only handling used rush-order restrictions.")
    return "rush"


def _persist_validated_address_via_modal_scope(driver, shipping_modal, use_override=False, timeout=45):
    return driver.execute_async_script(
        """
        const modalRoot = arguments[0];
        const useOverride = arguments[1];
        const done = arguments[arguments.length - 1];
        function resolveScope(root, predicate) {
            const visitedNodes = new Set();
            const visitedScopes = new Set();
            const nodes = [];
            function push(node) {
                if (node && !visitedNodes.has(node)) {
                    visitedNodes.add(node);
                    nodes.push(node);
                }
            }
            push(root);
            if (root && root.querySelectorAll) {
                root.querySelectorAll('button, input, select, textarea, [ng-click], [ng-model]').forEach(push);
            }
            for (const node of nodes) {
                let current = node;
                let depth = 0;
                while (current && depth < 10) {
                    try {
                        const wrapped = angular.element(current);
                        const scope = (wrapped && ((wrapped.scope && wrapped.scope()) || (wrapped.isolateScope && wrapped.isolateScope()))) || null;
                        if (scope && !visitedScopes.has(scope)) {
                            visitedScopes.add(scope);
                            if (predicate(scope)) {
                                return scope;
                            }
                        }
                    } catch (err) {}
                    current = current.parentElement;
                    depth += 1;
                }
            }
            return null;
        }
        const scope = resolveScope(modalRoot, (scope) => scope && scope.modal && typeof scope.modal.saveAddress === 'function');
        if (!scope || !scope.modal || typeof scope.modal.saveAddress !== 'function') {
            done({ok:false, error:'CRM modal scope is missing saveAddress().'});
            return;
        }
        const priorShipping = {};
        try {
            if (scope.shipping) {
                priorShipping.shippingMethodId = scope.shipping.shippingMethodId;
                priorShipping.shippingMethodDescription = scope.shipping.shippingMethodDescription;
                priorShipping.shippingMethodReturnCode = scope.shipping.shippingMethodReturnCode;
                priorShipping.shippingMethodServiceCode = scope.shipping.shippingMethodServiceCode;
                priorShipping.carrier = scope.shipping.carrier;
            }
        } catch (err) {}
        try {
            scope.address.validationOverride = useOverride ? '1' : (scope.address.validationOverride || '0');
            scope.address.isValidated = '1';
            scope.address.shipTo = scope.shipTo || scope.address.shipTo;
            scope.address.orgname = scope.orgname || scope.address.orgname;
            scope.savingAddress = true;
        } catch (err) {
            done({ok:false, error:String(err)});
            return;
        }
        let finished = false;
        function finish(payload) {
            if (!finished) {
                finished = true;
                done(payload);
            }
        }
        scope.modal.saveAddress(scope.address).then(function(address) {
            try {
                scope.address = address;
                if (
                    scope.order &&
                    scope.order.getResource &&
                    scope.order.getResource().account &&
                    Array.isArray(scope.order.getResource().account.addresses)
                ) {
                    scope.order.getResource().account.addresses.push(address);
                }
                scope.selectAddress(address, scope.forms.addressForm);
                let scheduledShipDateAdjusted = false;
                if (scope.shipping) {
                    const addressId = address && (address.id || address.account_address_id || address.customer_address_id);
                    if (addressId) {
                        scope.shipping.addressId = addressId;
                    }
                    if (address) {
                        scope.shipping.address = [address];
                    }
                    const missingMethod = scope.shipping.shippingMethodId == null || Number(scope.shipping.shippingMethodId) < 0;
                    if (missingMethod && priorShipping.shippingMethodId != null && Number(priorShipping.shippingMethodId) > 0) {
                        scope.shipping.shippingMethodId = priorShipping.shippingMethodId;
                        scope.shipping.shippingMethodDescription = priorShipping.shippingMethodDescription;
                        scope.shipping.shippingMethodReturnCode = priorShipping.shippingMethodReturnCode;
                        scope.shipping.shippingMethodServiceCode = priorShipping.shippingMethodServiceCode;
                        scope.shipping.carrier = priorShipping.carrier;
                    }
                    if (
                        scope.shipBlockDate &&
                        scope.shipping.scheduledShipDate &&
                        String(scope.shipping.scheduledShipDate) < String(scope.shipBlockDate)
                    ) {
                        scope.shipping.scheduledShipDate = scope.shipBlockDate;
                        scheduledShipDateAdjusted = true;
                    }
                }
                if (scope.$applyAsync) {
                    scope.$applyAsync();
                }
                scope.savingAddress = false;
                scope.loadingStateName = false;
                finish({ok:true, address: address, scheduledShipDateAdjusted: scheduledShipDateAdjusted});
            } catch (err) {
                finish({ok:false, error:String(err)});
            }
        }, function(message) {
            scope.loadingStateName = false;
            scope.savingAddress = false;
            finish({ok:false, error:String(message)});
        });
        setTimeout(function() {
            try {
                finish({
                    ok:false,
                    timeout:true,
                    error:'Timed out waiting for CRM modal.saveAddress() to resolve.',
                    state:{
                        validationOverride: scope.address && scope.address.validationOverride,
                        validated: scope.address && scope.address.isValidated,
                        savingAddress: scope.savingAddress
                    }
                });
            } catch (err) {
                finish({ok:false, timeout:true, error:String(err)});
            }
        }, arguments[2]);
        """,
        shipping_modal,
        bool(use_override),
        int(timeout * 1000),
    )


def _ensure_override_ready(driver, shipping_modal, warnings, dry_run=False):
    override_became_valid = _wait_for_address_valid(shipping_modal, timeout=4)
    use_scope_send = False
    if not override_became_valid and not _final_save_ready(driver, shipping_modal, timeout=4):
        if dry_run:
            return False, False
        persisted = _persist_validated_address_via_modal_scope(
            driver,
            shipping_modal,
            use_override=True,
        )
        if not persisted.get("ok"):
            return False, False
        warnings.append("Persisted the override through the CRM modal service because the validator controls did not surface a normal valid-address state.")
        use_scope_send = True
    return True, use_scope_send


def _attempt_validation_candidate_selection(driver, shipping_modal, validation_modal, candidate_text, warnings, dry_run=False):
    print("Selecting the validated address candidate...")
    selected_candidate = _select_validation_candidate_by_text(
        driver,
        validation_modal,
        candidate_text,
    )
    if not selected_candidate:
        raise TimeoutException("The matching validated address could not be selected from the validation popup.")

    select_button = _wait_for_any(
        validation_modal,
        [(By.XPATH, ".//button[normalize-space()='select' or normalize-space()='Select']")],
        timeout=6,
        condition="clickable",
    )
    _click_with_fallback(driver, select_button)

    validated_ready = _wait_for_address_valid(shipping_modal, timeout=6)
    use_scope_send = False
    if not validated_ready and _final_save_ready(driver, shipping_modal, timeout=2.5):
        warnings.append("Validated address did not render the green valid-address text, but the final Save button became available.")
        validated_ready = True
    if validated_ready and not dry_run:
        persisted = _persist_validated_address_via_modal_scope(
            driver,
            shipping_modal,
            use_override=False,
        )
        if not persisted.get("ok"):
            if _address_is_valid(shipping_modal):
                warnings.append(
                    "CRM reported the selected validated address as ready, but the modal service could not pre-persist it before the final Save."
                )
                return validated_ready, use_scope_send
            raise TimeoutException(
                "CRM failed to persist the validated address before saving the shipping transaction: "
                + str(persisted.get("error") or persisted.get("state") or persisted)
            )
        use_scope_send = True
        if _address_is_valid(shipping_modal):
            warnings.append("Persisted the selected validated address through the CRM modal service before saving the shipping transaction.")
        else:
            warnings.append("Persisted the validated address through the CRM modal service because the validator controls did not surface a normal valid-address state.")
        if persisted.get("scheduledShipDateAdjusted"):
            warnings.append("Updated the stale scheduled ship date to CRM's current ship-block date so the existing shipping method could be preserved.")
    return validated_ready, use_scope_send



def _send_shipping_transaction_via_modal_scope(driver, shipping_modal):
    driver.execute_script(
        """
        const modalRoot = arguments[0];
        function resolveScope(root, predicate) {
            const visitedNodes = new Set();
            const visitedScopes = new Set();
            const nodes = [];
            function push(node) {
                if (node && !visitedNodes.has(node)) {
                    visitedNodes.add(node);
                    nodes.push(node);
                }
            }
            push(root);
            if (root && root.querySelectorAll) {
                root.querySelectorAll('button, input, select, textarea, [ng-click], [ng-model]').forEach(push);
            }
            for (const node of nodes) {
                let current = node;
                let depth = 0;
                while (current && depth < 10) {
                    try {
                        const wrapped = angular.element(current);
                        const scope = (wrapped && ((wrapped.scope && wrapped.scope()) || (wrapped.isolateScope && wrapped.isolateScope()))) || null;
                        if (scope && !visitedScopes.has(scope)) {
                            visitedScopes.add(scope);
                            if (predicate(scope)) {
                                return scope;
                            }
                        }
                    } catch (err) {}
                    current = current.parentElement;
                    depth += 1;
                }
            }
            return null;
        }
        const scope = resolveScope(modalRoot, (scope) => scope && typeof scope.send === 'function');
        if (!scope || typeof scope.send !== 'function') {
            throw new Error('CRM modal scope is missing send().');
        }
        scope.send();
        """,
        shipping_modal,
    )



def _save_shipping_transaction(driver, shipping_modal, order_id, dry_run, use_scope_send=False, accept_success_banner=False):
    if dry_run:
        print("Dry run reached a valid-address state; skipping the final Save click.")
        return
    print("Saving the shipping transaction...")
    if use_scope_send:
        _send_shipping_transaction_via_modal_scope(driver, shipping_modal)
    else:
        save_button = _wait_for_final_save_button(driver, shipping_modal, timeout=8, condition="clickable")
        _click_with_fallback(driver, save_button)
    deadline = time.time() + max(CRM_ACTION_TIMEOUT, 25)
    reloaded_after_success_banner = False
    while time.time() < deadline:
        body = _body_text(driver)
        lowered = body.lower()
        if "valid address" in lowered and order_id in body:
            return
        if _shipping_panel_has_valid_address(driver):
            return
        if "shipping transaction added successfully" in lowered:
            if accept_success_banner:
                return
            if reloaded_after_success_banner:
                time.sleep(0.25)
                continue
            try:
                driver.switch_to.default_content()
            except Exception:
                pass
            safe_get_with_partial_load(driver, f"https://crm2.legacy.printfly.com/order/{order_id}", f"CRM order {order_id} reload after shipping save")
            _switch_to_order_app_frame(driver, timeout=2.0)
            reloaded_after_success_banner = True
            if _shipping_panel_has_valid_address(driver):
                return
        time.sleep(0.25)

    try:
        driver.switch_to.default_content()
    except Exception:
        pass
    safe_get_with_partial_load(driver, f"https://crm2.legacy.printfly.com/order/{order_id}", f"CRM order {order_id} final reload after shipping save")
    final_deadline = time.time() + max(CRM_ACTION_TIMEOUT * 3, 45)
    while time.time() < final_deadline:
        try:
            _switch_to_order_app_frame(driver, timeout=1.0)
        except Exception:
            pass
        if _shipping_panel_has_valid_address(driver):
            return
        time.sleep(0.25)
    raise TimeoutException("The order page never showed the green Valid Address confirmation after saving.")


def _evaluate_and_resolve_order(driver, order_id=None, dry_run=False, retry_on_invalid_field=True, shipping_filter=None, list_url_override=None):
    order_id = _open_target_order(driver, order_id, shipping_filter=shipping_filter, list_url_override=list_url_override)
    if not order_id:
        return _result_for(
            None,
            "no_orders_detected",
            f"No {ALLOWED_SHIPPING_LIST_ROW_DESCRIPTION} invalid-address orders were detected in the CRM shipping list.",
            success=True,
            resolution="no_orders",
            manual_review=False,
            warnings=[],
        )
    panel_address = _extract_shipping_panel_address(driver)
    panel_valid_but_needs_caps_normalization = False
    panel_valid_but_needs_split_street_normalization = False
    if _shipping_panel_has_valid_address(driver):
        current_address = panel_address or _extract_shipping_panel_address(driver)
        if _address_fields_contain_email(current_address):
            return _email_in_shipping_address_result(
                order_id,
                ["Detected an email address in a shipping address field that CRM had marked valid."],
                current_address,
                current_address,
            )
        panel_valid_but_needs_caps_normalization = _shipping_address_needs_caps_normalization(current_address)
        panel_valid_but_needs_split_street_normalization = _shipping_address_needs_split_street_normalization(current_address)
        if (
            not panel_valid_but_needs_caps_normalization
            and not panel_valid_but_needs_split_street_normalization
        ):
            return _result_for(
                order_id,
                "already_valid_skipped",
                "Skipped because the order already showed a valid shipping address before opening edit.",
                success=True,
                resolution="already_valid",
                manual_review=False,
                warnings=[],
                original_address=current_address,
                final_address=current_address,
            )
    shipping_filter_key = _normalize_shipping_filter(shipping_filter)
    all_mode_order_totals_shipping_class = ""
    if shipping_filter_key == "all" and _classify_po_box_address(panel_address).get("has_po_box"):
        all_mode_order_totals_shipping_class = _read_order_totals_shipping_class(driver)
    shipping_modal = _open_shipping_editor(driver)
    original_address = _merge_address_fields(_extract_current_address(shipping_modal), panel_address)
    if not _normalize_space(original_address.get("recipient")):
        modal_recipient = _extract_modal_recipient_name(shipping_modal)
        if modal_recipient:
            original_address["recipient"] = modal_recipient
    warnings = []
    po_box_shipping_filter_key = shipping_filter_key
    if panel_valid_but_needs_caps_normalization:
        warnings.append("The order already showed a valid shipping address, but it was not all caps. Running Save & Verify Address to look for a normalized validated match.")
    if panel_valid_but_needs_split_street_normalization:
        warnings.append("The order already showed a valid shipping address, but Address only contained the house number and Address (cont) contained the street name. Opening the editor to merge them.")

    if _address_is_valid(shipping_modal):
        modal_needs_split_street_normalization = _shipping_address_needs_split_street_normalization(original_address)
        if (
            not _shipping_address_needs_caps_normalization(original_address)
            and not modal_needs_split_street_normalization
        ):
            return _result_for(
                order_id,
                "already_valid_modal_skipped",
                "Skipped because the shipping editor already showed Address is valid.",
                success=True,
                resolution="already_valid",
                manual_review=False,
                warnings=warnings,
                original_address=original_address,
                final_address=original_address,
            )
        if _shipping_address_needs_caps_normalization(original_address):
            warnings.append("The shipping editor already showed Address is valid, but the current address was not all caps. Running Save & Verify Address to look for a normalized validated match.")
        if modal_needs_split_street_normalization:
            warnings.append("The shipping editor already showed Address is valid, but Address only contained the house number and Address (cont) contained the street name. Merging the street into Address before saving.")

    recipient_ok, current_address = _ensure_recipient_present(shipping_modal, original_address.get("recipient"), warnings)
    current_address = _merge_address_fields(current_address, panel_address)
    if not recipient_ok:
        return _recipient_missing_result(order_id, warnings, original_address, current_address)
    if _address_fields_contain_email(current_address):
        warnings.append("Detected an email address in a shipping address field before validation.")
        return _email_in_shipping_address_result(order_id, warnings, original_address, current_address)
    original_address = _merge_address_fields(dict(original_address), current_address)
    original_address["recipient"] = current_address.get("recipient") or original_address.get("recipient")
    recovered_street_number_from_cont = ""

    po_box_profile = _classify_po_box_address(current_address)
    if po_box_profile["has_po_box"]:
        po_box_shipping_filter_key = _po_box_shipping_policy_filter(
            driver,
            shipping_filter_key,
            warnings,
            detected_shipping_class=all_mode_order_totals_shipping_class,
        )
    if po_box_profile["needs_swap"]:
        current_address, po_box_profile = _swap_street_and_po_box_lines(shipping_modal, warnings)
        recipient_ok, current_address = _ensure_recipient_present(shipping_modal, original_address.get("recipient"), warnings)
        current_address = _merge_address_fields(current_address, panel_address)
        if not recipient_ok:
            return _recipient_missing_result(order_id, warnings, original_address, current_address)
        po_box_profile = _classify_po_box_address(current_address)
        if po_box_profile["needs_swap"]:
            return _result_for(
                order_id,
                "po_box_street_swap_failed",
                "Skipped because both a PO Box and a street address were present, but the street address could not be moved into the primary Address field.",
                success=False,
                resolution="manual_review",
                manual_review=True,
                warnings=warnings,
                original_address=original_address,
                final_address=current_address,
            )
    elif po_box_profile["street_already_primary"]:
        warnings.append(
            f"Detected both a PO Box and a street address. Using '{po_box_profile['street_line']}' for validation and keeping '{po_box_profile['po_box_line']}' in Address (cont)."
        )

    needs_split_street_normalization = _shipping_address_needs_split_street_normalization(current_address)
    initial_address_cont = _effective_address_cont(current_address)
    if needs_split_street_normalization:
        preserved_address_cont = ""
    else:
        _, preview_preserved_address_cont, _ = _dedupe_address_identifier(
            current_address.get("address"),
            initial_address_cont,
        )
        preserved_address_cont = preview_preserved_address_cont or initial_address_cont

    existing_options = None
    best_existing = None
    had_valid_existing_modal_state = _address_is_valid(shipping_modal)
    if po_box_profile["mixed_po_box_and_street"]:
        warnings.append("Detected both a PO Box and a street address. Skipping saved-address shortcuts so CRM validates the street address layout directly.")
    elif needs_split_street_normalization:
        warnings.append("Skipping saved-address shortcuts until the split street line is merged into Address.")
    else:
        existing_options = _collect_existing_address_options(driver, shipping_modal)
        best_existing = _find_best_existing_address_option(current_address, existing_options)
        if best_existing is not None and _existing_address_looks_like_weak_duplicate(best_existing):
            save_verify_button = _find_visible_element(shipping_modal, SAVE_VERIFY_BUTTON_SELECTORS)
            existing_result = _try_resolve_with_existing_address(
                driver,
                shipping_modal,
                order_id,
                dry_run,
                original_address,
                current_address,
                preserved_address_cont,
                warnings,
                existing_options=existing_options,
                best_existing=best_existing,
                accept_save_button_ready=False,
                allow_assessed_current_address=True,
            )
            if existing_result is not None:
                warnings.append("Used a matching saved address that CRM already marked as valid.")
                return existing_result
            if had_valid_existing_modal_state and save_verify_button is None:
                warnings.append("CRM kept the saved address in a valid state and did not show Save & Verify Address, so the matching existing address was used directly.")
                existing_result = _try_resolve_with_existing_address(
                    driver,
                    shipping_modal,
                    order_id,
                    dry_run,
                    original_address,
                    current_address,
                    preserved_address_cont,
                    warnings,
                    existing_options=existing_options,
                    best_existing=best_existing,
                )
                if existing_result is not None:
                    return existing_result
            warnings.append("Found a matching saved address, but it looks like an unnormalized duplicate. Running Save & Verify Address first.")
        else:
            existing_result = _try_resolve_with_existing_address(
                driver,
                shipping_modal,
                order_id,
                dry_run,
                original_address,
                current_address,
                preserved_address_cont,
                warnings,
                existing_options=existing_options,
                best_existing=best_existing,
            )
            if existing_result is not None:
                return existing_result

    current_address, preserved_address_cont = _rewrite_address_fields_if_needed(shipping_modal, warnings, preserved_address_cont)
    recipient_ok, current_address = _ensure_recipient_present(shipping_modal, original_address.get("recipient"), warnings)
    current_address = _merge_address_fields(current_address, panel_address)
    if not recipient_ok:
        return _recipient_missing_result(order_id, warnings, original_address, current_address)
    if _address_fields_contain_email(current_address):
        warnings.append("Detected an email address in a shipping address field after cleanup.")
        return _email_in_shipping_address_result(order_id, warnings, original_address, current_address)
    po_box_profile = _classify_po_box_address(current_address)

    if not any(_normalize_space(current_address.get(key)) for key in ("address", "city", "zip", "state")):
        return _result_for(
            order_id,
            "address_fields_unavailable",
            "Skipped because the shipping address fields did not populate in CRM before validation started.",
            success=False,
            resolution="manual_review",
            manual_review=True,
            warnings=warnings,
            original_address=original_address,
            final_address=current_address,
        )

    if not po_box_profile["mixed_po_box_and_street"]:
        existing_options_after_rewrite = _collect_existing_address_options(driver, shipping_modal, max_scrolls=6)
        best_existing_after_rewrite = _find_best_existing_address_option(current_address, existing_options_after_rewrite)
        if best_existing_after_rewrite is not None and not _existing_address_looks_like_weak_duplicate(best_existing_after_rewrite):
            existing_result = _try_resolve_with_existing_address(
                driver,
                shipping_modal,
                order_id,
                dry_run,
                original_address,
                current_address,
                preserved_address_cont,
                warnings,
                max_scrolls=6,
                existing_options=existing_options_after_rewrite,
                best_existing=best_existing_after_rewrite,
            )
            if existing_result is not None:
                warnings.append("Used a matching saved address after rewriting the shipping fields into a cleaner format.")
                return existing_result

    if (
        _is_missing_street_number(current_address.get("address"))
        and not po_box_profile["po_box_only"]
        and not _is_military_address(current_address)
    ):
        current_address, recovered_street_number_from_cont = _move_address_cont_number_to_primary_address(
            shipping_modal,
            warnings,
            current_address,
        )
        current_address = _merge_address_fields(current_address, panel_address)
        if recovered_street_number_from_cont:
            current_address["address_cont"] = ""
            preserved_address_cont = ""
            po_box_profile = _classify_po_box_address(current_address)

    if (
        _is_missing_street_number(current_address.get("address"))
        and not po_box_profile["po_box_only"]
        and not _is_military_address(current_address)
    ):
        return _result_for(
            order_id,
            "missing_street_number",
            "Skipped because the shipping address is missing the street number.",
            success=False,
            resolution="manual_review",
            manual_review=True,
            warnings=warnings,
            original_address=original_address,
            final_address=current_address,
        )

    if _is_missing_street_name(current_address.get("address")) and not po_box_profile["po_box_only"]:
        return _result_for(
            order_id,
            "missing_street_name",
            "Skipped because the shipping address is missing the street name and only shows the address number.",
            success=False,
            resolution="manual_review",
            manual_review=True,
            warnings=warnings,
            original_address=original_address,
            final_address=current_address,
        )

    if po_box_profile["po_box_only"]:
        if po_box_shipping_filter_key == "rush":
            warnings.append("Rush orders cannot ship to a PO Box without a separate street address.")
            return _result_for(
                order_id,
                "po_box_rush_skipped",
                "Skipped because this rush order only has a PO Box and no separate street shipping address.",
                success=False,
                resolution="manual_review",
                manual_review=True,
                warnings=warnings,
                original_address=original_address,
                final_address=current_address,
            )
        existing_result = _try_resolve_with_existing_address(
            driver,
            shipping_modal,
            order_id,
            dry_run,
            original_address,
            current_address,
            preserved_address_cont,
            warnings,
            max_scrolls=6,
        )
        if existing_result is not None:
            warnings.append("Used a matching existing saved address instead of overriding the PO Box-only order.")
            return existing_result
        print("Detected a free-ship PO Box with no separate street address. Using override flow...")
        _apply_override(driver, shipping_modal)
        override_ready, use_scope_send = _ensure_override_ready(
            driver,
            shipping_modal,
            warnings,
            dry_run=dry_run,
        )
        if not override_ready:
            warnings.extend(_collect_shipping_blocker_warnings(driver, shipping_modal))
            return _result_for(
                order_id,
                "po_box_free_override_manual_review",
                "Skipped because the free-ship PO Box override did not produce a valid address state automatically.",
                success=False,
                resolution="manual_review",
                manual_review=True,
                warnings=warnings,
                original_address=original_address,
                final_address=_extract_current_address(shipping_modal),
            )
        failure_result, final_address, preserved_address_cont = _prepare_shipping_form_for_save(order_id, shipping_modal, original_address, preserved_address_cont, warnings)
        if failure_result:
            return failure_result
        if not _address_is_valid(shipping_modal):
            if use_scope_send:
                warnings.append("Override did not render the green valid-address text, but it was persisted through the CRM modal service.")
            else:
                warnings.append("Override did not render the green valid-address text, but the final Save button became available.")
        warnings.append("Free-ship PO Box was overridden because no separate street address was provided.")
        _save_shipping_transaction(
            driver,
            shipping_modal,
            order_id,
            dry_run,
            use_scope_send=use_scope_send,
        )
        return _result_for(
            order_id,
            "po_box_free_override_saved" if not dry_run else "po_box_free_override_ready",
            "Free-ship order only had a PO Box, so the address was overridden and saved.",
            success=True,
            resolution="override",
            manual_review=False,
            warnings=warnings,
            original_address=original_address,
            final_address=final_address,
        )

    if _is_po_box(current_address.get("address")):
        return _result_for(
            order_id,
            "po_box_skipped",
            "Skipped because the shipping address is a PO Box and should not be processed.",
            success=False,
            resolution="manual_review",
            manual_review=True,
            warnings=warnings,
            original_address=original_address,
            final_address=current_address,
        )

    if _is_military_address(current_address):
        existing_result = _try_resolve_with_existing_address(
            driver,
            shipping_modal,
            order_id,
            dry_run,
            original_address,
            current_address,
            preserved_address_cont,
            warnings,
            max_scrolls=6,
        )
        if existing_result is not None:
            warnings.append("Used a matching existing saved address instead of overriding the military/APO address.")
            return existing_result
        print("Detected a military/APO-style address. Using override flow...")
        _apply_override(driver, shipping_modal)
        override_ready, use_scope_send = _ensure_override_ready(
            driver,
            shipping_modal,
            warnings,
            dry_run=dry_run,
        )
        if not override_ready:
            warnings.extend(_collect_shipping_blocker_warnings(driver, shipping_modal))
            return _result_for(
                order_id,
                "apo_override_manual_review",
                "Skipped because the military/APO override did not produce a valid address state automatically.",
                success=False,
                resolution="manual_review",
                manual_review=True,
                warnings=warnings,
                original_address=original_address,
                final_address=_extract_current_address(shipping_modal),
            )
        failure_result, final_address, preserved_address_cont = _prepare_shipping_form_for_save(order_id, shipping_modal, original_address, preserved_address_cont, warnings)
        if failure_result:
            return failure_result
        if not _address_is_valid(shipping_modal):
            if use_scope_send:
                warnings.append("Override did not render the green valid-address text, but it was persisted through the CRM modal service.")
            else:
                warnings.append("Override did not render the green valid-address text, but the final Save button became available.")
        warnings.append("Military/APO override was used.")
        _save_shipping_transaction(
            driver,
            shipping_modal,
            order_id,
            dry_run,
            use_scope_send=use_scope_send,
            accept_success_banner=True,
        )
        return _result_for(
            order_id,
            "apo_override_saved" if not dry_run else "apo_override_ready",
            "Military/APO address was overridden and reached a valid-address state.",
            success=True,
            resolution="apo_override",
            manual_review=False,
            warnings=warnings,
            original_address=original_address,
            final_address=final_address,
        )

    recipient_ok, current_address = _ensure_recipient_present(shipping_modal, original_address.get("recipient"), warnings)
    if not recipient_ok:
        return _recipient_missing_result(order_id, warnings, original_address, current_address)
    save_verify_button = _find_visible_element(shipping_modal, SAVE_VERIFY_BUTTON_SELECTORS)
    if _address_is_valid(shipping_modal) and save_verify_button is None:
        failure_result, final_address, preserved_address_cont = _prepare_shipping_form_for_save(
            order_id,
            shipping_modal,
            original_address,
            preserved_address_cont,
            warnings,
        )
        if failure_result:
            return failure_result
        warnings.append("Shipping form reached a valid-address state before Save & Verify Address was needed.")
        _save_shipping_transaction(driver, shipping_modal, order_id, dry_run)
        return _result_for(
            order_id,
            "already_valid" if not dry_run else "already_valid_ready",
            "The shipping form reached a valid-address state without needing an additional validation step.",
            success=True,
            resolution="validated",
            manual_review=False,
            warnings=warnings,
            original_address=original_address,
            final_address=final_address,
        )
    _click_save_verify(driver, shipping_modal)
    validation = _wait_for_validation_result(driver, shipping_modal, timeout=10)

    if validation["kind"] == "invalid_field":
        warning_message = _normalize_space(validation.get("message"))
        lowered_warning = warning_message.lower()
        _close_modal_with_generic_button(driver, validation["modal"])
        time.sleep(0.4)
        try:
            follow_up_validation = _wait_for_validation_result(driver, shipping_modal, timeout=4)
        except TimeoutException:
            follow_up_validation = None
        if follow_up_validation is not None and follow_up_validation.get("kind") != "invalid_field":
            validation = follow_up_validation
        else:
            current_after_warning = _extract_current_address(shipping_modal)
            recipient_ok, current_after_warning = _ensure_recipient_present(shipping_modal, original_address.get("recipient"), warnings)
            current_after_warning = _merge_address_fields(current_after_warning, panel_address)
            current_warning_assessment = _assess_address_text(original_address, _format_address_fields(current_after_warning))
            existing_options_after_warning = None
            safe_existing_after_warning = None
            try:
                existing_options_after_warning = _collect_existing_address_options(driver, shipping_modal, max_scrolls=4)
                safe_existing_after_warning = _pick_existing_address_option(current_after_warning, existing_options_after_warning)
            except Exception:
                safe_existing_after_warning = None
            ignorable_name_warning = ("to_address" in lowered_warning and "field" in lowered_warning and "name" in lowered_warning)
            if dry_run and ignorable_name_warning and recipient_ok and safe_existing_after_warning is not None:
                failure_result, final_address, preserved_address_cont = _prepare_shipping_form_for_save(order_id, shipping_modal, original_address, preserved_address_cont, warnings)
                if failure_result:
                    return failure_result
                warnings.append("Closed the ignorable Veeqo name warning during dry run because the recipient was restored and the saved address still matched safely.")
                return _result_for(
                    order_id,
                    "existing_address_ready_warning_ignored",
                    "Ignored the Veeqo manager warning during dry run because the recipient was present and the saved address still matched safely.",
                    success=True,
                    resolution="existing_address",
                    manual_review=False,
                    warnings=warnings,
                    original_address=original_address,
                    final_address=final_address,
                )
            if recipient_ok and safe_existing_after_warning is not None:
                existing_result = _try_resolve_with_existing_address(
                    driver,
                    shipping_modal,
                    order_id,
                    dry_run,
                    original_address,
                    current_after_warning,
                    preserved_address_cont,
                    warnings,
                    max_scrolls=4,
                    existing_options=existing_options_after_warning,
                    best_existing=safe_existing_after_warning,
                    accept_save_button_ready=False,
                    allow_prevalidated_selection=True,
                )
                if existing_result is not None:
                    warnings.append("Manager warning prevented the validation popup from completing, so a matching saved address was used instead.")
                    return existing_result
            current_warning_ready = _address_is_valid(shipping_modal)
            use_scope_send = False
            if not current_warning_ready and _assessment_can_be_used(current_warning_assessment):
                current_warning_ready = _final_save_ready(driver, shipping_modal, timeout=2.5)
                if current_warning_ready:
                    warnings.append("Closed the manager warning and CRM still allowed the shipping transaction to be saved.")
            if recipient_ok and _assessment_can_be_used(current_warning_assessment) and current_warning_ready:
                if not dry_run and not _address_is_valid(shipping_modal):
                    persisted = _persist_validated_address_via_modal_scope(
                        driver,
                        shipping_modal,
                        use_override=False,
                    )
                    if not persisted.get("ok"):
                        raise TimeoutException(
                            "CRM failed to persist the validated address before saving the shipping transaction: "
                            + str(persisted.get("error") or persisted.get("state") or persisted)
                        )
                    use_scope_send = True
                    warnings.append("Persisted the current shipping address through the CRM modal service after the manager warning blocked the normal valid-address state.")
                failure_result, final_address, preserved_address_cont = _prepare_shipping_form_for_save(
                    order_id,
                    shipping_modal,
                    original_address,
                    preserved_address_cont,
                    warnings,
                )
                if failure_result:
                    return failure_result
                warnings.append("Ignored the CRM manager warning because the current shipping address still matched safely.")
                resolution = _resolution_from_assessment(
                    "validated_address",
                    "Validated address",
                    current_warning_assessment,
                    warnings,
                )
                _save_shipping_transaction(
                    driver,
                    shipping_modal,
                    order_id,
                    dry_run,
                    use_scope_send=use_scope_send,
                )
                return _result_for(
                    order_id,
                    "manager_warning_ignored_saved" if not dry_run else "manager_warning_ignored_ready",
                    "Ignored the CRM manager warning because the current shipping address still matched safely and the form was ready to save.",
                    success=True,
                    resolution=resolution,
                    manual_review=False,
                    warnings=warnings,
                    original_address=original_address,
                    final_address=final_address,
                )
            if retry_on_invalid_field:
                print("Address validation returned the manager warning. Closing it, refreshing, and retrying once...")
                safe_get_with_partial_load(driver, driver.current_url, f"Order {order_id} refresh after invalid-field warning")
                result = _evaluate_and_resolve_order(driver, order_id, dry_run=dry_run, retry_on_invalid_field=False)
                result["retry_attempted"] = True
                return result
            return _result_for(
                order_id,
                "invalid_field_manager_warning",
                "Address validation reported an invalid field / tell a manager warning even after one retry.",
                success=False,
                resolution="manual_review",
                manual_review=True,
                warnings=warnings,
                original_address=original_address,
                final_address=current_after_warning,
                retry_attempted=True,
            )

    if validation["kind"] == "no_candidates":
        _close_modal_with_generic_button(driver, validation["modal"])
        final_address = _merge_address_fields(_extract_current_address(shipping_modal), panel_address)
        warnings.append("Validator reported no address candidates.")
        if recovered_street_number_from_cont:
            final_address["address_cont"] = ""
            return _result_for(
                order_id,
                "missing_street_number",
                "Skipped because the shipping address was missing the street number; moving the number from Address (cont) into Address produced no validator candidates.",
                success=False,
                resolution="manual_review",
                manual_review=True,
                warnings=warnings,
                original_address=original_address,
                final_address=final_address,
            )
        if had_valid_existing_modal_state and best_existing is not None:
            existing_result = _try_resolve_with_existing_address(
                driver,
                shipping_modal,
                order_id,
                dry_run,
                original_address,
                final_address,
                preserved_address_cont,
                warnings,
                max_scrolls=6,
                existing_options=existing_options,
                best_existing=best_existing,
                accept_save_button_ready=False,
                allow_prevalidated_selection=True,
                allow_rewrite=needs_split_street_normalization,
            )
            if existing_result is not None:
                warnings.append("Used a previously valid matching saved address after the validator returned no candidates.")
                return existing_result
        existing_result = _try_resolve_with_existing_address(
            driver,
            shipping_modal,
            order_id,
            dry_run,
            original_address,
            final_address,
            preserved_address_cont,
            warnings,
            max_scrolls=6,
            accept_save_button_ready=False,
            allow_assessed_current_address=True,
            allow_rewrite=needs_split_street_normalization,
        )
        if existing_result is not None:
            warnings.append("Used a matching existing saved address after the validator returned no candidates.")
            return existing_result
        if _allow_override_after_no_candidates(final_address):
            print("No candidates were found, but this address qualifies for last-resort override handling.")
            _apply_override(driver, shipping_modal)
            override_ready, use_scope_send = _ensure_override_ready(
                driver,
                shipping_modal,
                warnings,
                dry_run=dry_run,
            )
            if not override_ready:
                warnings.extend(_collect_shipping_blocker_warnings(driver, shipping_modal))
                return _result_for(
                    order_id,
                    "override_after_no_candidates_manual_review",
                    "Skipped because the no-candidates override did not produce a valid address state automatically.",
                    success=False,
                    resolution="manual_review",
                    manual_review=True,
                    warnings=warnings,
                    original_address=original_address,
                    final_address=_extract_current_address(shipping_modal),
                )
            failure_result, final_address, preserved_address_cont = _prepare_shipping_form_for_save(order_id, shipping_modal, original_address, preserved_address_cont, warnings)
            if failure_result:
                return failure_result
            if not _address_is_valid(shipping_modal):
                warnings.append("Override did not render the green valid-address text, but the final Save button became available.")
            warnings.append("Override was used after a no-candidates validation result.")
            _save_shipping_transaction(
                driver,
                shipping_modal,
                order_id,
                dry_run,
                use_scope_send=use_scope_send,
            )
            return _result_for(
                order_id,
                "override_after_no_candidates" if not dry_run else "override_after_no_candidates_ready",
                "Validator found no candidates, so the address was overridden based on the matching original address fields.",
                success=True,
                resolution="override",
                manual_review=False,
                warnings=warnings,
                original_address=original_address,
                final_address=final_address,
            )
        return _result_for(
            order_id,
            "no_candidates_manual_review",
            "Skipped because the validator found no address candidates and the address was not safe to override automatically.",
            success=False,
            resolution="manual_review",
            manual_review=True,
            warnings=warnings,
            original_address=original_address,
            final_address=final_address,
        )

    if validation["kind"] == "validation_popup":
        validation_modal = validation["modal"]
        candidates = _collect_validation_candidates(driver, validation_modal)
        if not candidates:
            return _result_for(
                order_id,
                "validation_popup_empty",
                "Address validation popup appeared, but no selectable candidates were visible.",
                success=False,
                resolution="manual_review",
                manual_review=True,
                warnings=warnings,
                original_address=original_address,
                final_address=_extract_current_address(shipping_modal),
            )
        assessed_candidates = _assessed_validation_candidates(current_address, candidates)
        best_candidate, saw_zip_plus4_only = _pick_validation_candidate(current_address, candidates)
        postal_extension_bug_candidates = _postal_extension_bug_candidates(current_address, assessed_candidates)
        compact_runon_zip_plus4_override_line = _compact_runon_zip_plus4_override_line(
            current_address,
            postal_extension_bug_candidates,
        )
        postal_extension_override_allowed = (
            best_candidate is not None
            and _is_base_only_us_postal(current_address.get("zip"))
            and best_candidate["assessment"].get("required_match")
            and not best_candidate["assessment"].get("postal_full_match")
            and len(postal_extension_bug_candidates) == 0
        )
        if _has_zip_plus4_bug(current_address, assessed_candidates):
            if compact_runon_zip_plus4_override_line:
                if not _close_validation_popup_without_selecting(driver, validation_modal):
                    _close_modal_with_generic_button(driver, validation_modal)
                time.sleep(0.4)
                _set_primary_address_line(shipping_modal, compact_runon_zip_plus4_override_line)
                warnings.append(
                    f"Normalized the compact street '{current_address.get('address')}' to '{compact_runon_zip_plus4_override_line}' using the validated candidate."
                )
                warnings.append("Validator returned multiple ZIP+4 variants, but they shared the same street line, so the original 5-digit ZIP was preserved with override.")
                _apply_override(driver, shipping_modal)
                override_became_valid = _wait_for_address_valid(shipping_modal, timeout=4)
                if not override_became_valid and not _final_save_ready(driver, shipping_modal, timeout=4):
                    warnings.extend(_collect_shipping_blocker_warnings(driver, shipping_modal))
                    return _result_for(
                        order_id,
                        "compact_runon_zip_plus4_override_manual_review",
                        "Skipped because the compact-street ZIP+4 override did not produce a valid address state automatically.",
                        success=False,
                        resolution="manual_review",
                        manual_review=True,
                        warnings=warnings,
                        original_address=original_address,
                        final_address=_extract_current_address(shipping_modal),
                    )
                failure_result, final_address, preserved_address_cont = _prepare_shipping_form_for_save(order_id, shipping_modal, original_address, preserved_address_cont, warnings)
                if failure_result:
                    return failure_result
                if not override_became_valid:
                    warnings.append("Override did not render the green valid-address text, but the final Save button became available.")
                _save_shipping_transaction(driver, shipping_modal, order_id, dry_run)
                return _result_for(
                    order_id,
                    "override_after_compact_runon_zip_plus4_popup" if not dry_run else "override_after_compact_runon_zip_plus4_popup_ready",
                    "Validator returned multiple ZIP+4 variants, but they agreed on the normalized street line, so the spacing was fixed and the original ZIP was preserved.",
                    success=True,
                    resolution="override",
                    manual_review=False,
                    warnings=warnings,
                    original_address=original_address,
                    final_address=final_address,
                )
            top_zip_plus4_candidate = postal_extension_bug_candidates[0] if postal_extension_bug_candidates else None
            if top_zip_plus4_candidate is not None:
                top_candidate_text = top_zip_plus4_candidate["candidate"].get("text")
                try:
                    validated_ready, use_scope_send = _attempt_validation_candidate_selection(
                        driver,
                        shipping_modal,
                        validation_modal,
                        top_candidate_text,
                        warnings,
                        dry_run=dry_run,
                    )
                except TimeoutException as exc:
                    validated_ready = False
                    use_scope_send = False
                    warnings.append(
                        "Tried the top ZIP+4 candidate from the validation popup, but CRM did not complete validation automatically."
                    )
                    warnings.append(str(exc))
                if validated_ready:
                    failure_result, final_address, preserved_address_cont = _prepare_shipping_form_for_save(
                        order_id,
                        shipping_modal,
                        original_address,
                        preserved_address_cont,
                        warnings,
                    )
                    if failure_result:
                        return failure_result
                    resolution = _resolution_from_assessment(
                        "validated_address",
                        "Validated address",
                        top_zip_plus4_candidate.get("assessment"),
                        warnings,
                    )
                    warnings.append("Selected the top ZIP+4 candidate because CRM accepted it as a valid address despite multiple ZIP+4 variants.")
                    if resolution == "validated_address":
                        resolution = "validated_address_zip_extension"
                    _save_shipping_transaction(
                        driver,
                        shipping_modal,
                        order_id,
                        dry_run,
                        use_scope_send=use_scope_send,
                    )
                    result = _result_for(
                        order_id,
                        "validated_zip_plus4_bug_top_candidate_saved" if not dry_run else "validated_zip_plus4_bug_top_candidate_ready",
                        "Selected the top ZIP+4 candidate from the validation popup and CRM accepted it.",
                        success=True,
                        resolution=resolution,
                        manual_review=False,
                        warnings=warnings,
                        original_address=original_address,
                        final_address=final_address,
                    )
                    selected_validated_text = _normalize_selected_address_text(top_candidate_text)
                    if selected_validated_text:
                        result["selected_validated_address_text"] = selected_validated_text
                    return result
            _close_validation_popup_without_selecting(driver, validation_modal)
            warnings.append("Validator returned multiple ZIP+4 variants for the same base address, so the order was left for manual review.")
            warnings.extend([candidate["text"] for candidate in candidates[:3]])
            return _result_for(
                order_id,
                "validated_zip_plus4_bug_manual_review",
                "Skipped because the validator returned multiple ZIP+4 variants and the correct normalized address could not be chosen safely.",
                success=False,
                resolution="manual_review",
                manual_review=True,
                warnings=warnings,
                original_address=original_address,
                final_address=_extract_current_address(shipping_modal),
            )
        if (
            len(postal_extension_bug_candidates) == 1
            and best_candidate is not None
            and postal_extension_bug_candidates[0]["candidate"].get("text") == best_candidate["candidate"].get("text")
        ):
            warnings.append("Selected the only validated ZIP+4 candidate because it was the sole safe normalized address returned by CRM.")
            postal_extension_override_allowed = False
        if postal_extension_override_allowed:
            if not _close_validation_popup_without_selecting(driver, validation_modal):
                _close_modal_with_generic_button(driver, validation_modal)
            time.sleep(0.4)
            warnings.append("Validator suggested a matching address that would extend the customer ZIP beyond the original 5 digits. Preserving the customer ZIP and using override.")
            _apply_override(driver, shipping_modal)
            override_became_valid = _wait_for_address_valid(shipping_modal, timeout=4)
            if not override_became_valid and not _final_save_ready(driver, shipping_modal, timeout=4):
                warnings.extend(_collect_shipping_blocker_warnings(driver, shipping_modal))
                return _result_for(
                    order_id,
                    "postal_extension_override_manual_review",
                    "Skipped because the postal-extension override did not produce a valid address state automatically.",
                    success=False,
                    resolution="manual_review",
                    manual_review=True,
                    warnings=warnings,
                    original_address=original_address,
                    final_address=_extract_current_address(shipping_modal),
                )
            failure_result, final_address, preserved_address_cont = _prepare_shipping_form_for_save(order_id, shipping_modal, original_address, preserved_address_cont, warnings)
            if failure_result:
                return failure_result
            if not override_became_valid:
                warnings.append("Override did not render the green valid-address text, but the final Save button became available.")
            _save_shipping_transaction(driver, shipping_modal, order_id, dry_run)
            return _result_for(
                order_id,
                "override_after_postal_extension_popup" if not dry_run else "override_after_postal_extension_popup_ready",
                "Validator only suggested a postal-extension variant, so the original address was overridden and saved.",
                success=True,
                resolution="override",
                manual_review=False,
                warnings=warnings,
                original_address=original_address,
                final_address=final_address,
            )
        if best_candidate is None:
            _close_validation_popup_without_selecting(driver, validation_modal)
            if saw_zip_plus4_only:
                warnings.append("Validator suggested a ZIP+4 variant, but no candidate met the full matching rules.")
            warnings.extend([candidate["text"] for candidate in candidates[:3]])
            current_po_box_profile = _classify_po_box_address(current_address)
            if current_po_box_profile.get("mixed_po_box_and_street") and _looks_clearly_valid_for_override(current_address):
                warnings.append("Validator candidates changed the city or postal code for a mixed street/box address, so the original complete address was preserved with override.")
                _apply_override(driver, shipping_modal)
                override_ready, use_scope_send = _ensure_override_ready(
                    driver,
                    shipping_modal,
                    warnings,
                    dry_run=dry_run,
                )
                if not override_ready:
                    warnings.extend(_collect_shipping_blocker_warnings(driver, shipping_modal))
                    return _result_for(
                        order_id,
                        "mixed_box_candidate_mismatch_override_manual_review",
                        "Skipped because the mixed street/box override did not produce a valid address state automatically.",
                        success=False,
                        resolution="manual_review",
                        manual_review=True,
                        warnings=warnings,
                        original_address=original_address,
                        final_address=_extract_current_address(shipping_modal),
                    )
                failure_result, final_address, preserved_address_cont = _prepare_shipping_form_for_save(order_id, shipping_modal, original_address, preserved_address_cont, warnings)
                if failure_result:
                    return failure_result
                if not _address_is_valid(shipping_modal):
                    warnings.append("Override did not render the green valid-address text, but the final Save button became available.")
                _save_shipping_transaction(
                    driver,
                    shipping_modal,
                    order_id,
                    dry_run,
                    use_scope_send=use_scope_send,
                )
                return _result_for(
                    order_id,
                    "mixed_box_candidate_mismatch_override_saved" if not dry_run else "mixed_box_candidate_mismatch_override_ready",
                    "Validator candidates did not match the mixed street/box address, so the original address was overridden and saved.",
                    success=True,
                    resolution="override",
                    manual_review=False,
                    warnings=warnings,
                    original_address=original_address,
                    final_address=final_address,
                )
            return _result_for(
                order_id,
                "validated_candidate_mismatch",
                "Skipped because the suggested validated address did not match the original shipping address closely enough.",
                success=False,
                resolution="manual_review",
                manual_review=True,
                warnings=warnings,
                original_address=original_address,
                final_address=_extract_current_address(shipping_modal),
            )
        validated_ready, use_scope_send = _attempt_validation_candidate_selection(
            driver,
            shipping_modal,
            validation_modal,
            best_candidate["candidate"]["text"],
            warnings,
            dry_run=dry_run,
        )
        if not validated_ready:
            current_after_validation = _merge_address_fields(_extract_current_address(shipping_modal), panel_address)
            existing_options_after_validation = existing_options or _collect_existing_address_options(driver, shipping_modal, max_scrolls=6)
            existing_result = _try_resolve_with_existing_address(
                driver,
                shipping_modal,
                order_id,
                dry_run,
                original_address,
                current_after_validation,
                preserved_address_cont,
                warnings,
                max_scrolls=6,
                existing_options=existing_options_after_validation,
                accept_save_button_ready=False,
            )
            if existing_result is not None:
                warnings.append("Validated popup selection did not produce a valid-address state, so a matching saved address was used instead.")
                return existing_result
            raise TimeoutException("The shipping form never showed Address is valid after selecting the validated address.")
        failure_result, final_address, preserved_address_cont = _prepare_shipping_form_for_save(order_id, shipping_modal, original_address, preserved_address_cont, warnings)
        if failure_result:
            return failure_result
        resolution = _resolution_from_assessment(
            "validated_address",
            "Validated address",
            best_candidate.get("assessment"),
            warnings,
        )
        if (
            _is_base_only_us_postal(current_address.get("zip"))
            and best_candidate["assessment"].get("postal_base_match")
            and not best_candidate["assessment"]["postal_full_match"]
            and _text_has_zip_plus4(best_candidate["candidate"]["text"])
        ):
            warnings.append("Validated address was accepted with a normalized ZIP+4 extension because CRM returned a single safe candidate.")
            if resolution == "validated_address":
                resolution = "validated_address_zip_extension"
        _save_shipping_transaction(
            driver,
            shipping_modal,
            order_id,
            dry_run,
            use_scope_send=use_scope_send,
        )
        result = _result_for(
            order_id,
            "validated_address_saved" if not dry_run else "validated_address_ready",
            "Selected the validated address candidate and reached a valid-address state.",
            success=True,
            resolution=resolution,
            manual_review=False,
            warnings=warnings,
            original_address=original_address,
            final_address=final_address,
        )
        selected_validated_text = _normalize_selected_address_text(best_candidate["candidate"]["text"])
        if selected_validated_text:
            result["selected_validated_address_text"] = selected_validated_text
        return result

    if validation["kind"] == "address_valid":
        failure_result, final_address, preserved_address_cont = _prepare_shipping_form_for_save(order_id, shipping_modal, original_address, preserved_address_cont, warnings)
        if failure_result:
            return failure_result
        _save_shipping_transaction(driver, shipping_modal, order_id, dry_run)
        return _result_for(
            order_id,
            "already_valid" if not dry_run else "already_valid_ready",
            "The shipping form reached a valid-address state without needing an additional selection.",
            success=True,
            resolution="validated",
            manual_review=False,
            warnings=warnings,
            original_address=original_address,
            final_address=final_address,
        )

    return _result_for(
        order_id,
        "unexpected_validation_result",
        "Address validation ended in an unexpected state.",
        success=False,
        resolution="manual_review",
        manual_review=True,
        warnings=warnings,
        original_address=original_address,
        final_address=_extract_current_address(shipping_modal),
    )


def is_login_page(driver):
    try:
        current_url = str(driver.current_url or "").strip().lower()
        login_url = str(CRM_LOGIN_URL or "").strip().lower()
        if "/login" in current_url or "/signin" in current_url:
            return True
        if login_url and current_url.rstrip("/") == login_url.rstrip("/"):
            return True
    except Exception:
        pass
    username_fields = _visible_elements(driver, LOGIN_USERNAME_SELECTORS)
    password_fields = _visible_elements(driver, LOGIN_PASSWORD_SELECTORS)
    login_buttons = _visible_elements(driver, LOGIN_BUTTON_SELECTORS)
    if password_fields and (username_fields or login_buttons):
        return True
    return False


def _field_value(driver, element):
    try:
        return str(driver.execute_script("return arguments[0].value || '';", element) or "").strip()
    except Exception:
        try:
            return str(element.get_attribute("value") or "").strip()
        except Exception:
            return ""


def do_login(driver):
    credential = read_windows_credential(CRM_CREDENTIAL_TARGET)

    username_field = _wait_for_any(driver, LOGIN_USERNAME_SELECTORS, condition="clickable")
    password_field = _wait_for_any(driver, LOGIN_PASSWORD_SELECTORS, condition="clickable")
    login_button = _wait_for_any(driver, LOGIN_BUTTON_SELECTORS, condition="clickable")

    current_username = _field_value(driver, username_field)
    current_password = _field_value(driver, password_field)

    if not current_username or current_username.lower() != credential.username.strip().lower():
        username_field.send_keys(Keys.COMMAND if sys.platform == "darwin" else Keys.CONTROL, "a")
        username_field.send_keys(Keys.DELETE)
        username_field.send_keys(credential.username)

    if not current_password:
        password_field.send_keys(Keys.COMMAND if sys.platform == "darwin" else Keys.CONTROL, "a")
        password_field.send_keys(Keys.DELETE)
        password_field.send_keys(credential.secret)

    _click_with_fallback(driver, login_button)
    login_reclick_after = time.time() + 3
    deadline = time.time() + max(CRM_ACTION_TIMEOUT, 18)
    while time.time() < deadline:
        if not is_login_page(driver):
            return
        try:
            body_text = " ".join((driver.find_element(By.TAG_NAME, "body").text or "").lower().split())
        except Exception:
            body_text = ""
        if any(marker in body_text for marker in ("filters:", "order id", "shipping info", "valid address", "production date:")):
            return
        if time.time() >= login_reclick_after:
            try:
                login_button = _wait_for_any(driver, LOGIN_BUTTON_SELECTORS, timeout=2, condition="clickable")
                _click_with_fallback(driver, login_button)
            except Exception:
                pass
            login_reclick_after = time.time() + 999
        time.sleep(0.25)
    raise TimeoutException("CRM login did not complete before the timeout expired.")


def login_if_needed(driver):
    if is_login_page(driver):
        print("CRM login detected. Submitting credentials...")
        do_login(driver)
        return True
    return False



def _profile_clone_ignore(directory, names):
    del directory
    ignored = set()
    for name in names:
        lowered = name.lower()
        if lowered in PROFILE_CLONE_IGNORE_LOOKUPS:
            ignored.add(name)
            continue
        if lowered.endswith(".lock"):
            ignored.add(name)
    return ignored


def _safe_worker_profile_token(value):
    token = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip())
    return token.strip("._-") or "worker"


def _worker_profile_pool_root(pool_name="batch"):
    return os.path.join(WORKER_PROFILE_POOL_DIR, _safe_worker_profile_token(pool_name))


def _worker_profile_path(pool_name, worker_slot):
    slot = max(1, int(worker_slot or 1))
    return os.path.join(_worker_profile_pool_root(pool_name), f"worker_{slot}")


def _is_within_worker_profile_pool(path):
    root = os.path.abspath(WORKER_PROFILE_POOL_DIR)
    target = os.path.abspath(path)
    return target == root or target.startswith(root + os.sep)


def _worker_profile_lock(profile_path):
    key = os.path.abspath(profile_path)
    with WORKER_PROFILE_LOCKS_LOCK:
        lock = WORKER_PROFILE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            WORKER_PROFILE_LOCKS[key] = lock
        return lock


def _remove_path_quietly(path):
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            os.remove(path)
    except FileNotFoundError:
        pass
    except Exception:
        pass


def _cleanup_reusable_worker_profile(profile_path):
    if not _is_within_worker_profile_pool(profile_path) or not os.path.isdir(profile_path):
        return

    for name in WORKER_PROFILE_LOCK_CLEANUP_NAMES:
        _remove_path_quietly(os.path.join(profile_path, name))

    for parent in (profile_path, os.path.join(profile_path, "Default")):
        for name in WORKER_PROFILE_CACHE_DIR_NAMES:
            _remove_path_quietly(os.path.join(parent, name))


def _rebuild_worker_profile(base_profile_path, worker_profile_path):
    base_abs = os.path.abspath(base_profile_path)
    target_abs = os.path.abspath(worker_profile_path)
    if not _is_within_worker_profile_pool(target_abs):
        raise RuntimeError(f"Refusing to rebuild worker profile outside {WORKER_PROFILE_POOL_DIR}.")
    if not os.path.isdir(base_abs):
        raise RuntimeError(f"CRM profile path does not exist: {base_abs}")

    kill_stale_chrome(target_abs, profile_label="CRM reusable worker")
    if os.path.exists(target_abs):
        shutil.rmtree(target_abs, ignore_errors=True)
    os.makedirs(os.path.dirname(target_abs), exist_ok=True)
    shutil.copytree(base_abs, target_abs, ignore=_profile_clone_ignore)
    _cleanup_reusable_worker_profile(target_abs)
    return target_abs


def rebuild_worker_profile(base_profile_path, worker_profile_path):
    with _worker_profile_lock(worker_profile_path):
        return _rebuild_worker_profile(base_profile_path, worker_profile_path)


def _clone_profile_for_worker(base_profile_path, worker_label, worker_slot=None, pool_name="batch", rebuild=False):
    if not os.path.isdir(base_profile_path):
        raise RuntimeError(f"CRM profile path does not exist: {base_profile_path}")

    if worker_slot is None:
        temp_root = tempfile.mkdtemp(prefix=f"crm_batch_{worker_label}_")
        cloned_profile_path = os.path.join(temp_root, os.path.basename(os.path.normpath(base_profile_path)) or "chrome_profile_crm")
        shutil.copytree(base_profile_path, cloned_profile_path, ignore=_profile_clone_ignore)
        return temp_root, cloned_profile_path

    worker_profile_path = _worker_profile_path(pool_name, worker_slot)
    with _worker_profile_lock(worker_profile_path):
        if rebuild or not os.path.isdir(worker_profile_path):
            _rebuild_worker_profile(base_profile_path, worker_profile_path)
        else:
            _cleanup_reusable_worker_profile(worker_profile_path)
    return None, worker_profile_path


def _build_crm_session_driver(profile_path, headless_mode=True, profile_label="CRM address validator", skip_stale_chrome_check=False):
    resolved_profile_path = os.path.abspath(profile_path or PROFILE_PATH)
    if not skip_stale_chrome_check:
        kill_stale_chrome(resolved_profile_path, profile_label=profile_label)
    return build_chrome_driver(
        resolved_profile_path,
        headless_mode=bool(headless_mode),
        page_load_strategy="eager",
        page_load_timeout=max(CRM_PAGE_LOAD_TIMEOUT, 30),
        script_timeout=CRM_ACTION_TIMEOUT,
    )


def _collect_batch_order_ids_with_driver(driver, shipping_filter, limit, list_url_override=None, exclude_order_ids=None, allowed_row_labels=None, allowed_row_description=None):
    normalized_filter = _normalize_shipping_filter(shipping_filter or CRM_SHIPPING_FILTER_DEFAULT)
    target_limit = max(1, int(limit or 1))
    list_url = _shipping_list_url_for_filter(normalized_filter, list_url_override=list_url_override)
    list_label = _shipping_list_label(normalized_filter, list_url_override=list_url_override)
    try:
        driver.switch_to.default_content()
    except Exception:
        pass
    print(f"Opening shipping-address CRM report for {list_label} to collect up to {target_limit} order(s)...")
    safe_get_with_partial_load(driver, list_url, f"CRM shipping-address report ({list_label})")
    if login_if_needed(driver):
        safe_get_with_partial_load(driver, list_url, f"CRM shipping-address report after login ({list_label})")
    matches = _find_shipping_list_orders(
        driver,
        limit=target_limit,
        timeout=max(CRM_ACTION_TIMEOUT, 12),
        exclude_order_ids=exclude_order_ids,
        allowed_row_labels=allowed_row_labels,
        allowed_row_description=allowed_row_description,
    )
    return [item["order_id"] for item in matches if item.get("order_id")]


def _collect_batch_order_ids(shipping_filter, limit, profile_path, list_url_override=None, exclude_order_ids=None, visible=False, allowed_row_labels=None, allowed_row_description=None):
    resolved_profile_path = os.path.abspath(profile_path or PROFILE_PATH)
    attempt_modes = [False] if visible else _crm_attempt_modes()
    last_error = None

    for index, headless_mode in enumerate(attempt_modes, start=1):
        driver = None
        try:
            driver = _build_crm_session_driver(
                resolved_profile_path,
                headless_mode=headless_mode,
                profile_label="CRM address validator batch source",
            )
            return _collect_batch_order_ids_with_driver(
                driver,
                shipping_filter,
                limit,
                list_url_override=list_url_override,
                exclude_order_ids=exclude_order_ids,
                allowed_row_labels=allowed_row_labels,
                allowed_row_description=allowed_row_description,
            )
        except Exception as exc:
            last_error = exc
            if not headless_mode or index == len(attempt_modes) or not _is_retryable_exception(exc):
                raise
            print("Headless CRM batch source failed with a retryable error; retrying with visible Chrome...")
            time.sleep(1)
        finally:
            safe_driver_quit(driver, profile_path=resolved_profile_path)

    if last_error is not None:
        raise last_error
    return []


def _batch_summary_message(report_items, launched_count, refresh_passes=1):
    total = len(report_items)
    succeeded = sum(1 for item in report_items if item.get("success"))
    manual_review = sum(1 for item in report_items if item.get("manual_review_required"))
    failed = total - succeeded
    parts = [
        f"Processed {total} order(s) with {launched_count} parallel worker(s) across {max(1, int(refresh_passes or 1))} CRM list refresh pass(es).",
        f"{succeeded} succeeded.",
    ]
    if manual_review:
        parts.append(f"{manual_review} require manual review.")
    elif failed:
        parts.append(f"{failed} failed.")
    return " ".join(parts)


def _batch_collection_failure_payload(
    exc,
    *,
    dry_run=False,
    shipping_filter=None,
    batch_size=None,
    parallel_workers=1,
    refresh_passes=0,
    started_at=None,
    list_url_override=None,
):
    error_type = type(exc).__name__
    message = f"CRM batch list collection failed: {error_type}: {exc}"
    payload = {
        "action": "validate_batch",
        "success": False,
        "message": message,
        "order_count": 0,
        "order_ids": [],
        "report": [],
        "dry_run": bool(dry_run),
        "headless": bool(CRM_HEADLESS),
        "manual_review_required": False,
        "resolution": "list_collection_failed",
        "shipping_filter": _normalize_shipping_filter(shipping_filter or CRM_SHIPPING_FILTER_DEFAULT),
        "batch_size": batch_size,
        "parallel_workers": max(1, int(parallel_workers or 1)),
        "refresh_passes": max(0, int(refresh_passes or 0)),
        "retryable": _is_retryable_exception(exc),
        "error_type": error_type,
        "duration_seconds": _elapsed_seconds(started_at) if started_at is not None else 0.0,
    }
    if _normalized_list_url_override(list_url_override):
        payload["list_url"] = _normalized_list_url_override(list_url_override)
    return payload


def _run_single_payload(
    order_id=None,
    dry_run=False,
    shipping_filter=None,
    profile_path=None,
    list_url_override=None,
    visible=False,
    skip_stale_chrome_check=False,
):
    started_at = time.monotonic()
    normalized_shipping_filter = _normalize_shipping_filter(shipping_filter or CRM_SHIPPING_FILTER_DEFAULT)
    normalized_order_id = _normalize_target_order_id(order_id) if order_id else None
    if order_id and not normalized_order_id:
        raise RuntimeError("Order ID must be a 7-digit value or a CRM order URL ending in a 7-digit order ID.")

    modes = [False] if visible else _crm_attempt_modes()

    last_payload = None
    for index, headless_mode in enumerate(modes, start=1):
        print(f"Starting CRM address validator attempt mode {index}/{len(modes)} (headless={headless_mode})...")
        payload = _run_once(
            normalized_order_id or None,
            dry_run=dry_run,
            headless_mode=headless_mode,
            shipping_filter=normalized_shipping_filter,
            profile_path=profile_path,
            list_url_override=list_url_override,
            skip_stale_chrome_check=skip_stale_chrome_check,
        )
        last_payload = payload
        payload["headless"] = bool(headless_mode)
        payload["shipping_filter"] = normalized_shipping_filter
        if _normalized_list_url_override(list_url_override):
            payload["list_url"] = _normalized_list_url_override(list_url_override)
        _attach_duration(payload, _elapsed_seconds(started_at))
        if payload.get("success"):
            return payload
        if not payload.get("retryable") or not headless_mode or index == len(modes):
            break
        print("Headless CRM address validation failed with a retryable error; retrying with visible Chrome...")
        time.sleep(1)

    failure_payload = last_payload or {
        "success": False,
        "message": "CRM address validator did not produce a result.",
        "order_ids": [normalized_order_id] if normalized_order_id else [],
        "report": [],
        "manual_review_required": True,
        "resolution": "manual_review",
    }
    failure_payload["shipping_filter"] = normalized_shipping_filter
    failure_payload.setdefault("duration_seconds", _elapsed_seconds(started_at))
    failure_payload.setdefault("session_duration_seconds", failure_payload.get("duration_seconds"))
    if _normalized_list_url_override(list_url_override):
        failure_payload["list_url"] = _normalized_list_url_override(list_url_override)
    return failure_payload


def _run_batch_reusing_session(dry_run=False, shipping_filter=None, batch_size=2, profile_path=None, list_url_override=None, visible=False):
    normalized_shipping_filter = _normalize_shipping_filter(shipping_filter or CRM_SHIPPING_FILTER_DEFAULT)
    resolved_profile_path = os.path.abspath(profile_path or PROFILE_PATH)
    requested_batch_size = _normalize_requested_batch_size(batch_size)
    finished_payloads = []
    attempted_order_ids = []
    attempted_order_id_set = set()
    refresh_passes = 0

    modes = [False] if visible else _crm_attempt_modes()
    current_mode_index = 0
    current_headless_mode = modes[current_mode_index]
    driver = None

    def _launch_shared_driver():
        return _build_crm_session_driver(
            resolved_profile_path,
            headless_mode=current_headless_mode,
            profile_label="CRM address validator batch shared session",
        )

    def _relaunch_shared_driver(reason):
        nonlocal driver, current_mode_index, current_headless_mode
        print(reason)
        safe_driver_quit(driver, profile_path=resolved_profile_path)
        current_mode_index = min(current_mode_index + 1, len(modes) - 1)
        current_headless_mode = modes[current_mode_index]
        driver = _launch_shared_driver()

    driver = _launch_shared_driver()
    try:
        while not _batch_limit_reached(len(attempted_order_ids), requested_batch_size):
            refresh_passes += 1
            remaining_batch_slots = _batch_collection_limit(requested_batch_size, len(attempted_order_ids))
            while True:
                try:
                    order_ids = _collect_batch_order_ids_with_driver(
                        driver,
                        normalized_shipping_filter,
                        remaining_batch_slots,
                        list_url_override=list_url_override,
                        exclude_order_ids=attempted_order_id_set,
                    )
                    break
                except Exception as exc:
                    if current_headless_mode and current_mode_index + 1 < len(modes) and _is_retryable_exception(exc):
                        _relaunch_shared_driver(
                            "Headless CRM batch list collection failed with a retryable error; relaunching visible Chrome..."
                        )
                        continue
                    raise

            if not order_ids:
                break

            selected_order_ids = order_ids[:remaining_batch_slots]
            for order_index, order_id in enumerate(selected_order_ids, start=1):
                attempted_order_id_set.add(order_id)
                attempted_order_ids.append(order_id)
                print(
                    f"Processing batch order {order_index}/{len(selected_order_ids)} in the shared CRM browser session: {order_id}..."
                )
                payload = _run_once_with_driver(
                    driver,
                    order_id=order_id,
                    dry_run=dry_run,
                    headless_mode=current_headless_mode,
                    shipping_filter=normalized_shipping_filter,
                    list_url_override=None,
                )
                payload["headless"] = bool(current_headless_mode)
                payload["shipping_filter"] = normalized_shipping_filter

                if payload.get("retryable") and current_headless_mode and current_mode_index + 1 < len(modes):
                    _relaunch_shared_driver(
                        "Headless CRM batch validation failed with a retryable error; relaunching visible Chrome and retrying the same order..."
                    )
                    payload = _run_once_with_driver(
                        driver,
                        order_id=order_id,
                        dry_run=dry_run,
                        headless_mode=current_headless_mode,
                        shipping_filter=normalized_shipping_filter,
                        list_url_override=None,
                    )
                    payload["headless"] = bool(current_headless_mode)
                    payload["shipping_filter"] = normalized_shipping_filter

                finished_payloads.append(payload)

                if payload.get("error_type"):
                    print("Refreshing the shared CRM browser session before the next order because the last run hit a browser exception.")
                    safe_driver_quit(driver, profile_path=resolved_profile_path)
                    driver = _launch_shared_driver()

                if _batch_limit_reached(len(attempted_order_ids), requested_batch_size):
                    break

            if _batch_limit_reached(len(attempted_order_ids), requested_batch_size):
                print(f"Reached the requested CRM batch size of {requested_batch_size} order(s); ending the batch run.")
                break

            print(f"Finished CRM list refresh pass {refresh_passes}; reopening the list to look for additional scheduled orders...")
    finally:
        safe_driver_quit(driver, profile_path=resolved_profile_path)

    return finished_payloads, attempted_order_ids, refresh_passes


def _run_batch(dry_run=False, shipping_filter=None, batch_size=2, parallel_workers=1, profile_path=None, list_url_override=None, visible=False):
    batch_started_at = time.monotonic()
    normalized_shipping_filter = _normalize_shipping_filter(shipping_filter or CRM_SHIPPING_FILTER_DEFAULT)
    resolved_profile_path = os.path.abspath(profile_path or PROFILE_PATH)
    requested_batch_size = _normalize_requested_batch_size(batch_size)
    worker_limit = max(1, int(parallel_workers or 1))
    if requested_batch_size is not None:
        worker_limit = min(worker_limit, requested_batch_size)
    finished_payloads = []
    attempted_order_ids = []
    refresh_passes = 0
    stage_timings = []

    if worker_limit == 1:
        reuse_started_at = time.monotonic()
        for collection_attempt in range(1, 3):
            try:
                finished_payloads, attempted_order_ids, refresh_passes = _run_batch_reusing_session(
                    dry_run=dry_run,
                    shipping_filter=normalized_shipping_filter,
                    batch_size=requested_batch_size,
                    profile_path=resolved_profile_path,
                    list_url_override=list_url_override,
                    visible=visible,
                )
                break
            except Exception as exc:
                if collection_attempt == 1 and _is_retryable_exception(exc):
                    print(
                        "CRM batch list collection lost its browser session; "
                        "retrying once with a fresh CRM browser..."
                    )
                    time.sleep(1)
                    continue
                return _batch_collection_failure_payload(
                    exc,
                    dry_run=dry_run,
                    shipping_filter=normalized_shipping_filter,
                    batch_size=requested_batch_size,
                    parallel_workers=worker_limit,
                    refresh_passes=refresh_passes,
                    started_at=batch_started_at,
                    list_url_override=list_url_override,
                )
        _record_stage_timing(
            stage_timings,
            "shared_session_batch",
            reuse_started_at,
            order_count=len(attempted_order_ids),
            refresh_passes=refresh_passes,
        )
    else:
        finished_lock = threading.Lock()
        attempted_order_id_set = set()

        while not _batch_limit_reached(len(attempted_order_ids), requested_batch_size):
            refresh_passes += 1
            remaining_batch_slots = _batch_collection_limit(
                requested_batch_size,
                len(attempted_order_ids),
                worker_limit=worker_limit,
            )
            for collection_attempt in range(1, 3):
                list_scan_started_at = time.monotonic()
                try:
                    order_ids = _collect_batch_order_ids(
                        normalized_shipping_filter,
                        remaining_batch_slots,
                        resolved_profile_path,
                        list_url_override=list_url_override,
                        exclude_order_ids=attempted_order_id_set,
                        visible=visible,
                    )
                    _record_stage_timing(
                        stage_timings,
                        "list_scan",
                        list_scan_started_at,
                        refresh_pass=refresh_passes,
                        order_count=len(order_ids),
                        attempt=collection_attempt,
                    )
                    break
                except Exception as exc:
                    _record_stage_timing(
                        stage_timings,
                        "list_scan_failed",
                        list_scan_started_at,
                        refresh_pass=refresh_passes,
                        attempt=collection_attempt,
                        retryable=_is_retryable_exception(exc),
                    )
                    if collection_attempt == 1 and _is_retryable_exception(exc):
                        print(
                            "CRM batch list collection lost its browser session; "
                            "retrying once with a fresh CRM browser..."
                        )
                        time.sleep(1)
                        continue
                    return _batch_collection_failure_payload(
                        exc,
                        dry_run=dry_run,
                        shipping_filter=normalized_shipping_filter,
                        batch_size=requested_batch_size,
                        parallel_workers=worker_limit,
                        refresh_passes=refresh_passes,
                        started_at=batch_started_at,
                        list_url_override=list_url_override,
                    )
            if not order_ids:
                break
            order_ids = order_ids[:remaining_batch_slots]

            worker_gate = threading.BoundedSemaphore(worker_limit)
            threads = []
            chunk_results = []
            chunk_started_at = time.monotonic()

            def _worker(order_index, order_id):
                with worker_gate:
                    session_started_at = time.monotonic()
                    print(f"Launching batch worker {order_index + 1}/{len(order_ids)} for order {order_id}...")

                    def _exception_payload(exc):
                        return {
                            "action": "validate_order",
                            "target_order_id": order_id,
                            "order_count": 1,
                            "order_ids": [order_id],
                            "report": [
                                {
                                    "order_id": order_id,
                                    "success": False,
                                    "outcome": "batch_worker_exception",
                                    "message": str(exc),
                                    "resolution": "manual_review",
                                    "manual_review_required": True,
                                    "warnings": [],
                                    "retry_attempted": False,
                                }
                            ],
                            "success": False,
                            "message": str(exc),
                            "dry_run": bool(dry_run),
                            "headless": bool(CRM_HEADLESS),
                            "manual_review_required": True,
                            "resolution": "manual_review",
                            "shipping_filter": normalized_shipping_filter,
                            "retryable": _is_retryable_exception(exc),
                            "error_type": type(exc).__name__,
                        }

                    worker_slot = (order_index % worker_limit) + 1

                    def _run_worker_attempt(label_suffix="", rebuild_profile=False):
                        temp_root, cloned_profile_path = _clone_profile_for_worker(
                            resolved_profile_path,
                            f"{order_index + 1}_{order_id}{label_suffix}",
                            worker_slot=worker_slot,
                            pool_name="address_validator",
                            rebuild=rebuild_profile,
                        )
                        temp_result_file = os.path.join(temp_root or cloned_profile_path, f"thread_{order_id}.json")
                        try:
                            with _worker_profile_lock(cloned_profile_path):
                                payload = _run_single_payload(
                                    order_id=order_id,
                                    dry_run=dry_run,
                                    shipping_filter=normalized_shipping_filter,
                                    profile_path=cloned_profile_path,
                                    list_url_override=None,
                                    visible=visible,
                                    skip_stale_chrome_check=True,
                                )
                        except Exception as exc:
                            payload = _exception_payload(exc)
                        finally:
                            if temp_root:
                                shutil.rmtree(temp_root, ignore_errors=True)
                        return payload, temp_result_file

                    payload, temp_result_file = _run_worker_attempt()
                    if _payload_has_retryable_worker_exception(payload):
                        print(f"Retrying order {order_id} once with a fresh CRM worker profile after a transient browser/UI error...")
                        payload, temp_result_file = _run_worker_attempt("_retry", rebuild_profile=True)
                        _mark_transient_retry_attempted(payload)

                    _attach_duration(payload, _elapsed_seconds(session_started_at))
                    payload["target_order_id"] = payload.get("target_order_id") or order_id
                    write_result_payload(
                        AUTOMATION_NAME,
                        "crm_validate_address.py",
                        bool(payload.get("success")),
                        payload.get("message") or "Address validator completed.",
                        extra_fields=payload,
                        result_file=temp_result_file,
                        audit_log=False,
                    )
                    with finished_lock:
                        chunk_results.append(payload)

            for order_index, order_id in enumerate(order_ids):
                attempted_order_id_set.add(order_id)
                attempted_order_ids.append(order_id)
                thread = threading.Thread(target=_worker, args=(order_index, order_id), daemon=True)
                threads.append(thread)
                thread.start()

            for thread in threads:
                thread.join()
            _record_stage_timing(
                stage_timings,
                "parallel_order_processing",
                chunk_started_at,
                refresh_pass=refresh_passes,
                order_count=len(order_ids),
                worker_count=worker_limit,
            )

            chunk_order_position = {order_id: index for index, order_id in enumerate(order_ids)}
            chunk_results.sort(key=lambda payload: chunk_order_position.get(str(payload.get("target_order_id") or ""), 999999))
            finished_payloads.extend(chunk_results)

            if _batch_limit_reached(len(attempted_order_ids), requested_batch_size):
                print(f"Reached the requested CRM batch size of {requested_batch_size} order(s); ending the batch run.")
                break

            print(f"Finished CRM list refresh pass {refresh_passes}; reopening the list to look for additional scheduled orders...")

    if not attempted_order_ids:
        payload = {
            "action": "validate_batch",
            "success": True,
            "message": f"No {ALLOWED_SHIPPING_LIST_ROW_DESCRIPTION} invalid-address orders were detected in the CRM shipping list.",
            "order_count": 0,
            "order_ids": [],
            "report": [],
            "dry_run": bool(dry_run),
            "headless": bool(CRM_HEADLESS),
            "manual_review_required": False,
            "resolution": "no_orders",
            "shipping_filter": normalized_shipping_filter,
            "batch_size": requested_batch_size,
            "parallel_workers": worker_limit,
            "refresh_passes": refresh_passes,
            "duration_seconds": _elapsed_seconds(batch_started_at),
            "stage_timings": stage_timings,
        }
        if _normalized_list_url_override(list_url_override):
            payload["list_url"] = _normalized_list_url_override(list_url_override)
        return payload

    report_items = []
    for payload in finished_payloads:
        payload_report = payload.get("report")
        if isinstance(payload_report, list) and payload_report:
            report_items.extend(payload_report)

    success = bool(report_items) and all(bool(item.get("success")) for item in report_items)
    manual_review_required = any(bool(item.get("manual_review_required")) for item in report_items)
    return {
        **({"list_url": _normalized_list_url_override(list_url_override)} if _normalized_list_url_override(list_url_override) else {}),
        "action": "validate_batch",
        "success": success,
        "message": _batch_summary_message(report_items, worker_limit, refresh_passes=refresh_passes),
        "order_count": len(attempted_order_ids),
        "order_ids": attempted_order_ids,
        "report": report_items,
        "dry_run": bool(dry_run),
        "headless": bool(CRM_HEADLESS),
        "manual_review_required": manual_review_required,
        "resolution": "batch",
        "shipping_filter": normalized_shipping_filter,
        "batch_size": requested_batch_size,
        "parallel_workers": worker_limit,
        "refresh_passes": refresh_passes,
        "duration_seconds": _elapsed_seconds(batch_started_at),
        "stage_timings": stage_timings,
    }


def _run_once_with_driver(driver, order_id=None, dry_run=False, headless_mode=True, shipping_filter=None, list_url_override=None):
    started_at = time.monotonic()
    try:
        first_timeout = None
        try:
            result = _evaluate_and_resolve_order(
                driver,
                order_id,
                dry_run=dry_run,
                shipping_filter=shipping_filter,
                list_url_override=list_url_override,
            )
        except TimeoutException as exc:
            retry_order_id = _normalize_target_order_id(order_id)
            if not retry_order_id:
                raise
            first_timeout = str(exc)
            print(
                f"Address Validator timed out for order {retry_order_id}: {exc}. "
                "Reloading the CRM order once and retrying from a clean page."
            )
            try:
                driver.switch_to.default_content()
            except Exception:
                pass
            safe_get_with_partial_load(
                driver,
                f"https://crm2.legacy.printfly.com/order/{retry_order_id}",
                f"CRM order {retry_order_id} Address Validator timeout recovery",
            )
            if login_if_needed(driver):
                safe_get_with_partial_load(
                    driver,
                    f"https://crm2.legacy.printfly.com/order/{retry_order_id}",
                    f"CRM order {retry_order_id} Address Validator timeout recovery after login",
                )
            result = _evaluate_and_resolve_order(
                driver,
                retry_order_id,
                dry_run=dry_run,
                shipping_filter=shipping_filter,
                list_url_override=list_url_override,
            )
        if first_timeout:
            result.setdefault("warnings", []).append(
                f"Recovered after one CRM reload from initial timeout: {first_timeout}"
            )
            result["retried_after_timeout"] = True
        result.setdefault("warnings", [])
        duration = _elapsed_seconds(started_at)
        result["duration_seconds"] = duration
        result["session_duration_seconds"] = duration
        resolved_order_id = ''.join(ch for ch in str(result.get("order_id") or order_id or "") if ch.isdigit())
        order_ids = [resolved_order_id] if len(resolved_order_id) == 7 else []
        payload = {
            "action": "validate_order",
            "target_order_id": order_ids[0] if order_ids else None,
            "order_count": len(order_ids),
            "order_ids": order_ids,
            "report": [result],
            "dry_run": bool(dry_run),
            "headless": bool(headless_mode),
            "manual_review_required": bool(result.get("manual_review_required")),
            "resolution": result.get("resolution") or "",
            "duration_seconds": duration,
            "session_duration_seconds": duration,
        }
        payload["success"] = bool(result.get("success"))
        payload["message"] = str(result.get("message") or "Address validator completed.")
        return payload
    except Exception as exc:
        if driver is not None:
            safe_take_screenshot(driver, "crm_address_validator_error")
        duration = _elapsed_seconds(started_at)
        normalized_order_id = ''.join(ch for ch in str(order_id or "") if ch.isdigit())
        if len(normalized_order_id) != 7:
            match = ORDER_ID_PATTERN.search(str(exc) or "")
            normalized_order_id = match.group(0) if match else ""
        order_ids = [normalized_order_id] if len(normalized_order_id) == 7 else []
        return {
            "action": "validate_order",
            "target_order_id": order_ids[0] if order_ids else None,
            "order_count": len(order_ids),
            "order_ids": order_ids,
            "report": [
                {
                    "order_id": order_ids[0] if order_ids else None,
                    "success": False,
                    "outcome": "worker_exception",
                    "message": str(exc),
                    "resolution": "manual_review",
                    "manual_review_required": True,
                    "warnings": [],
                    "retry_attempted": False,
                    "duration_seconds": duration,
                    "session_duration_seconds": duration,
                }
            ],
            "success": False,
            "message": str(exc),
            "dry_run": bool(dry_run),
            "headless": bool(headless_mode),
            "manual_review_required": True,
            "resolution": "manual_review",
            "retryable": _is_retryable_exception(exc),
            "error_type": type(exc).__name__,
            "duration_seconds": duration,
            "session_duration_seconds": duration,
        }


def _run_once(
    order_id=None,
    dry_run=False,
    headless_mode=True,
    shipping_filter=None,
    profile_path=None,
    list_url_override=None,
    skip_stale_chrome_check=False,
):
    driver = None
    resolved_profile_path = os.path.abspath(profile_path or PROFILE_PATH)
    try:
        driver = _build_crm_session_driver(
            resolved_profile_path,
            headless_mode=headless_mode,
            profile_label="CRM address validator",
            skip_stale_chrome_check=skip_stale_chrome_check,
        )
        return _run_once_with_driver(
            driver,
            order_id=order_id,
            dry_run=dry_run,
            headless_mode=headless_mode,
            shipping_filter=shipping_filter,
            list_url_override=list_url_override,
        )
    finally:
        safe_driver_quit(driver, profile_path=resolved_profile_path)



def run(order_id=None, dry_run=False, shipping_filter=None, action="validate_order", batch_size=2, parallel_workers=1, profile_path=None, result_file=None, list_url=None, visible=False):
    normalized_shipping_filter = _normalize_shipping_filter(shipping_filter or CRM_SHIPPING_FILTER_DEFAULT)
    normalized_list_url = _normalized_list_url_override(list_url)
    started_at = time.monotonic()
    try:
        _validate_runtime_config(normalized_shipping_filter, list_url_override=normalized_list_url)
        resolved_profile_path = os.path.abspath(profile_path or PROFILE_PATH)

        if action == "validate_batch":
            payload = _run_batch(
                dry_run=dry_run,
                shipping_filter=normalized_shipping_filter,
                batch_size=batch_size,
                parallel_workers=parallel_workers,
                profile_path=resolved_profile_path,
                list_url_override=normalized_list_url,
                visible=visible,
            )
            write_result_payload(
                AUTOMATION_NAME,
                "crm_validate_address.py",
                bool(payload.get("success")),
                payload.get("message") or "Batch address validator completed.",
                extra_fields=payload,
                result_file=result_file,
            )
            return 0 if payload.get("success") else 1

        payload = _run_single_payload(
            order_id=order_id,
            dry_run=dry_run,
            shipping_filter=normalized_shipping_filter,
            profile_path=resolved_profile_path,
            list_url_override=normalized_list_url,
            visible=visible,
        )
        write_result_payload(
            AUTOMATION_NAME,
            "crm_validate_address.py",
            bool(payload.get("success")),
            payload.get("message") or (
                "Address validator completed successfully." if payload.get("success") else "CRM address validator failed."
            ),
            extra_fields=payload,
            result_file=result_file,
        )
        return 0 if payload.get("success") else 1
    except Exception as exc:
        payload = {
            "action": action if action in {"validate_order", "validate_batch"} else "validate_order",
            "success": False,
            "message": f"CRM address validator crashed before completion: {type(exc).__name__}: {exc}",
            "order_count": 0,
            "order_ids": [],
            "report": [],
            "dry_run": bool(dry_run),
            "headless": bool(CRM_HEADLESS),
            "manual_review_required": False,
            "resolution": "worker_crashed",
            "shipping_filter": normalized_shipping_filter,
            "batch_size": batch_size,
            "parallel_workers": max(1, int(parallel_workers or 1)),
            "retryable": _is_retryable_exception(exc),
            "error_type": type(exc).__name__,
            "duration_seconds": _elapsed_seconds(started_at),
        }
        if normalized_list_url:
            payload["list_url"] = normalized_list_url
        write_result_payload(
            AUTOMATION_NAME,
            "crm_validate_address.py",
            False,
            payload["message"],
            extra_fields=payload,
            result_file=result_file,
        )
        return 1



def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Validate CRM shipping-address orders.")
    parser.add_argument("--action", choices=["validate_order", "validate_batch"], default="validate_order")
    parser.add_argument("--order-id", required=False, help="Optional 7-digit CRM order ID override. If omitted, the first order from CRM_SHIPPING_URL is used.")
    parser.add_argument(
        "--shipping-filter",
        choices=["free", "rush", "all", "813", "high_value"],
        default=_normalize_shipping_filter(CRM_SHIPPING_FILTER_DEFAULT),
        help="Choose which shipping rules to apply and, unless --list-url is provided, which built-in CRM list to use.",
    )
    parser.add_argument(
        "--list-url",
        required=False,
        help="Optional CRM report URL override. When provided, the validator opens this list instead of the built-in free/rush URL.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=None,
        help="When --action validate_batch is used, process up to this many total orders in the current batch run. Leave it blank to keep running until no eligible orders remain.",
    )
    parser.add_argument(
        "--parallel-workers",
        type=int,
        default=1,
        help="When --action validate_batch is used, run up to this many validator workers at the same time. A value of 1 reuses one long-lived CRM browser session across the whole batch; higher values use isolated cloned-profile workers.",
    )
    parser.add_argument(
        "--profile-path",
        required=False,
        help="Optional Chrome user-data-dir override. Used internally for isolated batch workers.",
    )
    parser.add_argument(
        "--result-file",
        required=False,
        help="Optional path for the JSON result payload. Used internally for isolated batch workers.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Reach a valid-address state without clicking the final Save button.",
    )
    parser.add_argument("--visible", action="store_true", help="Run Chrome visibly instead of headless for testing.")
    return parser.parse_args(argv)


if __name__ == "__main__":
    options = parse_args(sys.argv[1:])
    sys.exit(
        run(
            options.order_id,
            dry_run=bool(options.dry_run),
            shipping_filter=options.shipping_filter,
            action=options.action,
            batch_size=options.batch_size,
            parallel_workers=options.parallel_workers,
            profile_path=options.profile_path,
            result_file=options.result_file,
            list_url=options.list_url,
            visible=bool(options.visible),
        )
    )


