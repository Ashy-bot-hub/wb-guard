# -*- coding: utf-8 -*-
"""公共配置：路径探测、账号发现、目录规范化。

所有脚本都从这里取路径，不写死用户名与盘符，因此整个目录可以直接拷到
另一台 Windows 电脑使用。唯一前提是该机器已装 WorkBuddy 并登录过至少两个账号。
"""
import glob
import json
import os
import shutil
import sys

HOME = os.path.expanduser("~")

# WorkBuddy 数据根（会话库与转录）
WORKBUDDY = os.path.join(HOME, ".workbuddy")
DB = os.path.join(WORKBUDDY, "workbuddy.db")
PROJ = os.path.join(WORKBUDDY, "projects")
APPLOGS = os.path.join(WORKBUDDY, "logs")          # 客户端日志（sandbox / 每日会话日志）
EDGE_PROFILE = os.path.join(WORKBUDDY, "edge-automation")   # 浏览器自动化 profile

# WorkDaddy 客户端配置根（账号标记、autoCopy 索引）
APPDATA = os.environ.get("APPDATA") or os.path.join(HOME, "AppData", "Roaming")
DADDY = os.path.join(APPDATA, "WorkDaddy")
ACC_DIR = os.path.join(DADDY, "accounts")
META = os.path.join(DADDY, "meta.json")

# 本包根目录（运行时生成物都放在这里，便于整体搬迁）
HERE = os.path.dirname(os.path.abspath(__file__))
BACKUP = os.path.join(HERE, "backups")
LOGS = os.path.join(HERE, "logs")
OVERRIDE = os.path.join(HERE, "accounts.json")   # 可选：手工指定参与镜像的账号

def _resolve_cdp_port(default=9222):
    """CDP 端口解析：环境变量 -> 客户端自写的 cdp-port.json -> 默认值。

    WorkDaddy 1.2.17 起自行挑选调试端口（本机实测 9226），并把结果写进
    %APPDATA%\\WorkDaddy\\cdp-port.json。端口写死 9222 的症状很隐蔽：
    守护进程存活、心跳照写，但注入永远不生效。
    """
    env = os.environ.get("WD_CDP_PORT")
    if env:
        try:
            return int(env)
        except ValueError:
            pass
    cands = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        cands.append(os.path.join(appdata, "WorkDaddy", "cdp-port.json"))
    cands.append(os.path.join(HOME, "AppData", "Roaming", "WorkDaddy", "cdp-port.json"))
    for path_ in cands:
        try:
            with open(path_, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except Exception:
            continue
        val = raw.get("port") if isinstance(raw, dict) else raw
        try:
            if val:
                return int(val)
        except (TypeError, ValueError):
            continue
    return default


CDP_PORT = _resolve_cdp_port()

# ---- 可观测面：写方与读方共用的文件名 ----
# 每个元组是「当前名 + 兼容名」，写方只取 [0]，读方遍历整组。
# 为什么集中到这里：同一个名字分别写进读方与写方，改一处就分叉 —— 守护写的文件
# 体检读不到，或任务名在注册与清理两处不一致。名字只该有一个来源。
PID_GUARD = ("guard.pid", "guard-watch.pid")
# 另一路守护用过的 pid 名，只作「该目录跑过没跑过」的痕迹，本包不产生它。
PID_LEGACY_TIMELINE = ("guard-timeline.pid",)
LOG_GUARD = "guard.log"


def norm(p):
    """目录规范化：正斜杠转反斜杠、折叠重复分隔符、去尾斜杠、统一小写。"""
    q = (p or "").replace("/", "\\")
    while "\\\\" in q:
        q = q.replace("\\\\", "\\")
    return q.lower().rstrip("\\")


def ensure_dirs():
    for d in (BACKUP, LOGS):
        os.makedirs(d, exist_ok=True)


_MUTEXES = {}


def acquire_mutex(name):
    """跨进程互斥：返回 (拿到没有, 句柄)。

    用在两个地方：

    1. 守护单实例（`Local\\WorkDaddy-SyncGuard`）—— 防止命令行再起一个，
       两个实例并发跑 unify 会互相改写转录。
    2. 写库互斥（`Local\\WorkDaddy-Write`）—— 守护每 6 小时自己跑一次
       `wd_unify --apply`，而 README 又教用户手动跑同一个命令。两条路径
       撞在一起时，SQLite 只能保证「行级不撕裂」，保证不了「两次 rebase
       基于同一份快照」，转录硬链接可能被互相覆盖。

    为什么用命名互斥体而不是锁文件：进程退出（含崩溃、被任务管理器结束）
    时内核自动释放，不会留下需要人工判断的僵尸锁。锁文件做不到这一点。

    API 不可用时返回「拿到」：保护本身不该成为脚本起不来的理由。
    """
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.CreateMutexW.argtypes = (wintypes.LPCVOID, wintypes.BOOL, wintypes.LPCWSTR)
        h = k32.CreateMutexW(None, False, name)
        if not h:
            return True, None
        if ctypes.get_last_error() == 183:       # ERROR_ALREADY_EXISTS
            k32.CloseHandle(h)
            return False, None
        _MUTEXES[name] = h                        # 保住引用，进程退出才释放
        return True, h
    except Exception:                             # noqa: BLE001
        return True, None


WRITE_LOCK = "Local\\WorkDaddy-Write"


_WARNED = set()


def _read_json(path, default):
    """读 JSON；读不动就返回默认值。

    但「读不动」分两种：文件本来就不存在（首次运行，正常），以及文件在、
    内容却解析不了（被写坏、被截断）。后者静默返回默认值是有害的 —— 例如
    注册表坏掉后回退成空表，`--undo` 会表现为「没有可撤销的记录」，而不是
    「记录丢了」。所以后者打一条告警，同一个路径只报一次，避免刷屏。
    """
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except OSError:
        return default
    except ValueError as e:
        if path not in _WARNED:
            _WARNED.add(path)
            sys.stderr.write("警告：%s 无法解析（%s），已按空内容处理\n"
                             % (os.path.basename(path), e))
        return default


def logged_uids():
    """本机登录过的账号（每个 .info 文件对应一个账号）。"""
    try:
        names = os.listdir(ACC_DIR)
    except OSError:
        return []
    return sorted(n[:-5] for n in names if n.endswith(".info"))


def active_uid():
    """当前登录账号。

    WorkDaddy 会给多个 .info 同时写 "lastLogin": true（切过的账号不清标），
    所以不能取第一个命中项，只能取 .info 修改时间最新的那个。
    """
    import re
    best, best_mtime = None, -1.0
    for uid in logged_uids():
        path = os.path.join(ACC_DIR, uid + ".info")
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                txt = f.read()
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if re.search(r'"lastLogin"\s*:\s*true', txt) and mtime > best_mtime:
            best, best_mtime = uid, mtime
    return best


def target_accounts(db_uids=None):
    """参与跨账号对齐的账号集合。

    优先级：accounts.json 的 uids > 本机登录过的账号 > 库中出现过的账号。
    三者取并集，保证「新登录的账号也能看到全部任务」。
    """
    cfg = _read_json(OVERRIDE, {})
    ids = [u for u in cfg.get("uids", []) if u]
    ids += logged_uids()
    ids += list(db_uids or [])
    return sorted(set(ids))


def find_node():
    """定位 node 可执行文件。"""
    hit = shutil.which("node")
    if hit:
        return hit
    cands = glob.glob(os.path.join(WORKBUDDY, "binaries", "node", "versions", "*", "node.exe"))
    return sorted(cands)[-1] if cands else None
