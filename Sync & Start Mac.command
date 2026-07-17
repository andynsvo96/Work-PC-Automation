#!/bin/zsh
set -eu
SCRIPT_DIR="${0:A:h}"
cd "$SCRIPT_DIR"
if [[ -x ".venv/bin/python" ]]; then
  exec ".venv/bin/python" safe_sync.py start
fi
exec python3 safe_sync.py start
