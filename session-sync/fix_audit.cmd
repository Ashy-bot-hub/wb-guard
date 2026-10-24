@echo off
REM One-click runner for wd_audit.py --fix.
REM wd_audit refuses to write meta.json while workbuddy.exe is alive,
REM so quit WorkBuddy completely (including the tray icon) before running this.
setlocal
cd /d "%~dp0"
set "PY=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.14.3\python.exe"
if not exist "%PY%" (
  echo ERROR: python not found at %PY%
  pause
  exit /b 2
)
echo ===== 1/2 read-only report =====
"%PY%" wd_audit.py
echo.
echo ===== 2/2 fix =====
"%PY%" wd_audit.py --fix
set RC=%ERRORLEVEL%
echo.
echo Exit code: %RC%
if "%RC%"=="3" echo Refused: WorkBuddy is still running. Quit it fully and run again.
echo.
pause
