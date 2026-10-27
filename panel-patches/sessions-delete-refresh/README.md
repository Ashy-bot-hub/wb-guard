# 补丁：同源副本级联删除 + 两步确认提示

适用对象：WorkDaddy 桌面端 `1.2.18`（面板 `5.8.3`），改动 `scripts/inject.js` 与 `scripts/daemon.js`。
1.2.17 版的旧补丁（面板侧刷新 + 副本指纹展开）见本文件历史版本，已被本版取代。

版本常量有两处口径，不要混用：GitHub tag `1.2.18` 里 `DAEMON_VERSION` 写作 `1.2.68`，
而官方安装包在打包时把它改写成 tag 号，装到本机后读到的值是 `1.2.18`
（`DAEMON_BUILD_ID` 同步为 `release-1.2.18-login-page-persona-switch`）。
两个值都已列入 `apply.py` 的可接受清单；实测安装包与 tag 的差异只有这两行常量。

## 一、适配基线

本补丁对应 WorkDaddy `1.2.18`（面板 `5.8.3`）。1.2.17 时期的适配对照与当时的现象记录
已归档到 `_archive/README-1.2.17-沿革.md`，不作为现行依据。

现行补丁**不含**面板侧的静默刷新、定时对账、静默加载这三类改动 —— 上游 1.2.18 已内置，
重复实现已删除；删除结果提示也已回退成上游原样。留在本补丁里的只有上游没有的能力，
逐项见下节。

## 二、补丁内容

### `scripts/daemon.js`（服务端；重启守护进程后生效）

`POST /api/sessions/delete`：在原有 lineage 展开之后，再按
`LOWER(RTRIM(REPLACE(cwd,'\','/'),'/')) + COALESCE(title,'') + CAST(created_at AS INTEGER)`
指纹展开兄弟行，一并纳入 `matchedIds`、规则清理与文件删除。

两个实现约束（为兼容上游测试契约，属于刻意选择）：

- 首个查询保持 `SELECT id, user_id FROM sessions` 原样，指纹列用**独立的第二条查询**获取；
- 指纹查询整体包在 `try/catch` 中：表结构缺列（老库或测试桩）时只跳过级联，主删除流程不受影响。

同一条路由末尾还回收会话留下的两张附属表：

| 表 | 处理 | 判据 |
|---|---|---|
| `session_usage` | 删除本次删掉的全部 `session_id` | 无需判据。这张表按 `session_id` 单行存上下文用量环，daemon 与客户端读它**都是** `WHERE session_id = ?`，没有聚合查询，会话不存在后永远不会再命中 |
| `workspaces` | 只删「本次删掉的一次性时间戳目录」 | 三条同时满足：路径以 `\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}` 结尾、已无 `deleted_at IS NULL` 的会话用它当 cwd、`last_opened_at` 早于 24 小时。与 `session-sync/wd_workspaces.py` 的判据一致 |

`workspaces` 这张表是 WorkBuddy 主程序的「最近打开」列表（`app.asar` 里 `getWorkspaces()` 按
`lastOpenedAt` 倒序取 `path`，daemon 自己完全不碰它），所以补丁**只回收因这条会话才存在的目录登记**，
不碰财务分析、日利润表这类手工目录。两张表任一失败只记日志，不影响删除结果。

### 磁盘：补上上游漏删的 5 类会话派生文件

`deleteSessionFiles` 原来只删 6 类：`projects/<proj>/<id>.jsonl`、`projects/<proj>/<id>/`、
`workspace/sessions/<id>/`、`tasks/<id>/`、`file-history/<id>/`、`artifact-index/<id>.json`。
用一个测试会话在 11 个位置各埋一个文件后调真删除接口，实测返回
`filesRemoved: 6`，**剩下 5 个位置原地不动**：

| 位置 | 内容 | 实测（本机） |
|---|---|---|
| `projects/<proj>/<id>.meta.json` | 会话元数据，与 `.jsonl` 同目录 | 残留；孤儿 1 |
| `projects/<proj>/<id>.file-rollback.ndjson` | 文件回滚记录，同上 | 残留；孤儿 47 |
| `changes-detail/<id>/` | 变更详情，目录名即会话 id | 残留；孤儿 36 |
| `changes-index/<id>.json` | 变更索引，`conversationId` 字段即会话 id | 残留；孤儿 36 |
| `file-tree-manifests/<id>.json` | 文件树清单，`conversationId` 字段即会话 id | 残留；孤儿 51 |

