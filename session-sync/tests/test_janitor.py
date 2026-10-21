# -*- coding: utf-8 -*-
"""清理脚本的行为测试：只覆盖「删不掉时会怎样」这条路径。

客户端对当日会话日志「写时开、写完关」，删除会随机撞上占用 —— 这是常态，
不是例外。失败后等 1.5 秒重试一次；这条路径一旦抛异常没人收住，整轮清理就停
在循环中间：前面的删除已执行、后面的全部没跑，日志上看像「只清理了一部分」。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import wd_janitor as J   # noqa: E402


class TestApplyRetry(unittest.TestCase):
    def test_locked_file_does_not_crash(self):
        """被占用的文件：不能抛 NameError，只能计入 skipped。"""
        d = tempfile.mkdtemp(prefix="wd-test-")
        fp = os.path.join(d, "locked.log")
        with open(fp, "w", encoding="utf-8") as f:
            f.write("x" * 64)
        handle = open(fp, "r+")           # Windows 下持有句柄会拒绝删除
        try:
            try:
                os.remove(fp)
            except OSError:
                pass                      # 环境允许删除，说明测不了锁定，仍继续
            with mock.patch.object(J.time, "sleep", return_value=None):
                freed, skipped = J.apply([(fp, 64, "测试占用")])
            self.assertEqual(freed, 0)
            self.assertEqual(skipped, [fp])
        finally:
            handle.close()
            try:
                os.remove(fp)
            except OSError:
                pass
            try:
                os.rmdir(d)
            except OSError:
                pass

    def test_missing_path_is_idempotent(self):
        """已经不存在的路径不算失败：重复执行同一份清单不该报错。"""
        d = tempfile.mkdtemp(prefix="wd-test-")
        try:
            freed, skipped = J.apply([(os.path.join(d, "nope.log"), 10, "不存在")])
            self.assertEqual(freed, 0)
            self.assertEqual(skipped, [])
        finally:
            os.rmdir(d)

    def test_normal_delete_counts_bytes(self):
        d = tempfile.mkdtemp(prefix="wd-test-")
        fp = os.path.join(d, "x.log")
        with open(fp, "w", encoding="utf-8") as f:
            f.write("y" * 100)
        try:
            freed, skipped = J.apply([(fp, 100, "正常")])
            self.assertEqual(freed, 100)
            self.assertEqual(skipped, [])
            self.assertFalse(os.path.exists(fp))
        finally:
            if os.path.exists(fp):
                os.remove(fp)
            os.rmdir(d)


class TestThresholds(unittest.TestCase):
    """体检与清理必须同一口径，否则会出现「体检达标但永远删不干净」。"""

    def test_doctor_budget_comes_from_janitor(self):
        import wd_doctor as D
        self.assertEqual(D.LOG_BUDGET, J.LOG_BUDGET_MB)
        self.assertEqual(D.HOT_IDLE_MIN, J.IDLE_MIN)


if __name__ == "__main__":
    unittest.main(verbosity=2)
