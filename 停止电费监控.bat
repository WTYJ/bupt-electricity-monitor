@echo off
title 电费监控系统 - 停止
echo ==================================
echo   停止电费监控系统
echo ==================================
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop_monitor.ps1"
echo.
pause
