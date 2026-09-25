param([switch]$Uninstall)

# Keeps the 8877 control server alive through a Task Scheduler task.
#
# A Startup-folder entry runs once per logon, so anything that killed the
# watchdog afterwards (on 2026-09-23 a shell restart at 17:41 took the watchdog
# and server down with the per-user services) left the pool dead until the
# next logon. The task runs the watchdog at logon and every five minutes; the
# watchdog's mutex makes an extra run exit at once, so a dead watchdog is back
# within five minutes. Task Scheduler owns the process, so it does not share
# the fate of the shell, a terminal or a tool session. Per-user, no elevation.

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$TaskName = "SmartStore-ControlServer"
$Watchdog = Join-Path $ProjectRoot "scripts\run-control-server-watchdog.ps1"
$LegacyEntry = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup\Start-SmartStore-ControlDashboard.cmd"

# The Startup entry is replaced by the task either way.
Remove-Item -LiteralPath $LegacyEntry -Force -ErrorAction SilentlyContinue

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Output "Removed smart_store control server task."
    exit 0
}

$user = "$env:USERDOMAIN\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute "powershell.exe" -WorkingDirectory $ProjectRoot `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Watchdog`""
$atLogon = New-ScheduledTaskTrigger -AtLogOn -User $user
$every5 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -Hidden
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($atLogon, $every5) -Settings $settings `
    -Principal $principal -Description "smart_store 8877 control server watchdog (logon + every 5 minutes)" -Force | Out-Null
Write-Output "Installed task $TaskName (at logon and every 5 minutes)."
