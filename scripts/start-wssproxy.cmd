@echo off
cd /d "%~dp0.."
if not exist logs mkdir logs
".venv\Scripts\python.exe" -u "scripts\run-service.py" wssproxy >> "logs\wssproxy.out.log" 2>&1
