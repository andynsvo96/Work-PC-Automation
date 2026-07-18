"""Read-only Git/version state used by Safe Sync and queue eligibility."""

from __future__ import annotations

import os
import subprocess


QUEUE_PROTOCOL_VERSION = 1
_GIT_CREATION_FLAGS = (
    getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
)


def _git_process_options():
    if not _GIT_CREATION_FLAGS:
        return {}
    return {"creationflags": _GIT_CREATION_FLAGS}


def _git(repo_dir, *args, timeout=10):
    result = subprocess.run(
        ["git", *args],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        timeout=timeout,
        **_git_process_options(),
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "Git command failed.").strip()
        raise RuntimeError(detail)
    return (result.stdout or "").strip()


def get_git_version_state(repo_dir):
    repo_dir = os.path.abspath(repo_dir)
    payload = {
        "available": False,
        "repo_dir": repo_dir,
        "commit": None,
        "short_commit": None,
        "branch": None,
        "dirty": None,
        "origin_commit": None,
        "relation": "unknown",
        "queue_protocol_version": QUEUE_PROTOCOL_VERSION,
        "error": None,
    }
    try:
        commit = _git(repo_dir, "rev-parse", "HEAD")
        branch = _git(repo_dir, "branch", "--show-current")
        status = _git(repo_dir, "status", "--porcelain")
        payload.update(
            {
                "available": True,
                "commit": commit,
                "short_commit": commit[:8],
                "branch": branch,
                "dirty": bool(status),
            }
        )
        try:
            origin_commit = _git(repo_dir, "rev-parse", "origin/main")
            payload["origin_commit"] = origin_commit
            if commit == origin_commit:
                payload["relation"] = "current"
            else:
                head_is_ancestor = subprocess.run(
                    ["git", "merge-base", "--is-ancestor", commit, origin_commit],
                    cwd=repo_dir,
                    capture_output=True,
                    timeout=10,
                    **_git_process_options(),
                ).returncode == 0
                origin_is_ancestor = subprocess.run(
                    ["git", "merge-base", "--is-ancestor", origin_commit, commit],
                    cwd=repo_dir,
                    capture_output=True,
                    timeout=10,
                    **_git_process_options(),
                ).returncode == 0
                payload["relation"] = "behind" if head_is_ancestor else ("ahead" if origin_is_ancestor else "diverged")
        except Exception:
            payload["relation"] = "origin-unavailable"
    except Exception as exc:
        payload["error"] = str(exc)
    return payload
