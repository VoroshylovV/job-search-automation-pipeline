# Registers a daily Windows Task Scheduler task for the pipeline.
# Run from the repo root (regular PowerShell, no admin rights needed):
#   powershell -ExecutionPolicy Bypass -File scripts\register_windows_task.ps1
# Custom time:  ... -File scripts\register_windows_task.ps1 -Time 12:30
param(
    [string]$Time = "12:00",   # local time; AFTER the chat auto-run (~11:40) so Step 5 has data to compare
    [string]$TaskName = "JobSearchPipeline"
)

$repo = Split-Path -Parent $PSScriptRoot
$bat = Join-Path $repo "scripts\run_pipeline.bat"
$vbs = Join-Path $repo "scripts\run_hidden.vbs"

# Run through wscript + run_hidden.vbs so no console window appears at $Time.
# Closing that window killed the run (Last Result -1073741510 = 0xC000013A).
$action   = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$vbs`"" -WorkingDirectory $repo
$trigger  = New-ScheduledTaskTrigger -Daily -At $Time
# StartWhenAvailable: if the PC was off/asleep at $Time, run as soon as it is back on.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 1)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Write-Host "Task '$TaskName' registered: daily at $Time -> $bat (hidden window)"
Write-Host "Test now: Start-ScheduledTask -TaskName $TaskName ; log: $repo\logs\scheduler.log"
