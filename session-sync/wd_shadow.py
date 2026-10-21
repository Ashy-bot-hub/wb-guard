# -*- coding: utf-8 -*-
"""会话附属数据回收：影子副本、诊断轨迹、编辑器历史。

三类数据同一性质 —— 都按「归属者」分目录，归属者消失后数据永远不会再被用到：

| 目录 | 分目录键 | 判废依据 |
|---|---|---|
| `workspace/sessions/<会话>/modify_backup` | 会话 | 安静超 KEEP_ACTIVE_HOURS 整目录清；活跃会话内非文档副本闲置 ≥ ACTIVE_IDLE_MIN 即回收，文档每源文件留最新 KEEP_DOC_VERSIONS 份 |
| `traces/<pid>` | 进程号 | 该 PID 已不存在 |
| `file-history/<会话>` | 会话 | 会话安静超 KEEP_ACTIVE_HOURS |

这不是「清理缓存」，而是「清掉已死进程和已关会话留下的完整副本」。其中
`modify_backup` 是最大头：客户端每改一个文件就整份复制一次，一个 19 MB 的
库改 9 次就是 171 MB，一段转录改 4 次又是 80 MB，且会话关闭后从不回收 ——
2026-10-19 实测 46 个会话累计 843 MB，占 `.workbuddy` 的 15%。

更关键的是：客户端**连自己的日志也整份复制**。2026-10-19 复查发现一个活跃
会话 30 分钟就复制出 127 MB，其中 110 MB 是 `logs/sandbox` 的副本，单份
`sandbox_center_*.log` 就 94 MB。所以除了删副本，还要断掉复制链本身 ——
`--exclude-logs` 把 `logs` 写进客户端自带的 `excludedBackupRoots` 白名单。

严格不动：
  - `projects/`（会话转录，是你的正文）
  - `blobs/`（内容寻址存储，可能被任意会话引用）
  - 闲置不足 ACTIVE_IDLE_MIN 分钟的副本（可能正在写）
  - 文档类副本的前 KEEP_DOC_VERSIONS 份（要留回滚点）
  - 仍然存活的 PID / 索引表里没有对应副本的条目

副本索引（`.modify_backup_meta`）不是「一律不碰」，而是**跟着副本走**：
删哪一份副本就删它对应的那一条索引，两者必须成对，否则客户端会按索引
把副本重新复制出来。

离线强制模式（--force）解决的是另一个问题：正在用的会话那部分清不掉。
客户端把 `modify_backup` 自身也算作「被改文件」，删除后按 `.modify_backup_meta`
把文件从记录的源路径再复制回来。2026-10-19 实测：981 MB 清空后 10 分钟回到
1087 MB，文件数 9419 → 24722，新文件名带上了「影子的影子」前缀。

因此离线清空必须**连索引一起删**，并且必须**先退出客户端**，否则等于白做。
代价是该会话在界面上失去「恢复上一版本」的历史 —— 文件本身不会被改动。

用法：
    python wd_shadow.py              # 预演
    python wd_shadow.py --apply      # 执行
    python wd_shadow.py --json       # 机器可读
    python wd_shadow.py --force      # 预演离线全量（只读，可随时看）
    python wd_shadow.py --force --apply       # 离线全量清空，需先退出客户端
    python wd_shadow.py --force --apply --limit 3   # 先拿 3 个会话试手
    python wd_shadow.py --exclude-logs          # 预演：把 logs 加入备份排除表
    python wd_shadow.py --exclude-logs --apply  # 写入（根因治理，幂等）
"""

import argparse
import ctypes
import json
import os
import re
import shutil
import subprocess
import tempfile
import sys
import time

import wdconfig as C
import wd_fs as F
import wdrules as R