这五处都只属这一条会话（后三处的内部字段 `conversationId` 与文件名一致，已抽查确认），
会话行删掉后永远不会再被读到。补丁沿用上游同一个 `delOne` 删除（内部仍有
`isManagedDirectoryNoFollow` / `resolveManagedSessionTarget` 的符号链接与越界校验），
目录不存在时直接跳过、不报错。

补完再跑同一实验：`filesRemoved: 11`，**残留 0 / 11**。

### `scripts/inject.js`（面板；重新注入即生效，无需重启）

| 位置 | 改动 |
|---|---|
| `WBS_I18N_EN` 字典 | 新增 `还需再点一次「永久删除」才会执行` 的英文映射 |
| `openDeleteModal` 第二步确认 | 给出 toast 提示，避免把「再次确认」当成已执行 |
| `wbsReportErr` 错误钩子 | 取不到 `ev.error` / `ev.reason` 时退回 `ev.message`，并过滤 `ResizeObserver loop` 噪声；监听器改为具名并登记到 `window.__wbsErrListener`，重注入时先解绑再注册 |

删除结果提示**没有**改动：1.2.18 上游已经写成
`var extra = (d && d.cascaded) ? '，含其他账号副本 ' + d.cascaded + ' 个' : ''`，
与补丁早期版本想做的「显示顺带删掉的副本数」完全等价。早先那版改成 `deleted - 勾选数`
只是把文案换成「含同源副本」，属重复实现，已回退成上游原样。

面板侧刻意**不**再发 `/api/sessions?range=all` 快照：上游 `session-delete-ui.test.js` 锁定
「删除只发一次 `/api/sessions/delete`」，且 daemon 侧已能一次删净。

### 与上游已有功能的重叠度（逐项核过）

| 补丁改动 | 上游 1.2.18 是否已有 | 判定 |
|---|---|---|
| 两步确认 toast | 没有（上游第一次点击只是把按钮文案换成「永久删除」并 `return`） | 纯增量 |
| 删除结果提示 | 有，`d.cascaded` + 「含其他账号副本」 | **完全重复，已回退** |
| `session_usage` 回收 | 没有（全文无 `DELETE`，只有 `INSERT OR REPLACE` 与单条 `SELECT`） | 纯增量 |
| `workspaces` 回收 | 没有（daemon 完全不碰这张表） | 纯增量 |
| `wbsReportErr` 降噪与解绑 | 没有（原钩子只读 `ev.error`，匿名监听器 + 标记守卫） | 纯增量 |
| 同源副本指纹级联 | 有 lineage 级联 `collectLineageMembersForDelete` | **功能重复，属防御性保留**，见下 |

指纹级联的重复度是实测的：把本机 26 组活跃重复指纹逐个拿去比对 `meta.json` 的三处映射
（`sessions[].members`、`sessionIndex`、`copies[].targetId`），**26 组全部落在同一 lineage 内、
0 组需要指纹补位**。也就是说在映射完整时上游的 lineage 展开已经能删净，指纹这一层当前零额外覆盖。

保留它的理由只有一条：映射被外部写坏时（例如 `wd_unify --prune-empty` 软删空壳、或旧版复制记录
没有迁进 lineage）指纹还能兜底。若要追求最小补丁，删掉 daemon.js 里那段指纹展开即可，
其余改动不依赖它。

### 为什么顺手改了错误钩子

`daemon.log` 里 99% 的 `[wbscrash] error: null` 不是崩溃。上游钩子只读 `ev.error` / `ev.reason`，
而浏览器在 `ResizeObserver` 循环、资源加载失败等场景下 `ev.error` 恒为 `null`，真实原因只写在
`ev.message` 里。于是在页面里加临时探针（未改文件）抓到的实际内容是
`ResizeObserver loop completed with undelivered notifications.`，属浏览器良性告警。

原钩子还有一处隐患：监听器是匿名函数且用 `window.__wbsErrHook` 做守卫，重注入时既不会解绑旧的、
也不会注册新的，改完代码必须重启客户端才生效。本补丁把监听器存进 `window.__wbsErrListener`，
并在注入前的清理段按引用解绑，重注入即可换新。

