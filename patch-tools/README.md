# patch-tools：WorkDaddy 补丁应用与升级体检

WorkDaddy 每次升级会整体覆盖 `scripts/`，本目录里的补丁随之丢失；WorkBuddy 升级后内部 DOM / API 也可能变。这个目录解决两件事：**升级后一键把补丁打回去**，以及**3 分钟内判断哪里坏了**。

配套的补丁内容与备份策略见 [PATCHES.md](PATCHES.md)，5.9.2 的适配审计结论见 [AUDIT-5.9.2.md](AUDIT-5.9.2.md)。

## 文件

| 文件 | 作用 |
| --- | --- |
| `check.cmd` / `upgrade-check.mjs` | 14 项只读体检，退出码 0/1；结果自动追加到 `%APPDATA%\WorkDaddy\backups\patches\checks\<日期>.json` |
| `repatch.cmd` / `apply-patches.mjs` | 幂等重打 A1、B1、C1 三处补丁并重建 `workbuddy-target.json`；默认预览，`--apply` 才落盘，`--only=c1` 只打指定项；改前备份、改后 `node --check`，失败自动回滚 |
| `test-c1-idle-wait.mjs` | C1 补丁行为测试，只读 `daemon.js`、抽补丁段跑桩件，八项断言，退出码 0/1 |
| `test-c1-live.sh` | C1 在线验证，临时拉起守护进程并对运行中会话发一次跨账号复制，看是否先等 60 秒再报错；跑完会留下一个守护进程，收尾时自行处理 |
| `regen-target.cjs` | 单独重建 `workbuddy-target.json`（调用 WorkDaddy 自带的 `workbuddy-target.js`，不手改字段） |
| `cdp-probe.mjs` | 一次性 CDP 求值器，配合任意表达式文件排查渲染进程 |

`.mjs` 用 WorkDaddy 自带的 Node 跑：`%LOCALAPPDATA%\Programs\WorkDaddy\scripts\runtime\node\node.exe`。两个 `.cmd` 已是 GBK + CRLF，双击即用，正常结束不 `pause`。

## 体检覆盖的 14 项

WorkBuddy 版本可读 / `workbuddy-target.json` 版本同步 / 补丁 A1 / 补丁 B1 / 补丁 C1 / 脚本语法 / 渲染进程注入 / 输入框与工具条识别 / 队列适配器 / 会话与控制器 / 运行版本 / `resize` 崩溃回归 / 近 24h 有效崩溃 / WB-GUARD r6 标记。

两个设计取舍：

1. **CDP 不可达时其余项标「未测」而不是「通过」。** 从桌面图标直启的 `WorkBuddy.exe` 命令行里没有 `--remote-debugging-port`，注入会整体不加载；这时「崩溃回归」如果照常判通过，就是假阳性。
2. **过滤崩溃噪声。** WorkBuddy 自身会往 `daemon.log` 打 `crash:error:null {"stack":""}`，一天能有 2000+ 条，与补丁无关；体检只统计有内容的崩溃，并单独报「补丁后新增」条数。

## 补丁顺序（重要）

`inject.js` 同时被两方修改：

| 来源 | 位置 |
| --- | --- |
| `panel-patches/sessions-delete-refresh/apply.py`（r6） | 1188 / 1856 / 2257 / 2266 / 10286 行附近 |
| 本目录 A1 | 5188 行附近 `onWindowChange` |

两者不重叠。恢复顺序是**先 r6、再 A1/B1**：`apply.py` 用严格上下文匹配打补丁，顺序反了会让它的 hunk 更难对上。

`apply-patches.mjs` 已按目标文件**实际行尾**拼接片段（`inject.js` 与 `workbuddy-compat.js` 都是 CRLF）。用编辑器手改时若被统一成 LF，`apply.py` 的上下文就匹配不上——这条踩过一次，写进 PATCHES.md 备忘了。

## 用法

```bat
:: 体检（只读）
check.cmd

:: 预览补丁状态
repatch.cmd

:: 落盘
repatch.cmd apply
```

改完需要退出并重开 WorkDaddy，渲染进程才会重新加载 `inject.js`。
