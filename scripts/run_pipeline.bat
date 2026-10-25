@echo off
REM Pipeline run for Windows Task Scheduler (wakes every 15 min; main.py --scheduled decides
REM whether it is time: >= SCHEDULE_RUN_AT_LOCAL in Europe/Warsaw and not yet run today).
REM Register the task with scripts\register_windows_task.ps1 (see README, "Планування запуску").
REM Extra args are passed on (e.g. --check-only: print the schedule decision, exit 0, no run).
REM Exit code: 0 ok / not due, 2 alarm (see pipeline/alarm.py), 1 crash.
cd /d "%~dp0.."
if not exist logs mkdir logs
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
call venv\Scripts\activate.bat
python main.py --scheduled %* >> logs\scheduler.log 2>&1
exit /b %ERRORLEVEL%
