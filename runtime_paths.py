"""
Centralized locations for local runtime artifacts.
"""

import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RUNTIME_DIR = os.path.join(SCRIPT_DIR, "runtime")
STATE_DIR = os.path.join(RUNTIME_DIR, "state")
RESULTS_DIR = os.path.join(RUNTIME_DIR, "results")
SCREENSHOTS_DIR = os.path.join(RUNTIME_DIR, "screenshots")
LOGS_DIR = os.path.join(RUNTIME_DIR, "logs")
DEBUG_DIR = os.path.join(RUNTIME_DIR, "debug")
GENERATED_PROFILES_DIR = os.path.join(RUNTIME_DIR, "generated_profiles")


def ensure_runtime_dirs():
    for folder in (
        RUNTIME_DIR,
        STATE_DIR,
        RESULTS_DIR,
        SCREENSHOTS_DIR,
        LOGS_DIR,
        DEBUG_DIR,
        GENERATED_PROFILES_DIR,
    ):
        os.makedirs(folder, exist_ok=True)


def state_file(filename):
    ensure_runtime_dirs()
    return os.path.join(STATE_DIR, filename)


def result_file(filename):
    ensure_runtime_dirs()
    return os.path.join(RESULTS_DIR, filename)


def log_file(filename):
    ensure_runtime_dirs()
    return os.path.join(LOGS_DIR, filename)


def resolve_runtime_file(path, default_dir):
    """
    Resolve simple configured filenames into runtime folders.

    Absolute paths and relative paths with a directory component remain explicit.
    Bare filenames such as "work_hours.json" are stored in the provided runtime
    directory.
    """
    text = str(path or "").strip()
    if not text:
        return ""
    if os.path.isabs(text):
        return text
    if os.path.dirname(text):
        return os.path.join(SCRIPT_DIR, text)
    ensure_runtime_dirs()
    return os.path.join(default_dir, text)
