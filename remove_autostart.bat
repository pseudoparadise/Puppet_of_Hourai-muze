@echo off
chcp 65001 >nul
setlocal

set TASK_NAME=DSphantomDaemon
set STARTUP_LINK="%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\DSphantom.lnk"

echo === 移除 DSphantom 开机自启 ===
echo.

:: 移除计划任务
echo [1/2] 移除计划任务 "%TASK_NAME%"...
schtasks /Delete /TN "%TASK_NAME%" /F 2>nul
if %ERRORLEVEL% EQU 0 (
    echo    [OK] 计划任务已移除
) else (
    echo    [跳过] 计划任务不存在或已移除
)

:: 移除启动文件夹快捷方式
echo [2/2] 移除启动文件夹快捷方式...
if exist %STARTUP_LINK% (
    del %STARTUP_LINK% >nul
    echo    [OK] 快捷方式已删除
) else (
    echo    [跳过] 快捷方式不存在
)

echo.
echo === 移除完成 ===
pause
