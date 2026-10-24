# -*- coding: utf-8 -*-
"""孤儿派生文件回收：sessions 表里已无对应行，磁盘上却还留着的会话附属数据。

为什么会有
----------
删会话的实现分两代。1.2.18 之前（以及面板补丁 r5 之前）只删
`projects/<proj>/<id>.jsonl` 和会话行，派生数据全部留在原地：
`changes-detail/<id>/`、`changes-index/<id>.json`、`file-tree-manifests/<id>.json`、
`<id>.file-rollback.ndjson`、`artifact-index/<id>.json`、`workspace/sessions/<id>/` ……
补丁 r6 把 11 个位置补齐后，**新删的会话不再产生孤儿**（verify_files.py 实测残留 0），
但历史硬删留下的仍在盘上。本机实测 176 个 / 87.9 MB，其中一个会话的
`changes-detail` 单独占 59 MB。

判定
----
节点名必须是 `<uuid>` 或 `<uuid><已知后缀>`，且该 uuid 不在 `sessions` 表。
**软删行不算孤儿** —— 它只是打了 `deleted_at` 标记，文件还要用，归
`wd_softdel.py` 的 `--prune` 管。两边判据互补，不重叠。

三条护栏
--------
1. 只认白名单目录（数据根七个位置 + `projects/<编码cwd>/` 的直接子项），
   不递归乱扫；`projects/<编码cwd>/` 目录本身是工作目录编码，不是会话 id，不碰。
2. 默认不碰 `.jsonl` 转录 —— 那是核心资产。孤儿转录只报告，加
   `--include-transcripts` 才一起处理。
3. 最近 `FRESH_MIN` 分钟内还改动过的不碰：客户端先落文件后写库，那段时间里
   文件暂时「无主」，当成垃圾会删掉正在建的会话。

不直删
------
`--apply` 是把节点摘到 `archive/orphans-<stamp>/` 并写 manifest，不是删除。
确认无误后 `--purge-archive --older-than 7` 才真删归档区。

用法
----
    python wd_orphans.py                            # 只读列出
    python wd_orphans.py --json                     # 机器可读
    python wd_orphans.py --apply                    # 摘到归档区
    python wd_orphans.py --prune-empty-cwd --apply   # 顺带收掉空的工作目录
    python wd_orphans.py --list-archive             # 看归档批次
    python wd_orphans.py --restore 20261024-180000  # 原路放回
    python wd_orphans.py --purge-archive --older-than 7
"""
import argparse
import datetime
import json
import os
import re
import shutil
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wdconfig as C
import wd_fs as F

UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"

# (目录, 允许的后缀列表)；后缀按长度降序匹配，"" 表示目录本身
ROOT_CHILDREN = (
    ("changes-detail", ("",)),
    ("changes-index", (".json",)),
    ("file-tree-manifests", (".json", ".meta.json")),
    ("artifact-index", (".json",)),
    ("workspace/sessions", ("",)),
    ("tasks", ("",)),
    ("file-history", ("",)),
)
PROJ_SUFFIXES = (".file-rollback.ndjson", ".meta.json", ".jsonl", "")

FRESH_MIN = 30            # 这么多分钟内动过的文件不判孤儿
EMPTY_CWD_HOURS = 24      # 空工作目录要闲置这么久才收
MANIFEST = "manifest.ndjson"


def _pattern(suffixes):
    alts = "|".join(re.escape(s) for s in sorted(suffixes, key=len, reverse=True) if s)
    return re.compile(r"^(%s)(%s)?$" % (UUID, alts)) if alts else re.compile(r"^(%s)$" % UUID)


def _split(m):
    """(会话 id, 后缀)。无后缀分支的正则只有一个 group。"""
    return m.group(1), (m.group(2) if m.re.groups > 1 else "")


def _node(path):
    """(字节数, 最后活动时刻)。

    目录的「最后活动」取内部最新文件的 mtime，不用目录自身的 —— 目录 mtime
    只在增删直接子项时刷新，`changes-detail/<id>/0001.json` 反复改写时它可能
    一直停在很早的时刻，也可能因为刚建目录显得很新。判「是不是正在写」要看
    内容。`F.tree_size` 正好一次遍历给两个值。
    """
    if os.path.isdir(path) and not os.path.islink(path):
        return F.tree_size(path)
    try:
        return os.path.getsize(path), os.path.getmtime(path)
    except OSError:
        return 0, 0.0


