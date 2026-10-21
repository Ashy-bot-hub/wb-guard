# -*- coding: utf-8 -*-
"""原子写的重试行为。

这组测试锁的是「写临时文件 + 替换」在 Windows 上的重试行为：目标文件被别的
进程打开时，MoveFileEx 直接抛 `PermissionError: [WinError 5]`，不是等一会儿
就好。一边用读循环持有产物、一边连跑 20 次写入，不带重试会崩 9 次。所以必须
带重试，失败还得把临时文件收干净。
"""
import os
import shutil
import tempfile
import unittest
from unittest import mock

import wd_fs as F


class AtomicWriteTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "data.js")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_writes_content_and_leaves_no_tmp(self):
        ok = F.atomic_write(self.path, "window.X = 1;\n")
        self.assertTrue(ok)
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "window.X = 1;\n")
        self.assertEqual([f for f in os.listdir(self.dir) if f.endswith(".tmp")], [])

    def test_creates_missing_parent_dir(self):
        deep = os.path.join(self.dir, "a", "b", "c.json")
        self.assertTrue(F.atomic_write(deep, "{}"))
        self.assertTrue(os.path.exists(deep))

    def test_retries_until_reader_releases_target(self):
        """前两次 WinError 5，第三次成功——模拟读端松手。"""
        real = os.replace
        calls = []

        def flaky(src, dst):
            calls.append(dst)
            if len(calls) <= 2:
                raise PermissionError(13, "拒绝访问")
            return real(src, dst)

        with mock.patch.object(F.os, "replace", side_effect=flaky):
            ok = F.atomic_write(self.path, "NEW", retries=8, delay=0)
        self.assertTrue(ok)
        self.assertEqual(len(calls), 3)
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "NEW")
        self.assertEqual([f for f in os.listdir(self.dir) if f.endswith(".tmp")], [])

    def test_gives_up_cleanly_after_retries(self):
        """一直占用：返回 False、不抛异常、旧内容不变、无 tmp 残留。"""
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("OLD")

        def always_busy(src, dst):
            raise PermissionError(13, "拒绝访问")

        with mock.patch.object(F.os, "replace", side_effect=always_busy):
            ok = F.atomic_write(self.path, "NEW", retries=3, delay=0)
        self.assertFalse(ok)
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "OLD")
        self.assertEqual([f for f in os.listdir(self.dir) if f.endswith(".tmp")], [])

    def test_other_oserror_fails_fast(self):
        """非占用类错误（如目标是目录）不该白等重试。"""
        os.makedirs(self.path)
        self.assertFalse(F.atomic_write(self.path, "NEW", retries=5, delay=0))

    def test_retry_count_is_respected(self):
        calls = []

        def busy(src, dst):
            calls.append(1)
            raise PermissionError(13, "拒绝访问")

        with mock.patch.object(F.os, "replace", side_effect=busy):
            F.atomic_write(self.path, "X", retries=4, delay=0)
        self.assertEqual(len(calls), 4)


class ReadJsonWarnTest(unittest.TestCase):
    """坏文件不该被当成「本来就没有」，但也不该抛出中断守护。"""

    def setUp(self):
        import wdconfig as C
        self.C = C
        C._WARNED.clear()
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "lineage-registry.json")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_missing_file_is_silent(self):
        with mock.patch("sys.stderr") as err:
            self.assertEqual(self.C._read_json(self.path, []), [])
        self.assertFalse(err.write.called)

    def test_broken_file_warns_once(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write('{"batches": [')          # 截断的 JSON
        with mock.patch("sys.stderr") as err:
            self.assertEqual(self.C._read_json(self.path, []), [])
            self.assertEqual(self.C._read_json(self.path, []), [])
        self.assertEqual(err.write.call_count, 1)

    def test_valid_file_returns_content(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write('{"batches": []}')
        self.assertEqual(self.C._read_json(self.path, None), {"batches": []})


class CopyAtomicTest(unittest.TestCase):
    """转录还原用的原子复制。

    原实现是 `os.unlink(dst)` 后 `shutil.copy2(src, dst)`：copy 一旦失败
    （磁盘满、被占用），dst 已经没了。这里钉住「失败不动原文件」。
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.src = os.path.join(self.dir, "src.jsonl")
        self.dst = os.path.join(self.dir, "dst.jsonl")
        with open(self.src, "w", encoding="utf-8") as f:
            f.write("new\n")
        with open(self.dst, "w", encoding="utf-8") as f:
            f.write("old\n")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_replaces_target(self):
        self.assertTrue(F.copy_atomic(self.src, self.dst))
        with open(self.dst, encoding="utf-8") as f:
            self.assertEqual(f.read(), "new\n")

    def test_creates_missing_target(self):
        os.remove(self.dst)
        self.assertTrue(F.copy_atomic(self.src, self.dst))
        with open(self.dst, encoding="utf-8") as f:
            self.assertEqual(f.read(), "new\n")

    def test_missing_source_keeps_target_intact(self):
        ok = F.copy_atomic(os.path.join(self.dir, "nope.jsonl"), self.dst)
        self.assertFalse(ok)
        with open(self.dst, encoding="utf-8") as f:
            self.assertEqual(f.read(), "old\n")
        self.assertEqual(sorted(os.listdir(self.dir)), ["dst.jsonl", "src.jsonl"],
                         "失败时不能残留 .tmp")

    def test_failed_replace_keeps_target(self):
        # 模拟 replace 一直失败（目标被长期占用）：dst 必须还是旧内容
        with mock.patch.object(F, "replace_retry", return_value=False):
            ok = F.copy_atomic(self.src, self.dst)
        self.assertFalse(ok)
        with open(self.dst, encoding="utf-8") as f:
            self.assertEqual(f.read(), "old\n")
        self.assertEqual([f for f in os.listdir(self.dir) if f.endswith(".tmp")], [])


class ConcurrentWriteTest(unittest.TestCase):
    """多进程写同一个文件，内容不能交错。

    原实现的临时文件名固定为 `目标 + ".tmp"`：两个进程撞在同一个临时文件上，
    最终替换进去的可能是「A 的前半 + B 的后半」。真实场景存在 —— 守护每 6
    小时写同一份注册表，用户同时手动跑脚本。
    """

    def test_no_interleaved_content(self):
        import subprocess
        import sys
        d = tempfile.mkdtemp()
        target = os.path.join(d, "data.js")
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        code = (
            "import sys; sys.path.insert(0, %r)\n"
            "import wd_fs\n"
            "ch = sys.argv[1]\n"
            "for _ in range(40):\n"
            "    wd_fs.atomic_write(%r, ch * 20000)\n" % (repo, target)
        )
        script = os.path.join(d, "w.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write(code)
        procs = [subprocess.Popen([sys.executable, script, ch],
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
                 for ch in "ABCD"]
        try:
            for p in procs:
                p.wait(timeout=120)
            with open(target, encoding="utf-8") as f:
                content = f.read()
            self.assertEqual(len(content), 20000, "内容长度必须是某一次完整写入")
            self.assertEqual(len(set(content)), 1, "不能出现两个进程交错的内容")
            self.assertEqual([x for x in os.listdir(d) if x.endswith(".tmp")], [],
                             "临时文件必须清理干净")
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
