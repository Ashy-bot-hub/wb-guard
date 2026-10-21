# -*- coding: utf-8 -*-
"""纯规则层：所有「删哪些 / 留哪些」的判断集中在这里，不碰磁盘。

为什么要单独一层
----------------
清理与保留策略若散落在 wd_janitor / wd_unify / 各脚本里，规则只能以
「遍历目录 + 顺手删」的形式存在，无法单独验证：
- 改一个阈值，要等到明天才能看出删错了没有；
- 目录里出现新文件名（例如数据库的 -wal 边车文件）时，规则会静默失配；
- 出问题只能靠事后从删除日志反推。

所以把判定抽成无副作用的纯函数：输入都是普通数据（名字、大小、时间戳），
输出是「该删哪些」。副作用只留在调用方（真正删文件的那一层）。
这样 `tests/test_rules.py` 能在毫秒级验证边界条件，不需要造假目录。

约定
----
- 时间一律用 epoch 秒（float），不传 datetime，避免时区混用。
- 函数不读磁盘、不读环境变量、不打印。
- 返回的列表按「先删哪个」排序，调用方可直接顺序执行。
"""
import os
import re
import datetime

# ---------------------------------------------------------------- 目录与任务身份

def norm_dir(p):
    """目录规范化：正斜杠转反斜杠、折叠重复分隔符、去尾斜杠、统一小写。"""
    q = (p or "").replace("/", "\\")
    while "\\\\" in q:
        q = q.replace("\\\\", "\\")
    return q.lower().rstrip("\\")


def task_key(cwd, title):
    """任务身份键 = 规范化目录 + 标题。

    与 wd_unify / wd_watch 使用同一口径，跨账号比对靠它。
    标题的 None 与空串视为同一个键（数据库里两种都出现过）。
    """
    return (norm_dir(cwd), (title or "").strip())


def merge_excluded_roots(existing, adds):
    """把新增的排除根并入客户端的 `excludedBackupRoots` 列表。

    客户端的对照表是一串绝对路径（大小写、斜杠、尾斜杠都不统一），
    直接 append 会重复；按 norm_dir 判重，保留原有条目与原有顺序，
    新项追加在末尾。只做纯计算，不读不写文件。

    返回 (新列表, 实际新增的条目)。
    """
    seen = set()
    out = []
    for x in existing or []:
        if not isinstance(x, str):
            continue
        out.append(x)
        seen.add(norm_dir(x))
    added = []
    for a in adds or []:
        k = norm_dir(a)
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(a)
        added.append(a)
    return out, added


# ---------------------------------------------------------------- 备份保留策略

SIDECARS = ("-wal", "-shm")     # SQLite 边车文件，必须与主库同生同灭

_STAMP = re.compile(r"(20\d{2})(\d{2})(\d{2})[-_]?(\d{2})(\d{2})(\d{2})")


def stamp_of(path, fallback=0.0):
    """从文件名里的时间戳取「快照批次时刻」，取不到再用 fallback（mtime）。

    为什么不直接信 mtime：本机实测同一个快照文件在不同次读取里 mtime 会
    漂移（沙箱/杀软/复制过程都会改 mtime），而文件名里的 20261019-181653
    是生成时就写死的，不会被后续操作改动。
    「哪个是最新快照」必须由确定性信息决定，否则保留策略会随机删掉最新那份。
    """
    m = _STAMP.search(os.path.basename(str(path)))
    if not m:
        return float(fallback)
    try:
        return datetime.datetime(*(int(x) for x in m.groups())).timestamp()
    except ValueError:
        return float(fallback)



def backup_unit(path):
    """把文件路径折算成「一个快照单元」的标识。

    同一批快照可能包含 x.db / x.db-wal / x.db-shm，它们是同一个单元，
    只能一起删或一起留；否则留下孤立的 -wal 会让下次打开时报错。
    """
    name = os.path.basename(str(path).rstrip("\\/"))
    for suf in SIDECARS:
        if name.endswith(suf):
            name = name[:-len(suf)]
            break
    return os.path.join(os.path.dirname(str(path)), name)


