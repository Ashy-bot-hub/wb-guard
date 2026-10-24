param(
    [string]$Version = "1.2.18",
    [switch]$Run,
    [string]$DownloadDir = (Join-Path $env:USERPROFILE "WorkBuddy\_wd_reinstall")
)

# WorkDaddy 卸载重装器（默认只演算，加 -Run 才真正执行）
#
# 背景：客户端自带更新（apply-update.ps1）会整体覆盖 inject.js，
#       因此重装只在文件损坏或需要降级时才有意义。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File .\reinstall_workdaddy.ps1            # 只打印计划
#   powershell -ExecutionPolicy Bypass -File .\reinstall_workdaddy.ps1 -Run       # 真正执行
#
# 注意：卸载会关闭 WorkDaddy / WorkBuddy，当前会话随之中断。

$ErrorActionPreference = "Stop"

$installDir = Join-Path $env:LOCALAPPDATA "Programs\WorkDaddy"
$uninst = Join-Path $installDir "unins000.exe"
$setup = Join-Path $DownloadDir ("WorkDaddy-Setup-" + $Version + ".exe")
$url = "https://github.com/babygoton/WorkDaddy/releases/download/$Version/WorkDaddy-Setup-$Version.exe"

Write-Host "目标版本 : $Version"
Write-Host "安装目录 : $installDir"
Write-Host "安装包   : $setup"

$reg = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*' -ErrorAction SilentlyContinue |
       Where-Object { $_.DisplayName -match 'WorkDaddy' } | Select-Object -First 1
if ($reg) { Write-Host ("已装版本 : " + $reg.DisplayVersion) } else { Write-Host "已装版本 : 未检测到注册表项" }

if (-not $Run) {
    Write-Host ""
    Write-Host "计划（未执行）："
    Write-Host "  1) 下载 $url"
    Write-Host "  2) 结束 WorkDaddy / WorkBuddy 进程"
    Write-Host "  3) 静默卸载：$uninst /VERYSILENT /SUPPRESSMSGBOXES /NORESTART"
    Write-Host "  4) 静默安装：$setup /VERYSILENT /SUPPRESSMSGBOXES /NORESTART"
    Write-Host "  5) 校验注册表版本并运行 check-wb-guard.ps1"
    Write-Host ""
    Write-Host "加 -Run 才执行。"
    exit 0
}

New-Item -ItemType Directory -Force -Path $DownloadDir | Out-Null
if (-not (Test-Path $setup)) {
    Write-Host "下载中..."
    $proxy = $env:HTTPS_PROXY
    $curlArgs = @("-L", "--fail", "-o", $setup, $url)
    if ($proxy) { $curlArgs = @("-x", $proxy) + $curlArgs }
    & curl.exe @curlArgs
    if (-not (Test-Path $setup)) { throw "下载失败: $url" }
}
Write-Host ("安装包大小: " + (Get-Item $setup).Length + " 字节")

foreach ($n in 'WorkDaddy', 'WorkDaddyLauncher', 'WorkBuddy') {
    Get-Process -Name $n -ErrorAction SilentlyContinue | ForEach-Object {
        Write-Host ("结束进程: " + $_.ProcessName + " (" + $_.Id + ")")
        $_ | Stop-Process -Force -ErrorAction SilentlyContinue
    }
}
Start-Sleep -Seconds 3

if (Test-Path $uninst) {
    Write-Host "静默卸载..."
    Start-Process -FilePath $uninst -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART' -Wait
    Start-Sleep -Seconds 3
} else {
    Write-Host "警告：未找到卸载器 $uninst" -ForegroundColor Yellow
}

Write-Host "静默安装..."
Start-Process -FilePath $setup -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART' -Wait
Start-Sleep -Seconds 5

$reg2 = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*' -ErrorAction SilentlyContinue |
        Where-Object { $_.DisplayName -match 'WorkDaddy' } | Select-Object -First 1
if ($reg2) { Write-Host ("安装后版本: " + $reg2.DisplayVersion) } else { Write-Host "安装后未检测到注册表项" -ForegroundColor Red }

$check = Join-Path (Split-Path -Parent $PSScriptRoot) "wb-guard\scripts\check-wb-guard.ps1"
if (Test-Path $check) {
    & powershell -ExecutionPolicy Bypass -File $check
    Write-Host ("check-wb-guard 退出码: " + $LASTEXITCODE)
}

Write-Host ""
Write-Host "完成。请手动启动 WorkDaddy 恢复会话列表。"
