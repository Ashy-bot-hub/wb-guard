@echo off
chcp 65001 >nul
setlocal
set PS=powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall-full.ps1"
echo.
echo  ============================================================
echo   WorkDaddy Guard - 彻底卸载
echo ============================================================
echo.
echo   直接回车 = 交互式卸载（会先列清单，输入 DELETE 才删）
echo   输入 d   = 预演，只看会删什么，不动任何文件
echo.
set /p MODE=选择 [回车/d]:
if /i "%MODE%"=="d" (
    %PS% -DryRun
) else (
    %PS%
)
echo.
pause