def plan_backup_deletions(units, keep_per_dir=1, min_global=2):
    """决定删哪些快照单元。

    参数
    ----
    units : [(unit_path, size_bytes, mtime), ...]
        每个单元一条记录；同一单元的多份文件由调用方先合并（取最大 mtime、
        求和 size）。
    keep_per_dir : 每个目录保留最新几个单元。
    min_global : 全局最少保留几个单元（跨目录合计）。

    返回
    ----
    (deletions, kept)
        deletions : [(unit_path, size_bytes)]，按 mtime 升序（先删最老的）
        kept      : [(unit_path, size_bytes)]，按 mtime 降序

    规则
    ----
    1. 每个目录内按 mtime 降序，前 keep_per_dir 个保留。
    2. 删除后全局剩余不足 min_global 时，从待删项里把最新的几个撤回，
       直到满足下限 —— 宁可少腾空间，也不把回滚点删空。
    3. 目录之间互不比较：不同目录是不同批次的产物，各自保留自己的最新。
    """
    by_dir = {}
    for path, size, mtime in units:
        by_dir.setdefault(os.path.dirname(str(path)), []).append(
            (str(path), int(size), float(mtime)))

    deletable, kept = [], []
    for _d, rows in by_dir.items():
        rows.sort(key=lambda r: r[2], reverse=True)
        kept.extend(rows[:keep_per_dir])
        deletable.extend(rows[keep_per_dir:])

    total_units = len(kept) + len(deletable)
    need_back = min_global - (total_units - len(deletable))
    if need_back > 0 and deletable:
        deletable.sort(key=lambda r: r[2], reverse=True)
        kept.extend(deletable[:need_back])
        deletable = deletable[need_back:]

    deletable.sort(key=lambda r: r[2])          # 先删最老的
    kept.sort(key=lambda r: r[2], reverse=True)
    return ([(p, s) for p, s, _m in deletable],
            [(p, s) for p, s, _m in kept])


# ---------------------------------------------------------------- 影子副本保留策略

# 值得保留「上一版本」的扩展名。其余一律视为系统文件或机器产物：
# 客户端会把日志、Edge 安装包、icudtl.dat、WebView.dll、无后缀哈希块也整份
# 复制，这些不存在「恢复到上一版本」的语义（2026-10-19 实测占 423 MB）。
DOC_EXT = {
    ".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt",
    ".txt", ".md", ".csv", ".json", ".yaml", ".yml",
    ".py", ".js", ".ts", ".html", ".css",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".svg",
    ".zip", ".rar", ".7z", ".eml", ".msg",
}


def plan_active_versions(entries, now_ts, idle_min=10, keep_docs=2, doc_ext=DOC_EXT,
                         non_doc_idle_min=None):
    """活跃会话内影子副本的回收计划。

    为什么活跃会话也要回收：最大的产生方恰恰是**当前正在用的会话** ——
    2026-10-19 实测单个会话 30 分钟就复制出 127 MB，其中 110 MB 是客户端
    自己的日志副本。只清安静超 6 小时的会话，等它安静下来磁盘早已被占满。

    entries : [(name, source_name, size_bytes, mtime)]
        name 是影子文件名，source_name 是剥掉前缀后的原始文件名。
    now_ts : 当前时间（epoch 秒）。
    idle_min : 文档副本闲置不足这么多分钟的不动，避免和正在进行的写操作冲突。
    keep_docs : 文档类每个源文件名保留最新几份。
    non_doc_idle_min : 非文档副本的闲置阈值，默认与 idle_min 相同。
        日志类内容（.log/.jsonl 等）是持续追加的，客户端每追加一次就复制一份
        （实测 3 分钟 280 个新副本），用 10 分钟阈值等于永远追不上；这类副本
        又没有「恢复到上一版本」的语义，因此单独给一个更短的阈值。

    返回 [(name, size_bytes)]，按 mtime 升序（先删最老的）。

    规则
    ----
    1. 闲置不足阈值 → 跳过（可能正在写）。
    2. 源文件名扩展名不在 doc_ext → 回收：日志、二进制、哈希块没有版本语义。
    3. 文档类 → 同一源文件名按 mtime 降序保留最新 keep_docs 份，其余回收。
    """
    hot = float(idle_min if non_doc_idle_min is None else non_doc_idle_min)
    doc_cutoff = float(now_ts) - float(idle_min) * 60
    non_doc_cutoff = float(now_ts) - hot * 60
    groups = {}
    out = []
    for name, source, size, mtime in entries:
        mtime = float(mtime)
        ext = os.path.splitext(str(source))[1].lower()
        if ext not in doc_ext:
            if mtime <= non_doc_cutoff:
                out.append((str(name), int(size), mtime))
            continue
        if mtime > doc_cutoff:
            continue
        groups.setdefault(str(source).lower(), []).append((str(name), int(size), mtime))
    for rows in groups.values():
        rows.sort(key=lambda r: r[2], reverse=True)
        out.extend(rows[max(0, int(keep_docs)):])
    out.sort(key=lambda r: r[2])
    return [(n, s) for n, s, _m in out]


