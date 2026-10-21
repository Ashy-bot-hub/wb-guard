@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo ==========================================================
echo   清理 autoCopy 幽灵索引（"已标记"误判的根源）
echo   前提：WorkBuddy 必须完全退出（含右下角托盘图标）
echo ==========================================================
echo.

tasklist /FI "IMAGENAME eq WorkBuddy.exe" 2>nul | find /I "WorkBuddy.exe" >nul
if not errorlevel 1 goto RUNNING

set "PY="
where python >nul 2>&1
if not errorlevel 1 set "PY=python"
if defined PY goto GO
where py >nul 2>&1
if not errorlevel 1 set "PY=py -3"
if defined PY goto GO

echo [中止] 找不到 python。请安装 Python 3.9+ 或把 python 加入 PATH。
pause
exit /b 1

:RUNNING
echo [中止] WorkBuddy 仍在运行。
echo 请右键托盘图标退出，或在任务管理器结束 WorkBuddy.exe 后重试。
echo.
pause
exit /b 1

:GO
%PY% wd_audit.py --fix
echo.
echo 完成。若要回滚：把 backups\meta-pre-audit-*.json 复制回
echo   %%APPDATA%%\WorkDaddy\meta.json
echo.
pause
