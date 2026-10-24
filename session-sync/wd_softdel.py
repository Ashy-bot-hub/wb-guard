# -*- coding: utf-8 -*-
"""软删积压核对：官方界面的「删除」只写 deleted_at，行和数据全留在原地。

问题是怎么被发现
----------------
面板（WorkDaddy 会话页）的删除走 `/api/sessions/delete`，是真实删除；而
**官方侧边栏的删除只把 sessions.deleted_at 置位**。于是同一次「删除」，
走面板能清干净，走官方界面就只留一具空壳：

* 会话行永久留在库里参与每一次全表扫描（本机 388/539 条是软的，占 72%）
* `session_usage` 里「上下文用量环」行没人回收（本机 90 行指向软删会话）
* 派生的转录、changes-detail、file-tree-manifests 等 11 处文件一个不少
  （本机 280/388 条仍有落盘，788 个节点）
* 本包自己的 `wd_unify --prune-empty` 也在软删空壳，所以这套积压是**持续产生**
  的，不是历史遗留 —— 只做一次性清理，过几个月会原样长回来

三条收敛路径，按风险从低到高
----------------------------
1. `--purge-orphan-usage`：只清「usage 行指向的会话 id 已完全不存在」的行。
   这类行没有任何读取路径能命中（各端都是 `WHERE session_id = ?`），删掉不影响
   任何界面，风险≈0，可以每天自动跑。
2. `--prune`：把**软删已超过宽限期**的会话按硬删收敛 —— 清 11 处落盘文件 +
   usage 行 + 会话行。不可逆，所以：默认只读、必须 `--yes`、先整库备份、
   单批限量、每条都写进 ndjson 日志。
3. 剩余的 autoCopy 幽灵索引要客户端退出后跑 `wd_audit.py --fix` 收尾：
   本脚本不动 meta.json（那是客户端内存态，运行中写必被覆盖）。

边界
----
* 只处理 `deleted_at IS NOT NULL` 的行；写库语句里再带一次
  `AND deleted_at IS NOT NULL`，防止判据改错时误删活跃会话。
* 文件只删白名单里的 11 处，且必须落在 `~/.workbuddy` 之下、名字与 id 严格对应。
* 软删会话仍有活跃同指纹副本时不跳过：文件是按 id 命名的，删 A 的链接不影响 B，
  而 wd_unify 合并后是硬链接，删一个 link 也不会带走另一个。

用法：
    python wd_softdel.py                          # 只读报告
    python wd_softdel.py --json                   # 供体检/守护调用
    python wd_softdel.py --purge-orphan-usage --yes
    python wd_softdel.py --prune --older-than 7d --yes
    python wd_softdel.py --prune --older-than 30d --limit 20 --yes
"""
import argparse
import datetime
import json
import os
import re
import shutil
import sqlite3
import sys

import wdconfig as C

GRACE_DAYS = 7          # 软删后至少留这么久，误删还能从备份捞回来
BATCH_LIMIT = 50        # 单次 --prune 的上限；积压多时分几批跑，每批都有备份
SID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

# 与 daemon.js 的 deleteSessionFiles() 一一对应。两边任何一处改了都要同步，
# 否则面板删干净了、这里的核对还报「有残留」，或反过来漏清。
PROJECT_SUFFIXES = ("", ".jsonl", ".meta.json", ".file-rollback.ndjson")
ROOT_CHILDREN = (
    ("workspace/sessions", ""),
    ("tasks", ""),
    ("file-history", ""),
    ("artifact-index", ".json"),
    ("changes-detail", ""),
    ("changes-index", ".json"),
    ("file-tree-manifests", ".json"),
)


def now_ms():
    return int(datetime.datetime.now().timestamp() * 1000)


def connect(readonly=True):
    uri = "file:%s%s" % (C.DB.replace("\\", "/"), "?mode=ro" if readonly else "")
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------- 落盘索引

