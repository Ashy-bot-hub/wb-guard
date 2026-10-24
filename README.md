# WB-GUARD

WorkBuddy / WorkDaddy 桌面端的多账号会话治理工具集。

| 目录 | 内容 |
|---|---|
| `session-sync/` | 一个 Windows 计划任务驱动的治理链：转录血缘统一、日志与影子副本回收、体检、库维护、备份保留策略。说明见 `session-sync/README.md` |
| `panel-patches/` | 针对上游安装文件的一次性补丁（目前 1 项：同源副本级联删除 + 两步确认提示，已适配 `1.2.18`），带校验、备份与回滚脚本。说明见各子目录 README |

## 与上游 WorkDaddy 的职责边界

上游 `babygoton/WorkDaddy` 的定位是功能增强，走 CDP 注入而非改动安装包（`scripts/inject.js` 注入渲染进程，`daemon.js` 起本地 HTTP 服务与 CDP 通道）。下表按能力划分，避免两边重复开发。

| 能力 | 上游 | 本仓库 |
|---|---|---|
| 切会话黑屏（`removeChild` 摘除官方导航） | `1.2.17` 用 `setOfficialConversationHistoryNavigationHidden()`：属性标记 + CSS 隐藏，不动 DOM 节点 | **不做**。本包不碰渲染层 |
| 切号后跨账号复制会话 | `autoCopy` + 会话页「切换账号时自动复制」 | **不做**。本包再造副本会产出「副本的副本」 |
| 定时任务跨账号同步 | `scripts/official-automation-sync.js` + 映射表 | **不做** |
| 会话转录共享 | 三层机制：`1.1.29` 按 lineage 内最新快照双向同步全部成员（内容与元数据）、`1.1.33` 显示层按账号 + lineage 折叠重复会话、`1.1.46` 删除时按 lineage 级联删其他账号副本。复制文件（新 inode），全目录仅 `sentry-report.js` 用 `fs.linkSync` | `wd_unify.py`：只做存储层 —— 内容可覆盖时合并 inode、同账号重名退役、空壳软删、status 归位。**不做内容与元数据同步**（交给上游），内容分叉的副本一律不动 |
| 定时任务运行记录展示 | 面板「自动化」页已有运行日志（含清除、仅清已结束） | **不做**。本包不注入看板 |
| CDP 客户端 | `daemon.js` 内 63 处 `Runtime.evaluate` + `POST /api/inject`，无命令行入口 | `cdp.mjs`：手动排障（`eval` / `shot` / `click` / `run`） |
| 磁盘治理（日志、浏览器缓存、影子副本、整库快照） | 未覆盖：全目录检索 `vacuum`、`doctor`、`diskUsage`、`cleanupLogs` 均 0 命中；`modify_backup` 自 `1.2.104` 起在同步时被跳过，只在删除会话时随 id 精确删 | `wd_janitor.py`、`wd_shadow.py`、`wd_prune_backups.py`（上游 `pruneSyncBackups` 只管自己的 `session-sync-backups`，30 天） |
| 体检与库维护 | 未覆盖 | `wd_doctor.py`（12 项，含复制索引残留）、`wd_db_maintain.py` |
| 复制索引残留 | `autoCopy.sessionIndex` 只在上游自己的 add / remove / move 路径维护；绕开这些路径的软删会留下「索引命中但会话已不存在」的条目 | `wd_audit.py` 检出并剔除。注意：本包 `wd_unify.py --prune-empty` 正是绕开路径的软删，两者互为配套 |
| 侧边栏登记与摘要导出 | `workspaces` 表在客户端代码中无任何写操作 | `wd_workspaces.py`、`build_digest.py` |

> 该表以 2026-10-21 本机安装的 WorkDaddy `1.2.17` 客户端 `scripts/` 目录（68 个 js）全量检索为准，并用上游仓库 README 与 `daemon.js` 版本注记（记到 `1.2.141`）交叉核对。上游改版后需重核。
>
> 2026-10-24 复核 `1.2.18`（tag 内 `DAEMON_VERSION` = `1.2.68`，官方安装包打包后写作 `1.2.18`）：`scripts/` 下仅 `automation-model.js`、`daemon.js`、`inject.js`、`lib.js`、`persona.js` 五个文件有改动，主题为凭据刷新加密落盘、登录页账号切换、空会话同步、persona 备份根规范化、Windows 重启窗口可见性；上表涉及的能力边界未发生变化，`1.2.18` 另新增了面板会话页静默刷新（原先由本仓库补丁提供）。

## 上游没有的能力：留存依据

这七项都在上游整目录检索不到对应实现，且每一项都能在体检报告里读到它挡住的数字（2026-10-21 本机实测）。

