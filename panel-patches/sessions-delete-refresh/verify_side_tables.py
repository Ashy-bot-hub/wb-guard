#!/usr/bin/env python3
"""端到端验证「附属登记回收」：session_usage 连带清理 + workspaces 窄判据回收。

做法：插 6 个测试会话（其中两个同指纹）、6 条 session_usage、4 条 workspaces，
只请求删除其中 4 个会话，然后核对：
  - 被请求的 4 个 + 同指纹副本 1 个，会话行都没了
  - session_usage 只剩那个没被请求的会话
  - workspaces 四个案例分别对应补丁的三条判据：
      W1 时间戳目录 + 会话被删 + 旧时间       -> 应删
      W2 时间戳目录 + 会话被删 + 刚打开过     -> 应保留（24 小时保护）
      W3 手工目录名 + 会话被删                -> 应保留（不是自动生成目录）
      W4 时间戳目录 + 仍有活跃会话在用        -> 应保留
最后物理清理测试数据。

安全默认：不带 --yes 只做只读预检；带 --yes 才写库，且删前必定先做全库一致性备份。

用法：
    python verify_side_tables.py            # 只读预检
    python verify_side_tables.py --yes      # 实跑（会写库，删前自动备份）
"""
import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request

APPDATA = os.environ.get("APPDATA", "")
DB = os.path.join(os.path.expanduser("~"), ".workbuddy", "workbuddy.db")
WD_DIR = os.path.join(APPDATA, "WorkDaddy")
TOKEN_FILE = os.path.join(WD_DIR, ".api-token")
PORT_FILE = os.path.join(WD_DIR, "ui-port.json")
BACKUP_DIR = os.path.join(WD_DIR, "backups")
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_wbgtest")
CREATED = 4102444800000            # 2100-01-01，避免与真实数据撞指纹
OLD = 1600000000000                # 2020 年，远超 24 小时


def conn():
    return sqlite3.connect(DB, timeout=20)


def read_only():
    return sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)


def norm(p):
    return str(p or "").replace("\\", "/").lower()


def preflight():
    for path in (DB, TOKEN_FILE, PORT_FILE):
        if not os.path.isfile(path):
            print("  [缺] %s" % path)
            return None
    with open(PORT_FILE, encoding="utf-8") as fh:
        base = "http://127.0.0.1:%s" % json.load(fh)["port"]
    try:
        with urllib.request.urlopen(base + "/api/status", timeout=5) as resp:
            st = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 预检要把任何异常报出来
        print("  [守护未就绪] %s" % exc)
        return None
    with read_only() as c:
        total = c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        usage = c.execute("SELECT COUNT(*) FROM session_usage").fetchone()[0]
        ws = c.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0]
    print("  库：%s" % DB)
    print("  守护：%s（version %s, pid %s）" % (base, st.get("version"), st.get("pid")))
    print("  当前：sessions %d / session_usage %d / workspaces %d" % (total, usage, ws))
    return base, (total, usage, ws)


