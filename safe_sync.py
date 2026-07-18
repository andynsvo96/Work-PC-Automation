"""Conservative Git synchronization used by Windows and macOS launchers."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

from version_state import get_git_version_state


_GIT_CREATION_FLAGS = (
    getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
)


def _git_process_options():
    if not _GIT_CREATION_FLAGS:
        return {}
    return {"creationflags": _GIT_CREATION_FLAGS}


def _run(repo_dir, *args, timeout=120):
    result = subprocess.run(
        ["git", *args],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        timeout=timeout,
        **_git_process_options(),
    )
    detail = (result.stdout or result.stderr or "").strip()
    return result.returncode, detail


def sync_repository(repo_dir, *, fetch=True):
    repo_dir = os.path.abspath(repo_dir)
    before = get_git_version_state(repo_dir)
    if not before.get("available"):
        return {"success": False, "updated": False, "blocked": True, "message": before.get("error") or "Git is unavailable.", "state": before}
    if before.get("dirty"):
        return {
            "success": False,
            "updated": False,
            "blocked": True,
            "message": "Safe Sync refused because this computer has uncommitted changes.",
            "state": before,
        }

    if fetch:
        code, detail = _run(repo_dir, "fetch", "origin", "main")
        if code != 0:
            return {
                "success": False,
                "updated": False,
                "blocked": True,
                "message": f"Could not fetch origin/main: {detail}",
                "state": get_git_version_state(repo_dir),
            }

    state = get_git_version_state(repo_dir)
    relation = state.get("relation")
    if relation == "current":
        return {"success": True, "updated": False, "blocked": False, "message": "Already current with origin/main.", "state": state}
    if relation == "behind":
        code, detail = _run(repo_dir, "pull", "--ff-only", "origin", "main")
        after = get_git_version_state(repo_dir)
        if code == 0 and after.get("relation") == "current" and not after.get("dirty"):
            return {"success": True, "updated": True, "blocked": False, "message": detail or "Updated from origin/main.", "state": after}
        return {"success": False, "updated": False, "blocked": True, "message": detail or "Fast-forward pull failed.", "state": after}
    if relation == "ahead":
        message = "Safe Sync refused because local commits have not been pushed to origin/main."
    elif relation == "diverged":
        message = "Safe Sync refused because local and origin/main histories have diverged."
    else:
        message = "Safe Sync could not verify origin/main."
    return {"success": False, "updated": False, "blocked": True, "message": message, "state": state}


def _start_server(repo_dir, block_reason=""):
    env = os.environ.copy()
    if block_reason:
        env["AUTOMATION_VERSION_BLOCK_REASON"] = block_reason
    python_executable = sys.executable
    server_path = os.path.join(repo_dir, "server.py")
    os.execve(python_executable, [python_executable, server_path], env)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Safely update and start Work Automation.")
    parser.add_argument("action", choices=("status", "sync", "start"), nargs="?", default="sync")
    parser.add_argument("--repo", default=os.path.dirname(os.path.abspath(__file__)))
    parser.add_argument("--no-fetch", action="store_true")
    options = parser.parse_args(argv)
    repo_dir = os.path.abspath(options.repo)
    if options.action == "status":
        print(get_git_version_state(repo_dir))
        return 0
    result = sync_repository(repo_dir, fetch=not options.no_fetch)
    print(result.get("message") or "Safe Sync finished.")
    if options.action == "start":
        _start_server(repo_dir, result.get("message") if result.get("blocked") else "")
    return 0 if result.get("success") else 2


if __name__ == "__main__":
    raise SystemExit(main())
