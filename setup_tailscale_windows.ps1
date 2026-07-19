param(
    [string]$ServiceName = "svc:automation-control",
    [string]$PeerUrl = ""
)

$ErrorActionPreference = "Stop"
$tailscale = (Get-Command tailscale.exe -ErrorAction SilentlyContinue).Source
if (-not $tailscale) {
    $candidate = Join-Path $env:ProgramFiles "Tailscale\tailscale.exe"
    if (Test-Path -LiteralPath $candidate) { $tailscale = $candidate }
}
if (-not $tailscale) {
    throw "Tailscale CLI was not found. Install Tailscale and sign in first."
}

& $tailscale status | Out-Null
# Device-specific HTTPS endpoint used only for authenticated clipboard peer
# requests. Unlike the shared Service URL, this always reaches this computer.
& $tailscale serve --bg --https=8443 http://127.0.0.1:5123
if ($LASTEXITCODE -ne 0) { throw "Tailscale clipboard endpoint setup failed." }
& $tailscale serve --bg "--service=$ServiceName" --https=443 http://127.0.0.1:5123
if ($LASTEXITCODE -ne 0) { throw "Tailscale Serve setup failed." }
& $tailscale serve status
Write-Host "Tailscale dashboard Service and device-specific clipboard endpoint configured."
Write-Host "Use this computer's device DNS URL with port 8443 as the peer URL in the Mac config.py."
if ($PeerUrl) {
    $scriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
    $virtualPython = Join-Path $scriptDirectory ".venv\Scripts\python.exe"
    $pythonCommand = if (Test-Path -LiteralPath $virtualPython) { $virtualPython } else { "python" }
    & $pythonCommand (Join-Path $scriptDirectory "configure_clipboard.py") $PeerUrl
    if ($LASTEXITCODE -ne 0) { throw "Clipboard peer configuration failed." }
}
