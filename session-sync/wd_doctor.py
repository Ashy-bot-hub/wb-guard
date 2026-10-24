# -*- coding: utf-8 -*-
"""一键体检：把「现在到底还健康吗」压成一条命令。

为什么需要
----------
检查分散在四个脚本里：`wd_audit` 查幽灵索引、`wd_unify` 查重名、
任务计划只能去任务管理器看。出问题要挨个跑一遍、人眼比对，判断标准没写下来。

本脚本把判断标准写死成阈值，输出「哪项不达标 + 具体数字」，并把
`wd_audit` 的只读统计也纳入巡检 —— 幽灵索引只能靠人想得起来才去跑。

    python wd_doctor.py            # 人读
    python wd_doctor.py --json     # 机读（守护每小时落一条日志）
    python wd_doctor.py --deep     # 加做整库 integrity_check

退出码：0 全部达标；1 有告警（能用但要处理）；2 有硬故障（立即处理）。

检查项与阈值（全部可判定，不含「感觉」）
----------------------------------------
| 项 | 达标 | 告警 | 故障 |
|---|---|---|---|
| 计划任务 | XML 存在且脚本路径与期望一致 | 缺 XML / 指向旧脚本 | — |
| 守护进程 | PID 存活 | — | PID 不在 |
| 库完整性 | ok | — | 非 ok |
| 库结构 | 4 张表 / 21 个必需列都在 | 库不存在 / 读不到 | 缺表或缺列 |
| 复制索引残留 | 幽灵条目 0 条 | 1~5 条 | > 5 条 |
| 活跃会话 | 每账号 >0 且无重名 | 有重名 | 0 条 |
| 定时任务 | 每账号都有自有任务且无同账号重名 | 某账号无自有任务 / 同账号重名 | 一条都没有 |
| 日志总量 | 可回收部分 ≤ janitor 预算（420 MB） | 420~840 MB | > 840 MB |
| 备份总量 | ≤ 400 MB | 400~600 MB | > 600 MB |
| 附属数据 | 合计 ≤ 2 GB 且单项 ≤ 1 GB | 超 2 GB | 合计 > 3 GB |
| 封口日志回收 | 无滞留 | 滞留超 35 分钟 | — |
| 数据增量 | 五项累积目录合计 ≤ 2 GB 且单项 ≤ 1 GB | 超 2 GB 或单项超 1 GB | 合计 > 3 GB |
| 补丁完整性 | 两文件都带当前 `wbg-patch rN` 标记，且 daemon 版本在适配清单 | 缺标记 / 版本不在清单 | — |
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wdconfig as C
import wd_fs as F
import wd_audit as A
import wd_db_maintain as D
import wd_janitor as J
import wd_shadow as S
import wdrules as R

# 任务名 → 当前包期望它跑的脚本。只校验「任务存在」是不够的：升级时漏改
# 任务，任务会继续指向旧脚本，而守护看起来一切正常。
TASKS = (("WorkDaddy-SyncGuard", "wd_watch.py"),)
TASK_DIR = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "Tasks")
# 守护目录。本机是「源码在 workdaddy-guard、部署在 _session-sync」的双目录布局，
# 但交付包解到别处（README 的示例是 D:\tools\workdaddy-guard）时没有 _session-sync
# 这一层 —— 那时守护就住在包自己身上。写死前者会让新机器第一次体检报「守护未部署」，
# 而实际正在跑，这正是最容易误导人的那种结论。
def _sync_dir(here):
    """有兄弟目录 _session-sync 就用它，否则认为包自己就是部署目录。

    只认一级不够：仓库被收进子目录后（本机 2026-10-24 起源码在
    `WorkBuddy\\wb-guard\\session-sync`，部署仍在 `WorkBuddy\\_session-sync`），
    要上跳两级才看得到。判错时的表现是「PID 文件不存在」这类假故障 —— 守护实际
    正在跑，人却去查一个不存在的部署。所以两级都探，且优先选真的放着 pid 文件
    的那个目录。
    """
    parent = os.path.dirname(here)
    cands = [os.path.join(parent, "_session-sync"),
             os.path.join(os.path.dirname(parent), "_session-sync")]
    for cand in cands:
        if os.path.isdir(cand) and any(
                os.path.exists(os.path.join(cand, n)) for n in C.PID_GUARD):
            return cand
    for cand in cands:
        if os.path.isdir(cand):
            return cand
    return here


SYNC = _sync_dir(C.HERE)
# 名字集中在 wdconfig（写方与读方同源）：这里只声明「要检查哪个进程」，
# 每个进程认全部兼容名字。读方认旧名、写方写新名，会得出「守护在跑、体检报
# 故障」这种自我否定的结论。名字只有一个来源，这类分叉从结构上不会发生。
PROC_FILES = (
    ("guard-watch", C.PID_GUARD),
)


# 阈值与清理脚本共用同一组常量。两边各写一套，同一台机器就会出现「体检达标、
# 清理永远删不干净」的矛盾结论 —— 两个口径必须同源。
LOG_BUDGET = J.LOG_BUDGET_MB          # MB，可回收部分的预算
HOT_IDLE_MIN = J.IDLE_MIN             # 分钟内仍在写的文件删不掉，单独计
# 备份上限按「保留策略的上界」定，不按「今天多大」定。wd_unify 每批备份一份
# 转录 + 一份库，保留最近 3 批，单批 ≈ 转录总量（本机 76 会话约 60 MB）→
# 正常上界 180 MB，再加 ~/.workbuddy/backups 里 30 MB 的安装前快照。
# 上界约 210 MB：一次切号触发统一就能把它顶到 183 MB，阈值贴着上界就会天天
# 报警。400/600 = 上界的 2 倍以上，只在备份没被回收时响。
BAK_WARN, BAK_FAIL = 400, 600         # MB
# MB，会话附属数据（影子副本/轨迹/历史）。阈值按磁盘余量定口径：本机可用
# 376 GB，2 GB 占比 0.5%，不值得打扰；3 GB 起算故障。与「数据增量」一项的
# 2048/3072 同口径，避免两处各说一套。不按「活跃会话数 × 单会话估值」定：
# 75 个活跃会话常态就在 520 MB 上下，估低了等于每轮体检都报同一条告警，
# 告警本身失去信息量（人对反复出现的红字会脱敏）。
SHADOW_WARN, SHADOW_FAIL = 2048, 3072
# 封口日志允许滞留的分钟数。清理周期是 30 分钟，阈值必须大于周期 + 3 分钟的
# 封口判定期，否则每次体检都必然报「滞留」—— 这是把调度节奏当成故障报警，
# 属假阳性。设 35 分钟：只有「上一轮就该删掉却还活着」才报警。
SEALED_LAG_MIN = 35
# 定时任务不硬编码条数，也不比对各账号是否等量：判据写在 chk_db 里。
# 判据只对着「会坏的两件事」写 —— 某账号无自有任务、同账号重名 —— 而不是对着
# 「今天刚好等于几」写：用户加一条任务、或本地镜像停用导致条数天然不等，
# 都不该报警。
# 按会话只增不减、且没有任何脚本回收的数据目录（判定逻辑与阈值由来见
# wdrules.judge_growth）。
GROWTH_DIRS = ("changes-detail", "blobs", "cache", "file-tree-manifests", "traces")
GROWTH_WARN, GROWTH_FAIL = 2048, 3072   # MB，五项合计
GROWTH_DIR_WARN = 1024                  # MB，任一单项


OK, WARN, FAIL = "达标", "告警", "故障"


def _mb(path):
    return F.dir_size(path) / 1048576.0


def _log_stats():
    """一次遍历同时给出 (日志总量, 今日量, 在写量)，单位 MB。

    三个数都来自同一份目录快照：各走一遍日志树，同一批 stat 要付三遍钱
    （实测 237 ms）。
    """
    stamp = time.strftime("%Y%m%d")
    today_dirs = (os.path.join(C.APPLOGS, "sandbox", stamp) + os.sep,
                  os.path.join(C.APPLOGS, time.strftime("%Y-%m-%d")) + os.sep)
    cutoff = time.time() - HOT_IDLE_MIN * 60
    total = today = hot = 0
    for fp, size, mtime in F.iter_files(C.APPLOGS):
        total += size
        if mtime > cutoff:
            hot += size
        for d in today_dirs:
            if fp.startswith(d):
                today += size
                break
    m = 1048576.0
    return total / m, today / m, hot / m


def _pids():
    """当前进程 PID 集合。

    直接用 wd_shadow 的进程表快照：它在 5 秒窗口内复用一次 tasklist 结果，
    体检里「存活 PID」「客户端是否在跑」「轨迹判废」三处共用同一份数据，
    避免各跑一次 tasklist（三次合计实测 672 ms）。
    """
    return S.alive_pids()


def chk_tasks():
    rows = []
    for name, expected in TASKS:
        p = os.path.join(TASK_DIR, name)
        item = {"name": "计划任务 %s" % name}
        if not os.path.exists(p):
            item.update(status=WARN, detail="找不到任务定义文件，可能未注册")
            rows.append(item)
            continue
        try:
            root = ET.parse(p).getroot()
            ns = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"
            cmd = root.findtext(".//%sCommand" % ns) or ""
            args = root.findtext(".//%sArguments" % ns) or ""
            limit = root.findtext(".//%sExecutionTimeLimit" % ns) or "未设置"
            target = R.task_script_path(args)
            state, why = R.judge_task_target(target, expected)
            status = {"ok": OK, "warn": WARN, "fail": FAIL}[state]
            item.update(status=status, detail="命令 %s；脚本 %s；时长上限 %s" % (
                os.path.basename(cmd) or cmd, why, limit))
        except Exception as e:                   # noqa: BLE001
            item.update(status=WARN, detail="解析失败：%s" % e)
        rows.append(item)
    return rows


def _started(sync):
    """这个目录里有没有「装过/跑过」的痕迹。

    为什么需要区分：交付包解到新机器时目录存在、但没跑过 install.ps1。只看
    「目录在不在」会把这种情况判成「部署了但进程死了」—— 报故障、催人排障，
    而正确动作只是「先安装」。判据取 logs/ 与 pid 文件：两者都没有就是没跑过。
    """
    if os.path.isdir(os.path.join(sync, "logs")):
        return True
    for cands in (C.PID_GUARD, C.PID_LEGACY_TIMELINE):
        if any(os.path.exists(os.path.join(sync, c)) for c in cands):
            return True
    return False


def chk_procs(live):
    rows = []
    # 找不到守护目录时，两个 PID 文件当然都不存在 —— 这是「没部署」，
    # 不是「部署了但进程死了」。前者报故障会让人去修一个根本不存在的部署，
    # 解包到别处跑一次体检就会撞上。
    if not os.path.isdir(SYNC):
        return [{"name": "守护进程", "status": WARN,
                 "detail": "未找到守护目录 %s；本包可能只做体检、守护未部署" % SYNC}]
    if not _started(SYNC):
        return [{"name": "守护进程", "status": WARN,
                 "detail": "目录在但从未启动（%s 下没有 logs/ 与 pid 文件）；先运行 install.ps1" % SYNC}]
    for label, cands in PROC_FILES:
        name = "守护进程 %s" % label
        hit = next((os.path.join(SYNC, c) for c in cands
                    if os.path.exists(os.path.join(SYNC, c))), None)
        if hit is None:
            rows.append({"name": name, "status": FAIL,
                         "detail": "PID 文件不存在（找过 %s）" % " / ".join(cands)})
            continue
        try:
            with open(hit, encoding="utf-8") as fh:
                pid = int(fh.read().strip())
        except (OSError, ValueError):
            rows.append({"name": name, "status": FAIL, "detail": "PID 文件不可读"})
            continue
        ok = (live is None) or (pid in live)
        rows.append({"name": name, "status": OK if ok else FAIL,
                     "detail": "PID %d %s（%s）" % (
                         pid, "存活" if ok else "已不在", os.path.basename(hit))})
    return rows



# ===== 补丁完整性 =====
# 客户端自动更新会把安装包整体覆盖到 scripts/ 下，panel-patches 打进去的改动随之
# 消失。其余检查项都看不出来：库、进程、日志、索引一切正常，唯一的表现是「删除
# 会话又退回旧行为」，而人往往几天后才发现。2026-10-24 就因为 1.2.17 → 1.2.18 的
# 升级重打过一次，所以把它做成可判定的一项。
#
# 判据不写死在体检里，而是回读 apply.py 的 PATCH_VERSION 与适配版本清单：
# 两处各写一份版本号，改补丁时必然漏改一处，届时体检报的「达标」就是假的。
PATCH_REL = os.path.join("panel-patches", "sessions-delete-refresh")
_PATCH_VER = re.compile(r'^PATCH_VERSION\s*=\s*"([^"]+)"', re.M)
_PATCH_ACCEPTED = re.compile(r'^ACCEPTED_DAEMON_VERSIONS\s*=\s*\(([^)]*)\)', re.M)
_PATCH_SEEN = re.compile(r"\[wbg-patch (r\d+)\]")
_DAEMON_VER = re.compile(r"const DAEMON_VERSION = '([^']+)'")


def _script_dirs():
    """本机可能装 WorkDaddy 的目录，按优先级排列。

    安装位置随发行渠道变化（用户目录安装 / Program Files / 旧名 WorkBuddy），
    写死一处会在换台机器时得出「找不到安装目录」的假结论。
    """
    cands = []
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        for name in ("WorkDaddy", "WorkBuddy"):
            cands.append(os.path.join(local, "Programs", name, "scripts"))
    for root_var in ("ProgramFiles", "ProgramFiles(x86)"):
        root = os.environ.get(root_var, "")
        if root:
            for name in ("WorkDaddy", "WorkBuddy"):
                cands.append(os.path.join(root, name, "resources", "scripts"))
    return cands


def _patch_tool():
    """panel-patches 可能所在的位置。

    交付包布局是「补丁目录与体检脚本同级」；本机是「源码进 wb-guard 子目录、
    部署在 _session-sync」的双层布局。只认一种，另一种机器上这项检查会静默跳过 ——
    跳过比误报更危险：人以为体检覆盖了补丁，其实没有。
    """
    here = C.HERE
    for base in (os.path.dirname(here), os.path.dirname(os.path.dirname(here))):
        for mid in ("", "wb-guard"):
            cand = os.path.join(base, mid, PATCH_REL) if mid else \
                os.path.join(base, PATCH_REL)
            if os.path.isfile(os.path.join(cand, "apply.py")):
                return cand
    return None


def chk_patch(scripts_dir=None):
    """补丁标记与 daemon 版本是否仍与仓库声明一致。

    仓库里没有 panel-patches（只部署了体检脚本的机器）时直接跳过，不报噪声 ——
    这类机器本来就不指望有补丁。
    """
    tool = _patch_tool()
    if tool is None:
        return []
    apply_py = os.path.join(tool, "apply.py")
    try:
        with open(apply_py, encoding="utf-8", errors="replace") as fh:
            src = fh.read()
    except OSError:
        return [{"name": "补丁完整性", "status": WARN,
                 "detail": "读不到 %s，无法确认补丁版本" % apply_py}]
    m_ver = _PATCH_VER.search(src)
    if not m_ver:
        return [{"name": "补丁完整性", "status": WARN,
                 "detail": "apply.py 里找不到 PATCH_VERSION"}]
    want = m_ver.group(1)
    m_acc = _PATCH_ACCEPTED.search(src)
    accepted = re.findall(r'"([^"]+)"', m_acc.group(1)) if m_acc else []

    if scripts_dir is None:
        for cand in _script_dirs():
            if os.path.isfile(os.path.join(cand, "inject.js")) and \
               os.path.isfile(os.path.join(cand, "daemon.js")):
                scripts_dir = cand
                break
    if not scripts_dir or not os.path.isdir(scripts_dir):
        return [{"name": "补丁完整性", "status": WARN,
                 "detail": "未找到 WorkDaddy 安装目录（找过 %d 处），无法确认补丁是否在位"
                           % len(_script_dirs())}]

    texts, missing = {}, []
    for name in ("inject.js", "daemon.js"):
        try:
            with open(os.path.join(scripts_dir, name), encoding="utf-8",
                      errors="replace") as fh:
                texts[name] = fh.read()
        except OSError:
            texts[name] = ""
            missing.append(name + "（读不到）")
    seen = set()
    for name, text in texts.items():
        seen.update(_PATCH_SEEN.findall(text))
        if "[wbg-patch %s]" % want not in text:
            missing.append(name)

    if missing:
        state = "测到更早版本 %s" % "/".join(sorted(seen)) if seen else "无任何补丁标记"
        return [{"name": "补丁完整性", "status": WARN,
                 "detail": "缺 %s（当前 %s，仓库要求 %s）；客户端更新很可能覆盖了 "
                           "scripts/，跑 panel-patches 的 apply.py 重打"
                           % ("、".join(missing), state, want)}]

    dm = _DAEMON_VER.search(texts["daemon.js"][:60000])
    version = dm.group(1) if dm else "unknown"
    if accepted and version not in accepted:
        return [{"name": "补丁完整性", "status": WARN,
                 "detail": "%s 标记在位，但 daemon 版本 %s 不在适配清单（%s）——"
                           "上游已变，补丁需重新核对上下文"
                           % (want, version, " / ".join(accepted))}]
    return [{"name": "补丁完整性", "status": OK,
             "detail": "%s 标记在两文件都在；daemon %s" % (want, version)}]


# 脚本硬编码依赖的表与列。客户端升级可能改名或删列，届时脚本会在运行时抛
# `sqlite3.OperationalError`，或在 `dict(row)` 里缺键 —— 两种都发生在守护的
# 定时任务里，表现为「某天开始不再工作」。只验 integrity_check 与会话条数
# 看不到结构漂移。这是唯一「外部升级导致内部失效」的入口，值一项。
REQUIRED_SCHEMA = {
    "sessions": ("id", "cwd", "user_id", "title", "custom_title", "status",
                 "created_at", "updated_at", "last_activity_at", "deleted_at"),
    "automations": ("id", "name", "prompt", "status", "next_run_at",
                    "last_run_at", "deleted_at"),
    "automation_runs": ("automation_id", "status"),
    "workspaces": ("path", "last_opened_at"),
}


def chk_schema():
    """必需表/列是否还在。缺任何一项都意味着某个脚本已经不可用。"""
    if not os.path.exists(C.DB):
        return [{"name": "库结构", "status": WARN, "detail": "库文件不存在（%s）" % C.DB}]
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % C.DB.replace("\\", "/"), uri=True)
    except sqlite3.Error as e:                      # noqa: BLE001
        return [{"name": "库结构", "status": WARN, "detail": "打不开库：%s" % e}]
    missing, tables = [], 0
    try:
        for t, cols in REQUIRED_SCHEMA.items():
            have = [r[1] for r in conn.execute("PRAGMA table_info(%s)" % t)]
            if not have:
                missing.append("表 %s" % t)
                continue
            tables += 1
            missing += ["%s.%s" % (t, c) for c in cols if c not in have]
    except sqlite3.Error as e:                      # noqa: BLE001
        return [{"name": "库结构", "status": WARN, "detail": "读表结构失败：%s" % e}]
    finally:
        conn.close()
    if missing:
        return [{"name": "库结构", "status": FAIL,
                 "detail": "缺少 %s —— 客户端升级可能改了结构，相关脚本会失效"
                           % "、".join(missing[:6])}]
    total = sum(len(v) for v in REQUIRED_SCHEMA.values())
    return [{"name": "库结构", "status": OK,
             "detail": "%d 张表 / %d 个必需列都在" % (tables, total)}]


def chk_index():
    """autoCopy 索引里的幽灵条目（已标记但会话不存在 / 已软删）。

    为什么值得占一项：索引命中意味着「客户端认为这个账号已经有这条会话」，
    于是跳过复制 —— 表现出来是「切了账号，侧边栏里那个任务不见了」，而库里、
    转录里一切正常，只能靠翻 meta.json 才看得见。它不是自愈的，也不会随时间
    变好；`wd_unify --prune-empty` 每删一条空壳就可能新增一条。

    只读统计，不写 meta.json（客户端运行中写必被覆盖）；真正的剔除要人退出
    客户端后跑 `wd_audit.py --fix`，属「看到了再处理」的动作。
    """
    n = A.counts()
    if n is None:
        return [{"name": "复制索引残留", "status": WARN,
                 "detail": "读不到 meta.json 或会话库，无法判定"}]
    ghost, stale = n["ghosts"], n["dup_stale"]
    state = OK if ghost == 0 else (WARN if ghost <= 5 else FAIL)
    detail = ("幽灵 %d 条（%d 个账号索引，%d 条血缘组；失效副本标记 %d 条）"
              % (ghost, n["accounts"], n["groups"], stale))
    if ghost:
        detail += "；退出客户端后跑 wd_audit.py --fix 剔除"
    return [{"name": "复制索引残留", "status": state, "detail": detail}]


def chk_db(deep=False):
    r = D.check(deep=deep, full=False)
    rows = [{"name": "库完整性", "status": OK if r["ok"] else FAIL,
             "detail": "%s，%.1f MB，空闲页 %.1f%%"
                       % (r["integrity"], r["size"] / 1048576, r["freelist_ratio"])}]

    per = r.get("sessions_by_user") or {}
    dup = r["duplicate_groups"]
    rows.append({"name": "活跃会话", "status": FAIL if not per else (WARN if dup else OK),
                 "detail": "%d 条，分布 %s；同账号重名 %d 组"
                           % (r["active_sessions"],
                              " ".join("%s:%d" % (k[:8], v) for k, v in sorted(per.items())),
                              dup)})

    auto = r["automations"]
    # 判据：不比条数，只看「某账号无自有任务」与「同账号重名」。
    # 理由与阈值由来写在 wdrules.judge_automations 里；这里只做状态映射。
    dupn = r.get("automation_dup_names") or 0
    state, detail = R.judge_automations(auto, dupn)
    rows.append({"name": "定时任务", "status": {"ok": OK, "warn": WARN, "fail": FAIL}[state],
                 "detail": detail})
    return rows, r


def _sealed_lag():
    """当日沙箱目录里已封口但仍未回收的文件数与滞留时间。"""
    stamp = time.strftime("%Y%m%d")
    d = os.path.join(C.APPLOGS, "sandbox", stamp)
    if not os.path.isdir(d):
        return 0, 0.0
    stats = {}
    for f in os.listdir(d):
        try:
            stats[f] = os.stat(os.path.join(d, f)).st_mtime
        except OSError:
            continue
    sealed = R.roll_seal(stats.items())
    now = time.time()
    lag, count = 0.0, 0
    for f, is_sealed in sealed.items():
        if not is_sealed:
            continue
        idle = (now - stats[f]) / 60.0
        if idle > SEALED_LAG_MIN:
            count += 1
            lag = max(lag, idle)
    return count, lag


def chk_disk():
    rows = []
    logs, today, hot = _log_stats()
    # 判定只看「清理脚本能动的那部分」：仍在写的文件删不掉，把它算进超标
    # 会得到永远无法达标的告警；完全不看又会漏报真实的日志堆积。
    control = max(0.0, logs - hot)
    # 三档与文件头的表格一致：≤ 预算（清理目标）达标，≤ 2 倍告警，再高故障。
    # 原实现把 1.5 倍当成了故障线、2 倍算出后从未使用（pyflakes 抓到的
    # 「assigned but never used」），实际效果是把告警区间压掉一半 ——
    # 420/630 就红字，而设计上 630~840 属于可观察区间。
    fail = LOG_BUDGET * 2
    st = OK if control <= LOG_BUDGET else (WARN if control <= fail else FAIL)
    rows.append({"name": "客户端日志", "status": st,
                 "detail": "%.0f MB = 在写不可删 %.0f MB（%d 分钟内）+ 可回收 %.0f MB"
                           "（预算 %d MB，故障线 %d MB；今天合计 %.0f MB）"
                           % (logs, hot, HOT_IDLE_MIN, control, LOG_BUDGET, fail, today)})
    # 两个候选目录在「包即部署目录」的布局下是同一个路径（SYNC 由 _sync_dir 算出，
    # 没有兄弟 _session-sync 时就等于 C.HERE，于是 SYNC/backups 就是 C.BACKUP）。
    # 两个备份目录可能指向同一处，直接相加会把同一份 689.6 MB 算成 1379 MB：
    # 体检报「备份快照 故障」，清理脚本却按真实体积判定「无需再删」。先去重再
    # 相加，保证「体检看到的数字」就是「磁盘上的数字」。
    bak_dirs = {os.path.normcase(os.path.abspath(p))
                for p in (C.BACKUP, os.path.join(SYNC, "backups"))
                if os.path.isdir(p)}
    baks = sum(_mb(p) for p in bak_dirs)
    rows.append({"name": "备份快照", "status": OK if baks <= BAK_WARN else (
        WARN if baks <= BAK_FAIL else FAIL),
        "detail": "%.0f MB（上限 %d MB）" % (baks, BAK_WARN)})
    # 一次遍历同时给出「可回收」与「活跃会话占用」，不重复走目录树。
    acts, active_bytes = S.scan_with_active()
    sh = sum(a["bytes"] for a in acts) / 1048576.0
    act = active_bytes / 1048576.0
    rows.append({"name": "附属数据", "status": OK if sh + act <= SHADOW_WARN else (
        WARN if sh + act <= SHADOW_FAIL else FAIL),
        "detail": "已结束会话 %.0f MB（在线可回收）+ 活跃会话 %.0f MB（在线删会被重建，"
                  "须退出客户端后 --force；合计上限 %d MB = %.0f GB）"
                  % (sh, act, SHADOW_WARN, SHADOW_WARN / 1024.0)})
    cnt, lag = _sealed_lag()
    rows.append({"name": "封口日志回收", "status": OK if cnt == 0 else WARN,
                 "detail": "无滞留" if cnt == 0 else
                           "%d 个封口文件滞留最久 %.0f 分钟（清理周期 30 分钟）" % (cnt, lag)})
    return rows


def chk_growth():
    """按会话累积、无人回收的目录总量。

    日志、备份、会话附属数据、库这四块只覆盖 `~/.workbuddy` 5 GB 里的约
    1 GB，剩下的 `changes-detail` / `blobs` / `cache` / `file-tree-manifests` /
    `traces` 不在视野里。2026-10-19 实测这五项合计 329 MB，其中
    `changes-detail` 已有单个会话占 59 MB —— 这类目录只增不减，某个会话把它
    撑到几 GB 时体检不会报，要等磁盘告警才发现。

    只统计这五个目录：文件数合计一千九百左右，遍历不到 100 ms。刻意不含
    `projects`（转录是核心资产）与 `binaries`/`plugins`（程序本体按版本替换，
    且几万个文件，遍历代价不划算）。
    """
    sizes = {}
    for name in GROWTH_DIRS:
        p = os.path.join(C.WORKBUDDY, name)
        if os.path.isdir(p):
            sizes[name] = _mb(p)
    state, detail = R.judge_growth(sizes, GROWTH_WARN, GROWTH_FAIL, GROWTH_DIR_WARN)
    return [{"name": "数据增量", "status": {"ok": OK, "warn": WARN, "fail": FAIL}[state],
             "detail": detail}]


def run(deep=False):
    live = _pids()
    rows = []
    rows += chk_tasks()
    rows += chk_procs(live)
    rows += chk_patch()
    rows += chk_schema()
    rows += chk_index()
    db_rows, db = chk_db(deep=deep)
    rows += db_rows
    rows += chk_disk()
    rows += chk_growth()
    status = FAIL if any(r["status"] == FAIL for r in rows) else (
        WARN if any(r["status"] == WARN for r in rows) else OK)
    return {"status": status, "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "checks": rows, "db": db}


def main(argv=None):
    ap = argparse.ArgumentParser(description="WorkDaddy 一键体检")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--deep", action="store_true")
    a = ap.parse_args(argv)

    r = run(deep=a.deep)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        print("WorkDaddy 体检  %s  结论：%s" % (r["time"], r["status"]))
        for c in r["checks"]:
            print("  [%s] %-18s %s" % (c["status"], c["name"], c["detail"]))
        bad = [c["name"] for c in r["checks"] if c["status"] != OK]
        print("\n不达标 %d 项%s" % (len(bad), ("：" + "、".join(bad)) if bad else ""))
    return 0 if r["status"] == OK else (2 if r["status"] == FAIL else 1)


if __name__ == "__main__":
    sys.exit(main())
