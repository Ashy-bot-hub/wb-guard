# -*- coding: utf-8 -*-
"""目录遍历：一次 scandir 拿全 stat，不逐文件落盘。

为什么单独一个模块
------------------
`os.walk` + `os.path.getsize()` / `os.stat()` 会对**每个文件**再发一次系统调用。
Windows 上 `os.scandir()` 的 DirEntry 已经带出了目录枚举（FindFirstFile）时的
大小与 mtime，`entry.stat()` 不再落盘。

2026-10-19 在 `workspace/sessions/*/modify_backup` 实测（17 个目录、1,675 个
文件、320 MB）：`os.walk` + `os.stat` 45.1 ms，`os.scandir` + `entry.stat`
2.7 ms，**16 倍**，两次统计的字节总数完全一致。`wd_doctor` 的日志目录因此
只走一遍就能同时给出总量、今日、在写三个数。

这里只放遍历本身。判废规则留在 `wdrules.py` —— 遍历有 IO、规则是纯函数，
混在一起就没法给规则写单测。
"""
import os
import shutil
import time


def iter_files(root):
    """逐个产出 (路径, 字节数, mtime)。遍历失败的分支直接跳过，不抛异常。"""
    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            it = os.scandir(cur)
        except OSError:
            continue
        with it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                        continue
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                yield e.path, st.st_size, st.st_mtime


def tree_size(root):
    """返回 (字节数, 最新 mtime)。空目录返回 (0, 0.0)。

    逐文件取 mtime，不用目录 mtime —— 目录 mtime 会被子项增删刷新，
    用它判断「会话是否还在写」会把早就安静下来的目录判成活跃。
    """
    size, newest = 0, 0.0
    for _p, s, m in iter_files(root):
        size += s
        if m > newest:
            newest = m
    return size, newest


def dir_size(root):
    """目录总字节数。"""
    return sum(s for _p, s, _m in iter_files(root))


def count_files(root):
    """返回 (文件数, 字节数)。"""
    n = sz = 0
    for _p, s, _m in iter_files(root):
        n += 1
        sz += s
    return n, sz


CHUNK = 1 << 20


def count_lines(path):
    """行数。与 `open(mode='r')` 逐行迭代的结果一致，但快约 11 倍。

    为什么不能只数 `\\n`：文本模式是 universal newlines，`\\r\\n` 与孤立的
    `\\r` 都算换行；而且**最后一行没有换行符时也会计入**。所以：

    - 按块 `count(b"\\n")`，再补「末尾没有换行符」的那一行；
    - 若 `\\r` 数量与 `\\n` 不等，说明可能存在孤立 `\\r`（两套口径会分叉），
      直接退回文本模式，保证两种口径逐字节一致。

    `\\r` 不逐块无条件 `count`：先看这一块里到底有没有 `\\r`
    （`b"\\r" in chunk` 走 memchr，未命中时比 count 的逐字节累加便宜得多），
    只有出现过才转为计数并从此每块都数。本机转录全是纯 LF，25 份唯一
    文件实测 **146 → 101 ms**，行数完全相同（26,023）。块大小仍取 1 MB：
    试过 4 MB 反而 123 ms —— 单次拷贝变大的开销超过了省下的系统调用。
    """
    nl = cr = size = 0
    last = b""
    has_cr = False
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(CHUNK)
            if not chunk:
                break
            size += len(chunk)
            nl += chunk.count(b"\n")
            if has_cr or b"\r" in chunk:
                cr += chunk.count(b"\r")
                has_cr = True
            last = chunk[-1:]
    if not size:
        return 0
    if cr and cr != nl:
        # 可能有孤立 \r，两套口径会分叉：退回文本模式
        with open(path, encoding="utf-8", errors="ignore") as fh:
            return sum(1 for _ in fh)
    return nl + (0 if last == b"\n" else 1)



