@echo off
chcp 65001 >nul
setlocal

set PROJECT_DIR=%~dp0
set TASK_NAME=DSphantomDaemon
set SCRIPT="%PROJECT_DIR%start_daemon.bat"
set STARTUP_LINK="%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\DSphantom.lnk"

echo === DSphantom 开机自启设置 ===
echo.

:: Method 1: Windows 计划任务 (登录时触发, 延迟30s等网络就绪)
echo [1/2] 注册计划任务 "%TASK_NAME%"...
schtasks /Create /SC ONLOGON /TN "%TASK_NAME%" /TR "%SCRIPT%" /RL HIGHEST /F /DELAY 0000:30 2>nul
if %ERRORLEVEL% EQU 0 (
    echo    [OK] 计划任务已创建 (登录后延迟30s启动)
) else (
    echo    [跳过] 计划任务可能已存在, 或权限不足
)

:: Method 2: Startup 文件夹快捷方式 (双保险)
echo [2/2] 创建启动文件夹快捷方式...
if exist %STARTUP_LINK% del %STARTUP_LINK% >nul
powershell -Command "$s=(New-Object -COM WScript.Shell).CreateShortcut(%STARTUP_LINK%);$s.TargetPath=%SCRIPT%;$s.WorkingDirectory='%PROJECT_DIR%';$s.WindowStyle=7;$s.Save()" 2>nul
if exist %STARTUP_LINK% (
    echo    [OK] 快捷方式已创建: %STARTUP_LINK%
) else (
    echo    [失败] 快捷方式创建失败。
)

echo.
echo === 设置完成 ===
echo 下次开机后, DSphantom 守护进程将自动启动。
echo 验证: 重启后运行 python phantom_status.py 或查看 .daemon.pid
echo.
echo 如需移除自启, 运行: remove_autostart.bat
pause
