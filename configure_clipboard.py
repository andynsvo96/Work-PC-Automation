"""Configure the machine-local clipboard peer without exposing other settings."""

from __future__ import annotations

import argparse
import ast
import os
import tempfile
from urllib.parse import urlsplit


SETTING_NAME = "AUTOMATION_CLIPBOARD_PEER_URL"


def validate_peer_url(value: str) -> str:
    normalized = str(value or "").strip().rstrip("/")
    parsed = urlsplit(normalized)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or not parsed.hostname.lower().endswith(".ts.net")
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Peer URL must be a device-specific https://...ts.net URL without a path.")
    return normalized


def configure_peer_url(config_path: str, peer_url: str) -> str:
    peer_url = validate_peer_url(peer_url)
    config_path = os.path.abspath(config_path)
    try:
        with open(config_path, "r", encoding="utf-8-sig") as handle:
            source = handle.read()
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"Local config.py was not found: {config_path}") from exc
    tree = ast.parse(source, filename=config_path)
    lines = source.splitlines()
    replacement = f"{SETTING_NAME} = {peer_url!r}"
    assignment = next(
        (
            node
            for node in tree.body
            if isinstance(node, (ast.Assign, ast.AnnAssign))
            and (
                any(isinstance(target, ast.Name) and target.id == SETTING_NAME for target in getattr(node, "targets", []))
                or (isinstance(getattr(node, "target", None), ast.Name) and node.target.id == SETTING_NAME)
            )
        ),
        None,
    )
    if assignment is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(replacement)
    else:
        start = int(assignment.lineno) - 1
        end = int(getattr(assignment, "end_lineno", assignment.lineno))
        lines[start:end] = [replacement]
    output = "\n".join(lines) + "\n"
    parent = os.path.dirname(config_path)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", suffix=".tmp", delete=False, dir=parent
        ) as handle:
            temp_path = handle.name
            handle.write(output)
        os.replace(temp_path, config_path)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)
    return peer_url


def main(argv=None):
    parser = argparse.ArgumentParser(description="Configure the opposite computer's Tailscale clipboard URL.")
    parser.add_argument("peer_url", help="Device-specific HTTPS URL, such as https://mac.tailnet.ts.net:8443")
    parser.add_argument(
        "--config",
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.py"),
        help="Path to the machine-local config.py.",
    )
    options = parser.parse_args(argv)
    configured = configure_peer_url(options.config, options.peer_url)
    print(f"Clipboard peer configured: {configured}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
