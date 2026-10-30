# WorkDaddy 补丁记录

本文件记录本机在 WorkDaddy 1.2.18 上打的自有补丁。WorkDaddy 升级会整体覆盖 `scripts/`，补丁随之丢失，届时按本文件重打一遍即可。

- 适配客户端：WorkBuddy 5.9.2（`C:\Program Files\WorkBuddy`，appVersion 5.9.2）
- 补丁日期：2026-10-25
- 备份目录：`%APPDATA%\WorkDaddy\backups\patches\`
- 验证入口：`%USERPROFILE%\WorkBuddy\2026-10-25-13-37-11\patch-tools\check.cmd`

## 已打补丁

### C1 会话忙碌时等待而非立刻报错（daemon.js）

- 位置：`scripts\daemon.js`，函数 `assertSessionSyncIdle`
- 日期：2026-10-30，目标为 WorkDaddy 1.2.18 的 `daemon.js`（与 WorkBuddy 5.9.3 同机运行）
- 原因：原实现一见有会话处于 `Working` / `Planning` 立即抛「当前账号有会话仍在运行」，一次失败就让整轮账号切换中止，并把失败记录挂到账号名下形成屏障，后续切号全被拦。本机 10 月 25 日至 30 日累计 16 次同因失败。
- 改法：把探测与判定拆开——`sessionSyncBusy` 只返回布尔值，`assertSessionSyncIdle` 改成轮询等待，最长 60 秒、每 2 秒复查一次，只有超时才抛原错误文案。等待与放行各记一条 `[wbg-patch C1]` 日志，便于在 `daemon.log` 里核对。
- 判据行：`// [wbg-patch C1] 会话忙碌时先轮询等待空闲，超时才报错`
- 修改前 sha256：`ed5ec5da848d5c0919a8dfc2d377da90512af352147d895b9b57cdcc6223a166`
- 修改后 sha256：`028126c576dde160ae2266e1cfe98be90e265ff8e4f04b90e8e954942ba045fe`
- 备份：`%APPDATA%\WorkDaddy\backups\patches\daemon_修改前_20261030-135947.js`
- 行为测试：`node patch-tools\test-c1-idle-wait.mjs`，八项断言覆盖空闲直通、短时忙碌放行、持续忙碌超时、codebuddy 档案、CDP 未连接
- 生效条件：结束守护进程后由看门狗重拉，或直接重启 WorkDaddy；本次实测看门狗未自动拉起，改为手工启动 `runtime\node\node.exe --experimental-sqlite daemon.js`

### A1 修复 resize 崩溃（inject.js）

- 位置：`scripts\inject.js`，函数 `onWindowChange`
- 原因：`resize` 事件的 target 是 `window` 而非 Node，原代码直接传给 `root.contains()` 会抛
  `TypeError: parameter 1 is not of type Node`，每次改窗口尺寸记一条未捕获异常。
- 改法：先判 `target.nodeType` 再 `contains`，外层加 `try/catch`。
- 判据行：`if (target && target.nodeType && root && root.contains(target))`
- 修改前 sha256：`5a2671e84570c424098f1049c9b5c440421779558722a6bcd82d1effd7706667`
- 修改后 sha256：`1ccb68fd6152c7bd8e267bd93c74347903ba73c3891eb970a81b26ff9a5e2484`

### B1 输入框选择器兼容（workbuddy-compat.js）

- 位置：`scripts\workbuddy-compat.js`，函数 `findCodeBuddySendButton`
- 原因：5.9.2 已移除 `_inputBox_` 类（安装包内命中 0 次），输入框容器改为 `div.cr-input-box`。
- 改法：`var selector = '[class*="_inputBox_"],div.cr-input-box';`
- 说明：该函数还要求按钮的 onClick 源码含 `prepareBeforeSubmit` 与 `flushPendingContentChange`，
  WorkBuddy 侧本来就不满足，因此本次改动不改变 WorkBuddy 现有行为，只避免类名改名后彻底失效。
