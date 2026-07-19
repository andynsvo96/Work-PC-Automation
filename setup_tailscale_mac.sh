#!/bin/zsh
set -euo pipefail

SERVICE_NAME="${1:-svc:automation-control}"
PEER_URL="${2:-}"
TARGET="http://127.0.0.1:5123"
SCRIPT_DIR="${0:A:h}"

if command -v tailscale >/dev/null 2>&1; then
  TAILSCALE_BIN="$(command -v tailscale)"
elif [[ -x "/Applications/Tailscale.app/Contents/MacOS/Tailscale" ]]; then
  TAILSCALE_BIN="/Applications/Tailscale.app/Contents/MacOS/Tailscale"
else
  print -u2 "Tailscale CLI was not found. Install Tailscale and sign in first."
  exit 1
fi

"$TAILSCALE_BIN" status >/dev/null
# Device-specific HTTPS endpoint used only for authenticated clipboard peer
# requests. Unlike the shared Service URL, this always reaches this Mac.
"$TAILSCALE_BIN" serve --bg --https=8443 "$TARGET"
"$TAILSCALE_BIN" serve --bg --service="$SERVICE_NAME" --https=443 "$TARGET"
"$TAILSCALE_BIN" serve status
print "Tailscale dashboard Service and device-specific clipboard endpoint configured."
print "Use this Mac's device DNS URL with port 8443 as the peer URL in the Windows config.py."
if [[ -n "$PEER_URL" ]]; then
  if [[ -x "$SCRIPT_DIR/.venv/bin/python" ]]; then
    "$SCRIPT_DIR/.venv/bin/python" "$SCRIPT_DIR/configure_clipboard.py" "$PEER_URL"
  else
    python3 "$SCRIPT_DIR/configure_clipboard.py" "$PEER_URL"
  fi
fi
