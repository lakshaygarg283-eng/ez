@echo off
title Supervisor-ADB (Cellular Airplane Mode)
color 0e
cd /d "%~dp0"
echo =========================================================
echo       SUPERVISOR-ADB (Mobile Airplane Mode Automation)
echo =========================================================
echo.
python supervisor_adb.py
pause