# ---------------------------------------------------------------- 日志预算淘汰

def plan_log_evictions(entries, budget_bytes):
    """按总预算淘汰日志。

    entries : [(path, size_bytes, mtime, protected), ...]
        protected=True 表示今天正在写入的目录/文件，永不淘汰。
    budget_bytes : 参与统计的日志总量上限。

    返回 [(path, size_bytes)]，按 mtime 升序。

    淘汰顺序：先老后新。当只剩受保护项、总量仍超预算时，返回已能给到的
    淘汰清单而不报错 —— 超额压力由调用方记录，不在规则层伪造结论。
    """
    total = sum(int(e[1]) for e in entries)
    if total <= budget_bytes:
        return []

    victims = sorted((e for e in entries if not e[3]), key=lambda e: e[2])
    out, cur = [], total
    for path, size, _mtime, _prot in victims:
        if cur <= budget_bytes:
            break
        out.append((path, int(size)))
        cur -= int(size)
    return out


# ---------------------------------------------------------------- 沙箱日志判定

def should_trash_sandbox(mtime, now_ts, idle_min=30, age_days=0, keep_days=1):
    """沙箱日志是否可删。

    - 目录年龄 >= keep_days：整天目录都过期，可整目录删。
    - 当天目录：只删闲置超过 idle_min 分钟的文件（进程已退出，不会再写）。
    """
    if age_days >= keep_days:
        return True
    return (now_ts - float(mtime)) >= idle_min * 60

# ---------------------------------------------------------------- 滚动日志封口

ROLL_RE = re.compile(r"^(?P<stem>.+)_(?P<idx>\d{3,})\.log$")


def roll_seal(entries):
    """判断哪些滚动日志已经封口。

    写入方写满一份就滚一份（`sandbox_center_17060_001.log` → `_002.log`）。
    同前缀存在「序号更高且写入更晚」的文件，才说明前一份真的滚走了。

    为什么必须带时间：进程重启后序号会回到 001（2026-10-19 实测 001 在写、
    005 已停），只按序号比较会把正在写的文件判成封口。

    entries: 可迭代的 (文件名, mtime)。返回 {文件名: 是否已封口}，
    非滚动命名的文件不出现在结果里。
    """
    best = {}                      # stem -> (idx, mtime)
    parsed = []
    for name, mtime in entries:
        m = ROLL_RE.match(name)
        if not m:
            continue
        idx = int(m.group("idx"))
        parsed.append((name, m.group("stem"), idx, float(mtime)))
        cur = best.get(m.group("stem"))
        if cur is None or idx > cur[0]:
            best[m.group("stem")] = (idx, float(mtime))
        # 同序号取更晚的
        elif idx == cur[0] and float(mtime) > cur[1]:
            best[m.group("stem")] = (idx, float(mtime))

    out = {}
    for name, stem, idx, mtime in parsed:
        top_idx, top_mtime = best[stem]
        out[name] = idx < top_idx and mtime < top_mtime
    return out


