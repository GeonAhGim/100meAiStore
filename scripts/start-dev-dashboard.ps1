$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = (Get-Command python -ErrorAction Stop).Source
$Port = 8767

$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if (-not $listener) {
    $arguments = @(
        "-m", "smart_store_aios.dev_dashboard",
        "--project-root", $ProjectRoot,
        "--host", "127.0.0.1",
        "--port", "$Port"
    )
    Start-Process -FilePath $Python -ArgumentList $arguments `
        -WorkingDirectory $ProjectRoot -WindowStyle Hidden

    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        Start-Sleep -Milliseconds 250
        if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
            break
        }
    }
}

Start-Process "http://127.0.0.1:$Port"
