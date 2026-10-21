# -*- coding: utf-8 -*-
"""磁盘自愈：清理可再生的日志与浏览器缓存。

为什么必须自动化
----------------
WorkBuddy 的 `logs/sandbox` 一天写 300 MB 以上，`logs/<日期>/` 单日 270 MB，
且没有任何轮转。2026-10-19 手动清到 470 MB，1.5 小时后回到 660 MB ——
一次性清理没有意义，必须由守护进程周期执行。

清理范围（全部可再生，删掉不影响功能）
--------------------------------------
| 位置 | 规则 |
|---|---|
| `logs/sandbox/<日期>/` | 保留 KEEP_SANDBOX_DAYS 天；当天目录内只删闲置超 IDLE_MIN 分钟的文件；同一前缀的封口日志只留最新 KEEP_SEALED_PER_STEM 份 |
| `logs/<日期>/` | 保留 KEEP_APP_DAYS 天 |
| `logs/*.old.log` | 直接删 |
| `logs/` 总量 | 超 LOG_BUDGET_MB 时按最旧优先淘汰日期目录（今天除外） |
| `logs/<今天>` 内部 | 超 TODAY_BUDGET_MB 时淘汰闲置 TODAY_IDLE_MIN 分钟以上的旧会话日志 |
| `edge-automation` 缓存 | Code Cache / Cache / GPUCache / 组件缓存 / Service Worker 缓存正文 |
| 本包 `logs/*.log` | 超 ROTATE_MB 就重命名为 `.1`，只保留一代 |

安全边界（明确不碰）
--------------------
- 不碰 `Cookies` / `Login Data` / `Web Data` / `Preferences` / `Extensions` / `IndexedDB`
  —— 这些是登录态与扩展，删了自动化要重新登录。
- 不碰 `projects/`（会话转录）。
- 被进程占用的文件删不掉就跳过并计数，不重试、不提权、不强制。
- 每个目录只删列入白名单的相对路径，不做通配递归删除。

用法
----
    python wd_janitor.py            # 只报告可回收多少
    python wd_janitor.py --apply    # 实际删除
"""
import argparse
import datetime
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wdconfig as C
import wd_fs as F
import wdrules as R

KEEP_SANDBOX_DAYS = 1      # 昨天的沙箱日志没有诊断价值
KEEP_APP_DAYS = 2          # 保留今天与昨天
IDLE_MIN = 30              # 沙箱文件 30 分钟没写入视为进程已退出
SEALED_MIN = 3             # 已封口的滚动日志只需 3 分钟：同前缀已有更高序号，
                           # 说明写入方已切到新文件，旧文件不会再被追加
KEEP_SEALED_PER_STEM = 1   # 同前缀封口文件只留最新 1 份：分钟口径管不住份数，
                           # 实测当天沙箱目录 318 MB 里 104 MB 是早滚走的 000 号
ROTATE_MB = 4              # 本包日志单文件上限
LOG_BUDGET_MB = 420        # 客户端日志总量上限（超出则按最旧优先淘汰）
TODAY_BUDGET_MB = 220      # 当日日志目录上限：今天删不掉最活跃的那几个文件，
TODAY_IDLE_MIN = 60        # 只能靠淘汰「同日但已闲置」的旧会话日志把总量压回来

# 浏览器缓存白名单：相对 profile 根的路径，只清这些
EDGE_CACHE_DIRS = [
    "Default/Code Cache",
    "Default/Cache",
    "Default/GPUCache",
    "Default/DawnWebGPUCache",
    "Default/DawnGraphiteCache",
    "Default/Service Worker/CacheStorage",
    "Default/Service Worker/ScriptCache",
    "GrShaderCache",
    "ShaderCache",
    "component_crx_cache",
    "BrowserMetrics",
    "ProvenanceData",        # Edge 视觉搜索模型（.ort），可重新下载
]


def dir_size(path):
    return F.dir_size(path)


def day_dirs(base):
    """按 YYYYMMDD 或 YYYY-MM-DD 命名的日期目录，返回 [(path, date)]。"""
    out = []
    try:
        names = os.listdir(base)
    except OSError:
        return out
    for n in names:
        p = os.path.join(base, n)
        if not os.path.isdir(p):
            continue
        digits = n.replace("-", "")
        if len(digits) != 8 or not digits.isdigit():
            continue
        try:
            d = datetime.date(int(digits[:4]), int(digits[4:6]), int(digits[6:]))
        except ValueError:
            continue
        out.append((p, d))
    return out


