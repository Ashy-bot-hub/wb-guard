# WorkBuddy 多账号会话守卫（跨机器交付包）

## 这个包解决什么

WorkBuddy（本地库 `~/.workbuddy/workbuddy.db`）支持多账号切换。上游 `1.2.17` 自带跨账号复制（`autoCopy`）、切号后经 CDP 重载窗口、同步类操作后调 `conversations.ensureList()` 刷列表，本包不重复这些。

本包只做上游没做的三件事：

| 本包职责 | 上游为什么不管 |
| --- | --- |
| 转录**存储层合并**：同任务的各账号副本在内容可覆盖时并成同一 inode | 上游按 lineage 双向同步最新快照（`1.1.29`），但只处理带脏标记的会话，且复制即新 inode；库里已有的独立副本不会被回溯共享，也不会合并成一份物理文件 |
| 退役同账号重名副本 | 上游 `1.1.33` 只在显示层按账号 + lineage 折叠，库里仍是多行；本包把保留者之外的行退役掉 |
| 磁盘 / 库自愈（日志、备份、影子数据、工作空间登记） | 客户端日志无轮转，一天能写 300 MB |

净结论：**不造副本，只回收副本。**

## 交付范围

只包含真正让 WorkBuddy 多账号功能完善的脚本，不含一次性治理脚本。

```
session-sync/
├── wdconfig.py               路径与账号自动发现（无硬编码用户名）
├── wdrules.py                保留/清理规则的纯函数层（可单测，不碰磁盘）
├── wd_doctor.py              一键体检（计划任务/进程/库/会话/磁盘/数据增量，退出码分级）
├── wd_db_maintain.py         会话库体检与压缩（运行中拒绝 VACUUM）
├── wd_prune_backups.py       过期整库快照回收（删除前逐个 quick_check）
├── wd_shadow.py              会话附属数据回收（影子副本/诊断轨迹/编辑器历史）
├── wd_unify.py               转录存储层合并（内容可覆盖时共享 inode + 去重 + 状态纠正 + 清空壳）
├── wd_audit.py               体检「已标记」幽灵索引（counts() 供体检复用；--fix 需应用已退出）
├── wd_watch.py               守护主程序（切换检测 / 巡检 / 数据层维护）
├── wd_janitor.py             磁盘自愈（可再生日志与浏览器缓存，每 30 分钟）
├── wd_workspaces.py          清理无引用的时间戳工作空间登记
├── build_digest.py           按日期导出会话摘要（标题级元数据，只读）
├── wd_fs.py                  原子写与文件工具（被上面各脚本引用）
├── cdp.mjs                   单文件 CDP 客户端（截图、点按、按需手动刷新）
├── clean-meta-ghosts.bat     双击清理幽灵索引（自动挡掉「应用未退出」的情况）
├── tests/                    规则与行为单测（193 项，python -m unittest discover -s tests）
├── tools/build_dat.py        打成自解包 .dat（模板 + base64 载荷）
├── tools/dat_template.py     .dat 模板：解包 + 交互菜单
├── tools/RUN-WB-GUARD.bat    双击入口：自动找 Python 再调 dat
├── install.ps1 / install.bat          安装（注册计划任务）
├── uninstall.ps1 / uninstall.bat      卸载（停服务，不动数据）
├── uninstall-full.ps1 / .bat          连目录删除（需输入 DELETE 确认）
└── reinstall_workdaddy.ps1            重装客户端（默认只演算，加 -Run 才执行）
```

## 前置条件

| 项 | 要求 |
| --- | --- |
| 系统 | Windows 10/11 |
| Python | 3.9+（`install.ps1` 会自动找 `python.exe`，找不到则退回 `~/.workbuddy/binaries/python/versions`） |
| Node | 22+，仅 `cdp.mjs` 需要；缺失时治理链不受影响 |
| 目录 | 本包放在固定路径，不要放桌面/临时目录（计划任务记录绝对路径） |

## 单文件交付（.dat）

`tools/build_dat.py` 把本目录打成单个 `.dat`：模板 + base64 压缩载荷，运行时自解包并进菜单，不用先解压。

```bash
python tools/build_dat.py                  # 输出到上级目录 wb-guard.dat
python tools/build_dat.py --out D:\x.dat
```

把产物与 `tools/RUN-WB-GUARD.bat` 放同一目录，双击 bat 即可。bat 按「WorkBuddy 托管 Python → PATH 里的 `python` → `py -3` → 常见安装位置」依次找解释器，都没有就直说，不会闪退。

