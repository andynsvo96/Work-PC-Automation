param(
    [string]$ServiceName = "svc:automation-control"
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
& $tailscale serve --bg "--service=$ServiceName" --https=443 http://127.0.0.1:5123
if ($LASTEXITCODE -ne 0) { throw "Tailscale Serve setup failed." }
& $tailscale serve status
Write-Host "Tailscale Service configured. Open the service URL on the Android tablet and enter the app PIN."