def build_index(wb=None):
    """一次遍历建索引：{sid: [路径...]}，只收「名字就是会话 id」的节点。

    逐条会话去 os.path.exists 11 次，388 条就是 4000+ 次系统调用，还要遍历
    ~200 个项目目录。改成走一遍 projects/ 建表，之后每条会话只做字典查询。
    """
    wb = wb or C.WORKBUDDY
    idx = {}

    def add(sid, path):
        idx.setdefault(sid, []).append(path)

    proj_root = os.path.join(wb, "projects")
    if os.path.isdir(proj_root):
        for name in os.listdir(proj_root):
            pj = os.path.join(proj_root, name)
            if not os.path.isdir(pj):
                continue
            try:
                entries = os.listdir(pj)
            except OSError:
                continue
            for e in entries:
                sid = e.split(".")[0]
                if not SID_RE.match(sid):
                    continue
                # 只认这 4 种形态，别的（如同名前缀的其他文件）不碰
                if e == sid or e in (sid + ".jsonl", sid + ".meta.json",
                                     sid + ".file-rollback.ndjson"):
                    add(sid, os.path.join(pj, e))
    for sub, ext in ROOT_CHILDREN:
        d = os.path.join(wb, *sub.split("/"))
        if not os.path.isdir(d):
            continue
        try:
            entries = os.listdir(d)
        except OSError:
            continue
        for e in entries:
            sid = e[:-len(ext)] if ext and e.endswith(ext) else e
            if ext and not e.endswith(ext):
                continue
            if not SID_RE.match(sid):
                continue
            add(sid, os.path.join(d, e))
    return idx


def node_size(path):
    """文件取大小，目录递归求和。读不到按 0 计（不因单个节点中断统计）。"""
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            total = 0
            for root, _dirs, files in os.walk(path):
                for f in files:
                    try:
                        total += os.path.getsize(os.path.join(root, f))
                    except OSError:
                        pass
            return total
        return os.path.getsize(path)
    except OSError:
        return 0


# ---------------------------------------------------------------- 只读统计

def counts(conn=None, index=None):
    """只读统计，供体检与守护调用。字段名保持稳定，改这里要同步 wd_doctor。

    只取用得到的列、按位置读元组：调用方可能传来没设 row_factory 的连接
    （测试与守护都是），依赖 dict(row) 会直接炸。
    """
    own = conn is None
    conn = conn or connect()
    try:
        soft = conn.execute(
            "SELECT id, user_id, title, cwd, deleted_at FROM sessions "
            "WHERE deleted_at IS NOT NULL").fetchall()
        live = conn.execute(
            "SELECT id FROM sessions WHERE deleted_at IS NULL").fetchall()
        usage = [r[0] for r in conn.execute("SELECT session_id FROM session_usage")]
    finally:
        if own:
            conn.close()

    soft_ids = {r[0] for r in soft}
    live_ids = {r[0] for r in live}
    idx = index if index is not None else build_index()
    with_files = [r for r in soft if idx.get(r[0])]
    bytes_ = sum(node_size(p) for r in with_files for p in idx[r[0]])
    cutoff = now_ms() - GRACE_DAYS * 86400000
    due = [r for r in soft if (r[4] or 0) <= cutoff]

    return {
        "soft": len(soft),
        "live": len(live),
        "orphan_usage": len([s for s in usage if s not in soft_ids and s not in live_ids]),
        "soft_usage": len([s for s in usage if s in soft_ids]),
        "usage_total": len(usage),
        "soft_with_files": len(with_files),
        "soft_file_nodes": sum(len(idx[r[0]]) for r in with_files),
        "soft_bytes": bytes_,
        "due": len(due),
        "grace_days": GRACE_DAYS,
    }


def judge(n):
    """把统计映射成体检档位。阈值取「积压是否已经妨碍日常判断」而不是百分比。"""
    if n["soft"] == 0 and n["orphan_usage"] == 0:
        return "ok", "无软删积压"
    if n["soft"] > 200 or n["orphan_usage"] > 100:
        return "warn", "软删积压偏多"
    return "ok", "软删积压在容差内"


# ---------------------------------------------------------------- 写操作

def _under(path, root):
    """路径必须落在 root 之内（防白名单里混入越界路径）。"""
    try:
        p = os.path.realpath(path)
        r = os.path.realpath(root)
        return p == r or p.startswith(r + os.sep)
    except OSError:
        return False