dat 首次运行解包到自身所在目录的 `wb-guard\`，之后复用同一目录。不带参数进菜单：

```
1 体检   2 磁盘清理   3 备份回收   4 转录合并   5 附属数据
6 工作空间   7 安装守护   8 卸载守护   9 清幽灵索引   0 退出
```

菜单里的「安装守卫」会把计划任务指向解包目录；本机若已从别处装过，任务会被改指到新目录，旧目录的守护随之失效 —— 菜单会先提示，要再确认一次。

## 安装

1. 复制整个目录到目标机器，例如 `D:\tools\workdaddy-guard`。
2. 完全退出 WorkBuddy（托盘也要退）。
3. 双击 `install.bat`，或在 PowerShell 里执行 `powershell -ExecutionPolicy Bypass -File install.ps1`。
4. 安装脚本做三件事：定位 Python/Node → 注册 `WorkDaddy-SyncGuard` 计划任务（AtLogOn 触发 + 每 15 分钟保活检查、失败重试 3 次、单实例、不限运行时长）→ 启动并打印 PID。

   机器上若还留着 `WorkDaddy-Guard` / `WorkDaddy-Timeline` 这两个任务名，安装脚本会先停掉并注销它们 —— 名字并存等于两个守护并发。
5. 重新打开 WorkBuddy，正常切一次账号验证。

## 验证

```powershell
# 守护是否在跑
Get-ScheduledTask -TaskName 'WorkDaddy-SyncGuard' | Select-Object TaskName, State

# 最近 20 行守护日志（应看到「检测到账号切换」）
Get-Content .\logs\guard.log -Tail 20

