# -*- coding: utf-8 -*-
"""autoCopy 索引体检：找出「已标记但会话已不存在」的幽灵条目。

「已标记」的判定条件
--------------------
WorkDaddy 的跨账号复制状态存在 %APPDATA%/WorkDaddy/meta.json 的 autoCopy 段：

    autoCopy.sessionIndex[<uid>][<sessionId>] = <lineageId>

**只要会话 id 出现在这个索引里，就被判定为该账号已有此会话，直接跳过复制。**
它不检查 sessions.deleted_at。所以一条会话被软删后，索引仍然命中，
客户端认为不需要再复制，该目录在侧边栏就没有会话，空间随之消失。

上游只在它自己的「添加 / 删除 / 移动」三条路径维护这份索引。任何绕开这三条
路径的写入都会留下幽灵条目 —— 本包的 `wd_unify --prune-empty`（软删空壳）
正是其中一种，所以两者必须配套使用：unify 负责数据，audit 负责索引。

`counts()` 是只读统计，供 `wd_doctor` 每小时巡检调用；
`--fix` 是唯一会写 meta.json 的路径，必须在客户端完全退出后执行。

用法：
    python wd_audit.py            # 只读报告
    python wd_audit.py --fix      # 剔除幽灵条目（先备份 meta.json，须关闭客户端）
"""
import argparse
import datetime
import json
import os
import shutil
import sqlite3
import sys

import wd_shadow as S
import wdconfig as C
import wd_fs as F


def app_running():
    """客户端是否在运行 —— 返回拒绝理由，None 表示可以继续。

    meta.json 是客户端的内存态落盘文件，运行中被改动会被覆盖甚至写坏。
    `--fix` 必须在 WorkBuddy 完全退出后执行，否则改完切个账号就回滚，
    看起来「修了但没生效」。

    进程判定复用 `wd_shadow.client_running()`：走 Toolhelp32 快照约 3 ms，
    比自起 `tasklist` 子进程（212 ms、还有 GBK 解码问题）快两个数量级。
    两份实现最大的代价不是慢，而是会分叉。查不到进程表时它返回 None，
    按「宁可拒绝、不默认放行」处理。
    """
    state = S.client_running()
    if state is None:
        return "无法确认客户端是否在运行（进程表不可用）。"
    return "检测到 workbuddy.exe 正在运行。" if state else None


def counts():
    """只读统计。返回 dict；配置或库读不到时返回 None。"""
    if not os.path.exists(C.META) or not os.path.exists(C.DB):
        return None
    meta = C._read_json(C.META, {})
    ac = meta.get("autoCopy") or {}
    idx = ac.get("sessionIndex") or {}
    dup = ac.get("duplicates") or []

    conn = sqlite3.connect("file:%s?mode=ro" % C.DB.replace("\\", "/"), uri=True)
    try:
        exists = {r[0] for r in conn.execute("SELECT id FROM sessions")}
        alive = {r[0] for r in conn.execute(
            "SELECT id FROM sessions WHERE deleted_at IS NULL")}
    finally:
        conn.close()

    per_uid = {}
    for uid, m in idx.items():
        missing = [sid for sid in m if sid not in exists]
        deleted = [sid for sid in m if sid in exists and sid not in alive]
        per_uid[uid] = (missing, deleted)

    return {
        "groups": len(ac.get("sessions") or {}),
        "accounts": len(idx),
        "ghosts": sum(len(a) + len(b) for a, b in per_uid.values()),
        "dup_total": len(dup),
        "dup_stale": len([d for d in dup if d.get("id") not in alive]),
        "per_uid": per_uid,
        "all_sessions": ac.get("allSessions"),
    }


def main():
    ap = argparse.ArgumentParser(description="autoCopy 幽灵索引体检")
    ap.add_argument("--fix", action="store_true", help="剔除幽灵条目")
    a = ap.parse_args()

    if not os.path.exists(C.META):
        print("找不到客户端配置：%s" % C.META)
        return 2
    if not os.path.exists(C.DB):
        print("找不到会话库：%s" % C.DB)
        return 2

    # --fix 一律先过进程检查：不依赖「索引是否健康」这个分支，
    # 应用在运行时永远不允许写 meta.json。
    if a.fix:
        reason = app_running()
        if reason:
            print("拒绝执行：%s" % reason)
            print("请完全退出 WorkBuddy（含右下角托盘图标）后再运行，")
            print("否则写入的 meta.json 会被客户端下次落盘覆盖。")
            return 3

    n = counts()
    meta = C._read_json(C.META, {})
    idx = (meta.get("autoCopy") or {}).get("sessionIndex") or {}

    print("autoCopy：allSessions=%s 血缘组=%d 索引账号=%d"
          % (n["all_sessions"], n["groups"], n["accounts"]))
    for uid, m in idx.items():
        missing, deleted = n["per_uid"].get(uid, ([], []))
        print("  %s 索引 %d 条 | 会话不存在 %d | 已软删 %d | 健康 %d"
              % (uid[:8], len(m), len(missing), len(deleted),
                 len(m) - len(missing) - len(deleted)))
        for sid in (missing + deleted)[:5]:
            print("     幽灵 %s → lineage %s" % (sid[:8], m[sid][:8]))

    print("\nduplicates 中失效副本：%d / %d" % (n["dup_stale"], n["dup_total"]))
    print("幽灵标记合计：%d 条" % n["ghosts"])

    if not n["ghosts"] and not n["dup_stale"]:
        print("\n索引健康，无需处理。")
        return 0
    if not a.fix:
        print("\n只读模式：加 --fix 剔除（会先备份 meta.json，建议关闭应用后执行）")
        return 0

    # 进程检查已在入口处完成（--fix 一进来就判定），这里不再重复。
    C.ensure_dirs()
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = os.path.join(C.BACKUP, "meta-pre-audit-%s.json" % ts)
    shutil.copy2(C.META, bak)
    print("\n备份：%s" % bak)

    alive = set()
    conn = sqlite3.connect("file:%s?mode=ro" % C.DB.replace("\\", "/"), uri=True)
    try:
        alive = {r[0] for r in conn.execute(
            "SELECT id FROM sessions WHERE deleted_at IS NULL")}
    finally:
        conn.close()

    removed = 0
    for uid, m in idx.items():
        for sid in [s for s in m if s not in alive]:
            m.pop(sid, None)
            removed += 1
    ac = meta.get("autoCopy") or {}
    ac["sessionIndex"] = idx
    if n["dup_stale"]:
        ac["duplicates"] = [d for d in (ac.get("duplicates") or [])
                            if d.get("id") in alive]
    meta["autoCopy"] = ac
    # meta.json 是客户端自己的配置，原地覆盖被强杀打断会留下半截 JSON，
    # 客户端下次启动读不动就可能重建索引。故用原子写。
    if not F.atomic_write(C.META, json.dumps(meta, ensure_ascii=False, indent=1)):
        print("写入失败（meta.json 被占用？），未做改动", file=sys.stderr)
        return 1
    print("已剔除幽灵索引 %d 条；还原方式：把 %s 复制回 meta.json" % (removed, bak))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
