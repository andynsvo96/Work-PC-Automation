# macOS and Android control-board setup

The two computers run the same Git commit, but keep credentials and machine settings in their own OS keychains. Supabase owns the single FIFO queue. Tailscale provides private HTTPS access; the app PIN is a second lock.

## 1. Prepare GitHub and Windows

1. Commit and push the finished implementation from Windows.
2. Run `python setup_app_security.py create --output app-security-transfer.json`.
3. Create separate Windows and Mac users in Supabase Authentication. Run the SQL files in `supabase/migrations/` in numeric order in the SQL editor. Existing installations should also apply any newly added migration, including `003_allow_zero_repeat_interval.sql`.
4. As the Windows/owner user, run `python configure_shared_queue.py bootstrap --node-key windows-pc --generate-encryption-key`. The prompts hide the password and store the finished bundle directly in Windows Credential Manager. Then run `python configure_shared_queue.py export-transfer queue-transfer.json` for the one-time Mac transfer.
5. Run `python configure_shared_queue.py add-member MAC_AUTH_USER_UUID`.
6. After the final code commit is pushed, run `python configure_shared_queue.py set-version-gate`.

## 2. Install on the Mac

```zsh
git clone https://github.com/andynsvo96/Work-PC-Automation.git ~/Automation
cd ~/Automation
chmod +x setup_mac.sh setup_tailscale_mac.sh "Sync & Start Mac.command"
./setup_mac.sh
```

Copy the Windows `config.py` values into the Mac's local `config.py`; keep `config.py` uncommitted. Desktop Metrics and the power-action cards remain Windows only; cross-system clipboard controls remain available on macOS.

Import the shared PIN bundle, then delete it:

```zsh
.venv/bin/python setup_app_security.py import /path/to/app-security-transfer.json
rm /path/to/app-security-transfer.json
```

Join the Mac with its separate Supabase Auth user, the same workspace UUID, and the same Fernet key printed/generated during Windows bootstrap:

```zsh
.venv/bin/python configure_shared_queue.py join --node-key macbook --transfer-file /path/to/queue-transfer.json
.venv/bin/python configure_shared_queue.py test
```

Delete `queue-transfer.json` immediately after the Mac imports it.

Enter Paycom, CRM, Slack/browser, SanMar, Salesforce, and Google credentials with `manage_credentials.py`. Do not copy Windows Credential Manager files to the Mac.

## 3. Enable shared mode and Tailscale

On both local `config.py` files set:

```python
AUTOMATION_QUEUE_MODE = "shared"
AUTOMATION_REMOTE_ACCESS_MODE = "tailscale"
AUTOMATION_APP_PIN_REQUIRED = True
```

Home Assistant REST commands should use the shared service URL
`https://automation-control.YOUR-TAILNET.ts.net/...` for Windows-first routing
with Mac fallback. A legacy command that must continue calling a computer's
private LAN address can set `AUTOMATION_LAN_REST_ENABLED = True` in that
computer's local `config.py`; this intentionally exposes the PIN-protected
server on port 5123 to the trusted LAN and cannot provide failover when that
specific computer is offline.

