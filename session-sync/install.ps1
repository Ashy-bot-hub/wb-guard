# WorkDaddy Guard - installer (ASCII only, paths resolved at runtime)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
Write-Host "[1/4] package root: $root"

# --- locate python ---
$py = $null
$cmd = Get-Command python.exe -ErrorAction SilentlyContinue
if ($cmd) { $py = $cmd.Source }
if (-not $py) {
    $cands = Get-ChildItem -Path (Join-Path $env:USERPROFILE '.workbuddy\binaries\python\versions') `
        -Directory -ErrorAction SilentlyContinue |
        Where-Object { Test-Path (Join-Path $_.FullName 'python.exe') } |
        Sort-Object Name
    if ($cands) { $py = Join-Path $cands[-1].FullName 'python.exe' }
}
if (-not $py) {
    Write-Host "[ERROR] python not found. Install Python 3.9+ or WorkBuddy runtime." -ForegroundColor Red
    exit 1
}
$pyw = $py -replace 'python\.exe$', 'pythonw.exe'
if (-not (Test-Path $pyw)) { $pyw = $py }
Write-Host "[2/4] python: $py"

# --- locate node (needed by the manual CDP client cdp.mjs) ---
$node = $null
$cmd = Get-Command node.exe -ErrorAction SilentlyContinue
if ($cmd) { $node = $cmd.Source }
if (-not $node) {
    $cands = Get-ChildItem -Path (Join-Path $env:USERPROFILE '.workbuddy\binaries\node\versions') `
        -Directory -ErrorAction SilentlyContinue |
        Where-Object { Test-Path (Join-Path $_.FullName 'node.exe') } |
        Sort-Object Name
    if ($cands) { $node = Join-Path $cands[-1].FullName 'node.exe' }
}
if ($node) {
    $ver = & $node -v
    Write-Host "[3/4] node: $node ($ver)"
    if ([int]($ver.TrimStart('v').Split('.')[0]) -lt 22) {
        Write-Host "[WARN] node < 22: cdp.mjs (manual CDP client) may not work." -ForegroundColor Yellow
    }
} else {
    Write-Host "[3/4] node not found: cdp.mjs (manual CDP client) unavailable." -ForegroundColor Yellow
}

# --- register scheduled tasks ---
Write-Host "[4/4] registering scheduled tasks"

# 任务名三段式保持一致：注册、体检（wd_doctor.TASKS）、README 命令用的是同一个。
# 机器上若已存在 'WorkDaddy-Guard'，只注册新名会在它之外再挂一个任务 ——
# 两个任务各自 IgnoreNew、互不知道对方存在，于是两个守护并发跑 unify 与
# janitor，同时改写同一份 guard.pid。先把这两个名字清掉，再注册。
$GuardTask = 'WorkDaddy-SyncGuard'
# 'WorkDaddy-Timeline' 是卡片注入守护的任务名，本包不提供该功能。留着它会让
# 任务每 15 分钟指向一个不存在的启动脚本、失败一次。
foreach ($legacy in @('WorkDaddy-Guard', 'WorkDaddy-Timeline')) {
    if (Get-ScheduledTask -TaskName $legacy -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $legacy -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $legacy -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host "  removed legacy task: $legacy" -ForegroundColor Yellow
    }
}

$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew -Hidden

# 两个触发器，缺一不可：
#   AtLogOn        —— 登录/重启后立即启动；
#   Once + 重复    —— 保活。进程被外部结束（任务管理器、系统更新、内存压力）时
#                     退出码不算失败，任务的「失败重试 3 次」不介入，只能靠它。
# 两个坑实测过（2026-10-19）：
#   1. 把 Repetition 挂在 LogonTrigger 上不会重复 —— XML 里看得到 Interval，
#      调度器却不排下一次，NextRun 恒空，属于「看着有配置、实际不工作」；
#   2. StartBoundary 放在过去会被判成「重复窗口已过」，Missed 一直涨而任务从不运行。
# 所以用独立的 TimeTrigger、开始时间放未来一分钟，Duration 留空表示无限重复。
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn
$keepAliveTrigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(1)) `
    -RepetitionInterval (New-TimeSpan -Minutes 15)
$triggers = @($logonTrigger, $keepAliveTrigger)

$guardAction = New-ScheduledTaskAction -Execute $pyw `
    -Argument ('"' + (Join-Path $root 'wd_watch.py') + '"') -WorkingDirectory $root
Register-ScheduledTask -TaskName $GuardTask -Action $guardAction `
    -Trigger $triggers -Settings $settings -Force | Out-Null

Start-ScheduledTask -TaskName $GuardTask
Start-Sleep -Seconds 5

Get-ScheduledTask -TaskName 'WorkDaddy-*' |
    Select-Object TaskName, State | Format-Table -AutoSize | Out-String | Write-Host

foreach ($f in @('guard.pid', 'guard-timeline.pid')) {
    $p = Join-Path $root $f
    if (Test-Path $p) {
        $id = (Get-Content $p -ErrorAction SilentlyContinue)
        $alive = if ($id) { [bool](Get-Process -Id $id -ErrorAction SilentlyContinue) } else { $false }
        Write-Host ("  {0} = {1} alive={2}" -f $f, $id, $alive)
    }
}
# --- CDP check (manual cdp.mjs needs it) ---
# Port is chosen by the client since 1.2.17; read its cdp-port.json instead of assuming 9222.
$cdpPort = $null
if ($env:WD_CDP_PORT) { $cdpPort = [int]$env:WD_CDP_PORT }
if (-not $cdpPort) {
    $pf = Join-Path $env:APPDATA 'WorkDaddy\cdp-port.json'
    if (Test-Path $pf) {
        try { $cdpPort = [int](Get-Content $pf -Raw | ConvertFrom-Json).port } catch { $cdpPort = $null }
    }
}
if (-not $cdpPort) { $cdpPort = 9222 }
$cdpOk = $false
try {
    $r = Invoke-WebRequest -Uri ("http://127.0.0.1:" + $cdpPort + "/json/version") -TimeoutSec 3 -UseBasicParsing
    $cdpOk = ($r.StatusCode -eq 200)
} catch { $cdpOk = $false }
if ($cdpOk) {
    Write-Host ("[OK] CDP " + $cdpPort + " reachable: cdp.mjs can attach.")
} else {
    Write-Host ("[WARN] CDP " + $cdpPort + " not reachable.") -ForegroundColor Yellow
    Write-Host "       Data sync still works. cdp.mjs is unavailable until CDP is open."
    Write-Host "       Fix: close WorkBuddy, then launch it with:"
    Write-Host ('         "C:\Program Files\WorkBuddy\WorkBuddy.exe" --remote-debugging-port=' + $cdpPort)
}

Write-Host "`nDone. Logs: $root\logs" -ForegroundColor Green
