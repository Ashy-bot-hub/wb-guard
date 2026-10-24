#!/usr/bin/env python3
"""把 sessions-delete-refresh 补丁应用到本机 WorkDaddy 安装目录。

用法：
    python apply.py [--scripts DIR] [--dry-run] [--revert]

流程：识别版本 -> 幂等检查 -> 校验（不落盘）-> 备份 -> 写入 -> 提示后续步骤。
只改 scripts/inject.js 与 scripts/daemon.js，不碰 workbuddy.db / projects / accounts。

优先使用 git apply（校验更严格）；机器上没有 git 时退化为内置的严格上下文匹配实现，
两条通道的产物逐字节一致，可用 --dry-run 先验证。
"""
import argparse
import datetime as dt
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PATCHES = [
    ("inject-sessions-refresh.patch", "inject.js"),
    ("daemon-delete-cascade.patch", "daemon.js"),
]
TARGET_RELEASE = "1.2.18"
# 官方 tag 的 DAEMON_VERSION 是 1.2.68，但 release 打包时会把它改写成 tag 号，
# 因此安装到本机的实际值是 1.2.18。两个都接受，其余值只警告不阻断。
ACCEPTED_DAEMON_VERSIONS = ("1.2.18", "1.2.68")
# 补丁写入的标记，用于幂等判断。1.2.17 版的旧标记一并识别。
MARKERS = {
    "inject.js": ("还需再点一次「永久删除」才会执行", "[同源副本]"),
    "daemon.js": ("[同源副本级联]",),
}
# 最新版补丁必须具备的标记。缺任一说明本机打的是更早的版本，须先 --revert 再应用，
# 因为新版 hunk 的行号与上下文件已经变了，无法叠在旧补丁之上。
REQUIRED_MARKERS = {
    # 两处都要有：只有 [wbscrash 降噪] 说明是「只过滤、没解绑监听器」的中间版本。
    "inject.js": ("[wbscrash 降噪]", "window.__wbsErrListener"),
    "daemon.js": (),
}


def default_scripts_dir():
    local = os.environ.get("LOCALAPPDATA", "")
    return os.path.join(local, "Programs", "WorkDaddy", "scripts")


def read_daemon_version(scripts_dir):
    path = os.path.join(scripts_dir, "daemon.js")
    if not os.path.isfile(path):
        return "unknown"
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        head = fh.read(60000)
    m = re.search(r"const DAEMON_VERSION = '([^']+)'", head)
    return m.group(1) if m else "unknown"


def read_text(path):
    with open(path, "rb") as fh:
        raw = fh.read()
    return raw.decode("utf-8"), raw


def parse_patch(patch_path):
    """解析单文件 unified diff，返回 (目标相对路径, hunk 列表)。"""
    lines = read_text(patch_path)[0].split("\n")
    target = None
    hunks = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("+++ "):
            target = line[4:].strip().split("\t")[0]
            if target.startswith("b/"):
                target = target[2:]
        m = re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
        if m:
            old_start = int(m.group(1))
            body = []
            i += 1
            while i < len(lines):
                body_line = lines[i]
                if body_line.startswith("@@"):
                    break
                if body_line.startswith("--- ") or body_line.startswith("diff --git "):
                    break
                if body_line == "":
                    # 空行在 diff 里表示“上下文空行”，但补丁文件末尾的空行要丢弃
                    if i == len(lines) - 1:
                        i += 1
                        continue
                    body.append(" ")
                elif body_line[0] in " +-\\":
                    if body_line.startswith("\\ "):  # "\ No newline at end of file"
                        body.append("\\")
                    else:
                        body.append(body_line)
                else:
                    break
                i += 1
            hunks.append({"old_start": old_start, "body": body})
            continue
        i += 1
    if not target:
        raise ValueError("补丁缺少 +++ 目标路径：%s" % patch_path)
    return target, hunks


def _flip(entry):
    """反向应用：把 -行 与 +行 互换，其余保持（上下文与 no-newline 标记行）。"""
    if entry.startswith("+"):
        return "-" + entry[1:]
    if entry.startswith("-"):
        return "+" + entry[1:]
    return entry


def _match_at(lines, start, body):
    """在 lines[start] 处按正向规则逐行比对 hunk；返回消费的行数，不匹配返回 None。"""
    pos = start
    for entry in body:
        tag = entry[0] if entry else " "
        text = entry[1:] if len(entry) > 1 else ""
        if tag == "\\":
            continue
        if tag == "+":
            continue  # 新增行不消费原文
        if pos >= len(lines) or lines[pos] != text:
            return None
        pos += 1
    return pos - start


