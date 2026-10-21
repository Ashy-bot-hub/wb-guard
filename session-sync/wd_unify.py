# -*- coding: utf-8 -*-
"""任务血缘统一（存储层）：让同一任务在各账号下的转录共享同一份物理内容。

职责边界
--------
上游 `autoCopy` 负责「有没有这条会话」，`daemon.js` 的快照同步负责「内容往哪边
收敛」。两者都不处理存储层：复制出来的转录是独立 inode，同一任务因此占两份磁盘。
本脚本只做上游不做的四件事：

1. 把内容可覆盖的副本合并成同一 inode（省磁盘，且任何一边追加写入另一边立即可见）；
2. 退役同账号内重名的副本（软删，可整批还原）；
3. 软删零内容空壳会话；
4. 纠正 status 污染与残留（同组多 working、标着 working 但早已无写入）。

合并判据（只合并不丢内容）
--------------------------
合并前先比字节，只有「一方是另一方的前缀或两者完全相同」才合并到内容更长的一方。
这覆盖了转录的常态：复制之后某一边继续对话，文件就是另一份的追加扩展。
两边各有对方没有的内容（真分叉，例如两边都单独聊过）—— 不动，计数上报，
交给上游的快照同步收敛；收敛到前缀关系后，下一轮自然合并。

不做元数据同步：`updated_at` / `title` / `custom_title` 的跨账号对齐由上游快照
同步完成，本脚本重复写一遍只会在两边同时写入时互相覆盖。

用法：
    python wd_unify.py                      # 预演
    python wd_unify.py --apply              # 执行
    python wd_unify.py --prune-empty --hours 48 --apply
    python wd_unify.py --undo               # 回滚最近一批
"""
import datetime
import json
import os
import shutil
import sqlite3
import sys

import wdconfig as C
import wd_fs as F

BACKUP = os.path.join(C.BACKUP, "lineage")
REG = os.path.join(C.HERE, "lineage-registry.json")

KEEP_BATCHES = 3          # 备份批次上限；设 0 关闭自动清理
STALE_HOURS = 2           # 超过这个时长没有任何写入，就认定状态是残留而非真实进行中
CHUNK = 1 << 20           # 比较内容时的读取块


def save_hist(hist):
    """原子写血缘登记表。

    `--undo` 完全依赖这份记录；原地覆盖一旦被强杀打断，`_read_json` 读不动
    就回退成空表，撤销能力会静默消失。走 `F.atomic_write` 后，要么是旧内容，
    要么是新内容，不存在半截。
    """
    F.atomic_write(REG, json.dumps(hist, ensure_ascii=False, indent=1))


_TRANSCRIPTS = None


def transcript_index(force=False):
    """session_id → 转录文件路径。整个 projects/ 只遍历一次。

    逐个会话 `glob.glob(PROJ/**/<sid>.jsonl, recursive=True)` 等于把转录树扫 N 遍：
    本机 75 个会话实测 3,728 ms，一次遍历建索引后 62 ms。

    用 scandir 栈而不是 os.walk：`os.walk` 在每个目录上都要判链接（本机 521 次、
    19 ms），而 `DirEntry.is_dir()` 直接用枚举时已带出的属性，不落盘。
    """
    global _TRANSCRIPTS
    if _TRANSCRIPTS is None or force:
        idx = {}
        stack = [C.PROJ]
        while stack:
            base = stack.pop()
            try:
                it = os.scandir(base)
            except OSError:
                continue
            with it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.name.endswith(".jsonl"):
                            idx.setdefault(e.name[:-6], e.path)
                    except OSError:
                        continue
        _TRANSCRIPTS = idx
    return _TRANSCRIPTS


def reset_transcript_index():
    """丢掉索引；测试与「运行期间新建了转录」的场景用。"""
    global _TRANSCRIPTS
    _TRANSCRIPTS = None


def jsonl_of(sid):
    return transcript_index().get(sid)


# 行数缓存：跨账号镜像把同一份转录硬链接给多个账号时，这些路径的
# (dev, ino, size, mtime) 完全相同，行数必然相同 —— 数一遍就够。
# 本机 75 个会话只对应 25 个 inode（每个 3 个账号），同一份 130 MB 数据
# 不必读三遍：count_lines 473 ms → 158 ms。
#
# 键里带 size 与 mtime_ns，是为了让「同一 inode 被追加写入」时自动失效，
# 不依赖「本进程是短命的」这个前提。
_LINES = {}
_LINES_MAX = 1024


def _lines_of(path, st):
    key = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
    hit = _LINES.get(key)
    if hit is None:
        if len(_LINES) >= _LINES_MAX:
            _LINES.clear()
        hit = F.count_lines(path)
        _LINES[key] = hit
    return hit


