# -*- coding: utf-8 -*-
"""体检会不会「真的响」——逐项注入故障，而不是看一次全绿。

全绿只能证明「现在没事」，不能证明「有事时会报」。这里的每个用例都把某一
项推到故障态，断言它报错，并且报的是**正确的那一档**（没部署 ≠ 部署了但
进程死了，两者处置完全不同）。
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wd_doctor as D


class SyncDirTest(unittest.TestCase):
    """部署目录的推导：双目录布局与本包即部署目录，两种都要对。

    写死「兄弟目录 _session-sync」会让新机器第一次体检就报「守护未部署」——
    而实际正在跑。这类误报会直接摧毁体检的可信度。
    """

    def test_prefers_sibling_session_sync(self):
        base = tempfile.mkdtemp(prefix="wd_sync_")
        pkg = os.path.join(base, "workdaddy-guard")
        os.makedirs(pkg)
        os.makedirs(os.path.join(base, "_session-sync"))
        try:
            self.assertEqual(D._sync_dir(pkg), os.path.join(base, "_session-sync"))
        finally:
            shutil.rmtree(base, ignore_errors=True)

    def test_falls_back_to_package_itself(self):
        base = tempfile.mkdtemp(prefix="wd_sync_")
        pkg = os.path.join(base, "workdaddy-guard")
        os.makedirs(pkg)
        try:
            self.assertEqual(D._sync_dir(pkg), pkg)
        finally:
            shutil.rmtree(base, ignore_errors=True)


class DoctorInjectTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wd_doc_")
        # 默认模拟「已安装」：有 logs/ 才会进入「进程在不在」的判定，
        # 否则会先命中「从未启动」的告警分支。
        os.makedirs(os.path.join(self.dir, "logs"), exist_ok=True)
        self.old = (D.SYNC, D.PROC_FILES)

    def tearDown(self):
        D.SYNC, D.PROC_FILES = self.old
        shutil.rmtree(self.dir, ignore_errors=True)

    def _file(self, name, content=""):
        p = os.path.join(self.dir, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        return p

    # ---- 守护进程 ----
    def test_dead_pid_is_fail(self):
        D.SYNC = self.dir
        D.PROC_FILES = (("guard-watch", ("guard.pid",)),)
        self._file("guard.pid", "999999")
        row = D.chk_procs(live=set())[0]
        self.assertEqual(row["status"], D.FAIL)
        self.assertIn("已不在", row["detail"])

    def test_alive_pid_is_ok(self):
        D.SYNC = self.dir
        D.PROC_FILES = (("guard-watch", ("guard.pid",)),)
        self._file("guard.pid", "999999")
        self.assertEqual(D.chk_procs(live={999999})[0]["status"], D.OK)

    def test_missing_pid_file_is_fail_and_lists_names(self):
        D.SYNC = self.dir
        D.PROC_FILES = (("guard-watch", ("guard.pid", "guard-watch.pid")),)
        row = D.chk_procs(live=None)[0]
        self.assertEqual(row["status"], D.FAIL)
        self.assertIn("guard.pid", row["detail"])
        self.assertIn("guard-watch.pid", row["detail"])

    def test_undeployed_is_warn_not_fail(self):
        # 没部署 ≠ 部署了但死了：前者不该催人去修一个不存在的部署
        D.SYNC = os.path.join(self.dir, "nope")
        self.assertEqual(D.chk_procs(live=None)[0]["status"], D.WARN)

    def test_never_started_is_warn_not_fail(self):
        # 交付包刚解到新机器：目录在、没跑过 install.ps1（无 logs/、无 pid）
        empty = os.path.join(self.dir, "fresh")
        os.makedirs(empty)
        D.SYNC = empty
        self.assertEqual(D.chk_procs(live=None)[0]["status"], D.WARN,
                         "从未启动应报告警，而不是故障")

    def test_legacy_name_still_recognized(self):
        D.SYNC = self.dir
        D.PROC_FILES = (("guard-watch", ("guard.pid", "guard-watch.pid")),)
        self._file("guard-watch.pid", "999999")      # 只有旧名字
        row = D.chk_procs(live={999999})[0]
        self.assertEqual(row["status"], D.OK)
        self.assertIn("guard-watch.pid", row["detail"])

    def test_legacy_timeline_pid_counts_as_started(self):
        # 另一路守护留下的 pid 只当「跑过没跑过」的痕迹，仍要认
        empty = os.path.join(self.dir, "legacy")
        os.makedirs(empty)
        self._file("legacy/guard-timeline.pid", "999999")
        self.assertTrue(D._started(empty))


if __name__ == "__main__":
    unittest.main()
