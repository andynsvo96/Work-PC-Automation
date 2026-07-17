# macOS and Android control-board setup

The two computers run the same Git commit, but keep credentials and machine settings in their own OS keychains. Supabase owns the single FIFO queue. Tailscale provides private HTTPS access; the app PIN is a second lock.

## 1. Prepare GitHub and Windows

1. Commit and push the finished implementation from Windows.
2. Run `python setup_app_security.py create --output app-security-transfer.json`.
3. Create separate Windows and Mac users in Supabase Authentication. Run `supabase/migrations/001_shared_queue.sql` in the SQL editor.
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

Copy the Windows `config.py` values into the Mac's local `config.py`; keep `config.py` uncommitted. Metrics and System Power remain disabled on macOS with “Windows only.”

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

Install Tailscale on Windows, macOS, and Android; sign all three into the same tailnet. Configure the same stable Service on both computers:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_tailscale_windows.ps1
```

```zsh
./setup_tailscale_mac.sh
```

The tailnet administrator must authorize `svc:automation-control` for both computers and the tablet. Tailscale Serve stays private to the tailnet and resumes after reboot when configured in background mode. The Flask server listens only on `127.0.0.1` in Tailscale mode.

## 4. Cut over safely

1. Stop new local runs and wait for the old Windows in-memory queue to be empty.
2. Push the final commit and run Safe Sync & Start on Windows.
3. On the Mac, let the LaunchAgent run Safe Sync & Start or launch `Sync & Start Mac.command` once.
4. Confirm both computers show the identical commit and queue protocol in Settings.
5. Submit one harmless dry run from Windows, then another from the Mac. Confirm the second remains queued until the first finishes.
6. Open the Tailscale Service URL on Android, enter the PIN, and repeat the dry-run test.

In Chrome on the Android tablet, open the menu and choose **Install app** (or **Add to Home screen**). Use the **Control target** selector in the header when an action must run on a specific computer. Selecting the Mac disables Metrics and System Power with “Windows only”; selecting Windows exposes its latest reported metrics and targets power actions to that PC.

If a running computer disappears, its task becomes **Interrupted**, the global queue pauses, and later tasks do not start. Check the CRM, enter a review note, then use **Resume After Review**. A queued task aimed at an offline computer can be reassigned from its device selector without losing its FIFO position.

## Git workflow on either computer

Before editing, run `git pull --ff-only`. Commit and push on the computer where you made the change. On the other computer, use Safe Sync & Start; it fetches and fast-forwards only when the checkout is clean. Dirty, ahead, or diverged checkouts are blocked rather than overwritten. The strict queue gate prevents different commits from claiming shared tasks.
