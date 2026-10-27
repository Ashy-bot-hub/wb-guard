# -*- coding: utf-8 -*-
"""验证 1.10.4 发布 zip：顶层条目、卸载 dat 提取、SHA256SUMS 复核。"""
import hashlib
import os
import shutil
import sys
import tempfile
import zipfile

_HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.dirname(_HERE)                              # session-sync
REPO = os.path.dirname(SRC)                               # repo 根
ZIP = sys.argv[1] if len(sys.argv) > 1 else os.path.join(REPO, "workdaddy-guard-1.10.4.zip")


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    z = zipfile.ZipFile(ZIP)
    assert z.testzip() is None, "CRC 损坏"
    top = sorted(n for n in z.namelist() if n.count("/") == 1)
    print("顶层条目:")
    for n in top:
        print("  %-45s %d B" % (n, z.getinfo(n).file_size))
    assert "workdaddy-guard-1.10.4/workdaddy-guard-uninstall.dat" in z.namelist(), "缺少卸载 dat"

    tmp = tempfile.mkdtemp(prefix="v1104-")
    z.extractall(tmp)

    # SHA256SUMS 复核
    sums_path = os.path.join(tmp, "workdaddy-guard-1.10.4", "SHA256SUMS.txt")
    ok = bad = 0
    with open(sums_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            digest, name = line.split(None, 1)
            p = os.path.join(tmp, "workdaddy-guard-1.10.4", name)
            if sha(p) == digest:
                ok += 1
            else:
                bad += 1
                print("  校验失败:", name)
    print("SHA256SUMS: %d 通过 / %d 失败" % (ok, bad))

    # src 与源目录逐字节比对（排除运行时产物）
    src_zip = os.path.join(tmp, "workdaddy-guard-1.10.4", "src")
    real = SRC
    skip_dirs = {"logs", "backups", "__pycache__", ".git"}
    skip_ext = {".pid", ".dat", ".pyc"}
    skip_suffix = ("-registry.json", "accounts.json")
    skip_rel = {"timeline/data.js"}
    n = diff = added = 0
    for base, dirs, files in os.walk(real):
        dirs[:] = sorted(d for d in dirs if d not in skip_dirs)
        for f in sorted(files):
            if os.path.splitext(f)[1] in skip_ext or f.endswith(skip_suffix):
                continue
            rel = os.path.relpath(os.path.join(base, f), real).replace(os.sep, "/")
            if rel in skip_rel:
                continue
            p_zip = os.path.join(src_zip, rel)
            n += 1
            # zip 是 1.10.4 当时的快照，源码此后可能新增文件：新增只计数，不算差异
            if not os.path.exists(p_zip):
                added += 1
                continue
            with open(os.path.join(base, f), "rb") as a, open(p_zip, "rb") as b:
                if a.read() != b.read():
                    diff += 1
                    print("  内容不同:", rel)
    print("src 比对: %d 文件 / %d 差异 / %d 仅源码有（zip 快照之后新增）" % (n, diff, added))

    # 两件事分开判：zip 自身是否完整（CRC + SHA256SUMS），以及它与当前源码是否同步。
    integrity_ok = bad == 0
    drift = diff + added
    print("zip 完整性:", "通过（CRC 与 SHA256SUMS 全部一致）" if integrity_ok else "失败")
    print("与当前源码:", "一致" if drift == 0 else "已分叉（差异 %d / 新增 %d）" % (diff, added))
    if added:
        print("提示：%d 个文件在 zip 中不存在，说明源码晚于该快照，重新打包才会包含。" % added)
    shutil.rmtree(tmp, ignore_errors=True)
    print("临时目录已清理")
    return 0 if integrity_ok else 1


if __name__ == "__main__":
    sys.exit(main())
