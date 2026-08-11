# Mac, Tablet, and Tailscale Setup

Windows and macOS each run their own local automation queue. There is no cloud
queue or external database. Use only one computer's control panel when adding
work so the same order is not queued independently on both computers.

Tailscale provides private HTTPS access to each local dashboard. The app PIN is
an additional browser-access control. Cross-system clipboard traffic also uses
device-specific Tailscale HTTPS endpoints and is authenticated with signed,
time-limited requests.

## Install the Mac app

From the repository on macOS:

```bash
chmod +x setup_mac.sh setup_tailscale_mac.sh "Sync & Start Mac.command"
./setup_mac.sh
```

Create a machine-local `config.py` from `config.example.py`. Keep the queue
local and enable Tailscale access:

```python
AUTOMATION_REMOTE_ACCESS_MODE = "tailscale"
AUTOMATION_APP_PIN_REQUIRED = True
AUTOMATION_LAN_REST_ENABLED = False
```

Store the required service credentials in the macOS Keychain with
`manage_credentials.py`. Install the same app-security bundle on Windows and
Mac with `setup_app_security.py` so both dashboards use the same PIN and peer
request secret.

## Configure Tailscale

On Windows:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_tailscale_windows.ps1
```

On macOS:

```bash
./setup_tailscale_mac.sh
```

Confirm Tailscale starts at login and remains connected on each computer. Open
the dashboard through the Tailscale HTTPS URL for the computer that should run
the automation. The dashboard no longer includes a control-target selector;
requests always execute on the computer serving that dashboard.

## Cross-system clipboard

The clipboard uses device-specific port `8443` endpoints. A shared Tailscale
service URL must not be used for clipboard traffic because each computer needs
to address the other computer directly.

Find the full MagicDNS device names in `tailscale status` or the Tailscale admin
console, then set the opposite computer's URL in each local config:

```python
AUTOMATION_CLIPBOARD_PEER_URL = "https://OTHER-COMPUTER.YOUR-TAILNET.ts.net:8443"
```

Clipboard contents are not written to logs, state files, or Git.

## Operating rule

- Queue work from only one OS at a time.
- The queue, retry history, schedules, and work-clock state belong to that
  computer and reset or persist according to their existing local behavior.
- Opening the other computer's dashboard shows that computer's independent
  queue and state.
- Windows-only metrics and power controls are available only from the Windows
  dashboard.
- Home Assistant and other authenticated HTTP triggers execute on the computer
  whose Tailscale or LAN URL receives the request.
