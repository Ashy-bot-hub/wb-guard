# WorkDaddy Guard - uninstaller
# 做三件事：停任务 / 杀进程 / 注销任务；顺带还原 WB-GUARD 补丁。
# 不动数据库与转录 —— 这两个目录里的东西是用户的资产，卸载器不该碰。
$ErrorActionPreference = 'SilentlyContinue'
$root = $PSScriptRoot

# 三个名字都清：机器上可能还留着 'WorkDaddy-Guard' 与 'WorkDaddy-Timeline'。
# 只清一个会出现「卸载后守护还在跑」—— 这是最糟的一种残留：用户以为卸干净了，
# 实际后台每 5 秒还在动数据。
$tasks = @('WorkDaddy-SyncGuard', 'WorkDaddy-Guard', 'WorkDaddy-Timeline')

Write-Host "[1/4] stopping scheduled tasks"
foreach ($t in $tasks) { Stop-ScheduledTask -TaskName $t }

Write-Host "[2/4] stopping processes"
foreach ($f in @('guard.pid', 'guard-watch.pid', 'timeline\timeline.pid', 'guard-timeline.pid')) {
    $p = Join-Path $root $f
    if (Test-Path $p) {
        $id = Get-Content $p
        if ($id) { Stop-Process -Id $id -Force }
        Remove-Item $p -Force
    }
}

Write-Host "[3/4] removing scheduled tasks"
foreach ($t in $tasks) { Unregister-ScheduledTask -TaskName $t -Confirm:$false }

# 补丁装在 WorkBuddy 的安装目录里，不在本包目录 —— 不还原的话，卸载后客户端
# 仍然跑着改过的 inject.js，而用户以为已经恢复原样。
Write-Host "[4/4] restoring WB-GUARD patch (if installed)"
$bakFile = $null
$cands = @(
    (Join-Path $env:LOCALAPPDATA "Programs\WorkDaddy"),
    (Join-Path $env:LOCALAPPDATA "WorkDaddy"),
    (Join-Path $env:APPDATA "WorkDaddy"),
    (Join-Path $env:ProgramFiles "WorkDaddy\resources")
)
foreach ($c in $cands) {
    if ([string]::IsNullOrWhiteSpace($c)) { continue }
    if (-not (Test-Path -LiteralPath $c)) { continue }
    $hit = Get-ChildItem -LiteralPath $c -Filter 'inject.js.bak-wbguard' -Recurse -Depth 4 -File -ErrorAction SilentlyContinue |
           Select-Object -First 1
    if ($hit) { $bakFile = $hit; break }
}
if ($bakFile) {
    $inject = Join-Path $bakFile.DirectoryName 'inject.js'
    if (-not (Test-Path -LiteralPath $inject)) {
        Write-Host "  skip: inject.js not found next to backup"
    } else {
        # 客户端升级会整体覆盖 inject.js，此时备份与当前文件对不上；
        # 直接覆盖等于把客户端回退到备份对应的构建，必须先比对再决定。
        $cur = [System.IO.File]::ReadAllText($inject, [System.Text.Encoding]::UTF8)
        $bak = [System.IO.File]::ReadAllText($bakFile.FullName, [System.Text.Encoding]::UTF8)
        $matchPrefix = ($cur.Length -ge $bak.Length) -and
            [string]::Equals($cur.Substring(0, $bak.Length), $bak, [System.StringComparison]::Ordinal)
        $matchSuffix = ($cur.Length -ge $bak.Length) -and
            [string]::Equals($cur.Substring($cur.Length - $bak.Length), $bak, [System.StringComparison]::Ordinal)
        if ($matchPrefix -or $matchSuffix) {
            Copy-Item -LiteralPath $bakFile.FullName -Destination $inject -Force
            Write-Host ("  restored: " + $inject)
            Write-Host ("  backup kept: " + $bakFile.FullName)
        } else {
            Write-Host "  skip: backup does not match current inject.js (客户端已升级，还原会造成降级)" -ForegroundColor Yellow
            Write-Host ("  backup kept: " + $bakFile.FullName)
        }
    }
} else {
    Write-Host "  no patch backup found (补丁未装或已还原)"
}

Write-Host "`nRemoved. The database and transcripts were never modified by this uninstaller." -ForegroundColor Green
Write-Host "To undo session changes:  python wd_unify.py --undo"
Write-Host "To reclaim disk before deleting the folder:  python wd_shadow.py --force --apply  (先退出 WorkBuddy)"
Write-Host ""
Write-Host "本目录内的文件不会被删除。确认不再需要后可整目录删除："
Write-Host ("  Remove-Item -LiteralPath '" + $root + "' -Recurse -Force")
