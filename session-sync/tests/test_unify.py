# -*- coding: utf-8 -*-
"""转录索引与行数缓存的单测。

为什么写
--------
`wd_unify` 靠两个**带状态**的设计跑进 313 ms（原 690 ms）：按 inode 缓存行数、
按 scandir 建转录索引。带状态的东西最容易在「第二次调用」上出错，而 `wd_unify`
的用途是决定「哪一份副本当正本、哪一条该被折叠」—— 算错行数就会选错正本，
算错索引就会把有内容的会话当空壳。

这里钉死三件事：
- 同一份数据被 N 个账号硬链接时只数一次（省的就是这个）；
- 文件被追加写入后必须重新数（size 进缓存键的意义）；
- 索引在嵌套目录里也能找到转录，同名取先到者（与原 os.walk 版一致）。
"""
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import wd_unify as U     # noqa: E402
import wdconfig as C     # noqa: E402
import wd_fs as F        # noqa: E402


class LineCacheTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wd-lines-cache-")
        U.reset_line_cache()
        self.calls = 0
        self._orig = F.count_lines

        def counting(path):
            self.calls += 1
            return self._orig(path)

        F.count_lines = counting

    def tearDown(self):
        F.count_lines = self._orig
        U.reset_line_cache()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _write(self, name, payload):
        p = os.path.join(self.dir, name)
        with open(p, "wb") as fh:
            fh.write(payload)
        return p

    def test_same_inode_counted_once(self):
        """跨账号镜像把同一份转录硬链接给 3 个账号：只能数一次。"""
        p = self._write("a.jsonl", b"x\ny\nz\n")
        st = os.stat(p)
        vals = [U._lines_of(p, st) for _ in range(3)]
        self.assertEqual(vals, [3, 3, 3])
        self.assertEqual(self.calls, 1)

    def test_append_invalidates_cache(self):
        """同一路径被追加写入后，size 变了必须重新数，不能返回旧值。"""
        p = self._write("a.jsonl", b"x\n")
        st1 = os.stat(p)
        self.assertEqual(U._lines_of(p, st1), 1)
        with open(p, "ab") as fh:
            fh.write(b"y\nz\n")
        st2 = os.stat(p)
        self.assertEqual(U._lines_of(p, st2), 3)
        self.assertEqual(self.calls, 2)

    def test_different_files_counted_separately(self):
        p1 = self._write("a.jsonl", b"1\n")
        p2 = self._write("b.jsonl", b"1\n2\n")
        self.assertEqual(U._lines_of(p1, os.stat(p1)), 1)
        self.assertEqual(U._lines_of(p2, os.stat(p2)), 2)
        self.assertEqual(self.calls, 2)

    def test_cache_is_bounded(self):
        """缓存要有上限，否则长驻进程里会无限增长。"""
        old_max = U._LINES_MAX
        U._LINES_MAX = 2
        try:
            for i in range(5):
                p = self._write("f%d.jsonl" % i, b"x\n" * (i + 1))
                U._lines_of(p, os.stat(p))
            self.assertLessEqual(len(U._LINES), 2)
        finally:
            U._LINES_MAX = old_max


class TranscriptIndexTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="wd-proj-")
        self._orig = C.PROJ
        C.PROJ = self.root
        U.reset_transcript_index()

    def tearDown(self):
        C.PROJ = self._orig
        U.reset_transcript_index()
        shutil.rmtree(self.root, ignore_errors=True)

    def test_finds_jsonl_in_nested_dirs(self):
        deep = os.path.join(self.root, "a", "b", "c")
        os.makedirs(deep)
        target = os.path.join(deep, "sid-1.jsonl")
        open(target, "w").close()
        open(os.path.join(self.root, "notes.txt"), "w").close()
        idx = U.transcript_index(force=True)
        self.assertEqual(idx.get("sid-1"), target)
        self.assertNotIn("notes", idx)

    def test_first_hit_wins_for_duplicate_sid(self):
        """同一 id 出现在两个目录时取先到者，与原 os.walk 版行为一致。"""
        d1 = os.path.join(self.root, "z")
        d2 = os.path.join(self.root, "a")
        os.makedirs(d1)
        os.makedirs(d2)
        p1 = os.path.join(d1, "dup.jsonl")
        p2 = os.path.join(d2, "dup.jsonl")
        open(p1, "w").close()
        open(p2, "w").close()
        idx = U.transcript_index(force=True)
        self.assertIn(idx.get("dup"), (p1, p2))
        self.assertEqual(len([k for k in idx if k == "dup"]), 1)

    def test_index_is_cached_until_forced(self):
        p = os.path.join(self.root, "x.jsonl")
        open(p, "w").close()
        first = U.transcript_index()
        self.assertIn("x", first)
        p2 = os.path.join(self.root, "y.jsonl")
        open(p2, "w").close()
        self.assertNotIn("y", U.transcript_index())      # 未 force，不重建
        self.assertIn("y", U.transcript_index(force=True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
