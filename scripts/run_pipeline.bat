@echo off
REM Daily pipeline run for Windows Task Scheduler.
REM Register the task with scripts\register_windows_task.ps1 (see README, "Планування запуску").
cd /d "%~dp0.."
if not exist logs mkdir logs
call venv\Scripts\activate.bat
python main.py >> logs\scheduler.log 2>&1
