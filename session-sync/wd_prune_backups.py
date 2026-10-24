# -*- coding: utf-8 -*-
"""备份快照保留：每个目录留最新 N 个可回滚点，全局至少留 M 个。

保留数不是本文件的私事 —— `wd_unify` 与守护的定时回收都调这里的 `prune()`，
三处共用 `wdconfig.BACKUP_KEEP` / `BACKUP_MIN_GLOBAL` 这一份口径。此前各写
各的（unify 留 3 批、这里默认留 1 个），同一批文件被两套规则轮流判，表现为
「刚被清到 1 份、下次 unify 又按 3 份放回去」。

现状（2026-10-19 实测）
----------------------
`backups/` 与 `_session-sync/backups/lineage/` 里堆了同一份数据库在不同时刻
的整库拷贝，合计 153 MB。它们不是配置，是「改表前拍的一张照片」——
改完确认无误后就没有保留价值，但脚本从不回收，于是每跑一次多 20~37 MB。

判定规则（纯函数在 wdrules.plan_backup_deletions，可单测）
----------------------------------------------------------
1. `x.db` / `x.db-wal` / `x.db-shm` 视为一个单元，同生同灭 —— 留半套会让
   下次打开时报 "database disk image is malformed"。
2. 每个目录保留最新 keep_per_dir 个单元（默认 `C.BACKUP_KEEP`，现为 2）。
3. 全局（跨目录）至少保留 min_global 个（默认 `C.BACKUP_MIN_GLOBAL`，现为 2），
   避免只跑一次就把仅有的回滚点删掉。
4. 删除前用 `pragma quick_check` 校验：能打开且结构完整才删。
   校验失败的一律跳过并列出 —— 这种情况说明快照本身已损坏，须人工看一眼，
   不能因为「它反正是坏的」就顺手删掉证据。

用法
----
    python wd_prune_backups.py                 # 只报告
    python wd_prune_backups.py --apply         # 实际删除
    python wd_prune_backups.py --keep 3        # 临时覆盖每目录保留数
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
import wd_fs as F
import wdrules as R

# 额外的备份根：本机在 _session-sync 下也存了一份整库快照
SIBLING = os.path.join(os.path.dirname(C.HERE), "_session-sync", "backups")


def candidate_roots():
    roots = [C.BACKUP]
    extra = os.environ.get("WD_EXTRA_BACKUPS", "")
    for p in [x for x in extra.split(os.pathsep) if x] + [SIBLING]:
        if os.path.isdir(p) and os.path.abspath(p) not in [os.path.abspath(r) for r in roots]:
            roots.append(os.path.abspath(p))
    return roots


def collect_units(roots):
    """扫描快照单元。

    一个单元 = 同一批次时间戳的全部产物，可能是：
      - `<stamp>.db` 及其 `-wal` / `-shm`；
      - 与 db 同批次的 `<stamp>/` 目录（存该批次的转录副本）。
    同一批次必须同生同灭：只留 db 删掉转录目录，回滚时数据是残缺的。
    """
    per_unit = {}
    for root in roots:
        for base, dirs, files in os.walk(root):
            # 只下钻一层：备份根下面的一级目录（如 lineage），
            # 其内部的时间戳目录本身是单元，不再递归进去逐个文件扫。
            entries = list(files) + list(dirs)
            if os.path.abspath(base) != os.path.abspath(root):
                dirs[:] = []
            for name in entries:
                fp = os.path.join(base, name)
                is_snapshot = name.endswith((".db", ".db-wal", ".db-shm")) or os.path.isdir(fp)
                if not is_snapshot:
                    continue                      # 小体积元数据（如 meta-*.json）是审计线索，不回收
                stamp = R.stamp_of(name)
                if not stamp:
                    continue                      # 不是快照产物，不碰
                label = os.path.join(base, "@%d" % int(stamp))
                ent = per_unit.setdefault(label, {"stamp": stamp, "paths": [], "size": 0})
                ent["paths"].append(fp)
                ent["size"] += dir_size(fp) if os.path.isdir(fp) else _file_size(fp)
    return [(label, e["size"], e["stamp"]) for label, e in per_unit.items()]


def _file_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def pretty(label):
    """把 `目录\\@<epoch>` 显示成 `目录\\@2026-10-19 18:27`。"""
    base, _, stamp = label.rpartition("@")
    try:
        return "%s @ %s" % (base, time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(stamp))))
    except (ValueError, OSError):
        return label


def dir_size(path):
    return F.dir_size(path)


def unit_files(unit):
    """单元对应的实际路径列表。单元标签形如 `...\\lineage\\@1759...`。"""
    base, _, stamp = unit.rpartition("@")
    if not base:
        return []
    out = []
    for name in os.listdir(base) if os.path.isdir(base) else []:
        if R.stamp_of(name) == float(stamp):
            out.append(os.path.join(base, name))
    return out


def verify(unit):
    """单元内每个 .db 都能打开且结构完整 → (True, "ok")。"""
    dbs = [p for p in unit_files(unit) if p.endswith(".db")]
    if not dbs:
        return True, "无数据库文件（仅转录副本）"
    for db in dbs:
        try:
            uri = "file:%s?mode=ro" % db.replace("\\", "/")
            conn = sqlite3.connect(uri, uri=True, timeout=5)
            try:
                row = conn.execute("PRAGMA quick_check").fetchone()
            finally:
                conn.close()
        except Exception as e:                   # noqa: BLE001 —— 任何打不开都算失败
            return False, "%s 打不开：%s" % (os.path.basename(db), e)
        if not row or row[0] != "ok":
            return False, "%s 结构异常：%s" % (os.path.basename(db), row[0] if row else "空结果")
    return True, "ok"


def prune(keep=None, min_global=None, apply=False, quiet=False, as_json=False):
    """回收过期快照，返回结果字典。

    这是唯一的实现入口：`wd_prune_backups.py` 命令行、`wd_unify.py` 收尾、
    守护的定时回收都走这里，保留数默认取 `C.BACKUP_KEEP` / `C.BACKUP_MIN_GLOBAL`，
    避免三处各有一套默认值。`keep` / `min_global` 只在显式传参时覆盖。

    quiet=True 时不打印明细（供守护与 unify 调用，只取返回值）。
    """
    keep = max(1, C.BACKUP_KEEP if keep is None else keep)
    min_global = max(1, C.BACKUP_MIN_GLOBAL if min_global is None else min_global)

    roots = candidate_roots()
    units = collect_units(roots)
    if not units:
        if not quiet and not as_json:
            print("未发现数据库快照")
        return {"roots": roots, "units": 0, "total_bytes": 0,
                "deletable_bytes": 0, "deletions": [], "refused": [],
                "kept": [], "applied": False, "freed": 0.0,
                "keep": keep, "min_global": min_global}

    deletions, kept = R.plan_backup_deletions(
        units, keep_per_dir=keep, min_global=min_global)

    verified, refused = [], []
    for path, size in deletions:
        ok, why = verify(path)
        (verified if ok else refused).append((path, size, why))

    total = sum(s for _p, s, _w in verified)
    result = {
        "roots": roots,
        "units": len(units),
        "total_bytes": sum(s for _p, s, _m in units),
        "deletable_bytes": total,
        "deletions": [{"path": p, "size": s} for p, s, _w in verified],
        "refused": [{"path": p, "size": s, "why": w} for p, s, w in refused],
        "kept": [{"path": p, "size": s} for p, s in kept],
        "applied": False,
        "freed": 0.0,
        "keep": keep,
        "min_global": min_global,
    }

    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result

    if not quiet:
        print("快照单元 %d 个，合计 %.1f MB；保留 %d 个（每目录 %d，全局至少 %d）"
              % (len(units), result["total_bytes"] / 1048576, len(kept),
                 keep, min_global))
        for p, s in kept:
            print("  保留  %7.1f MB  %s" % (s / 1048576, pretty(p)))
        for p, s, _w in verified:
            print("  可删  %7.1f MB  %s" % (s / 1048576, pretty(p)))
        for p, s, w in refused:
            print("  跳过  %7.1f MB  %s（%s）" % (s / 1048576, pretty(p), w))

    if not apply:
        if not quiet:
            print("\n只读模式：加 --apply 执行，预计回收 %.1f MB" % (total / 1048576))
        return result

    before_bytes = result["total_bytes"]
    failed = []
    for unit, _size, _w in verified:
        for p in unit_files(unit):
            try:
                if os.path.isdir(p):
                    shutil.rmtree(p)
                else:
                    os.remove(p)
            except OSError as e:
                failed.append("%s：%s" % (p, e))
    # 回收量用「删前总量 - 删后实扫总量」，与 wd_janitor 同一口径：
    # 只按预算累加会报出根本没释放的数字（占用中的文件删不掉但预算已计入）。
    freed = before_bytes - sum(s for _l, s, _m in collect_units(roots))
    result["applied"] = True
    result["freed"] = freed
    result["failed"] = failed

    if not quiet:
        print("\n已回收 %.1f MB%s" % (freed / 1048576,
                                      "；%d 个文件占用中" % len(failed) if failed else ""))
        for line in failed:
            print("  未删除 %s" % line)

    # 留痕：谁在什么时候删了哪些快照，便于事后追溯
    try:
        C.ensure_dirs()
        logp = os.path.join(C.LOGS, "prune-backups.log")
        with open(logp, "a", encoding="utf-8") as f:
            f.write("%s apply keep=%d min-global=%d freed=%.1fMB deleted=%s\n" % (
                time.strftime("%Y-%m-%d %H:%M:%S"), keep, min_global,
                freed / 1048576,
                json.dumps([p for p, _s, _w in verified], ensure_ascii=False)))
    except OSError:
        pass
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description="回收过期数据库快照")
    ap.add_argument("--apply", action="store_true", help="实际删除（默认只报告）")
    ap.add_argument("--keep", type=int, default=None,
                    help="每个目录保留最新几个快照（默认取 wdconfig.BACKUP_KEEP=%d）"
                         % C.BACKUP_KEEP)
    ap.add_argument("--min-global", type=int, default=None,
                    help="全局最少保留几个快照（默认取 wdconfig.BACKUP_MIN_GLOBAL=%d）"
                         % C.BACKUP_MIN_GLOBAL)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    prune(keep=a.keep, min_global=a.min_global, apply=a.apply, as_json=a.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
