# -*- coding: utf-8 -*-
"""账号切换守护：切换后做血缘统一（记忆共享）+ 周期自愈。

为什么需要
----------
WorkDaddy 的跨账号同步本质是「复制一条记录」：复制之后在哪边继续对话，另一边
不会跟着更新，同一任务变成两份互不相干的记忆。本进程在切号后把这些转录重新
硬链接到同一个 canonical 文件，并退役同账号内重复的那份。

本进程也不做「跨账号补齐」：
  - 上游 `autoCopy` 已经把缺的会话复制过去了，实测 12 次切号里有 8 次都是
    「无需补齐」，剩下几次补出来的又是一份新副本，反过来喂给 wd_unify 去退役；
  - 补齐是「制造副本」，统一是「回收副本」，两个动作在同一个循环里互相抵消，
    净效果只是多两次写库和一批备份。

本进程按 `.info` 修改时间判断当前账号，切换后：统一记忆（硬链接 + 退役重名）。

触发与时序
----------
每 5 秒（POLL）读一次账号标记；检测到切换后等 20 秒（SETTLE，让客户端自身落库）
再执行三步。从真实切换起最坏 25 秒、平均约 22.5 秒完成刷新。
每轮轮询前先做一次只读的重复巡检，发现同账号重名立即统一，不依赖切换事件。
另每 6 小时无条件兜底一次。

日志写在 logs/guard.log，PID 写在 guard.pid。

启动 / 停止
-----------
安装：双击 install.bat（注册登录触发的计划任务）
停止：Stop-ScheduledTask -TaskName WorkDaddy-SyncGuard
"""
import datetime
import functools
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wdconfig as C

HERE = C.HERE
UNIFY = os.path.join(HERE, "wd_unify.py")
JANITOR = os.path.join(HERE, "wd_janitor.py")
DOCTOR = os.path.join(HERE, "wd_doctor.py")
SOFTDEL = os.path.join(HERE, "wd_softdel.py")
LOG = os.path.join(C.LOGS, C.LOG_GUARD)
PID_FILE = os.path.join(HERE, C.PID_GUARD[0])

NODE = C.find_node()
PY = sys.executable

POLL = 5
SETTLE = 20
FALLBACK = 6 * 3600
CMD_TIMEOUT = 300
CDP_TIMEOUT = 40

# 本守护由 pythonw.exe 启动（GUI 子系统，没有控制台）。这种父进程一旦
# 启动控制台程序（schtasks.exe、node.exe），Windows 会给子进程新建一个
# 控制台窗口 —— 在桌面上就是一闪一闪的黑框。用户看到的「node 老是跳起来」
# 有一部分就来自这里：每次巡检、每次刷新前端都会闪一下。
# CREATE_NO_WINDOW 让子进程跑在一个不可见控制台里，视觉上完全消失。
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_run = functools.partial(subprocess.run, creationflags=CREATE_NO_WINDOW)


def log(msg):
    try:
        C.ensure_dirs()
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass


_MUTEX = None


def should_log(fails):
    """连续失败时是否写日志：前 3 次与每 20 次一条。

    持续性故障（库被锁、磁盘满）会每 65 秒触发一次异常，逐条记会把
    guard.log 写爆；完全不记又会让人以为守护在正常轮转。这个取舍值得
    单测钉住，因为它一旦写错，症状要几天后才以「日志几百 MB」的形式出现。
    """
    return fails <= 3 or fails % 20 == 0


def acquire_single_instance(name="Local\\WorkDaddy-SyncGuard"):
    """同机只允许一个守护实例，拿不到互斥体就退出。

    为什么不能只靠计划任务：`MultipleInstances=IgnoreNew` 只约束「任务触发」
    这一条路径。命令行或双击再起一个时，任务调度器根本不知道，两个实例会
    同时跑 janitor/unify、同时改写同一份 guard.pid 与注册表 ——
    并发写入的风险不比不跑更小。用命名互斥体把这条路堵掉。

    为什么名字可传：单测要验证「第二次被拒」，而生产实例正在占用默认名 ——
    不隔离的话，测试要么失败、要么必须先停掉真守护。让测试用自己的名字，
    测的是同一段逻辑，不碰生产。

    退出码必须是 0：非 0 会命中任务的「失败重试 3 次」，让第二个实例
    反复被拉起，恰好制造出它要防的那种混乱。
    """
    global _MUTEX
    ok, _MUTEX = C.acquire_mutex(name)     # 实现在 wdconfig，写库互斥共用同一段
    return ok


