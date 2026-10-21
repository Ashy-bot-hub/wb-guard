@echo off
rem WorkDaddy Guard - one click install
rem Content is ASCII only; all paths are resolved at runtime.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
echo.
pause