def main():
    ap = argparse.ArgumentParser(description="端到端验证附属登记回收")
    ap.add_argument("--yes", action="store_true", help="确认写库（不传则只做只读预检）")
    args = ap.parse_args()

    print("预检：")
    info = preflight()
    if info is None:
        sys.exit("预检未通过，未做任何改动。")
    base, before_counts = info
    if not args.yes:
        print("\n只读预检结束。要真正验证，加 --yes 重跑；届时会先做全库备份。")
        return

    dst = os.path.join(BACKUP_DIR, "workbuddy-before-side-tables-%s.db" % time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(BACKUP_DIR, exist_ok=True)
    src = conn()
    out = sqlite3.connect(dst)
    with out:
        src.backup(out)
    out.close()
    src.close()
    print("\n1) 全库备份 -> %s（%.2f MB）" % (dst, os.path.getsize(dst) / 1048576))

    w1 = os.path.join(ROOT, "2026-01-01-00-00-01")
    w2 = os.path.join(ROOT, "2026-01-02-00-00-02")
    w3 = os.path.join(ROOT, "wbg_manual_dir")
    w4 = os.path.join(ROOT, "2026-01-04-00-00-04")

    tag = "wbgean-%d" % int(time.time())
    s1, s1b, s2, s3, s4, s5 = (tag + x for x in ("-s1", "-s1b", "-s2", "-s3", "-s4", "-s5"))
    all_ids = (s1, s1b, s2, s3, s4, s5)
    now = int(time.time() * 1000)

    with conn() as c:
        uid = c.execute("SELECT user_id FROM sessions WHERE deleted_at IS NULL AND user_id <> '' "
                        "GROUP BY user_id ORDER BY COUNT(*) DESC LIMIT 1").fetchone()[0]
    print("2) owner uid：%s" % uid)

    rows = [
        (s1, w1, "wbg-same-tag", CREATED),
        (s1b, w1, "wbg-same-tag", CREATED),
        (s2, w2, "wbg-w2", CREATED + 10),
        (s3, w3, "wbg-w3", CREATED + 20),
        (s4, w4, "wbg-w4", CREATED + 30),
        (s5, w4, "wbg-w5", CREATED + 40),
    ]
    try:
        with conn() as c:
            c.executemany(
                "INSERT INTO sessions (id, cwd, user_id, title, status, created_at, updated_at, "
                "is_playground, transport, unread) VALUES (?,?,?,?, 'Pending', ?, ?, 0,'local',0)",
                [(i, cwd, uid, title, created, now) for i, cwd, title, created in rows])
            c.executemany(
                "INSERT OR REPLACE INTO session_usage (session_id, used, size, updated_at, credit_json) "
                "VALUES (?, 100, 300000, ?, '{}')", [(i, now) for i in all_ids])
            c.executemany("INSERT OR REPLACE INTO workspaces (path, last_opened_at) VALUES (?,?)",
                          [(w1, OLD), (w2, now), (w3, OLD), (w4, OLD)])
        print("3) 已插入 6 个会话 + 6 条 usage + 4 条 workspaces")

        token = open(TOKEN_FILE, encoding="utf-8").read().strip()
        req = urllib.request.Request(
            base + "/api/sessions/delete",
            data=json.dumps({"ids": [s1, s2, s3, s4]}).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", "X-WorkDaddy-Token": token})
        print("4) 只请求删除 4 个会话（s1 另有同指纹副本 s1b）")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            payload = json.loads(exc.read().decode("utf-8"))
        print("   ->", json.dumps(payload, ensure_ascii=False))
        time.sleep(1.5)

        with conn() as c:
            alive = {i for (i,) in c.execute("SELECT id FROM sessions WHERE id LIKE ?", (tag + "%",))}
            usage = {i for (i,) in c.execute("SELECT session_id FROM session_usage WHERE session_id LIKE ?", (tag + "%",))}
            ws = {p for (p,) in c.execute("SELECT path FROM workspaces WHERE path LIKE ?", (ROOT + "%",))}
    finally:
        with conn() as c:
            n1 = c.execute("DELETE FROM sessions WHERE id IN (%s)" % ",".join("?" * len(all_ids)), all_ids).rowcount
            n2 = c.execute("DELETE FROM session_usage WHERE session_id IN (%s)" % ",".join("?" * len(all_ids)), all_ids).rowcount
            n3 = c.execute("DELETE FROM workspaces WHERE path IN (?,?,?,?)", (w1, w2, w3, w4)).rowcount
        print("5) 清理测试数据：sessions %d / usage %d / workspaces %d" % (n1, n2, n3))

    got = {norm(p) for p in ws}
    checks = [
        ("被请求的 4 个会话都已删除", alive == {s5}),
        ("未被请求的 s5 仍在", s5 in alive),
        ("session_usage 只剩 s5", usage == {s5}),
        ("cascaded=1（同指纹副本被级联）", payload.get("cascaded") == 1),
        ("deleted=5", payload.get("deleted") == 5),
        ("W1 时间戳目录 + 旧时间 -> 已回收", norm(w1) not in got),
        ("W2 时间戳目录 + 刚打开 -> 保留", norm(w2) in got),
        ("W3 手工目录名 -> 保留", norm(w3) in got),
        ("W4 仍有活跃会话引用 -> 保留", norm(w4) in got),
    ]
    print("\n6) 判定：")
    for name, ok in checks:
        print("   [%s] %s" % ("通过" if ok else "未通过", name))

    with read_only() as c:
        after = (c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
                 c.execute("SELECT COUNT(*) FROM session_usage").fetchone()[0],
                 c.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0])
    print("\n清理后：sessions %d / session_usage %d / workspaces %d（测试前 %s）"
          % (after + (before_counts,)))
    print("备份保留在：%s" % dst)

    failed = [n for n, ok in checks if not ok]
    print("结论：%d/%d 通过" % (len(checks) - len(failed), len(checks)))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