def run_script(path, reason, label, need_lines=0):
    if not os.path.exists(path):
        log("%s → 跳过：找不到 %s" % (reason, os.path.basename(path)))
        return
    try:
        p = _run([PY, path, "--apply"], capture_output=True, text=True,
                           timeout=CMD_TIMEOUT, cwd=HERE)
        out = [l for l in (p.stdout or "").strip().splitlines() if l]
        tail = out[-need_lines:] if need_lines else out[-1:]
        detail = " / ".join(x.strip() for x in tail) if tail else ""
        if need_lines > 1:
            # 只保留关键摘要行，避免日志被明细淹没
            detail = " / ".join(x.strip() for x in out
                                if x.startswith(("跨账号任务组", "可合并为同一份转录",
                                                "内容已分叉")))
        log("%s → %s rc=%s | %s" % (reason, label, p.returncode, detail[:200]))
    except Exception as e:
        log("%s → %s 异常：%r" % (reason, label, e))


# 本脚本不做前端刷新：WorkDaddy 1.2.17 的 daemon 在账号切换时自行经 CDP 重载
# 窗口（日志 `[switch] 已通过 CDP 刷新 WorkBuddy 窗口`，切号后约 0.5 秒），并在
# 同步类操作后调用官方 conversations.ensureList() 刷列表。再 reload 一次，与上游
# 这一次撞在一起，实测表现为切号后连续白屏两次。本脚本只做数据层，界面刷新交给
# 客户端。确需手动刷新时用：
#   node cdp.mjs eval "location.reload()"

def pid_alive(pid):
    """判断进程是否存活（直接问内核，不枚举进程表）。

    原实现跑 `tasklist /FO CSV`，每次枚举全机 200+ 进程、约 150 ms；而且中文
    Windows 的输出是 GBK，不能按 utf-8 解码 —— 当年为此写了「只做 ASCII 匹配」
    的绕法。看门狗每分钟查一次，一天 1440 次枚举，纯属浪费。

    OpenProcess + GetExitCodeProcess 一次约 1 ms，不涉及编码，语义也更直接：
    句柄能打开说明进程还在；STILL_ACTIVE(259) 说明没退出。

    「查不清」时的取舍沿用原逻辑 —— 认为还活着。反过来会把活着的实例当成死了，
    每分钟重复拉起，而每次拉起都让上一个实例收到 CTRL_CLOSE_EVENT 退出，
    2026-10-19 实测过这种自杀循环。
    """
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        h = k32.OpenProcess(0x1000, False, int(pid))   # QUERY_LIMITED_INFORMATION
        if not h:
            # 5 = 拒绝访问：进程在，只是问不了；87 = 参数错：这个 PID 不存在
            return ctypes.get_last_error() == 5
        code = wintypes.DWORD()
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return bool(ok) and code.value == 259          # STILL_ACTIVE
    except Exception:                                  # noqa: BLE001
        return True


def clean_disk():
    """每 30 分钟清一次可再生日志与浏览器缓存。

    客户端日志没有任何轮转机制：sandbox 一天能写 300 MB 以上，
    单日会话日志 270 MB。手动清理一次 1.5 小时就涨回来，
    所以把清理做成守护的固定动作，而不是运维动作。
    """
    now = time.time()
    if now - clean_disk.last < 1800:
        return
    clean_disk.last = now
    if not os.path.exists(JANITOR):
        return
    try:
        p = _run([PY, JANITOR, "--apply"], capture_output=True, text=True,
                           timeout=CMD_TIMEOUT, cwd=HERE)
        line = ""
        for l in (p.stdout or "").splitlines():
            if l.startswith("已回收"):
                line = l.strip()
        if line:
            log("磁盘自愈 → %s" % line)
    except Exception as e:
        log("磁盘自愈异常：%r" % e)


clean_disk.last = 0.0


PRUNE = os.path.join(HERE, "wd_prune_backups.py")
BAK_TRIGGER_MB = 280        # 备份目录超过这个体积才回收
BAK_EVERY = 6 * 3600


