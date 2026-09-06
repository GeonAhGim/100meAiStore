param([switch]$OpenBrowser)

$ErrorActionPreference = "Continue"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = (Get-Command python -ErrorAction Stop).Source
$Port = 8767
$createdNew = $false
$mutex = [Threading.Mutex]::new($true, "Local\100meAiStoreDevDashboardWatchdog", [ref]$createdNew)
if (-not $createdNew) { exit 0 }

if ($OpenBrowser) { Start-Process "http://127.0.0.1:$Port" -ErrorAction SilentlyContinue }

try {
    while ($true) {
        $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
        if (-not $listener) {
            $arguments = @("-m", "smart_store_aios.dev_dashboard", "--project-root", $ProjectRoot,
                "--host", "127.0.0.1", "--port", "$Port")
            Start-Process -FilePath $Python -ArgumentList $arguments -WorkingDirectory $ProjectRoot -WindowStyle Hidden
        }
        Start-Sleep -Seconds 5
    }
} finally {
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