def reset_line_cache():
    """丢掉行数缓存；测试与「同一进程内文件被改写」的场景用。"""
    _LINES.clear()


def stat_of(sid):
    f = jsonl_of(sid)
    if not f:
        return {"path": None, "lines": -1, "mtime": 0, "ino": None, "size": -1}
    st = os.stat(f)
    return {"path": f, "lines": _lines_of(f, st), "mtime": st.st_mtime,
            "ino": (st.st_dev, st.st_ino), "size": st.st_size}


def pick_canonical(members, st):
    """代表会话：working > 行数 > updated_at > mtime。"""
    def key(m):
        return (1 if m["status"] == "working" else 0,
                st[m["id"]]["lines"],
                m["updated_at"] or 0,
                st[m["id"]]["mtime"])
    return max(members, key=key)


# 内容关系。命名以第一个参数（base）为参照。
SAME = "same"              # 完全相同
BASE_COVERS = "base"       # base 是 other 的追加扩展（other 全部内容在 base 里）
OTHER_COVERS = "other"     # other 是 base 的追加扩展
DIVERGE = "diverge"        # 两边各有对方没有的内容


def _prefix_equal(long_path, short_path):
    """short_path 的字节是否等于 long_path 的开头。"""
    with open(long_path, "rb") as a, open(short_path, "rb") as b:
        while True:
            piece = b.read(CHUNK)
            if not piece:
                return True
            if a.read(len(piece)) != piece:
                return False


def relation(base_path, other_path):
    """判断两份转录的内容关系；读不动时按「分叉」处理（宁可不动）。"""
    try:
        nb = os.path.getsize(base_path)
        no = os.path.getsize(other_path)
    except OSError:
        return DIVERGE
    try:
        if nb >= no:
            if not _prefix_equal(base_path, other_path):
                return DIVERGE
            return SAME if nb == no else BASE_COVERS
        if not _prefix_equal(other_path, base_path):
            return DIVERGE
        return OTHER_COVERS
    except OSError:
        return DIVERGE


def _merge_one(m, target, st, relink, diverged):
    """把 m 合并到 target 的 inode 上；不安全时记入 diverged。

    返回新的 target（m 内容更长时会顶替原 target）。
    """
    ms, ts = st[m["id"]], st[target["id"]]
    if not (ms["path"] and ts["path"]):
        return target                      # 缺转录的一方不参与
    if ms["ino"] == ts["ino"]:
        return target                      # 已经是同一份物理文件
    rel = relation(ts["path"], ms["path"])
    if rel in (SAME, BASE_COVERS):
        relink.append((m, target, ms))
        return target
    if rel == OTHER_COVERS:
        # m 是 target 的扩展：以 m 为新的承载文件，target 反过来并过去
        relink.append((target, m, ts))
        return m
    diverged.append((m, target))
    return target


def build_plan(conn):
    rows = [dict(r) for r in conn.execute(
        "SELECT id,user_id,cwd,title,custom_title,status,updated_at,created_at,"
        "last_activity_at FROM sessions WHERE deleted_at IS NULL")]

    groups = {}
    for r in rows:
        groups.setdefault((C.norm(r["cwd"]), r["title"] or ""), []).append(r)

    st = {r["id"]: stat_of(r["id"]) for r in rows}

    relink, diverged, dupes, status_fix = [], [], [], []
    groups_multi = 0

    for _key, members in groups.items():
        if len(members) < 2:
            continue
        groups_multi += 1
        target = pick_canonical(members, st)

        for m in members:
            if m["id"] == target["id"]:
                continue
            target = _merge_one(m, target, st, relink, diverged)

        # 同账号内多条：按 last_activity_at 择一保留。
        # 不能用「行数比较」—— 合并成硬链接后行数必然相等，该判据会永远跳过。
        by_uid = {}
        for m in members:
            by_uid.setdefault(m["user_id"], []).append(m)
        for _uid, ms in by_uid.items():
            if len(ms) < 2:
                continue
            keep = max(ms, key=lambda x: (1 if x["status"] == "working" else 0,
                                          x["last_activity_at"] or 0,
                                          x["updated_at"] or 0,
                                          st[x["id"]]["lines"]))
            for x in ms:
                if x["id"] != keep["id"]:
                    dupes.append((x, keep))

    # 同组多于一条 working 属状态污染：同步把 status 一并带过去所致，留最新一条
    for _key, members in groups.items():
        working = [m for m in members if m["status"] == "working"]
        if len(working) < 2:
            continue
        chief = max(working, key=lambda x: (x["last_activity_at"] or 0, x["updated_at"] or 0))
        for m in working:
            if m["id"] != chief["id"]:
                status_fix.append((m, "completed"))

    return (groups_multi, relink, diverged, dupes, status_fix,
            find_stale(rows, st), st)


