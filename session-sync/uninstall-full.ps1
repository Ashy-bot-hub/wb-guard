# WorkDaddy Guard - 彻底卸载
#
# 把 README「卸载」章节的四步做成程序：停服务 → 还原会话（可选）→ 回收磁盘 → 删目录。
# 与 uninstall.ps1 的分工：
#   uninstall.ps1      只停服务，不动文件（保守，随时可恢复）
#   uninstall-full.ps1 连目录一起删（不可逆，必须显式确认）
#
# 参数
#   -Restore     删除前用 wd_unify --undo 还原改过的会话（每批一次，最多 5 批）
#   -KeepData    保留 logs/、backups/、*-registry.json，只删程序
#   -Yes         跳过交互确认（自动化用；仍然会打印删除清单）
#   -DryRun      只打印将要做什么，不执行任何写操作
param(
    [switch]$Restore,
    [switch]$KeepData,
    [switch]$Yes,
    [switch]$DryRun
)

$ErrorActionPreference = 'SilentlyContinue'
$root = $PSScriptRoot
$tasks = @('WorkDaddy-SyncGuard', 'WorkDaddy-Guard', 'WorkDaddy-Timeline')

# 数据类文件：-KeepData 时保留。卸载器不该替用户决定「这些备份还要不要」。
$dataDirs = @('logs', 'backups')
$dataFiles = @('lineage-registry.json', 'mirror-registry.json', 'mirror-sessions-registry.json',
               'workspace-registry.json', 'accounts.json', 'guard.pid', 'guard-watch.pid')

function Say($msg) { Write-Host $msg }
function Step($n, $t) { Write-Host ""; Write-Host "[$n] $t" -ForegroundColor Cyan }

function Get-Targets {
    $items = @()
    Get-ChildItem -LiteralPath $root -Force | ForEach-Object {
        if ($_.PSIsContainer) {
            if ($KeepData -and ($dataDirs -contains $_.Name)) { return }
            $items += $_
        } else {
            if ($KeepData -and ($dataFiles -contains $_.Name)) { return }
            $items += $_
        }
    }
    return $items
}

function Get-SizeMB($items) {
    $b = 0
    foreach ($i in $items) {
        if ($i.PSIsContainer) {
            $b += (Get-ChildItem -LiteralPath $i.FullName -Recurse -File -Force -ErrorAction SilentlyContinue |
                   Measure-Object -Property Length -Sum).Sum
        } else {
            $b += $i.Length
        }
    }
    if (-not $b) { return 0 }
    return [math]::Round($b / 1MB, 1)
}

Say ("WorkDaddy Guard 彻底卸载   目录: " + $root)
if ($DryRun) { Say "（预演模式：只打印，不改任何东西）" }

# ---------------------------------------------------------------- 1. 清单
Step '1/5' '将要删除的内容'
$targets = Get-Targets
$totalMB = Get-SizeMB $targets
foreach ($t in $targets) {
    $tag = if ($t.PSIsContainer) { '目录' } else { '文件' }
    Say ("  {0,-4} {1}" -f $tag, $t.Name)
}
Say ("  合计 {0} 项 / {1} MB" -f $targets.Count, $totalMB)
if ($KeepData) {
    Say ("  保留（-KeepData）：" + (($dataDirs + $dataFiles) -join ', '))
}

# ---------------------------------------------------------------- 2. 停服务
Step '2/5' '停止并注销计划任务、结束进程'
foreach ($t in $tasks) {
    if ($DryRun) { Say ("  [预演] 停止并注销任务 " + $t); continue }
    Stop-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $t -Confirm:$false -ErrorAction SilentlyContinue
    $still = Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
    Say ("  " + $t + ": " + $(if ($still) { "仍存在（可能需要管理员权限）" } else { "已移除" }))
}

foreach ($f in @('guard.pid', 'guard-watch.pid', 'timeline\timeline.pid', 'guard-timeline.pid')) {
    $p = Join-Path $root $f
    if (-not (Test-Path $p)) { continue }
    $id = (Get-Content $p -ErrorAction SilentlyContinue | Select-Object -First 1)
    if (-not $id) { continue }
    if ($DryRun) { Say ("  [预演] 结束进程 " + $id + " (" + $f + ")"); continue }
    Stop-Process -Id $id -Force -ErrorAction SilentlyContinue
    Say ("  进程 " + $id + " (" + $f + "): 已结束")
}

# 兜底：按命令行匹配（pid 文件丢失时仍能收干净）
$left = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='node.exe'" |
        Where-Object { $_.CommandLine -like "*$root*" }
