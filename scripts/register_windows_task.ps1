# Registers the pipeline task in Windows Task Scheduler.
# Run from the repo root (regular PowerShell, no admin rights needed):
#   powershell -ExecutionPolicy Bypass -File scripts\register_windows_task.ps1
#
# SCHEDULE IS IN Europe/Warsaw TIME (not UTC, not the PC time zone). The task wakes every
# 15 minutes; `main.py --scheduled` runs the pipeline only when it is already
# SCHEDULE_RUN_AT_LOCAL (config.py, default 12:00 Warsaw) and no scheduled run has started
# today (Warsaw date). DST changes and the PC time zone therefore do not matter, and
# StartWhenAvailable-like behaviour is kept: if the PC was off at 12:00, the first wake-up
# after it is back on runs the pipeline. To change the time, edit SCHEDULE_RUN_AT_LOCAL.
param(
    [string]$TaskName = "JobSearchPipeline",
    [int]$WakeEveryMinutes = 15
)

$repo = Split-Path -Parent $PSScriptRoot
$bat = Join-Path $repo "scripts\run_pipeline.bat"
$vbs = Join-Path $repo "scripts\run_hidden.vbs"

# Run through wscript + run_hidden.vbs so no console window appears.
# Closing that window killed the run (Last Result -1073741510 = 0xC000013A).
$action   = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$vbs`"" -WorkingDirectory $repo
$trigger  = New-ScheduledTaskTrigger -Once -At "00:00" -RepetitionInterval (New-TimeSpan -Minutes $WakeEveryMinutes) -RepetitionDuration (New-TimeSpan -Days 9999)
# IgnoreNew: a wake-up while a run is still going does nothing.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 1)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Write-Host "Task '$TaskName' registered: wakes every $WakeEveryMinutes min -> $bat --scheduled (hidden window)."
Write-Host "Runs at the time set by SCHEDULE_RUN_AT_LOCAL in config.py, Europe/Warsaw time."
Write-Host "Test now (manual run, ignores the schedule gate): python main.py ; log: $repo\logs\scheduler.log"