def remove_nodes(sid, paths, wb=None):
    """删掉一条会话的所有落盘节点，返回 (删掉的节点数, 释放字节)。"""
    wb = wb or C.WORKBUDDY
    removed = freed = 0
    for p in paths:
        base = os.path.basename(p)
        if not (base == sid or base.startswith(sid + ".")):
            continue                       # 名字对不上，宁可不删
        if not _under(p, wb):
            continue                       # 越界，宁可不删
        size = node_size(p)
        try:
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p)
            else:
                os.remove(p)
            removed += 1
            freed += size
        except OSError:
            pass                           # 被占用就留到下一轮，不抛
    return removed, freed


def backup_db(tag="softdel"):
    C.ensure_dirs()
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = os.path.join(C.BACKUP, "workbuddy-pre-%s-%s.db" % (tag, stamp))
    try:
        src = sqlite3.connect(C.DB)
        src.execute("VACUUM INTO ?", (bak,))    # 比 cp 安全：WAL 未落盘也能拿到一致快照
        src.close()
    except sqlite3.Error:
        shutil.copy2(C.DB, bak)
    return bak


def purge_orphan_usage(conn, apply=False, limit=None):
    """清掉指向「会话行已完全不存在」的 usage 行。

    这些行不会被任何界面读到：各端读用量都是 `WHERE session_id = ?`。
    """
    have = {r[0] for r in conn.execute("SELECT id FROM sessions")}
    orphans = [r[0] for r in conn.execute("SELECT session_id FROM session_usage")
               if r[0] not in have]
    if limit:
        orphans = orphans[:limit]
    print("孤儿 usage 行：%d 条%s" % (len(orphans), "（本批 %d）" % len(orphans) if limit else ""))
    if not orphans or not apply:
        return 0
    conn.executemany("DELETE FROM session_usage WHERE session_id=?", [(s,) for s in orphans])
    conn.commit()
    return len(orphans)


