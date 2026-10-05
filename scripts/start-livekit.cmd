@echo off
cd /d "%~dp0.."
if not exist logs mkdir logs
".venv\Scripts\python.exe" -u "scripts\run-service.py" livekit >> "logs\livekit.out.log" 2>&1
