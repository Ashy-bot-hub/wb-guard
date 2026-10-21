# -*- coding: utf-8 -*-
"""数据增量判定的单测。

为什么写
--------
`~/.workbuddy` 里 `changes-detail` / `blobs` / `cache` / `file-tree-manifests` /
`traces` 这五个目录按会话只增不减，没有任何脚本回收。2026-10-19 实测合计
329 MB，其中 `changes-detail` 已有单个会话占 59 MB。

阈值定得宽松是有意的：本机合计 329 MB，上限 2 GB 留 6 倍余量，C 盘还有
376 GB 空闲。目的只在「明显失控时说话」，所以边界必须测准 —— 差一个等号
就会变成天天误报或永远不报。

这里把每条分支和边界值固定下来。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import wdrules as R   # noqa: E402


class TestJudgeGrowth(unittest.TestCase):
    def test_normal_ok(self):
        """本机实测的量级应判达标。"""
        sizes = {"changes-detail": 84, "blobs": 95, "cache": 71,
                 "file-tree-manifests": 47, "traces": 31}
        state, detail = R.judge_growth(sizes)
        self.assertEqual(state, "ok")
        self.assertIn("合计 328 MB", detail)

    def test_total_over_warn(self):
        sizes = {"changes-detail": 1500, "blobs": 600}
        state, _ = R.judge_growth(sizes)
        self.assertEqual(state, "warn")

    def test_total_over_fail(self):
        sizes = {"changes-detail": 2000, "blobs": 1200}
        state, _ = R.judge_growth(sizes)
        self.assertEqual(state, "fail")

    def test_single_dir_over_limit(self):
        """合计没超，但单个目录超 1 GB —— 也是失控信号。"""
        sizes = {"changes-detail": 1100, "blobs": 100}
        state, detail = R.judge_growth(sizes)
        self.assertEqual(state, "warn")
        self.assertIn("changes-detail", detail)

    def test_boundary_exact_warn_is_ok(self):
        """正好等于合计阈值不算超（判定用 > 而非 >=），避免阈值处反复抖动。

        必须用多个目录凑总量：单目录给 2048 会先撞上「单目录 1 GB」那条，
        测不到合计边界。
        """
        state, _ = R.judge_growth({"a": 1024, "b": 1024})
        self.assertEqual(state, "ok")
        state, _ = R.judge_growth({"a": 1024, "b": 1025})
        self.assertEqual(state, "warn")

    def test_boundary_exact_dir_is_ok(self):
        state, _ = R.judge_growth({"blobs": 1024, "cache": 10})
        self.assertEqual(state, "ok")
        state, _ = R.judge_growth({"blobs": 1025, "cache": 10})
        self.assertEqual(state, "warn")

    def test_empty_means_unreadable(self):
        """一个目录都没读到，说明路径不对，应该是告警而不是静默达标。"""
        state, detail = R.judge_growth({})
        self.assertEqual(state, "warn")
        self.assertIn("没读到", detail)

    def test_detail_names_largest_first(self):
        sizes = {"cache": 10, "blobs": 500, "traces": 5}
        _, detail = R.judge_growth(sizes)
        self.assertIn("最大 blobs", detail)

    def test_custom_thresholds(self):
        state, _ = R.judge_growth({"a": 60}, warn_mb=50, fail_mb=90)
        self.assertEqual(state, "warn")
        state, _ = R.judge_growth({"a": 95}, warn_mb=50, fail_mb=90)
        self.assertEqual(state, "fail")


if __name__ == "__main__":
    unittest.main()