def scan(now=None):
    """只读扫描，返回 [(绝对路径, 字节数, 原因)]。"""
    now = now or datetime.datetime.now()
    today = now.date()
    items = []

    # 1) 沙箱日志：整天目录超期直接删；当天目录删闲置文件
    sb = os.path.join(C.APPLOGS, "sandbox")
    for p, d in day_dirs(sb):
        age = (today - d).days
        if age >= KEEP_SANDBOX_DAYS:
            items.append((p, dir_size(p), "沙箱日志 %d 天前，整目录" % age))
        else:
            # 先 stat 全量：封口判定要同时看序号与写入时间
            stats = {}
            for f in os.listdir(p):
                fp = os.path.join(p, f)
                try:
                    stats[f] = os.stat(fp)
                except OSError:
                    continue
            sealed = R.roll_seal((f, st.st_mtime) for f, st in stats.items())
            for f, st in stats.items():
                fp = os.path.join(p, f)
                idle_min, why = IDLE_MIN, "沙箱日志 %d 分钟未写入" % IDLE_MIN
                if sealed.get(f):
                    # 封口判定解决的是峰值而不是总量：写入方每写满一份就滚一份，
                    # 按 30 分钟算会同时滞留 3 份 100 MB 的封口文件（实测 315 MB）。
                    idle_min = SEALED_MIN
                    why = "沙箱日志已封口（存在更高序号），%d 分钟未追加" % SEALED_MIN
                if now.timestamp() - st.st_mtime < idle_min * 60:
                    continue
                items.append((fp, st.st_size, why))

            # 份数上限：同前缀的封口文件只留最新 N 份。
            # 上面的 idle_min 是「分钟」口径，管不住「同时存在几份 100 MB」：
            # 2026-10-19 实测当天沙箱 318 MB，其中 104 MB 是一份早已滚走的
            # 000 号文件 —— 它闲置够久会被删，但和它同批的其它封口文件仍会滞留。
            roll = []
            for f, st in stats.items():
                if not sealed.get(f):
                    continue
                m = R.ROLL_RE.match(f)
                if m:
                    roll.append((os.path.join(p, f), st.st_size, st.st_mtime, m.group("stem")))
            taken = {fp for fp, _s, _w in items}
            for fp, size in R.plan_roll_evictions(roll, KEEP_SEALED_PER_STEM, protected=taken):
                items.append((fp, size, "沙箱封口日志同前缀超出 %d 份"
                              % KEEP_SEALED_PER_STEM))

    # 2) 每日会话日志：超期整目录删
    for p, d in day_dirs(C.APPLOGS):
        age = (today - d).days
        if age >= KEEP_APP_DAYS:
            items.append((p, dir_size(p), "会话日志 %d 天前，整目录" % age))

    # 3) 已轮转归档
    try:
        for n in os.listdir(C.APPLOGS):
            if n.endswith(".old.log"):
                fp = os.path.join(C.APPLOGS, n)
                if os.path.isfile(fp):
                    items.append((fp, os.path.getsize(fp), "已轮转的旧日志"))
    except OSError:
        pass

    # 4) 浏览器缓存（白名单）
    for rel in EDGE_CACHE_DIRS:
        p = os.path.join(C.EDGE_PROFILE, rel.replace("/", os.sep))
        if os.path.isdir(p):
            sz = dir_size(p)
            if sz:
                items.append((p, sz, "浏览器可再生缓存 %s" % rel))

    # 5) 总量预算：前面按天数保留仍可能超额（单日就能写 270 MB），
    #    这里按「最旧优先」继续淘汰，保证总量有硬上限。
    items.extend(budget_evictions(items, now))
    items.extend(today_evictions(items, now))
    return items


def today_evictions(items, now=None):
    """当日目录内部的预算。

    淘汰日期目录那套对今天无效（今天永远不整体删），可今天的目录本身就能到
    288 MB：一个会话的主线程日志 58 MB、转录日志 62 MB，再加上十来个小会话。
    活跃文件动不得，但**白天已经结束的会话**日志没有保留价值 —— 按最旧优先
    淘汰，直到当日目录回到预算内。
    """
    now = now or datetime.datetime.now()
    today_dir = os.path.join(C.APPLOGS, now.strftime("%Y-%m-%d"))
    if not os.path.isdir(today_dir):
        return []
    chosen = {p for p, _s, _w in items}
    rows, total = [], 0
    for f in os.listdir(today_dir):
        fp = os.path.join(today_dir, f)
        try:
            st = os.stat(fp)
        except OSError:
            continue
        if not os.path.isfile(fp):
            continue
        total += st.st_size
        rows.append((fp, st.st_size, st.st_mtime))
    if total <= TODAY_BUDGET_MB * 1048576:
        return []
    cutoff = now.timestamp() - TODAY_IDLE_MIN * 60
    cands = [(fp, s, m) for fp, s, m in rows if fp not in chosen and m <= cutoff]
    cands.sort(key=lambda x: x[2])            # 最旧优先
    out, remain = [], total
    for fp, size, _m in cands:
        if remain <= TODAY_BUDGET_MB * 1048576:
            break
        out.append((fp, size, "当日日志超 %d MB 预算，淘汰闲置 %d 分钟以上的旧会话日志"
                    % (TODAY_BUDGET_MB, TODAY_IDLE_MIN)))
        remain -= size
    return out


