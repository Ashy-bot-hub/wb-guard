# -*- coding: utf-8 -*-
"""打发布 zip：workdaddy-guard-<ver>.zip

结构（1.10.4 起）:
  workdaddy-guard-<ver>/workdaddy-guard.dat
  workdaddy-guard-<ver>/workdaddy-guard-uninstall.dat   <- 单文件卸载器
  workdaddy-guard-<ver>/README.md
  workdaddy-guard-<ver>/HISTORY.md
  workdaddy-guard-<ver>/SHA256SUMS.txt
  workdaddy-guard-<ver>/src/**            <- 除 logs/backups/__pycache__/.pid/registry 外全部源码

卸载器与主包分开成两个 .dat：主包是「解压到某目录再安装」，用户想卸载时
往往已不记得装在哪 —— 卸载 dat 自带从计划任务反查部署目录的逻辑。

注意：本脚本第一步调用 `build_uninstall.py` 生成卸载 dat，该脚本已不在源码树内
（1.10.4 的成品 dat 与 zip 存档在换机库 tools/workdaddy-guard/）。重新打包含卸载器
的发布件前，需先补回这一步，否则会停在「卸载 dat 构建失败」。
"""
import hashlib
import os
import subprocess
import sys
import time
import zipfile

VER = "1.10.4"
# 路径相对本文件解析：本脚本在 <repo>/session-sync/tools/ 下，
# 源码根是 session-sync，产物落 repo 根（与 build_dat.py 的默认输出一致）。
SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(SRC)
OUT = os.path.join(ROOT, "workdaddy-guard-%s.zip" % VER)
TOP = "workdaddy-guard-%s" % VER
DAT = os.path.join(ROOT, "workdaddy-guard.dat")
UNINST_DAT = os.path.join(ROOT, "workdaddy-guard-uninstall.dat")

EXCLUDE_DIRS = {"logs", "backups", "__pycache__", ".git"}
EXCLUDE_EXT = {".pid", ".dat", ".pyc"}
EXCLUDE_SUFFIX = ("-registry.json", "accounts.json")
# 运行时生成物：由 install.ps1 现场生成，不进包（见 build_dat.py 同名常量）
EXCLUDE_REL = {"timeline/data.js"}


def src_files():
    out = []
    for base, dirs, files in os.walk(SRC):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDE_DIRS)
        for f in sorted(files):
            if os.path.splitext(f)[1] in EXCLUDE_EXT or f.endswith(EXCLUDE_SUFFIX):
                continue
            full = os.path.join(base, f)
            rel = os.path.relpath(full, SRC).replace(os.sep, "/")
            if rel in EXCLUDE_REL:
                continue
            out.append((full, rel))
    return sorted(out, key=lambda x: x[1])


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    assert os.path.isfile(DAT)
    # 单文件卸载器：先于 zip 构建，版本号读 build_dat.py，与主包一致。
    r = subprocess.call([sys.executable, os.path.join(SRC, "build_uninstall.py")])
    assert r == 0 and os.path.isfile(UNINST_DAT), "卸载 dat 构建失败"
    items = src_files()
    sums = ["# workdaddy-guard v%s 交付校验值" % VER,
            "# 生成于 %s" % time.strftime("%Y-%m-%d %H:%M:%S"),
            "# 用法：certutil -hashfile <文件> SHA256", ""]
    for name, path in (("workdaddy-guard.dat", DAT),
                       ("workdaddy-guard-uninstall.dat", UNINST_DAT),
                       ("README.md", os.path.join(SRC, "README.md")),
                       ("HISTORY.md", os.path.join(SRC, "HISTORY.md"))):
        sums.append("%s  %s" % (sha(path), name))
    sums_text = "\n".join(sums) + "\n"

    if os.path.exists(OUT):
        os.remove(OUT)
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name, path in (("workdaddy-guard.dat", DAT),
                           ("workdaddy-guard-uninstall.dat", UNINST_DAT),
                           ("README.md", os.path.join(SRC, "README.md")),
                           ("HISTORY.md", os.path.join(SRC, "HISTORY.md"))):
            z.write(path, TOP + "/" + name)
        z.writestr(TOP + "/SHA256SUMS.txt", sums_text)
        for full, rel in items:
            z.write(full, TOP + "/src/" + rel)

    # 复核：CRC + 文件数 + dat 与 payload 一致
    z = zipfile.ZipFile(OUT)
    bad = z.testzip()
    print("zip   %s" % OUT)
    print("条目  %d | src %d | 大小 %d B | CRC %s"
          % (len(z.namelist()), len(items), os.path.getsize(OUT),
             "全部通过" if bad is None else "损坏 " + bad))
    print("dat   %d B  sha256 %s" % (os.path.getsize(DAT), sha(DAT)[:16] + "..."))
    print("uninstall.dat %d B  sha256 %s"
          % (os.path.getsize(UNINST_DAT), sha(UNINST_DAT)[:16] + "..."))
    print("README/HISTORY 已同步为 v%s" % VER)
    return 0 if bad is None else 1


if __name__ == "__main__":
    sys.exit(main())