# 数据层体检：同账号重名应为 0
.\wd_unify.py
```

`wd_unify.py` 不加参数是预演，加 `--apply` 才写库。稳态应输出：跨账号任务组 N 个、可合并为同一份转录 0 条、内容已分叉 0 条、同账号内多余副本 0 条。

「内容已分叉」不为 0 是正常的：它表示同一任务在两边各自继续对话过。本包**不合并**这种副本（合并必丢一方内容），交给上游快照同步收敛；收敛成前缀关系后，下一轮自动并成一份。

## 日常运维

| 场景 | 操作 |
| --- | --- |
| 查看状态 | `python wd_doctor.py`（体检结论 + 不达标项，退出码 0/1/2） |
| 看守护日志 | `Get-Content .\logs\guard.log -Tail 30` |
| 手动重建血缘 | `python wd_unify.py --apply`（守护在切号后与发现重名时自动跑这一个；只合并内容可覆盖的副本） |
| 界面又出现重复 | 守护会自动退役同账号那份；纯渲染残留关掉重开会话列表，或用 `node cdp.mjs eval "location.reload()"` |
| 改切换等待时间 | 编辑 `wd_watch.py` 的 `SETTLE`（默认 20 秒，配合 `POLL=5`，最坏 25 秒完成） |
| 定时任务在别的账号看不到 | 上游 `official-automation-sync` 负责，等切号后自动补；本包**不插入副本** |
| 「已标记」仍然误报 | 完全退出 WorkBuddy，双击 `clean-meta-ghosts.bat`（运行中会被拒绝，退出码 3） |
| 磁盘又涨回来 | 守护每 30 分钟自动清，日志总量超 420 MB 时按最旧优先淘汰；手动跑 `python wd_janitor.py --apply` |
| 侧边栏工作空间太乱 | `python wd_workspaces.py --apply`（不改磁盘文件，可 `--undo`） |
| 任务重名且有空白条目 | `python wd_unify.py --prune-empty --hours 48 --apply` |
| 备份快照占空间 | `python wd_prune_backups.py` 预演，`--apply` 回收（每目录留最新 1 个、全局至少 2 个） |
| 磁盘总量仍在涨 | `python wd_shadow.py` 看是不是影子副本/轨迹/历史，`--apply` 回收（守护每 6 小时自动跑） |
| 查库健康 / 压缩 | `python wd_db_maintain.py` 看报告；压缩回收空闲页请**先完全退出客户端**，再执行 `python wd_db_maintain.py --vacuum`（运行中会拒绝，退出码 3） |
| 规则改完先自测 | `python -m unittest discover -s tests -v` |
| 停守护 | `Stop-ScheduledTask -TaskName 'WorkDaddy-SyncGuard'` |
| 恢复守护 | `Start-ScheduledTask -TaskName 'WorkDaddy-SyncGuard'` |

守护每轮轮询（5 秒）先做一次只读重名计数，发现同账号重名立即统一并刷新，不依赖切换事件；另每 6 小时无条件兜底一次。

## 磁盘自愈（wd_janitor.py）

WorkBuddy 的日志没有任何轮转机制，`logs/sandbox` 一天能写 300 MB 以上，单日会话日志 270 MB。手动清一次不到两小时就涨回来，所以清理做成守护的固定动作：**每 30 分钟**执行一次。

| 位置 | 规则 |
| --- | --- |
| `logs/sandbox/<日期>/` | 保留 1 天；当天目录只删 30 分钟未写入的文件 |
| 已封口的滚动日志 | 同前缀已有「序号更高且更新」的文件时，闲置 3 分钟即回收 |
| 同前缀封口文件份数 | 只留最新 1 份（`KEEP_SEALED_PER_STEM`）。分钟口径管不住份数：当天沙箱 318 MB 里，104 MB 是一份早滚走的 000 号文件 |
| `logs/<今天>/` 内部 | 当日目录超 220 MB 时，淘汰闲置 60 分钟以上的旧会话日志（活跃文件不动） |
| 删除失败 | 客户端对当日会话日志「写时开、写完关」，删除会随机撞上占用；失败后等 1.5 秒重试一次，仍失败才计为跳过 |
| `logs/<日期>/` | 保留 2 天 |
| `logs/*.old.log` | 直接删 |
| `edge-automation` 缓存 | Code Cache / Cache / GPUCache / 组件缓存 / Service Worker 缓存正文 / 视觉搜索模型 |
| 本包 `logs/*.log` | 超 4 MB 滚一代，只保留 `.1` |

**不碰**：`Cookies`、`Login Data`、`Web Data`、`Preferences`、`Extensions`、`IndexedDB`、`projects/`（会话转录）。被进程占用的文件删不掉就跳过，不重试、不提权。

```bash
python wd_janitor.py            # 只报告可回收多少
python wd_janitor.py --apply    # 立即清一次
```

## 会话附属数据回收（wd_shadow.py）

客户端每改一个文件，就在 `workspace/sessions/<会话>/modify_backup/` 下整份复制一遍，供界面「恢复上一版本」。这是逐次全量复制：一个 19 MB 的库改 9 次就是 171 MB，一段转录改 4 次又是 80 MB，**会话关闭后从不回收**。46 个会话累计 843 MB，是 `.workbuddy` 里最大的单一增长源。

三类数据同一性质 —— 都按「归属者」分目录，归属者消失后不会再被用到：

| 目录 | 分目录键 | 判废依据 | 实测可回收 |
| --- | --- | --- | --- |
| `workspace/sessions/<会话>/modify_backup` | 会话 | 安静超 6 小时 | 276 MB |
| `traces/<PID>` | 进程号 | PID 已不存在 | 82 MB |
| `file-history/<会话>` | 会话 | 安静超 6 小时 | 77 MB |

**不碰**：`projects/`（会话转录）、`blobs/`（内容寻址存储，可能被任意会话引用）、安静不足 6 小时的会话、仍然存活的 PID。删不掉的文件计数跳过。

```bash
python wd_shadow.py            # 预演：按类别列出项数与体积
python wd_shadow.py --apply    # 立即回收
python wd_shadow.py --json     # 机器可读
python wd_shadow.py --exclude-logs --apply   # 排除客户端日志
```

守护每 6 小时执行一次，体检里对应「附属数据」一项（拆成「已结束会话可在线回收」与「活跃会话须离线」两个数字）。该项合计超 **2 GB** 才告警、超 3 GB 算故障：活跃会话的这部分删了会被客户端重建，属于「必须存在」的正常占用（75 会话约 520 MB），阈值按磁盘余量定，不按会话数估。

### 要立刻回收：离线模式

在用的会话不受 6 小时规则约束，只能离线清。流程是「检测客户端是否退出 → 预演 → 确认 → 回收 → 复核」。等价命令：

```bash
python wd_shadow.py --force            # 预演，只读，客户端运行时也能看
python wd_shadow.py --force --apply    # 执行；客户端在运行会直接拒绝（退出码 3）
python wd_shadow.py --force --apply --limit 3   # 先拿 3 个小会话试手
```

`--force` 与普通 `--apply` 的差别只有两点：忽略 6 小时阈值；**连 `.modify_backup_meta` 索引一起删**。

## 日常维护（守护每 24 小时执行一次）

| 动作 | 脚本 | 说明 |
| --- | --- | --- |
| 清工作空间登记 | `wd_workspaces.py --apply` | 删除「无活跃会话引用 + 超 24 小时未打开」的时间戳目录登记，91 → 37 条；**不动磁盘文件** |
| 清空壳会话 | `wd_unify.py --prune-empty --hours 48 --apply` | 软删零内容且超 48 小时的空壳任务 |

两者都可用各自的 `--undo` 整批还原。

## 卸载

两个层次，按需要选：

### 只停服务（推荐，数据全留）

双击 `uninstall.bat`，三步：

| 步骤 | 动作 |
| --- | --- |
| 1 | 停计划任务（`WorkDaddy-SyncGuard`，以及 `WorkDaddy-Guard` / `WorkDaddy-Timeline`） |
| 2 | 按 pid 文件结束遗留进程，兼容文件名都认 |
| 3 | 注销这些任务 |

**它不删除任何文件**，也不动数据库与转录。随时可以重新 `install.bat` 恢复。

### 彻底删除（不可逆）

双击 `uninstall-full.bat`，先选 `d` 可以预演看清会删什么。它把下面五步做成程序：

| 步骤 | 动作 |
| --- | --- |
| 1 | 列出将删除的每一项与合计大小 |
| 2 | 停任务、注销任务、结束进程（含按命令行兜底） |
| 3 | `-Restore` 时用 `wd_unify --undo` 还原改过的会话（最多 5 批） |
| 4 | 客户端已退出时跑 `wd_shadow --force --apply` 回收副本 |
| 5 | 输入 `DELETE` 后删除目录 |

参数：

```powershell
.\uninstall-full.bat                 # 交互式：先看清单，输入 DELETE 才删
.\uninstall-full.bat                 # 选 d → 预演，不动任何文件
powershell -File uninstall-full.ps1 -Restore          # 删之前先还原会话
powershell -File uninstall-full.ps1 -KeepData         # 只删程序，留 logs/backups/注册表
powershell -File uninstall-full.ps1 -Yes -KeepData    # 跳过确认（自动化用）
```

顺序不能反：`--undo` 依赖 `backups/` 与 `lineage-registry.json`，目录一删就再也还原不了。所以要么加 `-Restore`，要么先手动跑 `--undo` 再卸载。

## 回滚

`wd_unify.py` 每次 `--apply` 前会把转录原件与该批 DB 备份到 `backups/<时间戳>/`，并自动只保留最近 3 批（每批约 30 MB）。需要回滚时：

```powershell
# 1. 先停守护，再退出 WorkBuddy
Stop-ScheduledTask -TaskName 'WorkDaddy-SyncGuard'

# 2. 查看有哪些批次
Get-ChildItem .\backups | Sort-Object Name

# 3. 把某批的 transcripts 与 db 拷回原位（路径见该批 manifest）
```

## 已知边界

- 只处理「本机已有账号之间」的对称，不改变 WorkBuddy 的云端账号逻辑。
- 任务级唯一键 = 规范化目录 + 标题。改过标题的同一任务会被视为新任务。
- 界面刷新交给 WorkDaddy：客户端 daemon 在账号切换时自行经 CDP 重载窗口（切号后约 0.5 秒），并在同步类操作后调用官方 `conversations.ensureList()` 刷列表。本包不做整页 reload，避免切号后连续白屏两次。需要手动刷新时执行 `node cdp.mjs eval "location.reload()"`。
- 由此带来的一点取舍：本包在切号后写入的去重结果（SQLite）不会被客户端即时看见，需等下一次客户端刷新或手动刷新才显示。
- `wd_audit.py --fix` 会剔除 `meta.json` 里指向已软删会话的「已标记」索引。该操作建议在 WorkBuddy 完全退出时执行，本包不自动调用。脚本内的进程检查会直接拒绝（退出码 3），不会写出半截 `meta.json`。为省掉手敲路径，同目录提供 `fix_audit.cmd`：先打印只读报告，再执行 `--fix`，最后停住显示退出码；运行前须完全退出 WorkBuddy（含托盘图标）。
- 体检的 12 项覆盖「计划任务 / 进程 / 库完整性 / 库结构 / 复制索引残留 / 活跃会话 / 定时任务 / 日志 / 备份 / 会话附属数据 / 封口日志 / 数据增量」，加上五个按会话只增不减的目录（`changes-detail`、`blobs`、`cache`、`file-tree-manifests`、`traces`）。这五项合计超 2 GB 或任一单项超 1 GB 才告警 —— 阈值刻意放宽，只在明显失控时说话。不监控 `projects`（转录是核心资产，本就该大）与 `binaries`/`plugins`（程序本体按版本整体替换，不随使用累积）。
- `wd_unify.py` 没有 argparse：不带参数直接执行，`--help` 会被当成普通参数忽略。其余脚本都支持 `-h`。
- `wd_unify.py --apply` 带写库互斥：守护的定时任务正在写时，手动执行会打印「另一进程正在写库」并返回 3，等几十秒重试即可。

## 为什么不合并成单入口

包里有 14 个可执行脚本（13 个 Python + 1 个 Node），刻意不合并成一个 `wd.py <子命令>`：合并每天只能省约 12 秒解释器启动（占全天 CPU 0.01%），却会牺牲「单步可单独执行」和「一个脚本崩了不影响其它步骤」这两条。
