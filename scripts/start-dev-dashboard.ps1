$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Port = 8767

$watchdog = Join-Path $PSScriptRoot "run-dev-dashboard-watchdog.ps1"
Start-Process powershell.exe -ArgumentList @("-NoProfile", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass",
    "-File", $watchdog, "-OpenBrowser") -WorkingDirectory $ProjectRoot -WindowStyle Hidden
