@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-dev-dashboard.ps1"
if errorlevel 1 (
    echo Dashboard could not start. See the error above.
    pause
    exit /b 1
)
endlocal
