# -*- coding: utf-8 -*-
"""写库互斥：守护与手动执行不能同时改库。

为什么值得单独测：两条路径各自都正确，撞在一起才出问题 —— 而「撞在一起」
在单机上是低频事件，靠人碰运气发现不了。这里只测「锁是排他的」和「两个写
入口确实加锁了」，不去真跑 `--apply`（锁一旦失效，那个测试就会改真实数据，
用数据安全换覆盖率不划算）。
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wdconfig as C

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAME = "Local\\WorkDaddy-Write-test-%d" % os.getpid()


class MutexTest(unittest.TestCase):
    def setUp(self):
        # 句柄是裸 int，从字典里 pop 掉不会自动 CloseHandle，内核对象仍在 ——
        # 下一个用例会看到「已被占用」。必须显式关闭。
        import ctypes
        h = C._MUTEXES.pop(NAME, None)
        if h:
            ctypes.windll.kernel32.CloseHandle(h)

    def test_second_acquire_is_refused(self):
        ok1, h1 = C.acquire_mutex(NAME)
        self.assertTrue(ok1)
        ok2, h2 = C.acquire_mutex(NAME)
        self.assertFalse(ok2, "同名互斥体已存在时必须拒绝")
        self.assertIsNone(h2)

    def test_handle_is_kept(self):
        # 句柄被回收 = 锁提前释放，等于没加锁
        C.acquire_mutex(NAME)
        self.assertIn(NAME, C._MUTEXES)


class WritePathGuardedTest(unittest.TestCase):
    """两个写入口必须真的调用 acquire_mutex(WRITE_LOCK)。

    这是契约测试，不是行为测试：删掉那三行不会让任何现有测试失败，但会让
    「守护正在 unify 时用户手动再跑一次」重新变成数据风险。字符串检查足够
    拦住这种回归，且不需要拿真实库去冒险。
    """

    def _src(self, name):
        with open(os.path.join(ROOT, name), encoding="utf-8") as f:
            return f.read()

    def test_unify_guards_apply(self):
        src = self._src("wd_unify.py")
        self.assertRegex(
            src,
            re.compile(r"if apply or undo.*:\s*\n\s*ok, _h = C\.acquire_mutex\(C\.WRITE_LOCK\)"))

    def test_write_lock_name_is_single_source(self):
        # 锁名只允许出现在 wdconfig
        for f in ("wd_unify.py", "wd_watch.py"):
            self.assertNotIn('"Local\\\\WorkDaddy-Write"', self._src(f),
                             "%s 里不要硬编码锁名，用 C.WRITE_LOCK" % f)


if __name__ == "__main__":
    unittest.main()
