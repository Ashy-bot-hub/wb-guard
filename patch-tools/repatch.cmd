@echo off
rem WorkDaddy 补丁重打入口
rem 不带参数 = 只预览；带 apply = 落盘（会先备份并做语法校验）
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

if not exist "%SCRIPT_DIR%apply-patches.mjs" (
  echo 缺少 apply-patches.mjs，请与本脚本放在同一目录。
  pause
  exit /b 2
)

if /i "%~1"=="apply" goto DOAPPLY

"%NODE%" "%SCRIPT_DIR%apply-patches.mjs"
set "CODE=%ERRORLEVEL%"
echo.
echo 若上面显示待打或待改，再运行本脚本并加参数 apply。
if not "%CODE%"=="0" goto FAILED
exit /b 0

:DOAPPLY
"%NODE%" "%SCRIPT_DIR%apply-patches.mjs" --apply
set "CODE=%ERRORLEVEL%"
echo.
if not "%CODE%"=="0" goto FAILED
echo 已完成。请退出并重开 WorkDaddy，再跑一次升级检查。
ping -n 4 127.0.0.1 >nul
exit /b 0

:FAILED
echo 有未完成项，请按上面的明细处理。
pause
exit /b %CODE%