def find_stale(rows, st, now_ms=None, hours=STALE_HOURS):
    """找出状态是残留的会话：标着 working/Pending，但其实早已没有任何写入。

    判据必须同时满足三条，避免把真正在跑的会话改掉：
      1. status 不是 completed；
      2. last_activity_at（缺失时用 updated_at）早于 hours 小时；
      3. 转录文件 mtime 也早于 hours 小时，或根本没有转录。
    第 3 条是关键：数据库时间戳会被跨账号同步改写，转录 mtime 不会。
    """
    import time as _t
    now_ms = now_ms if now_ms is not None else int(_t.time() * 1000)
    cut_ms = now_ms - hours * 3600 * 1000
    cut_s = now_ms / 1000.0 - hours * 3600
    out = []
    for r in rows:
        if r["status"] == "completed":
            continue
        last = r["last_activity_at"] or r["updated_at"] or 0
        if last >= cut_ms:
            continue
        s = st[r["id"]]
        if s["path"] and s["mtime"] >= cut_s:
            continue                 # 文件还在被写，不动
        out.append((r, "completed"))
    return out


def find_empty_shells(rows, st, now_ms=None, hours=1):
    """零内容空壳：没有任何转录行，且创建已超过 hours 小时。

    这类会话在侧边栏里是一条永远空白的任务，删掉不丢数据（本来就没有）。
    """
    import time as _t
    now_ms = now_ms if now_ms is not None else int(_t.time() * 1000)
    cut_ms = now_ms - hours * 3600 * 1000
    out = []
    for r in rows:
        s = st[r["id"]]
        if s["path"] and s["lines"] > 0:
            continue
        if (r["created_at"] or 0) >= cut_ms:
            continue
        out.append(r)
    return out


def report(groups_multi, relink, diverged, dupes, status_fix, stale, st):
    print("跨账号任务组          : %d 个" % groups_multi)
    print("可合并为同一份转录    : %d 条" % len(relink))
    print("内容已分叉（本轮不动）: %d 条" % len(diverged))
    print("同账号内多余副本      : %d 条" % len(dupes))
    print("status 污染待纠正     : %d 条" % len(status_fix))
    print("残留状态待归位        : %d 条" % len(stale))
    print()
    if relink:
        print("--- 转录合并明细 ---")
        for m, target, _ms in relink[:20]:
            print("  %s(%s %d行) => %s(%s %d行)  %s" % (
                m["id"][:8], m["user_id"][:8], st[m["id"]]["lines"],
                target["id"][:8], target["user_id"][:8], st[target["id"]]["lines"],
                (m["title"] or "")[:34]))
        if len(relink) > 20:
            print("  … 另有 %d 条" % (len(relink) - 20))
        print()
    if diverged:
        print("--- 内容分叉（两边都有独有内容，留给上游收敛）---")
        for m, target in diverged[:20]:
            print("  %s(%s %d行) != %s(%s %d行)  %s" % (
                m["id"][:8], m["user_id"][:8], st[m["id"]]["lines"],
                target["id"][:8], target["user_id"][:8], st[target["id"]]["lines"],
                (m["title"] or "")[:34]))
        if len(diverged) > 20:
            print("  … 另有 %d 条" % (len(diverged) - 20))
        print()
    if dupes:
        print("--- 同账号多余副本（软删）---")
        for x, keep in dupes[:20]:
            print("  退役 %s(%d行,%s) 保留 %s(%d行)  %s" % (
                x["id"][:8], st[x["id"]]["lines"], x["user_id"][:8],
                keep["id"][:8], st[keep["id"]]["lines"], (x["title"] or "")[:34]))
        if len(dupes) > 20:
            print("  … 另有 %d 条" % (len(dupes) - 20))
        print()
    if status_fix:
        print("--- status 污染（同组多 working）---")
        for m, ns in status_fix[:20]:
            print("  %s(%s) working -> %s  %s" % (
                m["id"][:8], m["user_id"][:8], ns, (m["title"] or "")[:34]))
        if len(status_fix) > 20:
            print("  … 另有 %d 条" % (len(status_fix) - 20))
    if stale:
        print("--- 残留状态（超 %d 小时无任何写入）---" % STALE_HOURS)
        for m, ns in stale[:20]:
            print("  %s(%s) %s -> %s  %s" % (
                m["id"][:8], m["user_id"][:8], m["status"], ns, (m["title"] or "")[:34]))
        if len(stale) > 20:
            print("  … 另有 %d 条" % (len(stale) - 20))