- 修改前 sha256：`aee4bbbccecd1429bb331c665198f050b8ee2cc0ff3d3e8bd237cfb68b5962b7`
- 修改后 sha256：`51bb2b914bcbe19c82a1ed5fc7848c08e7ede885aa92fe98eeeea9ca2e67ad2b`

### B2 目标档案版本同步（workbuddy-target.json）

- 位置：`%APPDATA%\WorkDaddy\workbuddy-target.json`
- 原因：升级后该文件仍写 `version: 5.8.3.0`，与实际 5.9.2 不符。
- 改法：调用 WorkDaddy 自带的 `workbuddy-target.js` 重新构建并写盘，未手改字段。
- 结果：`version: 5.9.2`，`binary` 保持反斜杠原格式，`cdp.port` 仍为 9222，`lockVersion` 仍为 false。
- 修改前 sha256：`74607079b6908a52269f3e23fc3277552b8b98201feab365112877d49b39f5a2`
- 修改后 sha256：`031f08a701c37db65c912fe744604cbe59515e448245d58acd41d56be65da550`

## 明确未做

| 项 | 原因 |
| --- | --- |
| `appearance snapshot mismatch` | 5.9.2 外观 facade 自身行为，缓存保留后界面正常，改动风险高于收益 |
| `contacts.getOneidUToken` 的 `union_id not found` | 账号侧缺 oneid 绑定，补丁层无法修复 |
| 删除 `cr-conversation-timeline`、`.voice-mic-wrap` 的兜底分支 | 均有二级兜底，保留可减少下次与上游合并的冲突 |

## 升级后如何恢复

1. 双击 `patch-tools\check.cmd`，看哪几项标 `[失败]`。
2. 若「渲染进程注入」失败且提示「没有调试端口」，先确认启动方式：从桌面图标直启的 `WorkBuddy.exe` 命令行里没有 `--remote-debugging-port`，注入必然整体不加载。此时完全退出客户端，用桌面 `WorkDaddy.lnk` 重开，再体检一次。
3. 若「补丁 A1 / B1」或「workbuddy-target.json 版本同步」失败，优先用一条命令重打：`node patch-tools\apply-patches.mjs`（先预览）→ `node patch-tools\apply-patches.mjs --apply`（落盘）。该脚本幂等，已有标记就跳过，改前自动备份并做语法校验，失败会自动回滚；也可双击 `patch-tools\repatch.cmd`。只补其中一项时加 `--only=c1`（可写 `a1,b1,c1,b2` 组合）。
4. 只有 `apply-patches.mjs` 报「需手工」时，才按本文 A1、B1 的原文手改。
5. 若 WB-GUARD 的 r6 标记也丢了，**先**跑 `%USERPROFILE%\WorkBuddy\wb-guard\panel-patches\sessions-delete-refresh\apply.py` 打 r6，**再**跑 `apply-patches.mjs` 打 A1/B1。
6. 改完退出并重新打开 WorkDaddy，让渲染进程重新加载 `inject.js`。体检结果会自动留档在 `backups\patches\checks\<日期>.json`。

## 备忘（写给下次的自己）

1. **`Node.contains()` 只接受 Node。** 事件回调里取 `event.target` 前必须先判 `nodeType`：`resize` 的 target 是 `window`，直接传进 `contains()` 必抛 TypeError。A1 就是这么来的。
2. **补丁有先后顺序。** `inject.js` 同时被 WB-GUARD 的 r6 与本项目 A1 修改，两者位置不重叠（r6 在 1188/1856/2257/2266/10286 行附近，A1 在 5188 行）。恢复时先 r6 后 A1，反过来会让 `apply.py` 的上下文匹配变复杂。
3. **保住原文件行尾。** `inject.js` 与 `workbuddy-compat.js` 都是 CRLF。用编辑器改完若被统一成 LF，`apply.py` 的补丁上下文就对不上了；`apply-patches.mjs` 已按文件实际行尾拼接，手改时要自己注意。
4. **体检项里有噪声源。** WorkBuddy 自身会往 `daemon.log` 打 `crash:error:null {"stack":""}`，24 小时能有 2000+ 条，与补丁无关；体检脚本已把这类空上报和 WB-GUARD 的探针过滤掉。