foreach ($pr in $left) {
    if ($DryRun) { Say ("  [预演] 结束残留进程 " + $pr.ProcessId); continue }
    Stop-Process -Id $pr.ProcessId -Force -ErrorAction SilentlyContinue
    Say ("  残留进程 " + $pr.ProcessId + ": 已结束")
}

# ---------------------------------------------------------------- 3. 还原会话
Step '3/5' '还原改过的会话（可选）'
$hasUnify = Test-Path (Join-Path $root 'wd_unify.py')
if (-not $Restore) {
    Say "  跳过。需要还原请加 -Restore（会用 backups/ 里的批次记录把会话改回去）"
} elseif (-not $hasUnify) {
    Say "  跳过：目录里没有 wd_unify.py"
} else {
    $py = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $py) { $py = (Get-Command python3 -ErrorAction SilentlyContinue).Source }
    if (-not $py) {
        Say "  跳过：找不到 python，无法执行 --undo"
    } else {
        Push-Location $root
        for ($i = 1; $i -le 5; $i++) {
            if ($DryRun) { Say ("  [预演] python wd_unify.py --undo（第 " + $i + " 批）"); break }
            $out = & $py 'wd_unify.py' '--undo' 2>&1 | Out-String
            if ($out -match '登记为空' -or $out -match '无法撤销') { Say ("  已无更多批次（共还原 " + ($i - 1) + " 批）"); break }
            Say ("  第 " + $i + " 批：" + (($out -split "`n" | Where-Object { $_.Trim() } | Select-Object -First 1)))
        }
        Pop-Location
    }
}

# ---------------------------------------------------------------- 4. 回收磁盘
Step '4/5' '回收会话副本（需要 WorkBuddy 已退出）'
$client = Get-Process workbuddy, workdaddy -ErrorAction SilentlyContinue
if ($client) {
    Say "  WorkBuddy 正在运行，跳过（在线删会被客户端立刻重建，白做）。"
    Say "  想回收请退出客户端后单独跑：python wd_shadow.py --force --apply"
} elseif (-not (Test-Path (Join-Path $root 'wd_shadow.py'))) {
    Say "  跳过：目录里没有 wd_shadow.py"
} elseif ($DryRun) {
    Say "  [预演] python wd_shadow.py --force --apply"
} else {
    $py = (Get-Command python -ErrorAction SilentlyContinue).Source
    if ($py) {
        Push-Location $root
        $out = & $py 'wd_shadow.py' '--force' '--apply' 2>&1 | Out-String
        Pop-Location
        $line = ($out -split "`n" | Where-Object { $_ -match '已回收' } | Select-Object -First 1)
        Say ("  " + $(if ($line) { $line.Trim() } else { "没有可回收项" }))
    }
}

# ---------------------------------------------------------------- 5. 删目录
Step '5/5' '删除目录'
if ($DryRun) {
    Say ("  [预演] 删除 " + $targets.Count + " 项 / " + $totalMB + " MB，然后尝试删除目录本身")
    Say "预演结束，未做任何修改。"
    return
}

if (-not $Yes) {
    Say ""
    Say "这一步不可逆。" -ForegroundColor Yellow
    Say ("确认删除 " + $root + " 下的 " + $targets.Count + " 项（" + $totalMB + " MB）？")
    $ans = Read-Host "输入 DELETE 继续，其它任意输入取消"
    if ($ans -ne 'DELETE') { Say "已取消，未删除任何文件。"; return }
}

# 当前目录若在待删目录内会导致删除失败，先跳出去
Set-Location $env:TEMP

$failed = @()
foreach ($t in $targets) {
    Remove-Item -LiteralPath $t.FullName -Recurse -Force -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $t.FullName) { $failed += $t.Name }
}

# 目录本身（-KeepData 时里面还有数据，会失败，这是预期）
Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue
if (Test-Path -LiteralPath $root) {
    $rest = (Get-ChildItem -LiteralPath $root -Force -ErrorAction SilentlyContinue).Count
    Say ("目录仍存在，剩余 " + $rest + " 项" + $(if ($KeepData) { "（-KeepData 保留的数据）" } else { "（多为被占用的文件，退出客户端后重试）" }))
} else {
    Say "目录已删除。"
}

if ($failed.Count) {
    Say ("以下项目被占用，未能删除：" + ($failed -join ', '))
    Say "退出 WorkBuddy / 关闭编辑器后重跑本脚本即可。"
}

Say ""
Say "卸载完成。数据库与转录从未被本脚本修改；会话改动是否还原取决于是否用了 -Restore。"
