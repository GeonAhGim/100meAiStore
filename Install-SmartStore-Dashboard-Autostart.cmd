@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install-control-dashboard-startup.ps1"
if errorlevel 1 (
    echo Could not install smart_store dashboard auto-start.
    pause
    exit /b 1
)
echo Installed. The control server starts at logon and is revived within 5 minutes if it stops.
endlocal
