param([switch]$Uninstall)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Startup = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup"
$Target = Join-Path $Startup "Start-SmartStore-ControlDashboard.cmd"
$Source = Join-Path $ProjectRoot "Start-SmartStore-ControlDashboard.cmd"

if ($Uninstall) {
    Remove-Item -LiteralPath $Target -Force -ErrorAction SilentlyContinue
    Write-Output "Removed smart_store startup entry."
    exit 0
}

New-Item -ItemType Directory -Path $Startup -Force | Out-Null
Copy-Item -LiteralPath $Source -Destination $Target -Force
Write-Output "Installed smart_store startup entry: $Target"
