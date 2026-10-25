@echo off
rem WorkDaddy x WorkBuddy 升级体检 双击入口
rem 只读检查：读版本、读补丁标记、连渲染进程取 DOM 计数，不改任何文件
setlocal EnableExtensions
chcp 936 >nul 2>&1
set "SCRIPT_DIR=%~dp0"

set "NODE="
if exist "%USERPROFILE%\AppData\Local\Programs\WorkDaddy\scripts\runtime\node\node.exe" set "NODE=%USERPROFILE%\AppData\Local\Programs\WorkDaddy\scripts\runtime\node\node.exe"
if not defined NODE (
  for /d %%d in ("%USERPROFILE%\.workbuddy\binaries\node\versions\*") do (
    if not defined NODE if exist "%%d\node.exe" set "NODE=%%d\node.exe"
  )
)
if not defined NODE set "NODE=node"

if not exist "%SCRIPT_DIR%upgrade-check.mjs" (
  echo 缺少 upgrade-check.mjs，请与本脚本放在同一目录。
  pause
  exit /b 2
)

"%NODE%" "%SCRIPT_DIR%upgrade-check.mjs"
set "CODE=%ERRORLEVEL%"
echo.
if "%CODE%"=="0" (
  echo 体检通过，无需处理。
  ping -n 4 127.0.0.1 >nul
  exit /b 0
)
echo 有未通过项，请按上面的明细处理。
pause
exit /b %CODE%
