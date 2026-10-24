# 补丁：同源副本级联删除 + 两步确认提示

适用对象：WorkDaddy 桌面端 `1.2.18`（面板 `5.8.3`），改动 `scripts/inject.js` 与 `scripts/daemon.js`。
1.2.17 版的旧补丁（面板侧刷新 + 副本指纹展开）见本文件历史版本，已被本版取代。

版本常量有两处口径，不要混用：GitHub tag `1.2.18` 里 `DAEMON_VERSION` 写作 `1.2.68`，
而官方安装包在打包时把它改写成 tag 号，装到本机后读到的值是 `1.2.18`
（`DAEMON_BUILD_ID` 同步为 `release-1.2.18-login-page-persona-switch`）。
两个值都已列入 `apply.py` 的可接受清单；实测安装包与 tag 的差异只有这两行常量。

## 一、本次适配结论（1.2.17 → 1.2.18）

上游 1.2.18 自己实现了会话页静默刷新，因此旧补丁的前半部分作废：

| 旧补丁能力 | 1.2.18 上游状态 | 本版处理 |
|---|---|---|
| 切回「会话」页时静默拉取 | 已内置 `refreshSessionsSilently()` | 删除该 hunk |
| 6s 定时对账 | 已内置 `setBuildInterval(refreshSessionsSilently, 5000)` | 删除该 hunk |
| 静默模式不闪「加载中…」、保留勾选 | 已内置 `loadSessions({ silent: true })` | 删除该 hunk |
| 面板侧按指纹预展开副本 | 无 | 删除（改为完全由 daemon 侧展开） |
| 同源副本级联删除 | 无 | 保留（daemon 侧） |
| 两步确认提示 | 无 | 保留 |

## 二、补丁内容

### `scripts/daemon.js`（服务端；重启守护进程后生效）

`POST /api/sessions/delete`：在原有 lineage 展开之后，再按
`LOWER(RTRIM(REPLACE(cwd,'\','/'),'/')) + COALESCE(title,'') + CAST(created_at AS INTEGER)`
指纹展开兄弟行，一并纳入 `matchedIds`、规则清理与文件删除。

两个实现约束（为兼容上游测试契约，属于刻意选择）：

- 首个查询保持 `SELECT id, user_id FROM sessions` 原样，指纹列用**独立的第二条查询**获取；
- 指纹查询整体包在 `try/catch` 中：表结构缺列（老库或测试桩）时只跳过级联，主删除流程不受影响。

### `scripts/inject.js`（面板；重新注入即生效，无需重启）

| 位置 | 改动 |
|---|---|
| `WBS_I18N_EN` 字典 | 新增 `还需再点一次「永久删除」才会执行` 的英文映射 |
| `openDeleteModal` 第二步确认 | 给出 toast 提示，避免把「再次确认」当成已执行 |
| `openDeleteModal` 结果提示 | 用 `deleted - 勾选数` 显示「含同源副本 N 个」 |
| `wbsReportErr` 错误钩子 | 取不到 `ev.error` / `ev.reason` 时退回 `ev.message`，并过滤 `ResizeObserver loop` 噪声；监听器改为具名并登记到 `window.__wbsErrListener`，重注入时先解绑再注册 |

面板侧刻意**不**再发 `/api/sessions?range=all` 快照：上游 `session-delete-ui.test.js` 锁定
「删除只发一次 `/api/sessions/delete`」，且 daemon 侧已能一次删净。

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

| `apply.py` 往返 | 应用后回滚，两文件 sha256 与原始完全一致 |
| 实机产物与验证产物一致性 | 实机 `inject.js` 与源码树补丁产物逐行相同；`daemon.js` 仅差 `DAEMON_VERSION` / `DAEMON_BUILD_ID` 两行 |
| 实机守护重启 | 终止旧进程后 watchdog 3s 内拉起新进程，`/api/status` 返回 `version 1.2.18`，注入接口返回 `mounted: true`，页面悬浮按钮存在 |
| 指纹 SQL 在真实库的命中 | 本机库只读抽样 6 组重复指纹：5 组命中数与组内行数相同，1 组多命中 1 行已软删行（属预期，补丁不过滤 `deleted_at`） |
| 实机端到端级联（真实库、有写入） | 先做 12.20 MB 全库备份，再直插 3 行测试会话（2 行同指纹、1 行 `created_at` 仅差 1ms）。只请求删除其中 1 行：HTTP 200 返回 `{"deleted":2,"requested":1,"cascaded":1}`，同指纹行一并消失，差 1ms 的对照行保留。这 2 行只能来自补丁——直插的行在 `meta.json` 里没有 lineage 映射，上游展开器只会返回请求的那 1 行。测完物理清理，库回到 538 行 / 活跃 150 行，与测试前一致 |

