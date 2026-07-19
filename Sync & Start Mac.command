#!/bin/zsh
set -eu
SCRIPT_DIR="${0:A:h}"
cd "$SCRIPT_DIR"
LAUNCHD_TARGET="gui/$UID/com.workautomation.server"
if launchctl print "$LAUNCHD_TARGET" >/dev/null 2>&1; then
  exec launchctl kickstart -k "$LAUNCHD_TARGET"
fi
if [[ -x ".venv/bin/python" ]]; then
  exec ".venv/bin/python" safe_sync.py start
fi
exec python3 safe_sync.py start
