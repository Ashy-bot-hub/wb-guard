# -*- coding: utf-8 -*-
"""删除守卫：robocopy /MIR 是「把目标目录清成空」的操作，路径错一层就是灾难。

`_purge_dir` 的设计是对的（逐文件删除会被客户端目录监听拖死），代价是它
不可撤销。所以删除前必须有路径判据：目标在 ~/.workbuddy 之内，且不是任何
顶层容器。这组用例把「路径拼错」的几种可能全部走一遍。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wd_shadow as S
import wdconfig as C


class SafeTargetTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="wb_root_")
        self.old = C.WORKBUDDY
        C.WORKBUDDY = self.root

    def tearDown(self):
        C.WORKBUDDY = self.old
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    def p(self, *parts):
        return os.path.join(self.root, *parts)

    def test_root_itself_refused(self):
        self.assertFalse(S._safe_target(self.root))

    def test_top_level_containers_refused(self):
        for rel in ("workspace", "workspace/sessions", "traces", "file-history",
                    "projects", "blobs", "logs", "backups"):
            with self.subTest(rel=rel):
                self.assertFalse(S._safe_target(os.path.join(self.root, rel)))

    def test_leaf_dirs_allowed(self):
        for rel in ("traces/1234",
                    "workspace/sessions/abc/modify_backup",
                    "file-history/abc"):
            with self.subTest(rel=rel):
                self.assertTrue(S._safe_target(os.path.join(self.root, rel)))

    def test_outside_root_refused(self):
        self.assertFalse(S._safe_target(r"C:\Windows"))
        self.assertFalse(S._safe_target(os.path.join(self.root, "..", "elsewhere")))

    def test_relative_escape_refused(self):
        self.assertFalse(S._safe_target(os.path.join(self.root, "workspace", "..", "..")))

    def test_real_remove_refuses_container(self):
        # 行为断言：容器目录即使被误传进来，也不能被清空
        d = os.path.join(self.root, "workspace", "sessions")
        os.makedirs(os.path.join(d, "sid1", "modify_backup"))
        open(os.path.join(d, "sid1", "modify_backup", "f.txt"), "w").close()
        self.assertEqual(S._remove(d), 0)
        self.assertTrue(os.path.isdir(os.path.join(d, "sid1", "modify_backup")))


class ForceBlockedTest(unittest.TestCase):
    """离线清空的放行条件：只有「确定已退出」才允许。

    原实现是 `if running and apply` —— None（进程表读不到）在布尔上下文里是
    假，于是「不确定」被当成「已退出」，这条唯一不可逆的操作照做。这组用例
    把三种取值与两种模式全组合一遍。
    """

    def test_running_blocks(self):
        self.assertEqual(S.force_blocked(True, True), "running")

    def test_unknown_blocks_with_unknown_reason(self):
        self.assertEqual(S.force_blocked(None, True), "unknown")

    def test_exited_allows(self):
        self.assertIsNone(S.force_blocked(False, True))

    def test_dry_run_never_blocks(self):
        for running in (True, False, None):
            with self.subTest(running=running):
                self.assertIsNone(S.force_blocked(running, False))


if __name__ == "__main__":
    unittest.main()
