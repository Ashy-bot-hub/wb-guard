#!/usr/bin/env python3
"""端到端验证「同源副本级联删除」是否真的在本机守护进程上生效。

做法：往 WorkBuddy 的 workbuddy.db 直插 3 行测试会话（带唯一前缀，不碰任何真实行），
只请求删除其中 1 行，观察同指纹兄弟行是否被一并删除、差 1ms 的对照行是否保留，
最后物理清理测试行。因为直插的行在 meta.json 里没有 lineage 映射，上游展开器只会
返回被请求的那 1 行，所以 deleted 大于 1 只能来自本补丁的指纹级联。

安全默认：不带 --yes 只做只读预检（库路径、守护状态、当前行数、备份目录），
不写任何数据。带 --yes 才插行删行，且删前必定先做全库一致性备份。

用法：
    python verify_cascade.py                 # 只读预检
    python verify_cascade.py --yes           # 实跑（会写库，删前自动备份）
    python verify_cascade.py --yes --keep    # 实跑并保留数据库备份（默认也保留）
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


def read_conn():
    return sqlite3.connect(DB, timeout=20)


def read_only_conn():
    return sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)


def daemon_base():
    with open(PORT_FILE, encoding="utf-8") as fh:
        port = json.load(fh)["port"]
    return "http://127.0.0.1:%s" % port


def daemon_status(base):
    try:
        with urllib.request.urlopen(base + "/api/status", timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 预检要报出任何异常
        return {"ok": False, "error": str(exc)}


def preflight():
    problems = []
    for path in (DB, TOKEN_FILE, PORT_FILE):
        if not os.path.isfile(path):
            problems.append("缺文件：%s" % path)
    if problems:
        for p in problems:
            print("  [缺] " + p)
        return None
    base = daemon_base()
    st = daemon_status(base)
    if not st.get("ok"):
        print("  [守护未就绪] %s" % st.get("error"))
        return None
    with read_only_conn() as c:
        total = c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        active = c.execute("SELECT COUNT(*) FROM sessions WHERE deleted_at IS NULL").fetchone()[0]
    print("  库：%s" % DB)
    print("  守护：%s（version %s, pid %s）" % (base, st.get("version"), st.get("pid")))
    print("  当前：sessions %d 行，活跃 %d 行" % (total, active))
    print("  备份目录：%s" % BACKUP_DIR)
    return base, total, active


def backup_db():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    dst = os.path.join(BACKUP_DIR, "workbuddy-before-wbg-verify-%s.db"
                       % time.strftime("%Y%m%d-%H%M%S"))
    src = read_conn()
    out = sqlite3.connect(dst)
    with out:
        src.backup(out)
    out.close()
    src.close()
    return dst, os.path.getsize(dst)


def main():
    ap = argparse.ArgumentParser(description="端到端验证同源副本级联删除")
    ap.add_argument("--yes", action="store_true", help="确认写库（不传则只做只读预检）")
    args = ap.parse_args()

    print("预检：")
    info = preflight()
    if info is None:
        sys.exit("预检未通过，未做任何改动。")
    base, total_before, active_before = info

    if not args.yes:
        print("\n只读预检结束。要真正验证级联，加 --yes 重跑；届时会先做全库备份。")
        return

    print("\n1) 全库备份")
    dst, size = backup_db()
    print("   -> %s（%.2f MB）" % (dst, size / 1048576))

    tag = "__wbg_verify_%d" % int(time.time())
    cwd = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_wbg_fake_cwd")
    os.makedirs(cwd, exist_ok=True)
    created = 4102444800000  # 2100-01-01，确保不与真实数据撞指纹
    ids = ("wbgverify-a-" + tag, "wbgverify-b-" + tag, "wbgverify-c-" + tag)
    a, b, cid = ids

    with read_conn() as c:
        uid = c.execute(
            "SELECT user_id FROM sessions WHERE deleted_at IS NULL AND user_id <> '' "
            "GROUP BY user_id ORDER BY COUNT(*) DESC LIMIT 1").fetchone()[0]
    print("2) 测试行 owner uid：%s" % uid)

    now = int(time.time() * 1000)
    rows = [
        (a, cwd, uid, tag, "Pending", created, now),
        (b, cwd, uid, tag, "Pending", created, now),        # 同指纹，应被级联
        (cid, cwd, uid, tag, "Pending", created + 1, now),  # created_at 差 1ms，不应被级联
    ]
    try:
        with read_conn() as c:
            c.executemany(
                "INSERT INTO sessions (id, cwd, user_id, title, status, created_at, updated_at, "
                "is_playground, transport, unread) VALUES (?,?,?,?,?,?,?,0,'local',0)", rows)
        print("3) 已插入 3 行测试会话")

        token = open(TOKEN_FILE, encoding="utf-8").read().strip()
        req = urllib.request.Request(
            base + "/api/sessions/delete",
            data=json.dumps({"ids": [a]}).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", "X-WorkDaddy-Token": token})
        print("4) 只请求删除 A（1 行）")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                code, payload = resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            code, payload = exc.code, json.loads(exc.read().decode("utf-8"))
        print("   HTTP %s %s" % (code, json.dumps(payload, ensure_ascii=False)))

        time.sleep(1.5)
        with read_conn() as c2:
            left = {i: c2.execute("SELECT 1 FROM sessions WHERE id = ?", (i,)).fetchone()
                    for i in ids}
    finally:
        with read_conn() as c3:
            removed = c3.execute("DELETE FROM sessions WHERE id IN (?,?,?)", ids).rowcount
        print("5) 物理清理测试行：%d 行" % removed)

    print("\n判定：")
    passed = True
    checks = [
        ("A（被请求）已删除", left[a] is None),
        ("B（同指纹）已级联删除", left[b] is None),
        ("C（created_at+1ms）保留未删", left[cid] is not None),
    ]
    for name, ok in checks:
        print("  [%s] %s" % ("通过" if ok else "未通过", name))
        passed = passed and ok
    if isinstance(payload, dict) and payload.get("deleted") == 2:
        print("  [通过] 接口回报 deleted=2（请求 1 行，级联 1 行）")
    else:
        print("  [未通过] 接口回报不是 deleted=2，补丁可能未生效")
        passed = False

    with read_only_conn() as c4:
        total_after = c4.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        active_after = c4.execute("SELECT COUNT(*) FROM sessions WHERE deleted_at IS NULL").fetchone()[0]
    print("\n清理后：sessions %d 行、活跃 %d 行（测试前 %d / %d）"
          % (total_after, active_after, total_before, active_before))
    print("备份保留在：%s" % dst)
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