## 三、验证结果（本次实测）

环境：`git clone` 上游源码 `1.2.18` 标签，Node 22.22.2，Windows 10；实机部分为本机 WorkBuddy 5.8.3 + WorkDaddy 1.2.18。

| 项目 | 结果 |
|---|---|
| 补丁可干净应用 | `git apply --check` 两个补丁均通过；在实机 `scripts/` 上 `apply.py --dry-run` 与实跑均通过 |
| 语法 | `node --check` 对 `inject.js`、`daemon.js` 均通过（源码树与实机产物各一次） |
| 删除/会话/国际化相关测试 | 7 个相关测试文件共 112 项：112 项中 111 过、1 失败；未打补丁的干净基线同样 111 过 1 失败（同一项 `native automatic first copy registers destination ownership`），差集为空 |
| 上游全量测试 | 1393 项：未打补丁 29 项失败，打补丁同样 29 项失败，差集为空（失败项与补丁无关，多为平台/环境类断言） |
| 降噪实测（实机） | 重注入后连续 90 秒新增 `[wbscrash]` 0 条（改前该时段为每分钟 3~37 条），同期日志仍在正常写入；主动抛 `Error('wbg-filter-probe-real')` 被正常记录，手工派发 `ResizeObserver loop` 事件被过滤 |
| 附属表回收（实机、有写入） | 插 6 个会话 + 6 条 `session_usage` + 4 条 `workspaces`，只请求删 4 个：返回 `deleted=5 cascaded=1`；`session_usage` 只剩未请求的那个会话；`workspaces` 四个案例分别命中断言——时间戳目录+旧时间被回收，时间戳目录+刚打开保留，手工目录名保留，仍有活跃会话引用保留。守护日志同时打出「顺带清理工作空间登记 1 条」。跑两轮（临时脚本 + 仓库脚本各一次）均 9/9 通过，清理后库回到 538 / 312 / 35 |
| 磁盘保真（实机、有写入） | 11 个位置各埋一个探测文件后调真删除接口：打补丁前 `filesRemoved: 6`、残留 5 个位置；打补丁后 `filesRemoved: 11`、**残留 0 / 11**。库里现存对应孤儿：`changes-detail` 36、`changes-index` 36、`file-tree-manifests` 51、`.file-rollback.ndjson` 47、`.meta.json` 1 |
| `apply.py` 往返 | 应用后回滚，两文件 sha256 与原始完全一致 |
| 实机产物与验证产物一致性 | 实机 `inject.js` 与源码树补丁产物逐行相同；`daemon.js` 仅差 `DAEMON_VERSION` / `DAEMON_BUILD_ID` 两行 |
| 实机守护重启 | 终止旧进程后 watchdog 3s 内拉起新进程，`/api/status` 返回 `version 1.2.18`，注入接口返回 `mounted: true`，页面悬浮按钮存在 |
| 指纹 SQL 在真实库的命中 | 本机库只读抽样 6 组重复指纹：5 组命中数与组内行数相同，1 组多命中 1 行已软删行（属预期，补丁不过滤 `deleted_at`） |
| 实机端到端级联（真实库、有写入） | 先做 12.20 MB 全库备份，再直插 3 行测试会话（2 行同指纹、1 行 `created_at` 仅差 1ms）。只请求删除其中 1 行：HTTP 200 返回 `{"deleted":2,"requested":1,"cascaded":1}`，同指纹行一并消失，差 1ms 的对照行保留。这 2 行只能来自补丁——直插的行在 `meta.json` 里没有 lineage 映射，上游展开器只会返回请求的那 1 行。测完物理清理，库回到 538 行 / 活跃 150 行，与测试前一致 |
| **面板真实 UI 点删（2026-10-24 补做）** | 此前所有删除验证都直调 `/api/sessions/delete`，**从未点过面板的删除按钮**——「按钮 → 两步确认 → 列表刷新」这条链一直没被验证。用 `ui_delete.mjs` 走真实鼠标事件（`Input.dispatchMouseEvent`，不是 `el.click()`）补测：造 1 条一次性会话 + 11 处落盘全部铺开，开悬浮球 → 会话页 → 批量操作 → 勾选 → 删除所有账号同步副本 → 继续确认 → 永久删除。**6/6 判定通过**：首点标题仍为「删除所有账号的同源会话？/继续确认」；首点后变「再次确认永久删除/永久删除」且弹出「还需再点一次「永久删除」才会执行」；第二次才执行并关窗；执行后有「已删除 1 个会话」；列表行立即消失（`loadSessions()` 刷新生效）。**落盘 11/11 清空、`session_usage` 0 行、`sessions` 行消失** |

