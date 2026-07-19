"""Cross-platform node, capability, and worker-capacity detection."""

from __future__ import annotations

import math
import os
import platform
import socket
from dataclasses import asdict, dataclass

try:
    import psutil
except Exception:  # pragma: no cover - exercised by the fallback unit test
    psutil = None


WORKER_RAM_GB = 1.25
MIN_WORKERS = 1
MAX_WORKERS = 8


def normalize_os_name(system_name=None):
    value = str(system_name or platform.system() or "").strip().lower()
    if value == "windows":
        return "windows"
    if value == "darwin":
        return "macos"
    if value == "linux":
        return "linux"
    return value or "unknown"


def normalize_architecture(machine=None):
    value = str(machine or platform.machine() or "").strip().lower()
    if value in {"arm64", "aarch64"}:
        return "arm64"
    if value in {"amd64", "x86_64", "x64"}:
        return "x86_64"
    return value or "unknown"


def _positive_int(value, fallback=1):
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return max(1, int(fallback))


def _memory_snapshot():
    if psutil is not None:
        memory = psutil.virtual_memory()
        gib = float(1024 ** 3)
        return max(0.0, memory.total / gib), max(0.0, memory.available / gib)
    return 0.0, 0.0


def _cpu_snapshot():
    logical = _positive_int(os.cpu_count(), 1)
    physical = None
    if psutil is not None:
        physical = psutil.cpu_count(logical=False)
        logical = _positive_int(psutil.cpu_count(logical=True), logical)
    if not physical:
        physical = max(1, logical // 2)
    return _positive_int(physical, 1), logical


def recommend_parallel_workers(
    *,
    physical_cores,
    total_ram_gb,
    available_ram_gb,
    worker_ram_gb=WORKER_RAM_GB,
    maximum=MAX_WORKERS,
):
    """Return a conservative hardware recommendation bounded to 1..maximum."""
    physical_cores = _positive_int(physical_cores, 1)
    maximum = max(MIN_WORKERS, min(MAX_WORKERS, _positive_int(maximum, MAX_WORKERS)))
    worker_ram_gb = max(0.25, float(worker_ram_gb or WORKER_RAM_GB))
    total_ram_gb = max(0.0, float(total_ram_gb or 0.0))
    available_ram_gb = max(0.0, float(available_ram_gb or 0.0))

    cpu_cap = max(MIN_WORKERS, physical_cores // 2)
    if total_ram_gb > 0:
        reserved_total_gb = max(4.0, total_ram_gb * 0.30)
        total_memory_cap = max(
            MIN_WORKERS,
            math.floor(max(0.0, total_ram_gb - reserved_total_gb) / worker_ram_gb),
        )
    else:
        reserved_total_gb = None
        total_memory_cap = maximum

    if available_ram_gb > 0:
        available_memory_cap = max(
            MIN_WORKERS,
            math.floor(max(0.0, available_ram_gb - 2.0) / worker_ram_gb),
        )
    else:
        available_memory_cap = maximum

    recommended = max(
        MIN_WORKERS,
        min(maximum, cpu_cap, total_memory_cap, available_memory_cap),
    )
    return {
        "recommended_workers": recommended,
        "cpu_cap": min(maximum, cpu_cap),
        "total_memory_cap": min(maximum, total_memory_cap),
        "available_memory_cap": min(maximum, available_memory_cap),
        "reserved_total_ram_gb": round(reserved_total_gb, 2) if reserved_total_gb is not None else None,
        "worker_ram_gb": worker_ram_gb,
        "maximum_workers": maximum,
    }


@dataclass(frozen=True)
class PlatformSnapshot:
    node_name: str
    os_name: str
    os_display_name: str
    architecture: str
    physical_cores: int
    logical_cores: int
    total_ram_gb: float
    available_ram_gb: float
    capabilities: dict
    worker_recommendation: dict

    def to_dict(self):
        return asdict(self)


def platform_capabilities(os_name=None):
    normalized = normalize_os_name(os_name)
    is_windows = normalized == "windows"
    return {
        "automation": True,
        "crm": normalized in {"windows", "macos"},
        "paycom": normalized in {"windows", "macos"},
        "slack": normalized in {"windows", "macos"},
        "clipboard": normalized in {"windows", "macos"},
        "clipboard_image": normalized in {"windows", "macos"},
        "metrics": is_windows,
        "system_power": is_windows,
        "restart_explorer": is_windows,
    }


def get_platform_snapshot():
    os_name = normalize_os_name()
    physical_cores, logical_cores = _cpu_snapshot()
    total_ram_gb, available_ram_gb = _memory_snapshot()
    recommendation = recommend_parallel_workers(
        physical_cores=physical_cores,
        total_ram_gb=total_ram_gb,
        available_ram_gb=available_ram_gb,
    )
    display_names = {
        "windows": "Windows",
        "macos": "macOS",
        "linux": "Linux",
        "unknown": "Unknown",
    }
    return PlatformSnapshot(
        node_name=socket.gethostname(),
        os_name=os_name,
        os_display_name=display_names.get(os_name, os_name.title()),
        architecture=normalize_architecture(),
        physical_cores=physical_cores,
        logical_cores=logical_cores,
        total_ram_gb=round(total_ram_gb, 2),
        available_ram_gb=round(available_ram_gb, 2),
        capabilities=platform_capabilities(os_name),
        worker_recommendation=recommendation,
    )


def resolve_worker_count(mode="auto", manual_workers=1, *, snapshot=None, task_limit=None):
    snapshot = snapshot or get_platform_snapshot()
    mode = str(mode or "auto").strip().lower()
    manual_workers = max(MIN_WORKERS, min(MAX_WORKERS, _positive_int(manual_workers, 1)))
    if mode == "manual":
        workers = manual_workers
    else:
        mode = "auto"
        workers = _positive_int(snapshot.worker_recommendation.get("recommended_workers"), 1)
    if task_limit is not None:
        workers = min(workers, _positive_int(task_limit, 1))
    return {
        "mode": mode,
        "manual_workers": manual_workers,
        "effective_workers": max(MIN_WORKERS, min(MAX_WORKERS, workers)),
        "recommendation": snapshot.worker_recommendation,
    }
