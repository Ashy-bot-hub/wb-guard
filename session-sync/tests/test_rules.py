# -*- coding: utf-8 -*-
"""规则层单测：不建目录、不碰数据库，纯函数直接验边界。

为什么要写
----------
保留/清理规则一旦写错，代价是「删掉了不该删的」或「该删的一直不删」，
两种都要等到明天才发现。这里把边界条件全部固定下来，改阈值时先跑一遍：

    python -m unittest discover -s tests -v

覆盖的坑（都来自本机真实故障）
------------------------------
- 目录大小写/斜杠不一致导致同一任务被当成两条（跨账号重复的根因）。
- `-wal` / `-shm` 边车文件被当成独立快照，留下孤立的 wal。
- 快照 mtime 漂移（同一文件两次读到不同时间），导致「最新」判断随机。
- 全局只留 1 份快照，压缩前想回滚却已经没有回滚点。
- 今天正在写的日志被预算淘汰，排查线索丢失。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import wdrules as R   # noqa: E402


class TestIdentity(unittest.TestCase):
    def test_norm_dir_unifies_slashes_and_case(self):
        a = R.norm_dir("C:/Users/Administrator/WorkBuddy/")
        b = R.norm_dir("c:\\users\\administrator\\workbuddy")
        self.assertEqual(a, b)
        self.assertEqual(a, "c:\\users\\administrator\\workbuddy")

    def test_norm_dir_collapses_double_separator(self):
        self.assertEqual(R.norm_dir("C:\\\\a\\\\\\b"), "c:\\a\\b")

    def test_task_key_none_title_equals_empty(self):
        self.assertEqual(R.task_key("C:/x", None), R.task_key("c:\\x", ""))

    def test_task_key_trims_title(self):
        self.assertEqual(R.task_key("C:/x", "  日报 "), R.task_key("C:/x", "日报"))


class TestBackupUnit(unittest.TestCase):
    def test_sidecars_share_unit(self):
        base = R.backup_unit(r"C:\b\workbuddy-pre-lineage-20261019-181653.db")
        for suf in ("-wal", "-shm"):
            self.assertEqual(
                base, R.backup_unit(r"C:\b\workbuddy-pre-lineage-20261019-181653.db" + suf))

    def test_unit_is_absolute_path(self):
        self.assertTrue(R.backup_unit(r"C:\b\x.db").startswith("C:\\b"))


class TestStamp(unittest.TestCase):
    def test_name_stamp_wins_over_mtime(self):
        p = r"C:\b\workbuddy-pre-lineage-20261019-181653.db"
        self.assertEqual(R.stamp_of(p, fallback=1.0),
                         R.stamp_of(p, fallback=9.9))

    def test_no_stamp_falls_back(self):
        self.assertEqual(R.stamp_of("readme.txt", fallback=12.5), 12.5)

    def test_dir_name_stamp(self):
        a = R.stamp_of("20261019-182758")
        b = R.stamp_of("20261019-182028")
        self.assertGreater(a, b)     # 18:27 晚于 18:20


class TestBackupPlan(unittest.TestCase):
    def units(self):
        return [
            (r"C:\b\lineage\@100", 10, 100),
            (r"C:\b\lineage\@200", 10, 200),
            (r"C:\b\lineage\@300", 10, 300),
            (r"C:\c\@400", 5, 400),
        ]

    def test_keeps_newest_per_dir(self):
        dels, kept = R.plan_backup_deletions(self.units(), keep_per_dir=1, min_global=1)
        self.assertEqual([p for p, _s in dels], [r"C:\b\lineage\@100", r"C:\b\lineage\@200"])
        self.assertIn(r"C:\b\lineage\@300", [p for p, _s in kept])
        self.assertIn(r"C:\c\@400", [p for p, _s in kept])

    def test_min_global_restores_newest_deletable(self):
        # 三个单元分布在不同目录，keep=1 会删掉两个；min_global=2 要求至少留 2 个
        units = [(r"C:\a\@100", 1, 100), (r"C:\b\@200", 1, 200), (r"C:\c\@300", 1, 300)]
        dels, kept = R.plan_backup_deletions(units, keep_per_dir=1, min_global=2)
        self.assertEqual(len(dels) + len(kept), 3)
        self.assertGreaterEqual(len(kept), 2)
        self.assertIn(r"C:\c\@300", [p for p, _s in kept])

    def test_deletions_ordered_oldest_first(self):
        units = [(r"C:\b\@100", 11, 100), (r"C:\b\@200", 22, 200), (r"C:\b\@300", 33, 300)]
        dels, _kept = R.plan_backup_deletions(units, keep_per_dir=1, min_global=1)
        self.assertEqual([p for p, _s in dels], [r"C:\b\@100", r"C:\b\@200"])

    def test_nothing_to_delete_when_few_units(self):
        dels, kept = R.plan_backup_deletions([(r"C:\a\@1", 1, 1)], keep_per_dir=1)
        self.assertEqual(dels, [])
        self.assertEqual(len(kept), 1)


class TestLogBudget(unittest.TestCase):
    def test_under_budget_keeps_everything(self):
        e = [("/l/a", 10, 1, False), ("/l/b", 10, 2, False)]
        self.assertEqual(R.plan_log_evictions(e, 100), [])

    def test_evicts_oldest_first_until_fit(self):
        e = [("/l/old", 50, 1, False), ("/l/new", 50, 2, False)]
        self.assertEqual([p for p, _s in R.plan_log_evictions(e, 50)], ["/l/old"])

    def test_protected_never_evicted(self):
        e = [("/l/today", 100, 1, True), ("/l/yesterday", 100, 2, False)]
        out = R.plan_log_evictions(e, 100)
        self.assertEqual(out, [("/l/yesterday", 100)])

    def test_cannot_meet_budget_returns_partial(self):
        e = [("/l/today", 500, 1, True)]
        self.assertEqual(R.plan_log_evictions(e, 100), [])   # 不删受保护项，也不报错


class TestSandbox(unittest.TestCase):
    def test_old_dir_trashed_regardless_of_idle(self):
        self.assertTrue(R.should_trash_sandbox(mtime=1e9, now_ts=1e9, age_days=1, keep_days=1))

    def test_today_idle_file_trashed(self):
        now = 10_000
        self.assertTrue(R.should_trash_sandbox(now - 31 * 60, now, idle_min=30,
                                               age_days=0, keep_days=1))

    def test_today_hot_file_kept(self):
        now = 10_000
        self.assertFalse(R.should_trash_sandbox(now - 60, now, idle_min=30,
                                                age_days=0, keep_days=1))

    def test_boundary_exactly_idle_is_trashed(self):
        now = 10_000
        self.assertTrue(R.should_trash_sandbox(now - 30 * 60, now, idle_min=30,
                                               age_days=0, keep_days=1))


class TestRollSeal(unittest.TestCase):
    """滚动日志封口判定：序号更高且更晚，才算前一份滚走了。"""

    def test_higher_index_newer_is_sealed(self):
        got = R.roll_seal([("c_001.log", 100.0), ("c_002.log", 200.0)])
        self.assertTrue(got["c_001.log"])
        self.assertFalse(got["c_002.log"])

    def test_lower_index_but_newer_not_sealed(self):
        # 进程重启后序号回到 001，此时 001 在写、005 已停 —— 不能判 001 封口
        got = R.roll_seal([("c_001.log", 300.0), ("c_005.log", 200.0)])
        self.assertFalse(got["c_001.log"])
        self.assertFalse(got["c_005.log"])

    def test_single_file_never_sealed(self):
        got = R.roll_seal([("c_000.log", 100.0)])
        self.assertFalse(got["c_000.log"])

    def test_different_stems_independent(self):
        got = R.roll_seal([("a_001.log", 100.0), ("a_002.log", 200.0),
                           ("b_001.log", 100.0)])
        self.assertTrue(got["a_001.log"])
        self.assertFalse(got["b_001.log"])

    def test_non_rolling_names_ignored(self):
        got = R.roll_seal([("x.mmap3", 1.0), ("sandbox_12.mmap3", 2.0),
                           ("c_001.log", 1.0)])
        self.assertEqual(sorted(got), ["c_001.log"])

    def test_rollover_pattern_matches_three_digits_only(self):
        got = R.roll_seal([("c_01.log", 1.0), ("c_02.log", 2.0)])
        self.assertEqual(got, {})          # 两位序号不参与，避免误伤日期类命名


class TestRollEvictions(unittest.TestCase):
    """封口日志的份数上限：同前缀只留最新 N 份。

    分钟口径管不住份数 —— 实测当天沙箱 318 MB 里 104 MB 是一份早滚走的 000 号。
    """

    def rows(self):
        c = [
            (r"D:\l\c_000.log", 100, 100.0, "c"),
            (r"D:\l\c_001.log", 100, 200.0, "c"),
            (r"D:\l\c_002.log", 100, 300.0, "c"),
        ]
        return c

    def test_keeps_newest_one_per_stem(self):
        out = R.plan_roll_evictions(self.rows(), keep_per_stem=1)
        self.assertEqual([p for p, _s in out], [r"D:\l\c_000.log", r"D:\l\c_001.log"])

    def test_keep_zero_recycles_all(self):
        out = R.plan_roll_evictions(self.rows(), keep_per_stem=0)
        self.assertEqual(len(out), 3)

    def test_oldest_first_order(self):
        out = R.plan_roll_evictions(self.rows(), keep_per_stem=0)
        self.assertEqual([p for p, _s in out],
                         [r"D:\l\c_000.log", r"D:\l\c_001.log", r"D:\l\c_002.log"])

    def test_protected_skipped(self):
        out = R.plan_roll_evictions(self.rows(), keep_per_stem=0,
                                    protected={r"D:\l\c_001.log"})
        self.assertNotIn(r"D:\l\c_001.log", [p for p, _s in out])
        self.assertEqual(len(out), 2)

    def test_stems_independent(self):
        rows = self.rows() + [(r"D:\l\d_000.log", 50, 100.0, "d"),
                              (r"D:\l\d_001.log", 50, 200.0, "d")]
        out = R.plan_roll_evictions(rows, keep_per_stem=1)
        self.assertIn(r"D:\l\d_000.log", [p for p, _s in out])
        self.assertNotIn(r"D:\l\d_001.log", [p for p, _s in out])


class TestActiveVersions(unittest.TestCase):
    """活跃会话内的副本回收：非文档直接回收，文档留最新 N 份。"""

    def test_non_doc_idle_is_recycled(self):
        rows = [("1.d.aaaa1111.sandbox_center_1_000.log",
                 "sandbox_center_1_000.log", 100, 1000.0)]
        out = R.plan_active_versions(rows, now_ts=1000 + 20 * 60, idle_min=10)
        self.assertEqual([n for n, _s in out], ["1.d.aaaa1111.sandbox_center_1_000.log"])

    def test_hot_file_kept(self):
        rows = [("1.d.aaaa1111.main.log", "main.log", 100, 1000.0)]
        out = R.plan_active_versions(rows, now_ts=1000 + 60, idle_min=10)
        self.assertEqual(out, [])

    def test_doc_keeps_newest_n(self):
        rows = [("1.d.h1.a.docx", "a.docx", 10, 1000.0),
                ("2.d.h2.a.docx", "a.docx", 10, 2000.0),
                ("3.d.h3.a.docx", "a.docx", 10, 3000.0)]
        out = R.plan_active_versions(rows, now_ts=3000 + 3600, idle_min=10, keep_docs=2)
        self.assertEqual([n for n, _s in out], ["1.d.h1.a.docx"])

    def test_doc_uppercase_extension_is_doc(self):
        rows = [("1.d.h1.A.PDF", "A.PDF", 10, 1000.0)]
        out = R.plan_active_versions(rows, now_ts=1000 + 3600, idle_min=10, keep_docs=2)
        self.assertEqual(out, [])          # 唯一一份文档，留作回滚点

    def test_no_extension_is_non_doc(self):
        rows = [("1.d.h1.0efa3c6d", "0efa3c6d", 10, 1000.0)]
        out = R.plan_active_versions(rows, now_ts=1000 + 3600, idle_min=10)
        self.assertEqual(len(out), 1)

    def test_non_doc_uses_its_own_idle_threshold(self):
        """非文档副本用更短阈值：客户端每追加一次日志就复制一份，10 分钟永远追不上。"""
        rows = [("1.d.h1.x.log", "x.log", 10, 1000.0)]
        # 闲置 3 分钟：文档阈值 10 分钟下不动，非文档阈值 2 分钟下应回收
        self.assertEqual(R.plan_active_versions(rows, 1000 + 180, idle_min=10), [])
        out = R.plan_active_versions(rows, 1000 + 180, idle_min=10, non_doc_idle_min=2)
        self.assertEqual(len(out), 1)

    def test_output_oldest_first(self):
        rows = [("v2.d.h2.b.log", "b.log", 5, 2000.0),
                ("v1.d.h1.b.log", "b.log", 5, 1000.0)]
        out = R.plan_active_versions(rows, now_ts=9000, idle_min=10)
        self.assertEqual([n for n, _s in out], ["v1.d.h1.b.log", "v2.d.h2.b.log"])


class TestExcludedRoots(unittest.TestCase):
    """备份排除表合并：大小写/斜杠/尾斜杠不算新项，原有顺序不动。"""

    def test_append_new_keeps_order(self):
        new, added = R.merge_excluded_roots([r"C:\a", r"C:\b"], [r"D:\logs"])
        self.assertEqual(new, [r"C:\a", r"C:\b", r"D:\logs"])
        self.assertEqual(added, [r"D:\logs"])

    def test_duplicate_variants_ignored(self):
        existing = [r"C:\Users\Administrator\.workbuddy\logs"]
        for variant in (r"c:\users\administrator\.workbuddy\logs",
                        "C:/Users/Administrator/.workbuddy/logs/",
                        r"C:\\Users\\Administrator\\.workbuddy\\logs"):
            _new, added = R.merge_excluded_roots(existing, [variant])
            self.assertEqual(added, [], variant)

    def test_non_string_entries_dropped(self):
        new, _added = R.merge_excluded_roots(["C:\\a", None, 3], [])
        self.assertEqual(new, ["C:\\a"])

    def test_empty_inputs(self):
        self.assertEqual(R.merge_excluded_roots(None, None), ([], []))
        self.assertEqual(R.merge_excluded_roots([], ["C:\\x"]), (["C:\\x"], ["C:\\x"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