def plan_roll_evictions(entries, keep_per_stem=0, protected=()):
    """已封口的滚动日志按「同前缀只留最新 N 份」回收。

    为什么不能只按闲置时长：SEALED_MIN 是分钟口径，同前缀会同时滞留多份
    100 MB 的封口文件 —— 2026-10-19 实测当天沙箱目录 318 MB 里，有 104 MB
    是一份早已滚走的 000 号文件，闲置阈值再宽也不会被选中。

    entries : [(name, size_bytes, mtime, stem)]，调用方已筛出「封口」的那些。
    keep_per_stem : 同前缀保留最新几份（默认 0 = 封口文件全部可回收，
        因为写入方已经切到更高序号，旧文件不会再被追加）。
    protected : 文件名集合，命中即跳过。

    返回 [(name, size_bytes)]，按 mtime 升序（先删最老的）。
    """
    by_stem = {}
    for name, size, mtime, stem in entries:
        if name in protected:
            continue
        by_stem.setdefault(stem, []).append((str(name), int(size), float(mtime)))
    out = []
    for rows in by_stem.values():
        rows.sort(key=lambda r: r[2], reverse=True)      # 新的在前
        out.extend(rows[max(0, int(keep_per_stem)):])
    out.sort(key=lambda r: r[2])
    return [(n, s) for n, s, _m in out]


_SCRIPT_EXT = (".py", ".pyw", ".mjs", ".js", ".bat", ".ps1", ".cmd")


def task_script_path(arguments):
    """从计划任务的 Arguments 里取出真正被执行的脚本路径。

    任务的 Command 是解释器（pythonw.exe / node.exe），脚本在 Arguments 里，
    形如 `"C:\\tools\\_session-sync\\wd_watch.py"`，路径可能带空格、被引号
    包住，前面也可能还跟着别的开关。逐 token 取第一个后缀像脚本的。

    返回原始字符串（不规范化大小写，方便报错时原样展示）；取不到返回 ""。
    """
    if not arguments:
        return ""
    for m in re.finditer(r'"([^"]+)"|(\S+)', str(arguments)):
        tok = (m.group(1) or m.group(2) or "").strip().strip('"')
        if tok.lower().endswith(_SCRIPT_EXT):
            return tok
    return ""


def judge_task_target(target, expected, exists=None):
    """判定计划任务指向的脚本是不是当前包里的那一个。

    为什么需要单列一条判据：只看「任务定义文件存在 + 能解析出 Command」不够。
    任务指向别的脚本时（改名后没重注册、或指向另一个包），文件同样存在、命令
    同样解析得出，检查照样全绿，而守护实际跑的是那段别的代码 —— 假绿灯最常见的
    来源就是这一条。

    target   : task_script_path() 的结果
    expected : 当前包期望的脚本文件名，如 "wd_watch.py"
    exists   : 可注入的存在性判断（单测用），默认 os.path.exists

    返回 (state, detail)，state ∈ {"ok", "warn", "fail"}：
      ok   —— 名字对得上且文件在
      warn —— 命令里没有脚本，或指向的是别的脚本
      fail —— 名字对得上但文件不在了
    """
    exists = exists or os.path.exists
    if not target:
        return "warn", "任务命令里看不出脚本路径"
    name = os.path.basename(target)
    if name.lower() != expected.lower():
        if not exists(target):
            return "fail", "指向 %s，文件已不存在" % name
        return "warn", "仍指向旧脚本 %s（当前包为 %s）" % (name, expected)
    if not exists(target):
        return "fail", "指向 %s，但文件不存在" % target
    return "ok", name


