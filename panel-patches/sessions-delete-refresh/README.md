# 补丁：会话页刷新 + 同源副本级联删除

适用对象：WorkDaddy 桌面端 `1.2.17`（面板 `5.8.3`）安装目录 `scripts/inject.js` 与 `scripts/daemon.js`。
基线：2026-10-21 本机安装版原始文件。上游其他版本需重新核对行号与上下文。

## 一、现象

- 在 WorkBuddy 官方界面删除任务后，WorkDaddy 设置面板「会话」页仍长期列出该任务，重新进入标签页也不消失。
- 在面板里勾选任务删除，删完后同标题任务再次出现，看起来「删不掉」。
- 面板会话页只在首次打开时拉取一次列表，之后不再刷新。

## 二、根因（均为本机可验证事实）

1. 会话页数据源是 `~/.workbuddy/workbuddy.db` 的 `sessions` 表，查询条件 `deleted_at IS NULL AND COALESCE(is_background_automation,0)=0`。
2. `switchTab('sessions')` 只在 `!sessionsPane.dataset.built` 时构建并调用 `loadSessions()`；切走再切回不重新拉取，也未订阅官方 `sessionDeleted` / `sessionsChanged` 事件。
3. 自动同步会在同账号与跨账号留下 `cwd + title + created_at` 完全一致的副本行。本机实测：全库 23 组指纹重复、共 104 行；某账号 10 条可见任务背后另有 34 行同源副本。官方删除只标记它当时指向的那一行，兄弟行 `deleted_at` 仍为 `NULL`，因此刷新后同标题任务照旧出现。

## 三、补丁内容

### `inject.js`（面板；重新注入即生效，无需重启）

| 位置 | 改动 |
|---|---|
| `switchTab` | 再次进入 sessions 时调用 `sessionsPane.__wbsReload()` |
| `loadSessions(silent)` | 新增静默模式：不闪「加载中…」、保留勾选与展开、按 `id+大小+时间+标题` 指纹判重，无变化不重绘 |
| `buildSessionsPane` | 挂载 `__wbsReload`；6s 静默对账定时器（仅标签激活、页面可见、无弹窗、非批量勾选时执行） |
| `openDeleteModal` / 新增 `expandSessionCopies` | 删除前用 `/api/sessions?range=all&uid=` 快照按指纹并入同源副本；toast 显示「含同源副本 N 个」 |
| 两步确认 | 第一步确认时提示「还需再点一次「永久删除」才会执行」 |

### `daemon.js`（服务端；重启守护进程后生效）

`POST /api/sessions/delete`：在原有 lineage 展开之后，再按
`LOWER(RTRIM(REPLACE(cwd,'\','/'),'/')) + COALESCE(title,'') + CAST(created_at AS INTEGER)`
指纹展开兄弟行，一并纳入 `matchedIds`、规则清理与文件删除。

## 四、实测结果（2026-10-21）

| 环节 | 数据 |
|---|---|
| 单条删除 | `submit ids=1` → `expanded ids=4` → `已真实删除 6 个会话（DB + 22 项文件，级联副本 2）` |
| 批量清理 | 请求 37 行 → 实际删除 35 行 / 122 文件 / 23 条 auto-copy 规则 |
| 面板会话数 | 11 → 4（当前任务 + 3 个项目会话），125 MB → 100 MB |
| 残留 | 2 行 `is_background_automation=1`（自动化运行会话）被接口按设计跳过 |

## 五、应用与回滚

```powershell
# 1) 备份 + 应用（需要 git 在 PATH 中）
py apply.py            # 自动备份到 %APPDATA%\WorkDaddy\patches-backup-<时间戳>\
# 2) 让面板加载新代码：面板设置 → 关于 → 重新注入，或重启 WorkDaddy
# 3) daemon.js 的改动需要重启 WorkDaddy 守护进程后才生效
```

回滚：把备份目录里的 `inject.js`、`daemon.js` 覆盖回 `%LOCALAPPDATA%\Programs\WorkDaddy\scripts\`，再重新注入或重启。

## 六、已知局限

- WorkDaddy 自动更新会覆盖 `scripts/` 下的文件，更新后需重打补丁。
- `is_background_automation=1` 的会话被删除接口显式排除，本补丁不改变该行为。
- 指纹以 `created_at` 精确到毫秒为准；若上游改写 `created_at` 语义，需重核级联范围。