## 四、现象与根因（1.2.17 时期记录，机制在 1.2.18 未变）

- 在 WorkBuddy 官方界面删除任务后，WorkDaddy 面板「会话」页仍列出该任务；取消勾选重进也不消失。
- 根因：官方删除只标记它当时指向的那一行 `deleted_at`；自动同步在其他账号留下的
  `cwd + title + created_at` 完全一致的兄弟行仍为 `NULL`，于是列表里同标题任务照旧出现。
- 1.2.17 实测：全库 23 组指纹重复、共 104 行；某账号 10 条可见任务背后另有 34 行同源副本。
- 2026-10-24 在 1.2.18 实机库复测（只读）：`sessions` 共 538 行，活跃 150、已软删 388；
  活跃行中指纹重复 26 组、涉及 123 行；其中 11 组是「已有软删行 + 仍有活跃兄弟」，活跃行 49 条。
  指纹为空的活跃行为 0，说明补丁的指纹展开在本机没有覆盖盲区。

## 五、应用与回滚

```powershell
# 1) 先干跑，确认版本与上下文匹配（不写盘）
python apply.py --dry-run

# 2) 应用（自动备份到 %APPDATA%\WorkDaddy\patches-backup-<时间戳>\）
python apply.py

# 3) 让面板加载新代码：面板设置 → 关于 → 重新注入，或重启 WorkDaddy
# 4) daemon.js 的改动需重启 WorkDaddy 守护进程后才生效
```

装好之后可以端到端复验级联是否真的生效（会写库，默认只做只读预检）：

```powershell
python verify_cascade.py          # 只读预检：库路径、守护版本、当前行数
python verify_cascade.py --yes    # 插 3 行测试会话 → 调真删除接口 → 校验 → 物理清理
```

`verify_cascade.py` 成功的标志是：接口返回 `deleted=2 requested=1 cascaded=1`，
同指纹行消失、`created_at` 差 1ms 的对照行保留，清理后库行数与测试前一致。
删前必定先做全库一致性备份，备份留在 `%APPDATA%\WorkDaddy\backups\`。

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
- python 不在 PATH 时，用本机 Python 绝对路径调用同一脚本即可。

## 六、已知局限

- 降噪只针对 `ResizeObserver loop` 这一类。其它 `ev.error` 为 `null` 的 `error` 事件现在会带上
  `ev.message` 与 `文件名:行:列`，不再是一条无信息的 `null`；若上游以后改用别的无害告警文案，
  需要按实测新增过滤规则。
- 已经打开的页面里，旧版匿名监听器无法从代码侧解绑。补丁生效前若页面已注入过旧钩子，
  第一次需要重启客户端（或重载页面）让 `window` 重建；此后每次重注入都能正常换新。

- 作用域只在面板「删除」（硬删路径）。本补丁改的是 `POST /api/sessions/delete`，该接口执行
  `DELETE FROM sessions` 并清理文件；WorkBuddy 官方界面里的删除走的是软删（只写 `deleted_at`），
  不经过本补丁，因此从官方界面删掉的会话仍会在其他账号留下兄弟行。两条删除路径的差异是
  实测结论（`daemon.js:12220` 为 `DELETE FROM sessions`，`daemon.js:12245` 的
  `SET deleted_at = NULL` 是「恢复」而非删除）。
- WorkDaddy 自动更新会覆盖 `scripts/` 下的文件，更新后需重打补丁（本补丁针对 1.2.18）。
- `is_background_automation=1` 的会话被删除接口显式排除，本补丁不改变该行为。
- 指纹以 `created_at` 精确到毫秒为准；若上游改写 `created_at` 语义，需重核级联范围。
- 上游全量测试的 29 项失败与本补丁无关，但会随上游版本变化，升级时应重新对比差集。
