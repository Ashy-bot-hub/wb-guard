#!/usr/bin/env python3
"""端到端验证「删会话时磁盘文件是否删干净」。

做法：造一个测试会话，在 11 个位置各埋一个探测文件，调真删除接口，逐个核对是否被清掉。
11 个位置 = 上游原生删的 6 个 + 本补丁补的 5 个：

  上游原生：projects/<proj>/<id>.jsonl、projects/<proj>/<id>/、workspace/sessions/<id>/、
            tasks/<id>/、file-history/<id>/、artifact-index/<id>.json
  补丁补的：projects/<proj>/<id>.meta.json、projects/<proj>/<id>.file-rollback.ndjson、
            changes-detail/<id>/、changes-index/<id>.json、file-tree-manifests/<id>.json

不带 --yes 只做只读预检，不写任何东西（连探测文件都不建）。
带 --yes 才写：先备份数据库，再建会话与探测文件，删完无论成败都会清理干净。

用法：
    python verify_files.py            # 只读预检
    python verify_files.py --yes      # 实跑
"""
import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import uuid

HOME = os.path.expanduser("~")
ROOT = os.path.join(HOME, ".workbuddy")
DB = os.path.join(ROOT, "workbuddy.db")
APPDATA = os.environ.get("APPDATA", "")
TOKEN_FILE = os.path.join(APPDATA, "WorkDaddy", ".api-token")
PORT_FILE = os.path.join(APPDATA, "WorkDaddy", "ui-port.json")
BACKUP_DIR = os.path.join(APPDATA, "WorkDaddy", "backups")
CWD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_wbgfile")


def conn():
    return sqlite3.connect(DB, timeout=20)


def preflight():
    for path in (DB, TOKEN_FILE, PORT_FILE):
        if not os.path.isfile(path):
            print("  [缺] %s" % path)
            return None
    if not os.path.isdir(os.path.join(ROOT, "projects")):
        print("  [缺] %s\\projects" % ROOT)
        return None
    with open(PORT_FILE, encoding="utf-8") as fh:
        base = "http://127.0.0.1:%s" % json.load(fh)["port"]
    try:
        with urllib.request.urlopen(base + "/api/status", timeout=5) as resp:
            st = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 预检要把任何异常报出来
        print("  [守护未就绪] %s" % exc)
        return None
    print("  库：%s" % DB)
    print("  守护：%s（version %s, pid %s）" % (base, st.get("version"), st.get("pid")))
    return base


def main():
    ap = argparse.ArgumentParser(description="验证删会话时磁盘文件是否删干净")
    ap.add_argument("--yes", action="store_true", help="确认写盘（不传则只做只读预检）")
    args = ap.parse_args()

    print("预检：")
    base = preflight()
    if base is None:
        sys.exit("预检未通过，未做任何改动。")
    if not args.yes:
        print("\n只读预检结束。要真正验证，加 --yes 重跑；届时会先备份数据库。")
        return

    sid = str(uuid.uuid4())
    dst = os.path.join(BACKUP_DIR, "workbuddy-before-fileprobe-%s.db" % time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(BACKUP_DIR, exist_ok=True)
    src = conn()
    out = sqlite3.connect(dst)
    with out:
        src.backup(out)
    out.close()
    src.close()
    print("\n1) 数据库备份 -> %s（%.2f MB）" % (dst, os.path.getsize(dst) / 1048576))
    print("2) 测试会话 id：%s" % sid)

    proj_root = os.path.join(ROOT, "projects")
    proj = os.path.join(proj_root, sorted(os.listdir(proj_root))[0])

    targets = [
        ("projects/<id>.jsonl", os.path.join(proj, sid + ".jsonl")),
        ("projects/<id>.meta.json", os.path.join(proj, sid + ".meta.json")),
        ("projects/<id>.file-rollback.ndjson", os.path.join(proj, sid + ".file-rollback.ndjson")),
        ("projects/<id>/tool-results/", os.path.join(proj, sid, "tool-results", "x.txt")),
        ("workspace/sessions/<id>/", os.path.join(ROOT, "workspace", "sessions", sid, "x.txt")),
        ("tasks/<id>/", os.path.join(ROOT, "tasks", sid, "x.txt")),
        ("file-history/<id>/", os.path.join(ROOT, "file-history", sid, "x.txt")),
        ("artifact-index/<id>.json", os.path.join(ROOT, "artifact-index", sid + ".json")),
        ("changes-detail/<id>/", os.path.join(ROOT, "changes-detail", sid, "x.txt")),
        ("changes-index/<id>.json", os.path.join(ROOT, "changes-index", sid + ".json")),
        ("file-tree-manifests/<id>.json", os.path.join(ROOT, "file-tree-manifests", sid + ".json")),
    ]
    print("   借用 project 目录：%s" % os.path.basename(proj))

    with conn() as c:
        uid = c.execute("SELECT user_id FROM sessions WHERE deleted_at IS NULL AND user_id <> '' "
                        "GROUP BY user_id ORDER BY COUNT(*) DESC LIMIT 1").fetchone()[0]
        now = int(time.time() * 1000)
        c.execute("INSERT INTO sessions (id, cwd, user_id, title, status, created_at, updated_at, "
                  "is_playground, transport, unread) VALUES (?,?,?,?, 'Pending', ?, ?, 0,'local',0)",
                  (sid, CWD, uid, "wbg-fileprobe", now, now))

    created = []
    try:
        for label, path in targets:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("probe\n")
            created.append((label, path))
        print("3) 已在 %d 个位置写入探测文件" % len(created))

        token = open(TOKEN_FILE, encoding="utf-8").read().strip()
        req = urllib.request.Request(
            base + "/api/sessions/delete",
            data=json.dumps({"ids": [sid]}).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", "X-WorkDaddy-Token": token})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            payload = json.loads(exc.read().decode("utf-8"))
        print("4) 删除接口返回：%s" % json.dumps(payload, ensure_ascii=False))
        time.sleep(1.2)

        print("\n5) 逐位置核对：")
        left = []
        for label, path in created:
            still = os.path.exists(path)
            print("   [%s] %s" % ("残留" if still else "已删", label))
            if still:
                left.append(label)
    finally:
        for _label, path in created:
            try:
                if os.path.isfile(path):
                    os.remove(path)
                parent = os.path.dirname(path)
                while parent.startswith(ROOT + os.sep) and parent != ROOT:
                    if os.path.isdir(parent) and not os.listdir(parent):
                        os.rmdir(parent)
                        parent = os.path.dirname(parent)
                    else:
                        break
            except OSError:
                pass
        with conn() as c:
            c.execute("DELETE FROM sessions WHERE id = ?", (sid,))
        print("\n6) 已清理测试会话与探测文件")

    removed = payload.get("filesRemoved")
    ok = not left
    print("\n结论：%s（filesRemoved=%s，残留 %d / %d）"
          % ("通过" if ok else "未通过", removed, len(left), len(created)))
    if left:
        print("  残留位置：" + "、".join(left))
    print("数据库备份保留在：%s" % dst)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