| 脚本 | 挡住什么 | 实测数字 | 判定 |
|---|---|---|---|
| `wd_janitor.py` | 客户端日志无轮转：`sandbox` 与主日志只增不减，浏览器缓存同样 | 日志合计 361 MB（在写 146 MB + 可回收 214 MB） | 保留。清理周期 30 分钟，取消后一天可涨 300 MB 以上 |
| `wd_shadow.py` | 已结束会话的影子副本、`traces`、`file-history` 没人回收 | 已结束会话 91 MB 在线可回收 + 活跃会话 365 MB（须退出客户端后 `--force`） | 保留。上游只跳过这些目录、不清理 |
| `wd_prune_backups.py` | 备份目录只增不减（unify 每批约 30 MB + 客户端安装前快照） | 备份 115 MB，保留策略为每目录最新 1 个、全局至少 2 个 | 保留。上游 `pruneSyncBackups` 只管自家的 `session-sync-backups`（30 天），不管整库快照 |
| `wd_doctor.py` | 十余项状态分散在多个脚本，出问题时靠人逐个跑 | 12 项，单条命令给出「哪项不达标 + 数字」 | 保留。守护每小时落一条到 `guard.log`，是唯一的时间线判据 |
| `wd_db_maintain.py` | 库结构与空闲页无人看，`VACUUM` 无入口 | 库 11.1 MB、空闲页 0.0% | 保留。当前无事可做，但结构漂移只在这里可见 |
| `wd_audit.py` | `autoCopy.sessionIndex` 绕开上游维护路径后留下幽灵条目 | 幽灵 4 条、失效副本标记 15 / 18 | 保留，并已纳入体检（每小时可见）。剔除只能在客户端退出后做 |
| `wd_workspaces.py` | `workspaces` 表在客户端代码里零写操作，删会话后登记行残留 | 28 条登记、当前 0 条孤儿 | 保留但降为低优先：每日一次，当前已清干净，孤儿会随删会话再现 |

`build_digest.py` 不属于治理链，是只读的会话摘要导出（供跨设备同步用），与上游功能无交集；`cdp.mjs` 是手动排障入口 —— 上游有通道但无命令行等价物。

## 文件结构

```
.
├── README.md
├── panel-patches/            # 安装包补丁（sessions-delete-refresh：同源副本级联删除）
└── session-sync/             # 治理脚本与守护，说明见 session-sync/README.md
```

## session-sync 一览

由**一个** Windows 计划任务（`WorkDaddy-SyncGuard`）驱动。

| 子系统 | 主要文件 | 作用 |
|---|---|---|
| 看门狗 | `wd_watch.py` | 5 秒轮询账号切换；按周期跑清理、备份回收、每日治理、每小时体检 |
| CDP 连接 | `cdp.mjs`、`wdconfig.py` | 端口按「环境变量 → 客户端 `cdp-port.json` → 默认 9222」三级解析 |
| 体检 | `wd_doctor.py`、`wd_audit.py` | 12 项检查，覆盖任务、进程、库结构、复制索引、磁盘、数据一致性 |
| 数据维护 | `wd_db_maintain.py`、`wd_janitor.py`、`wd_fs.py`、`wd_shadow.py`、`wd_unify.py` | 数据库维护、磁盘清理、转录存储层合并 |
| 安装/卸载 | `install.ps1`、`uninstall.ps1`、`uninstall-full.ps1`、`reinstall_workdaddy.ps1` | 注册计划任务、卸载、整套重装 |
| 会话摘要 | `build_digest.py` | 只读会话库，输出标题级摘要 |
| 单元测试 | `tests/`（18 个文件，193 项） | `python -m unittest discover -s tests -p "test_*.py"` |
| 单文件交付 | `tools/build_dat.py`、`tools/RUN-WB-GUARD.bat` | 打成自解包 `.dat` + 双击启动器，不需先解压 |

本存档的取舍：

- **仅收代码与文档**，不收运行时产物：`backups/`（数据库快照）、`logs/`、`digest/`（会话摘要）、`*.pid`、`__pycache__/`、`lineage-registry.json`（含会话与账号标识）。
- **已脱敏**：`build_digest.py` 的账号由当日数据自动识别，脚本内不含任何账号标识；数据库路径为 `~/.workbuddy/workbuddy.db`。源码与文档中的账号标识统一为「账号 A / B / C / D」。
- 因此本目录是**可读的源码存档**，不是可直接运行的部署副本；在原机上运行时以 `C:\Users\<用户>\WorkBuddy\_session-sync` 的实际文件为准。

## 维护

- `session-sync` 用 `install.ps1` 注册计划任务、`uninstall.ps1` 停用（不动数据）、`uninstall-full.ps1` 连目录一起清。
- 守护是否正常，用 `wd_doctor.py` 判定，不依赖人工记忆。
- 客户端升级后若上游动了会话复制或自动化同步的实现，先重核上表再决定是否新增脚本。
- 客户端升级会覆盖 `scripts/` 下的文件，`panel-patches/` 的补丁随之失效：升级后先跑 `python apply.py --dry-run`，上下文不匹配就说明需要重新适配补丁，不要直接 `apply`。

## 文档索引

| 文档 | 内容 |
|---|---|
| `session-sync/README.md` | 守护脚本的安装与日常运维 |
