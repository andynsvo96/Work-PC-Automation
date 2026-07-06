"""
Paycom Automation HTTP Server
"""

import ast
import base64
import gzip
import importlib
import json
import logging
import math
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, Response, jsonify, request
import pystray
from PIL import Image, ImageDraw

from automation_audit import AUDIT_LOG_FILE, log_automation_event, log_automation_result
import config as config_module
from runtime_paths import STATE_DIR, log_file as runtime_log_file
from runtime_paths import resolve_runtime_file, result_file, state_file
from routes.system_routes import register_system_routes
from routes.work_routes import register_work_routes
from slack_message_rotation import select_slack_day_message

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.py")
RESULT_FILE = result_file("last_result.json")
AUTOMATION_STATUS_FILE = state_file("automation_status.json")
UI_TEMPLATE_FILE = os.path.join(SCRIPT_DIR, "ui_panel.html")
WORKERS_DIR = os.path.join(SCRIPT_DIR, "workers")

log_file = runtime_log_file("server.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(log_file), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

CLOCK_SCRIPT = os.path.join(WORKERS_DIR, "paycom_clock.py")
SLACK_SCRIPT = os.path.join(WORKERS_DIR, "slack_team.py")
PAYCOM_HOURS_SCRIPT = os.path.join(WORKERS_DIR, "paycom_hours.py")
CRM_SCRIPT = os.path.join(WORKERS_DIR, "crm_unlock_orders.py")
CRM_ADDRESS_VALIDATOR_SCRIPT = os.path.join(WORKERS_DIR, "crm_validate_address.py")
CRM_PRODUCT_SEPARATOR_SCRIPT = os.path.join(WORKERS_DIR, "crm_product_separator.py")
CRM_ORDER_GOODS_SCRIPT = os.path.join(WORKERS_DIR, "crm_order_goods.py")
CRM_SHIPPING_BYPASSER_SCRIPT = os.path.join(WORKERS_DIR, "crm_shipping_bypasser.py")
CRM_PUSH_BACK_SCRIPT = os.path.join(WORKERS_DIR, "crm_push_back.py")
CRM_AUTO_SPLITTER_SCRIPT = os.path.join(WORKERS_DIR, "crm_auto_splitter.py")
CRM_MASS_EMAILER_SCRIPT = os.path.join(WORKERS_DIR, "crm_copyright_cancel.py")
SLACK_SCRIPT_TIMEOUT_SECONDS = 150

WORK_CLOCK_CAPPED = True
WORK_CLOCK_CAP_HOURS = 40.0
WORK_CLOCK_BREAK_MINUTES = 30
WORK_CLOCK_BREAK_APPLIES_AFTER_HOURS = 4.0
WORK_CLOCK_DEFAULT_DAILY_HOURS = 8.0
WORK_CLOCK_AUTO_OUT_MAX_HOURS = 24.0
WORK_CLOCK_STATE_FILE = "work_hours.json"
WORK_CLOCK_RESET_ON_FRIDAY_CLOCK_OUT = True
WORK_CLOCK_SYNC_FROM_PAYCOM = True
WORK_CLOCK_SYNC_BEFORE_CLOCK_IN = True
WORK_CLOCK_SYNC_AFTER_CLOCK_OUT = True
WORK_STATE_FILE = resolve_runtime_file(WORK_CLOCK_STATE_FILE, STATE_DIR)
CRM_MAX_RETRIES = 2
CRM_RETRY_DELAY_SECONDS = 3
CRM_ACTION_TIMEOUT = 15
CRM_SHIPPING_ALL_URL = str(getattr(config_module, "CRM_SHIPPING_ALL_URL", "") or "").strip()
CRM_SHIPPING_813_URL = str(getattr(config_module, "CRM_SHIPPING_813_URL", "") or "").strip()
CRM_813_VALIDATOR_URL = str(getattr(config_module, "CRM_813_VALIDATOR_URL", CRM_SHIPPING_813_URL) or "").strip()
CRM_813_ORDER_GOODS_URL = str(getattr(config_module, "CRM_813_ORDER_GOODS_URL", "") or "").strip()
CRM_813_BYPASS_URL = str(getattr(config_module, "CRM_813_BYPASS_URL", "") or "").strip()
CRM_PUSH_BACK_RUSH_URL = str(getattr(config_module, "CRM_PUSH_BACK_RUSH_URL", "") or "").strip()
CRM_PUSH_BACK_813_URL = str(getattr(config_module, "CRM_PUSH_BACK_813_URL", "") or "").strip()
CRM_ADDRESS_CONTINUOUS_BATCH_TIMEOUT_SECONDS = 12 * 60 * 60
CRM_AUTO_SPLITTER_PREFLIGHT_REUSE_SECONDS = 10 * 60
CRM_SHIPPING_BYPASSER_BASE_TIMEOUT_SECONDS = int(
    getattr(config_module, "CRM_SHIPPING_BYPASSER_BASE_TIMEOUT_SECONDS", 30 * 60)
    or (30 * 60)
)
CRM_SHIPPING_BYPASSER_EXTRA_ORDER_TIMEOUT_SECONDS = int(
    getattr(config_module, "CRM_SHIPPING_BYPASSER_EXTRA_ORDER_TIMEOUT_SECONDS", 15 * 60)
    or (15 * 60)
)
CRM_STATE_FILE = state_file("crm_state.json")
CRM_ADDRESS_STATE_FILE = state_file("crm_address_validator_state.json")
CRM_PROCESSING_STATE_FILE = state_file("crm_processing_state.json")
CRM_MASS_EMAILER_STATE_FILE = state_file("crm_mass_emailer_state.json")
CRM_SHARED_MAX_PARALLEL_WORKERS = 8
CRM_ORDER_GOODS_MAX_PARALLEL_WORKERS = CRM_SHARED_MAX_PARALLEL_WORKERS
CRM_ADDRESS_REPORT_MAX_ITEMS = 500
CRM_MASS_EMAILER_TIMEOUT_SECONDS = 2 * 60 * 60
SERVER_BIND_HOST = "0.0.0.0"
SERVER_PORT = 5123
SERVER_STARTED_AT = datetime.now()

app = Flask(__name__)
clock_lock = threading.Lock()
state_lock = threading.Lock()
config_lock = threading.Lock()
crm_lock = threading.Lock()
crm_state_lock = threading.Lock()
crm_address_state_lock = threading.Lock()
crm_processing_state_lock = threading.Lock()
crm_runtime_lock = threading.Lock()
crm_address_runtime_lock = threading.Lock()
crm_product_separator_runtime_lock = threading.Lock()
crm_order_goods_runtime_lock = threading.Lock()
crm_shipping_bypasser_runtime_lock = threading.Lock()
crm_push_back_runtime_lock = threading.Lock()
crm_auto_splitter_runtime_lock = threading.Lock()
crm_processing_runtime_lock = threading.Lock()
crm_mass_emailer_state_lock = threading.Lock()
crm_mass_emailer_runtime_lock = threading.Lock()
automation_process_lock = threading.RLock()
auto_clock_timer = None
tray_icon_ref = None
tray_auto_out_text = "Scheduled auto clock-out: not scheduled"
tray_week_hours_text = "Current week hours: 0.00"
tray_auto_out_active = False
power_timer_lock = threading.Lock()
power_countdown_timer = None
power_countdown_state = {
    "action": None,
    "scheduled_at": None,
    "execute_at": None,
    "duration_seconds": 0,
}
SLACK_LUNCH_START_MESSAGE_FALLBACK = "breaking"
SLACK_LUNCH_RETURN_MESSAGE_FALLBACK = "back"
SLACK_LUNCH_BREAK_SECONDS = 3600
lunch_timer_lock = threading.Lock()
lunch_return_timer = None
lunch_break_state = {
    "active": False,
    "started_at": None,
    "return_at": None,
    "start_message": None,
    "return_message": None,
    "day_name": None,
    "force_test_url": False,
}
crm_runtime = {
    "running": False,
    "startedAt": None,
    "completedAt": None,
    "lastAction": None,
    "lastMessage": "No CRM runs yet.",
    "lastSuccess": None,
    "dryRun": False,
    "attempt": 0,
    "attemptsPlanned": 0,
}
crm_address_runtime = {
    "running": False,
    "startedAt": None,
    "completedAt": None,
    "lastAction": None,
    "targetOrderId": None,
    "activeFilter": "free",
    "listUrl": None,
    "batchSize": 1,
    "parallelWorkers": 1,
    "orderCount": 0,
    "refreshPasses": 0,
    "lastMessage": "No Address Validator runs yet.",
    "lastSuccess": None,
    "dryRun": False,
    "attempt": 0,
    "attemptsPlanned": 0,
}
crm_order_goods_runtime = {
    "running": False,
    "startedAt": None,
    "completedAt": None,
    "lastAction": None,
    "targetOrderId": None,
    "batchSize": None,
    "parallelWorkers": 1,
    "listUrl": None,
    "orderCount": 0,
    "currentOrderIndex": 0,
    "totalOrderCount": 0,
    "currentStage": None,
    "refreshPasses": 0,
    "lastMessage": "No Order Goods runs yet.",
    "lastSuccess": None,
    "dryRun": False,
    "payload": None,
}
crm_shipping_bypasser_runtime = {
    "running": False,
    "startedAt": None,
    "completedAt": None,
    "lastAction": None,
    "targetOrderId": None,
    "batchSize": None,
    "parallelWorkers": 1,
    "listUrl": None,
    "orderCount": 0,
    "currentOrderIndex": 0,
    "totalOrderCount": 0,
    "currentStage": None,
    "refreshPasses": 0,
    "lastMessage": "No Shipping Bypasser runs yet.",
    "lastSuccess": None,
    "dryRun": False,
    "payload": None,
}
crm_push_back_runtime = {
    "running": False,
    "startedAt": None,
    "completedAt": None,
    "lastAction": None,
    "targetOrderId": None,
    "batchSize": None,
    "parallelWorkers": 1,
    "listUrl": None,
    "orderCount": 0,
    "currentOrderIndex": 0,
    "totalOrderCount": 0,
    "currentStage": None,
    "refreshPasses": 0,
    "lastMessage": "No Push Back runs yet.",
    "lastSuccess": None,
    "dryRun": False,
    "payload": None,
}
crm_product_separator_runtime = {
    "running": False,
    "startedAt": None,
    "completedAt": None,
    "lastAction": None,
    "targetOrderId": None,
    "listMode": "rush",
    "listUrl": None,
    "orderCount": 0,
    "splitOrderCount": 0,
    "currentOrderIndex": 0,
    "totalOrderCount": 0,
    "parallelWorkers": 1,
    "lastMessage": "No Product Separator runs yet.",
    "lastSuccess": None,
    "dryRun": False,
    "payload": None,
}
crm_auto_splitter_runtime = {
    "running": False,
    "startedAt": None,
    "completedAt": None,
    "lastAction": None,
    "targetOrderId": None,
    "orderUrl": None,
    "tabCount": None,
    "divisions": None,
    "minimumTabs": 10,
    "lastMessage": "No Auto Splitter runs yet.",
    "lastSuccess": None,
    "dryRun": True,
    "payload": None,
}
crm_mass_emailer_runtime = {
    "running": False,
    "startedAt": None,
    "completedAt": None,
    "lastAction": None,
    "orderCount": 0,
    "failureCount": 0,
    "skippedCount": 0,
    "currentOrderIndex": 0,
    "totalOrderCount": 0,
    "currentStage": None,
    "lastMessage": "No Sheets Scanner runs yet.",
    "lastSuccess": None,
    "dryRun": True,
    "payload": None,
}
crm_processing_runtime = {
    "running": False,
    "startedAt": None,
    "completedAt": None,
    "currentStep": None,
    "processingFilter": "rush",
    "selectedSteps": [],
    "completedSteps": [],
    "currentOrderProgress": None,
    "lastMessage": "No automated processing runs yet.",
    "lastSuccess": None,
}
active_automation_processes = {}
force_stopped_process_pids = set()
automation_stop_requested_at = 0.0
automation_stop_block_until = 0.0
automation_queue_lock = threading.RLock()
automation_queue_condition = threading.Condition(automation_queue_lock)
automation_queue_tasks = []
automation_queue_worker_started = False
automation_queue_current_task_id = None
DAY_NAMES = ["SUNDAY", "MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY"]
AUTOMATION_TEST_CATALOG = [
    {"id": "paycom_in", "label": "Paycom In (Dry Run)", "kind": "paycom", "action": "in", "default": True},
    {"id": "paycom_out", "label": "Paycom Out (Dry Run)", "kind": "paycom", "action": "out", "default": True},
    {"id": "slack_in", "label": "Slack In", "kind": "slack", "action": "in", "default": True},
    {"id": "slack_out", "label": "Slack Out", "kind": "slack", "action": "out", "default": True},
    {
        "id": "slack_lunch",
        "label": "Slack Lunch (Test Channel)",
        "kind": "slack_lunch",
        "action": "lunch",
        "default": False,
    },
]
AUTOMATION_TEST_IDS = [x["id"] for x in AUTOMATION_TEST_CATALOG]
DESKTOP_METRICS_INIT_RETRY_SECONDS = 30
LHM_LISTENER_DEFAULT_PORT = 8085
LHM_LISTENER_REQUEST_TIMEOUT_SECONDS = 0.6
LHM_LISTENER_AUTOSTART_RETRY_SECONDS = 60
LHM_LISTENER_AUTOSTART_WAIT_SECONDS = 2.5
LHM_CONFIG_TEMP_INDEX_RE = re.compile(r"^/amdcpu/0/temperature/(\d+)/values$")
LHM_CONFIG_CPU_TEMP_INDEX = 2
LHM_CONFIG_CPU_PACKAGE_INDEX = 3
desktop_metrics_lock = threading.RLock()
desktop_metrics_runtime = {
    "ready": False,
    "computer": None,
    "hardware": None,
    "psutil": None,
    "error": None,
    "lhm_path": None,
    "last_init_attempt_at": 0.0,
    "last_lhm_launch_attempt_at": 0.0,
    "last_lhm_launch_error": None,
    "lhm_autostart_in_flight": False,
}


def _audit_result(automation_name, success, message):
    log_automation_result(automation_name, success, message, source="server.py")


def _resolve_console_python():
    p = sys.executable.strip().strip('"').strip("'")
    if os.path.basename(p).lower() == "pythonw.exe":
        p = os.path.join(os.path.dirname(p), "python.exe")
    return os.path.normpath(p)


def _resolve_windowless_python():
    p = sys.executable.strip().strip('"').strip("'")
    base = os.path.basename(p).lower()
    if base == "pythonw.exe":
        return os.path.normpath(p)
    if base == "python.exe":
        candidate = os.path.join(os.path.dirname(p), "pythonw.exe")
        if os.path.exists(candidate):
            return os.path.normpath(candidate)
    return os.path.normpath(p)


def _safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _normalize_duration_seconds(value):
    if value is None or value == "":
        return None
    return round(max(0.0, _safe_float(value, 0.0)), 1)


def _normalize_stage_timings(items, limit=50):
    rows = items if isinstance(items, list) else []
    cleaned = []
    for item in rows[: max(1, int(limit or 50))]:
        if not isinstance(item, dict):
            continue
        stage = str(item.get("stage") or "").strip()
        if not stage:
            continue
        row = {
            "stage": stage,
            "duration_seconds": _normalize_duration_seconds(item.get("duration_seconds")),
        }
        for key in (
            "refresh_pass",
            "order_count",
            "worker_count",
            "attempt",
            "retryable",
            "success",
        ):
            if key in item:
                row[key] = item.get(key)
        cleaned.append(row)
    return cleaned


def _runtime_duration_seconds(runtime):
    if not isinstance(runtime, dict):
        return None
    started = runtime.get("startedAt")
    if not started:
        return None
    try:
        start_dt = datetime.fromisoformat(str(started))
        end_raw = runtime.get("completedAt")
        end_dt = datetime.fromisoformat(str(end_raw)) if end_raw else datetime.now()
    except Exception:
        return None
    return _normalize_duration_seconds((end_dt - start_dt).total_seconds())


def _is_trueish(value):
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _tail_text_file(path, max_lines=300, max_bytes=512 * 1024):
    try:
        max_lines = max(1, int(max_lines))
    except (TypeError, ValueError):
        max_lines = 300
    max_lines = min(max_lines, 1000)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes), os.SEEK_SET)
            raw = f.read()
    except OSError:
        return []
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    return lines[-max_lines:]


def get_console_log_payload(max_lines=300):
    try:
        max_lines = int(max_lines)
    except (TypeError, ValueError):
        max_lines = 300
    max_lines = min(max(max_lines, 50), 1000)
    lines = _tail_text_file(AUDIT_LOG_FILE, max_lines=max_lines)
    return {
        "success": True,
        "lines": lines,
        "line_count": len(lines),
        "max_lines": max_lines,
        "path": AUDIT_LOG_FILE,
        "updated_at": datetime.now().isoformat(),
    }


def _desktop_metrics_payload_template():
    return {
        "available": False,
        "permissionRequired": False,
        "error": None,
        "cpuTempC": None,
        "gpuTempC": None,
        "cpuPackageTempC": None,
        "gpuHotspotTempC": None,
        "cpuUsagePercent": None,
        "gpuUsagePercent": None,
        "ramUsagePercent": None,
        "lhmPath": None,
        "cpuTempSource": None,
        "cpuPackageTempSource": None,
        "gpuTempSource": None,
        "gpuHotspotTempSource": None,
        "gpuUsageSource": None,
        "sensorNotes": [],
    }


def _desktop_metrics_error_payload(message, permission_required=False):
    payload = _desktop_metrics_payload_template()
    payload["available"] = False
    payload["permissionRequired"] = bool(permission_required)
    payload["error"] = str(message)
    payload["lhmPath"] = desktop_metrics_runtime.get("lhm_path")
    return payload


def _resolve_lhm_install_path():
    candidate_paths = []
    env_path = os.environ.get("LHM_PATH")
    if env_path:
        candidate_paths.append(Path(env_path))

    candidate_paths.extend(
        [
            Path(SCRIPT_DIR) / "LibreHardwareMonitor",
            Path(SCRIPT_DIR),
            Path(r"C:\LibreHardwareMonitor"),
        ]
    )

    for candidate in candidate_paths:
        dll_path = candidate / "LibreHardwareMonitorLib.dll"
        if dll_path.exists():
            return candidate
    return None


def _init_desktop_metrics_locked(force=False):
    if desktop_metrics_runtime["ready"]:
        return True, None

    now_ts = time.time()
    last_attempt = _safe_float(desktop_metrics_runtime.get("last_init_attempt_at"), 0.0)
    if not force and (now_ts - last_attempt) < DESKTOP_METRICS_INIT_RETRY_SECONDS:
        return False, desktop_metrics_runtime.get("error") or "Desktop metrics initialization failed."
    desktop_metrics_runtime["last_init_attempt_at"] = now_ts
    desktop_metrics_runtime["error"] = None

    try:
        import psutil as psutil_module
    except Exception as e:
        msg = f"psutil import failed: {e}"
        desktop_metrics_runtime["error"] = msg
        return False, msg

    try:
        import clr
    except Exception as e:
        msg = f"pythonnet import failed: {e}"
        desktop_metrics_runtime["error"] = msg
        return False, msg

    lhm_path = _resolve_lhm_install_path()
    if lhm_path is None:
        msg = (
            "LibreHardwareMonitorLib.dll not found. Set LHM_PATH to the folder containing the DLL "
            "or place the LibreHardwareMonitor folder under the Automation directory."
        )
        desktop_metrics_runtime["error"] = msg
        desktop_metrics_runtime["lhm_path"] = None
        return False, msg

    try:
        path_str = str(lhm_path)
        if path_str not in sys.path:
            sys.path.append(path_str)
        clr.AddReference(str(lhm_path / "LibreHardwareMonitorLib.dll"))
        from LibreHardwareMonitor import Hardware

        computer = Hardware.Computer()
        computer.IsCpuEnabled = True
        computer.IsGpuEnabled = True
        computer.IsMemoryEnabled = True
        computer.IsMotherboardEnabled = True
        computer.Open()
    except Exception as e:
        msg = f"LibreHardwareMonitor initialization failed: {e}"
        desktop_metrics_runtime["error"] = msg
        desktop_metrics_runtime["lhm_path"] = str(lhm_path)
        return False, msg

    desktop_metrics_runtime["ready"] = True
    desktop_metrics_runtime["computer"] = computer
    desktop_metrics_runtime["hardware"] = Hardware
    desktop_metrics_runtime["psutil"] = psutil_module
    desktop_metrics_runtime["error"] = None
    desktop_metrics_runtime["lhm_path"] = str(lhm_path)
    logger.info("Desktop metrics initialized from %s", lhm_path)
    return True, None


def _iter_desktop_hardware_nodes(nodes):
    for hw in nodes:
        yield hw
        for sub in _iter_desktop_hardware_nodes(hw.SubHardware):
            yield sub


def _update_desktop_hardware(computer):
    for hw in _iter_desktop_hardware_nodes(computer.Hardware):
        hw.Update()


def _iter_desktop_sensors(computer, sensor_type_name=None, hardware_type_contains=None):
    hw_contains = (hardware_type_contains or "").lower()
    for hw in _iter_desktop_hardware_nodes(computer.Hardware):
        hw_type = str(hw.HardwareType)
        if hw_contains and hw_contains not in hw_type.lower():
            continue
        for sensor in hw.Sensors:
            sensor_type = str(sensor.SensorType)
            if sensor_type_name and sensor_type != sensor_type_name:
                continue
            value = None
            if sensor.Value is not None:
                try:
                    value = float(sensor.Value)
                except Exception:
                    value = None
            yield {
                "hardware_type": hw_type,
                "sensor_type": sensor_type,
                "sensor_name": str(sensor.Name),
                "value": value,
            }


def _is_valid_temperature(value):
    try:
        val = float(value)
    except (TypeError, ValueError):
        return False
    return 0.1 <= val <= 150.0


def _sensor_name_has_keyword(row, keywords):
    name = str(row.get("sensor_name") or "").lower()
    return any((k or "").lower() in name for k in (keywords or []))


def _pick_sensor_row(rows, keywords=None, validator=None, choose_highest=False):
    if validator is None:
        validator = lambda v: v is not None
    valid_rows = [r for r in rows if validator(r.get("value"))]
    if not valid_rows:
        return None

    if keywords:
        keyword_rows = [r for r in valid_rows if _sensor_name_has_keyword(r, keywords)]
        if keyword_rows:
            if choose_highest:
                return max(keyword_rows, key=lambda r: float(r.get("value") or 0.0))
            return keyword_rows[0]

    if choose_highest:
        return max(valid_rows, key=lambda r: float(r.get("value") or 0.0))
    return valid_rows[0]


def _extract_first_number(value):
    m = re.search(r"-?\d+(?:\.\d+)?", str(value or ""))
    if not m:
        return None
    try:
        return float(m.group(0))
    except (TypeError, ValueError):
        return None


def _parse_temperature_to_celsius(raw_value):
    text = str(raw_value or "").strip()
    num = _extract_first_number(text)
    if num is None:
        return None
    low = text.lower().replace("°", "")
    if " fahrenheit" in low or low.endswith("f") or " f" in low:
        return (num - 32.0) * (5.0 / 9.0)
    if " celsius" in low or low.endswith("c") or " c" in low:
        return num
    if num > 120.0:
        return (num - 32.0) * (5.0 / 9.0)
    return num


def _format_lhm_listener_source(row):
    path = row.get("path") or []
    cleaned = [str(x).strip() for x in path if str(x).strip()]
    if cleaned:
        return "LHM Listener / " + " / ".join(cleaned)
    name = str(row.get("text") or "").strip()
    if name:
        return f"LHM Listener / {name}"
    return "LHM Listener"


def _iter_lhm_listener_nodes(node, path=None):
    if not isinstance(node, dict):
        return
    base_path = list(path or [])
    text = str(node.get("Text") or "").strip()
    current_path = base_path + ([text] if text else [])
    children = node.get("Children") if isinstance(node.get("Children"), list) else []
    yield {
        "text": text,
        "value": node.get("Value"),
        "min": node.get("Min"),
        "max": node.get("Max"),
        "children": children,
        "path": current_path,
    }
    for child in children:
        for nested in _iter_lhm_listener_nodes(child, current_path):
            yield nested


def _is_lhm_temperature_node(row):
    path_low = " / ".join([str(x).lower() for x in (row.get("path") or [])])
    value_low = str(row.get("value") or "").lower()
    if "temperature" in path_low:
        return True
    if "°c" in value_low or "°f" in value_low:
        return True
    if re.search(r"\bc\b", value_low) or re.search(r"\bf\b", value_low):
        return True
    return False


def _is_lhm_cpu_row(row):
    text_low = str(row.get("text") or "").lower()
    path_low = " / ".join([str(x).lower() for x in (row.get("path") or [])])
    if any(x in text_low for x in ["tctl", "tdie", "package", "ccd", "iod"]):
        return True
    if any(x in path_low for x in ["nvidia", "radeon", "gpu"]):
        return False
    return any(x in path_low for x in ["cpu", "ryzen", "intel"])


def _is_lhm_gpu_row(row):
    text_low = str(row.get("text") or "").lower()
    path_low = " / ".join([str(x).lower() for x in (row.get("path") or [])])
    if "gpu" in text_low:
        return True
    return any(x in path_low for x in ["gpu", "nvidia", "radeon"])


def _parse_lhm_listener_config(lhm_path):
    cfg_path = Path(str(lhm_path or "")) / "LibreHardwareMonitor.config"
    if not cfg_path.exists():
        return None, None, f"LibreHardwareMonitor.config not found at {cfg_path}"
    try:
        root = ET.parse(cfg_path).getroot()
        values = {}
        for add in root.findall(".//add"):
            key = str(add.get("key") or "")
            if key in {"listenerIp", "listenerPort"}:
                values[key] = str(add.get("value") or "").strip()
        listener_ip = values.get("listenerIp", "").strip()
        listener_port = int(values.get("listenerPort") or LHM_LISTENER_DEFAULT_PORT)
        return listener_ip, listener_port, None
    except Exception as e:
        return None, None, f"Failed to parse {cfg_path}: {e}"


def _decode_lhm_plot_latest_float(encoded_value, validator=None):
    text = str(encoded_value or "").strip()
    if not text:
        return None
    try:
        raw = gzip.decompress(base64.b64decode(text))
    except Exception:
        return None

    if len(raw) <= 8:
        return None

    # LHM stores plot history as an 8-byte header followed by repeated records:
    # 4-byte float value + 8-byte timestamp/counter.
    body = raw[8:]
    rec_count = len(body) // 12
    latest = None
    for i in range(rec_count):
        chunk = body[i * 12 : (i + 1) * 12]
        try:
            value = float(struct.unpack("<f", chunk[:4])[0])
        except Exception:
            continue
        if not math.isfinite(value):
            continue
        if validator and not validator(value):
            continue
        latest = value
    return latest


def _read_lhm_config_metrics(lhm_path):
    cfg_path = Path(str(lhm_path or "")) / "LibreHardwareMonitor.config"
    if not cfg_path.exists():
        return {"ok": False, "error": f"Config fallback unavailable: {cfg_path} was not found."}

    try:
        cfg_mtime_ts = cfg_path.stat().st_mtime
        cfg_updated_at = datetime.fromtimestamp(cfg_mtime_ts)
    except Exception:
        cfg_mtime_ts = None
        cfg_updated_at = None

    try:
        root = ET.parse(cfg_path).getroot()
    except Exception as e:
        return {"ok": False, "error": f"Config fallback failed to parse {cfg_path}: {e}"}

    encoded_by_index = {}
    for add in root.findall(".//add"):
        key = str(add.get("key") or "")
        m = LHM_CONFIG_TEMP_INDEX_RE.match(key)
        if not m:
            continue
        try:
            idx = int(m.group(1))
        except (TypeError, ValueError):
            continue
        encoded_by_index[idx] = str(add.get("value") or "")

    if not encoded_by_index:
        return {"ok": False, "error": "Config fallback found no Ryzen temperature history entries."}

    cpu_temp = _decode_lhm_plot_latest_float(
        encoded_by_index.get(LHM_CONFIG_CPU_TEMP_INDEX),
        validator=_is_valid_temperature,
    )
    cpu_package_temp = _decode_lhm_plot_latest_float(
        encoded_by_index.get(LHM_CONFIG_CPU_PACKAGE_INDEX),
        validator=_is_valid_temperature,
    )

    if cpu_temp is None and cpu_package_temp is not None:
        cpu_temp = cpu_package_temp
    if cpu_package_temp is None and cpu_temp is not None:
        cpu_package_temp = cpu_temp

    if cpu_temp is None and cpu_package_temp is None:
        return {"ok": False, "error": "Config fallback parsed no valid CPU temperature samples."}

    cfg_age_seconds = None
    if cfg_mtime_ts is not None:
        cfg_age_seconds = max(0.0, time.time() - cfg_mtime_ts)

    return {
        "ok": True,
        "cpuTempC": cpu_temp,
        "cpuTempSource": f"LHM Config / /amdcpu/0/temperature/{LHM_CONFIG_CPU_TEMP_INDEX}/values",
        "cpuPackageTempC": cpu_package_temp,
        "cpuPackageTempSource": f"LHM Config / /amdcpu/0/temperature/{LHM_CONFIG_CPU_PACKAGE_INDEX}/values",
        "configUpdatedAt": cfg_updated_at.isoformat() if cfg_updated_at else None,
        "configAgeSeconds": cfg_age_seconds,
    }


def _candidate_listener_hosts(listener_ip):
    raw = str(listener_ip or "").strip()
    if not raw:
        return []
    if raw in {"?", "disabled"}:
        return []
    if raw in {"*", "+", "0.0.0.0", "::", "[::]"}:
        return ["127.0.0.1"]
    if raw in {"localhost", "127.0.0.1"}:
        return ["127.0.0.1"]
    hosts = [raw]
    if "127.0.0.1" not in hosts:
        hosts.append("127.0.0.1")
    return hosts


def _maybe_start_lhm_listener_process(lhm_path):
    path_obj = Path(str(lhm_path or ""))
    exe_path = path_obj / "LibreHardwareMonitor.exe"
    if not exe_path.exists():
        return False, f"Could not auto-start LHM listener. Missing {exe_path}"

    now_ts = time.time()
    with desktop_metrics_lock:
        last_attempt = _safe_float(desktop_metrics_runtime.get("last_lhm_launch_attempt_at"), 0.0)
        if (now_ts - last_attempt) < LHM_LISTENER_AUTOSTART_RETRY_SECONDS:
            remaining = int(max(0, LHM_LISTENER_AUTOSTART_RETRY_SECONDS - (now_ts - last_attempt)))
            return False, f"Skipping auto-start retry for {remaining}s (cooldown)."
        desktop_metrics_runtime["last_lhm_launch_attempt_at"] = now_ts

    try:
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen([str(exe_path)], cwd=str(path_obj), creationflags=flags)
        time.sleep(LHM_LISTENER_AUTOSTART_WAIT_SECONDS)
        with desktop_metrics_lock:
            desktop_metrics_runtime["last_lhm_launch_error"] = None
        return True, f"Started {exe_path.name} and waited {LHM_LISTENER_AUTOSTART_WAIT_SECONDS:.1f}s."
    except OSError as e:
        if os.name == "nt" and getattr(e, "winerror", None) == 740:
            # WinError 740 means elevation is required. Shell launch can trigger UAC.
            try:
                os.startfile(str(exe_path))
                time.sleep(LHM_LISTENER_AUTOSTART_WAIT_SECONDS)
                with desktop_metrics_lock:
                    desktop_metrics_runtime["last_lhm_launch_error"] = None
                return (
                    True,
                    f"Requested launch of {exe_path.name} via Windows shell (UAC approval may be required).",
                )
            except Exception as shell_err:
                err = f"Auto-start of {exe_path} requires elevation and shell launch failed: {shell_err}"
                with desktop_metrics_lock:
                    desktop_metrics_runtime["last_lhm_launch_error"] = err
                return False, err
        err = f"Auto-start of {exe_path} failed: {e}"
        with desktop_metrics_lock:
            desktop_metrics_runtime["last_lhm_launch_error"] = err
        return False, err
    except Exception as e:
        err = f"Auto-start of {exe_path} failed: {e}"
        with desktop_metrics_lock:
            desktop_metrics_runtime["last_lhm_launch_error"] = err
        return False, err


def _request_lhm_listener_autostart_async(lhm_path):
    with desktop_metrics_lock:
        if desktop_metrics_runtime.get("lhm_autostart_in_flight"):
            return False, "Auto-start already in progress."
        last_attempt = _safe_float(desktop_metrics_runtime.get("last_lhm_launch_attempt_at"), 0.0)
        now_ts = time.time()
        if (now_ts - last_attempt) < LHM_LISTENER_AUTOSTART_RETRY_SECONDS:
            return False, "Auto-start retry cooldown active."
        desktop_metrics_runtime["lhm_autostart_in_flight"] = True

    def _runner():
        try:
            _maybe_start_lhm_listener_process(lhm_path)
        finally:
            with desktop_metrics_lock:
                desktop_metrics_runtime["lhm_autostart_in_flight"] = False

    threading.Thread(target=_runner, daemon=True).start()
    return True, "Requested background auto-start of LibreHardwareMonitor listener."


def _load_lhm_listener_json(listener_ip, listener_port, lhm_path=None, auto_start=False):
    hosts = _candidate_listener_hosts(listener_ip)
    if not hosts:
        return None, None, "LHM web listener is disabled (listenerIp is '?')."

    def _try_hosts():
        errs = []
        for host in hosts:
            url = f"http://{host}:{listener_port}/data.json"
            req = urllib.request.Request(url, headers={"User-Agent": "AutomationServer/desktop-metrics"})
            try:
                with urllib.request.urlopen(req, timeout=LHM_LISTENER_REQUEST_TIMEOUT_SECONDS) as resp:
                    body = resp.read()
                decoded = body.decode("utf-8", errors="replace")
                payload = json.loads(decoded)
                return payload, url, None
            except Exception as e:
                errs.append(f"{url}: {e}")
        return None, None, errs

    payload, url, errors = _try_hosts()
    if payload is not None:
        return payload, url, None

    start_note = ""
    if auto_start:
        started, start_msg = _maybe_start_lhm_listener_process(lhm_path)
        start_note = f" Auto-start: {start_msg}"
        payload, url, retry_errors = _try_hosts()
        if payload is not None:
            return payload, url, None
        errors = list(errors or []) + list(retry_errors or [])

    return None, None, "LHM listener was not reachable. " + " | ".join(errors or []) + start_note


def _pick_lhm_listener_row(rows, keywords=None, choose_highest=False):
    if not rows:
        return None
    selected = rows
    if keywords:
        keys = [str(k or "").lower() for k in keywords if str(k or "").strip()]
        named = []
        for row in rows:
            text_low = str(row.get("text") or "").lower()
            if any(k in text_low for k in keys):
                named.append(row)
        if named:
            selected = named
    if choose_highest:
        return max(selected, key=lambda r: float(r.get("temp_c") or 0.0))
    return selected[0]


def _read_lhm_listener_metrics(lhm_path):
    listener_ip, listener_port, cfg_error = _parse_lhm_listener_config(lhm_path)
    if cfg_error:
        return {"ok": False, "error": cfg_error}

    payload, url, load_error = _load_lhm_listener_json(
        listener_ip,
        listener_port,
        lhm_path=lhm_path,
        auto_start=False,
    )
    if payload is None:
        return {"ok": False, "error": load_error}

    temp_rows = []
    for row in _iter_lhm_listener_nodes(payload):
        if row.get("children"):
            continue
        if not _is_lhm_temperature_node(row):
            continue
        temp_c = _parse_temperature_to_celsius(row.get("value"))
        if temp_c is None or not _is_valid_temperature(temp_c):
            continue
        row_copy = dict(row)
        row_copy["temp_c"] = float(temp_c)
        temp_rows.append(row_copy)

    cpu_rows = [r for r in temp_rows if _is_lhm_cpu_row(r)]
    gpu_rows = [r for r in temp_rows if _is_lhm_gpu_row(r)]

    cpu_temp_row = _pick_lhm_listener_row(
        cpu_rows,
        keywords=["core (tctl/tdie)", "tctl/tdie", "tctl", "tdie", "cpu", "core"],
    )
    cpu_package_row = _pick_lhm_listener_row(cpu_rows, keywords=["package"])
    if cpu_temp_row is None and cpu_package_row is not None:
        cpu_temp_row = cpu_package_row
    if cpu_package_row is None and cpu_temp_row is not None and "package" in str(cpu_temp_row.get("text") or "").lower():
        cpu_package_row = cpu_temp_row

    gpu_temp_row = _pick_lhm_listener_row(gpu_rows, keywords=["gpu core", "edge", "gpu"])
    gpu_hotspot_row = _pick_lhm_listener_row(
        gpu_rows,
        keywords=["hot spot", "hotspot", "junction", "memory junction"],
        choose_highest=True,
    )
    if gpu_hotspot_row is None and gpu_temp_row is not None:
        gpu_hotspot_row = gpu_temp_row

    return {
        "ok": True,
        "listenerUrl": url,
        "cpuTempC": cpu_temp_row.get("temp_c") if cpu_temp_row else None,
        "cpuTempSource": _format_lhm_listener_source(cpu_temp_row) if cpu_temp_row else None,
        "cpuPackageTempC": cpu_package_row.get("temp_c") if cpu_package_row else None,
        "cpuPackageTempSource": _format_lhm_listener_source(cpu_package_row) if cpu_package_row else None,
        "gpuTempC": gpu_temp_row.get("temp_c") if gpu_temp_row else None,
        "gpuTempSource": _format_lhm_listener_source(gpu_temp_row) if gpu_temp_row else None,
        "gpuHotspotTempC": gpu_hotspot_row.get("temp_c") if gpu_hotspot_row else None,
        "gpuHotspotTempSource": _format_lhm_listener_source(gpu_hotspot_row) if gpu_hotspot_row else None,
    }


def read_desktop_metrics():
    with desktop_metrics_lock:
        ready, init_error = _init_desktop_metrics_locked()
        if not ready:
            return _desktop_metrics_error_payload(init_error or "Desktop metrics is unavailable.")

        computer = desktop_metrics_runtime["computer"]
        psutil_module = desktop_metrics_runtime["psutil"]

        try:
            _update_desktop_hardware(computer)

            cpu_temp_rows = list(_iter_desktop_sensors(computer, "Temperature", hardware_type_contains="Cpu"))
            gpu_temp_rows = list(_iter_desktop_sensors(computer, "Temperature", hardware_type_contains="Gpu"))
            gpu_load_rows = list(_iter_desktop_sensors(computer, "Load", hardware_type_contains="Gpu"))

            cpu_temp_row = _pick_sensor_row(
                cpu_temp_rows,
                keywords=["package", "tctl", "tdie", "core", "cpu"],
                validator=_is_valid_temperature,
            )
            if cpu_temp_row is None:
                cpu_temp_row = _pick_sensor_row(cpu_temp_rows, validator=_is_valid_temperature, choose_highest=True)

            cpu_package_row = _pick_sensor_row(
                cpu_temp_rows,
                keywords=["package"],
                validator=_is_valid_temperature,
            )
            if cpu_package_row is None and cpu_temp_row is not None:
                cpu_package_row = cpu_temp_row

            gpu_temp_row = _pick_sensor_row(
                gpu_temp_rows,
                keywords=["gpu core", "core", "edge"],
                validator=_is_valid_temperature,
            )
            if gpu_temp_row is None:
                gpu_temp_row = _pick_sensor_row(gpu_temp_rows, validator=_is_valid_temperature, choose_highest=True)

            gpu_hotspot_row = _pick_sensor_row(
                gpu_temp_rows,
                keywords=["hot spot", "hotspot", "junction"],
                validator=_is_valid_temperature,
                choose_highest=True,
            )
            if gpu_hotspot_row is None and gpu_temp_row is not None:
                gpu_hotspot_row = gpu_temp_row

            gpu_usage_row = _pick_sensor_row(
                gpu_load_rows,
                keywords=["gpu core", "core"],
                validator=lambda v: v is not None,
            )
            if gpu_usage_row is None:
                gpu_usage_row = _pick_sensor_row(
                    gpu_load_rows,
                    keywords=["d3d 3d", "d3d"],
                    validator=lambda v: v is not None,
                    choose_highest=True,
                )
            if gpu_usage_row is None:
                gpu_usage_row = _pick_sensor_row(gpu_load_rows, validator=lambda v: v is not None, choose_highest=True)

            cpu_usage = psutil_module.cpu_percent(interval=0.3)
            ram_usage = psutil_module.virtual_memory().percent

            cpu_temp = cpu_temp_row.get("value") if cpu_temp_row else None
            cpu_package_temp = cpu_package_row.get("value") if cpu_package_row else None
            gpu_temp = gpu_temp_row.get("value") if gpu_temp_row else None
            gpu_hotspot_temp = gpu_hotspot_row.get("value") if gpu_hotspot_row else None
            gpu_usage = gpu_usage_row.get("value") if gpu_usage_row else None

            cpu_temp_source = f"{cpu_temp_row.get('hardware_type')} / {cpu_temp_row.get('sensor_name')}" if cpu_temp_row else None
            cpu_package_source = (
                f"{cpu_package_row.get('hardware_type')} / {cpu_package_row.get('sensor_name')}" if cpu_package_row else None
            )
            gpu_temp_source = f"{gpu_temp_row.get('hardware_type')} / {gpu_temp_row.get('sensor_name')}" if gpu_temp_row else None
            gpu_hotspot_source = (
                f"{gpu_hotspot_row.get('hardware_type')} / {gpu_hotspot_row.get('sensor_name')}" if gpu_hotspot_row else None
            )

            lhm_path = desktop_metrics_runtime.get("lhm_path")
            listener_result = None
            listener_used_fields = []
            listener_autostart_note = None
            if cpu_temp is None or cpu_package_temp is None or gpu_hotspot_temp is None:
                listener_result = _read_lhm_listener_metrics(lhm_path)
                if listener_result.get("ok"):
                    if cpu_temp is None and listener_result.get("cpuTempC") is not None:
                        cpu_temp = float(listener_result.get("cpuTempC"))
                        cpu_temp_source = listener_result.get("cpuTempSource")
                        listener_used_fields.append("CPU temp")
                    if cpu_package_temp is None and listener_result.get("cpuPackageTempC") is not None:
                        cpu_package_temp = float(listener_result.get("cpuPackageTempC"))
                        cpu_package_source = listener_result.get("cpuPackageTempSource")
                        listener_used_fields.append("CPU package")
                    if gpu_temp is None and listener_result.get("gpuTempC") is not None:
                        gpu_temp = float(listener_result.get("gpuTempC"))
                        gpu_temp_source = listener_result.get("gpuTempSource")
                    if gpu_hotspot_temp is None and listener_result.get("gpuHotspotTempC") is not None:
                        gpu_hotspot_temp = float(listener_result.get("gpuHotspotTempC"))
                        gpu_hotspot_source = listener_result.get("gpuHotspotTempSource")
                        listener_used_fields.append("GPU hotspot")
                else:
                    needs_listener = cpu_temp is None or cpu_package_temp is None or gpu_hotspot_temp is None
                    if needs_listener:
                        started, start_note = _request_lhm_listener_autostart_async(lhm_path)
                        if started:
                            listener_autostart_note = start_note

            config_result = None
            config_used_fields = []
            if cpu_temp is None or cpu_package_temp is None:
                config_result = _read_lhm_config_metrics(lhm_path)
                if config_result.get("ok"):
                    if cpu_temp is None and config_result.get("cpuTempC") is not None:
                        cpu_temp = float(config_result.get("cpuTempC"))
                        cpu_temp_source = config_result.get("cpuTempSource")
                        config_used_fields.append("CPU temp")
                    if cpu_package_temp is None and config_result.get("cpuPackageTempC") is not None:
                        cpu_package_temp = float(config_result.get("cpuPackageTempC"))
                        cpu_package_source = config_result.get("cpuPackageTempSource")
                        config_used_fields.append("CPU package")

            sensor_notes = []
            raw_cpu_temp_row = _pick_sensor_row(cpu_temp_rows, keywords=["core", "cpu", "package"], validator=lambda v: v is not None)
            if cpu_temp is None:
                if raw_cpu_temp_row is not None:
                    raw_name = raw_cpu_temp_row.get("sensor_name")
                    raw_val = raw_cpu_temp_row.get("value")
                    sensor_notes.append(f"CPU temp sensor '{raw_name}' reported {raw_val}; treated as unavailable.")
                else:
                    sensor_notes.append("No CPU temperature sensor is currently exposed by LibreHardwareMonitor.")
            if cpu_package_temp is None:
                sensor_notes.append("CPU package temperature is not currently exposed by LibreHardwareMonitor on this system.")
            if gpu_hotspot_temp is None:
                sensor_notes.append("GPU hotspot/junction temperature is not currently exposed by LibreHardwareMonitor on this system.")
            if listener_used_fields and listener_result and listener_result.get("listenerUrl"):
                used_text = ", ".join(listener_used_fields)
                sensor_notes.append(
                    f"Using LHM web listener fallback ({listener_result.get('listenerUrl')}) for {used_text}."
                )
            elif (
                listener_result
                and not listener_result.get("ok")
                and (cpu_temp is None or cpu_package_temp is None or gpu_hotspot_temp is None)
            ):
                sensor_notes.append(
                    "LHM web listener fallback unavailable. "
                    "Enable LibreHardwareMonitor Web Server for full Ryzen CPU sensors. "
                    f"Details: {listener_result.get('error')}"
                )
            if listener_autostart_note:
                sensor_notes.append(listener_autostart_note)
            if config_used_fields and config_result:
                used_text = ", ".join(config_used_fields)
                updated_at = config_result.get("configUpdatedAt") or "unknown"
                age_seconds = config_result.get("configAgeSeconds")
                age_text = "unknown age"
                if age_seconds is not None:
                    age_text = f"{int(age_seconds)}s old"
                sensor_notes.append(
                    f"Using LHM config-history fallback for {used_text} "
                    f"(config updated: {updated_at}, {age_text})."
                )
            elif (
                config_result
                and not config_result.get("ok")
                and (cpu_temp is None or cpu_package_temp is None)
            ):
                sensor_notes.append(f"LHM config-history fallback unavailable. Details: {config_result.get('error')}")

            payload = _desktop_metrics_payload_template()
            payload.update(
                {
                    "available": True,
                    "permissionRequired": False,
                    "error": None,
                    "cpuTempC": round(float(cpu_temp), 1) if cpu_temp is not None else None,
                    "gpuTempC": round(float(gpu_temp), 1) if gpu_temp is not None else None,
                    "cpuPackageTempC": round(float(cpu_package_temp), 1) if cpu_package_temp is not None else None,
                    "gpuHotspotTempC": round(float(gpu_hotspot_temp), 1) if gpu_hotspot_temp is not None else None,
                    "cpuUsagePercent": round(float(cpu_usage), 1),
                    "gpuUsagePercent": round(float(gpu_usage), 1) if gpu_usage is not None else None,
                    "ramUsagePercent": round(float(ram_usage), 1),
                    "lhmPath": lhm_path,
                    "cpuTempSource": cpu_temp_source,
                    "cpuPackageTempSource": cpu_package_source,
                    "gpuTempSource": gpu_temp_source,
                    "gpuHotspotTempSource": gpu_hotspot_source,
                    "gpuUsageSource": (
                        f"{gpu_usage_row.get('hardware_type')} / {gpu_usage_row.get('sensor_name')}" if gpu_usage_row else None
                    ),
                    "sensorNotes": sensor_notes,
                }
            )
            return payload
        except PermissionError as e:
            return _desktop_metrics_error_payload(e, permission_required=True)
        except Exception as e:
            desktop_metrics_runtime["error"] = str(e)
            return _desktop_metrics_error_payload(e, permission_required=False)


def close_desktop_metrics_runtime():
    with desktop_metrics_lock:
        computer = desktop_metrics_runtime.get("computer")
        if computer is not None:
            try:
                computer.Close()
            except Exception:
                pass

        desktop_metrics_runtime["ready"] = False
        desktop_metrics_runtime["computer"] = None
        desktop_metrics_runtime["hardware"] = None
        desktop_metrics_runtime["psutil"] = None


def _get_local_ipv4_addresses():
    addresses = set()
    try:
        hostname = socket.gethostname()
        for entry in socket.getaddrinfo(hostname, None, socket.AF_INET, socket.SOCK_STREAM):
            ip = str(entry[4][0] or "")
            if ip and not ip.startswith("169.254."):
                addresses.add(ip)
    except Exception:
        pass

    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.settimeout(0.5)
        probe.connect(("8.8.8.8", 80))
        ip = probe.getsockname()[0]
        if ip and not ip.startswith("169.254."):
            addresses.add(ip)
        probe.close()
    except Exception:
        pass

    addresses.add("127.0.0.1")
    return sorted(addresses, key=lambda x: (x.startswith("127."), x))


def get_server_runtime_payload():
    local_ips = _get_local_ipv4_addresses()
    ui_urls = [f"http://{ip}:{SERVER_PORT}/ui" for ip in local_ips]
    metrics_urls = [f"http://{ip}:{SERVER_PORT}/api/metrics" for ip in local_ips]
    uptime_seconds = max(0, int((datetime.now() - SERVER_STARTED_AT).total_seconds()))

    with desktop_metrics_lock:
        metrics_ready = bool(desktop_metrics_runtime.get("ready"))
        metrics_error = desktop_metrics_runtime.get("error")
        lhm_path = desktop_metrics_runtime.get("lhm_path")
        lhm_launch_error = desktop_metrics_runtime.get("last_lhm_launch_error")

    listener_ip, listener_port, listener_cfg_error = _parse_lhm_listener_config(lhm_path) if lhm_path else (None, None, None)
    listener_enabled = bool(listener_ip and str(listener_ip).strip() not in {"?", "disabled"})

    return {
        "success": True,
        "bindHost": SERVER_BIND_HOST,
        "port": SERVER_PORT,
        "bindAddress": f"{SERVER_BIND_HOST}:{SERVER_PORT}",
        "startedAt": SERVER_STARTED_AT.isoformat(),
        "uptimeSeconds": uptime_seconds,
        "pid": os.getpid(),
        "pythonExecutable": sys.executable,
        "cwd": SCRIPT_DIR,
        "localIpAddresses": local_ips,
        "uiUrls": ui_urls,
        "metricsUrls": metrics_urls,
        "metricsRuntimeReady": metrics_ready,
        "metricsRuntimeError": str(metrics_error) if metrics_error else None,
        "lhmPath": lhm_path,
        "lhmListenerIp": listener_ip,
        "lhmListenerPort": listener_port,
        "lhmListenerEnabled": listener_enabled,
        "lhmListenerConfigError": listener_cfg_error,
        "lhmLastLaunchError": lhm_launch_error,
    }


def _normalize_inline_text(value):
    return " ".join(str(value or "").replace("\xa0", " ").split())


def _is_placeholder_punch_text(text):
    raw = _normalize_inline_text(text)
    if not raw:
        return True
    low = raw.lower()
    if low in {"--", "??", "n/a", "na", "missing"}:
        return True
    if low in {
        "error_outline",
        "highlight_off",
        "warning",
        "warning_amber",
        "help_outline",
        "cancel",
        "report_problem",
    }:
        return True
    if "request new punch" in low or "forgot to clock in/out" in low:
        return True
    return False


def _clean_paycom_punch_value(value):
    raw = _normalize_inline_text(value)
    if _is_placeholder_punch_text(raw):
        return None
    return raw


def _apply_runtime_config_from_module():
    global WORK_CLOCK_CAPPED, WORK_CLOCK_CAP_HOURS, WORK_CLOCK_BREAK_MINUTES
    global WORK_CLOCK_BREAK_APPLIES_AFTER_HOURS
    global WORK_CLOCK_DEFAULT_DAILY_HOURS, WORK_CLOCK_AUTO_OUT_MAX_HOURS, WORK_CLOCK_STATE_FILE
    global WORK_CLOCK_RESET_ON_FRIDAY_CLOCK_OUT, WORK_CLOCK_SYNC_FROM_PAYCOM
    global WORK_CLOCK_SYNC_BEFORE_CLOCK_IN, WORK_CLOCK_SYNC_AFTER_CLOCK_OUT
    global WORK_STATE_FILE, CRM_MAX_RETRIES, CRM_RETRY_DELAY_SECONDS, CRM_ACTION_TIMEOUT
    global CRM_SHIPPING_ALL_URL, CRM_SHIPPING_813_URL, CRM_813_VALIDATOR_URL, CRM_813_ORDER_GOODS_URL, CRM_813_BYPASS_URL
    global CRM_PUSH_BACK_RUSH_URL, CRM_PUSH_BACK_813_URL
    global CRM_AUTO_SPLITTER_PREFLIGHT_REUSE_SECONDS

    WORK_CLOCK_CAPPED = bool(getattr(config_module, "WORK_CLOCK_CAPPED", WORK_CLOCK_CAPPED))
    WORK_CLOCK_CAP_HOURS = _safe_float(getattr(config_module, "WORK_CLOCK_CAP_HOURS", WORK_CLOCK_CAP_HOURS), 40.0)
    WORK_CLOCK_BREAK_MINUTES = int(getattr(config_module, "WORK_CLOCK_BREAK_MINUTES", WORK_CLOCK_BREAK_MINUTES))
    WORK_CLOCK_BREAK_APPLIES_AFTER_HOURS = _safe_float(
        getattr(config_module, "WORK_CLOCK_BREAK_APPLIES_AFTER_HOURS", WORK_CLOCK_BREAK_APPLIES_AFTER_HOURS),
        4.0,
    )
    WORK_CLOCK_DEFAULT_DAILY_HOURS = _safe_float(
        getattr(config_module, "WORK_CLOCK_DEFAULT_DAILY_HOURS", WORK_CLOCK_DEFAULT_DAILY_HOURS), 8.0
    )
    WORK_CLOCK_AUTO_OUT_MAX_HOURS = _safe_float(
        getattr(config_module, "WORK_CLOCK_AUTO_OUT_MAX_HOURS", WORK_CLOCK_AUTO_OUT_MAX_HOURS), 24.0
    )
    WORK_CLOCK_STATE_FILE = str(getattr(config_module, "WORK_CLOCK_STATE_FILE", WORK_CLOCK_STATE_FILE))
    WORK_CLOCK_RESET_ON_FRIDAY_CLOCK_OUT = bool(
        getattr(config_module, "WORK_CLOCK_RESET_ON_FRIDAY_CLOCK_OUT", WORK_CLOCK_RESET_ON_FRIDAY_CLOCK_OUT)
    )
    WORK_CLOCK_SYNC_FROM_PAYCOM = bool(
        getattr(config_module, "WORK_CLOCK_SYNC_FROM_PAYCOM", WORK_CLOCK_SYNC_FROM_PAYCOM)
    )
    WORK_CLOCK_SYNC_BEFORE_CLOCK_IN = bool(
        getattr(config_module, "WORK_CLOCK_SYNC_BEFORE_CLOCK_IN", WORK_CLOCK_SYNC_BEFORE_CLOCK_IN)
    )
    WORK_CLOCK_SYNC_AFTER_CLOCK_OUT = bool(
        getattr(config_module, "WORK_CLOCK_SYNC_AFTER_CLOCK_OUT", WORK_CLOCK_SYNC_AFTER_CLOCK_OUT)
    )
    WORK_STATE_FILE = resolve_runtime_file(WORK_CLOCK_STATE_FILE, STATE_DIR)
    CRM_MAX_RETRIES = max(0, int(getattr(config_module, "CRM_MAX_RETRIES", CRM_MAX_RETRIES)))
    CRM_RETRY_DELAY_SECONDS = max(0, int(getattr(config_module, "CRM_RETRY_DELAY_SECONDS", CRM_RETRY_DELAY_SECONDS)))
    CRM_ACTION_TIMEOUT = max(5, int(getattr(config_module, "CRM_ACTION_TIMEOUT", CRM_ACTION_TIMEOUT)))
    CRM_SHIPPING_ALL_URL = str(getattr(config_module, "CRM_SHIPPING_ALL_URL", CRM_SHIPPING_ALL_URL) or "").strip()
    CRM_SHIPPING_813_URL = str(getattr(config_module, "CRM_SHIPPING_813_URL", CRM_SHIPPING_813_URL) or "").strip()
    CRM_813_VALIDATOR_URL = str(getattr(config_module, "CRM_813_VALIDATOR_URL", CRM_SHIPPING_813_URL) or "").strip()
    CRM_813_ORDER_GOODS_URL = str(getattr(config_module, "CRM_813_ORDER_GOODS_URL", CRM_813_ORDER_GOODS_URL) or "").strip()
    CRM_813_BYPASS_URL = str(getattr(config_module, "CRM_813_BYPASS_URL", CRM_813_BYPASS_URL) or "").strip()
    CRM_PUSH_BACK_RUSH_URL = str(getattr(config_module, "CRM_PUSH_BACK_RUSH_URL", CRM_PUSH_BACK_RUSH_URL) or "").strip()
    CRM_PUSH_BACK_813_URL = str(getattr(config_module, "CRM_PUSH_BACK_813_URL", CRM_PUSH_BACK_813_URL) or "").strip()
    CRM_AUTO_SPLITTER_PREFLIGHT_REUSE_SECONDS = max(
        0,
        int(getattr(config_module, "CRM_AUTO_SPLITTER_PREFLIGHT_REUSE_SECONDS", CRM_AUTO_SPLITTER_PREFLIGHT_REUSE_SECONDS)),
    )


def reload_runtime_config():
    with config_lock:
        importlib.reload(config_module)
        _apply_runtime_config_from_module()


def _automation_queue_now_iso():
    return datetime.now().isoformat()


def _automation_queue_parse_datetime(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return parsed
    except ValueError:
        pass
    for fmt in ("%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M%p"):
        try:
            parsed_time = datetime.strptime(text.upper().replace(".", ""), fmt).time()
            now = datetime.now()
            scheduled = datetime.combine(now.date(), parsed_time)
            if scheduled <= now:
                scheduled += timedelta(days=1)
            return scheduled
        except ValueError:
            continue
    return None


def _automation_queue_seconds_until(iso_value):
    due_at = _automation_queue_parse_datetime(iso_value)
    if not due_at:
        return None
    return max(0, int(math.ceil((due_at - datetime.now()).total_seconds())))


def _automation_queue_idle_message(task):
    mode = str(task.get("queue_mode") or "").strip().lower()
    seconds = _automation_queue_seconds_until(task.get("next_run_at"))
    if mode == "repeat":
        if seconds is None:
            return "Idle between repeat runs."
        return f"Idle. Next repeat run in {_format_countdown_hms(seconds)}."
    if mode == "scheduled":
        scheduled = task.get("scheduled_for") or task.get("next_run_at")
        if seconds is None:
            return "Idle until scheduled time."
        return f"Idle until {fmt_queue_time(scheduled)} ({_format_countdown_hms(seconds)})."
    return "Idle."


def fmt_queue_time(iso_value):
    try:
        return datetime.fromisoformat(str(iso_value)).strftime("%I:%M %p").lstrip("0")
    except Exception:
        return str(iso_value or "").strip()


def _automation_queue_progress_from_status_payload(payload):
    runtime = payload.get("runtime") if isinstance(payload, dict) else {}
    runtime = runtime if isinstance(runtime, dict) else {}
    progress = runtime.get("currentOrderProgress")
    if isinstance(progress, dict):
        current = progress.get("current")
        total = progress.get("total")
        normalized = _crm_processing_order_progress(current, total, source=progress.get("source") or "")
        if normalized:
            return normalized
    return _crm_processing_order_progress_from_runtime(runtime)


def _automation_queue_live_status(task):
    status_fn = task.get("status_fn")
    if str(task.get("status") or "").lower() != "running" or not callable(status_fn):
        return None
    try:
        payload = status_fn()
    except Exception as exc:
        return {"message": f"Status unavailable: {exc}"}
    if isinstance(payload, dict) and not payload.get("running"):
        return None
    runtime = payload.get("runtime") if isinstance(payload, dict) else {}
    runtime = runtime if isinstance(runtime, dict) else {}
    message = (
        runtime.get("lastMessage")
        or (payload.get("message") if isinstance(payload, dict) else "")
        or task.get("message")
        or ""
    )
    live = {"message": str(message)}
    progress = _automation_queue_progress_from_status_payload(payload if isinstance(payload, dict) else {})
    if progress:
        live["progress"] = progress
    stage = runtime.get("currentStage")
    if stage:
        live["stage"] = str(stage)
    target_order_id = runtime.get("targetOrderId")
    if target_order_id:
        live["targetOrderId"] = str(target_order_id)
    return live


def _automation_queue_task_payload(task):
    next_run_at = task.get("next_run_at")
    run_history = task.get("run_history") if isinstance(task.get("run_history"), list) else []
    live_status = _automation_queue_live_status(task)
    message = task.get("message")
    if live_status and live_status.get("message"):
        message = live_status.get("message")
    payload = {
        "id": task.get("id"),
        "label": task.get("label"),
        "category": task.get("category"),
        "details": task.get("details"),
        "status": task.get("status"),
        "queue_mode": task.get("queue_mode"),
        "idle_reason": task.get("idle_reason"),
        "next_run_at": next_run_at,
        "next_run_seconds": _automation_queue_seconds_until(next_run_at),
        "repeat_interval_minutes": task.get("repeat_interval_minutes"),
        "scheduled_for": task.get("scheduled_for"),
        "automation_signature": task.get("automation_signature"),
        "advanced_summary": task.get("advanced_summary"),
        "run_count": task.get("run_count"),
        "run_history": run_history[:50],
        "activated_at": task.get("activated_at"),
        "started_at": task.get("started_at"),
        "completed_at": task.get("completed_at"),
        "duration_seconds": task.get("duration_seconds"),
        "message": message,
        "success": task.get("success"),
        "cancel_requested": bool(task.get("cancel_requested")),
        "position": task.get("position"),
    }
    if live_status:
        payload["live_status"] = live_status
        if live_status.get("progress"):
            payload["progress"] = live_status.get("progress")
    return payload


def _automation_queue_duration_seconds(task):
    try:
        started_at = task.get("started_at")
        completed_at = task.get("completed_at")
        if not started_at or not completed_at:
            return None
        start_dt = datetime.fromisoformat(str(started_at))
        end_dt = datetime.fromisoformat(str(completed_at))
        return round(max(0.0, (end_dt - start_dt).total_seconds()), 1)
    except Exception:
        return None


def _automation_queue_snapshot_locked():
    running = None
    queued = []
    idle = []
    history = []
    for task in automation_queue_tasks:
        payload = _automation_queue_task_payload(task)
        if task.get("status") == "running":
            running = payload
        elif task.get("status") == "queued":
            queued.append(payload)
        elif task.get("status") == "idle":
            idle.append(payload)
        else:
            history.append(payload)
    for index, task in enumerate(queued, start=1):
        task["position"] = index
    for index, task in enumerate(idle, start=1):
        task["position"] = len(queued) + index
    return {
        "success": True,
        "running": running,
        "queued": queued,
        "idle": idle,
        "history": history[:30],
        "tasks": ([running] if running else []) + queued + idle + history[:30],
        "queued_count": len(queued),
        "idle_count": len(idle),
        "running_count": 1 if running else 0,
    }


def get_automation_queue_payload():
    with automation_queue_lock:
        return _automation_queue_snapshot_locked()


def _automation_queue_trim_history_locked(max_history=40):
    history_seen = 0
    kept = []
    for task in automation_queue_tasks:
        if task.get("status") in {"queued", "running", "idle"}:
            kept.append(task)
            continue
        history_seen += 1
        if history_seen <= max_history:
            kept.append(task)
    automation_queue_tasks[:] = kept


def _automation_queue_coerce_result(result):
    if isinstance(result, tuple):
        if len(result) >= 2:
            return bool(result[0]), str(result[1])
        if len(result) == 1:
            return bool(result[0]), str(result[0])
    if isinstance(result, dict):
        return bool(result.get("success")), str(result.get("message") or "Task finished.")
    if result is None:
        return True, "Task finished."
    return bool(result), str(result)


def _automation_queue_promote_due_idle_locked():
    now = datetime.now()
    promoted = []
    for task in automation_queue_tasks:
        if task.get("status") != "idle" or task.get("cancel_requested"):
            continue
        due_at = _automation_queue_parse_datetime(task.get("next_run_at"))
        if due_at and due_at <= now:
            task["status"] = "queued"
            task["idle_reason"] = None
            task["message"] = "Waiting in queue."
            task["activated_at"] = _automation_queue_now_iso()
            promoted.append(task)
    if promoted:
        promoted_ids = {task.get("id") for task in promoted}
        automation_queue_tasks[:] = [
            task for task in automation_queue_tasks
            if task.get("id") not in promoted_ids
        ] + promoted
    return promoted


def _automation_queue_next_idle_wait_seconds_locked():
    due_times = []
    now = datetime.now()
    for task in automation_queue_tasks:
        if task.get("status") != "idle" or task.get("cancel_requested"):
            continue
        due_at = _automation_queue_parse_datetime(task.get("next_run_at"))
        if due_at:
            due_times.append(max(0.0, (due_at - now).total_seconds()))
    if not due_times:
        return None
    return max(0.5, min(30.0, min(due_times)))


def _automation_queue_append_run_history(task, ok, message, started_at, completed_at, duration_seconds):
    if str(task.get("queue_mode") or "").strip().lower() != "repeat":
        return
    history = task.get("run_history") if isinstance(task.get("run_history"), list) else []
    history.insert(
        0,
        {
            "run_number": max(1, int(_safe_float(task.get("run_count"), 0))),
            "started_at": started_at,
            "completed_at": completed_at,
            "duration_seconds": duration_seconds,
            "success": bool(ok),
            "message": str(message),
            "details": task.get("details"),
            "automation_signature": task.get("automation_signature"),
            "advanced_summary": task.get("advanced_summary"),
        },
    )
    task["run_history"] = history[:50]


def _automation_queue_worker_loop():
    global automation_queue_current_task_id
    while True:
        with automation_queue_condition:
            while True:
                _automation_queue_promote_due_idle_locked()
                next_task = next((task for task in automation_queue_tasks if task.get("status") == "queued"), None)
                if next_task:
                    break
                wait_seconds = _automation_queue_next_idle_wait_seconds_locked()
                automation_queue_condition.wait(timeout=wait_seconds)
            next_task["status"] = "running"
            next_task["started_at"] = _automation_queue_now_iso()
            next_task["completed_at"] = None
            next_task["run_count"] = max(0, int(_safe_float(next_task.get("run_count"), 0))) + 1
            next_task["message"] = f"Starting {next_task.get('label') or 'queued task'}."
            automation_queue_current_task_id = next_task.get("id")
            fn = next_task.get("fn")

        ok = False
        message = "Task did not run."
        try:
            log_automation_event(
                "automation.queue",
                "STARTED",
                str(next_task.get("label") or "Queued task"),
                source="server.py",
            )
            ok, message = _automation_queue_coerce_result(fn())
        except Exception as exc:
            logger.exception("Queued automation task failed unexpectedly")
            ok = False
            message = str(exc)

        with automation_queue_condition:
            task = next((item for item in automation_queue_tasks if item.get("id") == next_task.get("id")), None)
            canceled = bool(task and task.get("cancel_requested"))
            if task:
                started_at = task.get("started_at")
                completed_at = _automation_queue_now_iso()
                task["completed_at"] = completed_at
                task["duration_seconds"] = _automation_queue_duration_seconds(task)
                task["success"] = bool(ok)
                task["message"] = str(message)
                _automation_queue_append_run_history(task, ok, message, started_at, completed_at, task.get("duration_seconds"))
                if canceled:
                    automation_queue_tasks.remove(task)
                elif str(task.get("queue_mode") or "").strip().lower() == "repeat":
                    interval_minutes = _normalize_crm_positive_int(
                        task.get("repeat_interval_minutes"),
                        default=5,
                        minimum=5,
                        maximum=60,
                    )
                    next_run_at = datetime.now() + timedelta(minutes=interval_minutes)
                    task["status"] = "idle"
                    task["idle_reason"] = "repeat_wait"
                    task["next_run_at"] = next_run_at.isoformat()
                    task["started_at"] = None
                    task["completed_at"] = None
                    task["message"] = _automation_queue_idle_message(task)
                else:
                    task["status"] = "completed" if ok else "failed"
            automation_queue_current_task_id = None
            _automation_queue_trim_history_locked()
            automation_queue_condition.notify_all()
        log_automation_event(
            "automation.queue",
            "CANCELED" if canceled else ("COMPLETED" if ok else "FAILED"),
            f"{next_task.get('label')}: {message}",
            source="server.py",
        )


def _ensure_automation_queue_worker():
    global automation_queue_worker_started
    with automation_queue_condition:
        if automation_queue_worker_started:
            return
        automation_queue_worker_started = True
        threading.Thread(target=_automation_queue_worker_loop, daemon=True).start()


def enqueue_automation(
    label,
    category,
    fn,
    details=None,
    status_fn=None,
    queue_mode=None,
    repeat_interval_minutes=None,
    scheduled_for=None,
    automation_signature=None,
    advanced_summary=None,
):
    if not callable(fn):
        return False, "Queued automation needs a callable task.", None
    _ensure_automation_queue_worker()
    mode = str(queue_mode or "normal").strip().lower()
    if mode not in {"normal", "repeat", "scheduled"}:
        mode = "normal"
    interval_minutes = None
    if mode == "repeat":
        interval_minutes = _normalize_crm_positive_int(repeat_interval_minutes, default=5, minimum=5, maximum=60)
    scheduled_dt = _automation_queue_parse_datetime(scheduled_for)
    if mode == "scheduled" and scheduled_dt is None:
        return False, "Scheduled queue task needs a valid time.", None
    now_iso = _automation_queue_now_iso()
    status = "queued"
    next_run_at = None
    idle_reason = None
    message = "Waiting in queue."
    if mode == "scheduled":
        status = "idle"
        next_run_at = scheduled_dt.isoformat()
        idle_reason = "scheduled_wait"
    task_label = str(label or "Automation Task")
    task = {
        "id": uuid.uuid4().hex,
        "label": task_label,
        "category": str(category or "Automation"),
        "details": str(details or "").strip() or None,
        "status": status,
        "queue_mode": mode,
        "idle_reason": idle_reason,
        "next_run_at": next_run_at,
        "repeat_interval_minutes": interval_minutes,
        "scheduled_for": scheduled_dt.isoformat() if scheduled_dt else None,
        "automation_signature": automation_signature,
        "advanced_summary": advanced_summary,
        "run_count": 0,
        "run_history": [],
        "activated_at": now_iso,
        "started_at": None,
        "completed_at": None,
        "duration_seconds": None,
        "message": message,
        "success": None,
        "cancel_requested": False,
        "fn": fn,
        "status_fn": status_fn if callable(status_fn) else None,
    }
    if status == "idle":
        task["message"] = _automation_queue_idle_message(task)
    with automation_queue_condition:
        automation_queue_tasks.append(task)
        _automation_queue_trim_history_locked()
        automation_queue_condition.notify_all()
        position = sum(1 for item in automation_queue_tasks if item.get("status") == "queued")
        payload = _automation_queue_task_payload(task)
        payload["position"] = position if status == "queued" else None
    if status == "idle":
        msg = f"Queued {task['label']} as idle. {task['message']}"
    else:
        msg = f"Queued {task['label']} at position {position}."
    log_automation_event("automation.queue", "QUEUED", msg, source="server.py")
    return True, msg, payload


def cancel_automation_queue_task(task_id):
    task_id = str(task_id or "").strip()
    if not task_id:
        return False, "Missing queue task ID."
    running_cancel = False
    with automation_queue_condition:
        task = next((item for item in automation_queue_tasks if item.get("id") == task_id), None)
        if not task:
            return False, "Queue task was not found."
        status = task.get("status")
        if status in {"queued", "idle"}:
            automation_queue_tasks.remove(task)
            _automation_queue_trim_history_locked()
            automation_queue_condition.notify_all()
            log_automation_event("automation.queue", "CANCELED", f"{task.get('label')} canceled before start.", source="server.py")
            return True, f"Canceled {task.get('label')}."
        if status == "running":
            task["cancel_requested"] = True
            task["message"] = "Cancel requested by user."
            running_cancel = True
        else:
            return False, "Only queued, idle, or running tasks can be canceled."
    if running_cancel:
        ok, msg = force_stop_automation()
        return ok, f"Cancel requested for running task. {msg}"
    return False, "Queue task could not be canceled."


def cancel_all_automation_queue_tasks():
    running_cancel = False
    canceled_count = 0
    with automation_queue_condition:
        for task in automation_queue_tasks:
            status = task.get("status")
            if status in {"queued", "idle"}:
                task["cancel_requested"] = True
                canceled_count += 1
            elif status == "running":
                task["cancel_requested"] = True
                task["message"] = "Cancel requested by user."
                running_cancel = True
        automation_queue_tasks[:] = [task for task in automation_queue_tasks if task.get("status") not in {"queued", "idle"}]
        _automation_queue_trim_history_locked()
        automation_queue_condition.notify_all()
    extra = ""
    if running_cancel:
        _ok, stop_msg = force_stop_automation()
        extra = f" {stop_msg}"
    msg = f"Canceled {canceled_count} queued or idle task{'s' if canceled_count != 1 else ''}."
    if running_cancel:
        msg += extra
    log_automation_event("automation.queue", "CANCELED", msg, source="server.py")
    return True, msg


def delete_automation_queue_task(task_id):
    task_id = str(task_id or "").strip()
    if not task_id:
        return False, "Missing queue task ID."
    with automation_queue_condition:
        task = next((item for item in automation_queue_tasks if item.get("id") == task_id), None)
        if not task:
            return False, "Queue task was not found."
        if task.get("status") in {"queued", "running", "idle"}:
            return False, "Only finished or failed tasks can be deleted. Cancel active tasks instead."
        automation_queue_tasks.remove(task)
        automation_queue_condition.notify_all()
    return True, f"Deleted {task.get('label')} from queue activity."


def clear_finished_automation_queue_tasks():
    with automation_queue_condition:
        before = len(automation_queue_tasks)
        automation_queue_tasks[:] = [
            task for task in automation_queue_tasks
            if task.get("status") in {"queued", "running", "idle"}
        ]
        removed = before - len(automation_queue_tasks)
        automation_queue_condition.notify_all()
    return True, f"Cleared {removed} finished queue entr{'y' if removed == 1 else 'ies'}."


def reorder_automation_queue(task_ids):
    desired = [str(value or "").strip() for value in (task_ids if isinstance(task_ids, list) else [])]
    desired = [value for value in desired if value]
    with automation_queue_condition:
        queued = [task for task in automation_queue_tasks if task.get("status") == "queued"]
        queued_by_id = {task.get("id"): task for task in queued}
        reordered = [queued_by_id[task_id] for task_id in desired if task_id in queued_by_id]
        reordered_ids = {task.get("id") for task in reordered}
        reordered.extend([task for task in queued if task.get("id") not in reordered_ids])
        queue_iter = iter(reordered)
        rebuilt = []
        for task in automation_queue_tasks:
            if task.get("status") == "queued":
                rebuilt.append(next(queue_iter))
            else:
                rebuilt.append(task)
        automation_queue_tasks[:] = rebuilt
        automation_queue_condition.notify_all()
        payload = _automation_queue_snapshot_locked()
    log_automation_event("automation.queue", "REORDERED", "Queued task order updated.", source="server.py")
    return True, "Queue order updated.", payload


def _wait_for_status_completion(status_payload_fn, fallback_message="Queued task started."):
    while True:
        payload = status_payload_fn()
        runtime = payload.get("runtime") if isinstance(payload, dict) else {}
        state = payload.get("state") if isinstance(payload, dict) else {}
        if not payload.get("running"):
            success = runtime.get("lastSuccess")
            if success is None and isinstance(state, dict):
                success = state.get("last_run_success")
            message = (
                runtime.get("lastMessage")
                or payload.get("message")
                or (state.get("last_run_message") if isinstance(state, dict) else "")
                or fallback_message
            )
            return bool(success) if success is not None else True, str(message)
        time.sleep(1.0)


def _automation_stop_is_blocking():
    with automation_process_lock:
        return time.time() < automation_stop_block_until


def _automation_stop_requested_since(started_at):
    with automation_process_lock:
        return automation_stop_requested_at >= started_at


def _register_automation_process(proc, label):
    if not proc or not getattr(proc, "pid", None):
        return
    with automation_process_lock:
        active_automation_processes[int(proc.pid)] = {"proc": proc, "label": str(label)}


def _unregister_automation_process(proc):
    if not proc or not getattr(proc, "pid", None):
        return
    with automation_process_lock:
        active_automation_processes.pop(int(proc.pid), None)


def _consume_force_stopped_pid(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    with automation_process_lock:
        if pid in force_stopped_process_pids:
            force_stopped_process_pids.discard(pid)
            return True
    return False


def _force_stop_message(label):
    return f"{label} force-stopped by user."


def _kill_process_tree(pid):
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, text=True, timeout=10)
        return True
    except Exception as e:
        logger.warning("Could not force-stop process tree %s: %s", pid, e)
        return False


def force_stop_automation():
    global automation_stop_requested_at, automation_stop_block_until
    now_ts = time.time()
    with automation_process_lock:
        automation_stop_requested_at = now_ts
        automation_stop_block_until = max(automation_stop_block_until, now_ts + 8)
        active_items = list(active_automation_processes.items())
        for pid, _info in active_items:
            force_stopped_process_pids.add(pid)

    stopped_labels = []
    for pid, info in active_items:
        label = str((info or {}).get("label") or pid)
        if _kill_process_tree(pid):
            stopped_labels.append(label)

    with crm_runtime_lock:
        if crm_runtime.get("running"):
            crm_runtime["lastMessage"] = "Force stop requested by user."
            crm_runtime["lastSuccess"] = False
    with crm_address_runtime_lock:
        if crm_address_runtime.get("running"):
            crm_address_runtime["lastMessage"] = "Force stop requested by user."
            crm_address_runtime["lastSuccess"] = False
    with crm_product_separator_runtime_lock:
        if crm_product_separator_runtime.get("running"):
            crm_product_separator_runtime["lastMessage"] = "Force stop requested by user."
            crm_product_separator_runtime["lastSuccess"] = False
    with crm_order_goods_runtime_lock:
        if crm_order_goods_runtime.get("running"):
            crm_order_goods_runtime["lastMessage"] = "Force stop requested by user."
            crm_order_goods_runtime["lastSuccess"] = False
    with crm_shipping_bypasser_runtime_lock:
        if crm_shipping_bypasser_runtime.get("running"):
            crm_shipping_bypasser_runtime["lastMessage"] = "Force stop requested by user."
            crm_shipping_bypasser_runtime["lastSuccess"] = False
    with crm_push_back_runtime_lock:
        if crm_push_back_runtime.get("running"):
            crm_push_back_runtime["lastMessage"] = "Force stop requested by user."
            crm_push_back_runtime["lastSuccess"] = False
    with crm_auto_splitter_runtime_lock:
        if crm_auto_splitter_runtime.get("running"):
            crm_auto_splitter_runtime["lastMessage"] = "Force stop requested by user."
            crm_auto_splitter_runtime["lastSuccess"] = False
    with crm_mass_emailer_runtime_lock:
        if crm_mass_emailer_runtime.get("running"):
            crm_mass_emailer_runtime["lastMessage"] = "Force stop requested by user."
            crm_mass_emailer_runtime["lastSuccess"] = False
    with crm_processing_runtime_lock:
        if crm_processing_runtime.get("running"):
            crm_processing_runtime["lastMessage"] = "Force stop requested by user."
            crm_processing_runtime["lastSuccess"] = False

    msg = (
        "Force stop requested. Stopping: " + ", ".join(stopped_labels)
        if stopped_labels
        else "Force stop requested. No active worker process was found, but the current automation sequence was interrupted."
    )
    log_automation_event("automation.force_stop", "STOPPED", msg, source="server.py")
    return True, msg


def _run_script(script_path, args, label, timeout=120, show_terminal=False):
    started_at = time.time()
    if _automation_stop_is_blocking():
        msg = _force_stop_message(label)
        return False, msg, {"success": False, "message": msg, "stopped": True}
    try:
        if os.path.exists(RESULT_FILE):
            try:
                os.remove(RESULT_FILE)
            except OSError:
                pass
        if os.path.exists(AUTOMATION_STATUS_FILE):
            try:
                os.remove(AUTOMATION_STATUS_FILE)
            except OSError:
                pass
        creation_flags = (
            getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
            if show_terminal
            else getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        env = os.environ.copy()
        env["AUTOMATION_STATUS_FILE"] = AUTOMATION_STATUS_FILE
        proc = subprocess.Popen(
            [_resolve_console_python(), script_path] + list(args),
            cwd=SCRIPT_DIR,
            creationflags=creation_flags,
            env=env,
        )
        _register_automation_process(proc, label)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except Exception:
                pass

            # If the child wrote last_result.json before hanging in cleanup,
            # prefer that payload over a generic timeout failure.
            payload = _read_result_file_with_retry()
            if isinstance(payload, dict):
                try:
                    base_msg = payload.get("message", "Unknown result")
                    msg = f"{base_msg} (process exceeded {timeout}s while exiting)"
                    payload["message"] = msg
                    return bool(payload.get("success")), msg, payload
                except Exception:
                    pass

            msg = f"{label} timed out after {timeout} seconds."
            return False, msg, {"success": False, "message": msg}
        finally:
            _unregister_automation_process(proc)

        if _consume_force_stopped_pid(proc.pid) or _automation_stop_requested_since(started_at):
            msg = _force_stop_message(label)
            return False, msg, {"success": False, "message": msg, "stopped": True}

        payload = _read_result_file_with_retry()
        if isinstance(payload, dict):
            return bool(payload.get("success")), payload.get("message", "Unknown result"), payload

        if proc.returncode == 0:
            msg = f"{label} completed successfully."
            return True, msg, {"success": True, "message": msg}

        msg = f"{label} failed (exit code {proc.returncode})."
        return False, msg, {"success": False, "message": msg}
    except Exception as e:
        return False, str(e), {"success": False, "message": str(e)}


def _read_result_file_with_retry(retries=8, delay=0.25):
    if not os.path.exists(RESULT_FILE):
        return None
    last_error = None
    for attempt in range(max(1, int(retries or 1))):
        try:
            with open(RESULT_FILE, "r", encoding="utf-8-sig") as f:
                loaded = json.load(f)
            return loaded if isinstance(loaded, dict) else None
        except Exception as exc:
            last_error = exc
            time.sleep(float(delay or 0) * (attempt + 1))
    logger.warning("Could not read %s after retries: %s", RESULT_FILE, last_error)
    return None


def _run_automation_script(path, action, label, extra_args=None, timeout=120):
    args = [action]
    if extra_args:
        args.extend([str(x) for x in extra_args if str(x).strip()])
    ok, msg, _ = _run_script(path, args, f"{label}-{action}", timeout=timeout)
    return ok, msg


def _sunday_of(day):
    return day - timedelta(days=(day.weekday() + 1) % 7)


def _new_work_state(today=None):
    if today is None:
        today = datetime.now().date()
    return {
        "version": 1,
        "week_start": _sunday_of(today).isoformat(),
        "total_paid_hours": 0.0,
        "days": {},
        "active_shift": None,
        "last_paycom_sync": None,
        "sync_history": [],
        "updated_at": datetime.now().isoformat(),
    }


def load_work_state(now=None):
    if now is None:
        now = datetime.now()
    state = _new_work_state(now.date())
    if os.path.exists(WORK_STATE_FILE):
        try:
            with open(WORK_STATE_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                state.update(loaded)
        except Exception as e:
            logger.warning("Could not read %s: %s", WORK_STATE_FILE, e)

    state["days"] = state.get("days") if isinstance(state.get("days"), dict) else {}
    for day_key, day_entry in list(state["days"].items()):
        if not isinstance(day_entry, dict):
            state["days"][day_key] = {}
            continue
        day_entry.pop("local_paid_hours", None)
        day_entry.pop("clock_out_mode", None)
    state["active_shift"] = state.get("active_shift") if isinstance(state.get("active_shift"), dict) else None
    state["total_paid_hours"] = round(_safe_float(state.get("total_paid_hours")), 2)
    state["sync_history"] = state.get("sync_history") if isinstance(state.get("sync_history"), list) else []

    expected = _sunday_of(now.date()).isoformat()
    if state.get("week_start") != expected:
        state = _new_work_state(now.date())
    return state


def save_work_state(state):
    state["updated_at"] = datetime.now().isoformat()
    with open(WORK_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def _format_time(dt):
    return dt.strftime("%I:%M %p").lstrip("0")


def _format_auto_clock_out_label(dt, now=None):
    if now is None:
        now = datetime.now()
    day = dt.date()
    if day == now.date():
        prefix = "today"
    elif day == (now + timedelta(days=1)).date():
        prefix = "tomorrow"
    else:
        prefix = day.isoformat()
    return f"{prefix} {_format_time(dt)}"


def _get_day_suffix_for_datetime(dt):
    idx = (dt.weekday() + 1) % 7
    return DAY_NAMES[idx]


def _is_auto_clock_out_day(day_obj):
    return True


def _get_active_shift_date(active_shift, default_day=None):
    if default_day is None:
        default_day = datetime.now().date()
    if not isinstance(active_shift, dict):
        return default_day

    date_iso = (active_shift.get("date") or "").strip()
    if date_iso:
        try:
            return datetime.fromisoformat(date_iso).date()
        except ValueError:
            pass

    clock_in_iso = (active_shift.get("clock_in_at") or "").strip()
    if clock_in_iso:
        try:
            return datetime.fromisoformat(clock_in_iso).date()
        except ValueError:
            pass

    return default_day


def _max_auto_clock_out_horizon_hours():
    return max(1.0 / 60.0, _safe_float(WORK_CLOCK_AUTO_OUT_MAX_HOURS, 24.0))


def _apply_auto_clock_out_horizon(clock_in_dt, auto_dt):
    max_dt = clock_in_dt + timedelta(hours=_max_auto_clock_out_horizon_hours())
    if auto_dt > max_dt:
        return max_dt
    return auto_dt


def _active_shift_open_status(state, active_shift, now=None):
    if now is None:
        now = datetime.now()
    if not isinstance(active_shift, dict) or not active_shift:
        return False, "No active shift found."

    clock_in_iso = str(active_shift.get("clock_in_at") or "").strip()
    if not clock_in_iso:
        return False, "Active shift is missing clock-in time."
    try:
        clock_in_dt = datetime.fromisoformat(clock_in_iso)
    except ValueError:
        return False, "Active shift has invalid clock-in time format."
    if clock_in_dt > now + timedelta(minutes=1):
        return False, "Active shift has a future clock-in time."

    shift_day = _get_active_shift_date(active_shift, default_day=clock_in_dt.date())
    max_open_until = clock_in_dt + timedelta(hours=_max_auto_clock_out_horizon_hours(), minutes=10)
    if now > max_open_until:
        return False, (
            "Auto clock-out skipped because the active shift is older than "
            f"the {_max_auto_clock_out_horizon_hours():g}-hour auto-out limit."
        )

    days = state.get("days") if isinstance(state, dict) and isinstance(state.get("days"), dict) else {}
    day_entry = days.get(shift_day.isoformat()) if isinstance(days.get(shift_day.isoformat()), dict) else {}
    if day_entry.get("clock_out_at"):
        return False, "Auto clock-out skipped because the local shift is already clocked out."

    paycom_out = _clean_paycom_punch_value(day_entry.get("paycom_clock_out"))
    if paycom_out and _parse_paycom_clock_time_to_iso(shift_day.isoformat(), paycom_out):
        return False, "Auto clock-out skipped because Paycom already shows a clock-out time."

    return True, ""


def _auto_clock_out_allowed_for_active_shift(active_shift, now=None, state=None):
    if now is None:
        now = datetime.now()
    if not WORK_CLOCK_CAPPED:
        return False, "Auto schedule is disabled because WORK_CLOCK_CAPPED is False."

    if state is not None:
        return _active_shift_open_status(state, active_shift, now=now)

    if not isinstance(active_shift, dict) or not active_shift:
        return False, "No active shift found."
    if not active_shift.get("clock_in_at"):
        return False, "Active shift is missing clock-in time."
    try:
        clock_in_dt = datetime.fromisoformat(str(active_shift.get("clock_in_at")))
    except ValueError:
        return False, "Active shift has invalid clock-in time format."
    if clock_in_dt > now + timedelta(minutes=1):
        return False, "Active shift has a future clock-in time."
    max_open_until = clock_in_dt + timedelta(hours=_max_auto_clock_out_horizon_hours(), minutes=10)
    if now > max_open_until:
        return False, (
            "Auto clock-out skipped because the active shift is older than "
            f"the {_max_auto_clock_out_horizon_hours():g}-hour auto-out limit."
        )
    return True, ""


def _get_slack_message_for_day(action, dt=None):
    if dt is None:
        dt = datetime.now()
    if action not in ("in", "out"):
        return ""
    try:
        selection = select_slack_day_message(config_module, action, now=dt)
        return str(selection.get("message") or "")
    except Exception:
        suffix = _get_day_suffix_for_datetime(dt)
        prefix = "SLACK_MESSAGE_OUT_" if action == "out" else "SLACK_MESSAGE_IN_"
        key = f"{prefix}{suffix}"
        return str(getattr(config_module, key, "") or "")


def _build_week_hours_text(total_paid):
    if WORK_CLOCK_CAPPED:
        return f"Current week hours: {total_paid:.2f} / {WORK_CLOCK_CAP_HOURS:.2f}"
    return f"Current week hours: {total_paid:.2f} (manual mode)"


def _refresh_tray_menu():
    if tray_icon_ref:
        try:
            tray_icon_ref.update_menu()
        except Exception:
            pass


def _set_tray_status(auto_text=None, week_text=None):
    global tray_auto_out_text, tray_week_hours_text
    if auto_text is not None:
        tray_auto_out_text = auto_text
    if week_text is not None:
        tray_week_hours_text = week_text
    _refresh_tray_menu()


def refresh_tray_status_from_state(state=None):
    global tray_auto_out_active
    if state is None:
        state = load_work_state()
    total_paid = _safe_float(state.get("total_paid_hours"))
    auto_active = False
    auto_text = "Scheduled auto clock-out: manual mode (CAPPED=False)"
    if WORK_CLOCK_CAPPED:
        active = state.get("active_shift") or {}
        auto_text = "Scheduled auto clock-out: waiting for Work In"
        auto_iso = active.get("auto_clock_out_at")
        manual_override = bool(active.get("manual_auto_clock_out"))
        if auto_iso:
            try:
                when_dt = datetime.fromisoformat(auto_iso)
                auto_active = True
                if manual_override:
                    auto_text = f"Scheduled auto clock-out: {_format_auto_clock_out_label(when_dt)} (manual override)"
                else:
                    auto_text = f"Scheduled auto clock-out: {_format_auto_clock_out_label(when_dt)}"
            except ValueError:
                pass
    tray_auto_out_active = auto_active
    _set_tray_status(auto_text=auto_text, week_text=_build_week_hours_text(total_paid))


def _cancel_auto_clock_timer_locked():
    global auto_clock_timer
    if auto_clock_timer:
        auto_clock_timer.cancel()
        auto_clock_timer = None


def cancel_auto_clock_out_timer():
    with state_lock:
        _cancel_auto_clock_timer_locked()


def notify_user(title, message):
    logger.info("%s: %s", title, message)
    if tray_icon_ref:
        try:
            tray_icon_ref.notify(message, title)
        except Exception:
            pass


def _record_sync_result(state, success, message, week_hours=None):
    entry = {"at": datetime.now().isoformat(), "success": bool(success), "message": str(message)}
    if week_hours is not None:
        entry["week_hours"] = round(_safe_float(week_hours), 2)
        state["paycom_week_hours"] = entry["week_hours"]
        state["total_paid_hours"] = entry["week_hours"]
    state["last_paycom_sync"] = entry
    history = state.get("sync_history") if isinstance(state.get("sync_history"), list) else []
    history.append(entry)
    state["sync_history"] = history[-100:]


def _paycom_label_to_iso_date(label, week_start_iso):
    if not isinstance(label, str):
        return None
    m = re.match(r"^\s*(Sun|Mon|Tue|Wed|Thu|Fri|Sat)\s+(\d{2})/(\d{2})\s*$", label, flags=re.IGNORECASE)
    if not m:
        return None

    mm = int(m.group(2))
    dd = int(m.group(3))
    try:
        week_start_dt = datetime.fromisoformat(str(week_start_iso)).date()
    except Exception:
        week_start_dt = datetime.now().date()

    candidates = []
    for year in (week_start_dt.year - 1, week_start_dt.year, week_start_dt.year + 1):
        try:
            d = datetime(year, mm, dd).date()
        except ValueError:
            continue
        delta = abs((d - week_start_dt).days)
        candidates.append((delta, d))
    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0])
    return candidates[0][1].isoformat()


def _merge_paycom_day_rows_into_state(state, day_rows):
    if not isinstance(day_rows, list):
        return 0
    merged = 0
    days = state.setdefault("days", {})
    week_start_iso = state.get("week_start")

    for row in day_rows:
        if not isinstance(row, dict):
            continue
        label = row.get("date_label")
        day_key = _paycom_label_to_iso_date(label, week_start_iso)
        if not day_key:
            continue

        entry = days.get(day_key, {}) if isinstance(days.get(day_key), dict) else {}
        entry["paycom_date_label"] = str(label)

        raw_hours = row.get("hours")
        try:
            paycom_hours = round(float(raw_hours), 2) if raw_hours is not None else None
        except Exception:
            paycom_hours = None
        entry["paycom_hours"] = paycom_hours

        paycom_in = row.get("clock_in")
        paycom_out = row.get("clock_out")
        paycom_code = row.get("pay_code")
        entry["paycom_clock_in"] = _clean_paycom_punch_value(paycom_in)
        entry["paycom_clock_out"] = _clean_paycom_punch_value(paycom_out)
        entry["paycom_pay_code"] = str(paycom_code).strip() if paycom_code else None
        entry["paycom_is_flex"] = bool(row.get("is_flex"))
        entry["paycom_is_possible_pto"] = bool(row.get("is_possible_pto"))
        entry["paycom_is_paid_leave_code"] = bool(row.get("is_paid_leave_code"))
        entry["paycom_synced_at"] = datetime.now().isoformat()

        days[day_key] = entry
        merged += 1

    state["days"] = days
    return merged


def _parse_paycom_clock_time_to_iso(day_iso, time_text):
    if not day_iso or not time_text:
        return None
    try:
        day = datetime.fromisoformat(str(day_iso)).date()
    except Exception:
        return None

    raw = _clean_paycom_punch_value(time_text)
    if not raw:
        return None

    candidates = [_normalize_inline_text(raw).upper().replace(".", "")]
    extracted = re.search(r"\b\d{1,2}:\d{2}\s*(?:[AP]M)?\b", candidates[0], flags=re.IGNORECASE)
    if extracted:
        token = extracted.group(0).upper().replace(".", "")
        if token and token not in candidates:
            candidates.insert(0, token)

    fmts = ("%I:%M %p", "%I:%M%p", "%H:%M")
    for cand in candidates:
        for fmt in fmts:
            try:
                t = datetime.strptime(cand, fmt).time()
                return datetime.combine(day, t).isoformat()
            except ValueError:
                continue
    return None


def _planned_paid_hours_to_weekly_cap(total_paid_hours):
    remaining = max(0.0, WORK_CLOCK_CAP_HOURS - _safe_float(total_paid_hours, 0.0))
    if remaining <= 0:
        return 0.01
    return remaining


def _break_hours_for_gross_shift(gross_hours):
    gross = _safe_float(gross_hours, 0.0)
    if gross > WORK_CLOCK_BREAK_APPLIES_AFTER_HOURS:
        return max(0.0, WORK_CLOCK_BREAK_MINUTES / 60.0)
    return 0.0


def _paid_hours_for_gross_shift(gross_hours):
    gross = max(0.0, _safe_float(gross_hours, 0.0))
    return max(0.0, gross - _break_hours_for_gross_shift(gross))


def _gross_hours_for_paid_target(paid_hours):
    paid = max(0.0, _safe_float(paid_hours, 0.0))
    break_hours = max(0.0, WORK_CLOCK_BREAK_MINUTES / 60.0)
    if paid <= WORK_CLOCK_BREAK_APPLIES_AFTER_HOURS:
        return paid
    return paid + break_hours


def _infer_active_shift_from_paycom_rows(state, now=None):
    """
    If user is already clocked in in Paycom (today has IN and no final OUT),
    infer an active shift in local state so auto clock-out can be scheduled.
    """
    if now is None:
        now = datetime.now()

    if state.get("active_shift"):
        return False, "Active shift already exists."

    days = state.get("days") if isinstance(state.get("days"), dict) else {}
    today_key = now.date().isoformat()
    today = days.get(today_key) if isinstance(days.get(today_key), dict) else None
    if not today:
        return False, "No Paycom row for today."

    paycom_in = _clean_paycom_punch_value(today.get("paycom_clock_in"))
    paycom_out = _clean_paycom_punch_value(today.get("paycom_clock_out"))
    if not paycom_in:
        return False, "No Paycom clock-in time found for today."
    if paycom_out and _parse_paycom_clock_time_to_iso(today_key, paycom_out):
        return False, "Paycom row already has clock-out time."
    if today.get("clock_out_at"):
        return False, "Local row already has clock-out time."

    clock_in_iso = _parse_paycom_clock_time_to_iso(today_key, paycom_in)
    if not clock_in_iso:
        return False, f"Could not parse Paycom clock-in time '{paycom_in}'."

    today["clock_in_at"] = today.get("clock_in_at") or clock_in_iso
    days[today_key] = today
    state["days"] = days
    state["active_shift"] = {
        "date": today_key,
        "clock_in_at": clock_in_iso,
        "auto_clock_out_at": None,
        "automatic_mode": bool(WORK_CLOCK_CAPPED),
        "manual_auto_clock_out": False,
        "auto_clock_out_source": None,
        "source": "paycom-sync",
    }
    return True, f"Detected active Paycom clock-in for today at {paycom_in}."


def sync_week_hours_from_paycom(reason):
    if not WORK_CLOCK_SYNC_FROM_PAYCOM:
        return False, "Paycom sync disabled by config.", None, []
    if not os.path.exists(PAYCOM_HOURS_SCRIPT):
        return False, "paycom_hours.py is missing.", None, []
    ok, msg, payload = _run_script(PAYCOM_HOURS_SCRIPT, ["week"], f"PaycomHoursSync-{reason}", timeout=180)
    if not ok:
        return False, msg, None, []
    raw = payload.get("week_hours")
    val = round(_safe_float(raw, -1), 2)
    if val < 0:
        return False, f"Paycom sync returned invalid week_hours: {raw}", None, []
    day_rows = payload.get("day_rows") if isinstance(payload.get("day_rows"), list) else []
    return True, msg, val, day_rows


def _sync_paycom_hours_into_work_state(reason, update_total_hours=True):
    ok, msg, hours, day_rows = sync_week_hours_from_paycom(reason)
    possible_pto_days = _count_paycom_possible_pto_days(day_rows) if ok else 0
    with state_lock:
        state = load_work_state()
        _record_sync_result(state, ok, msg, hours if ok and update_total_hours else None)
        merged_days = _merge_paycom_day_rows_into_state(state, day_rows) if ok else 0
        save_work_state(state)
        refresh_tray_status_from_state(state)
    return ok, msg, hours, merged_days, possible_pto_days


def _count_paycom_possible_pto_days(day_rows):
    if not isinstance(day_rows, list):
        return 0
    return sum(1 for row in day_rows if isinstance(row, dict) and bool(row.get("is_possible_pto")))


def _auto_clock_out_timer_callback():
    global auto_clock_timer
    with state_lock:
        auto_clock_timer = None

    def _queued_auto_clock_out():
        if WORK_CLOCK_SYNC_FROM_PAYCOM:
            _sync_paycom_hours_into_work_state("auto-clock-out-precheck", update_total_hours=True)

        with state_lock:
            state = load_work_state()
            active = state.get("active_shift") or {}
            allowed, reason = _auto_clock_out_allowed_for_active_shift(active, state=state)
            if not allowed:
                if not _clear_closed_active_shift_locked(state):
                    _clear_active_auto_clock_out_locked(state)
                save_work_state(state)
                refresh_tray_status_from_state(state)
                msg = f"Skipped auto clock-out: {reason}"
                _audit_result("work.auto_clock_out_timer", True, msg)
                return True, msg
        ok, msg = run_work("out", automatic=True)
        if not ok:
            notify_user("Work Auto Clock-Out Failed", msg)
        return ok, msg

    enqueue_automation("Automatic Work Clock Out", "Communications", _queued_auto_clock_out)


def schedule_auto_clock_out(auto_dt):
    global auto_clock_timer
    delay = max(1.0, (auto_dt - datetime.now()).total_seconds())
    with state_lock:
        _cancel_auto_clock_timer_locked()
        auto_clock_timer = threading.Timer(delay, _auto_clock_out_timer_callback)
        auto_clock_timer.daemon = True
        auto_clock_timer.start()


def _set_active_auto_clock_out_locked(state, auto_dt, source="auto"):
    active = state.get("active_shift") or {}
    if not active:
        return False

    source_key = "manual" if str(source or "").lower() == "manual" else "auto"
    auto_iso = auto_dt.isoformat() if auto_dt else None
    manual_override = source_key == "manual"

    active["auto_clock_out_at"] = auto_iso
    active["automatic_mode"] = bool(auto_dt)
    active["manual_auto_clock_out"] = manual_override
    active["auto_clock_out_source"] = source_key if auto_dt else None
    state["active_shift"] = active

    day_key = active.get("date")
    if day_key:
        day_entry = state.setdefault("days", {}).get(day_key, {})
        day_entry["auto_clock_out_at"] = auto_iso
        day_entry["manual_auto_clock_out"] = manual_override if auto_dt else False
        day_entry["auto_clock_out_source"] = source_key if auto_dt else None
        state["days"][day_key] = day_entry
    return True


def _parse_manual_auto_clock_out_datetime(raw_value, active_shift, now=None):
    if now is None:
        now = datetime.now()

    text = str(raw_value or "").strip()
    if not text:
        return None, "Manual auto clock-out time is required (use HH:MM)."

    shift_day = _get_active_shift_date(active_shift, default_day=now.date())
    parsed_dt = None

    try:
        parsed_dt = datetime.fromisoformat(text)
        if parsed_dt.tzinfo is not None:
            parsed_dt = parsed_dt.astimezone().replace(tzinfo=None)
    except ValueError:
        parsed_dt = None

    if parsed_dt is None:
        normalized = text.upper().replace(".", "")
        for fmt in ("%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M%p"):
            try:
                parsed_time = datetime.strptime(normalized, fmt).time()
                parsed_dt = datetime.combine(shift_day, parsed_time)
                break
            except ValueError:
                continue

    if parsed_dt is None:
        return None, "Invalid time format. Use HH:MM (24-hour) or h:mm AM/PM."

    if parsed_dt.date() != shift_day:
        return None, (
            f"Manual auto clock-out time must be on {shift_day.isoformat()} "
            "to match the active shift date."
        )

    if parsed_dt <= now:
        return None, (
            f"Manual auto clock-out time must be in the future. "
            f"Current time is {_format_time(now)}."
        )
    return parsed_dt, ""

def _compute_auto_out_for_active_shift(state, now=None):
    if now is None:
        now = datetime.now()

    active = state.get("active_shift") or {}
    if not active:
        return None, "No active shift found. Run Work In first."

    allowed, reason = _auto_clock_out_allowed_for_active_shift(active, now=now, state=state)
    if not allowed:
        return None, reason

    clock_in_iso = active.get("clock_in_at")
    if not clock_in_iso:
        return None, "Active shift is missing clock-in time."
    try:
        clock_in_dt = datetime.fromisoformat(clock_in_iso)
    except ValueError:
        return None, "Active shift has invalid clock-in time format."

    total_paid = _safe_float(state.get("total_paid_hours"), 0.0)
    planned_paid = _planned_paid_hours_to_weekly_cap(total_paid)
    planned_gross = _gross_hours_for_paid_target(planned_paid)

    auto_dt = _apply_auto_clock_out_horizon(clock_in_dt, clock_in_dt + timedelta(hours=planned_gross))
    if auto_dt <= now:
        auto_dt = now + timedelta(minutes=1)
    return auto_dt, ""


def _compute_auto_out_for_new_clock_in(total_paid, clock_in_dt):
    if not WORK_CLOCK_CAPPED:
        return None, "CAPPED mode is off."

    planned_paid = _planned_paid_hours_to_weekly_cap(total_paid)
    planned_gross = _gross_hours_for_paid_target(planned_paid)
    return _apply_auto_clock_out_horizon(clock_in_dt, clock_in_dt + timedelta(hours=planned_gross)), ""


def schedule_auto_clock_out_from_active_shift():
    with state_lock:
        state = load_work_state()
        inferred_active = False
        infer_note = ""
        if not state.get("active_shift"):
            inferred_active, infer_note = _infer_active_shift_from_paycom_rows(state)
            if inferred_active:
                save_work_state(state)

        auto_dt, err = _compute_auto_out_for_active_shift(state)
        if err:
            if _clear_closed_active_shift_locked(state):
                save_work_state(state)
                refresh_tray_status_from_state(state)
            if (not inferred_active) and infer_note and "No active shift found" in err:
                err = f"{err} ({infer_note})"
            _audit_result("work.schedule_auto_clock_out", False, err)
            return False, err

        _set_active_auto_clock_out_locked(state, auto_dt, source="auto")
        save_work_state(state)

    schedule_auto_clock_out(auto_dt)
    with state_lock:
        refresh_tray_status_from_state(load_work_state())
    msg = f"Auto clock-out is now scheduled for {_format_auto_clock_out_label(auto_dt)}."
    _audit_result("work.schedule_auto_clock_out", True, msg)
    return True, msg


def update_manual_auto_clock_out_schedule(raw_time_value):
    now = datetime.now()
    with state_lock:
        state = load_work_state()
        active = state.get("active_shift") or {}
        if not active:
            err = "No active shift found. Run Work In first."
            _audit_result("work.update_auto_clock_out_schedule", False, err)
            return False, err

        allowed, reason = _auto_clock_out_allowed_for_active_shift(active, now=now, state=state)
        if not allowed:
            if _clear_closed_active_shift_locked(state, now=now):
                save_work_state(state)
                refresh_tray_status_from_state(state)
            _audit_result("work.update_auto_clock_out_schedule", False, reason)
            return False, reason

        auto_dt, err = _parse_manual_auto_clock_out_datetime(raw_time_value, active, now=now)
        if err:
            _audit_result("work.update_auto_clock_out_schedule", False, err)
            return False, err

        _set_active_auto_clock_out_locked(state, auto_dt, source="manual")
        save_work_state(state)

    schedule_auto_clock_out(auto_dt)
    with state_lock:
        refresh_tray_status_from_state(load_work_state())
    msg = f"Auto clock-out manually updated to {_format_auto_clock_out_label(auto_dt)}."
    _audit_result("work.update_auto_clock_out_schedule", True, msg)
    return True, msg


def _clear_active_auto_clock_out_locked(state):
    changed = False
    active = state.get("active_shift") or {}
    if active and active.get("auto_clock_out_at"):
        active["auto_clock_out_at"] = None
        active["automatic_mode"] = False
        active["manual_auto_clock_out"] = False
        active["auto_clock_out_source"] = None
        state["active_shift"] = active
        changed = True

        day_key = active.get("date")
        if day_key and day_key in state.get("days", {}):
            day_entry = state["days"][day_key]
            if day_entry.get("auto_clock_out_at"):
                day_entry["auto_clock_out_at"] = None
                day_entry["manual_auto_clock_out"] = False
                day_entry["auto_clock_out_source"] = None
                state["days"][day_key] = day_entry
    return changed


def _clear_closed_active_shift_locked(state, now=None):
    active = state.get("active_shift") or {}
    if not active:
        return False
    is_open, _reason = _active_shift_open_status(state, active, now=now)
    if is_open:
        return False
    _cancel_auto_clock_timer_locked()
    _clear_active_auto_clock_out_locked(state)
    state["active_shift"] = None
    return True


def clear_auto_clock_out_schedule():
    cancel_auto_clock_out_timer()
    with state_lock:
        state = load_work_state()
        if _clear_active_auto_clock_out_locked(state):
            save_work_state(state)
        refresh_tray_status_from_state(state)
    msg = "Auto clock-out timer was canceled."
    _audit_result("work.clear_auto_clock_out_schedule", True, msg)
    return True, msg


def restore_auto_clock_out_timer_from_state():
    with state_lock:
        state = load_work_state()
        active = state.get("active_shift") or {}
        auto_iso = active.get("auto_clock_out_at")
        allowed, reason = _auto_clock_out_allowed_for_active_shift(active, state=state)
        manual_override = bool(active.get("manual_auto_clock_out"))
        if auto_iso and not allowed:
            _cancel_auto_clock_timer_locked()
            if not _clear_closed_active_shift_locked(state):
                _clear_active_auto_clock_out_locked(state)
            save_work_state(state)
            refresh_tray_status_from_state(state)
            _audit_result("work.restore_auto_clock_out", True, reason)
            return

        # Recompute target auto clock-out from current weekly remaining hours.
        # This keeps persisted schedules aligned after config/logic updates.
        if WORK_CLOCK_CAPPED and active and allowed and not manual_override:
            recomputed_dt, recompute_err = _compute_auto_out_for_active_shift(state)
            if not recompute_err and recomputed_dt:
                recomputed_iso = recomputed_dt.isoformat()
                if active.get("auto_clock_out_at") != recomputed_iso:
                    _set_active_auto_clock_out_locked(state, recomputed_dt, source="auto")
                    save_work_state(state)
                auto_iso = recomputed_iso

        refresh_tray_status_from_state(state)

    if not WORK_CLOCK_CAPPED or not auto_iso:
        return
    try:
        auto_dt = datetime.fromisoformat(auto_iso)
    except ValueError:
        return
    if auto_dt <= datetime.now():
        auto_dt = datetime.now() + timedelta(minutes=1)
    schedule_auto_clock_out(auto_dt)


def ensure_auto_clock_out_schedule_if_needed(force_recompute=False):
    """
    Ensure an active shift has an auto clock-out timestamp and timer when capped mode is on.
    Returns (changed: bool, message: str).
    """
    if not WORK_CLOCK_CAPPED:
        return False, "CAPPED mode is off."

    now = datetime.now()
    with state_lock:
        state = load_work_state()
        active = state.get("active_shift") or {}
        infer_note = ""
        if not active:
            inferred, infer_note = _infer_active_shift_from_paycom_rows(state, now=now)
            if inferred:
                save_work_state(state)
                active = state.get("active_shift") or {}
            else:
                refresh_tray_status_from_state(state)
                msg = "No active shift found."
                if infer_note:
                    msg = f"{msg} {infer_note}"
                return False, msg
        allowed, reason = _auto_clock_out_allowed_for_active_shift(active, state=state)
        if not allowed:
            changed = False
            if auto_clock_timer is not None:
                _cancel_auto_clock_timer_locked()
                changed = True
            if _clear_closed_active_shift_locked(state, now=now):
                changed = True
            elif _clear_active_auto_clock_out_locked(state):
                changed = True
            if changed:
                save_work_state(state)
            refresh_tray_status_from_state(state)
            return False, reason
        manual_override = bool(active.get("manual_auto_clock_out"))
        has_auto = bool(active.get("auto_clock_out_at"))
        has_timer = auto_clock_timer is not None
        auto_iso = active.get("auto_clock_out_at")

    if has_auto and manual_override:
        if has_timer:
            try:
                manual_dt = datetime.fromisoformat(auto_iso)
                return False, f"Manual auto clock-out override remains scheduled for {_format_auto_clock_out_label(manual_dt, now=now)}."
            except ValueError:
                return False, "Manual auto clock-out override remains scheduled."
        try:
            manual_dt = datetime.fromisoformat(auto_iso)
        except ValueError:
            manual_dt = None
        if manual_dt is None:
            return False, "Manual auto clock-out override has an invalid saved time."
        if manual_dt <= now:
            manual_dt = now + timedelta(minutes=1)
        schedule_auto_clock_out(manual_dt)
        return True, f"Manual auto clock-out override remains scheduled for {_format_auto_clock_out_label(manual_dt, now=now)}."

    if has_auto and has_timer and not force_recompute:
        return False, "Auto clock-out already scheduled."

    ok, msg = schedule_auto_clock_out_from_active_shift()
    return ok, msg


def _build_auto_clock_payload(state):
    now = datetime.now()
    active = state.get("active_shift") or {}
    manual_override = bool(active.get("manual_auto_clock_out"))
    auto_allowed_today = bool(WORK_CLOCK_CAPPED)
    auto_allowed_for_shift = bool(WORK_CLOCK_CAPPED)
    blocked_reason = ""
    if WORK_CLOCK_CAPPED and active:
        auto_allowed_for_shift, blocked_reason = _auto_clock_out_allowed_for_active_shift(active, now=now, state=state)
    total_paid = _safe_float(state.get("total_paid_hours"), 0.0)
    remaining_to_cap = max(0.0, WORK_CLOCK_CAP_HOURS - total_paid) if WORK_CLOCK_CAPPED else None
    planned_paid_today = None
    if WORK_CLOCK_CAPPED:
        planned_paid_today = _planned_paid_hours_to_weekly_cap(total_paid)

    auto_iso = active.get("auto_clock_out_at") if active else None
    auto_dt = None
    if auto_iso:
        try:
            auto_dt = datetime.fromisoformat(auto_iso)
        except ValueError:
            auto_dt = None

    slack_out_message = _get_slack_message_for_day("out", now)
    if not WORK_CLOCK_CAPPED:
        status_text = "Auto clock-out disabled because CAPPED is False."
    elif not active:
        status_text = "Auto clock-out not scheduled yet. Run Work In to start an active shift."
    elif not auto_allowed_for_shift:
        status_text = blocked_reason or "Auto clock-out unavailable because the active shift is not open."
    elif auto_dt:
        if manual_override:
            status_text = (
                f"Auto clock-out manually set for {_format_auto_clock_out_label(auto_dt, now=now)} "
                f"({WORK_CLOCK_BREAK_MINUTES}m unpaid break applies only after "
                f"{WORK_CLOCK_BREAK_APPLIES_AFTER_HOURS:g}h)."
            )
        else:
            status_text = (
                f"Auto clock-out scheduled for {_format_auto_clock_out_label(auto_dt, now=now)} "
                f"({WORK_CLOCK_BREAK_MINUTES}m unpaid break applies only after "
                f"{WORK_CLOCK_BREAK_APPLIES_AFTER_HOURS:g}h)."
            )
    else:
        status_text = "Active shift found, but auto clock-out time is missing. Click Schedule Auto Out."

    return {
        "capped": WORK_CLOCK_CAPPED,
        "active_shift": bool(active),
        "active_shift_source": active.get("source"),
        "timer_active": bool(auto_clock_timer),
        "auto_clock_out_at": auto_iso,
        "scheduled": bool(auto_dt) and auto_allowed_for_shift,
        "manual_override": manual_override,
        "auto_clock_out_source": (
            active.get("auto_clock_out_source")
            or ("manual" if manual_override else ("auto" if auto_iso else None))
        ),
        "friday_only": False,
        "auto_allowed_today": auto_allowed_today,
        "auto_allowed_for_active_shift": auto_allowed_for_shift,
        "status_text": status_text,
        "remaining_to_cap_hours": round(remaining_to_cap, 2) if remaining_to_cap is not None else None,
        "planned_paid_hours_today": round(planned_paid_today, 2) if planned_paid_today is not None else None,
        "break_minutes": int(WORK_CLOCK_BREAK_MINUTES),
        "break_applies_after_hours": round(_safe_float(WORK_CLOCK_BREAK_APPLIES_AFTER_HOURS, 4.0), 2),
        "auto_out_max_hours": round(_max_auto_clock_out_horizon_hours(), 2),
        "slack_out_message_today": slack_out_message,
    }


def _run_clock_action(action, dry_run=False):
    mode_flag = "--dry-run" if dry_run else "--real"
    label = "ClockTest" if dry_run else "Clock"
    # Paycom clock script can run a second visible-mode fallback when headless is unstable.
    return _run_automation_script(CLOCK_SCRIPT, action, label, extra_args=[mode_flag], timeout=180)


def _is_retryable_clock_failure(message):
    text = str(message or "").lower()
    if "force-stopped" in text or "force stopped" in text:
        return False
    retry_signals = (
        "session not created",
        "devtoolsactiveport",
        "chrome failed to start",
        "timed out",
        "renderer",
        "unable to discover open pages",
        "disconnected: not connected to devtools",
        "invalid session id",
        "still on paycom login page after submitting pin",
        "failed (exit code",
    )
    return any(signal in text for signal in retry_signals)


def _run_clock_action_with_retry(action, dry_run=False, retries=1, delay_seconds=3):
    ok, msg = _run_clock_action(action, dry_run=dry_run)
    if ok:
        return ok, msg

    if not _is_retryable_clock_failure(msg):
        return ok, msg

    for attempt in range(1, retries + 1):
        if _automation_stop_is_blocking():
            return False, _force_stop_message(f"Clock-{action}")
        time.sleep(delay_seconds)
        ok, msg2 = _run_clock_action(action, dry_run=dry_run)
        if ok:
            return True, f"{msg2} (retry {attempt} succeeded)"
        msg = msg2
    return False, msg


def run_clock(action, dry_run=False):
    mode_name = "test" if dry_run else "real"
    automation_name = f"clock.{action}.{mode_name}"
    if not clock_lock.acquire(blocking=False):
        msg = "Another operation is already running."
        _audit_result(automation_name, False, msg)
        return False, msg
    try:
        prefix_notes = []
        if action == "in" and not dry_run and WORK_CLOCK_SYNC_FROM_PAYCOM and WORK_CLOCK_SYNC_BEFORE_CLOCK_IN:
            s_ok, s_msg, s_hours, merged_days, pto_days = _sync_paycom_hours_into_work_state("clock-in-direct")
            if s_ok:
                prefix_notes.append(f"Paycom sync before clock-in: week is {s_hours:.2f}h ({merged_days} daily rows).")
                if pto_days:
                    prefix_notes.append(f"Possible PTO/paid leave rows detected: {pto_days}.")
                if WORK_CLOCK_CAPPED and s_hours >= WORK_CLOCK_CAP_HOURS:
                    msg = (
                        f"{' '.join(prefix_notes)} "
                        f"Weekly cap already reached ({s_hours:.2f}/{WORK_CLOCK_CAP_HOURS:.2f} hours). Clock-in skipped."
                    ).strip()
                    _audit_result(automation_name, False, msg)
                    return False, msg
            else:
                with state_lock:
                    local_total = _safe_float(load_work_state().get("total_paid_hours"), 0.0)
                prefix_notes.append(
                    f"Paycom sync before clock-in failed: {s_msg} Using local total {local_total:.2f}h."
                )

        ok, msg = _run_clock_action_with_retry(action, dry_run=dry_run, retries=1, delay_seconds=3)
        if ok and action == "in" and not dry_run:
            now = datetime.now()
            auto_out_dt = None
            active_already_tracked = False
            with state_lock:
                state = load_work_state(now)
                active = state.get("active_shift") or {}
                if not active:
                    total_paid = _safe_float(state.get("total_paid_hours"), 0.0)
                    auto_out_note = ""
                    if WORK_CLOCK_CAPPED and total_paid < WORK_CLOCK_CAP_HOURS:
                        auto_out_dt, auto_out_note = _compute_auto_out_for_new_clock_in(total_paid, now)

                    day_key = now.date().isoformat()
                    day_entry = state.setdefault("days", {}).get(day_key, {})
                    day_entry["clock_in_at"] = now.isoformat()
                    day_entry["clock_out_at"] = None
                    day_entry["break_minutes"] = WORK_CLOCK_BREAK_MINUTES
                    day_entry["auto_clock_out_at"] = auto_out_dt.isoformat() if auto_out_dt else None
                    day_entry["manual_auto_clock_out"] = False
                    day_entry["auto_clock_out_source"] = "auto" if auto_out_dt else None
                    state["days"][day_key] = day_entry
                    state["active_shift"] = {
                        "date": day_key,
                        "clock_in_at": now.isoformat(),
                        "auto_clock_out_at": auto_out_dt.isoformat() if auto_out_dt else None,
                        "automatic_mode": bool(auto_out_dt),
                        "manual_auto_clock_out": False,
                        "auto_clock_out_source": "auto" if auto_out_dt else None,
                        "source": "clock-direct",
                    }
                    save_work_state(state)
                else:
                    active_already_tracked = True
                refresh_tray_status_from_state(state)

            if auto_out_dt:
                schedule_auto_clock_out(auto_out_dt)
                prefix_notes.append(f"Auto clock-out scheduled for {_format_auto_clock_out_label(auto_out_dt)}.")
                notify_user("Paycom Clock In", f"Auto clock-out {_format_auto_clock_out_label(auto_out_dt)}.")
            elif active_already_tracked and WORK_CLOCK_CAPPED:
                sch_ok, sch_msg = ensure_auto_clock_out_schedule_if_needed(force_recompute=True)
                prefix_notes.append(sch_msg)
            elif WORK_CLOCK_CAPPED:
                prefix_notes.append(auto_out_note or "Auto clock-out was not scheduled because cap is reached.")

        if ok and action == "out" and not dry_run and WORK_CLOCK_SYNC_FROM_PAYCOM:
            s_ok, s_msg, s_hours, merged_days, pto_days = _sync_paycom_hours_into_work_state("clock-out-direct")
            if s_ok:
                msg = f"{msg} Paycom sync after clock-out: week is {s_hours:.2f}h ({merged_days} daily rows)."
                if pto_days:
                    msg += f" Possible PTO/paid leave rows detected: {pto_days}."
            else:
                msg = f"{msg} Paycom sync after clock-out failed: {s_msg}"
        if prefix_notes:
            msg = f"{msg} {' '.join(prefix_notes)}".strip()
        _audit_result(automation_name, ok, msg)
        return ok, msg
    finally:
        clock_lock.release()


def run_slack(action, custom_message=None, force_test_url=False):
    action_key = str(action or "").strip().lower()
    automation_name = f"slack.{action_key or 'unknown'}"
    if action_key not in ("in", "out", "custom"):
        msg = "Invalid action argument."
        _audit_result(automation_name, False, msg)
        return False, msg

    custom_text = ""
    if action_key == "custom":
        custom_text = str(custom_message or "").strip()
        if not custom_text:
            msg = "Custom Slack message cannot be empty."
            _audit_result(automation_name, False, msg)
            return False, msg

    if not clock_lock.acquire(blocking=False):
        msg = "Another operation is already running."
        _audit_result(automation_name, False, msg)
        return False, msg
    try:
        if action_key == "custom":
            ok, msg = _run_slack_custom_action_with_retry(
                custom_text,
                retries=1,
                delay_seconds=3,
                force_test_url=force_test_url,
            )
        elif force_test_url:
            ok, msg = _run_slack_action_with_retry(
                action_key,
                retries=1,
                delay_seconds=3,
                extra_args=["--test-url"],
                label="SlackTest",
            )
        else:
            ok, msg = _run_slack_action_with_retry(action_key, retries=1, delay_seconds=3)
        _audit_result(automation_name, ok, msg)
        return ok, msg
    finally:
        clock_lock.release()


def _run_slack_action_with_retry(action, retries=1, delay_seconds=3, extra_args=None, label="Slack"):
    ok, msg = _run_automation_script(
        SLACK_SCRIPT,
        action,
        label,
        extra_args=extra_args,
        timeout=SLACK_SCRIPT_TIMEOUT_SECONDS,
    )
    if ok:
        return ok, msg
    if "force-stopped" in str(msg).lower() or "force stopped" in str(msg).lower():
        return ok, msg
    for attempt in range(1, retries + 1):
        if _automation_stop_is_blocking():
            return False, _force_stop_message(f"{label}-{action}")
        time.sleep(delay_seconds)
        ok, msg2 = _run_automation_script(
            SLACK_SCRIPT,
            action,
            label,
            extra_args=extra_args,
            timeout=SLACK_SCRIPT_TIMEOUT_SECONDS,
        )
        if ok:
            return True, f"{msg2} (retry {attempt} succeeded)"
        msg = msg2
    return False, msg


def _run_slack_custom_action_with_retry(custom_message, retries=1, delay_seconds=3, force_test_url=False):
    message = str(custom_message or "").strip()
    if not message:
        return False, "Custom Slack message cannot be empty."

    extra_args = ["--message", message]
    if force_test_url:
        extra_args.append("--test-url")
    label = "SlackCustomTest" if force_test_url else "SlackCustom"
    return _run_slack_action_with_retry(
        "custom",
        retries=retries,
        delay_seconds=delay_seconds,
        extra_args=extra_args,
        label=label,
    )


def _run_slack_test_action_with_retry(action, retries=1, delay_seconds=3):
    return _run_slack_action_with_retry(
        action,
        retries=retries,
        delay_seconds=delay_seconds,
        extra_args=["--test-url"],
        label="SlackTest",
    )


def _resolve_slack_day_message(prefix, fallback, now=None):
    if now is None:
        now = datetime.now()
    day_index = (now.weekday() + 1) % 7
    day_name = DAY_NAMES[day_index]
    key = f"{prefix}_{day_name}"
    raw = getattr(config_module, key, fallback)
    message = str(raw if raw is not None else fallback).strip()
    if not message:
        message = str(fallback or "").strip()
    return day_name, message


def _resolve_slack_lunch_messages(now=None):
    day_name, start_message = _resolve_slack_day_message(
        "SLACK_MESSAGE_LUNCH",
        SLACK_LUNCH_START_MESSAGE_FALLBACK,
        now=now,
    )
    _day_name_back, return_message = _resolve_slack_day_message(
        "SLACK_MESSAGE_BACK",
        SLACK_LUNCH_RETURN_MESSAGE_FALLBACK,
        now=now,
    )
    return day_name, start_message, return_message


def _clear_lunch_break_timer_locked():
    global lunch_return_timer
    if lunch_return_timer:
        lunch_return_timer.cancel()
        lunch_return_timer = None
    lunch_break_state["active"] = False
    lunch_break_state["started_at"] = None
    lunch_break_state["return_at"] = None
    lunch_break_state["start_message"] = None
    lunch_break_state["return_message"] = None
    lunch_break_state["day_name"] = None
    lunch_break_state["force_test_url"] = False


def get_slack_lunch_payload(now=None):
    if now is None:
        now = datetime.now()
    with lunch_timer_lock:
        started_at = lunch_break_state.get("started_at")
        return_at = lunch_break_state.get("return_at")
        start_message = lunch_break_state.get("start_message")
        return_message = lunch_break_state.get("return_message")
        day_name = lunch_break_state.get("day_name")
        force_test_url = bool(lunch_break_state.get("force_test_url"))
        active = bool(lunch_break_state.get("active") and lunch_return_timer is not None and isinstance(return_at, datetime))
    remaining_seconds = 0
    if active and isinstance(return_at, datetime):
        remaining_seconds = max(0, int(math.ceil((return_at - now).total_seconds())))
    status_text = (
        f"Lunch timer active: {_format_countdown_hms(remaining_seconds)} remaining."
        if active
        else "No active lunch timer."
    )
    return {
        "success": True,
        "active": active,
        "status_text": status_text,
        "start_message": str(start_message or SLACK_LUNCH_START_MESSAGE_FALLBACK),
        "return_message": str(return_message or SLACK_LUNCH_RETURN_MESSAGE_FALLBACK),
        "day_name": day_name,
        "force_test_url": force_test_url,
        "channel_mode": ("test" if force_test_url else "production"),
        "duration_seconds": SLACK_LUNCH_BREAK_SECONDS,
        "started_at": started_at.isoformat() if isinstance(started_at, datetime) else None,
        "return_at": return_at.isoformat() if isinstance(return_at, datetime) else None,
        "remaining_seconds": remaining_seconds,
        "remaining_text": _format_countdown_hms(remaining_seconds),
    }


def _run_slack_custom_when_available(custom_message, wait_seconds=120, force_test_url=False):
    wait_for = max(1, int(_safe_float(wait_seconds, 120)))
    deadline = time.time() + wait_for
    while True:
        if clock_lock.acquire(blocking=False):
            try:
                return _run_slack_custom_action_with_retry(
                    custom_message,
                    retries=2,
                    delay_seconds=3,
                    force_test_url=force_test_url,
                )
            finally:
                clock_lock.release()
        if time.time() >= deadline:
            return False, "Another operation is already running."
        time.sleep(1.0)


def _slack_lunch_return_timer_callback():
    with lunch_timer_lock:
        scheduled_at = lunch_break_state.get("return_at")
        return_message = str(lunch_break_state.get("return_message") or SLACK_LUNCH_RETURN_MESSAGE_FALLBACK)
        force_test_url = bool(lunch_break_state.get("force_test_url"))
        day_name = lunch_break_state.get("day_name")
        _clear_lunch_break_timer_locked()

    def _queued_lunch_return():
        ok, msg = _run_slack_custom_when_available(
            return_message,
            wait_seconds=120,
            force_test_url=force_test_url,
        )
        summary = msg
        if isinstance(scheduled_at, datetime):
            summary = (
                f"{msg} (scheduled return at {scheduled_at.isoformat()}, "
                f"day={day_name or 'unknown'}, channel={'test' if force_test_url else 'production'})"
            )
        _audit_result("slack.lunch.return", ok, summary)
        notify_user("Slack Lunch Return", msg if ok else f"Failed: {msg}")
        return ok, summary

    enqueue_automation("Slack Lunch Return", "Communications", _queued_lunch_return)


def _start_slack_lunch_break_locked(force_test_url=False):
    global lunch_return_timer
    now = datetime.now()
    day_name, start_message, return_message = _resolve_slack_lunch_messages(now=now)
    if not start_message.strip():
        return False, f"Slack lunch message is empty for {day_name}. Check config.py."

    ok, msg = _run_slack_custom_action_with_retry(
        start_message,
        retries=1,
        delay_seconds=3,
        force_test_url=force_test_url,
    )
    if not ok:
        return False, f"Lunch start message failed: {msg}"

    return_at = now + timedelta(seconds=SLACK_LUNCH_BREAK_SECONDS)
    with lunch_timer_lock:
        had_active = bool(lunch_break_state.get("active") and lunch_return_timer is not None)
        _clear_lunch_break_timer_locked()
        timer = threading.Timer(SLACK_LUNCH_BREAK_SECONDS, _slack_lunch_return_timer_callback)
        timer.daemon = True
        timer.start()
        lunch_return_timer = timer
        lunch_break_state["active"] = True
        lunch_break_state["started_at"] = now
        lunch_break_state["return_at"] = return_at
        lunch_break_state["start_message"] = start_message
        lunch_break_state["return_message"] = return_message
        lunch_break_state["day_name"] = day_name
        lunch_break_state["force_test_url"] = bool(force_test_url)

    when = return_at.strftime("%I:%M:%S %p").lstrip("0")
    replace_note = " Replaced a previous active lunch timer." if had_active else ""
    channel_name = "test" if force_test_url else "production"
    summary = (
        f"Slack lunch started for {day_name.title()} on {channel_name} channel. "
        f"Sent '{start_message}'. Return message '{return_message}' is scheduled for {when} (in 01:00:00)."
        f"{replace_note}"
    )
    return True, summary


def start_slack_lunch_break(force_test_url=False):
    automation_name = "slack.lunch.start.test" if force_test_url else "slack.lunch.start"
    if not clock_lock.acquire(blocking=False):
        msg = "Another operation is already running."
        _audit_result(automation_name, False, msg)
        return False, msg
    try:
        ok, msg = _start_slack_lunch_break_locked(force_test_url=force_test_url)
        _audit_result(automation_name, ok, msg)
        return ok, msg
    finally:
        clock_lock.release()


def cancel_slack_lunch_break(audit=False):
    with lunch_timer_lock:
        was_active = bool(lunch_break_state.get("active") and lunch_return_timer is not None)
        _clear_lunch_break_timer_locked()
    msg = "Canceled active Slack lunch timer." if was_active else "No active Slack lunch timer."
    if audit:
        _audit_result("slack.lunch.cancel", True, msg)
    return True, msg


def _normalize_automation_suite_selection(selected):
    if selected is None:
        return list(AUTOMATION_TEST_IDS), []
    if not isinstance(selected, list):
        return [], ["Selection must be a list."]

    normalized = []
    invalid = []
    seen = set()
    for raw in selected:
        key = str(raw or "").strip().lower()
        if not key:
            continue
        if key not in AUTOMATION_TEST_IDS:
            invalid.append(key)
            continue
        if key in seen:
            continue
        normalized.append(key)
        seen.add(key)
    return normalized, invalid


def run_automation_test_suite(selected_tests=None):
    automation_name = "automation.test_suite"
    selected, invalid = _normalize_automation_suite_selection(selected_tests)
    if invalid:
        msg = f"Invalid test IDs: {', '.join(sorted(set(invalid)))}"
        _audit_result(automation_name, False, msg)
        return False, msg, []
    if not selected:
        msg = "No tests selected."
        _audit_result(automation_name, False, msg)
        return False, msg, []

    if not clock_lock.acquire(blocking=False):
        msg = "Another operation is already running."
        _audit_result(automation_name, False, msg)
        return False, msg, []

    catalog = {item["id"]: item for item in AUTOMATION_TEST_CATALOG}
    results = []
    try:
        for test_id in selected:
            item = catalog[test_id]
            started = time.time()
            if item["kind"] == "paycom":
                ok, msg = _run_clock_action_with_retry(item["action"], dry_run=True, retries=1, delay_seconds=2)
            elif item["kind"] == "slack":
                ok, msg = _run_slack_test_action_with_retry(item["action"], retries=1, delay_seconds=3)
            elif item["kind"] == "slack_lunch":
                ok, msg = _start_slack_lunch_break_locked(force_test_url=True)
            else:
                ok, msg = False, f"Unsupported test kind: {item['kind']}"

            results.append(
                {
                    "id": test_id,
                    "label": item["label"],
                    "success": bool(ok),
                    "message": msg,
                    "duration_seconds": round(time.time() - started, 1),
                }
            )

        passed = sum(1 for r in results if r["success"])
        total = len(results)
        failed_labels = [r["label"] for r in results if not r["success"]]
        if passed == total:
            summary = f"Automation suite passed ({passed}/{total})."
        else:
            summary = f"Automation suite had failures ({passed}/{total} passed). Failed: {', '.join(failed_labels)}."
        ok_all = passed == total
        _audit_result(automation_name, ok_all, summary)
        return ok_all, summary, results
    finally:
        clock_lock.release()


def _default_crm_state():
    return {
        "last_run_timestamp": None,
        "last_run_success": None,
        "last_run_message": None,
        "last_run_duration_seconds": None,
        "last_stage_timings": [],
        "last_order_count": 0,
        "last_order_goods_parallel_workers": 1,
        "saved_order_goods_parallel_workers": 1,
        "last_product_separator_parallel_workers": 1,
        "saved_product_separator_parallel_workers": 1,
        "saved_auto_splitter_parallel_workers": 1,
        "total_runs": 0,
        "total_orders_processed": 0,
        "last_order_ids": [],
        "run_history": [],
        "auto_splitter_run_history": [],
    }


def ensure_crm_state_file():
    if os.path.exists(CRM_STATE_FILE):
        return
    with open(CRM_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(_default_crm_state(), f, indent=2)


def load_crm_state():
    state = _default_crm_state()
    if os.path.exists(CRM_STATE_FILE):
        try:
            with open(CRM_STATE_FILE, "r", encoding="utf-8-sig") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                state.update(loaded)
        except Exception as e:
            logger.warning("Could not read %s: %s", CRM_STATE_FILE, e)

    state["last_order_count"] = max(0, int(_safe_float(state.get("last_order_count"), 0)))
    state["last_run_duration_seconds"] = _normalize_duration_seconds(state.get("last_run_duration_seconds"))
    state["last_stage_timings"] = _normalize_stage_timings(state.get("last_stage_timings"))
    state["last_order_goods_parallel_workers"] = _normalize_crm_positive_int(
        state.get("last_order_goods_parallel_workers"),
        default=1,
        minimum=1,
        maximum=CRM_ORDER_GOODS_MAX_PARALLEL_WORKERS,
    )
    state["saved_order_goods_parallel_workers"] = _normalize_crm_positive_int(
        state.get("saved_order_goods_parallel_workers"),
        default=1,
        minimum=1,
        maximum=CRM_ORDER_GOODS_MAX_PARALLEL_WORKERS,
    )
    state["last_product_separator_parallel_workers"] = _normalize_crm_positive_int(
        state.get("last_product_separator_parallel_workers"),
        default=1,
        minimum=1,
        maximum=CRM_SHARED_MAX_PARALLEL_WORKERS,
    )
    state["saved_product_separator_parallel_workers"] = _normalize_crm_positive_int(
        state.get("saved_product_separator_parallel_workers"),
        default=1,
        minimum=1,
        maximum=CRM_SHARED_MAX_PARALLEL_WORKERS,
    )
    state["saved_auto_splitter_parallel_workers"] = _normalize_crm_positive_int(
        state.get("saved_auto_splitter_parallel_workers"),
        default=1,
        minimum=1,
        maximum=CRM_SHARED_MAX_PARALLEL_WORKERS,
    )
    state["total_runs"] = max(0, int(_safe_float(state.get("total_runs"), 0)))
    state["total_orders_processed"] = max(0, int(_safe_float(state.get("total_orders_processed"), 0)))
    state["last_order_ids"] = _extract_crm_order_ids({"order_ids": state.get("last_order_ids")})
    history = state.get("run_history") if isinstance(state.get("run_history"), list) else []
    auto_history = state.get("auto_splitter_run_history") if isinstance(state.get("auto_splitter_run_history"), list) else []
    last_result_payload = _load_last_result_payload()
    cleaned_history = []
    migrated_auto_history = list(auto_history)
    for entry in history[:20]:
        if not isinstance(entry, dict):
            continue
        row = dict(entry)
        row["automation_key"] = str(row.get("automation_key") or "stock_unlocker")
        row["automation_label"] = str(row.get("automation_label") or "Stock Unlocker")
        if row["automation_key"] == "auto_splitter":
            migrated_auto_history.append(row)
            continue
        row["duration_seconds"] = _normalize_duration_seconds(row.get("duration_seconds"))
        row["stage_timings"] = _normalize_stage_timings(row.get("stage_timings"))
        row["order_ids"] = _extract_crm_order_ids({"order_ids": row.get("order_ids")})
        if (
            row["automation_key"] == "order_goods"
            and not row.get("order_results")
            and isinstance(last_result_payload, dict)
            and str(last_result_payload.get("action") or "") == "order_goods_batch"
            and _extract_crm_order_ids(last_result_payload) == row["order_ids"]
        ):
            row["order_results"] = _build_crm_order_goods_order_results(last_result_payload)
        row["order_results"] = _normalize_crm_stock_order_results(
            row.get("order_results"),
            row["order_ids"],
            row.get("success", True),
            row.get("message", ""),
        )
        if row["automation_key"] == "shipping_bypasser":
            row["message"] = _compact_crm_shipping_bypasser_history_message(row.get("message") or "")
        cleaned_history.append(row)
    state["run_history"] = cleaned_history
    if cleaned_history:
        latest = cleaned_history[0]
        if (
            latest.get("automation_key") == "shipping_bypasser"
            and latest.get("timestamp") == state.get("last_run_timestamp")
            and latest.get("message")
        ):
            state["last_run_message"] = latest.get("message")
    state["auto_splitter_run_history"] = _normalize_crm_auto_splitter_history(migrated_auto_history)
    state = _backfill_crm_address_state_from_last_result(state)
    return state


def _write_json_file_atomic(path, data):
    directory = os.path.dirname(os.path.abspath(path)) or "."
    temp_path = os.path.join(directory, f".{os.path.basename(path)}.{os.getpid()}.{threading.get_ident()}.tmp")
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp_path, path)


def save_crm_state(state):
    _write_json_file_atomic(CRM_STATE_FILE, state)


def _crm_runtime_snapshot():
    with crm_runtime_lock:
        return dict(crm_runtime)


def _extract_crm_order_count(payload):
    if not isinstance(payload, dict):
        return 0
    raw = payload.get("order_count")
    if raw is None:
        raw = payload.get("last_order_count")
    return max(0, int(_safe_float(raw, 0)))


def _extract_crm_order_ids(payload):
    if not isinstance(payload, dict):
        return []
    raw_ids = payload.get("order_ids")
    if not isinstance(raw_ids, list):
        return []
    cleaned = []
    seen = set()
    for raw in raw_ids:
        text = "".join(ch for ch in str(raw) if ch.isdigit())
        if len(text) != 7 or text in seen:
            continue
        seen.add(text)
        cleaned.append(text)
    return cleaned


def _normalize_crm_stock_order_results(items, fallback_order_ids=None, fallback_success=True, fallback_message=""):
    rows = items if isinstance(items, list) else []
    cleaned = []
    seen = set()
    for item in rows[:100]:
        if not isinstance(item, dict):
            continue
        order_ids = _extract_crm_order_ids({"order_ids": [item.get("order_id")]})
        if not order_ids:
            continue
        order_id = order_ids[0]
        tab_index = item.get("stock_tab_index")
        tab_count = item.get("stock_tab_count")
        tab_label = str(item.get("stock_tab_label") or "").strip()
        if tab_index is not None or tab_count is not None or tab_label:
            result_key = (order_id, str(tab_index or ""), str(tab_count or ""), tab_label)
        else:
            result_key = (order_id,)
        if result_key in seen:
            continue
        seen.add(result_key)
        row = {
            "order_id": order_id,
            "success": bool(item.get("success")),
            "status": str(item.get("status") or ("Success" if item.get("success") else "Needs attention")),
            "outcome": str(item.get("outcome") or ""),
            "message": str(item.get("message") or ""),
            "duration_seconds": _normalize_duration_seconds(item.get("duration_seconds") or item.get("session_duration_seconds")),
            "sanmar_confirmation": item.get("sanmar_confirmation") if isinstance(item.get("sanmar_confirmation"), dict) else None,
        }
        if tab_index is not None:
            row["stock_tab_index"] = tab_index
        if tab_count is not None:
            row["stock_tab_count"] = tab_count
        if tab_label:
            row["stock_tab_label"] = tab_label
        if isinstance(item.get("partial_success_details"), list):
            row["partial_success_details"] = item.get("partial_success_details")
        for key in ("warehouse", "warehouses", "eta", "eta_by_warehouse"):
            if item.get(key) is not None:
                row[key] = item.get(key)
        cleaned.append(row)
    if cleaned:
        return cleaned
    fallback_success = bool(fallback_success)
    fallback_message = "" if fallback_success else str(fallback_message or "")
    return [
        {
            "order_id": order_id,
            "success": fallback_success,
            "status": "Success" if fallback_success else "Needs attention",
            "outcome": "",
            "message": fallback_message,
        }
        for order_id in _extract_crm_order_ids({"order_ids": fallback_order_ids or []})
    ]


def _crm_order_goods_outcome_label(outcome, success):
    key = str(outcome or "").strip().lower()
    if key == "partial_success":
        return "Partially successful"
    if key == "already_stock_ordered":
        return "Already stock ordered"
    if key == "order_goods_locked":
        return "Locked"
    if key == "order_goods_clicked":
        return "Ordered"
    if key == "order_goods_ready":
        return "Ready"
    if key == "shipping_bypass_ordered":
        return "Bypassed"
    if key == "shipping_bypass_ready":
        return "Ready"
    if key in {"push_back_saved", "push_back_saved_stock_ordered"}:
        return "Pushed"
    if key == "push_back_saved_stock_failed":
        return "Stock needs attention"
    if key in {"push_back_ready", "push_back_ready_stock_ready"}:
        return "Ready"
    if key == "push_back_ready_stock_failed":
        return "Stock dry run needs attention"
    if key in {"due_date_guard_skipped", "stock_already_ordered_skipped", "missing_production_date", "missing_due_date", "missing_order_production_date", "missing_order_due_date"}:
        return "Skipped"
    if key in {"sanmar_cart_not_empty", "eta_on_or_after_due_date", "no_single_warehouse", "multiple_warehouses", "checkout_warehouse_mismatch", "sanmar_product_mismatch"}:
        return "Skipped"
    return "Success" if success else "Needs attention"


def _crm_stock_tab_descriptor(item):
    if not isinstance(item, dict):
        return ""
    tab_index = item.get("stock_tab_index")
    tab_count = item.get("stock_tab_count")
    label = str(item.get("stock_tab_label") or "").strip()
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


def _crm_partial_stock_message(failed_items):
    parts = []
    for item in failed_items if isinstance(failed_items, list) else []:
        descriptor = _crm_stock_tab_descriptor(item)
        message = str(item.get("message") or "").strip()
        if descriptor and message:
            parts.append(f"{descriptor}: {message}")
        elif descriptor:
            parts.append(descriptor)
        elif message:
            parts.append(message)
    if not parts:
        return "Partially successful; at least one stock tab needs attention."
    return "Partially successful; skipped " + "; ".join(parts[:4]) + "."


def _build_crm_order_goods_order_results(payload):
    if not isinstance(payload, dict):
        return []
    report = payload.get("report") if isinstance(payload.get("report"), list) else []
    by_order = {}
    for item in report:
        if not isinstance(item, dict):
            continue
        order_ids = _extract_crm_order_ids({"order_ids": [item.get("order_id")]})
        if not order_ids:
            continue
        order_id = order_ids[0]
        bucket = by_order.setdefault(order_id, {"items": []})
        bucket["items"].append(item)

    results = []
    for order_id in _extract_crm_order_ids(payload):
        items = by_order.get(order_id, {}).get("items") or []
        if not items:
            results.append(
                {
                    "order_id": order_id,
                    "success": bool(payload.get("success")),
                    "status": "Success" if payload.get("success") else "Needs attention",
                    "outcome": "",
                    "message": str(payload.get("message") or ""),
                }
            )
            continue
        success = all(bool(item.get("success")) for item in items)
        failed_items = [item for item in items if not item.get("success")]
        partial_success = bool(failed_items) and any(bool(item.get("success")) for item in items)
        already_ordered_items = [item for item in items if str(item.get("outcome") or "") == "already_stock_ordered"]
        primary = failed_items[0] if failed_items else (already_ordered_items[0] if already_ordered_items else items[-1])
        unique_messages = []
        for item in items:
            msg = str(item.get("message") or "").strip()
            if msg and msg not in unique_messages:
                unique_messages.append(msg)
        tab_count = len(items)
        base_message = _crm_partial_stock_message(failed_items) if partial_success else str(primary.get("message") or "").strip()
        message = base_message or "; ".join(unique_messages)
        if tab_count > 1:
            message = f"{message} ({tab_count} stock tab(s) checked.)".strip()
        duration_values = [
            _normalize_duration_seconds(item.get("duration_seconds") or item.get("session_duration_seconds"))
            for item in items
        ]
        duration_values = [value for value in duration_values if value is not None]
        results.append(
            {
                "order_id": order_id,
                "success": success,
                "status": "Partially successful" if partial_success else _crm_order_goods_outcome_label(primary.get("outcome"), success),
                "outcome": "partial_success" if partial_success else str(primary.get("outcome") or ""),
                "message": message,
                "duration_seconds": max(duration_values) if duration_values else None,
                "sanmar_confirmation": primary.get("sanmar_confirmation") if isinstance(primary.get("sanmar_confirmation"), dict) else None,
            }
        )
    return results


CRM_SHIPPING_BYPASSER_STOCK_ORDER_SUCCESS_OUTCOMES = {"shipping_bypass_ordered"}


def _crm_shipping_bypasser_customer_po(item):
    if not isinstance(item, dict):
        return ""
    confirmation = item.get("sanmar_confirmation") if isinstance(item.get("sanmar_confirmation"), dict) else {}
    po = str(confirmation.get("po") or "").strip()
    if po:
        return po
    order = item.get("order") if isinstance(item.get("order"), dict) else {}
    po = str(order.get("po") or item.get("po") or "").strip()
    if po:
        return po
    tab_label = str(item.get("stock_tab_label") or "").strip()
    match = re.search(r"\b(H-[A-Za-z0-9]+)\b", tab_label, flags=re.I)
    return match.group(1) if match else ""


def _crm_shipping_bypasser_confirmation_url(item):
    if not isinstance(item, dict):
        return ""
    confirmation = item.get("sanmar_confirmation") if isinstance(item.get("sanmar_confirmation"), dict) else {}
    return str(confirmation.get("url") or "").strip()


def _crm_shipping_bypasser_ordered_stock_success_detail(item):
    if not isinstance(item, dict) or not item.get("success"):
        return None
    if str(item.get("outcome") or "") not in CRM_SHIPPING_BYPASSER_STOCK_ORDER_SUCCESS_OUTCOMES:
        return None
    descriptor = _crm_stock_tab_descriptor(item)
    po = _crm_shipping_bypasser_customer_po(item)
    url = _crm_shipping_bypasser_confirmation_url(item)
    if not descriptor or not po or not url:
        return None
    return {
        "stock_tab": descriptor,
        "po": po,
        "url": url,
        "sanmar_confirmation": item.get("sanmar_confirmation") if isinstance(item.get("sanmar_confirmation"), dict) else None,
    }


def _crm_shipping_bypasser_success_detail_text(details):
    parts = []
    seen = set()
    for detail in details if isinstance(details, list) else []:
        if not isinstance(detail, dict):
            continue
        text = f"{detail.get('stock_tab')}, customer PO {detail.get('po')}, SanMar confirmation {detail.get('url')}"
        if text in seen:
            continue
        seen.add(text)
        parts.append(text)
    return "; ".join(parts[:4])


def _crm_shipping_bypasser_ordered_stock_successfully(item):
    return _crm_shipping_bypasser_ordered_stock_success_detail(item) is not None


def _build_crm_shipping_bypasser_order_results(payload):
    if not isinstance(payload, dict):
        return []
    report = payload.get("report") if isinstance(payload.get("report"), list) else []
    order_success_details = {}
    has_non_cleanup_row = {}
    for item in report:
        if not isinstance(item, dict):
            continue
        order_ids = _extract_crm_order_ids({"order_ids": [item.get("order_id")]})
        if not order_ids:
            continue
        order_id = order_ids[0]
        success_detail = _crm_shipping_bypasser_ordered_stock_success_detail(item)
        if success_detail is not None:
            order_success_details.setdefault(order_id, []).append(success_detail)
        if str(item.get("outcome") or "") != "sanmar_cart_cleanup_failed":
            has_non_cleanup_row[order_id] = True

    results = []
    for item in report[:100]:
        if not isinstance(item, dict):
            continue
        order_ids = _extract_crm_order_ids({"order_ids": [item.get("order_id")]})
        if not order_ids:
            continue
        order_id = order_ids[0]
        if (
            str(item.get("outcome") or "") == "sanmar_cart_cleanup_failed"
            and has_non_cleanup_row.get(order_id)
        ):
            continue
        item_success = bool(item.get("success"))
        partial_details = order_success_details.get(order_id) or []
        partial_success = bool((not item_success) and partial_details)
        effective_success = bool(item_success or partial_success)
        status = (
            _crm_order_goods_outcome_label(item.get("outcome"), item_success)
            if item_success
            else ("Partially successful" if partial_success else "Needs attention")
        )
        message = str(item.get("message") or "")
        if partial_success:
            detail_text = _crm_shipping_bypasser_success_detail_text(partial_details)
            if detail_text:
                message = f"{message} Successful stock tab(s): {detail_text}.".strip()
        sanmar_confirmation = item.get("sanmar_confirmation") if isinstance(item.get("sanmar_confirmation"), dict) else None
        if partial_success and sanmar_confirmation is None:
            for detail in partial_details:
                candidate = detail.get("sanmar_confirmation") if isinstance(detail, dict) else None
                if isinstance(candidate, dict):
                    sanmar_confirmation = candidate
                    break
        results.append(
            {
                "order_id": order_id,
                "success": effective_success,
                "status": status,
                "outcome": str(item.get("outcome") or ""),
                "message": message,
                "duration_seconds": _normalize_duration_seconds(item.get("duration_seconds") or item.get("session_duration_seconds")),
                "sanmar_confirmation": sanmar_confirmation,
                "partial_success_details": partial_details if partial_success else [],
                "stock_tab_index": item.get("stock_tab_index"),
                "stock_tab_count": item.get("stock_tab_count"),
                "stock_tab_label": str(item.get("stock_tab_label") or ""),
            }
        )
    if results:
        return results
    return _normalize_crm_stock_order_results(
        [],
        _extract_crm_order_ids(payload),
        bool(payload.get("success")),
        str(payload.get("message") or ""),
    )


CRM_SHIPPING_BYPASSER_PARTIAL_DETAIL_RE = re.compile(
    r"(\b\d+\s+order\(s\)\s+partially succeeded)(?::\s*.*?)(?=(?:\s+\d+\s+order\(s\)\s+need attention:)|$)",
    flags=re.I | re.S,
)


def _compact_crm_shipping_bypasser_history_message(message):
    text = str(message or "").strip()
    if not text:
        return text
    text = re.split(r"\s+Stock tabs:\s*", text, maxsplit=1, flags=re.I)[0].strip()
    text = CRM_SHIPPING_BYPASSER_PARTIAL_DETAIL_RE.sub(r"\1.", text)
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"\.\s*\.", ".", text)


def _crm_shipping_bypasser_order_results_success(order_results):
    rows = order_results if isinstance(order_results, list) else []
    return bool(rows) and all(bool(item.get("success")) for item in rows if isinstance(item, dict))


def _crm_shipping_bypasser_history_tab_summary(order_results):
    rows = [row for row in (order_results if isinstance(order_results, list) else []) if isinstance(row, dict)]
    grouped = {}
    for row in rows:
        order_id = str(row.get("order_id") or "").strip()
        if order_id:
            grouped.setdefault(order_id, []).append(row)
    order_parts = []
    for order_id, items in grouped.items():
        has_multi_tab_context = len(items) > 1 or any(
            int(_safe_float(item.get("stock_tab_count"), 0)) > 1
            for item in items
            if isinstance(item, dict)
        )
        if not has_multi_tab_context:
            continue
        tab_parts = []
        for item in items:
            descriptor = _crm_stock_tab_descriptor(item)
            outcome = str(item.get("outcome") or "").strip().lower()
            if outcome == "shipping_bypass_ordered":
                po = _crm_shipping_bypasser_customer_po(item)
                url = _crm_shipping_bypasser_confirmation_url(item)
                detail = f"{descriptor} ordered stock" if descriptor else "ordered stock"
                if po:
                    detail = f"{detail}, customer PO {po}"
                if url:
                    detail = f"{detail}, SanMar confirmation {url}"
            elif outcome == "already_stock_ordered":
                detail = f"{descriptor} skipped because stock is already ordered" if descriptor else "skipped because stock is already ordered"
            else:
                status = str(item.get("status") or ("success" if item.get("success") else "needs attention")).strip()
                detail = f"{descriptor} {status}" if descriptor else status
            if detail and detail not in tab_parts:
                tab_parts.append(detail)
        if tab_parts:
            order_parts.append(f"{order_id}: " + "; ".join(tab_parts))
    if not order_parts:
        return ""
    return " Stock tabs: " + " | ".join(order_parts[:8]) + (" ..." if len(order_parts) > 8 else "") + "."


def _normalize_crm_single_order_id(raw):
    text = str(raw or "").strip()
    if not text:
        return None
    direct_match = re.search(r"/order/(\d{7})(?:\D|$)", text)
    if direct_match:
        return direct_match.group(1)
    matches = re.findall(r"\b\d{7}\b", text)
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    return matches[-1]


CRM_PROCESSING_FILTERS = ("rush", "free", "all", "813")
CRM_PROCESSING_GLOBAL_PREF_KEYS = ()
CRM_PROCESSING_MODE_PREF_KEYS = (
    "stock_unlocker_enabled",
    "address_validator_enabled",
    "product_separator_enabled",
    "order_goods_enabled",
    "shipping_bypasser_enabled",
    "push_back_enabled",
)
CRM_PROCESSING_PREF_KEYS = CRM_PROCESSING_GLOBAL_PREF_KEYS + CRM_PROCESSING_MODE_PREF_KEYS


def _normalize_crm_shipping_filter(value):
    key = str(value or "").strip().lower()
    return key if key in set(CRM_PROCESSING_FILTERS) else "free"


def _normalize_crm_address_action(value):
    key = str(value or "").strip().lower()
    return "validate_batch" if key in {"validate_batch", "batch"} else "validate_order"


def _normalize_crm_positive_int(value, default=1, minimum=1, maximum=25):
    number = int(_safe_float(value, default))
    return max(minimum, min(number, maximum))


def _normalize_crm_batch_size(value, default=1, minimum=1, maximum=25, allow_unlimited=False):
    if value is None:
        return None if allow_unlimited else _normalize_crm_positive_int(default, default=default, minimum=minimum, maximum=maximum)
    text = str(value).strip()
    if not text:
        return None if allow_unlimited else _normalize_crm_positive_int(default, default=default, minimum=minimum, maximum=maximum)
    if text.lower() in {"all", "continuous", "unlimited"}:
        return None if allow_unlimited else _normalize_crm_positive_int(default, default=default, minimum=minimum, maximum=maximum)
    number = int(_safe_float(text, default))
    if allow_unlimited and number <= 0:
        return None
    return max(minimum, min(number, maximum))


def _crm_batch_size_display(batch_size):
    normalized_batch_size = _normalize_crm_batch_size(batch_size, default=1, minimum=1, maximum=25, allow_unlimited=True)
    return "continuous" if normalized_batch_size is None else str(normalized_batch_size)


def _crm_batch_scope_phrase(batch_size):
    normalized_batch_size = _normalize_crm_batch_size(batch_size, default=1, minimum=1, maximum=25, allow_unlimited=True)
    if normalized_batch_size is None:
        return "continuously until no orders remain"
    return f"for up to {normalized_batch_size} order(s)"


def _normalize_crm_list_url(value):
    text = str(value or "").strip()
    return text or None


def _crm_processing_value_supplied(value):
    if value is None:
        return False
    return str(value).strip() != ""


def _normalize_crm_processing_enabled(value, default=False):
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if not text:
        return bool(default)
    if text in {"0", "false", "no", "off"}:
        return False
    if text in {"1", "true", "yes", "on"}:
        return True
    return bool(default)


def _default_crm_processing_mode_preferences(processing_filter):
    key = _normalize_crm_shipping_filter(processing_filter or "rush")
    defaults = {
        "stock_unlocker_enabled": key == "rush",
        "address_validator_enabled": True,
        "product_separator_enabled": key != "813",
        "order_goods_enabled": key in {"rush", "813"},
        "shipping_bypasser_enabled": key == "813",
        "push_back_enabled": False,
    }
    if key == "all":
        defaults.update(
            {
                "stock_unlocker_enabled": False,
                "order_goods_enabled": False,
                "shipping_bypasser_enabled": False,
                "push_back_enabled": False,
            }
        )
    elif key == "free":
        defaults.update(
            {
                "stock_unlocker_enabled": False,
                "order_goods_enabled": False,
                "shipping_bypasser_enabled": False,
                "push_back_enabled": False,
            }
        )
    elif key == "813":
        defaults.update(
            {
                "stock_unlocker_enabled": False,
                "product_separator_enabled": False,
            }
        )
    return defaults


def _sanitize_crm_processing_mode_preferences(processing_filter, values=None):
    key = _normalize_crm_shipping_filter(processing_filter or "rush")
    source = values if isinstance(values, dict) else {}
    prefs = _default_crm_processing_mode_preferences(key)
    for pref_key in CRM_PROCESSING_MODE_PREF_KEYS:
        if pref_key in source:
            prefs[pref_key] = _normalize_crm_processing_enabled(source.get(pref_key), default=prefs.get(pref_key))
    if key == "all":
        prefs["address_validator_enabled"] = True
        prefs["stock_unlocker_enabled"] = False
        prefs["order_goods_enabled"] = False
        prefs["shipping_bypasser_enabled"] = False
        prefs["push_back_enabled"] = False
    elif key == "813":
        prefs["stock_unlocker_enabled"] = False
        prefs["product_separator_enabled"] = False
    elif key != "rush":
        prefs["stock_unlocker_enabled"] = False
        prefs["order_goods_enabled"] = False
        prefs["shipping_bypasser_enabled"] = False
        prefs["push_back_enabled"] = False
    return prefs


def _normalize_crm_processing_mode_preferences(raw_preferences, migrated_filter=None, migrated_values=None):
    raw_preferences = raw_preferences if isinstance(raw_preferences, dict) else {}
    mode_preferences = {}
    for processing_filter in CRM_PROCESSING_FILTERS:
        mode_preferences[processing_filter] = _sanitize_crm_processing_mode_preferences(
            processing_filter,
            raw_preferences.get(processing_filter),
        )
    if migrated_filter:
        normalized_filter = _normalize_crm_shipping_filter(migrated_filter)
        mode_preferences[normalized_filter] = _sanitize_crm_processing_mode_preferences(
            normalized_filter,
            migrated_values,
        )
    return mode_preferences


def _apply_crm_processing_mode_preferences_to_state(state, processing_filter=None):
    normalized_filter = _normalize_crm_shipping_filter(processing_filter or state.get("processing_filter") or "rush")
    preferences = state.get("mode_preferences") if isinstance(state.get("mode_preferences"), dict) else {}
    mode_preferences = _normalize_crm_processing_mode_preferences(preferences)
    state["mode_preferences"] = mode_preferences
    state["processing_filter"] = normalized_filter
    active_preferences = mode_preferences.get(normalized_filter) or _default_crm_processing_mode_preferences(normalized_filter)
    for pref_key in CRM_PROCESSING_MODE_PREF_KEYS:
        state[pref_key] = bool(active_preferences.get(pref_key))
    return state


def _crm_processing_step_label(step_key):
    if step_key == "mass_emailer":
        return "Sheets Scanner"
    if step_key == "address_validator_batch":
        return "Address Validator (Batch)"
    if step_key == "product_separator":
        return "Product Separator"
    if step_key == "order_goods":
        return "Order Goods"
    if step_key == "shipping_bypasser":
        return "Shipping Bypasser"
    if step_key == "push_back":
        return "Push Back"
    return "Stock Unlocker"


def _crm_processing_selected_steps_from_state(state):
    steps = []
    processing_filter = _normalize_crm_shipping_filter(state.get("processing_filter"))
    if processing_filter == "all" or _normalize_crm_processing_enabled(state.get("address_validator_enabled"), default=True):
        steps.append("address_validator_batch")
    if processing_filter != "813" and _normalize_crm_processing_enabled(state.get("product_separator_enabled"), default=True):
        steps.append("product_separator")
    if processing_filter == "rush" and _normalize_crm_processing_enabled(state.get("stock_unlocker_enabled"), default=True):
        steps.append("stock_unlocker")
    if processing_filter in {"rush", "813"} and _normalize_crm_processing_enabled(state.get("order_goods_enabled"), default=True):
        steps.append("order_goods")
    if processing_filter in {"rush", "813"} and _normalize_crm_processing_enabled(state.get("shipping_bypasser_enabled"), default=False):
        steps.append("shipping_bypasser")
    if processing_filter in {"rush", "813"} and _normalize_crm_processing_enabled(state.get("push_back_enabled"), default=False):
        steps.append("push_back")
    return steps


def _crm_processing_address_list_url_for_filter(processing_filter):
    normalized_filter = _normalize_crm_shipping_filter(processing_filter)
    if normalized_filter == "813":
        return str(CRM_813_VALIDATOR_URL or CRM_SHIPPING_813_URL or "").strip() or None
    if normalized_filter == "all":
        return str(CRM_SHIPPING_ALL_URL or "").strip() or None
    return None


def _crm_processing_813_list_url_for_step(step_key):
    if step_key == "address_validator_batch":
        return str(CRM_813_VALIDATOR_URL or CRM_SHIPPING_813_URL or "").strip() or None
    if step_key == "order_goods":
        return str(CRM_813_ORDER_GOODS_URL or "").strip() or None
    if step_key == "shipping_bypasser":
        return str(CRM_813_BYPASS_URL or "").strip() or None
    if step_key == "push_back":
        return str(CRM_PUSH_BACK_813_URL or "").strip() or None
    return None


def _crm_processing_push_back_list_url_for_filter(processing_filter):
    normalized_filter = _normalize_crm_shipping_filter(processing_filter)
    if normalized_filter == "813":
        return str(CRM_PUSH_BACK_813_URL or "").strip() or None
    if normalized_filter == "rush":
        return str(CRM_PUSH_BACK_RUSH_URL or "").strip() or None
    return None


def _crm_processing_813_url_config_key_for_step(step_key):
    if step_key == "address_validator_batch":
        return "CRM_813_VALIDATOR_URL"
    if step_key == "order_goods":
        return "CRM_813_ORDER_GOODS_URL"
    if step_key == "shipping_bypasser":
        return "CRM_813_BYPASS_URL"
    if step_key == "push_back":
        return "CRM_PUSH_BACK_813_URL"
    return "CRM_813_VALIDATOR_URL"


def _default_crm_processing_state():
    return {
        "stock_unlocker_enabled": True,
        "address_validator_enabled": True,
        "product_separator_enabled": True,
        "order_goods_enabled": True,
        "shipping_bypasser_enabled": False,
        "push_back_enabled": False,
        "processing_filter": "rush",
        "mode_preferences": {
            processing_filter: _default_crm_processing_mode_preferences(processing_filter)
            for processing_filter in CRM_PROCESSING_FILTERS
        },
        "last_run_timestamp": None,
        "last_run_success": None,
        "last_run_message": None,
        "last_run_duration_seconds": None,
        "last_filter_used": "rush",
        "last_selected_steps": ["address_validator_batch", "product_separator", "stock_unlocker", "order_goods"],
        "last_step_results": [],
        "total_runs": 0,
        "run_history": [],
    }


def ensure_crm_processing_state_file():
    if os.path.exists(CRM_PROCESSING_STATE_FILE):
        return
    with open(CRM_PROCESSING_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(_default_crm_processing_state(), f, indent=2)


def _normalize_crm_processing_step_results(items):
    rows = items if isinstance(items, list) else []
    cleaned = []
    for item in rows[:20]:
        if not isinstance(item, dict):
            continue
        step_key = str(item.get("key") or "").strip()
        if step_key not in {"mass_emailer", "address_validator_batch", "product_separator", "stock_unlocker", "order_goods", "shipping_bypasser", "push_back"}:
            continue
        cleaned.append(
            {
                "key": step_key,
                "label": _crm_processing_step_label(step_key),
                "success": bool(item.get("success")),
                "duration_seconds": _normalize_duration_seconds(item.get("duration_seconds")),
                "stage_timings": _normalize_stage_timings(item.get("stage_timings")),
                "message": str(item.get("message") or ""),
            }
        )
    return cleaned


def load_crm_processing_state():
    state = _default_crm_processing_state()
    loaded_has_mode_preferences = False
    if os.path.exists(CRM_PROCESSING_STATE_FILE):
        try:
            with open(CRM_PROCESSING_STATE_FILE, "r", encoding="utf-8-sig") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                loaded_has_mode_preferences = isinstance(loaded.get("mode_preferences"), dict)
                state.update(loaded)
        except Exception as e:
            logger.warning("Could not read %s: %s", CRM_PROCESSING_STATE_FILE, e)

    state.pop("mass_emailer_enabled", None)
    state["stock_unlocker_enabled"] = _normalize_crm_processing_enabled(state.get("stock_unlocker_enabled"), default=True)
    state["address_validator_enabled"] = _normalize_crm_processing_enabled(state.get("address_validator_enabled"), default=True)
    state["product_separator_enabled"] = _normalize_crm_processing_enabled(state.get("product_separator_enabled"), default=True)
    state["order_goods_enabled"] = _normalize_crm_processing_enabled(state.get("order_goods_enabled"), default=True)
    state["shipping_bypasser_enabled"] = _normalize_crm_processing_enabled(state.get("shipping_bypasser_enabled"), default=False)
    state["push_back_enabled"] = _normalize_crm_processing_enabled(state.get("push_back_enabled"), default=False)
    state["processing_filter"] = _normalize_crm_shipping_filter(state.get("processing_filter") or "rush")
    state["last_filter_used"] = _normalize_crm_shipping_filter(state.get("last_filter_used") or state.get("processing_filter") or "rush")
    migrated_values = {pref_key: state.get(pref_key) for pref_key in CRM_PROCESSING_MODE_PREF_KEYS}
    state["mode_preferences"] = _normalize_crm_processing_mode_preferences(
        state.get("mode_preferences"),
        migrated_filter=state["processing_filter"] if not loaded_has_mode_preferences else None,
        migrated_values=migrated_values if not loaded_has_mode_preferences else None,
    )
    _apply_crm_processing_mode_preferences_to_state(state)
    state["last_selected_steps"] = _normalize_crm_processing_step_results(
        [{"key": step} for step in (state.get("last_selected_steps") if isinstance(state.get("last_selected_steps"), list) else [])]
    )
    state["last_selected_steps"] = [item["key"] for item in state["last_selected_steps"]]
    state["last_step_results"] = _normalize_crm_processing_step_results(state.get("last_step_results"))
    state["last_run_duration_seconds"] = _normalize_duration_seconds(state.get("last_run_duration_seconds"))
    state["total_runs"] = max(0, int(_safe_float(state.get("total_runs"), 0)))
    history = state.get("run_history") if isinstance(state.get("run_history"), list) else []
    cleaned_history = []
    for entry in history[:20]:
        if not isinstance(entry, dict):
            continue
        row = dict(entry)
        row["processing_filter"] = _normalize_crm_shipping_filter(row.get("processing_filter") or state.get("processing_filter") or "rush")
        row["selected_steps"] = _normalize_crm_processing_step_results(
            [{"key": step} for step in (row.get("selected_steps") if isinstance(row.get("selected_steps"), list) else [])]
        )
        row["selected_steps"] = [item["key"] for item in row["selected_steps"]]
        row["step_results"] = _normalize_crm_processing_step_results(row.get("step_results"))
        row["duration_seconds"] = _normalize_duration_seconds(row.get("duration_seconds"))
        row["success"] = bool(row.get("success") if row.get("success") is not None else False)
        row["message"] = str(row.get("message") or "")
        cleaned_history.append(row)
    state["run_history"] = cleaned_history
    return state


def save_crm_processing_state(state):
    with open(CRM_PROCESSING_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def _crm_processing_runtime_snapshot():
    with crm_processing_runtime_lock:
        return dict(crm_processing_runtime)


def _crm_processing_order_progress(current, total, source=""):
    current_int = max(0, int(_safe_float(current, 0)))
    total_int = max(0, int(_safe_float(total, 0)))
    if total_int <= 0:
        return None
    current_int = min(current_int, total_int)
    payload = {
        "current": current_int,
        "total": total_int,
        "label": f"{current_int}/{total_int}",
    }
    if source:
        payload["source"] = str(source)
    return payload


def _crm_processing_order_progress_from_runtime(runtime, source=""):
    if not isinstance(runtime, dict) or not runtime.get("running"):
        return None

    progress = _crm_processing_order_progress(
        runtime.get("currentOrderIndex"),
        runtime.get("totalOrderCount") or runtime.get("orderCount"),
        source=source,
    )
    if progress:
        return progress

    match = re.search(r"\((\d+)\s*/\s*(\d+)\)", str(runtime.get("lastMessage") or ""))
    if match:
        return _crm_processing_order_progress(match.group(1), match.group(2), source=source)
    return None


def _crm_processing_current_order_progress(current_step):
    step = str(current_step or "").strip()
    if step == "product_separator":
        return _crm_processing_order_progress_from_runtime(_crm_product_separator_runtime_snapshot(), source=step)
    if step == "order_goods":
        return _crm_processing_order_progress_from_runtime(_crm_order_goods_runtime_snapshot(), source=step)
    if step == "shipping_bypasser":
        return _crm_processing_order_progress_from_runtime(_crm_shipping_bypasser_runtime_snapshot(), source=step)
    if step == "push_back":
        return _crm_processing_order_progress_from_runtime(_crm_push_back_runtime_snapshot(), source=step)
    if step == "address_validator_batch":
        return _crm_processing_order_progress_from_runtime(_crm_address_runtime_snapshot(), source=step)
    return None


def _read_automation_status_file():
    if not os.path.exists(AUTOMATION_STATUS_FILE):
        return {}
    try:
        with open(AUTOMATION_STATUS_FILE, "r", encoding="utf-8-sig") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _automation_status_name_matches(payload, expected_names):
    expected = {
        str(name or "").strip().lower()
        for name in (expected_names if isinstance(expected_names, (list, tuple, set)) else [expected_names])
        if str(name or "").strip()
    }
    actual = str((payload or {}).get("automation_name") or "").strip().lower()
    return bool(actual and actual in expected)


def _merge_live_automation_status(runtime, expected_names):
    runtime = dict(runtime or {})
    if not runtime.get("running"):
        return runtime
    status = _read_automation_status_file()
    if not _automation_status_name_matches(status, expected_names):
        return runtime

    message = str(status.get("message") or "").strip()
    if message:
        runtime["lastMessage"] = message
    stage = str(status.get("stage") or "").strip()
    if stage:
        runtime["currentStage"] = stage
    target_order_id = _normalize_crm_single_order_id(status.get("order_id"))
    if target_order_id:
        runtime["targetOrderId"] = target_order_id

    progress = status.get("progress") if isinstance(status.get("progress"), dict) else {}
    normalized_progress = _crm_processing_order_progress(progress.get("current"), progress.get("total"))
    if normalized_progress:
        runtime["currentOrderIndex"] = normalized_progress["current"]
        runtime["totalOrderCount"] = normalized_progress["total"]
        runtime["currentOrderProgress"] = normalized_progress
    return runtime


def _build_crm_processing_summary(step_results):
    results = _normalize_crm_processing_step_results(step_results)
    if not results:
        return False, "Automate Processing did not run any automation."
    failed = [item for item in results if not item.get("success")]
    if not failed:
        labels = ", ".join(item["label"] for item in results)
        return True, f"Automate Processing completed successfully: {labels}."
    if len(failed) == len(results):
        labels = ", ".join(item["label"] for item in failed)
        return False, f"Automate Processing finished with failures: {labels}."
    failed_labels = ", ".join(item["label"] for item in failed)
    return False, f"Automate Processing completed with partial success. Needs attention: {failed_labels}."


def _persist_crm_processing_run_result(success, message, selected_steps, step_results, processing_filter="rush"):
    ensure_crm_processing_state_file()
    timestamp = datetime.now().isoformat()
    normalized_steps = [
        step
        for step in selected_steps
        if step in {"stock_unlocker", "address_validator_batch", "product_separator", "order_goods", "shipping_bypasser", "push_back"}
    ]
    normalized_results = _normalize_crm_processing_step_results(step_results)
    normalized_filter = _normalize_crm_shipping_filter(processing_filter)
    duration_seconds = _runtime_duration_seconds(_crm_processing_runtime_snapshot())

    with crm_processing_state_lock:
        state = load_crm_processing_state()
        state["last_run_timestamp"] = timestamp
        state["last_run_success"] = bool(success)
        state["last_run_message"] = str(message)
        state["last_run_duration_seconds"] = duration_seconds
        state["last_filter_used"] = normalized_filter
        state["last_selected_steps"] = normalized_steps
        state["last_step_results"] = normalized_results
        state["total_runs"] = max(0, int(_safe_float(state.get("total_runs"), 0))) + 1
        entry = {
            "timestamp": timestamp,
            "success": bool(success),
            "processing_filter": normalized_filter,
            "selected_steps": normalized_steps,
            "step_results": normalized_results,
            "duration_seconds": duration_seconds,
            "message": str(message),
        }
        history = state.get("run_history") if isinstance(state.get("run_history"), list) else []
        state["run_history"] = [entry] + history[:19]
        save_crm_processing_state(state)

    return state


def _start_crm_runtime(dry_run=False, last_message="Stock Unlocker queued."):
    with crm_runtime_lock:
        crm_runtime["running"] = True
        crm_runtime["startedAt"] = datetime.now().isoformat()
        crm_runtime["completedAt"] = None
        crm_runtime["lastAction"] = "unlock_all"
        crm_runtime["dryRun"] = bool(dry_run)
        crm_runtime["lastMessage"] = str(last_message)
        crm_runtime["lastSuccess"] = None
        crm_runtime["attempt"] = 0
        crm_runtime["attemptsPlanned"] = max(1, CRM_MAX_RETRIES + 1)


def _finish_crm_runtime(ok, message, state, release_lock=True):
    with crm_runtime_lock:
        crm_runtime["running"] = False
        crm_runtime["completedAt"] = datetime.now().isoformat()
        crm_runtime["lastMessage"] = str(message)
        crm_runtime["lastSuccess"] = bool(ok)
        crm_runtime["stateSnapshot"] = state
    if release_lock and crm_lock.locked():
        crm_lock.release()


def _start_crm_address_runtime(
    *,
    dry_run=False,
    action="validate_order",
    target_order_id=None,
    active_filter="free",
    list_url=None,
    batch_size=None,
    parallel_workers=1,
    last_message="Address Validator queued.",
):
    with crm_address_runtime_lock:
        crm_address_runtime["running"] = True
        crm_address_runtime["startedAt"] = datetime.now().isoformat()
        crm_address_runtime["completedAt"] = None
        crm_address_runtime["lastAction"] = _normalize_crm_address_action(action)
        crm_address_runtime["targetOrderId"] = _normalize_crm_single_order_id(target_order_id)
        crm_address_runtime["activeFilter"] = _normalize_crm_shipping_filter(active_filter)
        crm_address_runtime["listUrl"] = _normalize_crm_list_url(list_url)
        crm_address_runtime["batchSize"] = _normalize_crm_batch_size(batch_size, default=0, minimum=1, maximum=25, allow_unlimited=True)
        crm_address_runtime["parallelWorkers"] = _normalize_crm_positive_int(parallel_workers, default=1, minimum=1, maximum=CRM_SHARED_MAX_PARALLEL_WORKERS)
        if crm_address_runtime["batchSize"] is not None:
            crm_address_runtime["parallelWorkers"] = min(crm_address_runtime["parallelWorkers"], crm_address_runtime["batchSize"])
        crm_address_runtime["orderCount"] = 0
        crm_address_runtime["refreshPasses"] = 0
        crm_address_runtime["dryRun"] = bool(dry_run)
        crm_address_runtime["lastMessage"] = str(last_message)
        crm_address_runtime["lastSuccess"] = None
        crm_address_runtime["attempt"] = 0
        crm_address_runtime["attemptsPlanned"] = _crm_address_total_attempts(action)


def _finish_crm_address_runtime(ok, message, payload, state, release_lock=True):
    with crm_address_runtime_lock:
        crm_address_runtime["running"] = False
        crm_address_runtime["completedAt"] = datetime.now().isoformat()
        crm_address_runtime["lastMessage"] = str(message)
        crm_address_runtime["lastSuccess"] = bool(ok)
        crm_address_runtime["orderCount"] = _extract_crm_order_count(payload if isinstance(payload, dict) else {})
        crm_address_runtime["refreshPasses"] = max(0, int(_safe_float(payload.get("refresh_passes") if isinstance(payload, dict) else 0, 0)))
        crm_address_runtime["stateSnapshot"] = state
    if release_lock and crm_lock.locked():
        crm_lock.release()


def _crm_address_total_attempts(action):
    normalized_action = _normalize_crm_address_action(action)
    if normalized_action == "validate_batch":
        # Batch runs already handle browser/session retries internally and should
        # not replay the whole batch from the server layer.
        return 1
    return max(1, CRM_MAX_RETRIES + 1)


def _crm_address_worker_timeout(action="validate_order", batch_size=1, parallel_workers=1):
    base_timeout = max(180, CRM_ACTION_TIMEOUT * 12)
    normalized_action = _normalize_crm_address_action(action)
    if normalized_action != "validate_batch":
        return base_timeout

    normalized_batch_size = _normalize_crm_batch_size(batch_size, default=1, minimum=1, maximum=25, allow_unlimited=True)
    normalized_parallel_workers = _normalize_crm_positive_int(parallel_workers, default=1, minimum=1, maximum=CRM_SHARED_MAX_PARALLEL_WORKERS)
    if normalized_batch_size is None:
        return max(base_timeout, CRM_ADDRESS_CONTINUOUS_BATCH_TIMEOUT_SECONDS)
    normalized_parallel_workers = min(normalized_parallel_workers, normalized_batch_size)
    waves = max(1, math.ceil(normalized_batch_size / max(1, normalized_parallel_workers)))
    extra_wave_timeout = max(120, CRM_ACTION_TIMEOUT * 8)
    return base_timeout + max(0, waves - 1) * extra_wave_timeout


def _crm_shipping_filter_label(value):
    key = _normalize_crm_shipping_filter(value)
    if key == "813":
        return "813 Orders"
    if key == "all":
        return "All Invalid-Address Orders"
    return "Rush Orders" if key == "rush" else "Free Ship Orders"


def _is_crm_transient_failure(message, payload):
    if isinstance(payload, dict) and payload.get("stopped") is True:
        return False
    if "force-stopped" in str(message or "").lower() or "force stopped" in str(message or "").lower():
        return False
    if isinstance(payload, dict) and payload.get("retryable") is True:
        return True

    text = f"{message} {json.dumps(payload, default=str) if isinstance(payload, dict) else payload}".lower()
    signals = (
        "timeout",
        "timed out",
        "renderer",
        "session not created",
        "devtoolsactiveport",
        "chrome failed to start",
        "navigation",
        "dns",
        "connection",
        "invalid session id",
        "disconnected: not connected to devtools",
        "unable to discover open pages",
    )
    return any(signal in text for signal in signals)


def _execute_crm_worker(dry_run=False):
    args = ["--action", "unlock_all"]
    if dry_run:
        args.append("--dry-run")
        args.append("--visible")
    timeout = max(180, CRM_ACTION_TIMEOUT * 12)
    return _run_script(CRM_SCRIPT, args, "CRMUnlock", timeout=timeout, show_terminal=bool(dry_run))


def _persist_crm_run_result(ok, message, payload, dry_run=False):
    ensure_crm_state_file()
    timestamp = datetime.now().isoformat()
    order_count = _extract_crm_order_count(payload)
    order_ids = _extract_crm_order_ids(payload)
    duration_seconds = _normalize_duration_seconds(payload.get("duration_seconds") if isinstance(payload, dict) else None)
    if duration_seconds is None:
        duration_seconds = _runtime_duration_seconds(_crm_runtime_snapshot())

    with crm_state_lock:
        state = load_crm_state()
        state["last_run_timestamp"] = timestamp
        state["last_run_success"] = bool(ok)
        state["last_run_message"] = str(message)
        state["last_run_duration_seconds"] = duration_seconds
        state["last_order_count"] = order_count
        state["last_order_ids"] = order_ids
        state["total_runs"] = max(0, int(_safe_float(state.get("total_runs"), 0))) + 1
        if ok and not dry_run:
            state["total_orders_processed"] = (
                max(0, int(_safe_float(state.get("total_orders_processed"), 0))) + order_count
            )
        entry = {
            "timestamp": timestamp,
            "automation_key": "stock_unlocker",
            "automation_label": "Stock Unlocker",
            "success": bool(ok),
            "order_count": order_count,
            "order_ids": order_ids,
            "order_results": _normalize_crm_stock_order_results([], order_ids, ok, message),
            "duration_seconds": duration_seconds,
            "message": str(message),
            "dry_run": bool(dry_run),
        }
        history = state.get("run_history") if isinstance(state.get("run_history"), list) else []
        state["run_history"] = [entry] + history[:19]
        save_crm_state(state)

    _audit_result("crm.unlock_orders", ok, message)
    return state


def _persist_crm_order_goods_run_result(ok, message, payload, dry_run=False):
    ensure_crm_state_file()
    timestamp = datetime.now().isoformat()
    order_count = _extract_crm_order_count(payload)
    order_ids = _extract_crm_order_ids(payload)
    order_results = _build_crm_order_goods_order_results(payload)
    duration_seconds = _normalize_duration_seconds(payload.get("duration_seconds") if isinstance(payload, dict) else None)
    if duration_seconds is None:
        duration_seconds = _runtime_duration_seconds(_crm_order_goods_runtime_snapshot())
    stage_timings = _normalize_stage_timings(payload.get("stage_timings") if isinstance(payload, dict) else [])
    parallel_workers = _normalize_crm_positive_int(
        payload.get("parallel_workers") if isinstance(payload, dict) else 1,
        default=1,
        minimum=1,
        maximum=CRM_ORDER_GOODS_MAX_PARALLEL_WORKERS,
    )

    with crm_state_lock:
        state = load_crm_state()
        state["last_run_timestamp"] = timestamp
        state["last_run_success"] = bool(ok)
        state["last_run_message"] = str(message)
        state["last_run_duration_seconds"] = duration_seconds
        state["last_stage_timings"] = stage_timings
        state["last_order_count"] = order_count
        state["last_order_ids"] = order_ids
        state["last_order_goods_parallel_workers"] = parallel_workers
        state["total_runs"] = max(0, int(_safe_float(state.get("total_runs"), 0))) + 1
        if ok and not dry_run:
            state["total_orders_processed"] = (
                max(0, int(_safe_float(state.get("total_orders_processed"), 0))) + order_count
            )
        entry = {
            "timestamp": timestamp,
            "automation_key": "order_goods",
            "automation_label": "Rush Order Goods",
            "success": bool(ok),
            "order_count": order_count,
            "order_ids": order_ids,
            "parallel_workers": parallel_workers,
            "order_results": order_results,
            "duration_seconds": duration_seconds,
            "stage_timings": stage_timings,
            "message": str(message),
            "dry_run": bool(dry_run),
        }
        history = state.get("run_history") if isinstance(state.get("run_history"), list) else []
        state["run_history"] = [entry] + history[:19]
        save_crm_state(state)

    return state


def _run_crm_unlock_with_retry(dry_run=False):
    total_attempts = max(1, CRM_MAX_RETRIES + 1)
    delay_seconds = max(0, CRM_RETRY_DELAY_SECONDS)
    last_result = (False, "CRM unlock did not run.", {"success": False, "message": "CRM unlock did not run."})

    for attempt in range(1, total_attempts + 1):
        if _automation_stop_is_blocking():
            msg = _force_stop_message("CRMUnlock")
            return False, msg, {"success": False, "message": msg, "stopped": True}
        with crm_runtime_lock:
            crm_runtime["attempt"] = attempt
            crm_runtime["attemptsPlanned"] = total_attempts
            crm_runtime["lastMessage"] = f"Attempt {attempt} of {total_attempts} started."

        log_automation_event(
            "crm.unlock_orders",
            "STARTED",
            f"Attempt {attempt}/{total_attempts} started. dry_run={bool(dry_run)}",
            source="server.py",
        )
        ok, message, payload = _execute_crm_worker(dry_run=dry_run)
        last_result = (ok, message, payload)

        with crm_runtime_lock:
            crm_runtime["lastMessage"] = str(message)
            crm_runtime["lastSuccess"] = bool(ok)

        if ok:
            return last_result

        if attempt < total_attempts and _is_crm_transient_failure(message, payload):
            retry_message = f"Attempt {attempt} failed with a transient error. Retrying in {delay_seconds}s. {message}"
            log_automation_event("crm.unlock_orders", "RETRY", retry_message, source="server.py")
            with crm_runtime_lock:
                crm_runtime["lastMessage"] = retry_message
            time.sleep(delay_seconds)
            continue

        return last_result

    return last_result


def _crm_run_thread(dry_run=False):
    ok = False
    message = "CRM run did not start."
    payload = {"success": False, "message": message}
    try:
        ok, message, payload = _run_crm_unlock_with_retry(dry_run=dry_run)
    except Exception as e:
        logger.exception("CRM background run failed unexpectedly")
        ok = False
        message = str(e)
        payload = {"success": False, "message": message, "error_type": type(e).__name__}
    finally:
        state = _persist_crm_run_result(ok, message, payload, dry_run=dry_run)
        _finish_crm_runtime(ok, message, state, release_lock=True)


def start_crm_run(dry_run=False):
    ensure_crm_state_file()
    if not crm_lock.acquire(blocking=False):
        return False, "A CRM automation run is already in progress."

    _start_crm_runtime(dry_run=dry_run, last_message="Stock Unlocker queued.")
    threading.Thread(target=_crm_run_thread, args=(dry_run,), daemon=True).start()
    return True, ("Stock Unlocker dry run started." if dry_run else "Stock Unlocker started.")


def get_crm_status_payload():
    ensure_crm_state_file()
    with crm_state_lock:
        state = load_crm_state()
    runtime = _crm_runtime_snapshot()
    return {
        "success": True,
        "running": bool(runtime.get("running")),
        "runtime": runtime,
        "state": state,
    }


def get_crm_state_payload():
    ensure_crm_state_file()
    with crm_state_lock:
        state = load_crm_state()
    return {"success": True, "state": state}


def clear_crm_history():
    ensure_crm_state_file()
    with crm_state_lock:
        state = load_crm_state()
        state["run_history"] = []
        save_crm_state(state)
    with crm_runtime_lock:
        crm_runtime["stateSnapshot"] = state
    return True, "Stock tools history cleared."


def clear_crm_auto_splitter_history():
    ensure_crm_state_file()
    with crm_state_lock:
        state = load_crm_state()
        state["auto_splitter_run_history"] = []
        save_crm_state(state)
    return True, "Auto Splitter history cleared."


def _default_crm_address_state():
    return {
        "active_filter": "free",
        "last_filter_used": "free",
        "last_action": "validate_order",
        "last_run_timestamp": None,
        "last_run_success": None,
        "last_run_message": None,
        "last_run_duration_seconds": None,
        "last_order_id": None,
        "last_order_count": 0,
        "last_order_ids": [],
        "last_batch_size": None,
        "last_parallel_workers": 1,
        "last_stage_timings": [],
        "saved_batch_size": None,
        "saved_parallel_workers": 1,
        "last_refresh_passes": 0,
        "last_list_url": None,
        "last_resolution": None,
        "last_manual_review_required": None,
        "last_report": [],
        "total_runs": 0,
        "run_history": [],
    }


def ensure_crm_address_state_file():
    if os.path.exists(CRM_ADDRESS_STATE_FILE):
        return
    with open(CRM_ADDRESS_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(_default_crm_address_state(), f, indent=2)


def _extract_crm_address_report(payload):
    if not isinstance(payload, dict):
        return []
    raw = payload.get("report")
    if not isinstance(raw, list):
        return []
    fallback_order_id = _normalize_crm_single_order_id(payload.get("target_order_id") or payload.get("order_id") or payload.get("last_order_id"))
    cleaned = []
    for item in raw[:CRM_ADDRESS_REPORT_MAX_ITEMS]:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        row["order_id"] = _normalize_crm_single_order_id(row.get("order_id")) or fallback_order_id
        row["warnings"] = row.get("warnings") if isinstance(row.get("warnings"), list) else []
        row["duration_seconds"] = _normalize_duration_seconds(row.get("duration_seconds") or row.get("session_duration_seconds"))
        row["session_duration_seconds"] = row["duration_seconds"]
        cleaned.append(row)
    return cleaned


def _extract_crm_address_order_id(payload):
    if not isinstance(payload, dict):
        return None
    direct = _normalize_crm_single_order_id(payload.get("target_order_id"))
    if direct:
        return direct
    order_ids = payload.get("order_ids") if isinstance(payload.get("order_ids"), list) else []
    if order_ids:
        normalized = _normalize_crm_single_order_id(order_ids[0])
        if normalized:
            return normalized
    report = _extract_crm_address_report(payload)
    if report:
        return _normalize_crm_single_order_id(report[0].get("order_id"))
    return None


def _load_last_result_payload():
    if not os.path.exists(RESULT_FILE):
        return None
    try:
        with open(RESULT_FILE, "r", encoding="utf-8-sig") as f:
            loaded = json.load(f)
        return loaded if isinstance(loaded, dict) else None
    except Exception:
        return None


def _report_addresses_match(left, right):
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    return (
        str(left.get("original_address_text") or "").strip() == str(right.get("original_address_text") or "").strip()
        and str(left.get("final_address_text") or "").strip() == str(right.get("final_address_text") or "").strip()
        and str(left.get("message") or "").strip() == str(right.get("message") or "").strip()
    )


def _backfill_crm_address_state_from_last_result(state):
    if not isinstance(state, dict):
        return state
    last_result = _load_last_result_payload()
    if not isinstance(last_result, dict):
        return state
    if str(last_result.get("action") or "") != "validate_order":
        return state
    backfill_order_id = _extract_crm_address_order_id(last_result)
    report = _extract_crm_address_report(last_result)
    if not backfill_order_id or not report:
        return state

    changed = False
    state_last_report = state.get("last_report") if isinstance(state.get("last_report"), list) else []
    if state_last_report and _report_addresses_match(state_last_report[0], report[0]):
        if not state.get("last_order_id"):
            state["last_order_id"] = backfill_order_id
            changed = True
        for row in state_last_report:
            if not row.get("order_id"):
                row["order_id"] = backfill_order_id
                changed = True

    history = state.get("run_history") if isinstance(state.get("run_history"), list) else []
    for entry in history:
        rows = entry.get("report") if isinstance(entry.get("report"), list) else []
        if rows and _report_addresses_match(rows[0], report[0]):
            if not entry.get("order_id"):
                entry["order_id"] = backfill_order_id
                changed = True
            for row in rows:
                if not row.get("order_id"):
                    row["order_id"] = backfill_order_id
                    changed = True
            break

    if changed:
        save_crm_address_state(state)
    return state


def load_crm_address_state():
    state = _default_crm_address_state()
    if os.path.exists(CRM_ADDRESS_STATE_FILE):
        try:
            with open(CRM_ADDRESS_STATE_FILE, "r", encoding="utf-8-sig") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                state.update(loaded)
        except Exception as e:
            logger.warning("Could not read %s: %s", CRM_ADDRESS_STATE_FILE, e)

    state["active_filter"] = _normalize_crm_shipping_filter(state.get("active_filter"))
    state["last_filter_used"] = _normalize_crm_shipping_filter(state.get("last_filter_used") or state.get("active_filter"))
    state["last_action"] = _normalize_crm_address_action(state.get("last_action"))
    state["last_order_id"] = _normalize_crm_single_order_id(state.get("last_order_id"))
    state["last_order_count"] = _extract_crm_order_count({"order_count": state.get("last_order_count")})
    state["last_order_ids"] = _extract_crm_order_ids({"order_ids": state.get("last_order_ids")})
    state["last_run_duration_seconds"] = _normalize_duration_seconds(state.get("last_run_duration_seconds"))
    state["last_batch_size"] = _normalize_crm_batch_size(state.get("last_batch_size"), default=0, minimum=1, maximum=25, allow_unlimited=True)
    state["last_parallel_workers"] = _normalize_crm_positive_int(state.get("last_parallel_workers"), default=1, minimum=1, maximum=CRM_SHARED_MAX_PARALLEL_WORKERS)
    state["saved_batch_size"] = _normalize_crm_batch_size(state.get("saved_batch_size"), default=0, minimum=1, maximum=25, allow_unlimited=True)
    state["saved_parallel_workers"] = _normalize_crm_positive_int(state.get("saved_parallel_workers"), default=1, minimum=1, maximum=CRM_SHARED_MAX_PARALLEL_WORKERS)
    if state["last_batch_size"] is not None:
        state["last_parallel_workers"] = min(state["last_parallel_workers"], state["last_batch_size"])
    state["last_refresh_passes"] = max(0, int(_safe_float(state.get("last_refresh_passes"), 0)))
    state["last_stage_timings"] = _normalize_stage_timings(state.get("last_stage_timings"))
    state["last_list_url"] = _normalize_crm_list_url(state.get("last_list_url"))
    state["last_resolution"] = str(state.get("last_resolution") or "")
    state["last_manual_review_required"] = bool(state.get("last_manual_review_required"))
    state["last_report"] = _extract_crm_address_report({"report": state.get("last_report"), "last_order_id": state.get("last_order_id")})
    state["total_runs"] = max(0, int(_safe_float(state.get("total_runs"), 0)))
    history = state.get("run_history") if isinstance(state.get("run_history"), list) else []
    cleaned_history = []
    for entry in history[:20]:
        if not isinstance(entry, dict):
            continue
        row = dict(entry)
        row["action"] = _normalize_crm_address_action(row.get("action"))
        row["order_id"] = _normalize_crm_single_order_id(row.get("order_id"))
        row["order_count"] = _extract_crm_order_count({"order_count": row.get("order_count")})
        row["order_ids"] = _extract_crm_order_ids({"order_ids": row.get("order_ids")})
        row["duration_seconds"] = _normalize_duration_seconds(row.get("duration_seconds"))
        row["stage_timings"] = _normalize_stage_timings(row.get("stage_timings"))
        row["batch_size"] = _normalize_crm_batch_size(row.get("batch_size"), default=1, minimum=1, maximum=25, allow_unlimited=True)
        row["parallel_workers"] = _normalize_crm_positive_int(row.get("parallel_workers"), default=1, minimum=1, maximum=CRM_SHARED_MAX_PARALLEL_WORKERS)
        row["refresh_passes"] = max(0, int(_safe_float(row.get("refresh_passes"), 0)))
        row["list_url"] = _normalize_crm_list_url(row.get("list_url"))
        row["filter"] = _normalize_crm_shipping_filter(row.get("filter") or state.get("active_filter"))
        row["resolution"] = str(row.get("resolution") or "")
        row["manual_review_required"] = bool(row.get("manual_review_required"))
        row["report"] = _extract_crm_address_report({"report": row.get("report"), "order_id": row.get("order_id")})
        cleaned_history.append(row)
    state["run_history"] = cleaned_history
    return state

def save_crm_address_state(state):
    with open(CRM_ADDRESS_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def _sync_crm_stock_parallel_worker_preferences(parallel_workers):
    normalized_workers = _normalize_crm_positive_int(
        parallel_workers,
        default=1,
        minimum=1,
        maximum=CRM_SHARED_MAX_PARALLEL_WORKERS,
    )
    ensure_crm_state_file()
    with crm_state_lock:
        state = load_crm_state()
        state["saved_product_separator_parallel_workers"] = normalized_workers
        state["saved_order_goods_parallel_workers"] = normalized_workers
        state["saved_auto_splitter_parallel_workers"] = normalized_workers
        save_crm_state(state)
    return state


def _saved_crm_automation_parallel_workers(default=1):
    ensure_crm_address_state_file()
    with crm_address_state_lock:
        state = load_crm_address_state()
    return _normalize_crm_positive_int(
        state.get("saved_parallel_workers"),
        default=default,
        minimum=1,
        maximum=CRM_SHARED_MAX_PARALLEL_WORKERS,
    )


def _crm_address_runtime_snapshot():
    with crm_address_runtime_lock:
        return dict(crm_address_runtime)


def _execute_crm_address_worker(order_id=None, dry_run=False, shipping_filter=None, action="validate_order", batch_size=1, parallel_workers=1, list_url=None):
    normalized_action = _normalize_crm_address_action(action)
    normalized_batch_size = _normalize_crm_batch_size(batch_size, default=1, minimum=1, maximum=25, allow_unlimited=True)
    normalized_parallel_workers = _normalize_crm_positive_int(
        parallel_workers,
        default=1,
        minimum=1,
        maximum=CRM_SHARED_MAX_PARALLEL_WORKERS,
    )
    if normalized_batch_size is not None:
        normalized_parallel_workers = min(normalized_parallel_workers, normalized_batch_size)
    normalized_list_url = _normalize_crm_list_url(list_url)
    normalized_filter = _normalize_crm_shipping_filter(shipping_filter)
    args = ["--action", normalized_action, "--shipping-filter", _normalize_crm_shipping_filter(shipping_filter)]
    if order_id:
        args.extend(["--order-id", order_id])
    if normalized_action == "validate_batch":
        if normalized_batch_size is not None:
            args.extend(["--batch-size", str(normalized_batch_size)])
        args.extend(["--parallel-workers", str(normalized_parallel_workers)])
    if normalized_list_url:
        args.extend(["--list-url", normalized_list_url])
    if dry_run:
        args.append("--dry-run")
        args.append("--visible")
    timeout = _crm_address_worker_timeout(normalized_action, normalized_batch_size, normalized_parallel_workers)
    ok, message, payload = _run_script(
        CRM_ADDRESS_VALIDATOR_SCRIPT,
        args,
        "CRMAddressValidator",
        timeout=timeout,
        show_terminal=bool(dry_run),
    )
    if not isinstance(payload, dict):
        payload = {"success": bool(ok), "message": str(message)}
    payload.setdefault("action", normalized_action)
    payload.setdefault("dry_run", bool(dry_run))
    payload.setdefault("shipping_filter", normalized_filter)
    payload.setdefault("batch_size", normalized_batch_size)
    payload.setdefault("parallel_workers", normalized_parallel_workers)
    if normalized_list_url:
        payload.setdefault("list_url", normalized_list_url)
    return ok, message, payload

def _persist_crm_address_run_result(ok, message, payload, order_id, shipping_filter, dry_run=False, action="validate_order", batch_size=1, parallel_workers=1, list_url=None):
    ensure_crm_address_state_file()
    timestamp = datetime.now().isoformat()
    report = _extract_crm_address_report(payload)
    payload_action_source = payload.get("action") if isinstance(payload, dict) and payload.get("action") is not None else action
    payload_action = _normalize_crm_address_action(payload_action_source)
    final_order_id = _extract_crm_address_order_id(payload) or _normalize_crm_single_order_id(order_id)
    order_count = _extract_crm_order_count(payload if isinstance(payload, dict) else {"order_count": 0})
    order_ids = _extract_crm_order_ids(payload if isinstance(payload, dict) else {"order_ids": []})
    normalized_batch_size = _normalize_crm_batch_size(
        payload.get("batch_size") if isinstance(payload, dict) and "batch_size" in payload else batch_size,
        default=1,
        minimum=1,
        maximum=25,
        allow_unlimited=True,
    )
    normalized_parallel_workers = _normalize_crm_positive_int(payload.get("parallel_workers") if isinstance(payload, dict) and payload.get("parallel_workers") is not None else parallel_workers, default=1, minimum=1, maximum=CRM_SHARED_MAX_PARALLEL_WORKERS)
    if normalized_batch_size is not None:
        normalized_parallel_workers = min(normalized_parallel_workers, normalized_batch_size)
    refresh_passes = max(0, int(_safe_float(payload.get("refresh_passes") if isinstance(payload, dict) else 0, 0)))
    duration_seconds = _normalize_duration_seconds(payload.get("duration_seconds") if isinstance(payload, dict) else None)
    if duration_seconds is None:
        duration_seconds = _runtime_duration_seconds(_crm_address_runtime_snapshot())
    stage_timings = _normalize_stage_timings(payload.get("stage_timings") if isinstance(payload, dict) else [])
    normalized_list_url = _normalize_crm_list_url(payload.get("list_url") if isinstance(payload, dict) else list_url)
    resolution = ""
    manual_review_required = False
    normalized_filter = _normalize_crm_shipping_filter(shipping_filter)
    if report:
        if isinstance(payload, dict) and payload.get("resolution") is not None:
            resolution = str(payload.get("resolution") or "")
        elif payload_action == "validate_batch":
            resolution = "batch"
        else:
            resolution = str(report[0].get("resolution") or "")
        manual_review_required = any(bool(item.get("manual_review_required")) for item in report)
    else:
        resolution = str(payload.get("resolution") or "") if isinstance(payload, dict) else ""
        manual_review_required = bool(payload.get("manual_review_required")) if isinstance(payload, dict) else False

    with crm_address_state_lock:
        state = load_crm_address_state()
        state["last_run_timestamp"] = timestamp
        state["last_run_success"] = bool(ok)
        state["last_run_message"] = str(message)
        state["last_run_duration_seconds"] = duration_seconds
        state["last_filter_used"] = normalized_filter
        state["last_action"] = payload_action
        state["last_order_id"] = final_order_id
        state["last_order_count"] = order_count
        state["last_order_ids"] = order_ids
        state["last_batch_size"] = normalized_batch_size
        state["last_parallel_workers"] = normalized_parallel_workers
        state["last_refresh_passes"] = refresh_passes
        state["last_stage_timings"] = stage_timings
        state["last_list_url"] = normalized_list_url
        state["last_resolution"] = resolution
        state["last_manual_review_required"] = bool(manual_review_required)
        state["last_report"] = _extract_crm_address_report({"report": report, "target_order_id": final_order_id})
        state["total_runs"] = max(0, int(_safe_float(state.get("total_runs"), 0))) + 1
        entry = {
            "timestamp": timestamp,
            "success": bool(ok),
            "action": payload_action,
            "order_id": final_order_id,
            "order_count": order_count,
            "order_ids": order_ids,
            "batch_size": normalized_batch_size,
            "parallel_workers": normalized_parallel_workers,
            "refresh_passes": refresh_passes,
            "duration_seconds": duration_seconds,
            "stage_timings": stage_timings,
            "list_url": normalized_list_url,
            "filter": normalized_filter,
            "message": str(message),
            "dry_run": bool(dry_run),
            "resolution": resolution,
            "manual_review_required": bool(manual_review_required),
            "report": _extract_crm_address_report({"report": report, "target_order_id": final_order_id}),
        }
        history = state.get("run_history") if isinstance(state.get("run_history"), list) else []
        state["run_history"] = [entry] + history[:19]
        save_crm_address_state(state)

    _audit_result("crm.address_validator", ok, message)
    return state

def _run_crm_address_with_retry(order_id, shipping_filter, dry_run=False, action="validate_order", batch_size=1, parallel_workers=1, list_url=None):
    total_attempts = _crm_address_total_attempts(action)
    delay_seconds = max(0, CRM_RETRY_DELAY_SECONDS)
    last_result = (False, "Address Validator did not run.", {"success": False, "message": "Address Validator did not run."})

    for attempt in range(1, total_attempts + 1):
        if _automation_stop_is_blocking():
            msg = _force_stop_message("CRMAddressValidator")
            return False, msg, {"success": False, "message": msg, "stopped": True}
        with crm_address_runtime_lock:
            crm_address_runtime["attempt"] = attempt
            crm_address_runtime["attemptsPlanned"] = total_attempts
            crm_address_runtime["lastMessage"] = (
                f"Attempt {attempt} of {total_attempts} started for "
                f"{'batch' if _normalize_crm_address_action(action) == 'validate_batch' else 'single-order'} "
                f"Address Validator on {_crm_shipping_filter_label(shipping_filter)}."
            )

        log_automation_event(
            "crm.address_validator",
            "STARTED",
            f"Attempt {attempt}/{total_attempts} started for order {order_id}. "
            f"action={_normalize_crm_address_action(action)} filter={_normalize_crm_shipping_filter(shipping_filter)} "
            f"dry_run={bool(dry_run)} batch_size={_crm_batch_size_display(batch_size)} parallel_workers={parallel_workers} list_url={_normalize_crm_list_url(list_url)}",
            source="server.py",
        )
        ok, message, payload = _execute_crm_address_worker(order_id, dry_run=dry_run, shipping_filter=shipping_filter, action=action, batch_size=batch_size, parallel_workers=parallel_workers, list_url=list_url)
        last_result = (ok, message, payload)

        with crm_address_runtime_lock:
            crm_address_runtime["lastMessage"] = str(message)
            crm_address_runtime["lastSuccess"] = bool(ok)

        if ok:
            return last_result

        if attempt < total_attempts and _is_crm_transient_failure(message, payload):
            retry_message = (
                f"Attempt {attempt} failed with a transient error for order {order_id}. "
                f"action={_normalize_crm_address_action(action)} filter={_normalize_crm_shipping_filter(shipping_filter)} "
                f"Retrying in {delay_seconds}s. {message}"
            )
            log_automation_event("crm.address_validator", "RETRY", retry_message, source="server.py")
            with crm_address_runtime_lock:
                crm_address_runtime["lastMessage"] = retry_message
            time.sleep(delay_seconds)
            continue

        return last_result

    return last_result

def _crm_address_run_thread(order_id, shipping_filter, dry_run=False, action="validate_order", batch_size=1, parallel_workers=1, list_url=None):
    ok = False
    message = "Address Validator run did not start."
    payload = {"success": False, "message": message}
    try:
        ok, message, payload = _run_crm_address_with_retry(order_id, shipping_filter, dry_run=dry_run, action=action, batch_size=batch_size, parallel_workers=parallel_workers, list_url=list_url)
    except Exception as e:
        logger.exception("CRM Address Validator background run failed unexpectedly")
        ok = False
        message = str(e)
        payload = {"success": False, "message": message, "error_type": type(e).__name__}
    finally:
        state = _persist_crm_address_run_result(ok, message, payload, order_id, shipping_filter, dry_run=dry_run, action=action, batch_size=batch_size, parallel_workers=parallel_workers, list_url=list_url)
        _finish_crm_address_runtime(ok, message, payload, state, release_lock=True)

def _crm_address_value_supplied(value):
    if value is None:
        return False
    return str(value).strip() != ""


def update_crm_address_preferences(batch_size=None, parallel_workers=None):
    ensure_crm_address_state_file()
    batch_size_supplied = _crm_address_value_supplied(batch_size)
    parallel_workers_supplied = _crm_address_value_supplied(parallel_workers)
    normalized_batch_size = _normalize_crm_batch_size(batch_size, default=0, minimum=1, maximum=25, allow_unlimited=True)
    normalized_parallel_workers = _normalize_crm_positive_int(parallel_workers, default=1, minimum=1, maximum=CRM_SHARED_MAX_PARALLEL_WORKERS)

    with crm_address_state_lock:
        state = load_crm_address_state()
        if batch_size_supplied:
            state["saved_batch_size"] = normalized_batch_size
        if parallel_workers_supplied:
            state["saved_parallel_workers"] = normalized_parallel_workers
        save_crm_address_state(state)

    if parallel_workers_supplied:
        _sync_crm_stock_parallel_worker_preferences(state.get("saved_parallel_workers"))

    with crm_address_runtime_lock:
        crm_address_runtime["stateSnapshot"] = state

    batch_label = _crm_batch_size_display(state.get("saved_batch_size"))
    worker_label = int(_safe_float(state.get("saved_parallel_workers"), 1))
    return True, f"CRM settings saved. Batch Size {batch_label.title()} | Parallel Workers {worker_label}.", state


def update_crm_order_goods_preferences(parallel_workers=None):
    ensure_crm_state_file()
    if _crm_address_value_supplied(parallel_workers):
        ok, _message, address_state = update_crm_address_preferences(parallel_workers=parallel_workers)
        worker_label = int(_safe_float(address_state.get("saved_parallel_workers"), 1))
        with crm_state_lock:
            state = load_crm_state()
        return ok, f"CRM worker settings saved. Automation Workers {worker_label}.", state
    with crm_state_lock:
        state = load_crm_state()
    return True, "CRM worker settings unchanged.", state


def start_crm_address_run(order_id=None, dry_run=False, action="validate_order", batch_size=None, parallel_workers=None, list_url=None):
    ensure_crm_address_state_file()
    normalized_action = _normalize_crm_address_action(action)
    normalized_order_id = _normalize_crm_single_order_id(order_id) if order_id else None
    normalized_list_url = _normalize_crm_list_url(list_url)
    batch_size_supplied = _crm_address_value_supplied(batch_size)
    parallel_workers_supplied = _crm_address_value_supplied(parallel_workers)
    requested_batch_size = _normalize_crm_batch_size(batch_size, default=0, minimum=1, maximum=25, allow_unlimited=True)
    requested_parallel_workers = _normalize_crm_positive_int(parallel_workers, default=1, minimum=1, maximum=CRM_SHARED_MAX_PARALLEL_WORKERS)
    if order_id and not normalized_order_id:
        return False, "Order ID must be a 7-digit value."
    if normalized_action == "validate_batch" and normalized_order_id:
        return False, "Batch mode does not accept a single order ID. Clear the order field or switch back to Single mode."
    with crm_address_state_lock:
        state = load_crm_address_state()
    active_filter = _normalize_crm_shipping_filter(state.get("active_filter"))
    saved_batch_size = _normalize_crm_batch_size(state.get("saved_batch_size"), default=0, minimum=1, maximum=25, allow_unlimited=True)
    saved_parallel_workers = _normalize_crm_positive_int(state.get("saved_parallel_workers"), default=1, minimum=1, maximum=CRM_SHARED_MAX_PARALLEL_WORKERS)
    normalized_batch_size = requested_batch_size if batch_size_supplied else saved_batch_size
    normalized_parallel_workers = requested_parallel_workers if parallel_workers_supplied else saved_parallel_workers
    if normalized_batch_size is not None:
        normalized_parallel_workers = min(normalized_parallel_workers, normalized_batch_size)
    if not crm_lock.acquire(blocking=False):
        return False, "A CRM automation run is already in progress."

    _start_crm_address_runtime(
        dry_run=dry_run,
        action=normalized_action,
        target_order_id=normalized_order_id,
        active_filter=active_filter,
        list_url=normalized_list_url,
        batch_size=normalized_batch_size,
        parallel_workers=normalized_parallel_workers,
        last_message=(
            f"Address Validator queued for order {normalized_order_id}."
            if normalized_order_id
            else (
                f"Address Validator batch queued {_crm_batch_scope_phrase(normalized_batch_size)} from {_crm_shipping_filter_label(active_filter)}{' via custom list URL' if normalized_list_url else ''}."
                if normalized_action == "validate_batch"
                else f"Address Validator queued to grab the first order from the {_crm_shipping_filter_label(active_filter)}{' via custom list URL' if normalized_list_url else ''} list."
            )
        ),
    )
    threading.Thread(target=_crm_address_run_thread, args=(normalized_order_id, active_filter, dry_run, normalized_action, normalized_batch_size, normalized_parallel_workers, normalized_list_url), daemon=True).start()
    if normalized_action == "validate_batch":
        if dry_run:
            return True, f"Address Validator batch dry run started {_crm_batch_scope_phrase(normalized_batch_size)} from the {_crm_shipping_filter_label(active_filter)}{' custom list' if normalized_list_url else ' list'}."
        return True, f"Address Validator batch started {_crm_batch_scope_phrase(normalized_batch_size)} from the {_crm_shipping_filter_label(active_filter)}{' custom list' if normalized_list_url else ' list'}."
    if normalized_order_id:
        if dry_run:
            return True, f"Address Validator dry run started for order {normalized_order_id}."
        return True, f"Address Validator started for order {normalized_order_id}."
    if dry_run:
        return True, f"Address Validator dry run started. It will grab the first order from the {_crm_shipping_filter_label(active_filter)}{' custom list' if normalized_list_url else ' list'}."
    return True, f"Address Validator started. It will grab the first order from the {_crm_shipping_filter_label(active_filter)}{' custom list' if normalized_list_url else ' list'}."

def get_crm_address_status_payload():
    ensure_crm_address_state_file()
    with crm_address_state_lock:
        state = load_crm_address_state()
    runtime = _crm_address_runtime_snapshot()
    if not runtime.get("running"):
        runtime["activeFilter"] = _normalize_crm_shipping_filter(state.get("active_filter"))
    return {
        "success": True,
        "running": bool(runtime.get("running")),
        "runtime": runtime,
        "state": state,
    }


def get_crm_address_state_payload():
    ensure_crm_address_state_file()
    with crm_address_state_lock:
        state = load_crm_address_state()
    return {"success": True, "state": state}


def clear_crm_address_history():
    ensure_crm_address_state_file()
    with crm_address_state_lock:
        state = load_crm_address_state()
        state["run_history"] = []
        save_crm_address_state(state)
    with crm_address_runtime_lock:
        crm_address_runtime["stateSnapshot"] = state
    return True, "Address Validator history cleared."


def set_crm_address_filter(filter_key):
    ensure_crm_address_state_file()
    normalized_filter = _normalize_crm_shipping_filter(filter_key)
    with crm_address_state_lock:
        state = load_crm_address_state()
        state["active_filter"] = normalized_filter
        save_crm_address_state(state)
    with crm_address_runtime_lock:
        crm_address_runtime["activeFilter"] = normalized_filter
        crm_address_runtime["stateSnapshot"] = state
    return True, f"Address Validator shipping filter switched to {_crm_shipping_filter_label(normalized_filter)}."


def _crm_order_goods_runtime_snapshot():
    with crm_order_goods_runtime_lock:
        runtime = dict(crm_order_goods_runtime)
    return _merge_live_automation_status(runtime, "crm.order_goods")


def _crm_product_separator_runtime_snapshot():
    with crm_product_separator_runtime_lock:
        return dict(crm_product_separator_runtime)


def _product_separator_mode_label(value):
    key = _normalize_crm_shipping_filter(value)
    if key == "813":
        return "813"
    if key == "all":
        return "All"
    return "Rush" if key == "rush" else "Free Ship"


def _product_separator_payload_order_ids(payload):
    if not isinstance(payload, dict):
        return []
    keys = (
        "split_order_ids",
        "live_order_ids",
        "order_ids",
        "skipped_order_ids",
        "manual_review_order_ids",
        "failed_order_ids",
    )
    ordered = []
    for key in keys:
        raw_values = payload.get(key)
        if not isinstance(raw_values, list):
            continue
        for value in raw_values:
            order_id = _normalize_crm_single_order_id(value)
            if order_id and order_id not in ordered:
                ordered.append(order_id)
    single_id = _normalize_crm_single_order_id(payload.get("target_order_id") or payload.get("order_id"))
    if single_id and single_id not in ordered:
        ordered.insert(0, single_id)
    return ordered


def _build_crm_product_separator_order_results(payload):
    if not isinstance(payload, dict):
        return []
    rows = []
    source_rows = payload.get("order_results") if isinstance(payload.get("order_results"), list) else []
    if not source_rows and isinstance(payload.get("report"), list):
        source_rows = payload.get("report")
    if not source_rows:
        single_order_id = _normalize_crm_single_order_id(payload.get("target_order_id") or payload.get("order_id"))
        if single_order_id:
            source_rows = [payload]
    for item in source_rows[:100]:
        if not isinstance(item, dict):
            continue
        order_id = _normalize_crm_single_order_id(item.get("order_id") or item.get("target_order_id"))
        if not order_id:
            continue
        resolution = str(item.get("resolution") or item.get("outcome") or "").strip()
        needs_split = bool(item.get("needs_split"))
        manual_review = bool(item.get("manual_review_required"))
        if manual_review:
            status = "Manual review"
        elif resolution == "split_complete":
            status = "Separated"
        elif resolution == "dry_run_ready" or needs_split:
            status = "Ready"
        elif resolution == "skipped_no_split_needed":
            status = "Skipped"
        else:
            status = "Success" if item.get("success") else "Needs attention"
        row = {
            "order_id": order_id,
            "success": bool(item.get("success")),
            "status": status,
            "outcome": resolution,
            "message": str(item.get("message") or ""),
            "duration_seconds": _normalize_duration_seconds(item.get("duration_seconds") or item.get("session_duration_seconds")),
            "manual_review_required": manual_review,
        }
        stock_ordered_status = _product_separator_stock_ordered_status(item)
        if stock_ordered_status:
            row["stock_ordered_status"] = stock_ordered_status
        rows.append(
            row
        )
    if rows:
        return rows
    return _normalize_crm_stock_order_results(
        [],
        _product_separator_payload_order_ids(payload),
        bool(payload.get("success")),
        str(payload.get("message") or ""),
    )


def _product_separator_stock_ordered_status(item):
    if not isinstance(item, dict):
        return {}
    existing = item.get("stock_ordered_status") if isinstance(item.get("stock_ordered_status"), dict) else {}
    if existing.get("label"):
        return {
            "applied": bool(existing.get("applied")),
            "skipped": bool(existing.get("skipped")),
            "label": str(existing.get("label") or "").strip(),
            "reason": str(existing.get("reason") or "").strip(),
            "order_stock_status_before_split": str(existing.get("order_stock_status_before_split") or "").strip(),
        }
    report = item.get("report") if isinstance(item.get("report"), dict) else item
    if not isinstance(report, dict):
        return {}
    apply_state = report.get("stock_status_apply") if isinstance(report.get("stock_status_apply"), dict) else {}
    plan = report.get("plan") if isinstance(report.get("plan"), dict) else {}
    if not apply_state and not plan:
        return {}
    already_applied = bool(apply_state.get("already_applied"))
    applied = bool(apply_state.get("status_applied") or already_applied)
    skipped = bool(apply_state.get("skipped"))
    reason = str(apply_state.get("reason") or plan.get("stock_ordered_apply_skip_reason") or "").strip()
    if already_applied:
        label = "Stock Ordered already applied"
    elif applied:
        label = "Stock Ordered applied"
    elif skipped or reason:
        label = "Stock Ordered not applied"
    elif plan.get("apply_stock_ordered_after_split") is False:
        label = "Stock Ordered not applied"
    elif plan.get("apply_stock_ordered_after_split") is True:
        label = "Stock Ordered planned"
    else:
        label = ""
    if not label:
        return {}
    return {
        "applied": applied,
        "skipped": bool(skipped or (not applied and bool(reason))),
        "label": label,
        "reason": reason,
        "order_stock_status_before_split": str(
            apply_state.get("order_stock_status_before_split")
            or plan.get("order_stock_status_before_split")
            or ""
        ).strip(),
    }


def _product_separator_attention_rows(rows):
    attention = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        if not row.get("success") or row.get("status") in {"Needs attention", "Manual review", "Stopped"}:
            attention.append(row)
    return attention


def _product_separator_attention_row_is_manual_review(row):
    if not isinstance(row, dict):
        return False
    status = str(row.get("status") or "").strip().lower()
    outcome = str(row.get("outcome") or "").strip().lower()
    return bool(row.get("manual_review_required")) or status == "manual review" or "manual_review" in outcome


def _format_product_separator_order_ids(order_ids, limit=8):
    ordered = []
    for value in order_ids:
        order_id = _normalize_crm_single_order_id(value)
        if order_id and order_id not in ordered:
            ordered.append(order_id)
    if len(ordered) <= limit:
        return ", ".join(ordered)
    remaining = len(ordered) - limit
    return f"{', '.join(ordered[:limit])}, +{remaining} more"


def _format_product_separator_attention_summary(rows):
    attention = _product_separator_attention_rows(rows)
    if not attention:
        return ""
    manual_review_ids = [
        row.get("order_id")
        for row in attention
        if _product_separator_attention_row_is_manual_review(row)
    ]
    preflight_ids = [
        row.get("order_id")
        for row in attention
        if str(row.get("phase") or "").lower() == "preflight"
        and not _product_separator_attention_row_is_manual_review(row)
    ]
    live_ids = [
        row.get("order_id")
        for row in attention
        if str(row.get("phase") or "").lower() == "live"
        and not _product_separator_attention_row_is_manual_review(row)
    ]
    other_ids = [
        row.get("order_id")
        for row in attention
        if str(row.get("phase") or "").lower() not in {"preflight", "live"}
        and not _product_separator_attention_row_is_manual_review(row)
    ]
    parts = []
    if manual_review_ids:
        parts.append(f"manual review {_format_product_separator_order_ids(manual_review_ids)}")
    if preflight_ids:
        parts.append(f"preflight {_format_product_separator_order_ids(preflight_ids)}")
    if live_ids:
        parts.append(f"live {_format_product_separator_order_ids(live_ids)}")
    if other_ids:
        parts.append(_format_product_separator_order_ids(other_ids))
    return "; ".join(part for part in parts if part)


def _crm_product_separator_worker_timeout(order_count=1, workers=1, live_order_count=0):
    base_timeout = max(240, CRM_ACTION_TIMEOUT * 16)
    count = max(1, int(_safe_float(order_count, 1)))
    worker_count = _normalize_crm_positive_int(workers, default=1, minimum=1, maximum=CRM_SHARED_MAX_PARALLEL_WORKERS)
    waves = max(1, math.ceil(count / max(1, worker_count)))
    live_count = max(0, int(_safe_float(live_order_count, 0)))
    return base_timeout + (waves * max(120, CRM_ACTION_TIMEOUT * 8)) + (live_count * max(300, CRM_ACTION_TIMEOUT * 20))


def _execute_crm_product_separator_script(args, timeout=None, show_terminal=False):
    ok, message, payload = _run_script(
        CRM_PRODUCT_SEPARATOR_SCRIPT,
        args,
        "CRMProductSeparator",
        timeout=timeout or max(300, CRM_ACTION_TIMEOUT * 20),
        show_terminal=bool(show_terminal),
    )
    if not isinstance(payload, dict):
        payload = {"success": bool(ok), "message": str(message)}
    return ok, message, payload


def _crm_product_separator_payload_retryable(ok, message, payload):
    if ok:
        return False
    if not isinstance(payload, dict):
        payload = {}
    if payload.get("success"):
        return False
    text = " ".join(
        str(value or "")
        for value in (
            message,
            payload.get("message"),
            payload.get("error_type"),
            payload.get("resolution"),
        )
    ).lower()
    return any(
        phrase in text
        for phrase in (
            "crm app did not become ready",
            "not authenticated",
            "chrome not reachable",
            "disconnected",
            "timeout",
            "timed out",
        )
    )


def _execute_crm_product_separator_worker(dry_run=False, list_mode="rush", list_url=None, parallel_workers=1, order_id=None, visible=None, show_terminal=None):
    normalized_order_id = _normalize_crm_single_order_id(order_id)
    if _crm_address_value_supplied(order_id) and not normalized_order_id:
        return False, "Order ID must be a 7-digit value or CRM order URL.", {
            "success": False,
            "message": "Order ID must be a 7-digit value or CRM order URL.",
            "action": "product_separator_order",
            "dry_run": bool(dry_run),
            "manual_review_required": True,
            "resolution": "invalid_order_id",
        }
    normalized_mode = _normalize_crm_shipping_filter(list_mode)
    normalized_workers = _normalize_crm_positive_int(parallel_workers, default=1, minimum=1, maximum=CRM_SHARED_MAX_PARALLEL_WORKERS)
    normalized_list_url = _normalize_crm_list_url(list_url)
    visible = bool(dry_run) if visible is None else bool(visible)
    show_terminal = bool(dry_run) if show_terminal is None else bool(show_terminal)

    if normalized_order_id:
        args = ["--action", "product_separator_order", "--order-id", normalized_order_id]
        args.append("--dry-run" if dry_run else "--real")
        if visible:
            args.append("--visible")
        ok, message, payload = _execute_crm_product_separator_script(
            args,
            timeout=_crm_product_separator_worker_timeout(order_count=1, workers=1, live_order_count=0 if dry_run else 1),
            show_terminal=show_terminal,
        )
        if _crm_product_separator_payload_retryable(ok, message, payload):
            first_message = str(message or payload.get("message") or "")
            ok, message, payload = _execute_crm_product_separator_script(
                args,
                timeout=_crm_product_separator_worker_timeout(order_count=1, workers=1, live_order_count=0 if dry_run else 1),
                show_terminal=show_terminal,
            )
            payload["retried_after_transient_error"] = True
            payload["first_attempt_message"] = first_message
        payload.setdefault("action", "product_separator_order")
        payload.setdefault("dry_run", bool(dry_run))
        payload.setdefault("target_order_id", normalized_order_id)
        payload.setdefault("order_ids", [normalized_order_id])
        return ok, message, payload

    preflight_args = [
        "--action",
        "product_separator_list",
        "--list-mode",
        normalized_mode,
        "--workers",
        str(normalized_workers),
        "--dry-run",
    ]
    if normalized_list_url:
        preflight_args.extend(["--list-url", normalized_list_url])
    if visible:
        preflight_args.append("--visible")
    preflight_ok, preflight_message, preflight_payload = _execute_crm_product_separator_script(
        preflight_args,
        timeout=max(1800, CRM_ACTION_TIMEOUT * 120),
        show_terminal=show_terminal,
    )
    if dry_run:
        preflight_payload.setdefault("action", "product_separator_list")
        preflight_payload.setdefault("dry_run", True)
        preflight_payload.setdefault("list_mode", normalized_mode)
        preflight_payload.setdefault("parallel_workers", normalized_workers)
        if normalized_list_url:
            preflight_payload.setdefault("list_url", normalized_list_url)
        return preflight_ok, preflight_message, preflight_payload

    split_order_ids = _extract_crm_order_ids({"order_ids": preflight_payload.get("split_order_ids")})
    skipped_order_ids = _extract_crm_order_ids({"order_ids": preflight_payload.get("skipped_order_ids")})
    preflight_order_results = _build_crm_product_separator_order_results(preflight_payload)
    preflight_attention_results = [
        row
        for row in preflight_order_results
        if not row.get("success") or row.get("status") == "Manual review"
    ]
    for row in preflight_attention_results:
        row["phase"] = "preflight"
    preflight_skipped_results = [
        row
        for row in preflight_order_results
        if row.get("order_id") in skipped_order_ids
    ]
    for row in preflight_skipped_results:
        row["phase"] = "preflight"
    if not preflight_ok and not split_order_ids:
        preflight_payload.setdefault("action", "product_separator_list")
        preflight_payload.setdefault("dry_run", True)
        preflight_payload.setdefault("list_mode", normalized_mode)
        preflight_payload.setdefault("parallel_workers", normalized_workers)
        if normalized_list_url:
            preflight_payload.setdefault("list_url", normalized_list_url)
        return preflight_ok, preflight_message, preflight_payload
    if not split_order_ids:
        no_orders_detected = not _product_separator_payload_order_ids(preflight_payload)
        payload = dict(preflight_payload)
        payload.update(
            {
                "success": True,
                "message": (
                    "No orders detected"
                    if no_orders_detected
                    else f"Product Separator found no orders needing separation for {_product_separator_mode_label(normalized_mode)} mode."
                ),
                "action": "product_separator_batch",
                "dry_run": False,
                "preflight": preflight_payload,
                "live_order_ids": [],
                "order_results": _build_crm_product_separator_order_results(preflight_payload),
            }
        )
        return True, payload["message"], payload

    live_results = list(preflight_attention_results)
    live_ok = not preflight_attention_results
    live_success_count = 0
    attempted_live_order_ids = []
    with crm_product_separator_runtime_lock:
        crm_product_separator_runtime["orderCount"] = len(split_order_ids)
        crm_product_separator_runtime["splitOrderCount"] = len(split_order_ids)
        crm_product_separator_runtime["currentOrderIndex"] = 0
        crm_product_separator_runtime["totalOrderCount"] = len(split_order_ids)
    for index, split_order_id in enumerate(split_order_ids, start=1):
        if _automation_stop_is_blocking():
            msg = _force_stop_message("CRMProductSeparator")
            live_results.append(
                {
                    "order_id": split_order_id,
                    "success": False,
                    "status": "Stopped",
                    "outcome": "force_stopped",
                    "message": msg,
                    "phase": "live",
                }
            )
            live_ok = False
            break
        with crm_product_separator_runtime_lock:
            crm_product_separator_runtime["lastMessage"] = f"Separating order {split_order_id} ({index}/{len(split_order_ids)})."
            crm_product_separator_runtime["targetOrderId"] = split_order_id
            crm_product_separator_runtime["currentOrderIndex"] = index
            crm_product_separator_runtime["totalOrderCount"] = len(split_order_ids)
        live_args = ["--action", "product_separator_order", "--order-id", split_order_id, "--real"]
        attempted_live_order_ids.append(split_order_id)
        live_step_ok, live_message, live_payload = _execute_crm_product_separator_script(
            live_args,
            timeout=_crm_product_separator_worker_timeout(order_count=1, workers=1, live_order_count=1),
            show_terminal=False,
        )
        if _crm_product_separator_payload_retryable(live_step_ok, live_message, live_payload):
            first_message = str(live_message or live_payload.get("message") or "")
            live_step_ok, live_message, live_payload = _execute_crm_product_separator_script(
                live_args,
                timeout=_crm_product_separator_worker_timeout(order_count=1, workers=1, live_order_count=1),
                show_terminal=False,
            )
            live_payload["retried_after_transient_error"] = True
            live_payload["first_attempt_message"] = first_message
        live_ok = live_ok and bool(live_step_ok)
        order_result = _build_crm_product_separator_order_results(live_payload)
        if order_result:
            for row in order_result:
                row["phase"] = "live"
                if live_step_ok and row.get("order_id") == split_order_id and row.get("status") == "Success":
                    row["status"] = "Separated"
                    row["outcome"] = str(live_payload.get("resolution") or row.get("outcome") or "split_complete")
            live_results.extend(order_result)
        else:
            live_results.append(
                {
                    "order_id": split_order_id,
                    "success": bool(live_step_ok),
                    "status": "Separated" if live_step_ok else "Needs attention",
                    "outcome": str(live_payload.get("resolution") or ""),
                    "message": str(live_message),
                    "duration_seconds": _normalize_duration_seconds(live_payload.get("duration_seconds")),
                    "phase": "live",
                }
            )
        if live_step_ok:
            live_success_count += 1
    success = bool(preflight_ok and live_ok)
    attention_results = _product_separator_attention_rows(live_results)
    attention_count = len(attention_results)
    manual_review_count = len(
        [row for row in attention_results if _product_separator_attention_row_is_manual_review(row)]
    )
    non_manual_attention_count = max(0, attention_count - manual_review_count)
    attention_summary = _format_product_separator_attention_summary(attention_results)
    attention_order_ids = _extract_crm_order_ids({"order_ids": [row.get("order_id") for row in attention_results]})
    live_result_order_ids = _extract_crm_order_ids({"order_ids": [row.get("order_id") for row in live_results]})
    all_result_order_ids = _extract_crm_order_ids(
        {"order_ids": attempted_live_order_ids + attention_order_ids + live_result_order_ids + skipped_order_ids}
    )
    all_order_results = live_results + [
        row
        for row in preflight_skipped_results
        if row.get("order_id") not in live_result_order_ids
    ]
    result_order_index = {order_id: index for index, order_id in enumerate(all_result_order_ids)}
    all_order_results.sort(key=lambda row: result_order_index.get(row.get("order_id"), 999999))
    if success:
        message = f"Product Separator completed {live_success_count}/{len(split_order_ids)} order(s)."
    else:
        attention_phrases = []
        if manual_review_count:
            attention_phrases.append(f"{manual_review_count} order(s) require manual review")
        if non_manual_attention_count:
            attention_phrases.append(f"{non_manual_attention_count} order(s) need attention")
        if not attention_phrases:
            attention_phrases.append(f"{attention_count} order(s) need attention")
        message = (
            f"Product Separator completed {live_success_count}/{len(split_order_ids)} live order(s); "
            + " and ".join(attention_phrases)
        )
        if attention_summary:
            message += f": {attention_summary}"
        message += "."
    payload = {
        "success": success,
        "message": message,
        "action": "product_separator_batch",
        "dry_run": False,
        "list_mode": normalized_mode,
        "list_url": normalized_list_url,
        "parallel_workers": normalized_workers,
        "order_count": len(all_result_order_ids),
        "order_ids": all_result_order_ids,
        "split_order_ids": split_order_ids,
        "skipped_order_ids": skipped_order_ids,
        "live_order_ids": attempted_live_order_ids,
        "attention_order_ids": attention_order_ids,
        "order_results": all_order_results,
        "preflight": preflight_payload,
        "duration_seconds": _normalize_duration_seconds(
            _safe_float(preflight_payload.get("duration_seconds"), 0)
            + sum(_safe_float(row.get("duration_seconds"), 0) for row in live_results)
        ),
    }
    try:
        _write_json_file_atomic(RESULT_FILE, payload)
    except Exception:
        pass
    return success, message, payload


def _persist_crm_product_separator_run_result(ok, message, payload, dry_run=False):
    ensure_crm_state_file()
    payload = payload if isinstance(payload, dict) else {"success": bool(ok), "message": str(message)}
    timestamp = datetime.now().isoformat()
    order_ids = _product_separator_payload_order_ids(payload)
    order_results = _build_crm_product_separator_order_results(payload)
    duration_seconds = _normalize_duration_seconds(payload.get("duration_seconds"))
    if duration_seconds is None:
        duration_seconds = _runtime_duration_seconds(_crm_product_separator_runtime_snapshot())
    parallel_workers = _normalize_crm_positive_int(payload.get("parallel_workers"), default=1, minimum=1, maximum=CRM_SHARED_MAX_PARALLEL_WORKERS)

    with crm_state_lock:
        state = load_crm_state()
        state["last_run_timestamp"] = timestamp
        state["last_run_success"] = bool(ok)
        state["last_run_message"] = str(message)
        state["last_run_duration_seconds"] = duration_seconds
        state["last_order_count"] = len(order_ids)
        state["last_order_ids"] = order_ids
        state["last_product_separator_parallel_workers"] = parallel_workers
        state["total_runs"] = max(0, int(_safe_float(state.get("total_runs"), 0))) + 1
        if ok and not dry_run:
            state["total_orders_processed"] = (
                max(0, int(_safe_float(state.get("total_orders_processed"), 0))) + len(order_ids)
            )
        entry = {
            "timestamp": timestamp,
            "automation_key": "product_separator",
            "automation_label": "Product Separator",
            "success": bool(ok),
            "order_count": len(order_ids),
            "order_ids": order_ids,
            "parallel_workers": parallel_workers,
            "order_results": order_results,
            "duration_seconds": duration_seconds,
            "message": str(message),
            "dry_run": bool(dry_run),
        }
        history = state.get("run_history") if isinstance(state.get("run_history"), list) else []
        state["run_history"] = [entry] + history[:19]
        save_crm_state(state)

    _audit_result("crm.product_separator", ok, message)
    return state


def _start_crm_product_separator_runtime(dry_run=False, list_mode="rush", list_url=None, parallel_workers=1, order_id=None):
    normalized_order_id = _normalize_crm_single_order_id(order_id)
    normalized_mode = _normalize_crm_shipping_filter(list_mode)
    normalized_workers = _normalize_crm_positive_int(parallel_workers, default=1, minimum=1, maximum=CRM_SHARED_MAX_PARALLEL_WORKERS)
    with crm_product_separator_runtime_lock:
        crm_product_separator_runtime["running"] = True
        crm_product_separator_runtime["startedAt"] = datetime.now().isoformat()
        crm_product_separator_runtime["completedAt"] = None
        crm_product_separator_runtime["lastAction"] = "product_separator_order" if normalized_order_id else "product_separator_batch"
        crm_product_separator_runtime["targetOrderId"] = normalized_order_id
        crm_product_separator_runtime["listMode"] = normalized_mode
        crm_product_separator_runtime["listUrl"] = None if normalized_order_id else _normalize_crm_list_url(list_url)
        crm_product_separator_runtime["orderCount"] = 1 if normalized_order_id else 0
        crm_product_separator_runtime["splitOrderCount"] = 0
        crm_product_separator_runtime["currentOrderIndex"] = 0
        crm_product_separator_runtime["totalOrderCount"] = 1 if normalized_order_id else 0
        crm_product_separator_runtime["parallelWorkers"] = normalized_workers
        crm_product_separator_runtime["lastMessage"] = (
            f"Single Product Separator queued for order {normalized_order_id}."
            if normalized_order_id
            else f"Product Separator queued for {_product_separator_mode_label(normalized_mode)} mode."
        )
        crm_product_separator_runtime["lastSuccess"] = None
        crm_product_separator_runtime["dryRun"] = bool(dry_run)
        crm_product_separator_runtime["payload"] = None


def _finish_crm_product_separator_runtime(ok, message, payload, release_lock=True):
    payload = payload if isinstance(payload, dict) else {"success": bool(ok), "message": str(message)}
    with crm_product_separator_runtime_lock:
        crm_product_separator_runtime["running"] = False
        crm_product_separator_runtime["completedAt"] = datetime.now().isoformat()
        crm_product_separator_runtime["lastMessage"] = str(message)
        crm_product_separator_runtime["lastSuccess"] = bool(ok)
        crm_product_separator_runtime["targetOrderId"] = _normalize_crm_single_order_id(payload.get("target_order_id"))
        crm_product_separator_runtime["orderCount"] = len(_product_separator_payload_order_ids(payload))
        crm_product_separator_runtime["splitOrderCount"] = len(_extract_crm_order_ids({"order_ids": payload.get("split_order_ids")}))
        crm_product_separator_runtime["currentOrderIndex"] = crm_product_separator_runtime["orderCount"]
        crm_product_separator_runtime["totalOrderCount"] = crm_product_separator_runtime["orderCount"]
        crm_product_separator_runtime["parallelWorkers"] = _normalize_crm_positive_int(
            payload.get("parallel_workers") or crm_product_separator_runtime.get("parallelWorkers"),
            default=1,
            minimum=1,
            maximum=CRM_SHARED_MAX_PARALLEL_WORKERS,
        )
        crm_product_separator_runtime["payload"] = payload
    if release_lock and crm_lock.locked():
        crm_lock.release()


def _crm_product_separator_run_thread(dry_run=False, list_mode="rush", list_url=None, parallel_workers=1, order_id=None):
    ok = False
    message = "Product Separator did not run."
    payload = {"success": False, "message": message}
    try:
        log_automation_event(
            "crm.product_separator",
            "STARTED",
            f"run started. dry_run={bool(dry_run)} action={'product_separator_order' if order_id else 'product_separator_batch'} order_id={order_id or ''} list_mode={_normalize_crm_shipping_filter(list_mode)} parallel_workers={parallel_workers} list_url={_normalize_crm_list_url(list_url)}",
            source="server.py",
        )
        ok, message, payload = _execute_crm_product_separator_worker(
            dry_run=dry_run,
            list_mode=list_mode,
            list_url=list_url,
            parallel_workers=parallel_workers,
            order_id=order_id,
        )
    except Exception as e:
        logger.exception("CRM Product Separator background run failed unexpectedly")
        ok = False
        message = str(e)
        payload = {"success": False, "message": message, "error_type": type(e).__name__}
    finally:
        state = _persist_crm_product_separator_run_result(ok, message, payload, dry_run=dry_run)
        payload = payload if isinstance(payload, dict) else {"success": bool(ok), "message": str(message)}
        payload["state"] = state
        _finish_crm_product_separator_runtime(ok, message, payload, release_lock=True)


def start_crm_product_separator_run(dry_run=False, list_mode="rush", list_url=None, parallel_workers=None, order_id=None):
    normalized_order_id = _normalize_crm_single_order_id(order_id) if _crm_address_value_supplied(order_id) else None
    if _crm_address_value_supplied(order_id) and not normalized_order_id:
        return False, "Order ID must be a 7-digit value or CRM order URL."
    normalized_mode = _normalize_crm_shipping_filter(list_mode)
    saved_workers = _saved_crm_automation_parallel_workers(default=1)
    normalized_workers = (
        _normalize_crm_positive_int(parallel_workers, default=saved_workers, minimum=1, maximum=CRM_SHARED_MAX_PARALLEL_WORKERS)
        if _crm_address_value_supplied(parallel_workers)
        else saved_workers
    )
    if normalized_order_id:
        normalized_workers = 1
    if not crm_lock.acquire(blocking=False):
        return False, "A CRM automation run is already in progress."
    _start_crm_product_separator_runtime(
        dry_run=dry_run,
        list_mode=normalized_mode,
        list_url=list_url,
        parallel_workers=normalized_workers,
        order_id=normalized_order_id,
    )
    threading.Thread(
        target=_crm_product_separator_run_thread,
        args=(bool(dry_run), normalized_mode, _normalize_crm_list_url(list_url), normalized_workers, normalized_order_id),
        daemon=True,
    ).start()
    if normalized_order_id:
        if dry_run:
            return True, f"Single Product Separator dry run started for order {normalized_order_id}."
        return True, f"Single Product Separator run started for order {normalized_order_id}."
    if dry_run:
        return True, f"Product Separator list dry run started for {_product_separator_mode_label(normalized_mode)} mode with {normalized_workers} worker(s)."
    return True, f"Product Separator started for {_product_separator_mode_label(normalized_mode)} mode with {normalized_workers} worker(s)."


def get_crm_product_separator_status_payload():
    runtime = _crm_product_separator_runtime_snapshot()
    ensure_crm_state_file()
    with crm_state_lock:
        state = load_crm_state()
    return {
        "success": True,
        "running": bool(runtime.get("running")),
        "runtime": runtime,
        "state": state,
    }


def _crm_order_goods_worker_timeout(batch_size=None, parallel_workers=1):
    base_timeout = max(180, CRM_ACTION_TIMEOUT * 12)
    normalized_batch_size = _normalize_crm_batch_size(batch_size, default=0, minimum=1, maximum=25, allow_unlimited=True)
    normalized_parallel_workers = _normalize_crm_positive_int(
        parallel_workers,
        default=1,
        minimum=1,
        maximum=CRM_ORDER_GOODS_MAX_PARALLEL_WORKERS,
    )
    if normalized_batch_size is None:
        return max(base_timeout, CRM_ADDRESS_CONTINUOUS_BATCH_TIMEOUT_SECONDS)
    normalized_parallel_workers = min(normalized_parallel_workers, normalized_batch_size)
    waves = max(1, math.ceil(normalized_batch_size / max(1, normalized_parallel_workers)))
    return base_timeout + max(0, waves - 1) * max(45, CRM_ACTION_TIMEOUT * 3)


def _execute_crm_order_goods_worker(dry_run=False, batch_size=None, parallel_workers=1, list_url=None, visible=None, show_terminal=None, order_id=None):
    normalized_order_id = _normalize_crm_single_order_id(order_id)
    if _crm_address_value_supplied(order_id) and not normalized_order_id:
        return False, "Order ID must be a 7-digit value or CRM order URL.", {
            "success": False,
            "message": "Order ID must be a 7-digit value or CRM order URL.",
            "action": "order_goods_single",
            "dry_run": bool(dry_run),
            "shipping_filter": "rush",
            "manual_review_required": True,
            "resolution": "invalid_order_id",
        }
    normalized_batch_size = _normalize_crm_batch_size(batch_size, default=0, minimum=1, maximum=25, allow_unlimited=True)
    normalized_parallel_workers = _normalize_crm_positive_int(
        parallel_workers,
        default=1,
        minimum=1,
        maximum=CRM_ORDER_GOODS_MAX_PARALLEL_WORKERS,
    )
    if normalized_batch_size is not None:
        normalized_parallel_workers = min(normalized_parallel_workers, normalized_batch_size)
    if normalized_order_id:
        normalized_batch_size = 1
        normalized_parallel_workers = 1
    normalized_list_url = _normalize_crm_list_url(list_url)
    action = "order_goods_single" if normalized_order_id else "order_goods_batch"
    args = ["--action", action]
    visible = bool(dry_run) if visible is None else bool(visible)
    show_terminal = bool(dry_run) if show_terminal is None else bool(show_terminal)
    if visible:
        args.append("--visible")
    if normalized_order_id:
        args.extend(["--order-id", normalized_order_id])
    elif normalized_batch_size is not None:
        args.extend(["--batch-size", str(normalized_batch_size)])
    args.extend(["--parallel-workers", str(normalized_parallel_workers)])
    if normalized_list_url and not normalized_order_id:
        args.extend(["--list-url", normalized_list_url])
    if dry_run:
        args.append("--dry-run")
    ok, message, payload = _run_script(
        CRM_ORDER_GOODS_SCRIPT,
        args,
        "CRMOrderGoods",
        timeout=_crm_order_goods_worker_timeout(normalized_batch_size, normalized_parallel_workers),
        show_terminal=show_terminal,
    )
    if not isinstance(payload, dict):
        payload = {"success": bool(ok), "message": str(message)}
    payload.setdefault("action", action)
    payload.setdefault("dry_run", bool(dry_run))
    payload.setdefault("shipping_filter", "rush")
    payload.setdefault("batch_size", normalized_batch_size)
    payload.setdefault("parallel_workers", normalized_parallel_workers)
    if normalized_order_id:
        payload.setdefault("target_order_id", normalized_order_id)
        payload.setdefault("order_ids", [normalized_order_id])
    if normalized_list_url and not normalized_order_id:
        payload.setdefault("list_url", normalized_list_url)
    if not ok:
        order_results = _build_crm_shipping_bypasser_order_results(payload)
        if _crm_shipping_bypasser_order_results_success(order_results):
            ok = True
            payload["success"] = True
    return ok, message, payload


def _start_crm_order_goods_runtime(dry_run=False, batch_size=None, parallel_workers=1, list_url=None, order_id=None):
    normalized_order_id = _normalize_crm_single_order_id(order_id)
    normalized_parallel_workers = _normalize_crm_positive_int(
        parallel_workers,
        default=1,
        minimum=1,
        maximum=CRM_ORDER_GOODS_MAX_PARALLEL_WORKERS,
    )
    normalized_batch_size = _normalize_crm_batch_size(
        batch_size,
        default=0,
        minimum=1,
        maximum=25,
        allow_unlimited=True,
    )
    if normalized_batch_size is not None:
        normalized_parallel_workers = min(normalized_parallel_workers, normalized_batch_size)
    if normalized_order_id:
        normalized_batch_size = 1
        normalized_parallel_workers = 1
    with crm_order_goods_runtime_lock:
        crm_order_goods_runtime["running"] = True
        crm_order_goods_runtime["startedAt"] = datetime.now().isoformat()
        crm_order_goods_runtime["completedAt"] = None
        crm_order_goods_runtime["lastAction"] = "order_goods_single" if normalized_order_id else "order_goods_batch"
        crm_order_goods_runtime["targetOrderId"] = normalized_order_id
        crm_order_goods_runtime["batchSize"] = normalized_batch_size
        crm_order_goods_runtime["parallelWorkers"] = normalized_parallel_workers
        crm_order_goods_runtime["listUrl"] = None if normalized_order_id else _normalize_crm_list_url(list_url)
        crm_order_goods_runtime["orderCount"] = 0
        crm_order_goods_runtime["currentOrderIndex"] = 0
        crm_order_goods_runtime["totalOrderCount"] = 1 if normalized_order_id else 0
        crm_order_goods_runtime["currentStage"] = "queued"
        crm_order_goods_runtime["refreshPasses"] = 0
        crm_order_goods_runtime["lastMessage"] = f"Single Stock queued for order {normalized_order_id}." if normalized_order_id else "Rush Order Goods queued."
        crm_order_goods_runtime["lastSuccess"] = None
        crm_order_goods_runtime["dryRun"] = bool(dry_run)
        crm_order_goods_runtime["payload"] = None


def _finish_crm_order_goods_runtime(ok, message, payload, release_lock=True):
    payload = payload if isinstance(payload, dict) else {}
    with crm_order_goods_runtime_lock:
        crm_order_goods_runtime["running"] = False
        crm_order_goods_runtime["completedAt"] = datetime.now().isoformat()
        crm_order_goods_runtime["lastMessage"] = str(message)
        crm_order_goods_runtime["lastSuccess"] = bool(ok)
        crm_order_goods_runtime["targetOrderId"] = _normalize_crm_single_order_id(payload.get("target_order_id"))
        crm_order_goods_runtime["orderCount"] = _extract_crm_order_count(payload)
        crm_order_goods_runtime["currentOrderIndex"] = crm_order_goods_runtime["orderCount"]
        crm_order_goods_runtime["totalOrderCount"] = crm_order_goods_runtime["orderCount"]
        crm_order_goods_runtime["currentStage"] = None
        crm_order_goods_runtime["parallelWorkers"] = _normalize_crm_positive_int(
            payload.get("parallel_workers"),
            default=crm_order_goods_runtime.get("parallelWorkers") or 1,
            minimum=1,
            maximum=CRM_ORDER_GOODS_MAX_PARALLEL_WORKERS,
        )
        crm_order_goods_runtime["refreshPasses"] = max(0, int(_safe_float(payload.get("refresh_passes"), 0)))
        crm_order_goods_runtime["payload"] = payload
    if release_lock and crm_lock.locked():
        crm_lock.release()


def _crm_order_goods_run_thread(dry_run=False, batch_size=None, parallel_workers=1, list_url=None, order_id=None):
    normalized_order_id = _normalize_crm_single_order_id(order_id)
    ok = False
    message = "Rush Order Goods did not run."
    payload = {"success": False, "message": message}
    try:
        log_automation_event(
            "crm.order_goods",
            "STARTED",
            f"Rush-only run started. dry_run={bool(dry_run)} action={'order_goods_single' if normalized_order_id else 'order_goods_batch'} order_id={normalized_order_id or ''} batch_size={_crm_batch_size_display(batch_size)} parallel_workers={parallel_workers} list_url={_normalize_crm_list_url(list_url)}",
            source="server.py",
        )
        ok, message, payload = _execute_crm_order_goods_worker(
            dry_run=dry_run,
            batch_size=batch_size,
            parallel_workers=parallel_workers,
            list_url=list_url,
            order_id=normalized_order_id,
        )
    except Exception as e:
        logger.exception("CRM Order Goods background run failed unexpectedly")
        ok = False
        message = str(e)
        payload = {"success": False, "message": message, "error_type": type(e).__name__}
    finally:
        state = _persist_crm_order_goods_run_result(ok, message, payload, dry_run=dry_run)
        _audit_result("crm.order_goods", ok, message)
        payload = payload if isinstance(payload, dict) else {"success": bool(ok), "message": str(message)}
        payload["state"] = state
        _finish_crm_order_goods_runtime(ok, message, payload, release_lock=True)


def start_crm_order_goods_run(dry_run=False, batch_size=None, parallel_workers=None, list_url=None, order_id=None):
    normalized_order_id = _normalize_crm_single_order_id(order_id) if _crm_address_value_supplied(order_id) else None
    if _crm_address_value_supplied(order_id) and not normalized_order_id:
        return False, "Order ID must be a 7-digit value or CRM order URL."
    normalized_batch_size = None
    requested_parallel_workers = _normalize_crm_positive_int(
        parallel_workers,
        default=1,
        minimum=1,
        maximum=CRM_ORDER_GOODS_MAX_PARALLEL_WORKERS,
    )
    saved_parallel_workers = _saved_crm_automation_parallel_workers(default=1)
    normalized_parallel_workers = (
        requested_parallel_workers
        if _crm_address_value_supplied(parallel_workers)
        else saved_parallel_workers
    )
    if not crm_lock.acquire(blocking=False):
        return False, "A CRM automation run is already in progress."
    _start_crm_order_goods_runtime(
        dry_run=dry_run,
        batch_size=normalized_batch_size,
        parallel_workers=normalized_parallel_workers,
        list_url=list_url,
        order_id=normalized_order_id,
    )
    threading.Thread(
        target=_crm_order_goods_run_thread,
        args=(bool(dry_run), normalized_batch_size, normalized_parallel_workers, _normalize_crm_list_url(list_url), normalized_order_id),
        daemon=True,
    ).start()
    if normalized_order_id:
        if dry_run:
            return True, f"Single Stock dry run started for order {normalized_order_id}."
        return True, f"Single Stock run started for order {normalized_order_id}."
    scope = _crm_batch_scope_phrase(normalized_batch_size)
    if dry_run:
        return True, f"Rush Order Goods dry run started {scope} with {normalized_parallel_workers} worker(s)."
    return True, f"Rush Order Goods started {scope} with {normalized_parallel_workers} worker(s)."


def get_crm_order_goods_status_payload():
    runtime = _crm_order_goods_runtime_snapshot()
    ensure_crm_state_file()
    with crm_state_lock:
        state = load_crm_state()
    return {
        "success": True,
        "running": bool(runtime.get("running")),
        "runtime": runtime,
        "state": state,
    }


def _crm_shipping_bypasser_runtime_snapshot():
    with crm_shipping_bypasser_runtime_lock:
        runtime = dict(crm_shipping_bypasser_runtime)
    return _merge_live_automation_status(runtime, "crm.shipping_bypasser")


def _shipping_bypasser_worker_timeout(batch_size=None):
    base_timeout = max(
        600,
        CRM_ACTION_TIMEOUT * 40,
        CRM_SHIPPING_BYPASSER_BASE_TIMEOUT_SECONDS,
    )
    extra_order_timeout = max(
        240,
        CRM_ACTION_TIMEOUT * 16,
        CRM_SHIPPING_BYPASSER_EXTRA_ORDER_TIMEOUT_SECONDS,
    )
    normalized_batch_size = _normalize_crm_batch_size(batch_size, default=0, minimum=1, maximum=25, allow_unlimited=True)
    if normalized_batch_size is None:
        return CRM_ADDRESS_CONTINUOUS_BATCH_TIMEOUT_SECONDS
    return base_timeout + max(0, normalized_batch_size - 1) * extra_order_timeout


def _execute_crm_shipping_bypasser_worker(dry_run=False, batch_size=None, list_url=None, visible=None, show_terminal=None, order_id=None):
    normalized_order_id = _normalize_crm_single_order_id(order_id)
    if _crm_address_value_supplied(order_id) and not normalized_order_id:
        return False, "Order ID must be a 7-digit value or CRM order URL.", {
            "success": False,
            "message": "Order ID must be a 7-digit value or CRM order URL.",
            "action": "shipping_bypass_single",
            "dry_run": bool(dry_run),
            "shipping_filter": "rush",
            "manual_review_required": True,
            "resolution": "invalid_order_id",
        }
    normalized_batch_size = _normalize_crm_batch_size(batch_size, default=0, minimum=1, maximum=25, allow_unlimited=True)
    if normalized_order_id:
        normalized_batch_size = 1
    normalized_list_url = _normalize_crm_list_url(list_url)
    action = "shipping_bypass_single" if normalized_order_id else "shipping_bypass_batch"
    args = ["--action", action]
    visible = bool(dry_run) if visible is None else bool(visible)
    show_terminal = bool(dry_run) if show_terminal is None else bool(show_terminal)
    if visible:
        args.append("--visible")
    if normalized_order_id:
        args.extend(["--order-id", normalized_order_id])
    elif normalized_batch_size is not None:
        args.extend(["--batch-size", str(normalized_batch_size)])
    if normalized_list_url and not normalized_order_id:
        args.extend(["--list-url", normalized_list_url])
    if dry_run:
        args.append("--dry-run")
    ok, message, payload = _run_script(
        CRM_SHIPPING_BYPASSER_SCRIPT,
        args,
        "CRMShippingBypasser",
        timeout=_shipping_bypasser_worker_timeout(normalized_batch_size),
        show_terminal=show_terminal,
    )
    if not isinstance(payload, dict):
        payload = {"success": bool(ok), "message": str(message)}
    payload.setdefault("action", action)
    payload.setdefault("dry_run", bool(dry_run))
    payload.setdefault("shipping_filter", "rush")
    payload.setdefault("batch_size", normalized_batch_size)
    payload.setdefault("parallel_workers", 1)
    if normalized_order_id:
        payload.setdefault("target_order_id", normalized_order_id)
        payload.setdefault("order_ids", [normalized_order_id])
    if normalized_list_url and not normalized_order_id:
        payload.setdefault("list_url", normalized_list_url)
    return ok, message, payload


def _shipping_bypasser_failed_order_ids(payload):
    if not isinstance(payload, dict):
        return []
    candidates = []
    report = payload.get("report") if isinstance(payload.get("report"), list) else []
    for item in report:
        if not isinstance(item, dict) or item.get("success"):
            continue
        candidates.append(item.get("order_id"))
    if not candidates:
        for item in _build_crm_order_goods_order_results(payload):
            if isinstance(item, dict) and not item.get("success"):
                candidates.append(item.get("order_id"))
    cleaned = []
    seen = set()
    for raw in candidates:
        order_ids = _extract_crm_order_ids({"order_ids": [raw]})
        if not order_ids:
            continue
        order_id = order_ids[0]
        if order_id in seen:
            continue
        seen.add(order_id)
        cleaned.append(order_id)
    return cleaned


def _shipping_bypasser_problem_details(payload):
    if not isinstance(payload, dict):
        return []
    report = payload.get("report") if isinstance(payload.get("report"), list) else []
    details = []
    seen = set()
    for item in report:
        if not isinstance(item, dict) or item.get("success"):
            continue
        order_ids = _extract_crm_order_ids({"order_ids": [item.get("order_id")]})
        if not order_ids:
            continue
        order_id = order_ids[0]
        descriptor = _crm_stock_tab_descriptor(item)
        key = (order_id, descriptor, str(item.get("outcome") or ""))
        if key in seen:
            continue
        seen.add(key)
        if descriptor and descriptor != "tab":
            details.append(f"{order_id} {descriptor}")
        else:
            details.append(order_id)
    return details


def _notify_shipping_bypasser_problem_orders(payload, message=None):
    if not isinstance(payload, dict) or bool(payload.get("success")):
        return
    details = _shipping_bypasser_problem_details(payload)
    if details:
        notify_user("Shipping Bypasser Needs Attention", f"Skipped/failed stock tab(s): {', '.join(details[:10])}.")
        return
    order_ids = _shipping_bypasser_failed_order_ids(payload)
    if order_ids:
        notify_user("Shipping Bypasser Needs Attention", f"Order(s) need attention: {', '.join(order_ids)}.")
        return
    text = str(message or payload.get("message") or "Shipping Bypasser needs attention.").strip()
    notify_user("Shipping Bypasser Needs Attention", text)


def _start_crm_shipping_bypasser_runtime(dry_run=False, batch_size=None, list_url=None, order_id=None):
    normalized_order_id = _normalize_crm_single_order_id(order_id)
    normalized_batch_size = _normalize_crm_batch_size(
        batch_size,
        default=0,
        minimum=1,
        maximum=25,
        allow_unlimited=True,
    )
    if normalized_order_id:
        normalized_batch_size = 1
    with crm_shipping_bypasser_runtime_lock:
        crm_shipping_bypasser_runtime["running"] = True
        crm_shipping_bypasser_runtime["startedAt"] = datetime.now().isoformat()
        crm_shipping_bypasser_runtime["completedAt"] = None
        crm_shipping_bypasser_runtime["lastAction"] = "shipping_bypass_single" if normalized_order_id else "shipping_bypass_batch"
        crm_shipping_bypasser_runtime["targetOrderId"] = normalized_order_id
        crm_shipping_bypasser_runtime["batchSize"] = normalized_batch_size
        crm_shipping_bypasser_runtime["parallelWorkers"] = 1
        crm_shipping_bypasser_runtime["listUrl"] = None if normalized_order_id else _normalize_crm_list_url(list_url)
        crm_shipping_bypasser_runtime["orderCount"] = 0
        crm_shipping_bypasser_runtime["currentOrderIndex"] = 0
        crm_shipping_bypasser_runtime["totalOrderCount"] = 1 if normalized_order_id else 0
        crm_shipping_bypasser_runtime["currentStage"] = "queued"
        crm_shipping_bypasser_runtime["refreshPasses"] = 0
        crm_shipping_bypasser_runtime["lastMessage"] = f"Single Shipping Bypasser queued for order {normalized_order_id}." if normalized_order_id else "Shipping Bypasser queued."
        crm_shipping_bypasser_runtime["lastSuccess"] = None
        crm_shipping_bypasser_runtime["dryRun"] = bool(dry_run)
        crm_shipping_bypasser_runtime["payload"] = None


def _finish_crm_shipping_bypasser_runtime(ok, message, payload, release_lock=True):
    payload = payload if isinstance(payload, dict) else {}
    with crm_shipping_bypasser_runtime_lock:
        crm_shipping_bypasser_runtime["running"] = False
        crm_shipping_bypasser_runtime["completedAt"] = datetime.now().isoformat()
        crm_shipping_bypasser_runtime["lastMessage"] = str(message)
        crm_shipping_bypasser_runtime["lastSuccess"] = bool(ok)
        crm_shipping_bypasser_runtime["targetOrderId"] = _normalize_crm_single_order_id(payload.get("target_order_id"))
        crm_shipping_bypasser_runtime["orderCount"] = _extract_crm_order_count(payload)
        crm_shipping_bypasser_runtime["currentOrderIndex"] = crm_shipping_bypasser_runtime["orderCount"]
        crm_shipping_bypasser_runtime["totalOrderCount"] = crm_shipping_bypasser_runtime["orderCount"]
        crm_shipping_bypasser_runtime["currentStage"] = None
        crm_shipping_bypasser_runtime["parallelWorkers"] = 1
        crm_shipping_bypasser_runtime["refreshPasses"] = max(0, int(_safe_float(payload.get("refresh_passes"), 0)))
        crm_shipping_bypasser_runtime["payload"] = payload
    if release_lock and crm_lock.locked():
        crm_lock.release()


def _persist_crm_shipping_bypasser_run_result(ok, message, payload, dry_run=False):
    ensure_crm_state_file()
    timestamp = datetime.now().isoformat()
    order_count = _extract_crm_order_count(payload)
    order_ids = _extract_crm_order_ids(payload)
    order_results = _build_crm_shipping_bypasser_order_results(payload)
    effective_ok = bool(ok or _crm_shipping_bypasser_order_results_success(order_results))
    history_message = _compact_crm_shipping_bypasser_history_message(message)
    duration_seconds = _normalize_duration_seconds(payload.get("duration_seconds") if isinstance(payload, dict) else None)
    if duration_seconds is None:
        duration_seconds = _runtime_duration_seconds(_crm_shipping_bypasser_runtime_snapshot())
    with crm_state_lock:
        state = load_crm_state()
        state["last_run_timestamp"] = timestamp
        state["last_run_success"] = effective_ok
        state["last_run_message"] = history_message
        state["last_run_duration_seconds"] = duration_seconds
        state["last_order_count"] = order_count
        state["last_order_ids"] = order_ids
        state["total_runs"] = max(0, int(_safe_float(state.get("total_runs"), 0))) + 1
        if effective_ok and not dry_run:
            state["total_orders_processed"] = (
                max(0, int(_safe_float(state.get("total_orders_processed"), 0))) + order_count
            )
        entry = {
            "timestamp": timestamp,
            "automation_key": "shipping_bypasser",
            "automation_label": "Shipping Bypasser",
            "success": effective_ok,
            "order_count": order_count,
            "order_ids": order_ids,
            "parallel_workers": 1,
            "order_results": order_results,
            "duration_seconds": duration_seconds,
            "message": history_message,
            "dry_run": bool(dry_run),
        }
        history = state.get("run_history") if isinstance(state.get("run_history"), list) else []
        state["run_history"] = [entry] + history[:19]
        save_crm_state(state)
    return state


def _crm_shipping_bypasser_run_thread(dry_run=False, batch_size=None, list_url=None, order_id=None):
    normalized_order_id = _normalize_crm_single_order_id(order_id)
    ok = False
    message = "Shipping Bypasser did not run."
    payload = {"success": False, "message": message}
    try:
        log_automation_event(
            "crm.shipping_bypasser",
            "STARTED",
            f"Run started. dry_run={bool(dry_run)} action={'shipping_bypass_single' if normalized_order_id else 'shipping_bypass_batch'} order_id={normalized_order_id or ''} batch_size={_crm_batch_size_display(batch_size)} list_url={_normalize_crm_list_url(list_url)}",
            source="server.py",
        )
        ok, message, payload = _execute_crm_shipping_bypasser_worker(
            dry_run=dry_run,
            batch_size=batch_size,
            list_url=list_url,
            order_id=normalized_order_id,
        )
    except Exception as e:
        logger.exception("CRM Shipping Bypasser background run failed unexpectedly")
        ok = False
        message = str(e)
        payload = {"success": False, "message": message, "error_type": type(e).__name__}
    finally:
        state = _persist_crm_shipping_bypasser_run_result(ok, message, payload, dry_run=dry_run)
        _audit_result("crm.shipping_bypasser", ok, message)
        payload = payload if isinstance(payload, dict) else {"success": bool(ok), "message": str(message)}
        payload["state"] = state
        _notify_shipping_bypasser_problem_orders(payload, message)
        _finish_crm_shipping_bypasser_runtime(ok, message, payload, release_lock=True)


def start_crm_shipping_bypasser_run(dry_run=False, batch_size=None, list_url=None, order_id=None):
    normalized_order_id = _normalize_crm_single_order_id(order_id) if _crm_address_value_supplied(order_id) else None
    if _crm_address_value_supplied(order_id) and not normalized_order_id:
        return False, "Order ID must be a 7-digit value or CRM order URL."
    normalized_batch_size = None
    if normalized_order_id:
        normalized_batch_size = 1
    if not crm_lock.acquire(blocking=False):
        return False, "A CRM automation run is already in progress."
    _start_crm_shipping_bypasser_runtime(
        dry_run=dry_run,
        batch_size=normalized_batch_size,
        list_url=list_url,
        order_id=normalized_order_id,
    )
    threading.Thread(
        target=_crm_shipping_bypasser_run_thread,
        args=(bool(dry_run), normalized_batch_size, _normalize_crm_list_url(list_url), normalized_order_id),
        daemon=True,
    ).start()
    if normalized_order_id:
        if dry_run:
            return True, f"Single Shipping Bypasser dry run started for order {normalized_order_id}."
        return True, f"Single Shipping Bypasser run started for order {normalized_order_id}."
    if dry_run:
        return True, "Shipping Bypasser dry run started."
    return True, "Shipping Bypasser started."


def get_crm_shipping_bypasser_status_payload():
    runtime = _crm_shipping_bypasser_runtime_snapshot()
    ensure_crm_state_file()
    with crm_state_lock:
        state = load_crm_state()
    return {
        "success": True,
        "running": bool(runtime.get("running")),
        "runtime": runtime,
        "state": state,
    }


def _crm_push_back_runtime_snapshot():
    with crm_push_back_runtime_lock:
        runtime = dict(crm_push_back_runtime)
    return _merge_live_automation_status(runtime, "crm.push_back")


def _push_back_worker_timeout(batch_size=None, parallel_workers=1):
    base_timeout = max(600, CRM_ACTION_TIMEOUT * 40)
    normalized_batch_size = _normalize_crm_batch_size(batch_size, default=0, minimum=1, maximum=25, allow_unlimited=True)
    normalized_parallel_workers = _normalize_crm_positive_int(
        parallel_workers,
        default=1,
        minimum=1,
        maximum=CRM_SHARED_MAX_PARALLEL_WORKERS,
    )
    if normalized_batch_size is None:
        return CRM_ADDRESS_CONTINUOUS_BATCH_TIMEOUT_SECONDS
    normalized_parallel_workers = min(normalized_parallel_workers, normalized_batch_size)
    waves = max(1, math.ceil(normalized_batch_size / max(1, normalized_parallel_workers)))
    return base_timeout + max(0, waves - 1) * max(180, CRM_ACTION_TIMEOUT * 12)


def _build_crm_push_back_order_results(payload):
    if not isinstance(payload, dict):
        return []
    report = payload.get("report") if isinstance(payload.get("report"), list) else []
    rows = []
    seen = set()
    for item in report[:100]:
        if not isinstance(item, dict):
            continue
        order_ids = _extract_crm_order_ids({"order_ids": [item.get("order_id")]})
        if not order_ids:
            continue
        order_id = order_ids[0]
        if order_id in seen:
            continue
        seen.add(order_id)
        success = bool(item.get("success"))
        outcome = str(item.get("outcome") or "")
        rows.append(
            {
                "order_id": order_id,
                "success": success,
                "status": _crm_order_goods_outcome_label(outcome, success),
                "outcome": outcome,
                "message": str(item.get("message") or ""),
                "duration_seconds": _normalize_duration_seconds(item.get("duration_seconds") or item.get("session_duration_seconds")),
                "production_date": item.get("production_date"),
                "target_production_date": item.get("target_production_date"),
                "saved_production_date": item.get("saved_production_date"),
                "due_date": item.get("due_date"),
                "stock_order_attempted": bool(item.get("stock_order_attempted")),
                "stock_order_success": item.get("stock_order_success") if item.get("stock_order_attempted") else None,
                "stock_order_results": item.get("stock_order_results") if isinstance(item.get("stock_order_results"), list) else None,
            }
        )
    if rows:
        return rows
    return _normalize_crm_stock_order_results(
        [],
        fallback_order_ids=_extract_crm_order_ids(payload),
        fallback_success=bool(payload.get("success")),
        fallback_message=str(payload.get("message") or ""),
    )


def _execute_crm_push_back_worker(dry_run=False, batch_size=None, processing_filter="rush", list_url=None, visible=None, show_terminal=None, order_id=None, parallel_workers=1):
    normalized_filter = _normalize_crm_shipping_filter(processing_filter)
    if normalized_filter not in {"rush", "813"}:
        message = "Push Back is only available for Rush and 813 modes."
        return False, message, {
            "success": False,
            "message": message,
            "action": "push_back_batch",
            "dry_run": bool(dry_run),
            "shipping_filter": normalized_filter,
            "manual_review_required": False,
            "resolution": "unsupported_filter",
        }
    normalized_order_id = _normalize_crm_single_order_id(order_id)
    if _crm_address_value_supplied(order_id) and not normalized_order_id:
        message = "Order ID must be a 7-digit value or CRM order URL."
        return False, message, {
            "success": False,
            "message": message,
            "action": "push_back_single",
            "dry_run": bool(dry_run),
            "shipping_filter": normalized_filter,
            "manual_review_required": True,
            "resolution": "invalid_order_id",
        }
    normalized_batch_size = _normalize_crm_batch_size(batch_size, default=0, minimum=1, maximum=25, allow_unlimited=True)
    normalized_parallel_workers = _normalize_crm_positive_int(
        parallel_workers,
        default=1,
        minimum=1,
        maximum=CRM_SHARED_MAX_PARALLEL_WORKERS,
    )
    if normalized_batch_size is not None:
        normalized_parallel_workers = min(normalized_parallel_workers, normalized_batch_size)
    if normalized_order_id:
        normalized_batch_size = 1
        normalized_parallel_workers = 1
    normalized_list_url = _normalize_crm_list_url(list_url)
    action = "push_back_single" if normalized_order_id else "push_back_batch"
    args = ["--action", action, "--processing-filter", normalized_filter]
    visible = bool(dry_run) if visible is None else bool(visible)
    show_terminal = bool(dry_run) if show_terminal is None else bool(show_terminal)
    if visible:
        args.append("--visible")
    if normalized_order_id:
        args.extend(["--order-id", normalized_order_id])
    elif normalized_batch_size is not None:
        args.extend(["--batch-size", str(normalized_batch_size)])
    args.extend(["--parallel-workers", str(normalized_parallel_workers)])
    if normalized_list_url and not normalized_order_id:
        args.extend(["--list-url", normalized_list_url])
    if dry_run:
        args.append("--dry-run")
    ok, message, payload = _run_script(
        CRM_PUSH_BACK_SCRIPT,
        args,
        "CRMPushBack",
        timeout=_push_back_worker_timeout(normalized_batch_size, normalized_parallel_workers),
        show_terminal=show_terminal,
    )
    if not isinstance(payload, dict):
        payload = {"success": bool(ok), "message": str(message)}
    payload.setdefault("action", action)
    payload.setdefault("dry_run", bool(dry_run))
    payload.setdefault("shipping_filter", normalized_filter)
    payload.setdefault("batch_size", normalized_batch_size)
    payload.setdefault("parallel_workers", normalized_parallel_workers)
    if normalized_order_id:
        payload.setdefault("target_order_id", normalized_order_id)
        payload.setdefault("order_ids", [normalized_order_id])
    if normalized_list_url and not normalized_order_id:
        payload.setdefault("list_url", normalized_list_url)
    return ok, message, payload


def _start_crm_push_back_runtime(dry_run=False, batch_size=None, processing_filter="rush", list_url=None, order_id=None, parallel_workers=1):
    normalized_order_id = _normalize_crm_single_order_id(order_id)
    normalized_batch_size = _normalize_crm_batch_size(
        batch_size,
        default=0,
        minimum=1,
        maximum=25,
        allow_unlimited=True,
    )
    normalized_parallel_workers = _normalize_crm_positive_int(
        parallel_workers,
        default=1,
        minimum=1,
        maximum=CRM_SHARED_MAX_PARALLEL_WORKERS,
    )
    if normalized_batch_size is not None:
        normalized_parallel_workers = min(normalized_parallel_workers, normalized_batch_size)
    if normalized_order_id:
        normalized_batch_size = 1
        normalized_parallel_workers = 1
    with crm_push_back_runtime_lock:
        crm_push_back_runtime["running"] = True
        crm_push_back_runtime["startedAt"] = datetime.now().isoformat()
        crm_push_back_runtime["completedAt"] = None
        crm_push_back_runtime["lastAction"] = "push_back_single" if normalized_order_id else "push_back_batch"
        crm_push_back_runtime["targetOrderId"] = normalized_order_id
        crm_push_back_runtime["processingFilter"] = _normalize_crm_shipping_filter(processing_filter)
        crm_push_back_runtime["batchSize"] = normalized_batch_size
        crm_push_back_runtime["parallelWorkers"] = normalized_parallel_workers
        crm_push_back_runtime["listUrl"] = None if normalized_order_id else _normalize_crm_list_url(list_url)
        crm_push_back_runtime["orderCount"] = 0
        crm_push_back_runtime["currentOrderIndex"] = 0
        crm_push_back_runtime["totalOrderCount"] = 1 if normalized_order_id else 0
        crm_push_back_runtime["currentStage"] = "queued"
        crm_push_back_runtime["refreshPasses"] = 0
        crm_push_back_runtime["lastMessage"] = f"Single Push Back queued for order {normalized_order_id}." if normalized_order_id else "Push Back queued."
        crm_push_back_runtime["lastSuccess"] = None
        crm_push_back_runtime["dryRun"] = bool(dry_run)
        crm_push_back_runtime["payload"] = None


def _finish_crm_push_back_runtime(ok, message, payload, release_lock=True):
    payload = payload if isinstance(payload, dict) else {}
    with crm_push_back_runtime_lock:
        crm_push_back_runtime["running"] = False
        crm_push_back_runtime["completedAt"] = datetime.now().isoformat()
        crm_push_back_runtime["lastMessage"] = str(message)
        crm_push_back_runtime["lastSuccess"] = bool(ok)
        crm_push_back_runtime["targetOrderId"] = _normalize_crm_single_order_id(payload.get("target_order_id"))
        crm_push_back_runtime["orderCount"] = _extract_crm_order_count(payload)
        crm_push_back_runtime["currentOrderIndex"] = crm_push_back_runtime["orderCount"]
        crm_push_back_runtime["totalOrderCount"] = crm_push_back_runtime["orderCount"]
        crm_push_back_runtime["currentStage"] = None
        crm_push_back_runtime["parallelWorkers"] = _normalize_crm_positive_int(
            payload.get("parallel_workers"),
            default=crm_push_back_runtime.get("parallelWorkers") or 1,
            minimum=1,
            maximum=CRM_SHARED_MAX_PARALLEL_WORKERS,
        )
        crm_push_back_runtime["refreshPasses"] = max(0, int(_safe_float(payload.get("refresh_passes"), 0)))
        crm_push_back_runtime["payload"] = payload
    if release_lock and crm_lock.locked():
        crm_lock.release()


def _persist_crm_push_back_run_result(ok, message, payload, dry_run=False):
    ensure_crm_state_file()
    timestamp = datetime.now().isoformat()
    order_count = _extract_crm_order_count(payload)
    order_ids = _extract_crm_order_ids(payload)
    order_results = _build_crm_push_back_order_results(payload)
    duration_seconds = _normalize_duration_seconds(payload.get("duration_seconds") if isinstance(payload, dict) else None)
    if duration_seconds is None:
        duration_seconds = _runtime_duration_seconds(_crm_push_back_runtime_snapshot())
    stage_timings = _normalize_stage_timings(payload.get("stage_timings") if isinstance(payload, dict) else [])
    parallel_workers = _normalize_crm_positive_int(
        payload.get("parallel_workers") if isinstance(payload, dict) else 1,
        default=1,
        minimum=1,
        maximum=CRM_SHARED_MAX_PARALLEL_WORKERS,
    )
    with crm_state_lock:
        state = load_crm_state()
        state["last_run_timestamp"] = timestamp
        state["last_run_success"] = bool(ok)
        state["last_run_message"] = str(message)
        state["last_run_duration_seconds"] = duration_seconds
        state["last_stage_timings"] = stage_timings
        state["last_order_count"] = order_count
        state["last_order_ids"] = order_ids
        state["total_runs"] = max(0, int(_safe_float(state.get("total_runs"), 0))) + 1
        if ok and not dry_run:
            state["total_orders_processed"] = (
                max(0, int(_safe_float(state.get("total_orders_processed"), 0))) + order_count
            )
        entry = {
            "timestamp": timestamp,
            "automation_key": "push_back",
            "automation_label": "Push Back",
            "success": bool(ok),
            "order_count": order_count,
            "order_ids": order_ids,
            "parallel_workers": parallel_workers,
            "order_results": order_results,
            "duration_seconds": duration_seconds,
            "stage_timings": stage_timings,
            "message": str(message),
            "dry_run": bool(dry_run),
        }
        history = state.get("run_history") if isinstance(state.get("run_history"), list) else []
        state["run_history"] = [entry] + history[:19]
        save_crm_state(state)
    return state


def _crm_push_back_run_thread(dry_run=False, batch_size=None, processing_filter="rush", list_url=None, order_id=None, parallel_workers=1):
    normalized_order_id = _normalize_crm_single_order_id(order_id)
    normalized_filter = _normalize_crm_shipping_filter(processing_filter)
    ok = False
    message = "Push Back did not run."
    payload = {"success": False, "message": message}
    try:
        log_automation_event(
            "crm.push_back",
            "STARTED",
            f"Run started. dry_run={bool(dry_run)} action={'push_back_single' if normalized_order_id else 'push_back_batch'} order_id={normalized_order_id or ''} filter={normalized_filter} batch_size={_crm_batch_size_display(batch_size)} parallel_workers={parallel_workers} list_url={_normalize_crm_list_url(list_url)}",
            source="server.py",
        )
        ok, message, payload = _execute_crm_push_back_worker(
            dry_run=dry_run,
            batch_size=batch_size,
            processing_filter=normalized_filter,
            list_url=list_url,
            order_id=normalized_order_id,
            parallel_workers=parallel_workers,
        )
    except Exception as e:
        logger.exception("CRM Push Back background run failed unexpectedly")
        ok = False
        message = str(e)
        payload = {"success": False, "message": message, "error_type": type(e).__name__}
    finally:
        state = _persist_crm_push_back_run_result(ok, message, payload, dry_run=dry_run)
        _audit_result("crm.push_back", ok, message)
        payload = payload if isinstance(payload, dict) else {"success": bool(ok), "message": str(message)}
        payload["state"] = state
        _finish_crm_push_back_runtime(ok, message, payload, release_lock=True)


def start_crm_push_back_run(dry_run=False, batch_size=None, processing_filter="rush", list_url=None, order_id=None, parallel_workers=None):
    normalized_filter = _normalize_crm_shipping_filter(processing_filter)
    if normalized_filter not in {"rush", "813"}:
        return False, "Push Back is only available for Rush and 813 modes."
    normalized_order_id = _normalize_crm_single_order_id(order_id) if _crm_address_value_supplied(order_id) else None
    if _crm_address_value_supplied(order_id) and not normalized_order_id:
        return False, "Order ID must be a 7-digit value or CRM order URL."
    normalized_batch_size = None
    requested_parallel_workers = _normalize_crm_positive_int(
        parallel_workers,
        default=1,
        minimum=1,
        maximum=CRM_SHARED_MAX_PARALLEL_WORKERS,
    )
    saved_parallel_workers = _saved_crm_automation_parallel_workers(default=1)
    normalized_parallel_workers = (
        requested_parallel_workers
        if _crm_address_value_supplied(parallel_workers)
        else saved_parallel_workers
    )
    if normalized_order_id:
        normalized_batch_size = 1
        normalized_parallel_workers = 1
    if not crm_lock.acquire(blocking=False):
        return False, "A CRM automation run is already in progress."
    _start_crm_push_back_runtime(
        dry_run=dry_run,
        batch_size=normalized_batch_size,
        processing_filter=normalized_filter,
        list_url=list_url,
        order_id=normalized_order_id,
        parallel_workers=normalized_parallel_workers,
    )
    threading.Thread(
        target=_crm_push_back_run_thread,
        args=(bool(dry_run), normalized_batch_size, normalized_filter, _normalize_crm_list_url(list_url), normalized_order_id, normalized_parallel_workers),
        daemon=True,
    ).start()
    if normalized_order_id:
        if dry_run:
            return True, f"Single Push Back dry run started for order {normalized_order_id}."
        return True, f"Single Push Back run started for order {normalized_order_id}."
    if dry_run:
        return True, f"Push Back dry run started with {normalized_parallel_workers} worker(s)."
    return True, f"Push Back started with {normalized_parallel_workers} worker(s)."


def get_crm_push_back_status_payload():
    runtime = _crm_push_back_runtime_snapshot()
    ensure_crm_state_file()
    with crm_state_lock:
        state = load_crm_state()
    return {
        "success": True,
        "running": bool(runtime.get("running")),
        "runtime": runtime,
        "state": state,
    }


def _normalize_crm_auto_split_count(value, *, field_label, minimum=1, maximum=100):
    try:
        number = int(str(value or "").strip())
    except Exception:
        raise ValueError(f"{field_label} is required.")
    if number < minimum:
        raise ValueError(f"{field_label} must be at least {minimum}.")
    if number > maximum:
        raise ValueError(f"{field_label} must be {maximum} or less.")
    return number


def _normalize_crm_auto_split_target(raw):
    text = str(raw or "").strip()
    if not text:
        return None, None
    if re.match(r"^https?://", text, flags=re.I):
        return _normalize_crm_single_order_id(text), text
    order_id = _normalize_crm_single_order_id(text)
    return order_id, None


def _crm_auto_splitter_runtime_snapshot():
    with crm_auto_splitter_runtime_lock:
        return dict(crm_auto_splitter_runtime)


def _start_crm_auto_splitter_runtime(order_target, tab_count, divisions, minimum_tabs=10, dry_run=True, parallel_workers=1):
    order_id, order_url = _normalize_crm_auto_split_target(order_target)
    with crm_auto_splitter_runtime_lock:
        crm_auto_splitter_runtime["running"] = True
        crm_auto_splitter_runtime["startedAt"] = datetime.now().isoformat()
        crm_auto_splitter_runtime["completedAt"] = None
        crm_auto_splitter_runtime["lastAction"] = "split_order"
        crm_auto_splitter_runtime["targetOrderId"] = order_id
        crm_auto_splitter_runtime["orderUrl"] = order_url
        crm_auto_splitter_runtime["tabCount"] = tab_count
        crm_auto_splitter_runtime["divisions"] = divisions
        crm_auto_splitter_runtime["minimumTabs"] = minimum_tabs
        crm_auto_splitter_runtime["parallelWorkers"] = parallel_workers
        crm_auto_splitter_runtime["lastMessage"] = f"Auto Splitter queued for order {order_id or order_url}."
        crm_auto_splitter_runtime["lastSuccess"] = None
        crm_auto_splitter_runtime["dryRun"] = bool(dry_run)
        crm_auto_splitter_runtime["payload"] = None


def _finish_crm_auto_splitter_runtime(ok, message, payload, release_lock=True):
    payload = payload if isinstance(payload, dict) else {"success": bool(ok), "message": str(message)}
    with crm_auto_splitter_runtime_lock:
        crm_auto_splitter_runtime["running"] = False
        crm_auto_splitter_runtime["completedAt"] = datetime.now().isoformat()
        crm_auto_splitter_runtime["lastMessage"] = str(message)
        crm_auto_splitter_runtime["lastSuccess"] = bool(ok)
        crm_auto_splitter_runtime["targetOrderId"] = _normalize_crm_single_order_id(payload.get("target_order_id"))
        crm_auto_splitter_runtime["orderUrl"] = str(payload.get("order_url") or crm_auto_splitter_runtime.get("orderUrl") or "").strip() or None
        tab_count = payload.get("expected_tab_count") or crm_auto_splitter_runtime.get("tabCount")
        divisions = payload.get("divisions") or crm_auto_splitter_runtime.get("divisions")
        crm_auto_splitter_runtime["tabCount"] = (
            _normalize_crm_auto_split_count(tab_count, field_label="Tab count", minimum=1, maximum=1000)
            if tab_count not in (None, "")
            else None
        )
        crm_auto_splitter_runtime["divisions"] = (
            _normalize_crm_auto_split_count(divisions, field_label="Divisions", minimum=1, maximum=100)
            if divisions not in (None, "")
            else None
        )
        crm_auto_splitter_runtime["parallelWorkers"] = _normalize_crm_positive_int(
            payload.get("parallel_workers") or payload.get("parallelWorkers") or crm_auto_splitter_runtime.get("parallelWorkers"),
            default=1,
            minimum=1,
            maximum=CRM_SHARED_MAX_PARALLEL_WORKERS,
        )
        crm_auto_splitter_runtime["payload"] = payload
    if release_lock and crm_lock.locked():
        crm_lock.release()


def _crm_auto_splitter_recovery_payload_is_usable(payload, order_id, tab_count, divisions):
    if not isinstance(payload, dict):
        return False
    if bool(payload.get("success")) or bool(payload.get("dry_run")):
        return False
    payload_order_id = _normalize_crm_single_order_id(payload.get("target_order_id") or payload.get("order_id"))
    if not payload_order_id or payload_order_id != _normalize_crm_single_order_id(order_id):
        return False
    if tab_count in (None, "") or divisions in (None, ""):
        return False
    if int(_safe_float(payload.get("expected_tab_count"), 0)) != int(_safe_float(tab_count, 0)):
        return False
    if int(_safe_float(payload.get("divisions"), 0)) != int(_safe_float(divisions, 0)):
        return False
    report = payload.get("report") if isinstance(payload.get("report"), dict) else {}
    return bool(report.get("partial")) and _crm_auto_splitter_recovery_order_ids_from_payload(payload)


def _crm_auto_splitter_recovery_order_ids_from_payload(payload):
    if not isinstance(payload, dict):
        return []
    direct_ids = payload.get("new_order_ids") if isinstance(payload.get("new_order_ids"), list) else []
    report = payload.get("report") if isinstance(payload.get("report"), dict) else {}
    split_orders = report.get("split_orders") if isinstance(report.get("split_orders"), list) else []
    order_ids = list(direct_ids) + [item.get("order_id") for item in split_orders if isinstance(item, dict)]
    normalized = []
    for value in order_ids:
        order_id = _normalize_crm_single_order_id(value)
        if order_id and order_id not in normalized:
            normalized.append(order_id)
    return normalized


def _crm_auto_splitter_recovery_order_ids(order_id, tab_count, divisions):
    with crm_state_lock:
        state = load_crm_state()
        candidates = [
            state.get("last_auto_splitter_recovery_payload"),
            state.get("last_auto_splitter_payload"),
        ]
    for payload in candidates:
        if _crm_auto_splitter_recovery_payload_is_usable(payload, order_id, tab_count, divisions):
            return _crm_auto_splitter_recovery_order_ids_from_payload(payload)
    return []


def _crm_auto_splitter_payload_matches_request(payload, order_target, tab_count, divisions, minimum_tabs):
    if not isinstance(payload, dict):
        return False
    order_id, order_url = _normalize_crm_auto_split_target(order_target)
    payload_order_id = _normalize_crm_single_order_id(payload.get("target_order_id") or payload.get("order_id"))
    payload_order_url = str(payload.get("order_url") or "").strip()
    if order_id:
        if payload_order_id != order_id and _normalize_crm_single_order_id(payload_order_url) != order_id:
            return False
    elif order_url and payload_order_url and payload_order_url.rstrip("/") != order_url.rstrip("/"):
        return False

    report = payload.get("report") if isinstance(payload.get("report"), dict) else {}
    payload_minimum_tabs = payload.get("minimum_tabs") or report.get("minimum_tabs")
    if tab_count not in (None, "") and int(_safe_float(payload.get("expected_tab_count") or report.get("expected_tab_count"), 0)) != int(_safe_float(tab_count, 0)):
        return False
    if divisions not in (None, "") and int(_safe_float(payload.get("divisions") or report.get("divisions"), 0)) != int(_safe_float(divisions, 0)):
        return False
    if payload_minimum_tabs is not None and int(_safe_float(payload_minimum_tabs, 0)) != int(_safe_float(minimum_tabs, 0)):
        return False
    return True


def _crm_auto_splitter_recent_dry_run_payload(order_target, tab_count, divisions, minimum_tabs):
    if CRM_AUTO_SPLITTER_PREFLIGHT_REUSE_SECONDS <= 0:
        return None
    if tab_count in (None, "") or divisions in (None, ""):
        return None
    with crm_state_lock:
        state = load_crm_state()
        payload = state.get("last_auto_splitter_payload")
        timestamp = state.get("last_run_timestamp")
    if not isinstance(payload, dict) or not payload.get("success") or not payload.get("dry_run"):
        return None
    if not _crm_auto_splitter_payload_matches_request(payload, order_target, tab_count, divisions, minimum_tabs):
        return None
    try:
        completed_at = datetime.fromisoformat(str(timestamp))
    except Exception:
        return None
    age_seconds = (datetime.now() - completed_at).total_seconds()
    if age_seconds < 0 or age_seconds > CRM_AUTO_SPLITTER_PREFLIGHT_REUSE_SECONDS:
        return None
    reused = dict(payload)
    reused["preflight_reused"] = True
    reused["preflight_reused_age_seconds"] = round(age_seconds, 1)
    return reused


def _normalize_crm_auto_splitter_history(rows):
    cleaned = []
    seen = set()
    for entry in rows or []:
        if not isinstance(entry, dict):
            continue
        row = dict(entry)
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        report = row.get("report") if isinstance(row.get("report"), dict) else payload.get("report") if isinstance(payload.get("report"), dict) else {}
        row["automation_key"] = "auto_splitter"
        row["automation_label"] = "Auto Splitter"
        row["success"] = bool(row.get("success"))
        row["dry_run"] = bool(row.get("dry_run") if row.get("dry_run") is not None else payload.get("dry_run"))
        row["duration_seconds"] = _normalize_duration_seconds(row.get("duration_seconds") if row.get("duration_seconds") is not None else payload.get("duration_seconds"))
        row["message"] = str(row.get("message") or payload.get("message") or "")
        row["order_ids"] = _extract_crm_order_ids({"order_ids": row.get("order_ids") or payload.get("new_order_ids")})
        target_order_id = _normalize_crm_single_order_id(row.get("target_order_id") or payload.get("target_order_id"))
        if target_order_id and target_order_id not in row["order_ids"]:
            row["order_ids"] = [target_order_id] + row["order_ids"]
        row["order_count"] = len(row["order_ids"])
        row["expected_tab_count"] = int(_safe_float(row.get("expected_tab_count") or payload.get("expected_tab_count") or report.get("expected_tab_count"), 0))
        row["divisions"] = int(_safe_float(row.get("divisions") or payload.get("divisions") or report.get("divisions"), 0))
        row["parallel_workers"] = _normalize_crm_positive_int(row.get("parallel_workers") or payload.get("parallel_workers") or report.get("parallel_workers"), default=1, minimum=1, maximum=CRM_SHARED_MAX_PARALLEL_WORKERS)
        row["report"] = report
        key = (
            str(row.get("timestamp") or ""),
            row["message"],
            tuple(row["order_ids"]),
            bool(row["dry_run"]),
        )
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(row)
        if len(cleaned) >= 20:
            break
    return cleaned


def _crm_auto_splitter_history_entry(timestamp, ok, message, payload, duration_seconds, order_ids, dry_run):
    payload = payload if isinstance(payload, dict) else {}
    report = payload.get("report") if isinstance(payload.get("report"), dict) else {}
    entry = {
        "timestamp": timestamp,
        "automation_key": "auto_splitter",
        "automation_label": "Auto Splitter",
        "success": bool(ok),
        "order_count": len(order_ids),
        "order_ids": order_ids,
        "duration_seconds": duration_seconds,
        "message": str(message),
        "dry_run": bool(dry_run),
        "expected_tab_count": payload.get("expected_tab_count") or report.get("expected_tab_count"),
        "divisions": payload.get("divisions") or report.get("divisions"),
        "parallel_workers": payload.get("parallel_workers") or report.get("parallel_workers"),
        "report": report,
    }
    return _normalize_crm_auto_splitter_history([entry])[0]


def _execute_crm_auto_splitter_worker(order_target, tab_count, divisions, minimum_tabs=10, dry_run=True, parallel_workers=1, show_terminal=None):
    order_id, order_url = _normalize_crm_auto_split_target(order_target)
    normalized_parallel_workers = _normalize_crm_positive_int(parallel_workers, default=1, minimum=1, maximum=CRM_SHARED_MAX_PARALLEL_WORKERS)
    if divisions not in (None, ""):
        normalized_parallel_workers = min(normalized_parallel_workers, max(1, int(divisions or 1)))
    if dry_run:
        normalized_parallel_workers = 1
    args = [
        "--action",
        "split_order",
        "--minimum-tabs",
        str(minimum_tabs),
        "--parallel-workers",
        str(normalized_parallel_workers),
    ]
    if tab_count not in (None, ""):
        args.extend(["--tab-count", str(tab_count)])
    if divisions not in (None, ""):
        args.extend(["--divisions", str(divisions)])
    if order_url:
        args.extend(["--order-url", order_url])
    elif order_id:
        args.extend(["--order-id", order_id])
    else:
        return False, "Enter a valid order number or CRM order link.", {"success": False, "message": "Enter a valid order number or CRM order link."}
    if dry_run:
        args.append("--dry-run")
    else:
        args.append("--real")
        if tab_count not in (None, "") and divisions not in (None, ""):
            for resume_order_id in _crm_auto_splitter_recovery_order_ids(order_id, tab_count, divisions):
                args.extend(["--resume-existing-order-id", resume_order_id])
    ok, message, payload = _run_script(
        CRM_AUTO_SPLITTER_SCRIPT,
        args,
        "CRMAutoSplitter",
        timeout=max(1800, CRM_ACTION_TIMEOUT * 120),
        show_terminal=bool(dry_run) if show_terminal is None else bool(show_terminal),
    )
    if not isinstance(payload, dict):
        payload = {"success": bool(ok), "message": str(message)}
    payload.setdefault("action", "split_order")
    payload.setdefault("dry_run", bool(dry_run))
    payload.setdefault("target_order_id", order_id)
    payload.setdefault("order_url", order_url)
    if tab_count not in (None, ""):
        payload.setdefault("expected_tab_count", tab_count)
    if divisions not in (None, ""):
        payload.setdefault("divisions", divisions)
    payload.setdefault("parallel_workers", normalized_parallel_workers)
    return ok, message, payload


def _persist_crm_auto_splitter_run_result(ok, message, payload, dry_run=True):
    ensure_crm_state_file()
    payload = payload if isinstance(payload, dict) else {"success": bool(ok), "message": str(message)}
    timestamp = datetime.now().isoformat()
    target_order_id = _normalize_crm_single_order_id(payload.get("target_order_id"))
    new_order_ids = [
        _normalize_crm_single_order_id(value)
        for value in (payload.get("new_order_ids") if isinstance(payload.get("new_order_ids"), list) else [])
    ]
    new_order_ids = [value for value in new_order_ids if value]
    order_ids = ([target_order_id] if target_order_id else []) + new_order_ids
    duration_seconds = _normalize_duration_seconds(payload.get("duration_seconds"))
    if duration_seconds is None:
        duration_seconds = _runtime_duration_seconds(_crm_auto_splitter_runtime_snapshot())

    with crm_state_lock:
        state = load_crm_state()
        previous_recovery_payload = state.get("last_auto_splitter_recovery_payload") or state.get("last_auto_splitter_payload")
        previous_recovery_usable = (
            isinstance(previous_recovery_payload, dict)
            and _crm_auto_splitter_recovery_payload_is_usable(
                previous_recovery_payload,
                previous_recovery_payload.get("target_order_id"),
                previous_recovery_payload.get("expected_tab_count"),
                previous_recovery_payload.get("divisions"),
            )
        )
        state["last_run_timestamp"] = timestamp
        state["last_run_success"] = bool(ok)
        state["last_run_message"] = str(message)
        state["last_run_duration_seconds"] = duration_seconds
        state["last_order_count"] = len(order_ids)
        state["last_order_ids"] = order_ids
        state["last_auto_splitter_payload"] = payload
        if ok and not dry_run:
            state.pop("last_auto_splitter_recovery_payload", None)
        elif (
            not ok
            and not dry_run
            and _crm_auto_splitter_recovery_payload_is_usable(payload, target_order_id, payload.get("expected_tab_count"), payload.get("divisions"))
        ):
            state["last_auto_splitter_recovery_payload"] = payload
        elif dry_run and previous_recovery_usable:
            state["last_auto_splitter_recovery_payload"] = previous_recovery_payload
        state["total_runs"] = max(0, int(_safe_float(state.get("total_runs"), 0))) + 1
        if ok and not dry_run:
            state["total_orders_processed"] = (
                max(0, int(_safe_float(state.get("total_orders_processed"), 0))) + len(new_order_ids)
            )
        entry = _crm_auto_splitter_history_entry(timestamp, ok, message, payload, duration_seconds, order_ids, dry_run)
        history = state.get("auto_splitter_run_history") if isinstance(state.get("auto_splitter_run_history"), list) else []
        state["auto_splitter_run_history"] = _normalize_crm_auto_splitter_history([entry] + history[:19])
        save_crm_state(state)

    _audit_result("crm.auto_splitter", ok, message)
    return state


def _crm_auto_splitter_run_thread(order_target, tab_count, divisions, minimum_tabs=10, dry_run=True, parallel_workers=1):
    ok = False
    message = "Auto Splitter did not run."
    payload = {"success": False, "message": message}
    try:
        if dry_run:
            ok, message, payload = _execute_crm_auto_splitter_worker(
                order_target,
                tab_count,
                divisions,
                minimum_tabs=minimum_tabs,
                dry_run=True,
                parallel_workers=1,
            )
        else:
            dry_payload = _crm_auto_splitter_recent_dry_run_payload(order_target, tab_count, divisions, minimum_tabs)
            if dry_payload:
                dry_ok = True
                dry_message = "Reused recent matching Auto Splitter dry run preflight."
                with crm_auto_splitter_runtime_lock:
                    crm_auto_splitter_runtime["lastMessage"] = dry_message
            else:
                with crm_auto_splitter_runtime_lock:
                    crm_auto_splitter_runtime["lastMessage"] = "Running Auto Splitter dry run preflight..."
                dry_ok, dry_message, dry_payload = _execute_crm_auto_splitter_worker(
                    order_target,
                    tab_count,
                    divisions,
                    minimum_tabs=minimum_tabs,
                    dry_run=True,
                    parallel_workers=1,
                    show_terminal=False,
                )
            if not dry_ok:
                ok = False
                message = f"Auto Splitter dry run failed: {dry_message}"
                payload = dry_payload if isinstance(dry_payload, dict) else {}
                payload.update({
                    "success": False,
                    "message": message,
                    "dry_run": False,
                    "preflight_dry_run": dry_payload,
                })
            else:
                if isinstance(dry_payload, dict):
                    tab_count = dry_payload.get("expected_tab_count") or dry_payload.get("detected_tab_count") or tab_count
                    divisions = dry_payload.get("divisions") or divisions
                    with crm_auto_splitter_runtime_lock:
                        crm_auto_splitter_runtime["tabCount"] = tab_count
                        crm_auto_splitter_runtime["divisions"] = divisions
                if tab_count in (None, "") or divisions in (None, ""):
                    ok = False
                    message = "Auto Splitter dry run passed but did not return computed tab and division counts."
                    payload = {
                        "success": False,
                        "message": message,
                        "dry_run": False,
                        "preflight_dry_run": dry_payload,
                    }
                    return
                with crm_auto_splitter_runtime_lock:
                    crm_auto_splitter_runtime["lastMessage"] = (
                        "Recent dry run reused. Starting live Auto Splitter run..."
                        if isinstance(dry_payload, dict) and dry_payload.get("preflight_reused")
                        else "Dry run passed. Starting live Auto Splitter run..."
                    )
                ok, message, payload = _execute_crm_auto_splitter_worker(
                    order_target,
                    tab_count,
                    divisions,
                    minimum_tabs=minimum_tabs,
                    dry_run=False,
                    parallel_workers=parallel_workers,
                )
                if isinstance(payload, dict):
                    payload["preflight_dry_run"] = dry_payload
                    if isinstance(dry_payload, dict) and dry_payload.get("preflight_reused"):
                        payload["preflight_reused"] = True
    except Exception as e:
        logger.exception("CRM Auto Splitter run failed unexpectedly")
        ok = False
        message = str(e)
        payload = {"success": False, "message": message, "error_type": type(e).__name__}
    finally:
        _persist_crm_auto_splitter_run_result(ok, message, payload, dry_run=dry_run)
        _finish_crm_auto_splitter_runtime(ok, message, payload, release_lock=True)


def start_crm_auto_splitter_run(order_target=None, tab_count=None, divisions=None, minimum_tabs=10, dry_run=True, parallel_workers=None):
    try:
        if minimum_tabs is None or str(minimum_tabs).strip() == "":
            minimum_tabs = 10
        tab_count_supplied = tab_count not in (None, "")
        divisions_supplied = divisions not in (None, "")
        normalized_tab_count = (
            _normalize_crm_auto_split_count(tab_count, field_label="Tab count", minimum=1, maximum=1000)
            if tab_count_supplied
            else None
        )
        normalized_divisions = (
            _normalize_crm_auto_split_count(divisions, field_label="Divisions", minimum=2, maximum=100)
            if divisions_supplied
            else None
        )
        normalized_minimum_tabs = _normalize_crm_auto_split_count(minimum_tabs, field_label="Minimum tabs", minimum=1, maximum=1000)
        parallel_workers_supplied = _crm_address_value_supplied(parallel_workers)
        saved_parallel_workers = _saved_crm_automation_parallel_workers(default=1)
        normalized_parallel_workers = _normalize_crm_positive_int(
            parallel_workers,
            default=saved_parallel_workers,
            minimum=1,
            maximum=CRM_SHARED_MAX_PARALLEL_WORKERS,
        )
        if normalized_divisions is not None:
            normalized_parallel_workers = min(normalized_parallel_workers, normalized_divisions)
        if dry_run:
            normalized_parallel_workers = 1
    except ValueError as e:
        return False, str(e)
    order_id, order_url = _normalize_crm_auto_split_target(order_target)
    if not order_id and not order_url:
        return False, "Enter a valid order number or CRM order link."
    if normalized_tab_count is not None and normalized_tab_count <= normalized_minimum_tabs:
        return False, f"Auto Splitter only splits orders with more than {normalized_minimum_tabs} tabs."
    if not crm_lock.acquire(blocking=False):
        return False, "A CRM automation run is already in progress."

    if parallel_workers_supplied and not dry_run:
        update_crm_address_preferences(parallel_workers=parallel_workers)

    _start_crm_auto_splitter_runtime(
        order_target,
        normalized_tab_count,
        normalized_divisions,
        normalized_minimum_tabs,
        dry_run=dry_run,
        parallel_workers=normalized_parallel_workers,
    )
    threading.Thread(
        target=_crm_auto_splitter_run_thread,
        args=(order_target, normalized_tab_count, normalized_divisions, normalized_minimum_tabs, bool(dry_run), normalized_parallel_workers),
        daemon=True,
    ).start()
    if dry_run:
        return True, f"Auto Splitter dry run started for order {order_id or order_url}."
    return True, f"Auto Splitter started for order {order_id or order_url}. Dry run preflight will run before live split."


def get_crm_auto_splitter_status_payload():
    runtime = _crm_auto_splitter_runtime_snapshot()
    ensure_crm_state_file()
    with crm_state_lock:
        state = load_crm_state()
    return {
        "success": True,
        "running": bool(runtime.get("running")),
        "runtime": runtime,
        "state": state,
    }


def _default_crm_mass_emailer_state():
    return {
        "last_run_timestamp": None,
        "last_run_success": None,
        "last_run_message": None,
        "last_run_duration_seconds": None,
        "last_action": "process_queue",
        "last_order_count": 0,
        "last_failure_count": 0,
        "last_skipped_count": 0,
        "last_order_ids": [],
        "last_payload": None,
        "total_runs": 0,
        "total_orders_processed": 0,
        "run_history": [],
    }


def ensure_crm_mass_emailer_state_file():
    if os.path.exists(CRM_MASS_EMAILER_STATE_FILE):
        return
    _write_json_file_atomic(CRM_MASS_EMAILER_STATE_FILE, _default_crm_mass_emailer_state())


def _normalize_crm_mass_emailer_action(value):
    key = str(value or "").strip().lower()
    if key in {"scan", "scan_sheet"}:
        return "scan_sheet"
    if key in {"process_order", "order"}:
        return "process_order"
    return "process_queue"


def _crm_mass_emailer_sheet_function_label(row):
    if not isinstance(row, dict):
        return ""
    for key in ("function_label", "sheet_function", "issue_type"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    process_key = str(row.get("process") or row.get("outcome") or "").strip()
    if not process_key:
        return ""
    labels = {
        "copyright_cancel": getattr(config_module, "COPYRIGHT_CANCEL_ISSUE_TYPE", "Copyright - Cancel"),
        "content_violation_cancel": getattr(config_module, "CONTENT_VIOLATION_CANCEL_ISSUE_TYPE", "Content Violation - Cancel"),
        "complicated_emb_to_hdd": getattr(config_module, "COMPLICATED_EMB_ISSUE_TYPE", "Complicated EMB"),
        "oversize_emb_to_hdd": getattr(config_module, "OVERSIZE_EMB_TO_HDD_ISSUE_TYPE", "Oversize EMB to HDD"),
        "copyright_reachout": getattr(config_module, "COPYRIGHT_REACHOUT_ISSUE_TYPE", "Copyright - Reachout"),
        "auto_splitter": getattr(config_module, "AUTO_SPLITTER_ISSUE_TYPE", "Auto Splitter"),
        "manual_stock_order": getattr(config_module, "MANUAL_STOCK_ORDER_ISSUE_TYPE", "Manual Stock Order"),
    }
    label = labels.get(process_key)
    if label:
        return str(label)
    return process_key.replace("_", " ").strip().title()


def _crm_mass_emailer_order_ids_from_payload(payload):
    if not isinstance(payload, dict):
        return []
    rows = []
    for key in ("processed", "failures", "eligible_rows", "order_details"):
        value = payload.get(key)
        if isinstance(value, list):
            rows.extend(value)
    rows.extend(payload.get("order_ids") if isinstance(payload.get("order_ids"), list) else [])
    if payload.get("order_id"):
        rows.append(payload.get("order_id"))
    cleaned = []
    seen = set()
    for item in rows:
        raw = item.get("order_id") if isinstance(item, dict) else item
        text = "".join(ch for ch in str(raw or "") if ch.isdigit())
        if len(text) != 7 or text in seen:
            continue
        seen.add(text)
        cleaned.append(text)
    return cleaned


def _crm_mass_emailer_order_details_from_payload(payload):
    if not isinstance(payload, dict):
        return []

    def _row_message(row):
        for key in ("message", "error", "outcome"):
            value = row.get(key)
            if value:
                return str(value)
        for key in ("auto_splitter", "shipping_bypasser", "preflight_dry_run"):
            value = row.get(key)
            if isinstance(value, dict) and value.get("message"):
                return str(value.get("message"))
        return ""

    details = []
    seen = set()

    def _append(row, success, status, default_message=""):
        if not isinstance(row, dict):
            return
        order_id = _extract_crm_order_ids({"order_ids": [row.get("order_id")]})
        if not order_id:
            return
        order_id = order_id[0]
        message = _row_message(row) or default_message
        if order_id in seen:
            if not success:
                for detail in details:
                    if detail.get("order_id") == order_id:
                        detail["success"] = False
                        detail["status"] = str(status)
                        detail["message"] = str(message or detail.get("message") or "")
                        break
            return
        seen.add(order_id)
        details.append(
            {
                "order_id": order_id,
                "success": bool(success),
                "status": str(status),
                "outcome": str(row.get("outcome") or row.get("process") or row.get("issue_type") or ""),
                "function_label": _crm_mass_emailer_sheet_function_label(row),
                "message": str(message or ""),
                "duration_seconds": _normalize_duration_seconds(row.get("duration_seconds")),
            }
        )

    for row in (payload.get("processed") if isinstance(payload.get("processed"), list) else []):
        _append(row, True, "Success", "Completed successfully.")
    for row in (payload.get("failures") if isinstance(payload.get("failures"), list) else []):
        _append(row, False, "Needs attention")
    for row in (payload.get("eligible_rows") if isinstance(payload.get("eligible_rows"), list) else []):
        has_error = bool(str(row.get("error") or "").strip()) if isinstance(row, dict) else False
        _append(row, not has_error, "Needs attention" if has_error else "Ready")
    for row in (payload.get("order_details") if isinstance(payload.get("order_details"), list) else []):
        if not isinstance(row, dict):
            continue
        success = row.get("success")
        if success is None:
            success = str(row.get("status") or "").strip().lower() not in {"needs attention", "failed", "error"}
        _append(row, bool(success), row.get("status") or ("Success" if success else "Needs attention"))

    return details


def _crm_mass_emailer_counts(payload):
    payload = payload if isinstance(payload, dict) else {}
    processed = payload.get("processed") if isinstance(payload.get("processed"), list) else []
    failures = payload.get("failures") if isinstance(payload.get("failures"), list) else []
    eligible = payload.get("eligible_rows") if isinstance(payload.get("eligible_rows"), list) else []
    skipped = payload.get("skipped_rows") if isinstance(payload.get("skipped_rows"), list) else []
    action = _normalize_crm_mass_emailer_action(payload.get("action"))
    if action == "scan_sheet":
        order_count = len(eligible)
    elif action == "process_order":
        order_count = 1 if payload.get("success") else 0
    else:
        order_count = len(processed)
    return {
        "order_count": max(0, int(_safe_float(payload.get("order_count"), order_count))),
        "failure_count": len(failures),
        "skipped_count": len(skipped),
        "eligible_count": len(eligible) if eligible else len(processed) + len(failures),
        "order_ids": _crm_mass_emailer_order_ids_from_payload(payload),
    }


def _crm_mass_emailer_payload_snapshot(payload):
    payload = payload if isinstance(payload, dict) else {}
    clean = dict(payload)
    clean.pop("state", None)
    try:
        return json.loads(json.dumps(clean))
    except Exception:
        return {
            "success": bool(clean.get("success")),
            "message": str(clean.get("message") or ""),
            "action": _normalize_crm_mass_emailer_action(clean.get("action")),
        }


def _normalize_crm_mass_emailer_history(rows):
    cleaned = []
    source = rows if isinstance(rows, list) else []
    for item in source[:20]:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        row["action"] = _normalize_crm_mass_emailer_action(row.get("action"))
        row["duration_seconds"] = _normalize_duration_seconds(row.get("duration_seconds"))
        row["order_count"] = max(0, int(_safe_float(row.get("order_count"), 0)))
        row["failure_count"] = max(0, int(_safe_float(row.get("failure_count"), 0)))
        row["skipped_count"] = max(0, int(_safe_float(row.get("skipped_count"), 0)))
        row["order_details"] = _crm_mass_emailer_order_details_from_payload(row)
        row["order_ids"] = _crm_mass_emailer_order_ids_from_payload(row)
        row["success"] = bool(row.get("success"))
        row["message"] = str(row.get("message") or "")
        cleaned.append(row)
    return cleaned


def load_crm_mass_emailer_state():
    state = _default_crm_mass_emailer_state()
    if os.path.exists(CRM_MASS_EMAILER_STATE_FILE):
        try:
            with open(CRM_MASS_EMAILER_STATE_FILE, "r", encoding="utf-8-sig") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                state.update(loaded)
        except Exception as e:
            logger.warning("Could not read %s: %s", CRM_MASS_EMAILER_STATE_FILE, e)
    state["last_run_duration_seconds"] = _normalize_duration_seconds(state.get("last_run_duration_seconds"))
    state["last_action"] = _normalize_crm_mass_emailer_action(state.get("last_action"))
    state["last_order_count"] = max(0, int(_safe_float(state.get("last_order_count"), 0)))
    state["last_failure_count"] = max(0, int(_safe_float(state.get("last_failure_count"), 0)))
    state["last_skipped_count"] = max(0, int(_safe_float(state.get("last_skipped_count"), 0)))
    state["last_order_ids"] = _extract_crm_order_ids({"order_ids": state.get("last_order_ids")})
    state["total_runs"] = max(0, int(_safe_float(state.get("total_runs"), 0)))
    state["total_orders_processed"] = max(0, int(_safe_float(state.get("total_orders_processed"), 0)))
    state["run_history"] = _normalize_crm_mass_emailer_history(state.get("run_history"))
    return state


def save_crm_mass_emailer_state(state):
    _write_json_file_atomic(CRM_MASS_EMAILER_STATE_FILE, state)


def _crm_mass_emailer_runtime_snapshot():
    with crm_mass_emailer_runtime_lock:
        return dict(crm_mass_emailer_runtime)


def _start_crm_mass_emailer_runtime(action="process_queue", dry_run=True, limit=None, retry_errors=False):
    normalized_action = _normalize_crm_mass_emailer_action(action)
    with crm_mass_emailer_runtime_lock:
        crm_mass_emailer_runtime["running"] = True
        crm_mass_emailer_runtime["startedAt"] = datetime.now().isoformat()
        crm_mass_emailer_runtime["completedAt"] = None
        crm_mass_emailer_runtime["lastAction"] = normalized_action
        crm_mass_emailer_runtime["orderCount"] = 0
        crm_mass_emailer_runtime["failureCount"] = 0
        crm_mass_emailer_runtime["skippedCount"] = 0
        crm_mass_emailer_runtime["currentOrderIndex"] = 0
        crm_mass_emailer_runtime["totalOrderCount"] = 0
        crm_mass_emailer_runtime["currentStage"] = "queued"
        if normalized_action == "scan_sheet":
            message = "Sheets Scanner sheet scan queued."
        else:
            mode = "dry run" if dry_run else "live run"
            suffix = f" | Limit {int(limit)}" if int(_safe_float(limit, 0)) > 0 else ""
            retry = " | Retry errors" if retry_errors else ""
            message = f"Sheets Scanner {mode} queued{suffix}{retry}."
        crm_mass_emailer_runtime["lastMessage"] = message
        crm_mass_emailer_runtime["lastSuccess"] = None
        crm_mass_emailer_runtime["dryRun"] = bool(dry_run)
        crm_mass_emailer_runtime["payload"] = None


def _finish_crm_mass_emailer_runtime(ok, message, payload, release_lock=True):
    payload = payload if isinstance(payload, dict) else {"success": bool(ok), "message": str(message)}
    counts = _crm_mass_emailer_counts(payload)
    runtime_payload = _crm_mass_emailer_payload_snapshot(payload)
    with crm_mass_emailer_runtime_lock:
        crm_mass_emailer_runtime["running"] = False
        crm_mass_emailer_runtime["completedAt"] = datetime.now().isoformat()
        crm_mass_emailer_runtime["lastMessage"] = str(message)
        crm_mass_emailer_runtime["lastSuccess"] = bool(ok)
        crm_mass_emailer_runtime["lastAction"] = _normalize_crm_mass_emailer_action(payload.get("action") or crm_mass_emailer_runtime.get("lastAction"))
        crm_mass_emailer_runtime["orderCount"] = counts["order_count"]
        crm_mass_emailer_runtime["failureCount"] = counts["failure_count"]
        crm_mass_emailer_runtime["skippedCount"] = counts["skipped_count"]
        crm_mass_emailer_runtime["currentOrderIndex"] = counts["order_count"]
        crm_mass_emailer_runtime["totalOrderCount"] = counts["eligible_count"]
        crm_mass_emailer_runtime["currentStage"] = None
        crm_mass_emailer_runtime["payload"] = runtime_payload
    if release_lock and crm_lock.locked():
        crm_lock.release()


def _execute_crm_mass_emailer_worker(action="process_queue", dry_run=True, limit=None, retry_errors=False, show_terminal=None):
    normalized_action = _normalize_crm_mass_emailer_action(action)
    args = ["--action", normalized_action]
    if int(_safe_float(limit, 0)) > 0:
        args.extend(["--limit", str(int(_safe_float(limit, 0)))])
    if retry_errors:
        args.append("--retry-errors")
    if normalized_action != "scan_sheet":
        if dry_run:
            args.append("--dry-run")
        else:
            args.append("--real")
    ok, message, payload = _run_script(
        CRM_MASS_EMAILER_SCRIPT,
        args,
        "CRMMassEmailer",
        timeout=CRM_MASS_EMAILER_TIMEOUT_SECONDS,
        show_terminal=bool(show_terminal),
    )
    if not isinstance(payload, dict):
        payload = {"success": bool(ok), "message": str(message)}
    payload.setdefault("action", normalized_action)
    payload.setdefault("dry_run", bool(dry_run))
    return ok, message, payload


def _persist_crm_mass_emailer_run_result(ok, message, payload, dry_run=True):
    ensure_crm_mass_emailer_state_file()
    payload = payload if isinstance(payload, dict) else {"success": bool(ok), "message": str(message)}
    counts = _crm_mass_emailer_counts(payload)
    timestamp = datetime.now().isoformat()
    duration_seconds = payload.get("duration_seconds")
    if duration_seconds is None:
        duration_seconds = _runtime_duration_seconds(_crm_mass_emailer_runtime_snapshot())
    duration_seconds = _normalize_duration_seconds(duration_seconds)
    action = _normalize_crm_mass_emailer_action(payload.get("action"))
    entry = {
        "timestamp": timestamp,
        "success": bool(ok),
        "action": action,
        "dry_run": bool(dry_run),
        "order_count": counts["order_count"],
        "failure_count": counts["failure_count"],
        "skipped_count": counts["skipped_count"],
        "order_ids": counts["order_ids"],
        "order_details": _crm_mass_emailer_order_details_from_payload(payload),
        "duration_seconds": duration_seconds,
        "message": str(message),
    }
    with crm_mass_emailer_state_lock:
        state = load_crm_mass_emailer_state()
        state["last_run_timestamp"] = timestamp
        state["last_run_success"] = bool(ok)
        state["last_run_message"] = str(message)
        state["last_run_duration_seconds"] = duration_seconds
        state["last_action"] = action
        state["last_order_count"] = counts["order_count"]
        state["last_failure_count"] = counts["failure_count"]
        state["last_skipped_count"] = counts["skipped_count"]
        state["last_order_ids"] = counts["order_ids"]
        state["last_payload"] = _crm_mass_emailer_payload_snapshot(payload)
        state["total_runs"] = max(0, int(_safe_float(state.get("total_runs"), 0))) + 1
        if action != "scan_sheet" and not dry_run:
            state["total_orders_processed"] = max(0, int(_safe_float(state.get("total_orders_processed"), 0))) + counts["order_count"]
        history = state.get("run_history") if isinstance(state.get("run_history"), list) else []
        state["run_history"] = _normalize_crm_mass_emailer_history([entry] + history[:19])
        save_crm_mass_emailer_state(state)
    _audit_result("crm.mass_emailer", ok, message)
    return state


def _crm_mass_emailer_run_thread(action="process_queue", dry_run=True, limit=None, retry_errors=False):
    ok = False
    message = "Sheets Scanner did not run."
    payload = {"success": False, "message": message}
    try:
        ok, message, payload = _execute_crm_mass_emailer_worker(
            action=action,
            dry_run=dry_run,
            limit=limit,
            retry_errors=retry_errors,
            show_terminal=False,
        )
    except Exception as e:
        logger.exception("Sheets Scanner run failed unexpectedly")
        ok = False
        message = str(e)
        payload = {"success": False, "message": message, "error_type": type(e).__name__, "action": action}
    finally:
        state = _persist_crm_mass_emailer_run_result(ok, message, payload, dry_run=dry_run)
        payload = payload if isinstance(payload, dict) else {"success": bool(ok), "message": str(message)}
        payload["state"] = state
        _finish_crm_mass_emailer_runtime(ok, message, payload, release_lock=True)


def start_crm_mass_emailer_run(action="process_queue", dry_run=True, limit=None, retry_errors=False):
    normalized_action = _normalize_crm_mass_emailer_action(action)
    if not crm_lock.acquire(blocking=False):
        return False, "A CRM automation run is already in progress."
    _start_crm_mass_emailer_runtime(normalized_action, dry_run=dry_run, limit=limit, retry_errors=retry_errors)
    threading.Thread(
        target=_crm_mass_emailer_run_thread,
        args=(normalized_action, bool(dry_run), limit, bool(retry_errors)),
        daemon=True,
    ).start()
    if normalized_action == "scan_sheet":
        return True, "Sheets Scanner sheet scan started."
    mode = "dry run" if dry_run else "live run"
    return True, f"Sheets Scanner {mode} started."


def get_crm_mass_emailer_status_payload():
    ensure_crm_mass_emailer_state_file()
    runtime = _crm_mass_emailer_runtime_snapshot()
    with crm_mass_emailer_state_lock:
        state = load_crm_mass_emailer_state()
    return {
        "success": True,
        "running": bool(runtime.get("running")),
        "runtime": runtime,
        "state": state,
    }


def clear_crm_mass_emailer_history():
    ensure_crm_mass_emailer_state_file()
    with crm_mass_emailer_state_lock:
        state = load_crm_mass_emailer_state()
        state["run_history"] = []
        state["total_runs"] = 0
        state["total_orders_processed"] = 0
        save_crm_mass_emailer_state(state)
    return True, "Sheets Scanner history cleared."


def update_crm_processing_preferences(stock_unlocker_enabled=None, mass_emailer_enabled=None, address_validator_enabled=None, product_separator_enabled=None, order_goods_enabled=None, shipping_bypasser_enabled=None, push_back_enabled=None, processing_filter=None):
    ensure_crm_processing_state_file()
    unlock_supplied = _crm_processing_value_supplied(stock_unlocker_enabled)
    address_supplied = _crm_processing_value_supplied(address_validator_enabled)
    separator_supplied = _crm_processing_value_supplied(product_separator_enabled)
    order_goods_supplied = _crm_processing_value_supplied(order_goods_enabled)
    shipping_bypasser_supplied = _crm_processing_value_supplied(shipping_bypasser_enabled)
    push_back_supplied = _crm_processing_value_supplied(push_back_enabled)
    filter_supplied = _crm_processing_value_supplied(processing_filter)

    with crm_processing_state_lock:
        state = load_crm_processing_state()
        target_filter = _normalize_crm_shipping_filter(processing_filter) if filter_supplied else state.get("processing_filter")
        mode_preferences = state.get("mode_preferences") if isinstance(state.get("mode_preferences"), dict) else {}
        target_preferences = dict(
            mode_preferences.get(target_filter)
            or _default_crm_processing_mode_preferences(target_filter)
        )
        if unlock_supplied:
            target_preferences["stock_unlocker_enabled"] = _normalize_crm_processing_enabled(
                stock_unlocker_enabled,
                default=target_preferences.get("stock_unlocker_enabled"),
            )
        if address_supplied:
            target_preferences["address_validator_enabled"] = _normalize_crm_processing_enabled(
                address_validator_enabled,
                default=target_preferences.get("address_validator_enabled"),
            )
        if separator_supplied:
            target_preferences["product_separator_enabled"] = _normalize_crm_processing_enabled(
                product_separator_enabled,
                default=target_preferences.get("product_separator_enabled"),
            )
        if order_goods_supplied:
            target_preferences["order_goods_enabled"] = _normalize_crm_processing_enabled(
                order_goods_enabled,
                default=target_preferences.get("order_goods_enabled"),
            )
        if shipping_bypasser_supplied:
            target_preferences["shipping_bypasser_enabled"] = _normalize_crm_processing_enabled(
                shipping_bypasser_enabled,
                default=target_preferences.get("shipping_bypasser_enabled"),
            )
        if push_back_supplied:
            target_preferences["push_back_enabled"] = _normalize_crm_processing_enabled(
                push_back_enabled,
                default=target_preferences.get("push_back_enabled"),
            )
        mode_preferences[target_filter] = _sanitize_crm_processing_mode_preferences(target_filter, target_preferences)
        state["mode_preferences"] = _normalize_crm_processing_mode_preferences(mode_preferences)
        _apply_crm_processing_mode_preferences_to_state(state, target_filter)
        save_crm_processing_state(state)

    with crm_processing_runtime_lock:
        crm_processing_runtime["stateSnapshot"] = state

    selected_labels = [_crm_processing_step_label(step) for step in _crm_processing_selected_steps_from_state(state)]
    selection_text = ", ".join(selected_labels) if selected_labels else "none selected"
    return True, f"Automate Processing saved for {_crm_shipping_filter_label(state.get('processing_filter'))}: {selection_text}.", state


def _run_crm_processing_step(step_key, processing_filter, processing_state=None):
    if step_key == "product_separator":
        normalized_filter = _normalize_crm_shipping_filter(processing_filter)
        list_url = _crm_processing_address_list_url_for_filter(normalized_filter) if normalized_filter == "813" else None
        parallel_workers = _saved_crm_automation_parallel_workers(default=1)
        _start_crm_product_separator_runtime(
            dry_run=False,
            list_mode=normalized_filter,
            list_url=list_url,
            parallel_workers=parallel_workers,
        )
        ok = False
        message = "Product Separator did not run."
        payload = {"success": False, "message": message}
        try:
            ok, message, payload = _execute_crm_product_separator_worker(
                dry_run=False,
                list_mode=normalized_filter,
                list_url=list_url,
                parallel_workers=parallel_workers,
                visible=False,
                show_terminal=False,
            )
        except Exception as e:
            logger.exception("Automate Processing Product Separator step failed unexpectedly")
            ok = False
            message = str(e)
            payload = {"success": False, "message": message, "error_type": type(e).__name__}
        state = _persist_crm_product_separator_run_result(ok, message, payload, dry_run=False)
        payload = payload if isinstance(payload, dict) else {"success": bool(ok), "message": str(message)}
        payload["state"] = state
        _finish_crm_product_separator_runtime(ok, message, payload, release_lock=False)
        return {
            "key": step_key,
            "label": _crm_processing_step_label(step_key),
            "success": bool(ok),
            "stage_timings": _normalize_stage_timings(payload.get("stage_timings") if isinstance(payload, dict) else []),
            "message": str(message),
        }

    if step_key == "order_goods":
        normalized_filter = _normalize_crm_shipping_filter(processing_filter)
        list_url = _crm_processing_813_list_url_for_step(step_key) if normalized_filter == "813" else None
        if normalized_filter == "813" and not list_url:
            message = f"{_crm_processing_813_url_config_key_for_step(step_key)} is empty in config.py."
            return {
                "key": step_key,
                "label": _crm_processing_step_label(step_key),
                "success": False,
                "message": message,
            }
        parallel_workers = _saved_crm_automation_parallel_workers(default=1)
        _start_crm_order_goods_runtime(
            dry_run=False,
            batch_size=None,
            parallel_workers=parallel_workers,
            list_url=list_url,
        )
        ok = False
        message = "Rush Order Goods did not run."
        payload = {"success": False, "message": message}
        try:
            ok, message, payload = _execute_crm_order_goods_worker(
                dry_run=False,
                batch_size=None,
                parallel_workers=parallel_workers,
                list_url=list_url,
                visible=False,
                show_terminal=False,
            )
        except Exception as e:
            logger.exception("Automate Processing Rush Order Goods step failed unexpectedly")
            ok = False
            message = str(e)
            payload = {"success": False, "message": message, "error_type": type(e).__name__}
        state = _persist_crm_order_goods_run_result(ok, message, payload, dry_run=False)
        payload = payload if isinstance(payload, dict) else {"success": bool(ok), "message": str(message)}
        payload["state"] = state
        _finish_crm_order_goods_runtime(ok, message, payload, release_lock=False)
        return {
            "key": step_key,
            "label": _crm_processing_step_label(step_key),
            "success": bool(ok),
            "stage_timings": _normalize_stage_timings(payload.get("stage_timings") if isinstance(payload, dict) else []),
            "message": str(message),
        }

    if step_key == "shipping_bypasser":
        normalized_filter = _normalize_crm_shipping_filter(processing_filter)
        list_url = _crm_processing_813_list_url_for_step(step_key) if normalized_filter == "813" else None
        if normalized_filter == "813" and not list_url:
            message = f"{_crm_processing_813_url_config_key_for_step(step_key)} is empty in config.py."
            return {
                "key": step_key,
                "label": _crm_processing_step_label(step_key),
                "success": False,
                "message": message,
            }
        if _automation_stop_is_blocking():
            ok = False
            message = _force_stop_message("Shipping Bypasser")
            payload = {"success": False, "message": message, "stopped": True}
        else:
            _start_crm_shipping_bypasser_runtime(
                dry_run=False,
                batch_size=None,
                list_url=list_url,
            )
            ok = False
            message = "Shipping Bypasser did not run."
            payload = {"success": False, "message": message}
            try:
                ok, message, payload = _execute_crm_shipping_bypasser_worker(
                    dry_run=False,
                    batch_size=None,
                    list_url=list_url,
                    visible=False,
                    show_terminal=False,
                )
            except Exception as e:
                logger.exception("Automate Processing Shipping Bypasser step failed unexpectedly")
                ok = False
                message = str(e)
                payload = {"success": False, "message": message, "error_type": type(e).__name__}
            bypass_state = _persist_crm_shipping_bypasser_run_result(ok, message, payload, dry_run=False)
            payload = payload if isinstance(payload, dict) else {"success": bool(ok), "message": str(message)}
            payload["state"] = bypass_state
            _notify_shipping_bypasser_problem_orders(payload, message)
            _finish_crm_shipping_bypasser_runtime(ok, message, payload, release_lock=False)
        return {
            "key": step_key,
            "label": _crm_processing_step_label(step_key),
            "success": bool(ok),
            "stage_timings": _normalize_stage_timings(payload.get("stage_timings") if isinstance(payload, dict) else []),
            "message": str(message),
        }

    if step_key == "push_back":
        normalized_filter = _normalize_crm_shipping_filter(processing_filter)
        list_url = _crm_processing_push_back_list_url_for_filter(normalized_filter)
        if normalized_filter not in {"rush", "813"}:
            message = "Push Back is only available for Rush and 813 modes."
            return {
                "key": step_key,
                "label": _crm_processing_step_label(step_key),
                "success": False,
                "message": message,
            }
        if not list_url:
            config_key = "CRM_PUSH_BACK_813_URL" if normalized_filter == "813" else "CRM_PUSH_BACK_RUSH_URL"
            message = f"{config_key} is empty in config.py."
            return {
                "key": step_key,
                "label": _crm_processing_step_label(step_key),
                "success": False,
                "message": message,
            }
        if _automation_stop_is_blocking():
            ok = False
            message = _force_stop_message("Push Back")
            payload = {"success": False, "message": message, "stopped": True}
        else:
            parallel_workers = _saved_crm_automation_parallel_workers(default=1)
            _start_crm_push_back_runtime(
                dry_run=False,
                batch_size=None,
                processing_filter=normalized_filter,
                list_url=list_url,
                parallel_workers=parallel_workers,
            )
            ok = False
            message = "Push Back did not run."
            payload = {"success": False, "message": message}
            try:
                ok, message, payload = _execute_crm_push_back_worker(
                    dry_run=False,
                    batch_size=None,
                    processing_filter=normalized_filter,
                    list_url=list_url,
                    visible=False,
                    show_terminal=False,
                    parallel_workers=parallel_workers,
                )
            except Exception as e:
                logger.exception("Automate Processing Push Back step failed unexpectedly")
                ok = False
                message = str(e)
                payload = {"success": False, "message": message, "error_type": type(e).__name__}
            push_back_state = _persist_crm_push_back_run_result(ok, message, payload, dry_run=False)
            payload = payload if isinstance(payload, dict) else {"success": bool(ok), "message": str(message)}
            payload["state"] = push_back_state
            _finish_crm_push_back_runtime(ok, message, payload, release_lock=False)
        return {
            "key": step_key,
            "label": _crm_processing_step_label(step_key),
            "success": bool(ok),
            "stage_timings": _normalize_stage_timings(payload.get("stage_timings") if isinstance(payload, dict) else []),
            "message": str(message),
        }

    if step_key == "stock_unlocker":
        _start_crm_runtime(dry_run=False, last_message="Stock Unlocker started by Automate Processing.")
        ok = False
        message = "Stock Unlocker did not run."
        payload = {"success": False, "message": message}
        try:
            ok, message, payload = _run_crm_unlock_with_retry(dry_run=False)
        except Exception as e:
            logger.exception("Automate Processing stock unlocker step failed unexpectedly")
            ok = False
            message = str(e)
            payload = {"success": False, "message": message, "error_type": type(e).__name__}
        state = _persist_crm_run_result(ok, message, payload, dry_run=False)
        _finish_crm_runtime(ok, message, state, release_lock=False)
        return {
            "key": step_key,
            "label": _crm_processing_step_label(step_key),
            "success": bool(ok),
            "message": str(message),
        }

    normalized_filter = _normalize_crm_shipping_filter(processing_filter)
    with crm_address_state_lock:
        address_state = load_crm_address_state()
    batch_size = _normalize_crm_batch_size(address_state.get("saved_batch_size"), default=0, minimum=1, maximum=25, allow_unlimited=True)
    parallel_workers = _normalize_crm_positive_int(address_state.get("saved_parallel_workers"), default=1, minimum=1, maximum=CRM_SHARED_MAX_PARALLEL_WORKERS)
    if batch_size is not None:
        parallel_workers = min(parallel_workers, batch_size)
    list_url = _crm_processing_address_list_url_for_filter(normalized_filter)
    if normalized_filter == "813" and not list_url:
        message = f"{_crm_processing_813_url_config_key_for_step('address_validator_batch')} is empty in config.py."
        return {
            "key": step_key,
            "label": _crm_processing_step_label(step_key),
            "success": False,
            "message": message,
        }

    _start_crm_address_runtime(
        dry_run=False,
        action="validate_batch",
        target_order_id=None,
        active_filter=normalized_filter,
        list_url=list_url,
        batch_size=batch_size,
        parallel_workers=parallel_workers,
        last_message=(
            f"Address Validator batch queued {_crm_batch_scope_phrase(batch_size)} from "
            f"{_crm_shipping_filter_label(normalized_filter)}{' via configured mode list' if list_url else ''}."
        ),
    )
    ok = False
    message = "Address Validator batch did not run."
    payload = {"success": False, "message": message}
    try:
        ok, message, payload = _run_crm_address_with_retry(
            None,
            normalized_filter,
            dry_run=False,
            action="validate_batch",
            batch_size=batch_size,
            parallel_workers=parallel_workers,
            list_url=list_url,
        )
    except Exception as e:
        logger.exception("Automate Processing address validator step failed unexpectedly")
        ok = False
        message = str(e)
        payload = {"success": False, "message": message, "error_type": type(e).__name__}
    state = _persist_crm_address_run_result(
        ok,
        message,
        payload,
        None,
        normalized_filter,
        dry_run=False,
        action="validate_batch",
        batch_size=batch_size,
        parallel_workers=parallel_workers,
        list_url=list_url,
    )
    _finish_crm_address_runtime(ok, message, payload, state, release_lock=False)
    return {
        "key": step_key,
        "label": _crm_processing_step_label(step_key),
        "success": bool(ok),
        "stage_timings": _normalize_stage_timings(payload.get("stage_timings") if isinstance(payload, dict) else []),
        "message": str(message),
    }


def _crm_processing_run_thread(selected_steps, processing_filter):
    step_results = []
    overall_success = False
    summary = "Automate Processing did not run."
    normalized_filter = _normalize_crm_shipping_filter(processing_filter)
    try:
        with crm_processing_state_lock:
            processing_state = load_crm_processing_state()
        for step_key in selected_steps:
            if _automation_stop_is_blocking():
                summary = _force_stop_message("Automate Processing")
                step_results.append(
                    {
                        "key": step_key,
                        "label": _crm_processing_step_label(step_key),
                        "success": False,
                        "message": summary,
                    }
                )
                break
            step_label = _crm_processing_step_label(step_key)
            with crm_processing_runtime_lock:
                crm_processing_runtime["currentStep"] = step_key
                crm_processing_runtime["currentOrderProgress"] = None
                crm_processing_runtime["lastMessage"] = f"Running {step_label}."
            step_started_at = time.monotonic()
            result = _run_crm_processing_step(step_key, normalized_filter, processing_state=processing_state)
            result["duration_seconds"] = _normalize_duration_seconds(time.monotonic() - step_started_at)
            step_results.append(result)
            with crm_processing_runtime_lock:
                crm_processing_runtime["completedSteps"] = list(step_results)
                crm_processing_runtime["currentOrderProgress"] = None
                crm_processing_runtime["lastMessage"] = result["message"]
        overall_success, summary = _build_crm_processing_summary(step_results)
    except Exception as e:
        logger.exception("Automate Processing run failed unexpectedly")
        overall_success = False
        summary = str(e)
        if not step_results:
            step_results.append(
                {
                    "key": "stock_unlocker",
                    "label": "Automate Processing",
                    "success": False,
                    "message": summary,
                }
            )
    finally:
        state = _persist_crm_processing_run_result(overall_success, summary, selected_steps, step_results, processing_filter=normalized_filter)
        _audit_result("crm.processing", overall_success, summary)
        with crm_processing_runtime_lock:
            crm_processing_runtime["running"] = False
            crm_processing_runtime["completedAt"] = datetime.now().isoformat()
            crm_processing_runtime["currentStep"] = None
            crm_processing_runtime["completedSteps"] = list(step_results)
            crm_processing_runtime["currentOrderProgress"] = None
            crm_processing_runtime["lastMessage"] = str(summary)
            crm_processing_runtime["lastSuccess"] = bool(overall_success)
            crm_processing_runtime["stateSnapshot"] = state
        if crm_lock.locked():
            crm_lock.release()


def start_crm_processing_run(
    stock_unlocker_enabled=None,
    mass_emailer_enabled=None,
    address_validator_enabled=None,
    product_separator_enabled=None,
    order_goods_enabled=None,
    shipping_bypasser_enabled=None,
    push_back_enabled=None,
    processing_filter=None,
    persist_preferences=True,
):
    ensure_crm_processing_state_file()
    unlock_supplied = _crm_processing_value_supplied(stock_unlocker_enabled)
    address_supplied = _crm_processing_value_supplied(address_validator_enabled)
    separator_supplied = _crm_processing_value_supplied(product_separator_enabled)
    order_goods_supplied = _crm_processing_value_supplied(order_goods_enabled)
    shipping_bypasser_supplied = _crm_processing_value_supplied(shipping_bypasser_enabled)
    push_back_supplied = _crm_processing_value_supplied(push_back_enabled)
    filter_supplied = _crm_processing_value_supplied(processing_filter)

    with crm_processing_state_lock:
        state = load_crm_processing_state()
        target_filter = _normalize_crm_shipping_filter(processing_filter) if filter_supplied else state.get("processing_filter")
        mode_preferences = state.get("mode_preferences") if isinstance(state.get("mode_preferences"), dict) else {}
        target_preferences = dict(
            mode_preferences.get(target_filter)
            or _default_crm_processing_mode_preferences(target_filter)
        )
        if unlock_supplied:
            target_preferences["stock_unlocker_enabled"] = _normalize_crm_processing_enabled(
                stock_unlocker_enabled,
                default=target_preferences.get("stock_unlocker_enabled"),
            )
        if address_supplied:
            target_preferences["address_validator_enabled"] = _normalize_crm_processing_enabled(
                address_validator_enabled,
                default=target_preferences.get("address_validator_enabled"),
            )
        if separator_supplied:
            target_preferences["product_separator_enabled"] = _normalize_crm_processing_enabled(
                product_separator_enabled,
                default=target_preferences.get("product_separator_enabled"),
            )
        if order_goods_supplied:
            target_preferences["order_goods_enabled"] = _normalize_crm_processing_enabled(
                order_goods_enabled,
                default=target_preferences.get("order_goods_enabled"),
            )
        if shipping_bypasser_supplied:
            target_preferences["shipping_bypasser_enabled"] = _normalize_crm_processing_enabled(
                shipping_bypasser_enabled,
                default=target_preferences.get("shipping_bypasser_enabled"),
            )
        if push_back_supplied:
            target_preferences["push_back_enabled"] = _normalize_crm_processing_enabled(
                push_back_enabled,
                default=target_preferences.get("push_back_enabled"),
            )
        mode_preferences[target_filter] = _sanitize_crm_processing_mode_preferences(target_filter, target_preferences)
        state["mode_preferences"] = _normalize_crm_processing_mode_preferences(mode_preferences)
        _apply_crm_processing_mode_preferences_to_state(state, target_filter)
        if persist_preferences:
            save_crm_processing_state(state)

    normalized_filter = _normalize_crm_shipping_filter(state.get("processing_filter"))
    selected_steps = _crm_processing_selected_steps_from_state(state)
    if not selected_steps:
        return False, "Select at least one automation in Automate Processing before starting."
    if not crm_lock.acquire(blocking=False):
        return False, "A CRM automation run is already in progress."

    log_automation_event(
        "crm.processing",
        "STARTED",
        f"Filter={normalized_filter} | Selected steps: {', '.join(selected_steps)}",
        source="server.py",
    )
    with crm_processing_runtime_lock:
        crm_processing_runtime["running"] = True
        crm_processing_runtime["startedAt"] = datetime.now().isoformat()
        crm_processing_runtime["completedAt"] = None
        crm_processing_runtime["currentStep"] = None
        crm_processing_runtime["processingFilter"] = normalized_filter
        crm_processing_runtime["selectedSteps"] = list(selected_steps)
        crm_processing_runtime["completedSteps"] = []
        crm_processing_runtime["currentOrderProgress"] = None
        crm_processing_runtime["lastMessage"] = "Automate Processing queued."
        crm_processing_runtime["lastSuccess"] = None

    threading.Thread(target=_crm_processing_run_thread, args=(list(selected_steps), normalized_filter), daemon=True).start()
    labels = ", ".join(_crm_processing_step_label(step) for step in selected_steps)
    return True, f"Automate Processing started for {_crm_shipping_filter_label(normalized_filter)}: {labels}."


def get_crm_processing_status_payload():
    ensure_crm_processing_state_file()
    with crm_processing_state_lock:
        state = load_crm_processing_state()
    runtime = _crm_processing_runtime_snapshot()
    if not runtime.get("running"):
        runtime["processingFilter"] = _normalize_crm_shipping_filter(state.get("processing_filter"))
        runtime["currentOrderProgress"] = None
    else:
        runtime["currentOrderProgress"] = _crm_processing_current_order_progress(runtime.get("currentStep"))
    return {
        "success": True,
        "running": bool(runtime.get("running")),
        "runtime": runtime,
        "state": state,
    }


def get_crm_processing_state_payload():
    ensure_crm_processing_state_file()
    with crm_processing_state_lock:
        state = load_crm_processing_state()
    return {"success": True, "state": state}


def run_work(action, automatic=False):
    mode = "automatic" if automatic else "manual"
    automation_name = f"work.{action}.{mode}"

    def _finish(ok, msg):
        _audit_result(automation_name, ok, msg)
        return ok, msg

    if action not in ("in", "out"):
        return _finish(False, "Invalid action argument.")
    if not clock_lock.acquire(blocking=False):
        return _finish(False, "Another operation is already running.")

    try:
        now = datetime.now()
        sync_note = None

        with state_lock:
            state = load_work_state(now)
            active_shift = state.get("active_shift")
            total_paid = _safe_float(state.get("total_paid_hours"))

        if action == "in":
            if active_shift:
                return _finish(False, "Work clock-in rejected: an active shift already exists.")

            # If a previous Paycom sync already shows an open shift today, import it first.
            inferred_from_existing = False
            inferred_existing_note = ""
            with state_lock:
                state = load_work_state(now)
                inferred_from_existing, inferred_existing_note = _infer_active_shift_from_paycom_rows(state, now)
                if inferred_from_existing:
                    save_work_state(state)
                    refresh_tray_status_from_state(state)
            if inferred_from_existing:
                schedule_note = ""
                if WORK_CLOCK_CAPPED:
                    sch_ok, sch_msg = ensure_auto_clock_out_schedule_if_needed(force_recompute=True)
                    schedule_note = sch_msg if sch_ok else sch_msg
                with state_lock:
                    st_after = load_work_state()
                    active_after = st_after.get("active_shift") or {}
                    auto_iso = active_after.get("auto_clock_out_at")
                auto_txt = ""
                if auto_iso:
                    try:
                        auto_txt = f" Auto clock-out: {_format_time(datetime.fromisoformat(auto_iso))}."
                    except ValueError:
                        pass
                return _finish(True, (
                    f"{inferred_existing_note} Imported as active shift to avoid duplicate Paycom clock-in."
                    f" {schedule_note}{auto_txt}"
                ).strip())

            if WORK_CLOCK_SYNC_FROM_PAYCOM and WORK_CLOCK_SYNC_BEFORE_CLOCK_IN:
                s_ok, s_msg, s_hours, s_day_rows = sync_week_hours_from_paycom("clock-in")
                inferred_active_from_sync = False
                inferred_note = ""
                with state_lock:
                    state = load_work_state()
                    _record_sync_result(state, s_ok, s_msg, s_hours if s_ok else None)
                    merged_days = _merge_paycom_day_rows_into_state(state, s_day_rows) if s_ok else 0
                    if s_ok:
                        inferred_active_from_sync, inferred_note = _infer_active_shift_from_paycom_rows(state, now)
                    save_work_state(state)
                    total_paid = _safe_float(state.get("total_paid_hours"))
                if s_ok:
                    pto_days = _count_paycom_possible_pto_days(s_day_rows)
                    sync_note = f"Paycom sync before clock-in: week is {s_hours:.2f}h ({merged_days} daily rows)."
                    if pto_days:
                        sync_note += f" Possible PTO/paid leave rows detected: {pto_days}."
                else:
                    sync_note = f"Paycom sync before clock-in failed: {s_msg} Using local total {total_paid:.2f}h."

                if inferred_active_from_sync:
                    schedule_note = ""
                    if WORK_CLOCK_CAPPED:
                        sch_ok, sch_msg = ensure_auto_clock_out_schedule_if_needed(force_recompute=True)
                        schedule_note = sch_msg if sch_ok else sch_msg
                    with state_lock:
                        st_after = load_work_state()
                        active_after = st_after.get("active_shift") or {}
                        auto_iso = active_after.get("auto_clock_out_at")
                    auto_txt = ""
                    if auto_iso:
                        try:
                            auto_txt = f" Auto clock-out: {_format_time(datetime.fromisoformat(auto_iso))}."
                        except ValueError:
                            pass
                    msg = (
                        f"{inferred_note} Imported as active shift to avoid duplicate Paycom clock-in."
                        f" {schedule_note}{auto_txt}"
                    ).strip()
                    return _finish(True, msg)

            if WORK_CLOCK_CAPPED and total_paid >= WORK_CLOCK_CAP_HOURS:
                return _finish(False, f"Weekly cap already reached ({total_paid:.2f}/{WORK_CLOCK_CAP_HOURS:.2f} hours). Clock-in skipped.")

            c_ok, c_msg = _run_clock_action_with_retry("in", dry_run=False, retries=1, delay_seconds=3)
            if not c_ok:
                return _finish(False, c_msg)

            s_ok, s_msg = _run_slack_action_with_retry("in", retries=1, delay_seconds=3)
            now = datetime.now()
            auto_out_dt = None
            auto_out_note = ""
            auto_allowed_today = bool(WORK_CLOCK_CAPPED)

            with state_lock:
                state = load_work_state(now)
                total_paid = _safe_float(state.get("total_paid_hours"))
                if auto_allowed_today:
                    auto_out_dt, auto_out_note = _compute_auto_out_for_new_clock_in(total_paid, now)

                day_key = now.date().isoformat()
                day_entry = state.setdefault("days", {}).get(day_key, {})
                day_entry["clock_in_at"] = now.isoformat()
                day_entry["clock_out_at"] = None
                day_entry["break_minutes"] = WORK_CLOCK_BREAK_MINUTES
                day_entry["auto_clock_out_at"] = auto_out_dt.isoformat() if auto_out_dt else None
                day_entry["manual_auto_clock_out"] = False
                day_entry["auto_clock_out_source"] = "auto" if auto_out_dt else None
                state["days"][day_key] = day_entry
                state["active_shift"] = {
                    "date": day_key,
                    "clock_in_at": now.isoformat(),
                    "auto_clock_out_at": auto_out_dt.isoformat() if auto_out_dt else None,
                    "automatic_mode": bool(auto_out_dt),
                    "manual_auto_clock_out": False,
                    "auto_clock_out_source": "auto" if auto_out_dt else None,
                }
                save_work_state(state)

            if auto_out_dt:
                schedule_auto_clock_out(auto_out_dt)
            else:
                cancel_auto_clock_out_timer()
            with state_lock:
                refresh_tray_status_from_state(load_work_state())

            parts = ["Work clock-in completed.", c_msg, s_msg if s_ok else f"Slack in failed: {s_msg}"]
            if sync_note:
                parts.append(sync_note)
            if auto_out_dt:
                at = _format_auto_clock_out_label(auto_out_dt)
                parts.append(f"Auto clock-out scheduled for {at}.")
                notify_user("Work Clock In", f"Auto clock-out {at}.")
            else:
                if WORK_CLOCK_CAPPED:
                    parts.append(auto_out_note or "Auto clock-out could not be scheduled for this shift.")
                else:
                    parts.append("CAPPED is FALSE, so clock-out stays manual.")
            return _finish(True, " ".join(parts))

        if automatic and not active_shift:
            cancel_auto_clock_out_timer()
            with state_lock:
                refresh_tray_status_from_state(load_work_state())
            return _finish(False, "Auto clock-out skipped: no active tracked shift found.")

        if automatic:
            with state_lock:
                state = load_work_state(now)
                active_shift = state.get("active_shift") or {}
                allowed, reason = _auto_clock_out_allowed_for_active_shift(active_shift, now=now, state=state)
                if not allowed:
                    _cancel_auto_clock_timer_locked()
                    if not _clear_closed_active_shift_locked(state, now=now):
                        _clear_active_auto_clock_out_locked(state)
                    save_work_state(state)
                    refresh_tray_status_from_state(state)
                    return _finish(False, reason)

        c_ok, c_msg = _run_clock_action_with_retry("out", dry_run=False, retries=1, delay_seconds=3)
        if not c_ok:
            return _finish(False, c_msg)

        s_ok, s_msg = _run_slack_action_with_retry("out", retries=1, delay_seconds=3)
        now = datetime.now()
        paid = 0.0
        gross = 0.0
        did_reset = False

        with state_lock:
            state = load_work_state(now)
            active = state.get("active_shift") or {}
            day_key = active.get("date") or now.date().isoformat()
            day_entry = state.setdefault("days", {}).get(day_key, {})
            clock_in_iso = active.get("clock_in_at") or day_entry.get("clock_in_at")
            clock_in_dt = None
            if clock_in_iso:
                try:
                    clock_in_dt = datetime.fromisoformat(clock_in_iso)
                except ValueError:
                    pass
            if clock_in_dt:
                gross = max(0.0, (now - clock_in_dt).total_seconds() / 3600.0)
                paid = _paid_hours_for_gross_shift(gross)

            prev_paid = _safe_float(day_entry.get("paid_hours"), 0.0)
            day_entry["clock_in_at"] = clock_in_iso
            day_entry["clock_out_at"] = now.isoformat()
            day_entry["break_minutes"] = WORK_CLOCK_BREAK_MINUTES
            day_entry["gross_hours"] = round(gross, 2)
            day_entry["paid_hours"] = round(paid, 2)
            if active.get("auto_clock_out_at"):
                day_entry["auto_clock_out_at"] = active.get("auto_clock_out_at")
            state["days"][day_key] = day_entry
            state["total_paid_hours"] = round(max(0.0, _safe_float(state.get("total_paid_hours"), 0.0) - prev_paid + day_entry["paid_hours"]), 2)
            state["active_shift"] = None
            _cancel_auto_clock_timer_locked()

            if WORK_CLOCK_RESET_ON_FRIDAY_CLOCK_OUT and now.weekday() == 4:
                state = _new_work_state(now.date())
                state["last_reset_reason"] = "friday_clock_out"
                state["last_reset_at"] = now.isoformat()
                did_reset = True

            save_work_state(state)
            refresh_tray_status_from_state(state)

        if WORK_CLOCK_SYNC_FROM_PAYCOM and WORK_CLOCK_SYNC_AFTER_CLOCK_OUT:
            ps_ok, ps_msg, ps_hours, merged_days, pto_days = _sync_paycom_hours_into_work_state(
                "clock-out",
                update_total_hours=not did_reset,
            )
            if ps_ok:
                sync_note = f"Paycom sync after clock-out: week is {ps_hours:.2f}h ({merged_days} daily rows)."
                if pto_days:
                    sync_note += f" Possible PTO/paid leave rows detected: {pto_days}."
                if did_reset:
                    sync_note += " Weekly tracker stayed reset for the next Sunday cycle."
            else:
                sync_note = f"Paycom sync after clock-out failed: {ps_msg}"

        parts = ["Work clock-out completed.", c_msg, s_msg if s_ok else f"Slack out failed: {s_msg}"]
        if _break_hours_for_gross_shift(gross) > 0:
            break_note = f"includes {WORK_CLOCK_BREAK_MINUTES}m unpaid break deduction"
        else:
            break_note = f"no unpaid break because shift was not over {WORK_CLOCK_BREAK_APPLIES_AFTER_HOURS:g}h"
        parts.append(f"Tracked paid hours for this shift: {paid:.2f} ({break_note}).")
        if sync_note:
            parts.append(sync_note)
        if did_reset:
            parts.append("Weekly hour tracker was reset after Friday clock-out for the next Sunday cycle.")
        if automatic:
            auto_note = f"Automatically clocked out at {_format_time(now)}."
            parts.append(auto_note)
            notify_user("Work Auto Clock-Out", auto_note)
        return _finish(True, " ".join(parts))
    except Exception as e:
        logger.error("Work-%s error: %s", action, e)
        return _finish(False, str(e))
    finally:
        clock_lock.release()


def run_work_sync():
    automation_name = "work.sync.manual"

    def _finish(ok, msg):
        _audit_result(automation_name, ok, msg)
        return ok, msg

    if not WORK_CLOCK_SYNC_FROM_PAYCOM:
        return _finish(False, "Manual sync is disabled because WORK_CLOCK_SYNC_FROM_PAYCOM is False.")
    if not clock_lock.acquire(blocking=False):
        return _finish(False, "Another operation is already running.")
    try:
        ok, msg, hours, day_rows = sync_week_hours_from_paycom("manual")
        has_active_shift = False
        possible_pto_days = _count_paycom_possible_pto_days(day_rows) if ok else 0
        with state_lock:
            state = load_work_state()
            _record_sync_result(state, ok, msg, hours if ok else None)
            merged_days = _merge_paycom_day_rows_into_state(state, day_rows) if ok else 0
            has_active_shift = bool(state.get("active_shift"))
            save_work_state(state)
            refresh_tray_status_from_state(state)

        if ok:
            schedule_note = ""
            if has_active_shift and WORK_CLOCK_CAPPED:
                sch_ok, sch_msg = ensure_auto_clock_out_schedule_if_needed(force_recompute=True)
                if sch_ok:
                    schedule_note = f" {sch_msg}"
            pto_note = f" Possible PTO/paid leave rows detected: {possible_pto_days}." if possible_pto_days else ""
            return _finish(True, (
                f"Manual Paycom sync completed. Week hours: {hours:.2f}. Daily rows: {merged_days}."
                f"{pto_note}{schedule_note}"
            ))
        return _finish(False, f"Manual Paycom sync failed: {msg}")
    except Exception as e:
        logger.error("Work sync error: %s", e)
        return _finish(False, str(e))
    finally:
        clock_lock.release()


def _power_action_label(action):
    lookup = {
        "shutdown": "Shutdown",
        "sleep": "Sleep",
        "restart": "Restart",
    }
    return lookup.get(str(action or "").lower(), "Unknown")


def _format_countdown_hms(total_seconds):
    sec = max(0, int(math.ceil(_safe_float(total_seconds, 0))))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _clear_power_countdown_locked():
    global power_countdown_timer, power_countdown_state
    if power_countdown_timer:
        power_countdown_timer.cancel()
        power_countdown_timer = None
    power_countdown_state = {
        "action": None,
        "scheduled_at": None,
        "execute_at": None,
        "duration_seconds": 0,
    }


def _power_countdown_payload_locked(now=None):
    if now is None:
        now = datetime.now()
    action = power_countdown_state.get("action")
    scheduled_at = power_countdown_state.get("scheduled_at")
    execute_at = power_countdown_state.get("execute_at")
    duration_seconds = int(max(0, _safe_float(power_countdown_state.get("duration_seconds"), 0)))
    is_active = bool(action and execute_at)
    remaining_seconds = 0
    if is_active:
        remaining_seconds = max(0, int(math.ceil((execute_at - now).total_seconds())))
    action_label = _power_action_label(action)
    status_text = (
        f"{action_label} scheduled in {_format_countdown_hms(remaining_seconds)}."
        if is_active
        else "No active power countdown."
    )
    return {
        "success": True,
        "active": is_active,
        "action": action,
        "action_label": action_label,
        "scheduled_at": scheduled_at.isoformat() if isinstance(scheduled_at, datetime) else None,
        "execute_at": execute_at.isoformat() if isinstance(execute_at, datetime) else None,
        "duration_seconds": duration_seconds,
        "remaining_seconds": remaining_seconds,
        "remaining_text": _format_countdown_hms(remaining_seconds),
        "status_text": status_text,
    }


def get_power_countdown_payload():
    with power_timer_lock:
        return _power_countdown_payload_locked()


def _dispatch_power_action(action):
    action_key = str(action or "").strip().lower()
    if action_key == "shutdown":
        return trigger_pc_shutdown()
    if action_key == "sleep":
        return trigger_pc_sleep()
    if action_key == "restart":
        return trigger_pc_restart()
    return False, f"Unsupported power action: {action}"


def _parse_power_time_token(raw_time):
    text = str(raw_time or "").strip()
    if not text:
        return None
    normalized = text.upper().replace(".", "")
    for fmt in ("%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M%p"):
        try:
            return datetime.strptime(normalized, fmt).time()
        except ValueError:
            continue
    return None


def _resolve_power_schedule_datetime(raw_execute_at=None, raw_date=None, raw_time=None, now=None):
    if now is None:
        now = datetime.now()

    execute_text = str(raw_execute_at or "").strip()
    if execute_text:
        try:
            dt = datetime.fromisoformat(execute_text)
        except ValueError:
            return None, "Invalid execute_at format. Use YYYY-MM-DDTHH:MM."
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt, ""

    parsed_time = _parse_power_time_token(raw_time)
    if parsed_time is None:
        return None, "Provide either delay_seconds/delay_minutes or a valid schedule time."

    date_text = str(raw_date or "").strip()
    if date_text:
        try:
            target_day = datetime.fromisoformat(date_text).date()
        except ValueError:
            return None, "Invalid schedule_date format. Use YYYY-MM-DD."
        dt = datetime.combine(target_day, parsed_time)
        return dt, ""

    dt = datetime.combine(now.date(), parsed_time)
    if dt <= now:
        dt = dt + timedelta(days=1)
    return dt, ""


def schedule_power_at_datetime(action, execute_at):
    if not isinstance(execute_at, datetime):
        return False, "Invalid scheduled datetime."
    now = datetime.now()
    delay_seconds = int(math.ceil((execute_at - now).total_seconds()))
    if delay_seconds < 1:
        return False, "Scheduled time must be in the future."
    ok, msg = schedule_power_countdown(action, delay_seconds)
    if not ok:
        return ok, msg
    payload = get_power_countdown_payload()
    when = execute_at.strftime("%I:%M %p").lstrip("0")
    day = execute_at.strftime("%Y-%m-%d")
    return True, f"{_power_action_label(action)} scheduled for {when} on {day} (in {payload['remaining_text']})."


def _power_countdown_timer_callback():
    global power_countdown_timer, power_countdown_state
    with power_timer_lock:
        action = power_countdown_state.get("action")
        power_countdown_timer = None
        power_countdown_state = {
            "action": None,
            "scheduled_at": None,
            "execute_at": None,
            "duration_seconds": 0,
        }
    _refresh_tray_menu()
    if not action:
        return

    def _queued_power_countdown_action():
        ok, msg = _dispatch_power_action(action)
        label = _power_action_label(action)
        _audit_result(f"pc.countdown.{action}", ok, msg)
        notify_user(f"{label} Countdown {'OK' if ok else 'Failed'}", msg)
        return ok, msg

    enqueue_automation(f"{_power_action_label(action)} Countdown Action", "System Power", _queued_power_countdown_action)


def schedule_power_countdown(action, delay_seconds):
    action_key = str(action or "").strip().lower()
    if action_key not in {"shutdown", "sleep", "restart"}:
        return False, "Invalid action. Use shutdown, sleep, or restart."

    seconds = int(math.ceil(_safe_float(delay_seconds, -1)))
    if seconds < 1:
        return False, "Countdown must be at least 1 second."
    if seconds > (7 * 24 * 3600):
        return False, "Countdown is too long. Maximum is 7 days."

    now = datetime.now()
    execute_at = now + timedelta(seconds=seconds)
    with power_timer_lock:
        _clear_power_countdown_locked()
        power_countdown_state["action"] = action_key
        power_countdown_state["scheduled_at"] = now
        power_countdown_state["execute_at"] = execute_at
        power_countdown_state["duration_seconds"] = seconds
        global power_countdown_timer
        power_countdown_timer = threading.Timer(seconds, _power_countdown_timer_callback)
        power_countdown_timer.daemon = True
        power_countdown_timer.start()

    payload = get_power_countdown_payload()
    _refresh_tray_menu()
    msg = (
        f"{_power_action_label(action_key)} scheduled in {payload['remaining_text']} "
        f"(at {execute_at.strftime('%I:%M:%S %p').lstrip('0')})."
    )
    _audit_result("pc.countdown.schedule", True, msg)
    return True, msg


def cancel_power_countdown(audit=True):
    with power_timer_lock:
        action = power_countdown_state.get("action")
        execute_at = power_countdown_state.get("execute_at")
        is_active = bool(action and execute_at)
        _clear_power_countdown_locked()
    _refresh_tray_menu()
    if is_active:
        msg = f"Canceled {_power_action_label(action)} countdown."
    else:
        msg = "No active power countdown to cancel."
    if audit:
        _audit_result("pc.countdown.cancel", True, msg)
    return True, msg


def _launch_subprocess_async(cmd, automation_name):
    def worker():
        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0:
                _audit_result(automation_name, True, "System command finished.")
            else:
                details = (result.stderr or result.stdout or "").strip()
                msg = f"System command failed (exit code {result.returncode})."
                if details:
                    msg = f"{msg} {details}"
                _audit_result(automation_name, False, msg)
        except Exception as e:
            _audit_result(automation_name, False, f"System command error: {e}")

    threading.Thread(target=worker, daemon=True).start()


def trigger_pc_shutdown():
    automation_name = "pc.shutdown"
    log_automation_event(automation_name, "STARTED", "Dispatching system command.", source="server.py")
    _launch_subprocess_async(["shutdown", "/s", "/t", "0"], automation_name)
    return True, "PC is shutting down."


def trigger_pc_sleep():
    automation_name = "pc.sleep"
    log_automation_event(automation_name, "STARTED", "Dispatching system command.", source="server.py")
    _launch_subprocess_async(
        [
            "powershell",
            "-Command",
            "Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.Application]::SetSuspendState('Suspend', $false, $false)",
        ],
        automation_name,
    )
    return True, "PC is going to sleep."


def trigger_pc_restart():
    automation_name = "pc.restart"
    log_automation_event(automation_name, "STARTED", "Dispatching system command.", source="server.py")
    _launch_subprocess_async(["shutdown", "/r", "/t", "0"], automation_name)
    return True, "PC is restarting."


def trigger_pc_restart_explorer():
    automation_name = "pc.restart_explorer"
    log_automation_event(automation_name, "STARTED", "Restarting explorer.exe shell.", source="server.py")
    _launch_subprocess_async(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "Stop-Process -Name explorer -Force -ErrorAction SilentlyContinue; Start-Sleep -Seconds 2; Start-Process explorer.exe",
        ],
        automation_name,
    )
    return True, "Explorer.exe restart requested."


def run_crm_run_queued(dry_run=False):
    ok, msg = start_crm_run(dry_run=dry_run)
    if not ok:
        return ok, msg
    return _wait_for_status_completion(get_crm_status_payload, msg)


def run_crm_address_run_queued(order_id=None, dry_run=False, action="validate_order", batch_size=None, parallel_workers=None, list_url=None):
    ok, msg = start_crm_address_run(
        order_id=order_id,
        dry_run=dry_run,
        action=action,
        batch_size=batch_size,
        parallel_workers=parallel_workers,
        list_url=list_url,
    )
    if not ok:
        return ok, msg
    return _wait_for_status_completion(get_crm_address_status_payload, msg)


def run_crm_order_goods_run_queued(dry_run=False, batch_size=None, parallel_workers=None, list_url=None, order_id=None):
    ok, msg = start_crm_order_goods_run(
        dry_run=dry_run,
        batch_size=batch_size,
        parallel_workers=parallel_workers,
        list_url=list_url,
        order_id=order_id,
    )
    if not ok:
        return ok, msg
    return _wait_for_status_completion(get_crm_order_goods_status_payload, msg)


def run_crm_shipping_bypasser_run_queued(dry_run=False, batch_size=None, list_url=None, order_id=None):
    ok, msg = start_crm_shipping_bypasser_run(
        dry_run=dry_run,
        batch_size=batch_size,
        list_url=list_url,
        order_id=order_id,
    )
    if not ok:
        return ok, msg
    return _wait_for_status_completion(get_crm_shipping_bypasser_status_payload, msg)


def run_crm_push_back_run_queued(dry_run=False, batch_size=None, processing_filter="rush", list_url=None, order_id=None, parallel_workers=None):
    ok, msg = start_crm_push_back_run(
        dry_run=dry_run,
        batch_size=batch_size,
        processing_filter=processing_filter,
        list_url=list_url,
        order_id=order_id,
        parallel_workers=parallel_workers,
    )
    if not ok:
        return ok, msg
    return _wait_for_status_completion(get_crm_push_back_status_payload, msg)


def run_crm_product_separator_run_queued(dry_run=False, list_mode="rush", list_url=None, parallel_workers=None, order_id=None):
    ok, msg = start_crm_product_separator_run(
        dry_run=dry_run,
        list_mode=list_mode,
        list_url=list_url,
        parallel_workers=parallel_workers,
        order_id=order_id,
    )
    if not ok:
        return ok, msg
    return _wait_for_status_completion(get_crm_product_separator_status_payload, msg)


def run_crm_auto_splitter_run_queued(order_target=None, tab_count=None, divisions=None, minimum_tabs=10, dry_run=True, parallel_workers=None):
    ok, msg = start_crm_auto_splitter_run(
        order_target=order_target,
        tab_count=tab_count,
        divisions=divisions,
        minimum_tabs=minimum_tabs,
        dry_run=dry_run,
        parallel_workers=parallel_workers,
    )
    if not ok:
        return ok, msg
    return _wait_for_status_completion(get_crm_auto_splitter_status_payload, msg)


def run_crm_mass_emailer_run_queued(action="process_queue", dry_run=True, limit=None, retry_errors=False):
    ok, msg = start_crm_mass_emailer_run(
        action=action,
        dry_run=dry_run,
        limit=limit,
        retry_errors=retry_errors,
    )
    if not ok:
        return ok, msg
    return _wait_for_status_completion(get_crm_mass_emailer_status_payload, msg)


def run_crm_processing_run_queued(stock_unlocker_enabled=None, mass_emailer_enabled=None, address_validator_enabled=None, product_separator_enabled=None, order_goods_enabled=None, shipping_bypasser_enabled=None, push_back_enabled=None, processing_filter=None):
    ok, msg = start_crm_processing_run(
        stock_unlocker_enabled=stock_unlocker_enabled,
        mass_emailer_enabled=mass_emailer_enabled,
        address_validator_enabled=address_validator_enabled,
        product_separator_enabled=product_separator_enabled,
        order_goods_enabled=order_goods_enabled,
        shipping_bypasser_enabled=shipping_bypasser_enabled,
        push_back_enabled=push_back_enabled,
        processing_filter=processing_filter,
        persist_preferences=False,
    )
    if not ok:
        return ok, msg
    return _wait_for_status_completion(get_crm_processing_status_payload, msg)


def _load_config_assignment_keys():
    try:
        src = open(CONFIG_FILE, "r", encoding="utf-8-sig").read()
        tree = ast.parse(src)
    except Exception:
        return []
    keys = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name.isupper():
                keys.append(name)
    return keys


def _assignment_ranges(source):
    tree = ast.parse(source)
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name.isupper():
                out[name] = (node.lineno, getattr(node, "end_lineno", node.lineno))
    return out


def _field_kind(v):
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return "number"
    if isinstance(v, list):
        return "list"
    return "string"


def _cfg_group(key):
    if key == "PIN" or key.startswith("PAYCOM_"):
        return "paycom"
    if key.startswith("SLACK_"):
        return "slack"
    if key.startswith("WORK_"):
        return "work"
    if key.startswith("CRM_"):
        return "crm"
    return "other"


def _coerce_value(raw, expected):
    if isinstance(expected, bool):
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            s = raw.strip().lower()
            if s in ("1", "true", "yes", "on"):
                return True
            if s in ("0", "false", "no", "off"):
                return False
        raise ValueError("Expected boolean value")

    if isinstance(expected, int) and not isinstance(expected, bool):
        return int(raw)
    if isinstance(expected, float):
        return float(raw)
    if isinstance(expected, list):
        if isinstance(raw, list):
            return [str(x) for x in raw]
        if isinstance(raw, str):
            t = raw.strip()
            if not t:
                return []
            if t.startswith("["):
                parsed = json.loads(t)
                if not isinstance(parsed, list):
                    raise ValueError("Expected JSON list")
                return [str(x) for x in parsed]
            return [x.strip() for x in t.splitlines() if x.strip()]
        raise ValueError("Expected list value")

    return str(raw)


def _format_python_literal(v):
    if isinstance(v, str):
        return json.dumps(v)
    if isinstance(v, bool):
        return "True" if v else "False"
    if v is None:
        return "None"
    return repr(v)


def get_config_ui_payload():
    with config_lock:
        importlib.reload(config_module)
        _apply_runtime_config_from_module()

    groups = {"paycom": [], "slack": [], "work": [], "crm": [], "other": []}
    for key in _load_config_assignment_keys():
        if hasattr(config_module, key):
            val = getattr(config_module, key)
            groups[_cfg_group(key)].append({"key": key, "type": _field_kind(val), "value": val})
    return {"success": True, "groups": groups}


def update_config_values(updates):
    if not isinstance(updates, dict) or not updates:
        return False, "No config values provided."

    with config_lock:
        src = open(CONFIG_FILE, "r", encoding="utf-8-sig").read()
        keys = _load_config_assignment_keys()
        allowed = set(keys)
        bad = [k for k in updates if k not in allowed]
        if bad:
            return False, f"Invalid config keys: {', '.join(sorted(bad))}"

        importlib.reload(config_module)
        current = {k: getattr(config_module, k) for k in keys if hasattr(config_module, k)}
        parsed = {}
        for k, raw in updates.items():
            if k in current:
                try:
                    parsed[k] = _coerce_value(raw, current[k])
                except Exception as e:
                    return False, f"Invalid value for {k}: {e}"

        lines = src.splitlines()
        ranges = _assignment_ranges(src)
        repl = []
        for k, v in parsed.items():
            line = f"{k} = {_format_python_literal(v)}"
            if k in ranges:
                s, e = ranges[k]
                repl.append((s, e, line))
            else:
                repl.append((len(lines) + 1, len(lines), line))
        repl.sort(key=lambda x: x[0], reverse=True)
        for s, e, line in repl:
            if s <= len(lines):
                lines[s - 1:e] = [line]
            else:
                lines.append(line)

        new_src = "\n".join(lines) + "\n"
        backup = src
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                f.write(new_src)
            importlib.reload(config_module)
            _apply_runtime_config_from_module()
        except Exception as e:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                f.write(backup)
            importlib.reload(config_module)
            _apply_runtime_config_from_module()
            return False, f"Failed to save config: {e}"

    cancel_auto_clock_out_timer()
    restore_auto_clock_out_timer_from_state()
    return True, "Settings saved successfully."


def _run_async_with_notification(title, fn):
    def worker():
        ok, msg = fn()
        notify_user(f"{title} {'OK' if ok else 'Failed'}", msg)

    threading.Thread(target=worker, daemon=True).start()


def open_control_panel():
    try:
        webbrowser.open(f"http://127.0.0.1:{SERVER_PORT}/ui")
    except Exception as e:
        logger.warning("Could not open control panel: %s", e)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/", methods=["GET"])
def root():
    return Response('<html><body><meta http-equiv="refresh" content="0; url=/ui"></body></html>', mimetype="text/html")


@app.route("/ui", methods=["GET"])
def ui_page():
    try:
        with open(UI_TEMPLATE_FILE, "r", encoding="utf-8") as f:
            html = f.read()
    except Exception as e:
        logger.error("Could not load UI template %s: %s", UI_TEMPLATE_FILE, e)
        html = "<html><body><h1>Automation UI Failed To Load</h1><p>Check ui_panel.html file.</p></body></html>"
    return Response(html, mimetype="text/html")


@app.route("/api/server-runtime", methods=["GET"])
def api_server_runtime():
    try:
        return jsonify(get_server_runtime_payload())
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/console", methods=["GET"])
def api_console():
    try:
        return jsonify(get_console_log_payload(request.args.get("lines", 300)))
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/queue", methods=["GET"])
def api_queue():
    try:
        return jsonify(get_automation_queue_payload()), 200
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/queue/<task_id>/cancel", methods=["POST"])
def api_queue_cancel_task(task_id):
    ok, msg = cancel_automation_queue_task(task_id)
    payload = get_automation_queue_payload()
    payload.update({"success": ok, "message": msg})
    return jsonify(payload), (200 if ok else 404)


@app.route("/api/queue/<task_id>/delete", methods=["POST"])
def api_queue_delete_task(task_id):
    ok, msg = delete_automation_queue_task(task_id)
    payload = get_automation_queue_payload()
    payload.update({"success": ok, "message": msg})
    return jsonify(payload), (200 if ok else 400)


@app.route("/api/queue/cancel-all", methods=["POST"])
def api_queue_cancel_all():
    ok, msg = cancel_all_automation_queue_tasks()
    payload = get_automation_queue_payload()
    payload.update({"success": ok, "message": msg})
    return jsonify(payload), 200


@app.route("/api/queue/clear-finished", methods=["POST"])
def api_queue_clear_finished():
    ok, msg = clear_finished_automation_queue_tasks()
    payload = get_automation_queue_payload()
    payload.update({"success": ok, "message": msg})
    return jsonify(payload), 200


@app.route("/api/queue/reorder", methods=["POST"])
def api_queue_reorder():
    data = request.get_json(silent=True) or {}
    ok, msg, payload = reorder_automation_queue(data.get("task_ids") or data.get("taskIds") or [])
    payload.update({"success": ok, "message": msg})
    return jsonify(payload), (200 if ok else 400)


@app.route("/automation/force-stop", methods=["POST", "GET"])
def automation_force_stop():
    ok, msg = force_stop_automation()
    return jsonify({"success": ok, "message": msg}), 200


def _resolve_chrome_executable():
    env_path = os.environ.get("CHROME_PATH")
    candidates = [
        env_path,
        shutil.which("chrome.exe"),
        shutil.which("chrome"),
        os.path.join(os.environ.get("PROGRAMFILES", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
    ]
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    return None


def _resolve_profile_path(profile_dir):
    profile_text = str(profile_dir or "").strip()
    if not profile_text:
        raise ValueError("Chrome profile directory is empty.")
    if os.path.isabs(profile_text):
        return os.path.normpath(profile_text)
    return os.path.normpath(os.path.join(SCRIPT_DIR, profile_text))


def _chrome_profile_setup_targets():
    importlib.reload(config_module)
    return {
        "paycom": {
            "label": "Paycom",
            "profile_path": _resolve_profile_path("chrome_profile"),
            "url": str(getattr(config_module, "PAYCOM_URL", "") or "https://www.paycomonline.net/"),
        },
        "crm": {
            "label": "CRM",
            "profile_path": _resolve_profile_path(getattr(config_module, "CRM_PROFILE_DIR", "chrome_profile_crm")),
            "url": str(
                getattr(config_module, "CRM_LOGIN_URL", "")
                or getattr(config_module, "CRM_SHIPPING_URL", "")
                or "about:blank"
            ),
        },
        "slack": {
            "label": "Slack",
            "profile_path": _resolve_profile_path("slack_chrome_profile"),
            "url": str(getattr(config_module, "SLACK_CHANNEL_URL", "") or "https://app.slack.com/"),
        },
    }


def open_sanmar_cart_browser():
    try:
        importlib.reload(config_module)
        profile_dir = getattr(config_module, "SANMAR_PROFILE_DIR", "chrome_profile_sanmar")
        cart_url = str(getattr(config_module, "SANMAR_CART_URL", "") or "https://www.sanmar.com/cart").strip()
        profile_path = _resolve_profile_path(profile_dir)
        chrome = _resolve_chrome_executable()
        if chrome:
            subprocess.Popen(
                [chrome, f"--user-data-dir={profile_path}", cart_url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            webbrowser.open(cart_url)
        return True, "Opened SanMar cart.", {"profile_path": profile_path, "url": cart_url}
    except Exception as e:
        logger.warning("Could not open SanMar cart: %s", e)
        return False, f"Could not open SanMar cart: {e}", {}


@app.route("/automation/chrome-profile-setup", methods=["POST"])
def automation_chrome_profile_setup():
    data = request.get_json(silent=True) or {}
    profile_key = str(data.get("profile") or "").strip().lower()
    targets = _chrome_profile_setup_targets()
    target = targets.get(profile_key)
    if not target:
        return jsonify({"success": False, "message": "Unknown Chrome profile setup target."}), 400

    chrome_exe = _resolve_chrome_executable()
    if not chrome_exe:
        return jsonify({"success": False, "message": "Could not find chrome.exe on this PC."}), 500

    profile_path = target["profile_path"]
    os.makedirs(profile_path, exist_ok=True)
    args = [
        chrome_exe,
        f"--user-data-dir={profile_path}",
        "--profile-directory=Default",
        "--new-window",
        target["url"],
    ]
    try:
        subprocess.Popen(args, cwd=SCRIPT_DIR)
    except Exception as e:
        return jsonify({"success": False, "message": f"Could not open {target['label']} setup profile: {e}"}), 500

    msg = f"Opened {target['label']} Chrome profile setup window."
    logger.info("%s Profile path: %s", msg, profile_path)
    return jsonify({"success": True, "message": msg, "profile_path": profile_path, "url": target["url"]})


@app.route("/api/config", methods=["GET"])
def api_config_get():
    try:
        return jsonify(get_config_ui_payload())
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/config", methods=["POST"])
def api_config_set():
    data = request.get_json(silent=True) or {}
    updates = data.get("values")
    ok, msg = update_config_values(updates)
    return jsonify({"success": ok, "message": msg}), (200 if ok else 500)

def get_work_status_payload():
    with state_lock:
        state = load_work_state()
    active = state.get("active_shift") or {}
    is_scheduled = bool(active.get("auto_clock_out_at")) and auto_clock_timer is not None
    auto_clock = _build_auto_clock_payload(state)
    return {
        "success": True,
        "capped": WORK_CLOCK_CAPPED,
        "cap_hours": WORK_CLOCK_CAP_HOURS,
        "break_minutes": WORK_CLOCK_BREAK_MINUTES,
        "default_daily_hours": WORK_CLOCK_DEFAULT_DAILY_HOURS,
        "auto_out_max_hours": WORK_CLOCK_AUTO_OUT_MAX_HOURS,
        "paycom_sync_enabled": WORK_CLOCK_SYNC_FROM_PAYCOM,
        "paycom_sync_before_clock_in": WORK_CLOCK_SYNC_BEFORE_CLOCK_IN,
        "paycom_sync_after_clock_out": WORK_CLOCK_SYNC_AFTER_CLOCK_OUT,
        "auto_timer_active": bool(auto_clock_timer),
        "auto_scheduled": is_scheduled,
        "auto_clock": auto_clock,
        "state": state,
    }

register_work_routes(
    app,
    enqueue_automation=enqueue_automation,
    run_clock=run_clock,
    automation_test_catalog=AUTOMATION_TEST_CATALOG,
    run_automation_test_suite=run_automation_test_suite,
    run_slack=run_slack,
    is_trueish=_is_trueish,
    start_slack_lunch_break=start_slack_lunch_break,
    get_slack_lunch_payload=get_slack_lunch_payload,
    cancel_slack_lunch_break=cancel_slack_lunch_break,
    run_work=run_work,
    run_work_sync=run_work_sync,
    schedule_auto_clock_out_from_active_shift=schedule_auto_clock_out_from_active_shift,
    update_manual_auto_clock_out_schedule=update_manual_auto_clock_out_schedule,
    clear_auto_clock_out_schedule=clear_auto_clock_out_schedule,
    get_work_status_payload=get_work_status_payload,
    start_crm_run=start_crm_run,
    run_crm_run_queued=run_crm_run_queued,
    get_crm_status_payload=get_crm_status_payload,
    get_crm_state_payload=get_crm_state_payload,
    clear_crm_history=clear_crm_history,
    start_crm_address_run=start_crm_address_run,
    run_crm_address_run_queued=run_crm_address_run_queued,
    get_crm_address_status_payload=get_crm_address_status_payload,
    get_crm_address_state_payload=get_crm_address_state_payload,
    clear_crm_address_history=clear_crm_address_history,
    set_crm_address_filter=set_crm_address_filter,
    update_crm_address_preferences=update_crm_address_preferences,
    start_crm_order_goods_run=start_crm_order_goods_run,
    run_crm_order_goods_run_queued=run_crm_order_goods_run_queued,
    get_crm_order_goods_status_payload=get_crm_order_goods_status_payload,
    update_crm_order_goods_preferences=update_crm_order_goods_preferences,
    start_crm_shipping_bypasser_run=start_crm_shipping_bypasser_run,
    run_crm_shipping_bypasser_run_queued=run_crm_shipping_bypasser_run_queued,
    get_crm_shipping_bypasser_status_payload=get_crm_shipping_bypasser_status_payload,
    open_sanmar_cart_browser=open_sanmar_cart_browser,
    start_crm_product_separator_run=start_crm_product_separator_run,
    run_crm_product_separator_run_queued=run_crm_product_separator_run_queued,
    get_crm_product_separator_status_payload=get_crm_product_separator_status_payload,
    start_crm_auto_splitter_run=start_crm_auto_splitter_run,
    run_crm_auto_splitter_run_queued=run_crm_auto_splitter_run_queued,
    get_crm_auto_splitter_status_payload=get_crm_auto_splitter_status_payload,
    clear_crm_auto_splitter_history=clear_crm_auto_splitter_history,
    start_crm_mass_emailer_run=start_crm_mass_emailer_run,
    run_crm_mass_emailer_run_queued=run_crm_mass_emailer_run_queued,
    get_crm_mass_emailer_status_payload=get_crm_mass_emailer_status_payload,
    clear_crm_mass_emailer_history=clear_crm_mass_emailer_history,
    start_crm_processing_run=start_crm_processing_run,
    run_crm_processing_run_queued=run_crm_processing_run_queued,
    get_crm_processing_status_payload=get_crm_processing_status_payload,
    get_crm_processing_state_payload=get_crm_processing_state_payload,
    update_crm_processing_preferences=update_crm_processing_preferences,
)

register_system_routes(
    app,
    enqueue_automation=enqueue_automation,
    read_desktop_metrics=read_desktop_metrics,
    get_power_countdown_payload=get_power_countdown_payload,
    cancel_power_countdown=cancel_power_countdown,
    trigger_pc_shutdown=trigger_pc_shutdown,
    trigger_pc_sleep=trigger_pc_sleep,
    trigger_pc_restart=trigger_pc_restart,
    trigger_pc_restart_explorer=trigger_pc_restart_explorer,
    schedule_power_countdown=schedule_power_countdown,
    schedule_power_at_datetime=schedule_power_at_datetime,
    resolve_power_schedule_datetime=_resolve_power_schedule_datetime,
    safe_float=_safe_float,
)


def create_tray_icon():
    img = Image.new("RGB", (64, 64), "black")
    draw = ImageDraw.Draw(img)
    draw.ellipse((8, 8, 56, 56), fill="green")
    return img


def _tray_auto_out_label(_item):
    return tray_auto_out_text


def _tray_auto_out_visible(_item):
    return bool(tray_auto_out_active)


def _tray_week_hours_label(_item):
    return tray_week_hours_text


def _tray_power_countdown_label(_item):
    payload = get_power_countdown_payload()
    if payload.get("active"):
        return f"System countdown: {payload.get('action_label')} in {payload.get('remaining_text')}"
    return "System countdown: not scheduled"


def _tray_power_countdown_visible(_item):
    payload = get_power_countdown_payload()
    return bool(payload.get("active"))


def _tray_power_cancel_enabled(_item):
    payload = get_power_countdown_payload()
    return bool(payload.get("active"))


def tray_open_control_panel(icon, item):
    del icon, item
    open_control_panel()


def _restart_server_process():
    try:
        create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        hidden_launcher = os.path.join(SCRIPT_DIR, "start_server_hidden.vbs")
        if os.path.exists(hidden_launcher):
            subprocess.Popen(
                ["wscript.exe", hidden_launcher],
                cwd=SCRIPT_DIR,
                creationflags=create_no_window,
            )
            return True, "Server restart requested (hidden launcher)."

        python_exe = _resolve_windowless_python()
        script = os.path.abspath(__file__)
        cmd = [python_exe, script] + list(sys.argv[1:])
        subprocess.Popen(cmd, cwd=SCRIPT_DIR, creationflags=create_no_window)
        return True, "Server restart requested (windowless python)."
    except Exception as e:
        return False, f"Server restart failed: {e}"


def tray_restart_server(icon, item):
    del item
    ok, msg = _restart_server_process()
    if not ok:
        notify_user("Server Restart Failed", msg)
        return
    notify_user("Server Restart", "Restarting server now.")
    cancel_auto_clock_out_timer()
    cancel_power_countdown(audit=False)
    cancel_slack_lunch_break(audit=False)
    close_desktop_metrics_runtime()
    icon.stop()
    os._exit(0)


def tray_sleep(icon, item):
    del icon, item
    cancel_power_countdown(audit=False)
    _run_async_with_notification("PC Sleep", trigger_pc_sleep)


def tray_restart(icon, item):
    del icon, item
    cancel_power_countdown(audit=False)
    _run_async_with_notification("PC Restart", trigger_pc_restart)


def tray_shutdown(icon, item):
    del icon, item
    cancel_power_countdown(audit=False)
    _run_async_with_notification("PC Shutdown", trigger_pc_shutdown)


def tray_cancel_power_countdown(icon, item):
    del icon, item
    ok, msg = cancel_power_countdown(audit=True)
    notify_user("Power Countdown Cancel", msg if ok else "Countdown cancel failed.")


def on_exit(icon, _item=None):
    cancel_auto_clock_out_timer()
    cancel_power_countdown(audit=False)
    cancel_slack_lunch_break(audit=False)
    close_desktop_metrics_runtime()
    icon.stop()
    os._exit(0)


def kill_existing_server(port=SERVER_PORT):
    try:
        result = subprocess.run(["netstat", "-ano"], capture_output=True, text=True)
        for line in result.stdout.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                pid = int(line.strip().split()[-1])
                if pid != os.getpid():
                    subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
    except Exception as e:
        logger.warning("Could not check for existing server: %s", e)


if __name__ == "__main__":
    reload_runtime_config()
    ensure_crm_state_file()
    ensure_crm_processing_state_file()
    kill_existing_server()
    restore_auto_clock_out_timer_from_state()

    logger.info("Paycom Automation Server starting on http://%s:%s", SERVER_BIND_HOST, SERVER_PORT)
    logger.info("Endpoints:")
    logger.info("  GET      http://<your-pc-ip>:%s/ui", SERVER_PORT)
    logger.info("  GET      http://<your-pc-ip>:%s/api/metrics", SERVER_PORT)
    logger.info("  GET      http://<your-pc-ip>:%s/api/server-runtime", SERVER_PORT)
    logger.info("  GET      http://<your-pc-ip>:%s/api/console", SERVER_PORT)
    logger.info("  POST/GET http://<your-pc-ip>:%s/automation/force-stop", SERVER_PORT)
    logger.info("  GET/POST http://<your-pc-ip>:%s/api/config", SERVER_PORT)
    logger.info("  POST/GET http://<your-pc-ip>:%s/clock/test/in", SERVER_PORT)
    logger.info("  POST/GET http://<your-pc-ip>:%s/clock/test/out", SERVER_PORT)
    logger.info("  GET      http://<your-pc-ip>:%s/automation/test-options", SERVER_PORT)
    logger.info("  POST     http://<your-pc-ip>:%s/automation/test-suite", SERVER_PORT)
    logger.info("  POST/GET http://<your-pc-ip>:%s/slack/in", SERVER_PORT)
    logger.info("  POST/GET http://<your-pc-ip>:%s/slack/out", SERVER_PORT)
    logger.info("  POST/GET http://<your-pc-ip>:%s/slack/lunch", SERVER_PORT)
    logger.info("  GET      http://<your-pc-ip>:%s/slack/lunch/status", SERVER_PORT)
    logger.info("  POST/GET http://<your-pc-ip>:%s/slack/lunch/cancel", SERVER_PORT)
    logger.info("  POST/GET http://<your-pc-ip>:%s/work/in", SERVER_PORT)
    logger.info("  POST/GET http://<your-pc-ip>:%s/work/out", SERVER_PORT)
    logger.info("  POST/GET http://<your-pc-ip>:%s/work/sync", SERVER_PORT)
    logger.info("  POST/GET http://<your-pc-ip>:%s/work/schedule", SERVER_PORT)
    logger.info("  POST/GET http://<your-pc-ip>:%s/work/update-schedule", SERVER_PORT)
    logger.info("  POST/GET http://<your-pc-ip>:%s/work/cancel-schedule", SERVER_PORT)
    logger.info("  GET      http://<your-pc-ip>:%s/work/status", SERVER_PORT)
    logger.info("  POST/GET http://<your-pc-ip>:%s/crm/process", SERVER_PORT)
    logger.info("  POST/GET http://<your-pc-ip>:%s/crm/process/rush", SERVER_PORT)
    logger.info("  POST/GET http://<your-pc-ip>:%s/crm/process/free-ship", SERVER_PORT)
    logger.info("  POST/GET http://<your-pc-ip>:%s/crm/process/all", SERVER_PORT)
    logger.info("  POST/GET http://<your-pc-ip>:%s/crm/process/813", SERVER_PORT)
    logger.info("  GET      http://<your-pc-ip>:%s/crm/process/status", SERVER_PORT)
    logger.info("  POST     http://<your-pc-ip>:%s/crm/process/preferences", SERVER_PORT)
    logger.info("  POST/GET http://<your-pc-ip>:%s/crm/order-goods", SERVER_PORT)
    logger.info("  POST/GET http://<your-pc-ip>:%s/crm/order-goods/dry-run", SERVER_PORT)
    logger.info("  GET      http://<your-pc-ip>:%s/crm/order-goods/status", SERVER_PORT)
    logger.info("  POST/GET http://<your-pc-ip>:%s/crm/shipping-bypasser", SERVER_PORT)
    logger.info("  POST/GET http://<your-pc-ip>:%s/crm/shipping-bypasser/dry-run", SERVER_PORT)
    logger.info("  GET      http://<your-pc-ip>:%s/crm/shipping-bypasser/status", SERVER_PORT)
    logger.info("  POST/GET http://<your-pc-ip>:%s/crm/shipping-bypasser/sanmar-cart/open", SERVER_PORT)
    logger.info("  POST/GET http://<your-pc-ip>:%s/crm/auto-splitter", SERVER_PORT)
    logger.info("  POST/GET http://<your-pc-ip>:%s/crm/auto-splitter/dry-run", SERVER_PORT)
    logger.info("  GET      http://<your-pc-ip>:%s/crm/auto-splitter/status", SERVER_PORT)
    logger.info("  POST/GET http://<your-pc-ip>:%s/pc/sleep", SERVER_PORT)
    logger.info("  POST/GET http://<your-pc-ip>:%s/pc/restart", SERVER_PORT)
    logger.info("  POST/GET http://<your-pc-ip>:%s/pc/restart-explorer", SERVER_PORT)
    logger.info("  POST/GET http://<your-pc-ip>:%s/pc/shutdown", SERVER_PORT)
    logger.info("  POST/GET http://<your-pc-ip>:%s/pc/schedule", SERVER_PORT)
    logger.info("  POST/GET http://<your-pc-ip>:%s/pc/cancel-schedule", SERVER_PORT)
    logger.info("  GET      http://<your-pc-ip>:%s/pc/status", SERVER_PORT)

    threading.Thread(
        target=lambda: app.run(host=SERVER_BIND_HOST, port=SERVER_PORT, use_reloader=False),
        daemon=True,
    ).start()

    icon = pystray.Icon(
        "paycom_server",
        create_tray_icon(),
        "Paycom Server",
        menu=pystray.Menu(
            pystray.MenuItem("Open Control Panel", tray_open_control_panel, default=True),
            pystray.MenuItem(_tray_auto_out_label, None, enabled=False, visible=_tray_auto_out_visible),
            pystray.MenuItem(_tray_power_countdown_label, None, enabled=False, visible=_tray_power_countdown_visible),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Restart Server", tray_restart_server),
            pystray.MenuItem("Exit Server", on_exit),
        ),
    )

    tray_icon_ref = icon
    with state_lock:
        refresh_tray_status_from_state(load_work_state())
    icon.run()