def prune_backups():
    """备份目录涨过头就回收过期快照。

    unify 每批备份一份转录加一份库，单批约 30 MB；客户端自己也会往
    ~/.workbuddy/backups 里放安装前快照。实测堆到 690 MB（体检故障线
    600 MB）时没有任何自动回收环节 —— 只有人想起来才手动跑清理脚本。
    清理不该靠记性。

    阈值 280 MB 而不是「每次都清」：刚产生的两批备份是切号后唯一的回滚依据，
    频繁删除等于把保护网撤掉。超阈值时才按 `wdconfig.BACKUP_KEEP` /
    `BACKUP_MIN_GLOBAL` 回收 —— 与 `wd_unify` 收尾、手动执行脚本三处同一口径，
    不再各写一套默认值。
    """
    now = time.time()
    if now - prune_backups.last < BAK_EVERY:
        return
    prune_backups.last = now
    if not os.path.exists(PRUNE):
        return
    try:
        total = 0
        for dp, _dn, fn in os.walk(C.BACKUP):
            for f in fn:
                try:
                    total += os.path.getsize(os.path.join(dp, f))
                except OSError:
                    pass
        total /= 1048576.0
    except OSError:
        return
    if total < BAK_TRIGGER_MB:
        return
    try:
        p = _run([PY, PRUNE, "--apply"], capture_output=True, text=True,
                 timeout=CMD_TIMEOUT, cwd=HERE)
        line = ""
        for l in (p.stdout or "").splitlines():
            if l.startswith("已回收"):
                line = l.strip()
        if line:
            log("备份回收 → %s（此前 %.0f MB，触发线 %d MB）"
                % (line, total, BAK_TRIGGER_MB))
    except Exception as e:                       # noqa: BLE001
        log("备份回收异常：%r" % e)


prune_backups.last = 0.0


def hourly_doctor():
    """每小时体检一次，把结论写进守护日志。

    体检靠人手动跑四个脚本才看得出状态；做成每小时一条日志后，
    「昨天还好好的，今天不行了」这类退化可以直接从日志时间线定位。
    只写日志、不自动修复：体检报告是判据，修复动作各有各的前置条件。
    """
    now = time.time()
    if now - hourly_doctor.last < 3600:
        return
    hourly_doctor.last = now
    if not os.path.exists(DOCTOR):
        return
    try:
        p = _run([PY, DOCTOR, "--json"], capture_output=True, text=True,
                           timeout=CMD_TIMEOUT, cwd=HERE)
        data = json.loads(p.stdout or "{}")
        bad = [c["name"] for c in data.get("checks", []) if c.get("status") != "达标"]
        log("体检 → %s%s" % (data.get("status", "未知"),
                            ("（不达标：%s）" % "、".join(bad)) if bad else ""))
    except Exception as e:                       # noqa: BLE001
        log("体检异常：%r" % e)


hourly_doctor.last = 0.0


def daily_housekeeping():
    """每天一次：清无引用的工作空间登记 + 清零内容空壳会话 + 清孤儿用量行。

    前两者都只动数据库登记，不动磁盘文件，且都能用对应脚本的 --undo 还原。
    空壳阈值取 48 小时，避免把当天新建还没开始用的任务当成空壳。

    注意：空壳清理用的是**软删**（只置 deleted_at），所以它自己就在制造软删积压。
    软删行不会被任何界面再读到，但行、用量、11 处派生文件都会一直留着 ——
    所以每天顺带做一次「孤儿用量行」回收；过期软删会话的硬删仍然要人确认。
    """
    now = time.time()
    if now - daily_housekeeping.last < 24 * 3600:
        return
    daily_housekeeping.last = now

    ws = os.path.join(HERE, "wd_workspaces.py")
    for args, label in (( [PY, ws, "--apply"], "工作空间登记"),):
        if not os.path.exists(args[1]):
            continue
        try:
            p = _run(args, capture_output=True, text=True, timeout=CMD_TIMEOUT, cwd=HERE)
            line = ""
            for l in (p.stdout or "").splitlines():
                if l.startswith("已删除"):
                    line = l.strip()
            log("日常维护（%s）→ %s" % (label, line or "无需处理"))
        except Exception as e:
            log("日常维护（%s）异常：%r" % (label, e))

    try:
        p = _run([PY, UNIFY, "--prune-empty", "--hours", "48", "--apply"],
                           capture_output=True, text=True, timeout=CMD_TIMEOUT, cwd=HERE)
        line = ""
        for l in (p.stdout or "").splitlines():
            if l.startswith("已软删"):
                line = l.strip()
        log("日常维护（空壳会话）→ %s" % (line or "无需处理"))
    except Exception as e:
        log("日常维护（空壳会话）异常：%r" % e)

    # 软删积压的第一层收敛：孤儿 usage 行（指向已不存在会话的用量行）。
    # 这类行没有任何读取路径能命中（各端都是 WHERE session_id = ?），删掉不影响
    # 任何界面，所以可以无人值守做。第二层（硬删过期软删会话）不可逆，
    # 只靠体检与日志提醒，等人显式跑 wd_softdel.py --prune。
    if os.path.exists(SOFTDEL):
        try:
            p = _run([PY, SOFTDEL, "--purge-orphan-usage", "--yes"],
                     capture_output=True, text=True, timeout=CMD_TIMEOUT, cwd=HERE)
            line = ""
            for l in (p.stdout or "").splitlines():
                if l.startswith("已清理孤儿 usage"):
                    line = l.strip()
            log("日常维护（软删积压）→ %s" % (line or "无孤儿 usage"))
        except Exception as e:
            log("日常维护（软删积压）异常：%r" % e)


