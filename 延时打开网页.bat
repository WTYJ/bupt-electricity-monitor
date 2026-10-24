@echo off
rem 延时5秒后用默认浏览器打开电费监控页面
ping -n 6 127.0.0.1 >nul
start "" "http://localhost:5100/"
exit