def connect():
    return sqlite3.connect("file:%s?mode=ro" % C.DB.replace("\\", "/"), uri=True)


def known_ids(conn=None):
    """所有在册会话（含软删）。求补集时用它衡量「谁已经不在了」。"""
    own = conn is None
    conn = conn or connect()
    try:
        return {r[0] for r in conn.execute("SELECT id FROM sessions")}
    finally:
        if own:
            conn.close()


def scan(known, wb=None, proj=None, include_transcripts=False,
         fresh_min=FRESH_MIN, now=None):
    """列出孤儿节点。

    known 是在册会话 id 集合（含软删行），由调用方查库传入 —— 便于单测。
    返回 [{kind, sid, path, size, mtime}]，按 kind 分组由调用方负责。
    """
    wb = wb or C.WORKBUDDY
    proj = proj or C.PROJ
    now = time.time() if now is None else now
    cutoff = now - fresh_min * 60 if fresh_min else 0

    rows = []

    def consider(kind, path, sid, suffix):
        size, mt = _node(path)
        if cutoff and mt >= cutoff:
            return
        if suffix == ".jsonl" and not include_transcripts:
            rows.append({"kind": kind + "（转录，仅报告）", "sid": sid,
                         "path": path, "size": size, "mtime": mt,
                         "transcript": True})
            return
        rows.append({"kind": kind, "sid": sid, "path": path,
                     "size": size, "mtime": mt, "transcript": False})

    for sub, suffixes in ROOT_CHILDREN:
        d = os.path.join(wb, *sub.split("/"))
        if not os.path.isdir(d):
            continue
        pat = _pattern(suffixes)
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            if os.path.islink(os.path.join(d, name)):
                continue
            m = pat.match(name)
            if not m:
                continue
            sid, suf = _split(m)
            if sid in known:
                continue
            consider(sub, os.path.join(d, name), sid, suf)

    if os.path.isdir(proj):
        pat = _pattern(PROJ_SUFFIXES)
        for name in os.listdir(proj):
            entry = os.path.join(proj, name)
            # projects/<编码cwd>/ 是工作目录，目录名不是 uuid，正则天然不匹配；
            # 这里再显式排除符号链接与文件，避免误判成「项目目录」。
            if os.path.islink(entry) or not os.path.isdir(entry):
                continue
            try:
                children = os.listdir(entry)
            except OSError:
                continue
            for child in children:
                p = os.path.join(entry, child)
                if os.path.islink(p):
                    continue
                m = pat.match(child)
                if not m:
                    continue
                sid, suf = _split(m)
                if sid in known:
                    continue
                kind = "projects/<cwd>/" + (suf or "<会话目录>")
                consider(kind, p, sid, suf)
    return rows


def empty_cwd_dirs(proj=None, older_than_hours=EMPTY_CWD_HOURS, now=None):
    """`projects/<编码cwd>/` 里已经空了的工作目录。

    删掉某个项目的最后一条会话后，目录本身不会被谁回收 —— 客户端下次用到会
    重建，留着只是让 `projects` 下堆一串 0 字节目录（本机 134 个、合计 0 字节）。
    这是「删会话」的收尾，不改补丁也能由工具收干净。
    门槛取 24 小时闲置，避免把「刚建目录、会话还没写进去」当成空壳收掉。
    """
    proj = proj or C.PROJ
    now = time.time() if now is None else now
    cutoff = now - older_than_hours * 3600
    out = []
    if not os.path.isdir(proj):
        return out
    for name in os.listdir(proj):
        p = os.path.join(proj, name)
        if not name.startswith("c-") or os.path.islink(p) or not os.path.isdir(p):
            continue
        try:
            if os.listdir(p) or os.path.getmtime(p) >= cutoff:
                continue
        except OSError:
            continue
        out.append(p)
    return out


def summarize(rows):
    out = {}
    for r in rows:
        g = out.setdefault(r["kind"], {"n": 0, "bytes": 0, "ids": set()})
        g["n"] += 1
        g["bytes"] += r["size"]
        g["ids"].add(r["sid"])
    return out


def archive_dir(stamp=None):
    stamp = stamp or datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return os.path.join(C.ARCHIVE, "orphans-%s" % stamp), stamp


