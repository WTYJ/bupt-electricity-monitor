@echo off
title 电费自动监控系统
echo ==================================
echo   电费自动监控系统
echo   服务就绪后自动打开网页
echo   停止：关闭本窗口，或双击 停止电费监控.bat
echo ==================================
echo.

cd /d "%~dp0"
python app.py --open-browser
set "monitor_exit=%errorlevel%"
if "%monitor_exit%"=="0" exit /b 0

echo.
echo 程序已退出（原因请查看上方日志或 electric_monitor.log）
pause
exit /b %monitor_exit%