def prune_backups(keep):
    """只保留最近 keep 个批次。每批约 30 MB（DB 11 MB + 转录副本）。"""
    if keep <= 0 or not os.path.isdir(BACKUP):
        return
    stamps = sorted(d for d in os.listdir(BACKUP)
                    if os.path.isdir(os.path.join(BACKUP, d)))
    dead = stamps[:-keep] if len(stamps) > keep else []
    freed = 0
    for st in dead:
        d = os.path.join(BACKUP, st)
        for root, _dirs, files in os.walk(d):
            for f in files:
                try:
                    freed += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        try:
            shutil.rmtree(d)
        except OSError as e:
            print("  清理旧批次失败 %s: %s" % (st, e))
            continue
        db = os.path.join(BACKUP, "workbuddy-pre-lineage-%s.db" % st)
        if os.path.exists(db):
            try:
                freed += os.path.getsize(db)
                os.remove(db)
            except OSError:
                pass
        print("  已清理旧批次 %s" % st)
    if freed:
        print("  释放 %.1f MB（保留最近 %d 批）" % (freed / 1048576.0, keep))


def prune_empty(conn, apply=False, hours=1):
    """软删零内容空壳会话。

    判据：转录 0 行（或压根没有转录文件）+ 创建超过 hours 小时。
    这类会话在侧边栏是一条永远空白的任务，删掉不丢任何数据。
    用软删（deleted_at）而非物理删除，`--undo` 可整批还原。
    自动巡检用 hours=48，避免删掉用户刚建还没开始用的任务。
    """
    rows = [dict(r) for r in conn.execute(
        "SELECT id,user_id,cwd,title,custom_title,status,updated_at,created_at,"
        "last_activity_at FROM sessions WHERE deleted_at IS NULL")]
    st = {r["id"]: stat_of(r["id"]) for r in rows}
    shells = find_empty_shells(rows, st, hours=hours)

    print("零内容空壳会话：%d 条（转录 0 行且创建超 %d 小时）" % (len(shells), hours))
    for r in shells[:30]:
        print("  %s(%s) %s  cwd=%s" % (
            r["id"][:8], r["user_id"][:8], (r["title"] or "")[:30], r["cwd"]))
    if not shells:
        return 0
    if not apply:
        print("\n只读模式：加 --apply 软删（deleted_at 置位，可用 --undo 还原）")
        return 0

    C.ensure_dirs()
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    db_bak = os.path.join(BACKUP, "workbuddy-pre-prune-%s.db" % stamp)
    os.makedirs(BACKUP, exist_ok=True)
    shutil.copy2(C.DB, db_bak)

    now = int(datetime.datetime.now().timestamp() * 1000)
    ids = [r["id"] for r in shells]
    conn.execute("BEGIN")
    conn.executemany("UPDATE sessions SET deleted_at=? WHERE id=?",
                     [(now, sid) for sid in ids])
    conn.commit()

    hist = C._read_json(REG, [])
    hist.append({"stamp": stamp, "db_backup": db_bak, "prune_empty": ids,
                 "links": [], "deletes": [], "status_fixes": []})
    save_hist(hist)

    print("\n已软删 %d 条；DB 备份：%s" % (len(ids), db_bak))
    print("还原：python wd_unify.py --undo")
    return 0


def do_undo(conn):
    hist = C._read_json(REG, [])
    if not hist:
        print("登记为空，无法撤销。")
        return
    rec = hist.pop()
    bakdir = os.path.join(BACKUP, rec["stamp"])
    if not os.path.isdir(bakdir):
        print("注意：批次 %s 的备份已被清理，本次只能恢复软删记录，转录文件无法还原。"
              % rec["stamp"])

    for item in rec.get("deletes", []):
        conn.execute("UPDATE sessions SET deleted_at=NULL WHERE id=?", (item["sid"],))
    conn.commit()
    print("恢复软删 %d 条" % len(rec.get("deletes", [])))

    if rec.get("prune_empty"):
        conn.executemany("UPDATE sessions SET deleted_at=NULL WHERE id=?",
                         [(sid,) for sid in rec["prune_empty"]])
        conn.commit()
        print("恢复空壳会话 %d 条" % len(rec["prune_empty"]))

    for item in rec.get("links", []):
        src = os.path.join(bakdir, item["backup"])
        if os.path.exists(src):
            # 不「先 unlink 再 copy」：那样一旦 copy 失败，转录就没了。复制到
            # 临时文件再原子替换 —— 失败时原文件保持原样。
            if F.copy_atomic(src, item["path"]):
                print("  还原转录 %s" % item["sid"][:8])
            else:
                print("  还原失败 %s：目标被占用，备份仍在 %s" % (item["sid"][:8], src))

    save_hist(hist)
    print("撤销完成。")


