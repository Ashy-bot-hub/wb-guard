# -*- coding: utf-8 -*-
"""定时任务判据的单测。

为什么写
--------
判据只认真正会坏的两件事：某账号没有自有任务、同账号出现重名。不拿条数
比大小 —— 用户加一条任务、本地镜像停用后条数天然不等，`total` 里还混着两层
副本（库里残留的 mirror 副本 + 上游 official-automation-sync 把副本当源又
复制的那些），按条数比必然稳定误报。

判据错的表现不是崩溃，而是「体检永远红着」或「永远绿着」，两种都会让人对
体检失去信任。所以把每条分支和边界钉在这里。

2026-10-20 本机实测（清掉 31 条 mirror 副本后）：四个账号自有各 4 条，其中
两个账号还挂着 PAUSED 行 —— 这正是「条数不等但健康」的样子，必须判达标。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import wdrules as R   # noqa: E402


class TestJudgeAutomations(unittest.TestCase):
    def test_unequal_but_healthy_is_ok(self):
        """条数不等、但有自有任务且无重名 —— 达标。这是停用镜像后的常态。"""
        per = {"账号 B": {"total": 4, "own": 4, "paused": 0, "mirror": 0},
               "账号 A": {"total": 6, "own": 4, "paused": 2, "mirror": 0},
               "账号 C": {"total": 4, "own": 4, "paused": 0, "mirror": 0}}
        state, _ = R.judge_automations(per)
        self.assertEqual(state, "ok")

    def test_account_without_own_is_warn(self):
        """某账号全是 PAUSED / 只剩副本 —— 切过去看到空列表，要报。"""
        per = {"a1": {"total": 4, "own": 4, "paused": 0, "mirror": 0},
               "b2": {"total": 3, "own": 0, "paused": 3, "mirror": 3}}
        state, detail = R.judge_automations(per)
        self.assertEqual(state, "warn")
        self.assertIn("b2", detail)
        self.assertIn("无自有任务", detail)

    def test_same_account_duplicate_names_is_warn(self):
        """同账号同名多条 —— 界面上「同一任务好几份」，正是要清的那类。"""
        per = {"a1": {"total": 9, "own": 4, "paused": 5, "mirror": 5}}
        state, detail = R.judge_automations(per, dup_names=3)
        self.assertEqual(state, "warn")
        self.assertIn("同账号重名 3 组", detail)

    def test_empty_is_fail(self):
        """一条都没有是硬故障，不是告警。"""
        state, detail = R.judge_automations({})
        self.assertEqual(state, "fail")
        self.assertIn("一条都没有", detail)

    def test_mirror_only_account_is_warn(self):
        """只剩 mirror 副本的账号等于没有任务：副本 PAUSED，不会调度。"""
        state, _ = R.judge_automations(
            {"a1": {"total": 7, "own": 0, "paused": 7, "mirror": 7}})
        self.assertEqual(state, "warn")

    def test_legacy_payload_without_own_key(self):
        """旧调用方只给 total —— 回退按 total 判，不能 KeyError。"""
        state, detail = R.judge_automations({"a1": {"total": 5}})
        self.assertEqual(state, "ok")
        self.assertIn("自有5", detail)
        state, _ = R.judge_automations({"a1": {"total": 0}})
        self.assertEqual(state, "warn")


if __name__ == "__main__":
    unittest.main()
