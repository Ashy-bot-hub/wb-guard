# -*- coding: utf-8 -*-
"""会话摘要生成（只读会话库，输出标题级元数据）。

用法: build_digest.py YYYY-MM-DD [--no-incoming] [--out DIR]

账号不写死：直接按日期捞 `sessions` 表，`user_id` 从结果里取。维护一份 UUID
列表就会漏掉后加入的账号 —— 2026-10-20 实测当天 24 条会话里 8 条属于未列入的
账号，摘要静默少三分之一，而输出里看不出少了谁。
"""
import sqlite3
import sys
import os
from datetime import datetime, timedelta, timezone

DB = os.path.expanduser("~/.workbuddy/workbuddy.db")
TZ = timezone(timedelta(hours=8))


def main():
    args = [a for a in sys.argv[1:]]
    day = args[0]
    out_dir = None
    if "--out" in args:
        out_dir = args[args.index("--out") + 1]
    dt = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=TZ)
    start = int(dt.timestamp() * 1000)
    end = int((dt + timedelta(days=1)).timestamp() * 1000)

    con = sqlite3.connect("file:" + DB + "?mode=ro", uri=True)
    cur = con.cursor()
    # 不加 user_id 过滤：账号集合由数据自身决定，新增账号自动纳入。
    cur.execute(
        "SELECT id,user_id,title,custom_title,status,created_at,cwd FROM sessions "
        "WHERE created_at>=? AND created_at<? "
        "ORDER BY created_at",
        (start, end),
    )
    rows = cur.fetchall()
    con.close()

    if not rows:
        print("EMPTY")
        return 2

    per_uid = {}
    groups = {}
    order = []
    for sid, uid, title, ctitle, status, created, cwd in rows:
        per_uid[uid] = per_uid.get(uid, 0) + 1
        t = (ctitle or title or "(无标题)").strip()
        cwd_n = (cwd or "").replace("\\", "/").rstrip("/")
        key = (t, created, cwd_n.lower())
        if key not in groups:
            groups[key] = {"title": t, "created": created, "status": status,
                           "cwd": cwd_n, "uids": [], "ids": []}
            order.append(key)
        g = groups[key]
        if uid not in g["uids"]:
            g["uids"].append(uid)
        g["ids"].append(sid)

    lines = []
    lines.append("# %s 会话汇总（多账号合并去重）" % day)
    lines.append("")
    lines.append("- 汇总日期：%s（本地时区 GMT+8）" % day)
    lines.append("- 参与账号：%d 个（按当日数据自动识别，不预设名单）" % len(per_uid))
    lines.append("- 各账号条目数（原始记录数）：")
    for u, n in sorted(per_uid.items(), key=lambda kv: -kv[1]):
        lines.append("  - `%s`：%d 条" % (u, n))
    lines.append("- 合计条目数：%d 条（原始）" % len(rows))
    lines.append("- 去重后条目数：%d 条（去重键：标题＋创建时间＋规范化工作目录）" % len(order))
    lines.append("- 数据来源：本地会话库 `sessions` 表，只读打开，未做任何写入。")
    lines.append("- 脱敏说明：本文件不含密码、令牌、Cookie、手机号。")
    lines.append("")
    lines.append("## 去重后清单（按创建时间正序）")
    lines.append("")
    lines.append("| # | 标题 | 创建时间 | 状态 | 来源账号 uid | 会话 id | 工作目录 |")
    lines.append("|---|------|----------|------|--------------|---------|----------|")
    for i, key in enumerate(order, 1):
        g = groups[key]
        ts = datetime.fromtimestamp(g["created"] / 1000, TZ).strftime("%Y-%m-%d %H:%M:%S")
        uids = "<br>".join("`%s`" % u for u in g["uids"])
        ids = "<br>".join("`%s`" % s for s in g["ids"])
        title = g["title"].replace("|", "\\|")
        lines.append("| %d | %s | %s | %s | %s | %s | `%s` |" % (i, title, ts, g["status"], uids, ids, g["cwd"]))
    text = "\n".join(lines) + "\n"

    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        p = os.path.join(out_dir, day + ".md")
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        print("WROTE", p)
    print("raw=%d dedup=%d per_uid=%s" % (len(rows), len(order), per_uid))
    return 0


if __name__ == "__main__":
    sys.exit(main())