# --- 原子写入 ---------------------------------------------------------------
#
# 为什么需要重试
# --------------
# 只把「写临时文件 + `os.replace`」当成原子写是不够的：Windows 上
# `MoveFileEx(MOVEFILE_REPLACE_EXISTING)` 在**目标文件正被别的进程打开**时
# 会直接失败（WinError 5 拒绝访问），因为 Python 与 Node 的普通读打开都不带
# `FILE_SHARE_DELETE`。而本包的产物恰好都是「有人天天在读」的文件：
#
#   - 会话目录里的影子文件：客户端随时可能读；
#   - 注册表与 `meta.json`：脚本自身下次启动会读。
#
# 2026-10-19 实测：一边以读循环持有产物文件，一边连跑 20 次写入，
# **9 次**因 `PermissionError: [WinError 5]` 崩在 `os.replace`，并且残留
# `.tmp`、目标没更新。生产环境读频低得多，碰撞概率小，但一旦撞上就是
# 「守护报错 + 数据仍旧」——正是刚修掉的那类静默失效。
#
# 读端只是短暂占用，几十毫秒后必然松手，所以重试比任何加锁方案都简单可靠：
# 不需要读端配合，也不引入锁文件清理问题。若重试用尽仍失败（例如目标被设为
# 只读、或被别的进程长期独占），清理临时文件并把失败如实返回，由调用方决定
# 是告警还是继续。

ATOMIC_RETRIES = 8          # 8 次 × 递增退避 ≈ 最坏 72 ms
ATOMIC_DELAY = 0.02         # 起始间隔，按 (i+1) 递增


def replace_retry(tmp, dst, retries=ATOMIC_RETRIES, delay=ATOMIC_DELAY):
    """把 tmp 原子替换为 dst，遇到 Windows 占用则重试。

    成功返回 True；重试用尽或有非占用类错误返回 False（不抛异常，调用方据此
    决定是否告警）。失败时不删除 tmp，交给 `atomic_write` 统一清理。
    """
    for i in range(retries):
        try:
            os.replace(tmp, dst)
            return True
        except PermissionError:
            if i == retries - 1:
                return False
            time.sleep(delay * (i + 1))
        except OSError:
            return False
    return False


def _tmp_name(path):
    """临时文件名带 PID。

    固定用 `path + ".tmp"` 时，两个进程同时写同一个目标会撞在同一个临时文件上：
    A 写一半、B 打开同一个文件从零写、A 再 `os.replace` —— 替换进去的可能是
    两个人交错的内容。真实场景存在：守护巡检与手动脚本同时写同一份注册表。
    带 PID 后各写各的，最后一次 replace 胜出，
    内容至少是某一次完整的结果。
    """
    return "%s.%d.tmp" % (path, os.getpid())


def atomic_write(path, text, retries=ATOMIC_RETRIES, delay=ATOMIC_DELAY):
    """原子写文本文件。成功返回 True，失败返回 False 且不残留 tmp。

    不做 `fsync`：这里要防的是「读端读到半截」，不是掉电丢页；每次写都刷盘
    会让守护的周期任务多出可感知的延迟。
    """
    tmp = _tmp_name(path)
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    except OSError:
        _drop(tmp)
        return False
    if replace_retry(tmp, path, retries, delay):
        return True
    _drop(tmp)
    return False


def copy_atomic(src, dst, retries=ATOMIC_RETRIES, delay=ATOMIC_DELAY):
    """复制到同目录临时文件，再原子替换目标。成功返回 True。

    为什么不用 `shutil.copy2(src, dst)`：那是「打开目标 → 逐块写」。中途失败
    （磁盘满、进程被杀、目标被占用）会留下一个**被截断的目标文件**，而调用方
    往往已经先把旧目标删掉了 —— `wd_unify --undo` 正是这个顺序，一旦 copy2
    失败，转录就没了（备份还在，但用户不知道要手工恢复）。

    临时文件放在目标同目录，保证 `os.replace` 是同卷重命名（跨卷会退化成
    复制，不再是原子的）。
    """
    tmp = _tmp_name(dst)
    try:
        d = os.path.dirname(dst)
        if d:
            os.makedirs(d, exist_ok=True)
        shutil.copy2(src, tmp)
    except OSError:
        _drop(tmp)
        return False
    if replace_retry(tmp, dst, retries, delay):
        return True
    _drop(tmp)
    return False


def _drop(path):
    try:
        os.remove(path)
    except OSError:
        pass
