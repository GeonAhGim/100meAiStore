param([int]$Port = 8877)

$ErrorActionPreference = "Continue"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = (Get-Command python -ErrorAction Stop).Source
$ControlDir = Join-Path $ProjectRoot "data\control"
$Log = Join-Path $ControlDir "server.log"
$ExitLog = Join-Path $ControlDir "server.exit"
$createdNew = $false
$mutex = [Threading.Mutex]::new($true, "Local\100meAiStoreControlServerWatchdog", [ref]$createdNew)
if (-not $createdNew) { exit 0 }

New-Item -ItemType Directory -Path $ControlDir -Force | Out-Null

try {
    while ($true) {
        $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
        if (-not $listener) {
            # The server runs its workers in-process, so it is started hidden (no console
            # window to close by accident) and its output is kept for post-mortems.
            $server = "`"$Python`" -m smart_store_control.server --host 127.0.0.1 --port $Port >> `"$Log`" 2>&1"
            $proc = Start-Process -FilePath "cmd.exe" -ArgumentList "/d /s /c `"$server`"" `
                -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru -Wait
            Add-Content -LiteralPath $ExitLog -Value ("{0} exit={1} (watchdog restarts)" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $proc.ExitCode)
        }
        Start-Sleep -Seconds 10
    }
} finally {
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
