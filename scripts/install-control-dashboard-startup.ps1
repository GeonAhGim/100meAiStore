param([switch]$Uninstall)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Startup = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup"
$Target = Join-Path $Startup "Start-SmartStore-ControlDashboard.cmd"
$Watchdog = Join-Path $ProjectRoot "scripts\run-control-server-watchdog.ps1"

if ($Uninstall) {
    Remove-Item -LiteralPath $Target -Force -ErrorAction SilentlyContinue
    Write-Output "Removed smart_store startup entry."
    exit 0
}

New-Item -ItemType Directory -Path $Startup -Force | Out-Null
# The entry lives outside the project, so it names the watchdog by absolute path.
$Launcher = @(
    "@echo off",
    "start `"`" powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Watchdog`""
)
Set-Content -LiteralPath $Target -Value $Launcher -Encoding ASCII
Write-Output "Installed smart_store startup entry: $Target"