### 面板点删验证的两个坑（都会让结论整体失真）

1. **客户端同时有两个 page**：主窗口 + 文档窗口都匹配 `app.asar/renderer`。`cdp.mjs` 原来取列表第一个，第二次跑就打到文档窗口上，探到的 DOM 全是空的（`wbs-*` 节点 0 个），看起来像「面板根本没注入」。已给它加 `WD_CDP_TARGET=<id 前缀|URL 片段>` 与 `list` 子命令。
2. **测试文件放错目录**：第一轮把 `changes-detail` / `changes-index` / `file-tree-manifests` 造在 `projects/<项目>/` 下，而 daemon 是去**数据根** `~/.workbuddy/` 找的。删除结果 4/9，差点被判成「补丁漏删 5 类文件」。实际是测试现场构造错了——`deleteSessionFiles()` 的 11 处路径必须逐一对照，少对一处就会得出反向结论。

### 已知残留（未修，非空间问题）

删掉某个项目里的最后一条会话后，`projects/<编码后的cwd>/` 会留下**空目录**（`deleteSessionFiles()` 只删按 id 命名的节点，不判断目录是否已空）。本机现有 134 个这样的空目录，合计 0 字节。属整洁度问题而非空间问题，且删除空目录要额外判「是否真是本工具产生的空壳」，暂不改动补丁。

## 四、应用与回滚

```powershell
# 1) 先干跑，确认版本与上下文匹配（不写盘）
python apply.py --dry-run

# 2) 应用（自动备份到 %APPDATA%\WorkDaddy\patches-backup-<时间戳>\）
python apply.py

# 3) 让面板加载新代码：面板设置 → 关于 → 重新注入，或重启 WorkDaddy
# 4) daemon.js 的改动需重启 WorkDaddy 守护进程后才生效
```

### 装 1.2.18 与重打补丁

安装包不在本机留存，需要时从上游取：

- 下载：`https://github.com/babygoton/WorkDaddy/releases/download/1.2.18/WorkDaddy-Setup-1.2.18.exe`
- 校验：28954985 字节，SHA256 `0bb2b47425c831d7c602ce9c65e4bebcd6d57f532fe561934477d6974b4a8df4`（与发布页 digest 一致）

三步：

1. 静默安装：`WorkDaddy-Setup-1.2.18.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS /DIR="%LOCALAPPDATA%\Programs\WorkDaddy"`
2. 核对版本：`scripts\daemon.js` 的 `DAEMON_VERSION` 应为 `1.2.18`（GitHub tag 原文写作 `1.2.68`，安装包把它归一成 tag 号，两个值 `apply.py` 都接受）
3. 重打补丁：`python apply.py`，再重新注入并重启守护

自动更新会覆盖 `inject.js` 与 `daemon.js`，每次更新后重复第 3 步即可。

装好之后可以端到端复验是否真的生效（会写库，默认只做只读预检）：

```powershell
python verify_cascade.py          # 只读预检：库路径、守护版本、当前行数
python verify_cascade.py --yes    # 插 3 行测试会话 → 调真删除接口 → 校验 → 物理清理

python verify_side_tables.py          # 附属表回收的只读预检
python verify_side_tables.py --yes    # 插 6 会话 + 6 usage + 4 workspaces → 校验 9 项断言

python verify_files.py                # 磁盘文件覆盖的只读预检
python verify_files.py --yes          # 11 个位置各埋一个文件 → 真删 → 核对残留

# 面板按钮这条链（真实鼠标事件，不是 el.click()）
set WD_CDP_TARGET=<page id 前缀>      # 客户端有多个窗口时必须指定，否则会打到文档窗口
node ui_delete.mjs "<要删的会话标题>" run
```