KEEP_ACTIVE_HOURS = 6        # 会话安静多久算「已结束」
KEEP_TRACE_HOURS = 1         # 进程刚退出时的轨迹可能还有用，留一小时
ACTIVE_IDLE_MIN = 10         # 活跃会话内，文档副本闲置 10 分钟即可回收
NON_DOC_IDLE_MIN = 2         # 活跃会话内，日志/二进制副本只要 2 分钟不追加就回收：
                             # 客户端每追加一次日志就复制一份（实测 3 分钟 280 个），
                             # 10 分钟阈值永远追不上，而这类副本没有回滚价值
KEEP_DOC_VERSIONS = 2        # 活跃会话内，每个用户文档保留最新几份

# 值得保留「上一版本」的扩展名，判断逻辑在 wdrules.plan_active_versions。
# 客户端会把 Edge 安装包、icudtl.dat、WebView.dll、无后缀哈希块也整份复制，
# 这些不存在「恢复到上一版本」的语义（2026-10-19 实测占 423 MB）。
DOC_EXT = R.DOC_EXT

# 影子文件名形如 `3.d.0efa3c6d.原文件名`，前面可能套了不止一层
NAME_RE = re.compile(r"^\d+\.[A-Za-z\-]+\.([0-9a-f]{8})\.")


def source_name(fname):
    """剥掉影子文件的前缀，还原原始文件名。"""
    s = fname
    while True:
        m = NAME_RE.match(s)
        if not m:
            return s
        s = s[m.end():]


def meta_name(fname):
    """影子文件名 → 索引文件名（`<hash>.<原始文件名>`）。

    删副本必须连索引一起删：索引是「影子文件名 → 源路径」的对照表，
    留着它客户端就会按源路径把副本再复制一遍。
    """
    m = NAME_RE.match(fname)
    if not m:
        return None
    return m.group(1) + "." + source_name(fname)


def _base(name):
    return os.path.join(C.HOME, ".workbuddy", name)


def _tree(path):
    """返回 (字节数, 最新 mtime)。逐文件统计，避免目录 mtime 被写操作刷新。

    走 `wd_fs.tree_size()`（scandir 一次拿全 stat）：本机 1,675 个副本文件
    45 ms → 2.7 ms，结果一致。
    """
    return F.tree_size(path)


CLIENT_IMAGE = ("workbuddy.exe", "workdaddy.exe")

# 一次体检里「存活 PID」「客户端是否在跑」「轨迹判废」会各要一次进程表，
# 每跑一次 tasklist 约 220 ms（实测三项合计 672 ms）。改为在短窗口内复用
# 同一份快照：5 秒足够覆盖一次扫描，又不会把刚退出的 PID 长期判成存活。
PROC_TTL = 5.0
_PROC = {"at": -1.0, "pids": None, "names": None}

# 用 Toolhelp32 快照替代 tasklist 子进程。实测 236 ms → 3 ms（79 倍），
# 本机 275 个 PID 与 tasklist 完全一致（差的那 1 个是 tasklist 自己）。
# 子进程慢在进程创建 + 中文控制台输出解码，与「有多少进程」无关。
TH32CS_SNAPPROCESS = 0x00000002
MIN_PLAUSIBLE_PROCS = 20      # 少于这个数视为快照异常，转 tasklist 并拒绝判废