def apply_hunks(lines, hunks, reverse=False, fuzz=50):
    """严格按上下文匹配（允许 ±fuzz 行偏移），返回新行列表。

    反向应用先把 -/+ 互换，再走同一条正向路径；这样回滚可以逐字节还原原文。
    """
    out = list(lines)
    for hunk in reversed(hunks):
        body = [_flip(e) for e in hunk["body"]] if reverse else hunk["body"]
        hint = max(0, hunk["old_start"] - 1)
        candidates = [hint]
        for delta in range(1, fuzz + 1):
            candidates.extend([hint - delta, hint + delta])
        found = None
        for cand in candidates:
            if cand < 0 or cand > len(out):
                continue
            if _match_at(out, cand, body) is not None:
                found = cand
                break
        if found is None:
            return None
        pos = found
        replacement = []
        for entry in body:
            tag = entry[0] if entry else " "
            text = entry[1:] if len(entry) > 1 else ""
            if tag == "\\":
                continue
            if tag == " ":
                replacement.append(text)
                pos += 1
            elif tag == "-":
                pos += 1
            elif tag == "+":
                replacement.append(text)
        out[found:pos] = replacement
    return out


def git_available():
    try:
        r = subprocess.run(["git", "--version"], capture_output=True, text=True)
        return r.returncode == 0
    except OSError:
        return False


def git_apply(patch_path, root, reverse=False, check_only=False):
    cmd = ["git", "apply", "-p1"]
    if reverse:
        cmd.append("-R")
    if check_only:
        cmd.append("--check")
    cmd.append(patch_path)
    return subprocess.run(cmd, cwd=root, capture_output=True, text=True)


def plan_patch(patch_path, scripts_dir, reverse):
    """不改磁盘，返回 (目标文件绝对路径, 原文, 新文本) 或抛异常。"""
    rel, hunks = parse_patch(patch_path)
    name = os.path.basename(rel)
    target = os.path.join(scripts_dir, name)
    if not os.path.isfile(target):
        raise FileNotFoundError("找不到目标文件：%s" % target)
    text, raw = read_text(target)
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(nl)
    result = apply_hunks(lines, hunks, reverse=reverse)
    if result is None:
        raise ValueError("%s 的上下文与补丁不匹配" % name)
    return target, raw, nl.join(result)


def file_eol(path):
    with open(path, "rb") as fh:
        raw = fh.read(65536)
    return "\r\n" if b"\r\n" in raw else "\n"


def is_patched(scripts):
    for name, marks in MARKERS.items():
        path = os.path.join(scripts, name)
        if not os.path.isfile(path):
            continue
        text = read_text(path)[0]
        if any(mark in text for mark in marks):
            return True
    return False


def is_fully_patched(scripts):
    """补丁加了新内容后，老版本打过的机器不能直接重复应用（hunk 上下文已变），
    必须先用 --revert 回到原版。返回缺失的必需标记列表，空列表表示是最新版。"""
    missing = []
    for name, marks in REQUIRED_MARKERS.items():
        path = os.path.join(scripts, name)
        text = read_text(path)[0] if os.path.isfile(path) else ""
        for mark in marks:
            if mark not in text:
                missing.append("%s: %s" % (name, mark))
    return missing


def latest_backup(scripts):
    """找最近一份同时含全部目标文件的补丁前备份目录；找不到返回 None。"""
    base = os.path.join(os.environ.get("APPDATA", ""), "WorkDaddy")
    if not os.path.isdir(base):
        return None
    names = [os.path.basename(t) for _p, t in PATCHES]
    dirs = []
    for entry in os.listdir(base):
        if not entry.startswith("patches-backup-") or entry.startswith("patches-backup-before-revert-"):
            continue
        full = os.path.join(base, entry)
        if not os.path.isdir(full):
            continue
        if all(os.path.isfile(os.path.join(full, n)) for n in names):
            dirs.append((os.path.getmtime(full), full))
    if not dirs:
        return None
    dirs.sort(reverse=True)
    return dirs[0][1]


def restore_from_backup(scripts):
    """把备份里的原版文件复制回去。当前文件先另存，便于再次尝试。"""
    src_dir = latest_backup(scripts)
    if not src_dir:
        sys.exit("找不到含全部目标文件的 patches-backup-* 目录，无法用备份恢复。")
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    keep = os.path.join(os.environ.get("APPDATA", ""), "WorkDaddy", "patches-before-restore-" + stamp)
    os.makedirs(keep, exist_ok=True)
    print("备份来源：%s" % src_dir)
    for name in [os.path.basename(t) for _p, t in PATCHES]:
        dst = os.path.join(scripts, name)
        if os.path.isfile(dst):
            shutil.copy2(dst, os.path.join(keep, name))
        shutil.copy2(os.path.join(src_dir, name), dst)
        print("已恢复：%s" % name)
    print("当前（打补丁版）文件另存到：%s" % keep)