`verify_cascade.py` 成功的标志是：接口返回 `deleted=2 requested=1 cascaded=1`，
同指纹行消失、`created_at` 差 1ms 的对照行保留，清理后库行数与测试前一致。
`verify_side_tables.py` 成功的标志是 9 项断言全过（会话级联、`session_usage` 只剩未请求的那条、
`workspaces` 四个判据分支各自符合预期）。
`verify_files.py` 成功的标志是 `filesRemoved=11` 且 11 个探测位置**残留 0**。
`ui_delete.mjs` 成功会打印 6 条判定（首点不执行 / 首点给提示 / 二次才执行 / 有已删除反馈 / 列表已刷新），
任一条失败就说明面板这条链没走通；它自己造测试会话与文件，路径与 daemon 的 11 处白名单一致。
四个脚本删前都先做全库一致性备份，备份留在 `%APPDATA%\WorkDaddy\backups\`。

回滚：

```powershell
python apply.py --revert
```

补丁内容更新过之后，旧版本补丁无法反向应用（hunk 上下文已变），`apply.py` 会识别并给出提示，
此时用补丁前的备份把原版恢复回去再重打：

```powershell
python apply.py --restore-backup   # 从最新一份 patches-backup-* 恢复原版，当前文件另存备查
python apply.py --dry-run && python apply.py
```

`apply.py` 的行为说明：

- 两类通道：优先 `git apply` 做交叉校验；机器上没有 git、或目标文件是 CRLF 行尾时用内置应用器，
  两条通道产物逐字节一致（CRLF 文件保留原行尾）。
- 实机行尾不统一：本机 1.2.17 安装包中 `inject.js` 是 CRLF、`daemon.js` 是 LF，脚本按文件自动判断。
- 幂等：检测到补丁标记时直接提示「无需重复应用」，不会重复插入；如果本机打的是更早版本的补丁，
  脚本会列出缺失的标记并以退出码 2 停止，避免把新 hunk 叠到旧补丁上。
- 版本标记：补丁会在 `inject.js`、`daemon.js` 各写一行 `[wbg-patch rN]`。**每次改动补丁内容
  都要把 `apply.py` 里的 `PATCH_VERSION` 同步 +1**，否则已经打过旧版的机器会被误判成「已是最新」
  而跳过，新改动永远落不了地。
- python 不在 PATH 时，用本机 Python 绝对路径调用同一脚本即可。

## 五、已知局限

- 降噪只针对 `ResizeObserver loop` 这一类。其它 `ev.error` 为 `null` 的 `error` 事件现在会带上
  `ev.message` 与 `文件名:行:列`，不再是一条无信息的 `null`；若上游以后改用别的无害告警文案，
  需要按实测新增过滤规则。
- 已经打开的页面里，旧版匿名监听器无法从代码侧解绑。补丁生效前若页面已注入过旧钩子，
  第一次需要重启客户端（或重载页面）让 `window` 重建；此后每次重注入都能正常换新。
- `workspaces` 只回收「因这条会话才存在的一次性时间戳目录」。库里那些早就没有会话引用的
  历史残留不会因为删会话被清掉——那是 `session-sync/wd_workspaces.py` 的职责（它按批记
  registry、可整批还原）。两处判据一致，但作用域不同，不要混着理解。
- 作用域只在面板「删除」（硬删路径）。本补丁改的是 `POST /api/sessions/delete`，该接口执行
  `DELETE FROM sessions` 并清理文件；WorkBuddy 官方界面里的删除走的是软删（只写 `deleted_at`），
  不经过本补丁，因此从官方界面删掉的会话仍会在其他账号留下兄弟行、也不会回收附属表。
  两条删除路径的差异是实测结论（`daemon.js:12220` 为 `DELETE FROM sessions`，
  `daemon.js:12245` 的 `SET deleted_at = NULL` 是「恢复」而非删除）。
- WorkDaddy 自动更新会覆盖 `scripts/` 下的文件，更新后需重打补丁（本补丁针对 1.2.18）。
- `is_background_automation=1` 的会话被删除接口显式排除，本补丁不改变该行为。
- 指纹以 `created_at` 精确到毫秒为准；若上游改写 `created_at` 语义，需重核级联范围。
- 上游全量测试的 29 项失败与本补丁无关，但会随上游版本变化，升级时应重新对比差集。