def main():
    apply = "--apply" in sys.argv
    undo = "--undo" in sys.argv

    if not os.path.exists(C.DB):
        print("找不到会话库：%s" % C.DB)
        return 2

    # 写库互斥：守护在切号后与 6 小时兜底时自己跑 --apply，README 也教用户手动跑
    # 同一条命令。两条路径撞在一起时，SQLite 只保证行级不撕裂，保证不了
    # 「两次合并基于同一份快照」—— 转录会互相覆盖。预演只读，不加锁。
    if apply or undo or ("--prune-empty" in sys.argv):
        ok, _h = C.acquire_mutex(C.WRITE_LOCK)
        if not ok:
            print("另一进程正在写库（守护的定时任务或另一次手动执行）。稍后重试。")
            return 3

    conn = sqlite3.connect(C.DB, timeout=15)
    conn.row_factory = sqlite3.Row

    if undo:
        do_undo(conn)
        return 0

    if "--prune-empty" in sys.argv:
        hours = 1
        if "--hours" in sys.argv:
            try:
                hours = int(sys.argv[sys.argv.index("--hours") + 1])
            except (IndexError, ValueError):
                hours = 1
        return prune_empty(conn, apply=apply, hours=hours)

    groups_multi, relink, diverged, dupes, status_fix, stale, st = build_plan(conn)
    report(groups_multi, relink, diverged, dupes, status_fix, stale, st)

    if not apply:
        print("\n预演结束。加 --apply 执行（会备份转录原件与 DB）。")
        return 0
    if not (relink or dupes or status_fix or stale):
        print("\n无需改动。")
        return 0

    C.ensure_dirs()
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    bakdir = os.path.join(BACKUP, stamp)
    os.makedirs(bakdir, exist_ok=True)
    db_bak = os.path.join(BACKUP, "workbuddy-pre-lineage-%s.db" % stamp)
    shutil.copy2(C.DB, db_bak)
    print("\nDB 备份：%s" % db_bak)
    print("转录备份：%s" % bakdir)

    rec = {"stamp": stamp, "db_backup": db_bak,
           "links": [], "deletes": [], "status_fixes": []}

    for m, target, ms in relink:
        dst = ms["path"]
        name = "%s__%s.jsonl" % (m["id"][:8], m["user_id"][:8])
        shutil.copy2(dst, os.path.join(bakdir, name))
        src = st[target["id"]]["path"]
        try:
            os.unlink(dst)
            os.link(src, dst)
            rec["links"].append({"sid": m["id"], "backup": name,
                                 "canonical": target["id"], "path": dst})
            print("  合并 %s => %s" % (m["id"][:8], target["id"][:8]))
        except Exception as e:
            print("  失败 %s: %s" % (m["id"][:8], e))
            if not os.path.exists(dst):
                # 兜底还原也走原子写：直接 copy2 在这条路径上同样会留下半截文件
                if not F.copy_atomic(os.path.join(bakdir, name), dst):
                    print("  兜底还原失败 %s：备份在 %s" % (m["id"][:8], bakdir))

    if dupes:
        now = int(datetime.datetime.now().timestamp() * 1000)
        conn.execute("BEGIN")
        for x, keep in dupes:
            conn.execute("UPDATE sessions SET deleted_at=? WHERE id=?", (now, x["id"]))
            rec["deletes"].append({"sid": x["id"], "keep": keep["id"]})
        conn.commit()
        print("  退役副本 %d 条" % len(dupes))

    if status_fix:
        conn.execute("BEGIN")
        for m, ns in status_fix:
            conn.execute("UPDATE sessions SET status=? WHERE id=?", (ns, m["id"]))
            rec["status_fixes"].append({"sid": m["id"], "to": ns})
        conn.commit()
        print("  纠正 status %d 条" % len(status_fix))

    if stale:
        conn.execute("BEGIN")
        for m, ns in stale:
            conn.execute("UPDATE sessions SET status=? WHERE id=?", (ns, m["id"]))
            rec.setdefault("stale_fixes", []).append(
                {"sid": m["id"], "from": m["status"], "to": ns})
        conn.commit()
        print("  残留状态归位 %d 条" % len(stale))

    hist = C._read_json(REG, [])
    hist.append(rec)
    save_hist(hist)
    print("\n登记写入：%s" % REG)

    prune_backups(KEEP_BATCHES)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
