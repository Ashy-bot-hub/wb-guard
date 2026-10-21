@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "DAT=%~dp0wb-guard.dat"

if not exist "%DAT%" (
  echo [x] wb-guard.dat not found next to this file.
  pause
  exit /b 1
)

rem 1) WorkBuddy managed Python (last dir by name = newest version)
set "PY="
if exist "%USERPROFILE%\.workbuddy\binaries\python\versions" (
  for /d %%D in ("%USERPROFILE%\.workbuddy\binaries\python\versions\*") do (
    if exist "%%D\python.exe" set "PY=%%D\python.exe"
  )
)
if defined PY goto found

rem 2) python on PATH
where python >nul 2>&1 && set "PY=python"
if defined PY goto found

rem 3) py launcher
where py >nul 2>&1 && set "PY=py -3"
if defined PY goto found

rem 4) usual install locations
for %%V in (314 313 312 311 310 39) do (
  if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
)
if defined PY goto found

echo [x] Python 3 not found.
echo     Install from python.org ^(tick "Add python.exe to PATH"^), then run this file again.
pause
exit /b 1

:found
if /i "%PY%"=="python" (
  python "%DAT%" %*
) else if /i "%PY%"=="py -3" (
  py -3 "%DAT%" %*
) else (
  "%PY%" "%DAT%" %*
)

if errorlevel 1 pause