Install Tailscale on Windows, macOS, and Android; sign all three into the same tailnet. Configure the same stable Service on both computers:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_tailscale_windows.ps1
```

```zsh
./setup_tailscale_mac.sh
```

The tailnet administrator must authorize `svc:automation-control` for both computers and the tablet. Tailscale Serve stays private to the tailnet and resumes after reboot when configured in background mode. The Flask server listens only on `127.0.0.1` in Tailscale mode.

### Startup behavior

`setup_mac.sh` installs `com.workautomation.server` as a per-user LaunchAgent with both `RunAtLoad` and `KeepAlive`, so Safe Sync & Start launches at login and is restarted by macOS if it exits. Logs are written under `runtime/logs/`.

The first launch of the Tailscale Mac app installs its macOS login helper. In **System Settings → General → Login Items & Extensions**, confirm Tailscale is allowed to run at login and its Network Extension is enabled. Tailscale must be connected on both computers for clipboard transfer and private control-panel access. If either computer or Tailscale connection is offline, communication pauses until connectivity returns. See Tailscale's official [start-at-login policy documentation](https://tailscale.com/docs/features/tailscale-system-policies#automatically-start-tailscale-when-the-user-logs-in) and [macOS extension instructions](https://tailscale.com/docs/concepts/macos-sysext).

### Cross-system clipboard

The Tailscale setup scripts also expose an authenticated, device-specific HTTPS endpoint on port `8443`. The shared `svc:automation-control` URL must not be used for clipboard traffic because a shared Service can route to either computer.

After running both Tailscale setup scripts, find each computer's full MagicDNS device name in the Tailscale admin console or `tailscale status`. Add the opposite computer's URL to each machine-local `config.py`:

The setup scripts accept the opposite computer's URL as an optional second step/parameter and update only this one machine-local setting. For the current Mac, the one-command setup is:

```zsh
./setup_tailscale_mac.sh svc:automation-control https://sirius.tail45cc11.ts.net:8443
```

The equivalent Windows form is:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_tailscale_windows.ps1 -PeerUrl https://YOUR-MAC.YOUR-TAILNET.ts.net:8443
```

You can also set it manually:

```python
# Windows config.py points to the Mac:
AUTOMATION_CLIPBOARD_PEER_URL = "https://YOUR-MAC.YOUR-TAILNET.ts.net:8443"
```

```python
# Mac config.py points to Windows:
AUTOMATION_CLIPBOARD_PEER_URL = "https://YOUR-WINDOWS-PC.YOUR-TAILNET.ts.net:8443"
```

Restart Safe Sync & Start on both computers. The **System Controls** tab then provides:

- **Send to Other Computer** and **Get from Other Computer** for deliberate text or PNG image transfer, even when automatic sync is off.
- A machine-local **Automatic Sync** toggle. Automatic two-way monitoring becomes active only when both computers enable it.
- Connection and last-sync metadata without recording clipboard contents.

The same shared app-security bundle must remain installed on both computers. It supplies the server-to-server authentication secret. Requests are timestamped, signed, replay-protected, restricted to 1 MB of text or 8 MB PNG images, and carried only through the private Tailscale endpoint. Clipboard contents are not written to logs, state files, Supabase, or Git.

## 4. Cut over safely

1. Stop new local runs and wait for the old Windows in-memory queue to be empty.
2. Push the final commit and run Safe Sync & Start on Windows.
3. On the Mac, let the LaunchAgent run Safe Sync & Start or launch `Sync & Start Mac.command` once.
4. Confirm both computers show the identical commit and queue protocol in Settings.
5. Submit one harmless dry run from Windows, then another from the Mac. Confirm the second remains queued until the first finishes.
6. Open the Tailscale Service URL on Android, enter the PIN, and repeat the dry-run test.
7. Copy text and a small image in each direction using the manual clipboard buttons. Then enable Automatic Sync on both computers and repeat with newly copied content.

In Chrome on the Android tablet, open the menu and choose **Install app** (or **Add to Home screen**). Use the **Control target** selector in the header when an action must run on a specific computer. The **System Controls** tab remains available on macOS for clipboard sync, while its Windows-only power cards are hidden. Selecting Windows exposes its latest reported metrics and power actions.

Windows and Mac browsers select their matching OS node automatically and cannot accidentally target the other desktop. Android remains the only client that can choose either computer.

If a running computer disappears, its task becomes **Interrupted**, the global queue pauses, and later tasks do not start. Check the CRM, enter a review note, then use **Resume After Review**. A queued task aimed at an offline computer can be reassigned from its device selector without losing its FIFO position.

## Git workflow on either computer

Before editing, run `git pull --ff-only`. Commit and push on the computer where you made the change. On the other computer, use Safe Sync & Start; it fetches and fast-forwards only when the checkout is clean. Dirty, ahead, or diverged checkouts are blocked rather than overwritten. The strict queue gate prevents different commits from claiming shared tasks.