class _PROCESSENTRY32W(ctypes.Structure):
    """Win32 PROCESSENTRY32W（x64 下 568 字节）。

    字段一律用基础 ctypes 类型，不引 `ctypes.wintypes` —— 后者在非 Windows
    上 import 就会抛异常，而这个包里的 `wd_db_maintain` 要 import 本模块。
    `th32DefaultHeapID` 是 ULONG_PTR，用 c_void_p 表达（x64 为 8 字节），
    与官方结构的对齐方式一致；`dwSize` 由 sizeof 算出，不写死。
    """
    _fields_ = [
        ("dwSize", ctypes.c_ulong),
        ("cntUsage", ctypes.c_ulong),
        ("th32ProcessID", ctypes.c_ulong),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", ctypes.c_ulong),
        ("cntThreads", ctypes.c_ulong),
        ("th32ParentProcessID", ctypes.c_ulong),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_ulong),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def _snapshot_processes():
    """Toolhelp32 快照：(PID 集合, 进程名集合)。非 Windows 或调用失败返回 None。"""
    if not hasattr(ctypes, "WinDLL"):
        return None
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = ctypes.c_void_p
        k32.CreateToolhelp32Snapshot.restype = handle
        k32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
        k32.Process32FirstW.argtypes = [handle, ctypes.POINTER(_PROCESSENTRY32W)]
        k32.Process32NextW.argtypes = [handle, ctypes.POINTER(_PROCESSENTRY32W)]
        k32.CloseHandle.argtypes = [handle]
        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        invalid = ctypes.c_void_p(-1).value
        if not snap or snap == invalid:
            return None
        pids, names = set(), set()
        try:
            e = _PROCESSENTRY32W()
            e.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
            ok = k32.Process32FirstW(snap, ctypes.byref(e))
            while ok:
                pids.add(int(e.th32ProcessID))
                names.add(e.szExeFile.strip().lower())
                ok = k32.Process32NextW(snap, ctypes.byref(e))
        finally:
            k32.CloseHandle(snap)
    except (OSError, AttributeError, ValueError):
        return None
    return pids, names            # 完整性判定统一在 _process_table 里做


def _tasklist_processes():
    """兜底：(PID 集合, 进程名集合)。中文 Windows 的 tasklist 输出是 GBK。"""
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                             capture_output=True, timeout=25).stdout or b""
    except (OSError, subprocess.SubprocessError):
        return None, None                # 拿不到就不判废，宁可不删
    pids, names = set(), set()
    for line in out.decode("gbk", "replace").splitlines():
        parts = line.split('","')
        if len(parts) > 1:
            names.add(parts[0].strip('"').strip().lower())
            try:
                pids.add(int(parts[1].strip('"')))
            except ValueError:
                pass
    return pids, names


# 取数顺序：先快路径，再兜底。测试可替换这个列表来注入假数据。
PROC_GETTERS = (_snapshot_processes, _tasklist_processes)


def _process_table(ttl=PROC_TTL):
    """(存活 PID 集合, 进程名集合)。拿不到就返回 (None, None)，且不缓存失败。

    两条取数路径按顺序试：Toolhelp32 快照（3 ms）→ tasklist（236 ms）。
    任一结果少于 `MIN_PLAUSIBLE_PROCS` 个进程就视为不完整，换下一条路径；
    两条都不完整则返回 (None, None) —— **宁可不判废，也不拿残缺的进程表
    去删别人的轨迹**。这个判定放在这里而不是各个 getter 里，是因为它必须
    对两条路径同时生效（测试也因此能直接覆盖）。
    """
    now = time.time()
    if _PROC["pids"] is not None and now - _PROC["at"] < ttl:
        return _PROC["pids"], _PROC["names"]
    for getter in PROC_GETTERS:
        pids, names = getter()
        if pids and len(pids) >= MIN_PLAUSIBLE_PROCS:
            _PROC.update(at=now, pids=pids, names=names)
            return pids, names
    return None, None


def reset_process_cache():
    """丢掉进程表快照；测试与「客户端刚退出」的场景用。"""
    _PROC.update(at=-1.0, pids=None, names=None)


def alive_pids():
    """当前存活 PID。中文 Windows 的 tasklist 输出是 GBK，必须按字节解码。"""
    return _process_table()[0]


def force_blocked(running, apply):
    """离线清空是否拦下：返回 None 表示放行，否则返回拦截原因。

    只有「确定客户端已退出」（running is False）才放行。原实现写的是
    `if running and args.apply` —— 而 `client_running()` 查不到进程表时返回
    None，None 是假值，于是「不确定」被当成了「已退出」，离线清空照做。
    那是这条路径上唯一不可逆的操作（连索引一起删），不能靠假值语义决定。
    """
    if not apply:
        return None
    if running is False:
        return None
    return "running" if running else "unknown"


