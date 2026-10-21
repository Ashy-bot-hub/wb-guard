# -*- coding: utf-8 -*-
"""把 session-sync 目录打成单文件 .dat（自解包 + 交互菜单）。

产物 = dat_template.py 的内容 + base64 压缩载荷。运行时：

    python wb-guard.dat            交互菜单（体检 / 清理 / 合并 / 安装 / 卸载）
    python wb-guard.dat --list     列出包内文件
    python wb-guard.dat --extract DIR

用法：
    python tools/build_dat.py                    # 输出到仓库根 wb-guard.dat
    python tools/build_dat.py --out D:\\x.dat
"""
import argparse
import base64
import io
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
ROOT_NAME = "wb-guard"

EXCLUDE_DIRS = {"logs", "backups", "__pycache__", ".git", "tools", "digest"}
EXCLUDE_FILES = {"dat_template.py", "build_dat.py"}
EXCLUDE_EXT = {".pid", ".dat", ".pyc"}
# 机器状态，打进包里会让新机器继承一份指向不存在路径的撤销记录
EXCLUDE_SUFFIX = ("-registry.json", "accounts.json")


def collect():
    out = []
    for base, dirs, files in os.walk(PKG):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for f in files:
            if f in EXCLUDE_FILES or os.path.splitext(f)[1] in EXCLUDE_EXT:
                continue
            if f.endswith(EXCLUDE_SUFFIX):
                continue
            full = os.path.join(base, f)
            rel = os.path.relpath(full, PKG).replace(os.sep, "/")
            out.append((full, ROOT_NAME + "/" + rel))
    return sorted(out, key=lambda x: x[1])


def build_zip(items):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for full, arc in items:
            z.write(full, arc)
    return buf.getvalue()


def b64_wrap(data, width=100):
    s = base64.b64encode(data).decode("ascii")
    return "\n".join(s[i:i + width] for i in range(0, len(s), width))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(os.path.dirname(PKG), ROOT_NAME + ".dat"))
    a = ap.parse_args()

    items = collect()
    raw = build_zip(items)
    payload = b64_wrap(raw)

    with open(os.path.join(HERE, "dat_template.py"), encoding="utf-8") as f:
        tpl = f.read()
    if "__PAYLOAD__" not in tpl or "__VERSION__" not in tpl:
        print("模板缺少占位符")
        return 2

    version = __import__("datetime").date.today().strftime("%Y.%m.%d")
    text = tpl.replace("__VERSION__", version).replace("__PAYLOAD__", payload)
    with open(a.out, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)

    # 自校验：产物能否被解析、载荷能否还原出同样的 zip
    import importlib.machinery
    import importlib.util
    loader = importlib.machinery.SourceFileLoader("dat_check", a.out)
    spec = importlib.util.spec_from_loader("dat_check", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    names = mod.package_names()
    back = mod.payload_bytes()
    same = back == raw
    print("文件数 %d | zip %d B | dat %d B | 还原一致 %s"
          % (len(items), len(raw), os.path.getsize(a.out), same))
    print("输出：%s" % a.out)
    return 0 if same else 3


if __name__ == "__main__":
    sys.exit(main())