# ---------------------------------------------------------------- 定时任务判据
def judge_automations(per_user, dup_names=0):
    """判定「各账号的定时任务」是否健康。

    为什么不比条数：WorkDaddy 的定时任务只属于创建它的账号，跨账号可见性由
    上游 `official-automation-sync` 决定 —— 它什么时候补、补几份，本包控制不了。
    2026-10-20 本机 44 条未删任务里 26 条是历史镜像副本（`owner_source='mirror'`），
    而且上游把副本当源又复制了一层，`total` 这个数字里混着两层副本。用「各账号
    条数一致」当判据，等于拿一个受外部节奏摆布的数去比大小，稳定误报。

    真正的坏法只有两种，所以判据只认两种：
      1. 某账号一条自有任务都没有 —— 切过去看到的空列表（原 bug 的场景）；
         只剩 PAUSED 副本也算，因为副本不调度、只是列表里的一个空壳。
      2. 同账号内同名多条 —— 界面上「同一任务好几份」的真身。
    mirror 副本本身不算坏（它们 PAUSED、不调度），只作为明细展示。

    per_user : {账号前 8 位: {"total": n, "own": n, "paused": n, "mirror": n}}
               `own` 缺失时回退为 total，兼容旧调用方。
    dup_names: 同账号内同名的未删任务组数
    返回 (state, detail)，state ∈ {"ok", "warn", "fail"}
    """
    if not per_user:
        return "fail", "一条都没有"
    no_own = [k for k, v in per_user.items() if not v.get("own", v.get("total", 0))]
    detail = "  ".join("%s:自有%d/共%d" % (k, v.get("own", v.get("total", 0)), v["total"])
                       for k, v in per_user.items())
    if no_own:
        detail += "（%s 无自有任务）" % "、".join(no_own)
    if dup_names:
        detail += "（同账号重名 %d 组）" % dup_names
    if no_own or dup_names:
        return "warn", detail
    return "ok", detail


# ---------------------------------------------------------------- 数据增量
def judge_growth(sizes_mb, warn_mb=2048, fail_mb=3072, dir_warn_mb=1024):
    """判定「按会话累积、谁都不回收」的目录有没有失控。

    为什么单列一项：日志、备份、会话附属数据、库这四块只覆盖 `~/.workbuddy`
    的 5 GB 里约 1 GB，剩下 `changes-detail`、`blobs`、`cache`、
    `file-tree-manifests`、`traces` 这几个目录按会话只增不减，却不在视野里。
    2026-10-19 实测 `changes-detail` 已 84 MB，其中单个会话占 59 MB；哪个
    会话把它撑到几 GB，体检不会报，要等磁盘告警才发现 —— 又是一种「看着
    正常、实际在积累」。

    阈值为什么定这么松（合计 2 GB / 单目录 1 GB）：
      - 本机实测这五项合计 329 MB，2 GB 留了 6 倍余量，不会天天误报；
      - C 盘 376 GB 空闲，4 GB 以内不值得打扰人。
    监控的目的是「明显失控时说话」，不是精确记账。

    为什么不监控 `projects`（转录，591 MB）：那是核心资产，会话多就该大，
    不能按「可回收」对待。也不监控 `binaries`/`plugins`：程序本体按版本
    整体替换，不会随使用累积，且文件数几万，遍历代价不划算。

    sizes_mb : {目录名: MB}
    返回 (state, detail)，state ∈ {"ok", "warn", "fail"}
    """
    if not sizes_mb:
        return "warn", "没读到任何数据目录（路径可能不对）"
    total = sum(sizes_mb.values())
    ranked = sorted(((v, k) for k, v in sizes_mb.items()), reverse=True)
    top_mb, top_name = ranked[0]
    detail = "合计 %.0f MB（上限 %d MB），最大 %s %.0f MB；%s" % (
        total, warn_mb, top_name, top_mb,
        "、".join("%s %.0f" % (k, v) for v, k in ranked[:3]))
    if total > fail_mb:
        return "fail", detail
    if total > warn_mb or top_mb > dir_warn_mb:
        return "warn", detail
    return "ok", detail


