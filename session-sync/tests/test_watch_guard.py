# -*- coding: utf-8 -*-
"""守护自身可靠性的两条底线：单实例、异常不停摆。

为什么值得单测：这两处都只在「出问题的时候」才生效，平时跑一万轮也看不出
差异。等真出问题时它们已经来不及被发现了 —— 双开会表现为 unify 互相覆盖，
崩溃会表现为「昨天还好好的，今天什么都没做」。
"""
import ctypes
import gc
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wd_watch as W


class SingleInstanceTest(unittest.TestCase):
    NAME = "Local\\WorkDaddy-SyncGuard-test-%d" % os.getpid()

    def setUp(self):
        # 句柄是裸 int，置 None 不会 CloseHandle —— 内核对象仍在，下一个用例
        # 会看到「已有实例」。必须显式关闭，否则测试之间互相污染。
        if W._MUTEX:
            ctypes.windll.kernel32.CloseHandle(W._MUTEX)
        W._MUTEX = None
        gc.collect()

    def test_second_instance_is_refused(self):
        self.assertTrue(W.acquire_single_instance(self.NAME), "首次取锁应成功")
        self.assertFalse(W.acquire_single_instance(self.NAME),
                         "同名互斥体已存在时必须拒绝")

    def test_mutex_handle_kept_alive(self):
        # 句柄被 GC 回收会让锁提前失效，等于没加锁
        W.acquire_single_instance(self.NAME)
        self.assertIsNotNone(W._MUTEX)


class PidAliveTest(unittest.TestCase):
    def test_self_is_alive(self):
        self.assertTrue(W.pid_alive(os.getpid()))

    def test_absent_pid_is_dead(self):
        self.assertFalse(W.pid_alive(999999))

    def test_zero_pid_is_dead(self):
        # pid 0 是 System Idle Process，OpenProcess 必然失败
        self.assertFalse(W.pid_alive(0))

    def test_faster_than_tasklist(self):
        # 这条不是性能洁癖：看门狗每分钟查一次，tasklist 每次枚举全机进程约
        # 150 ms。超过 20 ms 说明有人把它改回了枚举式实现。
        import time
        t = time.perf_counter()
        W.pid_alive(os.getpid())
        self.assertLess((time.perf_counter() - t) * 1000, 20)


class ShouldLogTest(unittest.TestCase):
    def test_first_three_logged(self):
        for n in (1, 2, 3):
            self.assertTrue(W.should_log(n))

    def test_middle_suppressed(self):
        for n in (4, 5, 10, 19):
            self.assertFalse(W.should_log(n))

    def test_every_twentieth_logged(self):
        for n in (20, 40, 60):
            self.assertTrue(W.should_log(n))

    def test_density_bound(self):
        # 100 次连续失败最多 8 条（3 + 5），否则日志会被写爆
        hits = sum(1 for n in range(1, 101) if W.should_log(n))
        self.assertLessEqual(hits, 8)


if __name__ == "__main__":
    unittest.main()