def client_running():
    """客户端是否在运行。用于挡住「运行中做离线清空」这种白做的操作。"""
    names = _process_table()[1]
    if names is None:
        return None                      # 查不到就不放行，由调用方决定
    return any(n in names for n in CLIENT_IMAGE)


def _dirs(root):
    try:
        return [d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))]
    except OSError:
        return []


# ---------------------------------------------------------------- 根因：日志不该被复制

LOG_EXCLUDE_ROOT = os.path.join(C.WORKBUDDY, "logs")


def excluded_config_paths():
    root = os.path.join(_base("workspace"), "sessions")
    return [os.path.join(root, sid, "excluded_backup_roots.json") for sid in _dirs(root)]


def ensure_logs_excluded(roots=None, paths=None, write=True):
    """把客户端日志目录写进每个会话的备份排除表。

    为什么这是根因治理，而不是又一轮删除：客户端把「被修改的文件」整份复制进
    `modify_backup`，而它自己的日志一直在被追加。2026-10-19 实测一个会话的
    127 MB 影子副本里 110 MB 是客户端日志的副本，单份 `sandbox_center_*.log`
    就 94 MB。删除只能治标 —— 删掉副本，日志继续被追加，下一轮照样复制回来。

    客户端自带 `excludedBackupRoots` 白名单（已排除 plugins / binaries /
    skills-marketplace），把 `logs` 加进去，这条复制链就断了。配置按会话各存
    一份（本机 124 份），因此按文件逐个幂等合并；客户端重启若重写该文件，
    守护下一轮会自动补回。

    返回 {"files": 配置文件数, "patched": 实际改写数, "added": [新增条目]}。
    """
    roots = list(roots) if roots else [LOG_EXCLUDE_ROOT]
    targets = paths if paths is not None else excluded_config_paths()
    patched, added = 0, []
    for p in targets:
        try:
            with open(p, encoding="utf-8") as f:
                cfg = json.load(f)
        except (OSError, ValueError):
            cfg = {}
        if not isinstance(cfg, dict):
            cfg = {}
        cur = cfg.get("excludedBackupRoots")
        if not isinstance(cur, list):
            cur = []
        new, got = R.merge_excluded_roots(cur, roots)
        if not got:
            continue
        cfg["excludedBackupRoots"] = new
        if not write:
            patched += 1
            added.extend(got)
            continue
        # 客户端随时可能读这些会话文件，替换时目标被占用会失败，
        # 交给带重试的原子写（见 wd_fs 注释），失败只跳过这一份。
        if F.atomic_write(p, json.dumps(cfg, ensure_ascii=False, separators=(",", ":"))):
            patched += 1
            added.extend(got)
    return {"files": len(targets), "patched": patched, "added": sorted(set(added))}


