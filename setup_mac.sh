#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
cd "$SCRIPT_DIR"

if [[ "$(uname -s)" != "Darwin" ]]; then
  print -u2 "This installer must run on macOS."
  exit 1
fi

print "Detected macOS $(uname -m)."
if ! command -v python3 >/dev/null 2>&1; then
  print -u2 "Python 3 is required. Install it, then run this script again."
  exit 1
fi
if ! command -v git >/dev/null 2>&1; then
  print -u2 "Git is required. Run 'xcode-select --install', then try again."
  exit 1
fi

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
"$SCRIPT_DIR/.venv/bin/python" -m pip install --upgrade pip
"$SCRIPT_DIR/.venv/bin/python" -m pip install -r requirements.txt

if [[ ! -f config.py ]]; then
  cp config.example.py config.py
  print "Created local config.py from config.example.py. Copy your Windows settings into it before live runs."
fi

if [[ ! -d "/Applications/Google Chrome.app" ]]; then
  print "Warning: Google Chrome was not found in /Applications. Install Chrome before browser automation."
fi
if ! command -v tailscale >/dev/null 2>&1 && [[ ! -x "/Applications/Tailscale.app/Contents/MacOS/Tailscale" ]]; then
  print "Warning: Tailscale CLI was not found. Install and sign in to Tailscale before remote tablet setup."
fi

PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST_PATH="$PLIST_DIR/com.workautomation.server.plist"
mkdir -p "$PLIST_DIR" "$SCRIPT_DIR/runtime/logs"

python3 - "$PLIST_PATH" "$SCRIPT_DIR" <<'PY'
import plistlib
import sys
from pathlib import Path

output = Path(sys.argv[1])
repo = Path(sys.argv[2]).resolve()
payload = {
    "Label": "com.workautomation.server",
    "ProgramArguments": [str(repo / ".venv" / "bin" / "python"), str(repo / "safe_sync.py"), "start"],
    "WorkingDirectory": str(repo),
    "RunAtLoad": True,
    "KeepAlive": True,
    "ThrottleInterval": 10,
    "ProcessType": "Interactive",
    "StandardOutPath": str(repo / "runtime" / "logs" / "launchd.stdout.log"),
    "StandardErrorPath": str(repo / "runtime" / "logs" / "launchd.stderr.log"),
    "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
}
with output.open("wb") as handle:
    plistlib.dump(payload, handle, sort_keys=False)
PY

launchctl bootout "gui/$UID" "$PLIST_PATH" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$UID" "$PLIST_PATH"
launchctl enable "gui/$UID/com.workautomation.server"

print "macOS setup complete. The app now uses Safe Sync & Start at login."
print "Next: configure credentials, Supabase, app PIN, and Tailscale using docs/MAC_AND_TABLET_SETUP.md."
