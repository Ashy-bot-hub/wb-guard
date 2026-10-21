# -*- coding: utf-8 -*-
"""会话库体检与压缩。

为什么需要
----------
`workbuddy.db` 是所有任务、消息、定时任务的唯一存放处，客户端不会自己
VACUUM，也不会报告完整性。删过大量会话之后（本机曾一次性软删 500 多条），
文件里的空闲页会一直占着空间；如果哪天磁盘写满或断电，也没有任何地方会
告诉你库结构是否还完好。

本脚本做三件事，按风险从低到高：

1. 体检（默认）：完整性、页数、空闲页比例、各表行数与占用。
   全部只读，客户端开着也能跑。
2. 报告可回收空间：空闲页 > 阈值时明确说「VACUUM 能省多少」。
3. `--vacuum`：真正压缩。**客户端运行时拒绝执行** ——
   Windows 上正在被使用的库文件无法被替换，强行压缩只会得到半成品；
   这不是保守，是唯一正确的行为。

用法
----
    python wd_db_maintain.py               # 体检
    python wd_db_maintain.py --deep        # 完整 integrity_check（慢，逐页扫）
    python wd_db_maintain.py --vacuum      # 仅客户端关闭时可用
    python wd_db_maintain.py --json
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wdconfig as C
import wd_shadow as S

STALE_SQL = "SELECT COUNT(*) FROM sessions WHERE deleted_at IS NULL"
TABLES_OF_INTEREST = ("sessions", "messages", "automations", "automation_runs",
                      "workspaces", "projects")


def app_running():
    """客户端是否在运行。复用 wd_shadow 的进程表快照，不再自己跑 tasklist。

    这个判断在体检里已经被 `chk_procs` 做过一次，独立再起一个 tasklist
    实测多花 221 ms（占 `chk_db` 的三分之二），而答案必然相同。
    """
    return S.client_running()


def connect(readonly=True):
    mode = "?mode=ro" if readonly else ""
    return sqlite3.connect("file:%s%s" % (C.DB.replace("\\", "/"), mode), uri=True, timeout=10)


def table_rows(conn):
    out = {}
    for t in TABLES_OF_INTEREST:
        try:
            out[t] = conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
        except sqlite3.Error:
            out[t] = None
    return out


def table_footprint(conn):
    """每张表占多少页（dbstat 需要 SQLITE_ENABLE_DBSTAT_VTAB，没有就返回空）。"""
    try:
        rows = conn.execute(
            "SELECT name, SUM(pgsize) FROM dbstat GROUP BY name ORDER BY 2 DESC LIMIT 8"
        ).fetchall()
        return [(r[0], r[1]) for r in rows]
    except sqlite3.Error:
        return []


def check(deep=False, full=True):
    """库体检。

    full=False 时跳过两项只有人看报告才需要的统计：
      - `dbstat` 每表页占用（全库页扫描，实测 28 ms）；
      - `runs_json` 总长（要读遍所有执行历史 blob，实测 49 ms）。
    `wd_doctor` 只取完整性/空闲页/活跃会话/定时任务这几个字段，用 full=False
    可省掉 77 ms —— 每小时都会跑一次，属纯浪费。
    """
    if not os.path.exists(C.DB):
        return {"ok": False, "error": "数据库不存在：%s" % C.DB}

    size = os.path.getsize(C.DB)
    conn = connect()
    try:
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        free = conn.execute("PRAGMA freelist_count").fetchone()[0]
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        pragma = "integrity_check" if deep else "quick_check"
        integrity = conn.execute("PRAGMA %s" % pragma).fetchone()[0]
        rows = table_rows(conn)
        footprint = table_footprint(conn) if full else []
        idx = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'")]
        active = conn.execute(STALE_SQL).fetchone()[0]
        # 按账号分布：体检要用，这里一并查出，省得体检另开连接再查同一张表。
        sessions_by_user = {
            uid: n for uid, n in conn.execute(
                "SELECT user_id, COUNT(*) FROM sessions "
                "WHERE deleted_at IS NULL GROUP BY user_id")}
        dup = conn.execute(
            "SELECT COUNT(*) FROM (SELECT 1 FROM sessions WHERE deleted_at IS NULL "
            "GROUP BY user_id, cwd, title HAVING COUNT(*) > 1)").fetchone()[0]
        automations = {}
        automation_dup_names = 0
        try:
            # 只统计未软删的：automations 表里 deleted_at 非空的行是用户已删除
            # 的任务，计入会虚报「同一任务多账号重复」（本机实测 32 行里 16 行已删）。
            # 按来源拆开计数：`mirror` 是库里可能残留的同步副本行（本包不产生
            # 这类行），上游 official-automation-sync 会把副本当源再复制一遍，
            # 于是「每账号几条」里混着两层副本，用它做判据必然误报。
            for uid, n, paused, own, mirror in conn.execute(
                    "SELECT owner_user_id, COUNT(*), "
                    "SUM(CASE WHEN status='PAUSED' THEN 1 ELSE 0 END), "
                    "SUM(CASE WHEN coalesce(owner_source,'')<>'mirror' THEN 1 ELSE 0 END), "
                    "SUM(CASE WHEN coalesce(owner_source,'')='mirror' THEN 1 ELSE 0 END) "
                    "FROM automations WHERE deleted_at IS NULL GROUP BY owner_user_id"):
                # own = 自有任务（真实任务，可能 PAUSED 但会显示在列表里）；
                # mirror = 历史镜像副本（PAUSED、不调度，2026-10-20 已清空）。
                automations[uid[:8]] = {"total": n, "paused": paused or 0,
                                        "own": own or 0, "mirror": mirror or 0}
            # 同账号内同名的多条未删任务 —— 这才是「界面上看着有好几份」的真身。
            automation_dup_names = conn.execute(
                "SELECT COUNT(*) FROM (SELECT 1 FROM automations "
                "WHERE deleted_at IS NULL GROUP BY owner_user_id, name "
                "HAVING COUNT(*) > 1)").fetchone()[0]
        except sqlite3.Error:
            pass
        runs = {}
        if full:
            try:
                row = conn.execute(
                    "SELECT COUNT(*), SUM(LENGTH(COALESCE(runs_json,''))) FROM automation_runs"
                ).fetchone()
                runs = {"count": row[0], "runs_json_bytes": row[1] or 0}
            except sqlite3.Error:
                pass
    finally:
        conn.close()

    free_bytes = free * page_size
    ratio = (free / page_count * 100.0) if page_count else 0.0
    return {
        "ok": integrity == "ok",
        "db": C.DB,
        "size": size,
        "page_size": page_size,
        "page_count": page_count,
        "freelist_pages": free,
        "reclaimable_bytes": free_bytes,
        "freelist_ratio": round(ratio, 1),
        "journal_mode": journal,
        "integrity": integrity,
        "tables": rows,
        "footprint": footprint,
        "indexes": idx,
        "active_sessions": active,
        "sessions_by_user": sessions_by_user,
        "duplicate_groups": dup,
        "automations": automations,
        "automation_dup_names": automation_dup_names,
        "automation_runs": runs,
        "app_running": app_running(),
    }


def human(r):
    mb = lambda b: b / 1048576.0
    print("数据库  %s" % r["db"])
    print("大小    %.1f MB（页 %d × %d B）" % (mb(r["size"]), r["page_count"], r["page_size"]))
    print("完整性  %s%s" % (r["integrity"], "" if r["ok"] else "  ← 需要立即从备份恢复"))
    print("日志模式 %s" % r["journal_mode"])
    print("空闲页  %d 页 %.1f MB（占 %.1f%%）%s"
          % (r["freelist_pages"], mb(r["reclaimable_bytes"]), r["freelist_ratio"],
             "→ VACUUM 可回收" if r["freelist_ratio"] >= 10 else ""))
    print("活跃会话 %d；同账号重名组 %d" % (r["active_sessions"], r["duplicate_groups"]))
    print("表行数  " + "  ".join("%s=%s" % (k, v) for k, v in r["tables"].items()))
    if r["footprint"]:
        print("占用前几 " + "  ".join("%s %.1fMB" % (n, mb(s)) for n, s in r["footprint"][:4]))
    print("索引    %s" % (", ".join(r["indexes"]) or "无"))
    if r["automations"]:
        print("定时任务 " + "  ".join(
            "%s:%d条(自有%d/暂停%d/mirror副本%d)" % (
                k, v["total"], v.get("own", 0), v["paused"], v.get("mirror", 0))
            for k, v in r["automations"].items()))
        if r.get("automation_dup_names"):
            print("        同账号重名 %d 组（同一任务在该账号下有多份）"
                  % r["automation_dup_names"])
    if r.get("automation_runs"):
        rv = r["automation_runs"]
        print("执行历史 %d 条，runs_json %.1f MB（客户端自留，不做清理）"
              % (rv["count"], rv["runs_json_bytes"] / 1048576))
    print("客户端  %s" % {True: "运行中", False: "未运行", None: "未知"}[r["app_running"]])


def vacuum():
    running = app_running()
    if running is not False:
        print("客户端正在运行（或状态无法确认），跳过 VACUUM。")
        print("在 Windows 上被占用的库文件无法被替换；强行压缩只会留下半成品。")
        print("请在关闭客户端后重跑：python wd_db_maintain.py --vacuum")
        return 3

    before = os.path.getsize(C.DB)
    # 压缩前留一份整库快照，压缩失败可原样还原
    C.ensure_dirs()
    bak = os.path.join(C.BACKUP, "workbuddy-pre-vacuum-%s.db"
                       % time.strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(C.DB, bak)
    conn = connect(readonly=False)
    try:
        conn.execute("PRAGMA quick_check").fetchone()
        conn.isolation_level = None
        conn.execute("VACUUM")
    finally:
        conn.close()
    after = os.path.getsize(C.DB)
    ok = connect().execute("PRAGMA quick_check").fetchone()[0] == "ok"
    print("VACUUM 完成：%.1f MB → %.1f MB（省 %.1f MB），完整性 %s"
          % (before / 1048576, after / 1048576, (before - after) / 1048576,
             "完好" if ok else "异常"))
    if ok:
        try:
            os.remove(bak)                       # 压缩成功且校验通过，快照无保留价值
        except OSError:
            pass
    else:
        print("完整性异常，快照保留在 %s" % bak)
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="会话库体检与压缩")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--deep", action="store_true", help="完整 integrity_check")
    ap.add_argument("--vacuum", action="store_true", help="压缩（需关闭客户端）")
    a = ap.parse_args(argv)

    if a.vacuum:
        return vacuum()

    r = check(deep=a.deep)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        human(r)
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