def main():
    ap = argparse.ArgumentParser(description="应用/回滚 sessions-delete-refresh 补丁")
    ap.add_argument("--scripts", default=default_scripts_dir(), help="WorkDaddy scripts 目录")
    ap.add_argument("--dry-run", action="store_true", help="只校验，不写盘")
    ap.add_argument("--revert", action="store_true", help="反向应用，回滚到原始文件")
    ap.add_argument("--restore-backup", action="store_true",
                    help="用最近一份补丁前备份恢复原版文件（旧版补丁无法反向应用时用）")
    args = ap.parse_args()

    scripts = os.path.abspath(args.scripts)
    root = os.path.dirname(scripts)
    if not os.path.isdir(scripts):
        sys.exit("找不到 WorkDaddy scripts 目录：%s" % scripts)

    if args.restore_backup:
        restore_from_backup(scripts)
        print("\n恢复完成。接着跑：python apply.py --dry-run 与 python apply.py")
        return

    version = read_daemon_version(scripts)
    print("检测到 daemon 版本：%s（补丁目标：WorkDaddy %s，可接受 daemon %s）"
          % (version, TARGET_RELEASE, " / ".join(ACCEPTED_DAEMON_VERSIONS)))
    if version not in ("unknown",) + ACCEPTED_DAEMON_VERSIONS:
        print("警告：版本不在已适配清单内，先跑 --dry-run 确认上下文仍然匹配。")

    patched = is_patched(scripts)
    if patched and not args.revert:
        missing = is_fully_patched(scripts)
        if missing:
            print("检测到本机打的是更早版本的补丁，缺少：%s" % "、".join(missing))
            print("新版 hunk 无法叠在旧补丁之上，反向应用也对不上，先用补丁前备份恢复原版：")
            print("  python apply.py --restore-backup")
            print("  python apply.py --dry-run && python apply.py")
            sys.exit(2)
        print("检测到补丁标记：本机已包含该补丁的最新版本，无需重复应用。")
        return
    if args.revert and not patched:
        print("未检测到补丁标记，可能未打补丁或已被上游覆盖，无需回滚。")
        return

    plans = []
    for patch_name, _ in PATCHES:
        patch_path = os.path.join(HERE, patch_name)
        if not os.path.isfile(patch_path):
            sys.exit("缺补丁文件：%s" % patch_path)
        try:
            plans.append((patch_name, plan_patch(patch_path, scripts, args.revert)))
        except (ValueError, FileNotFoundError) as exc:
            hint = ""
            if args.revert:
                found = latest_backup(scripts)
                if found:
                    hint = ("\n本机打的是更早版本的补丁，反向应用对不上是正常的。\n"
                            "可以把补丁前的原版文件恢复回去再重打：\n"
                            "  python apply.py --restore-backup\n"
                            "会用最新一份备份：%s" % found)
            sys.exit("校验失败（%s）：%s%s" % (patch_name, exc, hint))
        print("内置校验通过：%s" % patch_name)

    if git_available():
        checked = 0
        for patch_name, (target, _raw, _new) in plans:
            # 安装包里 inject.js 是 CRLF、daemon.js 是 LF；git apply 不接受 CRLF 目标上的 LF 补丁，
            # 这类文件只走内置应用器（逐字节保留原行尾）。
            if file_eol(target) != "\n":
                print("跳过 git 交叉校验：%s 为 CRLF 行尾，由内置应用器处理。"
                      % os.path.basename(target))
                continue
            r = git_apply(os.path.join(HERE, patch_name), root, reverse=args.revert, check_only=True)
            if r.returncode != 0:
                sys.exit("git apply 校验失败（%s）：\n%s" % (patch_name, r.stderr or r.stdout))
            checked += 1
        if checked:
            print("git apply 交叉校验通过（%d 个补丁）。" % checked)
    else:
        print("未检测到 git，使用内置应用器。")

    if args.dry_run:
        print("--dry-run 结束，未改动任何文件。")
        return

    if args.revert:
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = os.path.join(os.environ.get("APPDATA", ""), "WorkDaddy", "patches-backup-before-revert-" + stamp)
    else:
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = os.path.join(os.environ.get("APPDATA", ""), "WorkDaddy", "patches-backup-" + stamp)
    os.makedirs(backup, exist_ok=True)
    for _patch_name, (target, _raw, _new) in plans:
        shutil.copy2(target, os.path.join(backup, os.path.basename(target)))
    print("已备份到：%s" % backup)

    for _patch_name, (target, _raw, new_text) in plans:
        tmp = target + ".wbs-tmp"
        with open(tmp, "wb") as fh:
            fh.write(new_text.encode("utf-8"))
        os.replace(tmp, target)
        print("已%s：%s" % ("回滚" if args.revert else "应用", os.path.basename(target)))

    print("\n后续：")
    print("  1) 面板设置里重新注入，或重启 WorkDaddy，使 inject.js 生效；")
    print("  2) daemon.js 的改动需重启 WorkDaddy 守护进程后才生效；")
    print("  3) 回滚：python apply.py --revert，或把 %s 下的两个文件覆盖回 %s" % (backup, scripts))


if __name__ == "__main__":
    main()