def budget_evictions(items, now=None):
    """日志总量超出预算时，追加最旧日期目录作为淘汰项。

    今天（含当天沙箱目录）永不淘汰：客户端正在写，删了会丢当前排查线索。
    规则本身在 wdrules.plan_log_evictions，可单测。
    """
    now = now or datetime.datetime.now()
    today = now.date()
    chosen = {p for p, _s, _w in items}
    total = dir_size(C.APPLOGS)
    planned = sum(s for p, s, _w in items if p.startswith(C.APPLOGS))
    remain = total - planned
    if remain <= LOG_BUDGET_MB * 1048576:
        return []

    cands = []
    for p, d in day_dirs(C.APPLOGS) + day_dirs(os.path.join(C.APPLOGS, "sandbox")):
        if p in chosen or d >= today:
            continue
        cands.append((p, dir_size(p), datetime.datetime(d.year, d.month, d.day).timestamp(), False))
    picked = R.plan_log_evictions(cands, max(0, LOG_BUDGET_MB * 1048576 - remain))
    return [(p, s, "日志总量超 %d MB 预算，按最旧优先淘汰" % LOG_BUDGET_MB) for p, s in picked]


def rotate_own_logs(limit_mb=ROTATE_MB):
    """本包日志超限就滚一代，避免守护自身日志无限增长。"""
    moved = []
    for n in os.listdir(C.LOGS) if os.path.isdir(C.LOGS) else []:
        if not n.endswith(".log") or n.endswith(".1.log"):
            continue
        p = os.path.join(C.LOGS, n)
        try:
            if os.path.getsize(p) <= limit_mb * 1048576:
                continue
        except OSError:
            continue
        dst = p[:-4] + ".1.log"
        try:
            if os.path.exists(dst):
                os.remove(dst)
            shutil.move(p, dst)
            moved.append(n)
        except OSError:
            pass
    return moved


def apply(items):
    """执行删除，返回 (实际回收字节, 失败项)。

    回收量按「删除前后目录实际大小之差」计。不能用 items 里预算的字节：
    被进程占用的目录只删掉一部分时 rmtree 会抛 OSError，若按预算累加就会
    报出根本没释放的数字（2026-10-19 实测：实际释放 335 MB，预算口径只报 10 MB）。
    """
    freed, skipped = 0, []
    for path, _size, _why in items:
        try:
            if os.path.isdir(path):
                before = dir_size(path)
                shutil.rmtree(path)          # 尽力删；占用文件会中断
                freed += before - (dir_size(path) if os.path.isdir(path) else 0)
            else:
                size = os.path.getsize(path)
                os.remove(path)
                freed += size
        except OSError:
            # 中途失败：把已经删掉的部分也计入
            if not os.path.exists(path):
                continue
            # 客户端对当日会话日志是「写时开、写完关」，删除会随机撞上占用；
            # 实测同一文件 3 分钟前删失败、3 分钟后删成功，因此重试一次再判失败。
            time.sleep(1.5)
            try:
                size = os.path.getsize(path)
                os.remove(path)
                freed += size
                continue
            except OSError:
                skipped.append(path)
    return freed, skipped


def main(argv=None):
    ap = argparse.ArgumentParser(description="清理可再生日志与浏览器缓存")
    ap.add_argument("--apply", action="store_true", help="实际删除（默认只报告）")
    ap.add_argument("--json", action="store_true", help="输出 JSON 明细")
    a = ap.parse_args(argv)

    items = scan()
    total = sum(x[1] for x in items)

    if a.json:
        print(json.dumps({"items": [{"path": p, "size": s, "reason": r} for p, s, r in items],
                          "total_bytes": total}, ensure_ascii=False, indent=2))
        return 0

    print("可回收 %.1f MB（%d 项）" % (total / 1048576, len(items)))
    for p, s, why in sorted(items, key=lambda x: -x[1])[:12]:
        print("  %8.1f MB  %-24s %s" % (s / 1048576, why[:24], p.replace("\\", "/")[-70:]))

    if not a.apply:
        print("\n只读模式：加 --apply 执行")
        return 0

    freed, skipped = apply(items)
    rotated = rotate_own_logs()
    print("\n已回收 %.1f MB；占用中跳过 %d 项" % (freed / 1048576, len(skipped)))
    if rotated:
        print("已轮转本包日志：%s" % ", ".join(rotated))
    return 0


if __name__ == "__main__":
    sys.exit(main())
