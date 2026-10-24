# 补丁：同源副本级联删除 + 两步确认提示

适用对象：WorkDaddy 桌面端 `1.2.18`（安装包内 `scripts/daemon.js` 的 `DAEMON_VERSION` 为 `1.2.68`，
面板 `5.8.3`），改动 `scripts/inject.js` 与 `scripts/daemon.js`。
1.2.17 版的旧补丁（面板侧刷新 + 副本指纹展开）见本文件历史版本，已被本版取代。

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

面板侧刻意**不**再发 `/api/sessions?range=all` 快照：上游 `session-delete-ui.test.js` 锁定
「删除只发一次 `/api/sessions/delete`」，且 daemon 侧已能一次删净。

## 三、验证结果（本次实测）

环境：`git clone` 上游源码 `1.2.18` 标签，Node 22.22.2，Windows 10。

| 项目 | 结果 |
|---|---|
| 补丁可干净应用 | `git apply --check` 两个补丁均通过 |
| 语法 | `node --check` 对 `inject.js`、`daemon.js` 均通过 |
| 删除/会话/国际化相关测试 | `session-delete-ui`、`session-delete-confinement`、`i18n-coverage`、`session-refresh-ui` 合计 40 项全过 |
| 上游全量测试 | 1393 项：未打补丁 29 项失败，打补丁同样 29 项失败，差集为空（失败项与补丁无关，多为平台/环境类断言） |
| `apply.py` 往返 | 应用后回滚，两文件 sha256 与原始完全一致 |

## 四、现象与根因（1.2.17 时期记录，机制在 1.2.18 未变）

- 在 WorkBuddy 官方界面删除任务后，WorkDaddy 面板「会话」页仍列出该任务；取消勾选重进也不消失。
- 根因：官方删除只标记它当时指向的那一行 `deleted_at`；自动同步在其他账号留下的
  `cwd + title + created_at` 完全一致的兄弟行仍为 `NULL`，于是列表里同标题任务照旧出现。
- 1.2.17 实测：全库 23 组指纹重复、共 104 行；某账号 10 条可见任务背后另有 34 行同源副本。

## 五、应用与回滚

```powershell
# 1) 先干跑，确认版本与上下文匹配（不写盘）
python apply.py --dry-run

# 2) 应用（自动备份到 %APPDATA%\WorkDaddy\patches-backup-<时间戳>\）
python apply.py

# 3) 让面板加载新代码：面板设置 → 关于 → 重新注入，或重启 WorkDaddy
# 4) daemon.js 的改动需重启 WorkDaddy 守护进程后才生效
```

回滚：

```powershell
python apply.py --revert
```

`apply.py` 的行为说明：

- 两类通道：优先 `git apply` 做交叉校验；机器上没有 git、或目标文件是 CRLF 行尾时用内置应用器，
  两条通道产物逐字节一致（CRLF 文件保留原行尾）。
- 实机行尾不统一：本机 1.2.17 安装包中 `inject.js` 是 CRLF、`daemon.js` 是 LF，脚本按文件自动判断。
- 幂等：检测到补丁标记时直接提示「无需重复应用」，不会重复插入。
- python 不在 PATH 时，用本机 Python 绝对路径调用同一脚本即可。

## 六、已知局限

- WorkDaddy 自动更新会覆盖 `scripts/` 下的文件，更新后需重打补丁（本补丁针对 1.2.18）。
- `is_background_automation=1` 的会话被删除接口显式排除，本补丁不改变该行为。
- 指纹以 `created_at` 精确到毫秒为准；若上游改写 `created_at` 语义，需重核级联范围。
- 上游全量测试的 29 项失败与本补丁无关，但会随上游版本变化，升级时应重新对比差集。
