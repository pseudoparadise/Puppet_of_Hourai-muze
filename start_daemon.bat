@echo off
cd /d "%~dp0"

echo [DSphantom Daemon] Starting...
echo.

REM ── 启动音乐同步服务（后台静默运行）──
start "" /B python music_sync_server.py
echo [music] Sync server started on http://127.0.0.1:8766
echo.

echo [DSphantom Daemon] Starting polling loop (auto-restart enabled)...
echo.

:loop
python polling_loop.py
echo [DSphantom Daemon] Polling loop exited with code %ERRORLEVEL% at %date% %time%
echo [DSphantom Daemon] Restarting in 10 seconds...
timeout /t 10 /nobreak > nul
goto loop
