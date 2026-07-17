#!/bin/zsh
set -euo pipefail

SERVICE_NAME="${1:-svc:automation-control}"
TARGET="http://127.0.0.1:5123"

if command -v tailscale >/dev/null 2>&1; then
  TAILSCALE_BIN="$(command -v tailscale)"
elif [[ -x "/Applications/Tailscale.app/Contents/MacOS/Tailscale" ]]; then
  TAILSCALE_BIN="/Applications/Tailscale.app/Contents/MacOS/Tailscale"
else
  print -u2 "Tailscale CLI was not found. Install Tailscale and sign in first."
  exit 1
fi

"$TAILSCALE_BIN" status >/dev/null
"$TAILSCALE_BIN" serve --bg --service="$SERVICE_NAME" --https=443 "$TARGET"
"$TAILSCALE_BIN" serve status
print "Tailscale Service configured. Open the service URL on the Android tablet and enter the app PIN."