def prune_soft(conn, older_than_ms, apply=False, limit=BATCH_LIMIT, index=None, wb=None,
               do_backup=True):
    """硬删「软删已过宽限期」的会话：文件 → usage → 行。"""
    rows = conn.execute(
        "SELECT id, user_id, title, cwd, deleted_at FROM sessions "
        "WHERE deleted_at IS NOT NULL AND deleted_at <= ? "
        "ORDER BY deleted_at LIMIT ?", (older_than_ms, limit)).fetchall()
    idx = index if index is not None else build_index(wb)
    print("到期可清理（软删超 %d 天）：%d 条%s"
          % (GRACE_DAYS, len(rows), "" if len(rows) < limit else "（受单批 %d 上限约束）" % limit))
    for r in rows[:10]:
        stamp = datetime.datetime.fromtimestamp((r[4] or 0) / 1000).strftime("%m-%d")
        print("   %s %s  %s" % (stamp, r[0][:8], (r[2] or "(无标题)")[:34]))
    if len(rows) > 10:
        print("   ……另有 %d 条" % (len(rows) - 10))
    if not rows:
        return {"pruned": 0, "files": 0, "freed": 0}
    if not apply:
        return {"planned": len(rows), "pruned": 0, "files": 0, "freed": 0}

    bak = backup_db() if do_backup else None
    if bak:
        print("备份：%s" % bak)
    ids = [r[0] for r in rows]
    files = freed = 0
    log_path = os.path.join(C.BACKUP, "softdel-%s.ndjson"
                            % datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    os.makedirs(C.BACKUP, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as log:
        for r in rows:
            sid = r[0]
            n, b = remove_nodes(sid, idx.get(sid, []), wb)
            files += n
            freed += b
            log.write(json.dumps({
                "id": sid, "user_id": r[1], "title": r[2],
                "cwd": r[3], "deleted_at": r[4],
                "nodes": idx.get(sid, []), "removed": n, "bytes": b,
            }, ensure_ascii=False) + "\n")
            log.flush()
    conn.executemany("DELETE FROM session_usage WHERE session_id=?", [(s,) for s in ids])
    # 写库语句再带一次判据：判据若被改错，这一句仍不会碰到活跃会话
    conn.executemany("DELETE FROM sessions WHERE id=? AND deleted_at IS NOT NULL",
                     [(s,) for s in ids])
    conn.commit()
    print("已清理 %d 条会话 / %d 个文件节点 / 释放 %.1f MB" % (len(ids), files, freed / 1048576))
    print("明细：%s" % log_path)
    if bak:
        print("还原：把 %s 复制回 %s（客户端未运行时可整体回滚）" % (bak, C.DB))
    print("收尾：退出客户端后跑 wd_audit.py --fix，剔除这些 id 的 autoCopy 幽灵索引")
    return {"pruned": len(ids), "files": files, "freed": freed, "backup": bak}


def parse_age(text):
    """把 7d / 24h / 30d 解析成毫秒阈值。写死天数会让「想更保守」没法表达。"""
    m = re.match(r"^(\d+)\s*([dh])$", (text or "").strip().lower())
    if not m:
        raise argparse.ArgumentTypeError("时间格式应为 7d 或 24h")
    n, unit = int(m.group(1)), m.group(2)
    return now_ms() - n * (86400000 if unit == "d" else 3600000)


# ---------------------------------------------------------------- 入口

def report(n):
    print("库现状：软删 %d 条 / 未删 %d 条（宽限期 %d 天）"
          % (n["soft"], n["live"], n["grace_days"]))
    print("usage：共 %d 行，指向软删 %d、指向已不存在 %d"
          % (n["usage_total"], n["soft_usage"], n["orphan_usage"]))
    print("软删落盘：%d 条仍有文件，共 %d 个节点，约 %.1f MB"
          % (n["soft_with_files"], n["soft_file_nodes"], n["soft_bytes"] / 1048576))
    print("到期可清理：%d 条" % n["due"])
    state, why = judge(n)
    print("判定：%s（%s）" % ({"ok": "达标", "warn": "告警"}[state], why))


def main(argv=None):
    ap = argparse.ArgumentParser(description="软删积压核对与收敛")
    ap.add_argument("--json", action="store_true", help="输出 JSON（供体检/守护调用）")
    ap.add_argument("--purge-orphan-usage", action="store_true", help="清孤儿 usage 行")
    ap.add_argument("--prune", action="store_true", help="硬删过期的软删会话")
    ap.add_argument("--older-than", type=parse_age, default=None,
                    help="只清理软删超过此时的会话，如 7d / 24h")
    ap.add_argument("--limit", type=int, default=BATCH_LIMIT, help="单批上限")
    ap.add_argument("--yes", action="store_true", help="确认真实写入")
    a = ap.parse_args(argv)

    if not os.path.exists(C.DB):
        print("找不到会话库：%s" % C.DB, file=sys.stderr)
        return 2

    conn = connect(readonly=not a.yes)
    try:
        idx = build_index()
        n = counts(conn, idx)
        if a.json:
            state, why = judge(n)
            print(json.dumps({"status": state, "why": why, **n}, ensure_ascii=False))
            return 0
        report(n)

        did = False
        # 一次写入只备份一次：两个动作同批跑时不必各存一份整库
        if a.yes and (a.purge_orphan_usage or a.prune):
            print("\n备份：%s" % backup_db())
        if a.purge_orphan_usage:
            did = True
            print()
            k = purge_orphan_usage(conn, apply=a.yes)
            if k:
                print("已清理孤儿 usage %d 行" % k)
        if a.prune:
            did = True
            print()
            cutoff = a.older_than if a.older_than is not None else now_ms() - GRACE_DAYS * 86400000
            r = prune_soft(conn, cutoff, apply=a.yes, limit=a.limit, index=idx,
                           do_backup=False)
            if not a.yes and r.get("planned"):
                print("\n只读模式：加 --yes 真正执行（会先备份整库）")
        if not did:
            print("\n只读报告。可执行：--purge-orphan-usage（低风险）"
                  "/ --prune --older-than 7d（不可逆，需 --yes）")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
