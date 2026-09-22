@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install-control-dashboard-startup.ps1"
if errorlevel 1 (
    echo Could not install smart_store dashboard auto-start.
    pause
    exit /b 1
)
echo Installed. The dashboard starts at the next Windows logon.
endlocal
