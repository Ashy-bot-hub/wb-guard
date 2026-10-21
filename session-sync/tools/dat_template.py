#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WB-GUARD 会话治理工具集 —— 单文件交付包

第一次运行会把包内容解到自身所在目录下的 wb-guard\\，然后进入菜单。

    python wb-guard.dat                交互菜单
    python wb-guard.dat --list         列出包内文件
    python wb-guard.dat --extract DIR  只解包
    python wb-guard.dat --extract DIR --refresh   强制覆盖已解包内容
    python wb-guard.dat --doctor       直接跑体检
    python wb-guard.dat --install      直接注册守护
    python wb-guard.dat --uninstall    停用守护

双击 wb-guard.bat 也行：它会自己找 Python，找不到会明说。
"""
import base64
import io
import os
import subprocess
import sys
import zipfile

VERSION = "__VERSION__"

PAYLOAD = """
__PAYLOAD__
"""

ROOT_NAME = "wb-guard"


def payload_bytes():
    return base64.b64decode("".join(PAYLOAD.split()))


def package_names():
    with zipfile.ZipFile(io.BytesIO(payload_bytes())) as z:
        bad = z.testzip()
        if bad:
            raise SystemExit("包内文件校验失败：%s（文件可能损坏，请重新打包）" % bad)
        return z.namelist()


def unpack(dest):
    data = payload_bytes()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        bad = z.testzip()
        if bad:
            raise SystemExit("包内文件校验失败：%s（文件可能损坏，请重新打包）" % bad)
        names = z.namelist()
        os.makedirs(dest, exist_ok=True)
        z.extractall(dest)
    return os.path.join(dest, ROOT_NAME), len(names)


def default_dest():
    return os.path.dirname(os.path.abspath(__file__))


def is_unpacked(root):
    return (os.path.isdir(root)
            and os.path.exists(os.path.join(root, "wdconfig.py"))
            and os.path.exists(os.path.join(root, "install.ps1")))


def ensure_root(dest, force=False):
    """返回可用根目录：已解过就直接复用，避免嵌套成 wb-guard\\wb-guard。"""
    root = os.path.join(dest, ROOT_NAME)
    if is_unpacked(root) and not force:
        return root, 0
    return unpack(dest)


def _python():
    """子进程用的解释器。pythonw 启动时换成同目录 python.exe，否则没有输出。"""
    exe = sys.executable or "python"
    if os.path.basename(exe).lower() == "pythonw.exe":
        alt = os.path.join(os.path.dirname(exe), "python.exe")
        if os.path.exists(alt):
            return alt
    return exe


def run_py(root, script, *args):
    path = os.path.join(root, script)
    if not os.path.exists(path):
        print("找不到脚本：%s" % path)
        return 127
    cmd = [_python(), path] + list(args)
    try:
        return subprocess.call(cmd, cwd=root)
    except OSError as e:
        print("无法启动 Python：%r" % e)
        return 127


def run_ps(root, script, *args):
    path = os.path.join(root, script)
    if not os.path.exists(path):
        print("找不到脚本：%s" % path)
        return 127
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path]
    cmd.extend(args)
    try:
        return subprocess.call(cmd, cwd=root)
    except OSError as e:
        print("无法启动 powershell：%r" % e)
        return 127


def ask(text):
    try:
        return input(text).strip().lower()
    except EOFError:
        return ""


def pause():
    ask("\n回车返回菜单…")


def preview_then_apply(root, script, apply_args, extra_args=()):
    """先预演一次，问过再带 --apply 执行。"""
    run_py(root, script, *extra_args)
    if ask("\n以上为预演。执行吗？(y/N) ") == "y":
        print()
        run_py(root, script, *(list(extra_args) + list(apply_args)))
    else:
        print("已跳过，未做任何改动。")
    pause()


MENU = """
==================================================================
 WB-GUARD 会话治理工具集 {ver}
 解包位置：{root}
 Python  ：{py}
 守护    ：{guard}
