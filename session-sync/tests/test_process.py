# -*- coding: utf-8 -*-
"""进程表取数的口径测试。

进程表决定「哪些 PID 已经死了」，进而决定删不删 `traces/<pid>`。错一次就是
删掉仍在使用的东西，所以这里锁三件事：

1. 快路径失败或返回残缺结果时必须换兜底路径；
2. 两条路径都残缺时返回 (None, None)，调用方据此「不判废」；
3. 5 秒窗口内复用同一份快照，超出窗口才重新取。
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wd_shadow as S


def _fake(n, name="python.exe"):
    pids = set(range(1000, 1000 + n))
    return pids, {name}


class TestProcessTable(unittest.TestCase):
    def setUp(self):
        S.reset_process_cache()
        self._orig = S.PROC_GETTERS
        self.calls = []

    def tearDown(self):
        S.PROC_GETTERS = self._orig
        S.reset_process_cache()

    def _install(self, *results):
        """安装两条取数路径，共用一条结果队列；记录被调用了几次。

        真实实现在 `PROC_GETTERS` 里放两条：Toolhelp32 快照与 tasklist 兜底。
        夹具保持同样的形状，否则「快照残缺时是否换路径」根本测不到。
        """
        queue = list(results)

        def make():
            def getter():
                self.calls.append(1)
                return queue.pop(0) if queue else (None, None)
            return getter

        S.PROC_GETTERS = (make(), make())

    def test_accepts_plausible_snapshot(self):
        self._install(_fake(200))
        pids, names = S._process_table(ttl=0)
        self.assertEqual(len(pids), 200)
        self.assertIn("python.exe", names)
        self.assertEqual(len(self.calls), 1)

    def test_truncated_snapshot_falls_back(self):
        """只看到 3 个进程 = 快照不完整，必须仍去问兜底路径。"""
        self._install(_fake(3), _fake(300, "workbuddy.exe"))
        pids, names = S._process_table(ttl=0)
        self.assertEqual(len(pids), 300)
        self.assertIn("workbuddy.exe", names)
        self.assertEqual(len(self.calls), 2)

    def test_all_paths_truncated_returns_none(self):
        """两条路径都残缺 → (None, None)。调用方会据此拒绝删任何轨迹。"""
        self._install(_fake(2), _fake(5))
        self.assertEqual(S._process_table(ttl=0), (None, None))
        self.assertEqual(len(self.calls), 2)

    def test_failure_is_not_cached(self):
        self._install((None, None), (None, None), _fake(200), _fake(200))
        self.assertEqual(S._process_table(ttl=0), (None, None))
        pids, _n = S._process_table(ttl=0)
        self.assertEqual(len(pids), 200)

    def test_reuses_snapshot_within_ttl(self):
        self._install(_fake(200), _fake(201))
        first, _ = S._process_table(ttl=60)
        second, _ = S._process_table(ttl=60)
        self.assertIs(first, second)
        self.assertEqual(len(self.calls), 1)

    def test_refreshes_after_ttl(self):
        self._install(_fake(200), _fake(201))
        S._process_table(ttl=0)
        time.sleep(0.01)
        pids, _ = S._process_table(ttl=0)
        self.assertEqual(len(pids), 201)
        self.assertEqual(len(self.calls), 2)

    def test_reset_drops_snapshot(self):
        self._install(_fake(200), _fake(201))
        S._process_table(ttl=60)
        S.reset_process_cache()
        pids, _ = S._process_table(ttl=60)
        self.assertEqual(len(pids), 201)

    def test_real_table_sees_this_process(self):
        """真实取数路径（不注入假数据）：必须能看到当前进程的存在。"""
        S.PROC_GETTERS = self._orig
        S.reset_process_cache()
        pids, _names = S._process_table(ttl=0)
        if pids is None:
            self.skipTest("本机取不到进程表")
        self.assertIn(os.getpid(), pids)

    def test_real_client_running_is_tristate(self):
        S.PROC_GETTERS = self._orig
        S.reset_process_cache()
        self.assertIn(S.client_running(), (True, False, None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
