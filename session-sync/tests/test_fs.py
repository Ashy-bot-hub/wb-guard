# -*- coding: utf-8 -*-
"""wd_fs 的遍历口径测试。

这里锁的是「换实现不能换结果」：`os.walk` + `os.stat` 与 `os.scandir` +
`entry.stat` 必须给出同一组字节数与同一份最新 mtime。后者是 `_tree()` 判定
「会话是否还在写」的唯一依据，差一秒就会把活跃会话判成已结束。
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wd_fs as F


def _write(path, data, mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def _reference(root):
    """旧实现：os.walk + os.stat。"""
    size, newest = 0, 0.0
    for base, _dirs, files in os.walk(root):
        for f in files:
            st = os.stat(os.path.join(base, f))
            size += st.st_size
            newest = max(newest, st.st_mtime)
    return size, newest


class TestTreeSize(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="wd-fs-")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_matches_walk_impl_on_nested_tree(self):
        _write(os.path.join(self.root, "a.bin"), b"x" * 100, 1_700_000_000)
        _write(os.path.join(self.root, "sub", "b.bin"), b"y" * 250, 1_700_000_500)
        _write(os.path.join(self.root, "sub", "deep", "c.bin"), b"z" * 7, 1_700_000_900)
        self.assertEqual(F.tree_size(self.root), _reference(self.root))

    def test_newest_mtime_is_the_max_not_the_directory(self):
        _write(os.path.join(self.root, "old.bin"), b"a", 1_600_000_000)
        _write(os.path.join(self.root, "new", "new.bin"), b"b", 1_700_000_000)
        size, newest = F.tree_size(self.root)
        self.assertEqual(size, 2)
        self.assertAlmostEqual(newest, 1_700_000_000, delta=1)

    def test_empty_dir_is_zero(self):
        self.assertEqual(F.tree_size(self.root), (0, 0.0))

    def test_missing_dir_does_not_raise(self):
        self.assertEqual(F.tree_size(os.path.join(self.root, "nope")), (0, 0.0))
        self.assertEqual(F.dir_size(os.path.join(self.root, "nope")), 0)

    def test_dir_size_and_count(self):
        _write(os.path.join(self.root, "a"), b"1" * 10)
        _write(os.path.join(self.root, "b", "c"), b"2" * 20)
        self.assertEqual(F.dir_size(self.root), 30)
        self.assertEqual(F.count_files(self.root), (2, 30))


class TestIterFiles(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="wd-fs-")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_yields_full_paths_and_real_stats(self):
        target = os.path.join(self.root, "x", "y.bin")
        _write(target, b"q" * 42, 1_650_000_000)
        rows = list(F.iter_files(self.root))
        self.assertEqual(len(rows), 1)
        path, size, mtime = rows[0]
        self.assertEqual(os.path.abspath(path), os.path.abspath(target))
        self.assertEqual(size, 42)
        self.assertAlmostEqual(mtime, os.stat(target).st_mtime, places=6)

    def test_does_not_descend_into_files(self):
        _write(os.path.join(self.root, "only.bin"), b"x")
        self.assertEqual(len(list(F.iter_files(self.root))), 1)


class TestCountLines(unittest.TestCase):
    """行数口径必须与 `open(mode='r')` 逐行迭代完全一致。

    `wd_unify` 用它判断「零内容空壳」：少算一行就会把一个有内容的会话当成
    空壳软删。所以这里逐种行尾形态钉死。
    """

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="wd-lines-")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _case(self, payload):
        path = os.path.join(self.root, "t.jsonl")
        with open(path, "wb") as fh:
            fh.write(payload)
        with open(path, encoding="utf-8", errors="ignore") as fh:
            return sum(1 for _ in fh), F.count_lines(path)

    def test_empty_file_is_zero(self):
        self.assertEqual(self._case(b""), (0, 0))

    def test_single_line_with_trailing_newline(self):
        self.assertEqual(self._case(b"{}\n"), (1, 1))

    def test_single_line_without_trailing_newline(self):
        """最后一行没有换行符也要计入 —— 只数 b"\\n" 会数成 0 行。"""
        self.assertEqual(self._case(b"{}"), (1, 1))

    def test_crlf_counts_as_one_line_each(self):
        self.assertEqual(self._case(b"a\r\nb\r\n"), (2, 2))

    def test_crlf_without_trailing_newline(self):
        self.assertEqual(self._case(b"a\r\nb"), (2, 2))

    def test_lone_cr_falls_back_to_text_mode(self):
        """孤立 \\r 是文本模式的换行；二进制只数 \\n 会少算，必须退回文本模式。"""
        self.assertEqual(self._case(b"a\rb\rc"), (3, 3))

    def test_many_lines_across_chunk_boundary(self):
        """跨 1 MB 块边界时不能漏行（这里造 3 MB 数据）。"""
        payload = b"".join(b'{"i":%d}\n' % i for i in range(200000))
        old, new = self._case(payload)
        self.assertEqual(old, new)
        self.assertEqual(old, 200000)

    def test_lone_cr_appearing_only_in_later_chunk(self):
        """\\r 的统计是「遇到才启用」的，第一块没有、第二块才有也必须抓到。

        为省掉每块一次 count(b"\\r")，先用 `b"\\r" in chunk` 探测、只在命中
        之后逐块计数。这个延迟启用一旦写错，就会出现「前面几块的 \\r 没算」
        —— 恰好是最难在真实数据里撞见的形态。
        """
        head = b"a\n" * (F.CHUNK // 2)          # 恰好填满第一块，全 LF
        payload = head + b"b\rc\n"
        expect = F.CHUNK // 2 + 2               # b 与 c 各占一行
        self.assertEqual(self._case(payload), (expect, expect))

    def test_pure_lf_file_never_counts_cr(self):
        """纯 LF 文件不该走 cr 统计分支，结果仍与文本模式一致。"""
        payload = b"x\n" * 1000
        self.assertEqual(self._case(payload), (1000, 1000))


if __name__ == "__main__":
    unittest.main(verbosity=2)
