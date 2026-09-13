@echo off
setlocal
cd /d "%~dp0"
echo ============================================================
echo   Hermes Google Antigravity Plugin - 1-Click Installer
echo ============================================================
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
echo.
pause
