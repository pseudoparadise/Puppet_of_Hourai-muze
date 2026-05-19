@echo off
cd /d "%~dp0"
echo [DSphantom Daemon] Starting polling loop...
echo [DSphantom Daemon] Auto-restart enabled — will recover from crashes.

:loop
python polling_loop.py
echo [DSphantom Daemon] Polling loop exited with code %ERRORLEVEL% at %date% %time%
echo [DSphantom Daemon] Restarting in 10 seconds...
timeout /t 10 /nobreak > nul
goto loop
