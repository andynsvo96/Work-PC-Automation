#!/bin/zsh
set -eu
SCRIPT_DIR="${0:A:h}"
cd "$SCRIPT_DIR"
LAUNCHD_TARGET="gui/$UID/com.workautomation.server"
if launchctl print "$LAUNCHD_TARGET" >/dev/null 2>&1; then
  launchctl kickstart -k "$LAUNCHD_TARGET"
elif [[ -x ".venv/bin/python" ]]; then
  ".venv/bin/python" safe_sync.py start >/dev/null 2>&1 &
else
  python3 safe_sync.py start >/dev/null 2>&1 &
fi

# A short health wait makes a double-click reliably show the dashboard after a
# restart instead of merely returning to Terminal.
for _ in {1..50}; do
  if curl --silent --fail --max-time 1 http://127.0.0.1:5123/ui >/dev/null 2>&1; then
    open "http://127.0.0.1:5123/ui"
    exit 0
  fi
  sleep 0.2
done

print -u2 "Automation started, but the dashboard did not become ready. Check runtime/logs/launchd.stderr.log."
exit 1