def scan_shadow(now=None, offline=False, stats=None):
    """会话影子副本：整目录或按项回收。

    stats 传入 dict 时，顺手累计「活跃会话占用」的字节数（`active_bytes`），
    让体检用一次遍历同时拿到「可回收」和「须离线」两个数 —— 各走一遍目录树
    实测多花 49 ms。

    offline=False（默认）时**不碰活跃会话**。依据是实测：客户端运行时删掉活跃
    会话的副本，它会在几秒内按索引重建，且每次重建都套一层新前缀 —— 2026-10-19
    实测删掉 133 MB 后 10 秒回到 138 MB，文件数 48 → 566。在线清理活跃会话
    不仅白做，还会把文件名越滚越长。活跃会话的副本只能等客户端退出后用
    `--force` 一次性处理。
    """
    now = now or time.time()
    root = os.path.join(_base("workspace"), "sessions")
    acts = []
    for sid in _dirs(root):
        p = os.path.join(root, sid, "modify_backup")
        if not os.path.isdir(p):
            continue
        size, newest = _tree(p)
        if not size:
            continue
        age_h = (now - newest) / 3600.0
        if age_h < KEEP_ACTIVE_HOURS and stats is not None:
            stats["active_bytes"] = stats.get("active_bytes", 0) + size
        if age_h >= KEEP_ACTIVE_HOURS:
            acts.append({"group": "影子副本", "owner": sid, "kind": "purge",
                         "paths": [(p, size)], "bytes": size,
                         "reason": "会话已安静 %.1f 小时" % age_h})
            continue
        if not offline:
            continue
        # 活跃会话：按「非文档直接回收 + 文档留最新 N 份」处理，而不是干等 7 天。
        # 实测单个活跃会话 30 分钟就能复制出 127 MB，等它安静 6 小时没有意义。
        meta_dir = os.path.join(root, sid, ".modify_backup_meta")
        rows = [(os.path.basename(fp), source_name(os.path.basename(fp)), s, m)
                for fp, s, m in F.iter_files(p)]
        picked = R.plan_active_versions(rows, now, ACTIVE_IDLE_MIN, KEEP_DOC_VERSIONS,
                                        non_doc_idle_min=NON_DOC_IDLE_MIN)
        if picked:
            paths, total = [], 0
            for f, size in picked:
                paths.append((os.path.join(p, f), size))
                total += size
                mn = meta_name(f)
                mp = os.path.join(meta_dir, mn) if mn else None
                if mp and os.path.isfile(mp):
                    try:
                        ms = os.path.getsize(mp)
                    except OSError:
                        ms = 0
                    paths.append((mp, ms))
                    total += ms
            acts.append({"group": "影子副本", "owner": sid, "kind": "active",
                         "paths": paths, "bytes": total,
                         "reason": "活跃会话内 %d 项副本（非文档闲置 ≥ %d 分钟、"
                                   "文档闲置 ≥ %d 分钟且只留最新 %d 份）"
                                   % (len(picked), NON_DOC_IDLE_MIN, ACTIVE_IDLE_MIN,
                                      KEEP_DOC_VERSIONS)})
    return acts


def scan_traces(now=None):
    """诊断轨迹：PID 已退出的目录。取不到进程表时整段跳过。"""
    now = now or time.time()
    alive = alive_pids()
    if alive is None:
        return []
    root = _base("traces")
    acts = []
    for d in _dirs(root):
        if not d.isdigit() or int(d) in alive:
            continue
        p = os.path.join(root, d)
        size, newest = _tree(p)
        if not size or (now - newest) < KEEP_TRACE_HOURS * 3600:
            continue
        acts.append({"group": "诊断轨迹", "owner": "PID %s" % d, "kind": "purge",
                     "paths": [(p, size)], "bytes": size,
                     "reason": "进程已退出 %.1f 小时" % ((now - newest) / 3600.0)})
    return acts


def scan_history(now=None):
    """编辑器文件历史：会话安静超阈值的目录。"""
    now = now or time.time()
    root = _base("file-history")
    acts = []
    for sid in _dirs(root):
        p = os.path.join(root, sid)
        size, newest = _tree(p)
        if not size:
            continue
        age_h = (now - newest) / 3600.0
        if age_h < KEEP_ACTIVE_HOURS:
            continue
        acts.append({"group": "编辑器历史", "owner": sid, "kind": "purge",
                     "paths": [(p, size)], "bytes": size,
                     "reason": "会话已安静 %.1f 小时" % age_h})
    return acts


