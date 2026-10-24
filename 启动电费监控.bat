@echo off
title 电费自动监控系统
echo ==================================
echo   电费自动监控系统
echo   启动5秒后自动打开网页
echo   停止：关闭本窗口，或双击 停止电费监控.bat
echo ==================================
echo.

cd /d "%~dp0"

if not exist "config.py" (
    echo [错误] 未找到 config.py，请先填写配置
    pause
    exit /b 1
)

python -c "import sys; s=open('config.py',encoding='utf-8').read(); sys.exit(0 if ('你的学号' not in s and '你的邮箱@qq.com' not in s) else 1)"
if %errorlevel% neq 0 (
    echo [提示] config.py 还没有填写你的信息！
    echo 请编辑 config.py 填入学号、密码、宿舍信息后再启动
    echo 也可以运行: python setup_config.py 使用配置向导
    pause
    exit /b 1
)

rem 已有实例在跑就不重复启动，只定时补开网页
python -c "import socket,sys; s=socket.socket(); sys.exit(1 if s.connect_ex(('127.0.0.1',5100))==0 else 0)"
if %errorlevel% equ 1 (
    echo [提示] 服务已经在运行了，5秒后为你打开网页...
    start "打开网页" /min "%~dp0延时打开网页.bat"
    ping -n 4 127.0.0.1 >nul
    exit /b 0
)

rem 独立小窗口5秒后打开网页（与服务启动并行，不互相阻塞）
start "打开网页" /min "%~dp0延时打开网页.bat"
echo 正在启动...
python app.py

echo.
echo 程序已退出（原因请查看上方日志或 electric_monitor.log）
pause
