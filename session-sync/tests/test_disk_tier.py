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

    def _status(self, logs, hot=0, plan=None):
        """plan 是「回收脚本真正会删的字节数」，默认取 logs - hot。

        判定口径已改成跟 wd_janitor 的扫描结果对齐，测试必须显式注入，
        否则会去扫真实日志目录，结果随机器状态漂移。
        """
        if plan is None:
            plan = max(0, logs - hot)
        with mock.patch.object(D, "_log_stats", return_value=(logs, 0, hot)), \
             mock.patch.object(D.J, "scan",
                               return_value=[("/x", plan * 1048576, "r")]):
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

    def test_control_follows_janitor_plan_not_idle_time(self):
        """总量远超预算，但保留策略下能删的很少 → 达标。

        实测踩过：昨天那 165 MB 按「30 分钟没动过」算进可回收，而回收脚本
        按「今天+昨天」保留根本不会动它，于是体检长期报 602 MB 超预算，
        怎么清都清不掉 —— 无法消除的告警等于没有告警。
        """
        self.assertEqual(self._status(2000, hot=10, plan=100), D.OK)

    def test_falls_back_when_plan_unavailable(self):
        with mock.patch.object(D, "_log_stats", return_value=(BUDGET + 100, 0, 100)), \
             mock.patch.object(D.J, "scan", side_effect=OSError("boom")):
            row = D.chk_disk()[0]
        self.assertEqual(row["status"], D.OK, "取不到计划时退回按闲置时间估算")
        self.assertIn("估算", row["detail"])


if __name__ == "__main__":
    unittest.main()