def scan_force(now=None):
    """离线全量：所有会话的影子副本 + 副本索引，不看到底安静了多久。

    为什么索引必须一起清：索引是「影子文件名 → 源路径」的对照表，客户端靠它
    重建被删的副本。只删副本留着索引，等于把源路径指过的文件再复制一遍：
    索引里可以有 18806 条指向隔离目录、9384 条指向索引备份目录的条目，重建
    出来的全是「影子的影子」。
    """
    root = os.path.join(_base("workspace"), "sessions")
    acts = []
    for sid in _dirs(root):
        sdir = os.path.join(root, sid)
        paths, total = [], 0
        for name, label in (("modify_backup", "影子副本"),
                            (".modify_backup_meta", "副本索引")):
            p = os.path.join(sdir, name)
            if not os.path.isdir(p):
                continue
            size, _newest = _tree(p)
            if size:
                paths.append((p, size))
                total += size
        if not paths:
            continue
        acts.append({"group": "离线清空", "owner": sid, "kind": "purge",
                     "paths": paths, "bytes": total,
                     "reason": "离线强制回收（副本 + 索引，防止客户端重建）"})
    # 从小到大：--limit 时先拿小的试手，出错代价最小
    acts.sort(key=lambda a: a["bytes"])
    return acts


def scan_with_active(now=None, offline=False):
    """一次遍历同时给出 (可回收动作, 活跃会话占用字节)。"""
    stats = {}
    acts = scan_shadow(now, offline, stats) + scan_traces(now) + scan_history(now)
    acts.sort(key=lambda a: -a["bytes"])
    return acts, stats.get("active_bytes", 0)


def scan(now=None, offline=False):
    return scan_with_active(now, offline)[0]


def _purge_dir(path):
    """整目录清空，返回是否成功。

    用 robocopy 把空目录镜像过去，而不是逐文件 os.remove：后者在这个环境里
    会被客户端的目录监听拖住，实测 2700 个文件删了 2 分半零进展，最后连进程
    都一起被掐掉（SIGTERM）。robocopy 走另一条路径，1 万个文件几秒清完。
    """
    empty = tempfile.mkdtemp(prefix="wd-empty-")
    try:
        r = subprocess.run(["robocopy", empty, path, "/MIR", "/NFL", "/NDL",
                            "/NJH", "/NJS", "/NC", "/NS", "/NP", "/R:0", "/W:0"],
                           capture_output=True, timeout=600)
        if r.returncode < 8:                 # robocopy 0-7 都是成功
            return True
    except (OSError, subprocess.SubprocessError):
        pass
    finally:
        shutil.rmtree(empty, ignore_errors=True)
    return False


# 删除前的最后一道闸：这些相对路径本身是「容器」，删了等于清空一整类数据。
# 正常情况下动作清单里的路径都是 workspace/sessions/<sid>/modify_backup 这种
# 叶子目录，不会命中；但如果哪天上游拼接出错（sid 为空、join 少一层），
# 没有这道闸就会把整个 sessions 目录 robocopy /MIR 清空 —— 那是不可逆的。
_DENY_REL = {
    "", "workspace", "workspace/sessions", "traces", "file-history",
    "projects", "blobs", "binaries", "plugins", "logs", "cache", "backups",
}


def _safe_target(path):
    """目标必须落在 ~/.workbuddy 之内，且不是任何一个顶层容器。"""
    try:
        root = os.path.realpath(C.WORKBUDDY)
        tgt = os.path.realpath(path)
    except OSError:
        return False
    if tgt == root or not tgt.startswith(root + os.sep):
        return False
    rel = os.path.relpath(tgt, root).replace(os.sep, "/")
    return rel not in _DENY_REL


def _remove(fp):
    """删文件或目录；返回实际释放的字节数（删不掉就是 0）。"""
    if not _safe_target(fp):
        return 0
    try:
        if os.path.isdir(fp):
            before, _ = _tree(fp)
            if not _purge_dir(fp):
                shutil.rmtree(fp, ignore_errors=True)
            after, _ = _tree(fp)
            return before - after
        size = os.path.getsize(fp)
        os.remove(fp)
        return size
    except OSError:
        return 0


def apply(acts):
    freed, failed = 0, []
    for a in acts:
        for p, _size in a["paths"]:
            got = _remove(p)
            freed += got
            if got == 0:
                failed.append(p)
    return freed, failed


def pretty(path):
    try:
        return os.path.relpath(path, os.path.join(C.HOME, ".workbuddy"))
    except ValueError:
        return path


