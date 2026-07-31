"""Bounded cleanup for disposable runtime artifacts."""

from __future__ import annotations

import os
import time

from runtime_paths import SCREENSHOTS_DIR


SCREENSHOT_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".txt"})


def prune_screenshot_artifacts(
    directory=SCREENSHOTS_DIR,
    *,
    max_age_days=30,
    max_files=250,
    now=None,
):
    """Remove old/excess screenshot evidence without leaving ``directory``.

    Only ordinary top-level screenshot files are eligible. Symlinks and nested
    directories are ignored so a malformed runtime folder cannot broaden the
    cleanup scope.
    """
    root = os.path.abspath(directory)
    if not os.path.isdir(root):
        return {"removed_files": 0, "removed_bytes": 0}

    current_time = float(time.time() if now is None else now)
    age_days = max(0, int(max_age_days))
    keep_count = max(0, int(max_files))
    cutoff = current_time - (age_days * 86400)
    candidates = []

    for entry in os.scandir(root):
        if not entry.is_file(follow_symlinks=False):
            continue
        if os.path.splitext(entry.name)[1].lower() not in SCREENSHOT_EXTENSIONS:
            continue
        stat = entry.stat(follow_symlinks=False)
        candidates.append((stat.st_mtime, stat.st_size, os.path.abspath(entry.path)))

    candidates.sort(reverse=True)
    remove_paths = {
        path
        for index, (modified, _size, path) in enumerate(candidates)
        if modified < cutoff or index >= keep_count
    }

    removed_files = 0
    removed_bytes = 0
    for _modified, size, path in candidates:
        if path not in remove_paths:
            continue
        if os.path.commonpath((root, path)) != root:
            raise RuntimeError(f"Refusing to prune screenshot outside runtime directory: {path}")
        try:
            os.remove(path)
        except FileNotFoundError:
            continue
        removed_files += 1
        removed_bytes += size

    return {"removed_files": removed_files, "removed_bytes": removed_bytes}


def run_startup_maintenance(config_module):
    return prune_screenshot_artifacts(
        max_age_days=getattr(config_module, "AUTOMATION_SCREENSHOT_RETENTION_DAYS", 30),
        max_files=getattr(config_module, "AUTOMATION_SCREENSHOT_MAX_FILES", 250),
    )
