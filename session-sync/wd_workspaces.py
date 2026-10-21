# -*- coding: utf-8 -*-
"""工作空间清单治理：把一次性时间戳目录从侧边栏清出去。

问题
----
WorkDaddy 每次新建会话都在 `C:\\Users\\Administrator\\WorkBuddy\\<时间戳>\\`
建一个工作目录，并往 `workspaces` 表插一行。这张表**没有 user_id 字段**，
所以三个账号看到的是同一份清单 —— 账号 A 丢弃的会话，在账号 B 里仍然
躺着一条工作空间。积累下来就是「切账号后空间又消失/又变乱」的观感。

判据（三条同时满足才处理）
--------------------------
1. 路径是 `<日期-时间戳>` 形式（WorkDaddy 自动生成，不是手工目录）；
2. 没有任何 `deleted_at IS NULL` 的会话以它为 cwd；
3. 超过 ORPHAN_MIN_HOURS 小时没被打开（避免删掉刚建还没来得及用的）。

只删 `workspaces` 表的登记行，不动磁盘上的任何文件 —— 那一行只是侧边栏
入口，重新打开目录会自动重建。删除明细写进 registry，可整批还原。

用法
----
    python wd_workspaces.py                 # 只报告
    python wd_workspaces.py --apply         # 删除无引用登记
    python wd_workspaces.py --undo          # 还原最近一批
"""
import argparse
import datetime
import json
import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wdconfig as C
import wd_fs as F

REGISTRY = os.path.join(C.HERE, "workspace-registry.json")
STAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}$")
ORPHAN_MIN_HOURS = 24


def load_reg():
    reg = C._read_json(REGISTRY, {"batches": []})
    return reg if isinstance(reg, dict) and "batches" in reg else {"batches": []}


def save_reg(reg):
    """登记表原子写。

    这个文件记的是「删了哪些登记」，`--undo` 全靠它。用
    `open(...,"w") + json.dump`，中途失败会留下截断的 JSON，而
    `_read_json` 读到坏文件会回退成空表 —— 症状是「还原时找不到批次」，
    但真正发生的是记录丢了。原子写把这两种情况分开：要么完整，要么不动。
    """
    if not F.atomic_write(REGISTRY, json.dumps(reg, ensure_ascii=False, indent=1)):
        print("警告：登记表写入失败（%s），本次改动将无法 --undo" % REGISTRY)


def dir_files(path):
    if not os.path.isdir(path):
        return 0, 0
    return F.count_files(path)


def connect():
    return sqlite3.connect("file:%s?mode=ro" % C.DB.replace("\\", "/"), uri=True)


def scan():
    """返回 (全部行数, 候选列表[(path, last_opened_at)])。"""
    conn = connect()
    rows = conn.execute("SELECT path, last_opened_at FROM workspaces").fetchall()
    live = {C.norm(c) for (c,) in
            conn.execute("SELECT DISTINCT cwd FROM sessions WHERE deleted_at IS NULL") if c}
    conn.close()

    cut = (datetime.datetime.now() - datetime.timedelta(hours=ORPHAN_MIN_HOURS)).timestamp() * 1000
    out = []
    for p, ts in rows:
        if not STAMP_RE.search(p or ""):
            continue
        if C.norm(p) in live:
            continue
        if (ts or 0) >= cut:
            continue
        out.append((p, ts))
    return len(rows), out


def main():
    ap = argparse.ArgumentParser(description="清理无引用的时间戳工作空间登记")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--undo", action="store_true")
    a = ap.parse_args()

    if not os.path.exists(C.DB):
        print("找不到会话库：%s" % C.DB)
        return 2

    if a.undo:
        reg = load_reg()
        if not reg["batches"]:
            print("没有可还原的批次。")
            return 0
        b = reg["batches"].pop()
        conn = sqlite3.connect(C.DB, timeout=15)
        conn.executemany("INSERT OR IGNORE INTO workspaces (path, last_opened_at) VALUES (?,?)",
                         [(x["path"], x["last_opened_at"]) for x in b["removed"]])
        conn.commit()
        conn.close()
        save_reg(reg)
        print("已还原 %d 条工作空间登记" % len(b["removed"]))
        return 0

    total, cand = scan()
    print("workspaces 共 %d 条；无活跃会话引用且超 %d 小时未打开的时间戳目录 %d 条"
          % (total, ORPHAN_MIN_HOURS, len(cand)))
    if not cand:
        return 0

    n_files = total_mb = 0
    for p, _ts in cand:
        n, sz = dir_files(p)
        n_files += n
        total_mb += sz
    print("  这些目录在磁盘上共 %d 个文件 / %.1f MB（本轮不动这些文件）"
          % (n_files, total_mb / 1048576))
    for p, ts in sorted(cand, key=lambda x: -(x[1] or 0))[:6]:
        print("    %-22s %s" % (os.path.basename(p),
                                datetime.datetime.fromtimestamp((ts or 0) / 1000).strftime("%m-%d %H:%M")))

    if not a.apply:
        print("\n只读模式：加 --apply 删除登记（不动磁盘文件）")
        return 0

    C.ensure_dirs()
    conn = sqlite3.connect(C.DB, timeout=15)
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.executemany("DELETE FROM workspaces WHERE path=?", [(p,) for p, _ in cand])
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0]
    conn.close()

    reg = load_reg()
    reg["batches"].append({
        "at": datetime.datetime.now().isoformat(timespec="seconds"),
        "removed": [{"path": p, "last_opened_at": ts} for p, ts in cand],
    })
    save_reg(reg)

    print("\n已删除 %d 条登记 → 剩余 %d 条；还原：python wd_workspaces.py --undo"
          % (len(cand), after))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