def main():
    ap = argparse.ArgumentParser(description="会话附属数据回收")
    ap.add_argument("--apply", action="store_true", help="真正删除（默认只预演）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--force", action="store_true",
                    help="离线全量：清所有会话的副本与索引，需先退出客户端")
    ap.add_argument("--limit", type=int, default=0,
                    help="最多处理 N 个会话（按体积从小到大），配合 --force 分步试手")
    ap.add_argument("--exclude-logs", action="store_true",
                    help="把客户端 logs 目录加入备份排除表（根因治理；配 --apply 才写）")
    args = ap.parse_args()

    if args.exclude_logs:
        r = ensure_logs_excluded(write=args.apply)
        print("备份排除表：%d 份配置，需改写 %d 份，新增条目 %d 个"
              % (r["files"], r["patched"], len(r["added"])))
        for a in r["added"][:5]:
            print("  + " + a)
        print("已写入" if args.apply else "预演结束，加 --apply 写入")
        return 0

    if args.force:
        running = client_running()
        blocked = force_blocked(running, args.apply)
        if blocked:
            if blocked == "running":
                print("客户端正在运行，已拒绝执行。")
                print("  离线清空必须连索引一起删，而客户端运行时会把索引里的源路径再复制一遍，")
                print("  实测 981 MB 清空后 10 分钟回到 1087 MB，等于白做还更大。")
                print("  请先在托盘图标上右键退出客户端，等 10 秒再重跑。")
            else:
                print("无法确认客户端是否已退出（读取进程表失败），已拒绝执行。")
                print("  离线清空不可逆，只在「确定客户端已退出」时才放行。")
                print("  请手动确认托盘里已退出 WorkBuddy，再重跑；或先跑一次不带 --force 的预演。")
            return 3
        acts = scan_force()
        if args.limit:
            acts = acts[:max(0, args.limit)]
    else:
        running = client_running()
        acts = scan(offline=(running is False))
        if running is True:
            print("说明：客户端正在运行，活跃会话的副本在线删除会被立刻重建"
                  "（实测 10 秒回到原体积），本轮只清理已结束会话。")
        elif running is None:
            print("说明：无法确认客户端是否在运行（读取进程表失败），本轮按「可能在运行」处理。")
    by_group = {}
    for a in acts:
        g = by_group.setdefault(a["group"], [0, 0])
        g[0] += 1
        g[1] += a["bytes"]

    if args.json:
        print(json.dumps({
            "groups": {k: {"dirs": v[0], "bytes": v[1]} for k, v in by_group.items()},
            "total_bytes": sum(a["bytes"] for a in acts),
            "actions": [{"group": a["group"], "owner": a["owner"],
                         "bytes": a["bytes"], "reason": a["reason"]} for a in acts],
        }, ensure_ascii=False))
        return 0

    if not acts:
        print("无可回收项")
        return 0

    print("%-8s %8s %8s  %s" % ("类别", "项数", "体积", "说明"))
    print("-" * 68)
    for g, (n, b) in sorted(by_group.items(), key=lambda kv: -kv[1][1]):
        print("%-8s %8d %7.1f MB" % (g, n, b / 1048576))
    print("-" * 68)
    total = sum(a["bytes"] for a in acts)
    print("合计可回收 %.1f MB" % (total / 1048576))
    # 排序口径与体积无关（离线模式按从小到大排是为了 --limit），展示一律取最大的
    for a in sorted(acts, key=lambda x: -x["bytes"])[:5]:
        print("  最大项  %7.1f MB  %s" % (a["bytes"] / 1048576, a["owner"][:40]))

    if not args.apply:
        print("预演结束，加 --apply 执行")
        return 0

    freed, failed = apply(acts)
    print("已回收 %.1f MB" % (freed / 1048576))
    if failed:
        print("删除失败 %d 项（多为客户端正占用）：" % len(failed))
        for p in failed[:5]:
            print("  " + pretty(p))
    return 0


if __name__ == "__main__":
    sys.exit(main())
