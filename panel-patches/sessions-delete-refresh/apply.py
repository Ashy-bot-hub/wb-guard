#!/usr/bin/env python3
"""把 sessions-delete-refresh 补丁应用到本机 WorkDaddy 安装目录。

用法：
    py apply.py [--scripts DIR] [--dry-run]

流程：校验版本 -> 备份 -> git apply -p1 --check -> 应用 -> 提示后续步骤。
不修改用户数据（workbuddy.db / projects），只改 scripts/inject.js 与 scripts/daemon.js。
"""
import argparse
import datetime as dt
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PATCHES = ["inject-sessions-refresh.patch", "daemon-delete-cascade.patch"]
EXPECTED_PREFIX = "1.2."  # 1.2.x 基线；其他版本先手工核对


def default_scripts_dir():
    local = os.environ.get("LOCALAPPDATA", "")
    return os.path.join(local, "Programs", "WorkDaddy", "scripts")


def read_version(scripts_dir):
    path = os.path.join(scripts_dir, "daemon.js")
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        head = fh.read(40000)
    m = re.search(r"const DAEMON_VERSION = '([^']+)'", head)
    return m.group(1) if m else "unknown"


def run(cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scripts", default=default_scripts_dir())
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    scripts = os.path.abspath(args.scripts)
    # 补丁头部为 a/scripts/…、b/scripts/…，应用时以安装根目录为 cwd
    root = os.path.dirname(scripts)
    if not os.path.isdir(scripts):
        sys.exit("找不到 WorkDaddy scripts 目录：%s" % scripts)

    version = read_version(scripts)
    print("检测到 WorkDaddy daemon 版本：%s" % version)
    if not version.startswith(EXPECTED_PREFIX):
        print("警告：补丁基线为 1.2.x，当前版本 %s 可能不匹配，先试 --dry-run。" % version)

    for p in PATCHES:
        r = run(["git", "apply", "-p1", "--check", os.path.join(HERE, p)], root)
        if r.returncode != 0:
            sys.exit("补丁校验失败（%s）：\n%s" % (p, r.stderr or r.stdout))
    print("两个补丁均可干净应用。")
    if args.dry_run:
        print("--dry-run 结束，未改动任何文件。")
        return

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = os.path.join(os.environ.get("APPDATA", ""), "WorkDaddy", "patches-backup-" + stamp)
    os.makedirs(backup, exist_ok=True)
    for name in ("inject.js", "daemon.js"):
        shutil.copy2(os.path.join(scripts, name), os.path.join(backup, name))
    print("已备份到：%s" % backup)

    for p in PATCHES:
        r = run(["git", "apply", "-p1", os.path.join(HERE, p)], root)
        if r.returncode != 0:
            sys.exit("应用失败（%s）：\n%s\n回滚：把 %s 覆盖回 %s" % (p, r.stderr or r.stdout, backup, scripts))
        print("已应用：%s" % p)

    print("\n后续：")
    print("  1) 面板设置里重新注入，或重启 WorkDaddy，使 inject.js 生效；")
    print("  2) daemon.js 的改动需重启守护进程后才生效；")
    print("  3) 回滚：把 %s 下的两个文件覆盖回 %s" % (backup, scripts))


if __name__ == "__main__":
    main()
