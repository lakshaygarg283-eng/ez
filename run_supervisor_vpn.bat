@echo off
title Supervisor-VPN (Mullvad WireGuard)
color 0a
cd /d "%~dp0"
echo =========================================================
echo       SUPERVISOR-VPN (Mullvad WireGuard Automation)
echo =========================================================
echo.
python supervisor_vpn.py
pause