==================================================================
  1  体检（12 项，含复制索引残留）
  2  磁盘清理：日志与浏览器缓存
  3  备份快照回收（每目录留最新 1 个）
  4  转录存储层合并（内容可覆盖时共享 inode）
  5  附属数据回收：影子副本 / 轨迹 / 编辑器历史
  6  侧边栏工作空间登记清理
  7  安装 / 修复守护（注册计划任务）
  8  卸载守护（停任务，不动数据）
  9  清幽灵索引（须先完全退出 WorkBuddy）
  0  退出
=================================================================="""


def guard_state(root):
    for name in ("guard.pid", "guard-watch.pid"):
        p = os.path.join(root, name)
        if os.path.exists(p):
            try:
                pid = open(p, encoding="utf-8").read().strip()
            except OSError:
                pid = ""
            return "本目录已登记 PID %s" % (pid or "?")
    return "本目录未装（菜单 7 可装；此时体检『守护进程』一项不达标属正常）"


def menu(root):
    while True:
        print(MENU.format(ver=VERSION, root=root, py=_python(), guard=guard_state(root)))
        c = ask("\n选择 [0-9]: ")
        if c in ("", "0"):
            print("已退出。")
            return 0

        if c == "1":
            run_py(root, "wd_doctor.py")
            pause()
        elif c == "2":
            preview_then_apply(root, "wd_janitor.py", ["--apply"])
        elif c == "3":
            preview_then_apply(root, "wd_prune_backups.py", ["--apply"])
        elif c == "4":
            preview_then_apply(root, "wd_unify.py", ["--apply"])
        elif c == "5":
            preview_then_apply(root, "wd_shadow.py", ["--apply"])
        elif c == "6":
            preview_then_apply(root, "wd_workspaces.py", ["--apply"])
        elif c == "7":
            print("\n注意：计划任务会指向本目录 %s。" % root)
            print("若本机已从别处装过，这一步会把任务改指到这里，旧目录的守护随之失效。")
            if ask("继续安装？(y/N) ") == "y":
                rc = run_ps(root, "install.ps1")
                print("退出码：%s" % rc)
            else:
                print("已取消。")
            pause()
        elif c == "8":
            if ask("确认停用守护？数据与转录都不会动。(y/N) ") == "y":
                rc = run_ps(root, "uninstall.ps1")
                print("退出码：%s" % rc)
            else:
                print("已取消。")
            pause()
        elif c == "9":
            print("\n这一步要求 WorkBuddy 已完全退出（托盘也退），否则会被拒绝。")
            if ask("执行吗？(y/N) ") == "y":
                rc = run_py(root, "wd_audit.py", "--fix")
                print("退出码：%s" % rc)
            else:
                print("已取消。")
            pause()
        else:
            print("没有这个选项。")


def main(argv):
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0

    if argv and argv[0] == "--list":
        names = package_names()
        print("包内 %d 个文件：" % len(names))
        for n in sorted(names):
            print("  " + n)
        return 0

    if argv and argv[0] == "--extract":
        dest = argv[1] if len(argv) > 1 else default_dest()
        root, n = ensure_root(dest, force="--refresh" in argv)
        print(("已解包 %d 个文件 → %s" % (n, root)) if n else ("复用已有目录 → %s" % root))
        return 0

    print("WB-GUARD %s" % VERSION)
    root, n = ensure_root(default_dest(), force=("--refresh" in argv))
    print(("已解包 %d 个文件 → %s" % (n, root)) if n else ("复用已有目录 → %s" % root))

    if argv:
        a = argv[0]
        if a == "--doctor":
            return run_py(root, "wd_doctor.py")
        if a == "--install":
            return run_ps(root, "install.ps1")
        if a == "--uninstall":
            return run_ps(root, "uninstall.ps1")
        print("未知参数：%s" % a)
        print(__doc__)
        return 2

    return menu(root)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
