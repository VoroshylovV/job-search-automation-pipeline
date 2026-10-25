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
#
# -CheckOnly registers a SEPARATE one-shot test task "<TaskName>-CheckOnly" that runs
# `main.py --scheduled --check-only` (prints the schedule decision to logs\scheduler.log, exit code 0,
# does NOT run the pipeline). Start it with: Start-ScheduledTask -TaskName JobSearchPipeline-CheckOnly
param(
    [string]$TaskName = "JobSearchPipeline",
    [int]$WakeEveryMinutes = 15,
    [switch]$CheckOnly
)

$repo = Split-Path -Parent $PSScriptRoot
$bat = Join-Path $repo "scripts\run_pipeline.bat"
$vbs = Join-Path $repo "scripts\run_hidden.vbs"

if ($CheckOnly) {
    $checkName = "$TaskName-CheckOnly"
    $checkAction = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$vbs`" --check-only" -WorkingDirectory $repo
    $checkTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1)
    Register-ScheduledTask -TaskName $checkName -Action $checkAction -Trigger $checkTrigger -Force | Out-Null
    Write-Host "Task '$checkName' registered (one-shot, runs in ~1 min; or: Start-ScheduledTask -TaskName $checkName)."
    Write-Host "Read the schedule decision: Get-Content $repo\logs\scheduler.log -Tail 6 ; Last Result must be 0."
    return
}

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
