# -*- coding: utf-8 -*-
"""孤儿派生文件回收（第 4 项）的回归测试。

判据只有一条「uuid 不在 sessions 表」，风险全在边界上，所以测试重点是把
「哪些不能动」钉死：在册会话、软删会话、刚建还没写库的、名字相近的、
工作目录编码目录、白名单之外的路径。另外验一遍归档可逆（apply → restore）。
"""
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import wdconfig as C      # noqa: E402
import wd_orphans as O    # noqa: E402

LIVE = "11111111-1111-1111-1111-111111111111"
SOFT = "22222222-2222-2222-2222-222222222222"
DEAD = "33333333-3333-3333-3333-333333333333"
LOOKALIKE = "33333333-3333-3333-3333-33333333333x"


def write(path, text="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wb = os.path.join(self.tmp.name, "workbuddy")
        self.proj = os.path.join(self.wb, "projects")
        self.cwd = os.path.join(self.proj, "c-Users-someone-demo")
        os.makedirs(self.cwd, exist_ok=True)
        self.archive = os.path.join(self.tmp.name, "archive")
        self._patches = [
            mock.patch.object(C, "ARCHIVE", self.archive),
            mock.patch.object(C, "WORKBUDDY", self.wb),
            mock.patch.object(C, "PROJ", self.proj),
        ]
        for p in self._patches:
            p.start()
        self.known = {LIVE, SOFT}

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def plant(self, sid, old=True):
        """在 11 个位置各埋一个节点，返回路径列表。"""
        made = [
            write(os.path.join(self.wb, "changes-detail", sid, "0001.json")),
            write(os.path.join(self.wb, "changes-index", sid + ".json")),
            write(os.path.join(self.wb, "file-tree-manifests", sid + ".json")),
            write(os.path.join(self.wb, "file-tree-manifests", sid + ".meta.json")),
            write(os.path.join(self.wb, "artifact-index", sid + ".json")),
            write(os.path.join(self.wb, "workspace", "sessions", sid, "s.json")),
            write(os.path.join(self.wb, "tasks", sid, "t.json")),
            write(os.path.join(self.wb, "file-history", sid, "h.json")),
            write(os.path.join(self.cwd, sid + ".jsonl")),
            write(os.path.join(self.cwd, sid + ".file-rollback.ndjson")),
            write(os.path.join(self.cwd, sid + ".meta.json")),
        ]
        if old:
            past = time.time() - 86400
            for p in made:
                os.utime(p, (past, past))
        return made


class ScanTest(Base):
    def test_orphan_is_found_everywhere(self):
        made = self.plant(DEAD)
        rows = [r for r in O.scan(self.known, self.wb, self.proj,
                                  include_transcripts=True)
                if r["sid"] == DEAD]
        self.assertEqual(len(rows), len(made), "11 个位置都该被找到")
        paths = {r["path"] for r in rows}
        for p in made:
            self.assertTrue(p in paths or os.path.dirname(p) in paths,
                            "漏了：%s" % p)

    def test_live_session_is_untouched(self):
        self.plant(LIVE)
        rows = O.scan(self.known, self.wb, self.proj, include_transcripts=True)
        self.assertEqual([r for r in rows if r["sid"] == LIVE], [])

    def test_soft_deleted_is_not_orphan(self):
        """软删行只是打标记，文件归 wd_softdel 管，这里不许抢。"""
        self.plant(SOFT)
        rows = O.scan(self.known, self.wb, self.proj, include_transcripts=True)
        self.assertEqual([r for r in rows if r["sid"] == SOFT], [])

    def test_lookalike_name_ignored(self):
        write(os.path.join(self.wb, "changes-index", LOOKALIKE + ".json"))
        rows = O.scan(self.known, self.wb, self.proj)
        self.assertEqual(rows, [], "非标准 uuid 不得当成会话 id")

    def test_cwd_directory_itself_ignored(self):
        rows = O.scan(self.known, self.wb, self.proj)
        self.assertEqual(rows, [], "projects/<编码cwd>/ 是工作目录，不是会话")

    def test_fresh_files_ignored(self):
        """客户端先落文件后写库，30 分钟窗口内的「无主」文件不能动。"""
        self.plant(DEAD, old=False)
        rows = O.scan(self.known, self.wb, self.proj, include_transcripts=True)
        self.assertEqual(rows, [], "新鲜文件应被跳过")

    def test_transcript_reported_but_not_actionable(self):
        made = self.plant(DEAD)
        rows = O.scan(self.known, self.wb, self.proj, include_transcripts=False)
        trans = [r for r in rows if r["transcript"]]
        self.assertEqual(len(trans), 1, "孤儿转录只报告")
        self.assertTrue(trans[0]["path"].endswith(".jsonl"))
        self.assertEqual(len(rows) - len(trans), len(made) - 1)

    def test_outside_data_root_not_scanned(self):
        outside = os.path.join(self.tmp.name, "elsewhere", DEAD + ".json")
        write(outside)
        rows = O.scan(self.known, self.wb, self.proj, include_transcripts=True)
        self.assertEqual(rows, [])


class ArchiveTest(Base):
    def test_apply_moves_and_restores(self):
        made = self.plant(DEAD)
        rows = [r for r in O.scan(self.known, self.wb, self.proj,
                                  include_transcripts=True)]
        stamp, dest, moved, size = O.to_archive(rows, self.wb, quiet=True)
        self.assertEqual(moved, len(made))
        self.assertGreater(size, 0)
        for p in made:
            self.assertFalse(os.path.exists(p), "原位应清空：%s" % p)
        man = os.path.join(dest, "manifest.ndjson")
        self.assertTrue(os.path.isfile(man))
        with open(man, encoding="utf-8") as f:
            self.assertEqual(len(f.readlines()), len(made))

        back, skipped = O.restore(stamp, self.wb, quiet=True)
        self.assertEqual(skipped, 0)
        self.assertEqual(back, len(made))
        for p in made:
            self.assertTrue(os.path.exists(p), "应原路放回：%s" % p)

    def test_restore_does_not_overwrite(self):
        p = self.plant(DEAD)[0]
        rows = O.scan(self.known, self.wb, self.proj, include_transcripts=True)
        stamp, _d, _n, _s = O.to_archive(rows, self.wb, quiet=True)
        write(p, "重新生成的内容")          # 归档期间原位又长出同名文件
        _back, skipped = O.restore(stamp, self.wb, quiet=True)
        self.assertEqual(skipped, 1)
        with open(p, encoding="utf-8") as f:
            self.assertEqual(f.read(), "重新生成的内容")

    def test_purge_respects_retention(self):
        rows = O.scan(self.known, self.wb, self.proj, include_transcripts=True)
        stamp, dest, _n, _s = O.to_archive(rows, self.wb, quiet=True)
        self.assertEqual(O.purge(7, quiet=True)[0], 0, "刚归档的不该被清")
        old = time.time() - 30 * 86400
        os.utime(dest, (old, old))
        gone, _freed = O.purge(7, quiet=True)
        self.assertEqual(gone, 1)
        self.assertFalse(os.path.exists(dest))
        self.assertEqual(O.list_archive(), [])

    def test_archive_outside_data_root_refused(self):
        """白名单之外的目标不移动，只在标准误里说明。"""
        outside = write(os.path.join(self.tmp.name, "elsewhere", DEAD + ".json"))
        row = {"kind": "x", "sid": DEAD, "path": outside, "size": 1,
               "mtime": time.time(), "transcript": False}
        _st, _d, moved, _s = O.to_archive([row], self.wb, quiet=True)
        self.assertEqual(moved, 0)
        self.assertTrue(os.path.exists(outside))


class EmptyCwdTest(Base):
    """删掉项目最后一条会话后，projects/<编码cwd>/ 会留空壳目录。"""

    def make(self, name, old_hours=48, child=None):
        d = os.path.join(self.proj, name)
        os.makedirs(d, exist_ok=True)
        if child:
            write(os.path.join(d, child))
        past = time.time() - old_hours * 3600
        os.utime(d, (past, past))
        return d

    def test_empty_and_old_is_listed(self):
        d = self.make("c-Users-demo-old")
        self.assertEqual(O.empty_cwd_dirs(self.proj), [d])

    def test_non_empty_is_kept(self):
        self.make("c-Users-demo-busy", child=DEAD + ".jsonl")
        self.assertEqual(O.empty_cwd_dirs(self.proj), [])

    def test_fresh_empty_is_kept(self):
        """刚建的目录可能是「会话还没落盘」，24 小时内不动。"""
        self.make("c-Users-demo-new", old_hours=0.1)
        self.assertEqual(O.empty_cwd_dirs(self.proj), [])

    def test_non_cwd_prefix_ignored(self):
        self.make("notes-old-empty")
        self.assertEqual(O.empty_cwd_dirs(self.proj), [])

    def test_apply_removes_only_empty(self):
        old = self.make("c-Users-demo-old")
        busy = self.make("c-Users-demo-busy", child="a.json")
        with mock.patch("sys.stdout"):        # main 会打印明细，测试里静音
            O.main(["--prune-empty-cwd", "--apply"])
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(busy), "非空的绝不能被删")


if __name__ == "__main__":
    unittest.main()