def _move(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        os.replace(src, dst)                     # 同盘 rename，瞬时
    except OSError:
        if os.path.isdir(src) and not os.path.islink(src):
            shutil.copytree(src, dst)
            shutil.rmtree(src)
        else:
            shutil.copy2(src, dst)
            os.remove(src)


def to_archive(rows, wb=None, stamp=None, quiet=False):
    """把孤儿节点原样摘到归档区，返回 (stamp, 归档目录, 移动数, 字节)。

    保留相对路径结构，`--restore` 才能原路放回。
    """
    wb = wb or C.WORKBUDDY
    dest, stamp = archive_dir(stamp)
    os.makedirs(dest, exist_ok=True)
    moved = size = 0
    with open(os.path.join(dest, MANIFEST), "a", encoding="utf-8") as man:
        for r in rows:
            rel = os.path.relpath(r["path"], wb)
            if rel.startswith(".."):
                if not quiet:
                    print("  跳过（不在数据区）：%s" % r["path"])
                continue
            target = os.path.join(dest, rel)
            _move(r["path"], target)
            man.write(json.dumps({"kind": r["kind"], "id": r["sid"],
                                  "rel": rel, "size": r["size"],
                                  "mtime": r["mtime"]}, ensure_ascii=False) + "\n")
            moved += 1
            size += r["size"]
            if not quiet:
                print("  %s %9.1f KB  %s" % (r["sid"][:8], r["size"] / 1024.0, rel))
    return stamp, dest, moved, size


def list_archive():
    if not os.path.isdir(C.ARCHIVE):
        return []
    out = []
    for name in sorted(os.listdir(C.ARCHIVE)):
        d = os.path.join(C.ARCHIVE, name)
        if not os.path.isdir(d) or not name.startswith("orphans-"):
            continue
        n = b = 0
        for root, _dirs, files in os.walk(d):
            for f in files:
                if f == MANIFEST:
                    continue
                n += 1
                try:
                    b += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        out.append({"stamp": name[len("orphans-"):], "dir": d,
                    "files": n, "bytes": b})
    return out


def restore(stamp, wb=None, quiet=False):
    """把某个归档批次原路放回数据区。已存在的目标不覆盖，跳过并报告。"""
    wb = wb or C.WORKBUDDY
    src = os.path.join(C.ARCHIVE, "orphans-%s" % stamp)
    if not os.path.isdir(src):
        raise SystemExit("没有这个归档批次：%s" % src)
    back = skipped = 0
    for root, _dirs, files in os.walk(src):
        for f in files:
            if f == MANIFEST:
                continue
            p = os.path.join(root, f)
            rel = os.path.relpath(p, src)
            dst = os.path.join(wb, rel)
            if os.path.exists(dst):
                skipped += 1
                if not quiet:
                    print("  已存在，跳过：%s" % rel)
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            _move(p, dst)
            back += 1
    # 清掉归档里已经空掉的目录
    for root, dirs, files in os.walk(src, topdown=False):
        if root == src:
            continue
        if not os.listdir(root):
            try:
                os.rmdir(root)
            except OSError:
                pass
    if not quiet:
        print("已放回 %d 个节点%s" % (back, "；跳过 %d 个" % skipped if skipped else ""))
    return back, skipped


def purge(older_than_days=7, now=None, quiet=False):
    """真删归档批次。只动 archive/orphans-*，且必须超过保留天数。"""
    now = time.time() if now is None else now
    cutoff = now - older_than_days * 86400
    gone = freed = 0
    for a in list_archive():
        # 归档批次按目录自身 mtime 计龄：内部文件是 move 过来的，保留的是
        # 原始 mtime，拿它们算会一归档就判「过期」。
        try:
            age_ref = os.path.getmtime(a["dir"])
        except OSError:
            continue
        if age_ref > cutoff:
            continue
        shutil.rmtree(a["dir"])
        gone += 1
        freed += a["bytes"]
        if not quiet:
            print("  已清归档 %s（%d 个文件，%.1f MB）"
                  % (a["stamp"], a["files"], a["bytes"] / 1048576.0))
    if not quiet and not gone:
        print("没有超过 %d 天的归档批次" % older_than_days)
    return gone, freed


def main(argv=None):
    ap = argparse.ArgumentParser(description="回收 sessions 表里已无行的派生文件")
    ap.add_argument("--apply", action="store_true",
                    help="摘到 archive/orphans-<stamp>/（不直删）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--include-transcripts", action="store_true",
                    help="把孤儿 .jsonl 转录也一起处理（默认只报告）")
    ap.add_argument("--fresh-min", type=int, default=FRESH_MIN,
                    help="多少分钟内动过的文件不判孤儿（默认 %d）" % FRESH_MIN)
    ap.add_argument("--list-archive", action="store_true")
    ap.add_argument("--prune-empty-cwd", action="store_true",
                    help="收掉 projects/ 下已空的 <编码cwd> 目录（配合 --apply）")
    ap.add_argument("--empty-hours", type=int, default=EMPTY_CWD_HOURS,
                    help="空目录闲置多少小时才收（默认 %d）" % EMPTY_CWD_HOURS)
    ap.add_argument("--restore", metavar="STAMP")
    ap.add_argument("--purge-archive", action="store_true")
    ap.add_argument("--older-than", type=int, default=7)
    a = ap.parse_args(argv)

    if a.list_archive:
        items = list_archive()
        if not items:
            print("归档区为空（%s）" % C.ARCHIVE)
            return 0
        for it in items:
            print("  %s  %4d 个文件  %7.1f MB  %s"
                  % (it["stamp"], it["files"], it["bytes"] / 1048576.0, it["dir"]))
        return 0
    if a.restore:
        restore(a.restore)
        return 0
    if a.purge_archive:
        purge(a.older_than)
        return 0
    if a.prune_empty_cwd:
        dirs = empty_cwd_dirs(older_than_hours=a.empty_hours)
        if not dirs:
            print("projects 下没有超 %d 小时的空调试目录" % a.empty_hours)
            return 0
        print("空工作目录 %d 个（0 内容、闲置超 %d 小时）" % (len(dirs), a.empty_hours))
        for d in dirs[:10]:
            print("  %s" % os.path.basename(d))
        if len(dirs) > 10:
            print("  … 另有 %d 个" % (len(dirs) - 10))
        if not a.apply:
            print("\n只读模式：加 --apply 后按目录逐个 rmdir（只删空目录）")
            return 0
        gone = 0
        for d in dirs:
            try:
                os.rmdir(d)                      # 只用 rmdir：非空会自己失败
                gone += 1
            except OSError as e:
                print("  跳过 %s：%s" % (os.path.basename(d), e))
        print("\n已收掉 %d 个空目录" % gone)
        return 0

    rows = scan(known_ids(), include_transcripts=a.include_transcripts,
                fresh_min=a.fresh_min)
    todo = [r for r in rows if not r["transcript"]]
    rep = [r for r in rows if r["transcript"]]

    if a.json:
        print(json.dumps({"orphans": rows,
                          "summary": {k: {"n": v["n"], "bytes": v["bytes"]}
                                      for k, v in summarize(rows).items()},
                          "actionable": len(todo),
                          "archive": C.ARCHIVE}, ensure_ascii=False, indent=2))
        return 0

    groups = summarize(rows)
    if not groups:
        print("没有孤儿派生文件（在册会话 %d 条）" % len(known_ids()))
        return 0

    total_n = len(rows)
    total_b = sum(r["size"] for r in rows)
    print("孤儿节点 %d 个 / %.1f MB（在册会话 %d 条）"
          % (total_n, total_b / 1048576.0, len(known_ids())))
    for kind in sorted(groups):
        g = groups[kind]
        print("  %-34s %4d 个 / %7.1f MB" % (kind, g["n"], g["bytes"] / 1048576.0))
    for r in sorted(rep, key=lambda x: -x["size"])[:5]:
        print("  转录（需人工确认）：%s %.1f MB %s"
              % (r["sid"][:8], r["size"] / 1048576.0,
                 os.path.relpath(r["path"], C.WORKBUDDY)))

    if not a.apply:
        print("\n只读模式：加 --apply 摘到 %s\\orphans-<stamp>\\（可 --restore 放回）"
              % C.ARCHIVE)
        return 0
    if not todo:
        print("\n没有可处理的节点（转录需 --include-transcripts）")
        return 0

    stamp, dest, moved, size = to_archive(todo)
    print("\n已摘出 %d 个节点 / %.1f MB → %s" % (moved, size / 1048576.0, dest))
    print("清单：%s" % os.path.join(dest, MANIFEST))
    print("放回：python wd_orphans.py --restore %s" % stamp)
    print("清归档：python wd_orphans.py --purge-archive --older-than 7")
    return 0


if __name__ == "__main__":
    sys.exit(main())
