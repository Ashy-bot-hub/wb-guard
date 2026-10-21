# -*- coding: utf-8 -*-
"""日志阈值的三档判定。

这条规则的原始实现把「1.5 倍预算」当成了故障线，同时又把「2 倍」算出来
放着不用 —— pyflakes 的 `local variable 'fail' is assigned but never used`
抓到了它。后果是告警区间被压掉一半：420 MB 预算下，630 MB 就报故障，而
文件头的表格写的是 840 MB。文档与代码各说一套，用户只能看到更吓人的那个。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wd_doctor as D

BUDGET = D.LOG_BUDGET
FAIL = BUDGET * 2


class DiskTierTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wd_disk_")
        self.patches = [
            # 备份项不参与本组判定，压到 0
            mock.patch.object(D, "SYNC", self.dir),
            mock.patch.object(D, "_mb", return_value=0.0),
            mock.patch.object(D, "_sealed_lag", return_value=(0, 0)),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def _status(self, logs, hot=0):
        with mock.patch.object(D, "_log_stats", return_value=(logs, 0, hot)):
            return D.chk_disk()[0]["status"]

    def test_under_budget_ok(self):
        self.assertEqual(self._status(BUDGET - 1), D.OK)
        self.assertEqual(self._status(BUDGET), D.OK)

    def test_over_budget_warn(self):
        self.assertEqual(self._status(BUDGET + 1), D.WARN)
        self.assertEqual(self._status(FAIL - 1), D.WARN)

    def test_over_double_fail(self):
        self.assertEqual(self._status(FAIL + 1), D.FAIL)

    def test_hot_bytes_excluded_from_control(self):
        # 仍在写的文件删不掉，算进超标就会永远无法达标
        self.assertEqual(self._status(BUDGET + 100, hot=100), D.OK)

    def test_all_negative_clamped(self):
        # hot > logs 的异常输入不应得出负的 control（会误判达标）
        self.assertEqual(self._status(10, hot=999), D.OK)


if __name__ == "__main__":
    unittest.main()
