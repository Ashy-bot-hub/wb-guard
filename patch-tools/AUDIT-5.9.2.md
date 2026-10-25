# WB-GUARD 在 WorkBuddy 5.9.2 上的审计

- 审计时间：2026-10-25 14:00
- 审计对象：运行副本 `%USERPROFILE%\WorkBuddy\_session-sync`（不是桌面交付包）
- 客户端：WorkBuddy 5.9.2；WorkDaddy 1.2.18
- 审计方式：全部为只读命令与只读 SQL，未使用任何 `--apply`

## 一、结论

守护链在 5.9.2 上健康，**没有需要修改的代码**。库结构、进程、补丁标记、索引、磁盘各项均在阈值内，254 项单测全通过。

## 二、逐项结果

| 检查 | 结果 |
| --- | --- |
| `wd_doctor.py` | 15 项全部「达标」，不达标 0 项，退出码 0 |
| 计划任务 | `WorkDaddy-SyncGuard` 指向 pythonw + `wd_watch.py`，无时长上限 |
| 守护进程 | PID 19752 存活，与 `guard.pid` 一致 |
| 补丁完整性 | `wbg-patch r6` 标记在 `inject.js`、`daemon.js` 都在；daemon 版本 1.2.18 |
| 库结构 | 必需 4 张表 / 21 个必需列全部命中 |
| 复制索引 | 幽灵标记 0 条；5 个账号索引 70 条血缘组；失效副本 16/23（属正常退役态） |
| 软删积压 | 软删 170 条，108 条仍占 50 MB，其中 2 条已过 7 天宽限期；usage 孤儿 0 |
| 孤儿派生文件 | 无（在册 330 条会话） |
| 库完整性 | `ok`，12.5 MB，空闲页 0.0%，WAL 模式 |
| 活跃会话 | 160 条，5 个账号分布 32–35 条；同账号重名 0 组 |
| `wd_unify.py` 预演 | 跨账号任务组 35 个、可合并 0 条、内容分叉 87 条（按设计不动）、同账号多余副本 0 条 |
| `wd_audit.py` | 幽灵标记 0；失效副本 16 / 23 |
| `wd_orphans.py` | 无孤儿派生文件 |
| `wd_prune_backups.py` | 快照 4 个共 48.8 MB，可回收 0 MB |
| `wd_workspaces.py` | 36 条登记，无过期可清 |
| 单测 | `Ran 254 tests ... OK`，6 秒 |

## 三、结构适配

5.9.2 的实际库有 11 张表，`sessions` 已扩到 40 列（含 `expert_runtime_identity`、`agent_dirty_at`、`unread` 等新列）。守护只声明自己要用的 21 列，因此新增列不会让它报错，也无需跟进。

## 四、补丁叠加关系（本次最需要记住的一条）

`inject.js` 同时被两方修改：

| 来源 | 位置 | 内容 |
| --- | --- | --- |
| WB-GUARD `panel-patches/sessions-delete-refresh/apply.py`（r6） | hunk 在 1188、1856、2257、2266、10286 行附近 | 会话删除级联与刷新 |
| 本次 A1 | 5188 行附近 `onWindowChange` | resize 崩溃修复 |

两者不重叠，`apply.py` 也不做哈希校验、靠标记幂等，所以它正常运行时不会覆盖 A1。真正的风险是 **WorkDaddy 升级会整体覆盖 `scripts/`，r6 与 A1 会一起丢失**；恢复顺序必须是先 `apply.py` 打 r6、再打 A1/B1，否则 `apply.py` 可能因上下文变化要求先 `--restore-backup`。

## 五、可回收空间（只报告，未执行）

| 位置 | 占用 | 说明 |
| --- | --- | --- |
| `%APPDATA%\WorkDaddy\client-tmp` | 989 MB | 含 `workbuddy-update-x64` 430 MB 更新包、`inv` 62 MB、若干 `.tmp.node` |
| 软删会话残留文件 | 50 MB | 108 条软删会话仍占文件，2 条已过 7 天宽限 |
| `.workbuddy\logs` | 192 MB | 守护每 30 分钟按策略回收，当前在预算内 |

## 六、文档与交付一致性

桌面 `WB-GUARD` 是 10-21 至 10-23 的旧副本，运行副本在 `WorkBuddy\_session-sync`（10-24、10-25 有更新，且多出 `wd_orphans.py`）。桌面那份的 `logs\` 是空的，不能用它判断守护死活。

## 七、顺手发现（与守护无关）

`daemon.log` 近 24 小时里有 **2374 条** `[breadcrumb] crash:error:null {"stack":""}`，是 WorkBuddy 自身 crash 上报的空记录，与 WB-GUARD、WorkDaddy 都无关。它会把「崩溃计数」类指标污染成假故障，因此新体检脚本已把这类空上报和 WB-GUARD 自己的 `wbg-filter-probe` 探针一并过滤，只统计有内容的崩溃：当前 24 小时有效崩溃 4 条，全部发生在补丁之前。
