# -*- coding: utf-8 -*-
"""合并与软删的判定逻辑：错一步就动到真数据。

这三个函数决定「哪条转录是权威」「哪些会话状态是残留」「哪些会话可以软删」。
它们都是纯函数（输入 rows + 统计快照），所以能穷举边界 —— 比在真实库上试
安全得多，也快得多。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wd_unify as U

HOUR = 3600
NOW_MS = 1_700_000_000_000


def row(sid, status="completed", updated=0, activity=None, created=0, title="t"):
    return {"id": sid, "status": status, "updated_at": updated,
            "last_activity_at": activity, "created_at": created,
            "title": title, "cwd": "C:\\x"}


def stat(path=True, lines=0, mtime=0, ino=None):
    name = str(ino[1]) if ino else "x"
    return {"path": ("C:\\t\\%s.jsonl" % name) if path else None,
            "lines": lines, "mtime": mtime, "ino": ino, "size": lines * 10}


class PickCanonicalTest(unittest.TestCase):
    """代表会话的选择顺序：working > 行数 > updated_at > mtime。"""

    def test_working_wins_over_more_lines(self):
        ms = [row("a", "completed", updated=1), row("b", "working", updated=0)]
        st = {"a": stat(lines=999, ino=("d", 1)), "b": stat(lines=1, ino=("d", 2))}
        self.assertEqual(U.pick_canonical(ms, st)["id"], "b")

    def test_more_lines_wins(self):
        ms = [row("a", updated=9), row("b", updated=1)]
        st = {"a": stat(lines=1, ino=("d", 1)), "b": stat(lines=500, ino=("d", 2))}
        self.assertEqual(U.pick_canonical(ms, st)["id"], "b")

    def test_newer_updated_at_breaks_tie(self):
        ms = [row("a", updated=5), row("b", updated=7)]
        st = {"a": stat(lines=10, ino=("d", 1)), "b": stat(lines=10, ino=("d", 2))}
        self.assertEqual(U.pick_canonical(ms, st)["id"], "b")

    def test_mtime_is_last_resort(self):
        ms = [row("a", updated=5), row("b", updated=5)]
        st = {"a": stat(lines=10, mtime=100, ino=("d", 1)),
              "b": stat(lines=10, mtime=200, ino=("d", 2))}
        self.assertEqual(U.pick_canonical(ms, st)["id"], "b")

    def test_missing_transcript_loses_to_present_one(self):
        # lines=-1 表示没有转录；不能因为「没有」就当作 0 行参与竞争以外的事
        ms = [row("a", updated=9), row("b", updated=1)]
        st = {"a": {"path": None, "lines": -1, "mtime": 0, "ino": None, "size": -1},
              "b": stat(lines=1, ino=("d", 2))}
        self.assertEqual(U.pick_canonical(ms, st)["id"], "b")


class FindStaleTest(unittest.TestCase):
    """残留状态判定：三条必须同时满足，任何一条不满足都不能动。"""

    def test_marks_old_working_without_recent_writes(self):
        r = row("a", "working", updated=NOW_MS - 10 * HOUR * 1000)
        st = {"a": stat(mtime=(NOW_MS / 1000) - 10 * HOUR, ino=("d", 1))}
        self.assertEqual(len(U.find_stale([r], st, NOW_MS, hours=6)), 1)

    def test_completed_is_never_touched(self):
        r = row("a", "completed", updated=NOW_MS - 100 * HOUR * 1000)
        st = {"a": stat(mtime=0, ino=("d", 1))}
        self.assertEqual(U.find_stale([r], st, NOW_MS, hours=6), [])

    def test_recent_activity_protects_session(self):
        r = row("a", "working", updated=NOW_MS - 1 * HOUR * 1000)
        st = {"a": stat(mtime=0, ino=("d", 1))}
        self.assertEqual(U.find_stale([r], st, NOW_MS, hours=6), [])

    def test_recent_file_write_protects_session(self):
        # 库里的时间戳会被跨账号同步改写，转录 mtime 才是「真的在写」的证据
        r = row("a", "working", updated=NOW_MS - 10 * HOUR * 1000)
        st = {"a": stat(mtime=(NOW_MS / 1000) - 60, ino=("d", 1))}
        self.assertEqual(U.find_stale([r], st, NOW_MS, hours=6), [])

    def test_missing_transcript_does_not_protect(self):
        r = row("a", "working", updated=NOW_MS - 10 * HOUR * 1000)
        st = {"a": {"path": None, "lines": -1, "mtime": 0, "ino": None, "size": -1}}
        self.assertEqual(len(U.find_stale([r], st, NOW_MS, hours=6)), 1)

    def test_falls_back_to_updated_at_when_activity_missing(self):
        r = row("a", "Pending", updated=NOW_MS - 9 * HOUR * 1000, activity=None)
        st = {"a": stat(mtime=0, ino=("d", 1))}
        self.assertEqual(len(U.find_stale([r], st, NOW_MS, hours=6)), 1)


class FindEmptyShellsTest(unittest.TestCase):
    def test_old_session_without_lines_is_shell(self):
        r = row("a", created=NOW_MS - 48 * HOUR * 1000)
        st = {"a": stat(lines=0, ino=("d", 1))}
        self.assertEqual(len(U.find_empty_shells([r], st, NOW_MS, hours=1)), 1)

    def test_session_with_lines_is_kept(self):
        r = row("a", created=NOW_MS - 48 * HOUR * 1000)
        st = {"a": stat(lines=2, ino=("d", 1))}
        self.assertEqual(U.find_empty_shells([r], st, NOW_MS, hours=1), [])

    def test_fresh_shell_is_kept(self):
        # 刚建的空会话可能正要开始用，不能删
        r = row("a", created=NOW_MS - 10 * 60 * 1000)
        st = {"a": stat(lines=0, ino=("d", 1))}
        self.assertEqual(U.find_empty_shells([r], st, NOW_MS, hours=1), [])

    def test_missing_transcript_counts_as_shell(self):
        r = row("a", created=NOW_MS - 48 * HOUR * 1000)
        st = {"a": {"path": None, "lines": -1, "mtime": 0, "ino": None, "size": -1}}
        self.assertEqual(len(U.find_empty_shells([r], st, NOW_MS, hours=1)), 1)


if __name__ == "__main__":
    unittest.main()
