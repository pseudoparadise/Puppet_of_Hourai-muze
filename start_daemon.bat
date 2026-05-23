@echo off
set PYTHON=C:\Users\23807\AppData\Local\Programs\Python\Python314\python.exe
set ROOT=%~dp0

cd /d "%ROOT%"

start "MusicSync" %PYTHON% "%ROOT%music_sync_server.py"
echo [music] Sync server starting...
timeout /t 3 /nobreak > nul

:loop
cd /d "%ROOT%"
%PYTHON% polling_loop.py
echo [DSphantom] Exited (%ERRORLEVEL%) at %date% %time% -- Restarting in 10s...
timeout /t 10 /nobreak > nul
goto loop
