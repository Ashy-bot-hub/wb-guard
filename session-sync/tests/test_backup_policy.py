# -*- coding: utf-8 -*-
"""备份保留口径统一（第 5 项）的回归测试。

背景：同一批快照文件被三处逻辑轮流判 —— `wd_unify` 收尾「留 3 批」、
`wd_prune_backups.py` 默认「每目录 1 个 / 全局 2 个」、守护每 6 小时调后者。
谁后跑谁说了算，于是保留份数在 1 和 3 之间反复跳，回滚点数量不可预测。

这一项修的是「口径唯一」，所以测试重点不在算法本身（算法在 test_rules 里已
覆盖），而在：常量只有一个来源、三个入口都不再自带默认值、以及 `prune()`
作为唯一实现的行为边界（只读不动手 / 执行后留够 / 坏快照拒绝删）。
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import wdconfig as C          # noqa: E402
import wd_prune_backups as PB  # noqa: E402
import wd_unify as U          # noqa: E402


def make_db(path, ok=True):
    """造一个能过 quick_check 的 db；ok=False 时写一坨非 SQLite 内容。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not ok:
        with open(path, "wb") as f:
            f.write(b"not a sqlite file at all" * 64)
        return path
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()
    return path


class PolicySourceTest(unittest.TestCase):
    """常量与入口：不允许再出现第二份默认值。"""

    def test_constants_exist_and_sane(self):
        self.assertIsInstance(C.BACKUP_KEEP, int)
        self.assertIsInstance(C.BACKUP_MIN_GLOBAL, int)
        self.assertGreaterEqual(C.BACKUP_KEEP, 1, "至少留 1 份才谈得上回滚")
        self.assertGreaterEqual(C.BACKUP_MIN_GLOBAL, 1)
        self.assertLessEqual(C.BACKUP_KEEP, 5, "留太多等于不做回收")

    def test_prune_defaults_follow_config(self):
        """不传参时，prune 的保留数必须直接来自 wdconfig。"""
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(PB, "candidate_roots", return_value=[tmp]):
                r = PB.prune(quiet=True)
        self.assertEqual(r["keep"], C.BACKUP_KEEP)
        self.assertEqual(r["min_global"], C.BACKUP_MIN_GLOBAL)

    def test_explicit_args_override_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(PB, "candidate_roots", return_value=[tmp]):
                r = PB.prune(keep=4, min_global=3, quiet=True)
        self.assertEqual((r["keep"], r["min_global"]), (4, 3))

    def test_unify_no_longer_owns_policy(self):
        """wd_unify 不得再有自己的批次常量与实现。"""
        self.assertFalse(hasattr(U, "KEEP_BATCHES"),
                         "KEEP_BATCHES 已迁到 wdconfig，残留即代表两套口径")
        with open(os.path.join(os.path.dirname(HERE), "wd_unify.py"),
                  encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("def prune_backups(", src,
                         "本地实现已删除，应调用 wd_prune_backups.prune")
        self.assertIn("PB.prune(", src)

    def test_watch_does_not_hardcode_keep(self):
        """守护只负责「到点触发」，保留数由脚本默认值决定。"""
        with open(os.path.join(os.path.dirname(HERE), "wd_watch.py"),
                  encoding="utf-8") as f:
            src = f.read()
        self.assertIn("wd_prune_backups.py", src)
        self.assertNotIn("--keep", src, "守护传死数字就又变成第二套口径")


class PruneBehaviourTest(unittest.TestCase):
    """prune() 的行为边界：只读不动手、执行后留够、坏的拒绝删。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "backups")
        os.makedirs(self.root, exist_ok=True)
        self.logdir = os.path.join(self.tmp.name, "logs")
        os.makedirs(self.logdir, exist_ok=True)
        self._patches = [
            mock.patch.object(PB, "candidate_roots", return_value=[self.root]),
            mock.patch.object(C, "LOGS", self.logdir),
            mock.patch.object(C, "ensure_dirs", lambda: None),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def batches(self, n=3):
        """造 n 个批次：`lineage/<stamp>/` 转录副本 + 同 stamp 的整库快照。

        真实布局就是这样（wd_unify 的 `%Y%m%d-%H%M%S` 命名），两者靠 stamp
        归并成同一个单元，必须同生同灭。
        """
        names = []
        for i in range(n):
            st = "2026100%d-120000" % (i + 1)
            d = os.path.join(self.root, "lineage", st)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "transcript.jsonl"), "w") as f:
                f.write("{}\n")
            make_db(os.path.join(self.root, "lineage",
                                 "workbuddy-pre-lineage-%s.db" % st))
            names.append(d)
        return names

    def test_dry_run_touches_nothing(self):
        dirs = self.batches(3)
        r = PB.prune(apply=False, quiet=True)
        self.assertEqual(len(r["deletions"]), 1, "3 批、每目录留 2 → 只有 1 个可删")
        self.assertGreater(r["deletable_bytes"], 0)
        self.assertFalse(r["applied"])
        for d in dirs:
            self.assertTrue(os.path.isdir(d), "只读模式不得删任何东西")

    def test_apply_keeps_configured_count(self):
        dirs = self.batches(3)
        oldest = dirs[0]
        r = PB.prune(apply=True, quiet=True)
        self.assertTrue(r["applied"])
        self.assertFalse(os.path.exists(oldest), "应删最老的那批")
        left = [d for d in dirs if os.path.isdir(d)]
        self.assertEqual(len(left), C.BACKUP_KEEP)

    def test_min_global_floor_across_dirs(self):
        """两个目录各 1 批，keep=1 也不许把全局删到只剩 1 个。"""
        for sub in ("lineage", "other"):
            make_db(os.path.join(self.root, sub,
                                 "workbuddy-pre-%s-20261001-120000.db" % sub))
        r = PB.prune(keep=1, min_global=2, apply=False, quiet=True)
        self.assertEqual(r["deletions"], [], "全局下限 2 生效时不该有可删项")
        self.assertEqual(len(r["kept"]), 2)

    def test_corrupt_snapshot_is_refused(self):
        """结构损坏的快照不能删 —— 那是需要人看的证据。"""
        for i in range(3):
            st = "2026100%d-120000" % (i + 1)
            make_db(os.path.join(self.root, "lineage",
                                 "workbuddy-pre-lineage-%s.db" % st),
                    ok=(i == 2))          # 只有最新那份是好的
        r = PB.prune(keep=1, min_global=1, apply=False, quiet=True)
        self.assertEqual(len(r["refused"]), 2, "两份坏 db 都应进 refused")
        self.assertEqual(r["deletions"], [], "坏快照不得进待删清单")

    def test_empty_root_is_noop(self):
        r = PB.prune(apply=True, quiet=True)
        self.assertEqual(r["units"], 0)
        self.assertFalse(r["applied"])


if __name__ == "__main__":
    unittest.main()
