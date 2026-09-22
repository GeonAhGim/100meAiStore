param([switch]$NoBrowser)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Port = 8767

$url = "http://127.0.0.1:$Port"
function Test-DashboardReady {
    try {
        $page = Invoke-WebRequest -Uri "$url/" -UseBasicParsing -TimeoutSec 2
        return $page.StatusCode -eq 200 -and $page.Content.Contains("/api/dev-dashboard")
    } catch { return $false }
}

if (-not (Test-DashboardReady)) {
    $watchdog = Join-Path $PSScriptRoot "run-dev-dashboard-watchdog.ps1"
    Start-Process powershell.exe -ArgumentList @("-NoProfile", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass",
        "-File", ('"' + $watchdog + '"')) -WorkingDirectory $ProjectRoot -WindowStyle Hidden
    $deadline = (Get-Date).AddSeconds(30)
    while (-not (Test-DashboardReady)) {
        if ((Get-Date) -ge $deadline) {
            throw "Dashboard did not start. Check Python installation and port $Port."
        }
        Start-Sleep -Milliseconds 500
    }
}

if (-not $NoBrowser) { Start-Process $url }
Write-Output "Dashboard ready: $url"