daily_housekeeping.last = 0.0


DUPE_EVERY = 15          # 秒；重复巡检的间隔


def count_dupes():
    """只读计数：同账号内「同目录 + 同标题」的多条活跃会话。

    不需要每 5 秒问一次：重复只可能由「新建会话」产生，而这类动作在秒级
    不会接连发生；15 秒足够及时，又省掉 2/3 的数据库连接。把「轮询账号
    切换」的心跳频率（5s）套给「扫库」是两回事，两者合理的频率本来就不同。
    """
    now = time.time()
    if now - count_dupes.last < DUPE_EVERY:
        return 0
    count_dupes.last = now
    import sqlite3
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % C.DB.replace("\\", "/"), uri=True)
        rows = conn.execute(
            "SELECT user_id, cwd, title FROM sessions WHERE deleted_at IS NULL").fetchall()
        conn.close()
    except Exception:
        return 0
    seen, dup = set(), 0
    for uid, cwd, title in rows:
        key = (uid, C.norm(cwd), title or "")
        if key in seen:
            dup += 1
        else:
            seen.add(key)
    return dup


count_dupes.last = 0.0


def main():
    if not acquire_single_instance():
        log("已有守护实例在运行（互斥体被占用）→ 本实例退出 PID=%s" % os.getpid())
        return
    try:
        C.ensure_dirs()
        with open(PID_FILE, "w") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass

    log("守护启动 PID=%s 轮询%ds 等待%ds 账号=%s node=%s"
        % (os.getpid(), POLL, SETTLE, C.active_uid(), NODE or "未找到"))

    last_uid = C.active_uid()
    last_run = time.time()
    fails = 0

    while True:
        # 循环体整体兜底。任何一步抛出未捕获异常，都会让整个进程退出：
        # 任务侧的「失败重试 3 次」只能救三次，之后要等下次登录才恢复，
        # 而这中间账号切换、日志清理、体检、备份回收全部停摆，且没人知道。
        # 守护的价值是「一直在」，单次异常不该等于永久停摆。
        try:
            time.sleep(POLL)
            clean_disk()
            prune_backups()
            daily_housekeeping()
            hourly_doctor()
            dup = count_dupes()
            if dup:
                log("巡检发现同账号重复 %d 条 → 血缘统一" % dup)
                run_script(UNIFY, "巡检去重", "血缘统一")
                log("去重后 → 界面刷新交由 WorkDaddy（本脚本不再 reload）")
                last_run = time.time()
                fails = 0
                continue

            cur = C.active_uid()
            if cur and cur != last_uid:
                log("检测到账号切换：%s → %s" % (last_uid, cur))
                last_uid = cur
                time.sleep(SETTLE)
                # 不造副本（见模块头）：缺的会话上游 autoCopy 会补，
                # 我们只负责把复制出来的多份合并成同一份记忆。
                run_script(UNIFY, "切换后", "血缘统一", need_lines=2)
                log("切换后 → 界面刷新交由 WorkDaddy（本脚本不再 reload）")
                last_run = time.time()
            elif time.time() - last_run >= FALLBACK:
                run_script(UNIFY, "定时兜底", "血缘统一", need_lines=2)
                last_run = time.time()
            fails = 0
        except Exception as e:                    # noqa: BLE001
            # 日志限流：持续性故障（如库被长期锁住）每 65 秒一条会把日志写爆，
            # 只记前 3 次与每 20 次一条，足以看出「一直是这个错」。
            fails += 1
            if should_log(fails):
                log("巡检异常（连续第 %d 次，本轮跳过）：%r" % (fails, e))
            time.sleep(60)


if __name__ == "__main__":
    main()
