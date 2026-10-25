# 上游 issue 观察清单

只看与「本机这套组合」直接相关的条目：Windows + 多账号轮换 + 重度自动化 + WB-GUARD + patch-tools。上游仓库 `babygoton/WorkDaddy`，基线 WorkBuddy 5.9.2 / WorkDaddy 1.2.18（2026-10-25 整理）。

## 一、观察中（等上游动作）

| Issue | 观察点 | 与本机的关系 | 怎么自己判 |
| --- | --- | --- | --- |
| [#463](https://github.com/babygoton/WorkDaddy/issues/463) 会话太多只关注进行中 | 上游是否给「仅进行中」筛选 | 库内 330 条会话、160 条活跃，侧栏负担真实存在 | 自己数：`wd_doctor.py` 的「活跃会话」一项 |
| [#455](https://github.com/babygoton/WorkDaddy/issues/455) 今天积分用量显示 0 | 上游是否补「今日已用」维度 | `account-credit-cache.json` 只有分段余额与 `expiresAt`，没有今日消耗 | 打开面板「积分」页对比官方账单 |
| [#470](https://github.com/babygoton/WorkDaddy/issues/470) 自定义模型余额查询 | 是否接模型方余额接口 | 本机在用第三方模型（Deepseek 系） | 取决于是否有对应厂商 API key |
| [#467](https://github.com/babygoton/WorkDaddy/issues/467) 重启后主窗口隐藏 | 是否修 `windowsHide:true` | 本机一天重启多次，走同一个 `relaunchWorkBuddy` 路径 | 重启后看屏幕是否只剩托盘图标 |
| [#459](https://github.com/babygoton/WorkDaddy/issues/459) 自动化三类失败 | 「发送未确认」是否修 | 与本项目 B1 补丁覆盖的发送按钮识别同域 | 跑一次自动化看是否卡在发送确认 |
| [#443](https://github.com/babygoton/WorkDaddy/issues/443) / [#212](https://github.com/babygoton/WorkDaddy/issues/212) resize 后点击热区偏移 | 是否同源于 `onWindowChange` | 本项目 A1 修的是同一事件回调里的崩溃 | 拖窗口边缘后点输入框，看是否点得到 |
| [#449](https://github.com/babygoton/WorkDaddy/issues/449) junction 下 cwd-repair 刷日志 | 是否加终止条件 | 仅当工作区在 D 盘且用目录联接才成立，本机未证实 | `dir /AL` 看 `.workbuddy\projects` 是否有联接 |

## 二、已回帖（2026-10-25）

| Issue | 回帖要点 |
| --- | --- |
| [#446](https://github.com/babygoton/WorkDaddy/issues/446) 切号后对话变两个 | 成因（`copyFile` 产生新 inode、非前缀即长期并存）+ 两种收敛方式 + 本机预演数据（35 组 / 可合并 0 / 分叉 87 / 同账号多余 0） |
| [#424](https://github.com/babygoton/WorkDaddy/issues/424) 自动更新后无法启动 | 区分「进程层起不来」与「注入层无调试端口」，给出命令行与端口两步判定 |
| [#452](https://github.com/babygoton/WorkDaddy/issues/452) 删除后列表不刷新、同源副本未级联 | r6 补丁实际覆盖范围（同指纹级联 + 伴生/派生文件）+ `crash:error:null` 24 小时 2374 条的降噪说明 |
| [#451](https://github.com/babygoton/WorkDaddy/issues/451) 会话数据层四处 | 5.9.2 实测 8 项指标 + 四个脚本各自负责哪一条 |

## 三、明确不做

| Issue | 原因 |
| --- | --- |
| #462 `dataRoot` 硬编码 | 本机用默认 `~/.workbuddy`，未迁移 |
| #425 网络代理 | 网络环境不受限 |
| #330 多开账号 | 走切换，不需要并排多开 |
| #262 / #409 人设管理 | WorkDaddy 自带 `persona.js` |
| #429 跨账号同步定时任务 | `official-automation-sync.js` 已在做 |
| #350 跟随系统深浅色 | 自身主题补丁可控，`followSystem` 想开就开 |
| #306 / #308 TraeWork、豆包 | 跨客户端，与本机工作流无关 |

## 四、维护方式

每月看一次上游 `open issues` 的增量即可；重点盯「已回帖」四条是否被合并或要求补信息。若上游合并了 r6 等价改动，`patch-tools` 里的 A1/B1 与 `panel-patches` 的 r6 都应重新评估是否还需要。
