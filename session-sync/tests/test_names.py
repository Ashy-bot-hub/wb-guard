# -*- coding: utf-8 -*-
"""可观测面文件名必须只有一个来源（wdconfig）。

为什么用测试钉住：这类分叉不会报错，只会让「体检说故障、其实在跑」或
「按文档装了第二个守护」。根因都一样 —— 同一个名字写进了两个文件，改一处
就漏一处。注释里写「请保持同源」没人会看，测试会拦。
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wdconfig as C

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# wdconfig 是唯一定义处，其它脚本一律不得硬编码这些名字。
ALLOW = {"wdconfig.py"}

PATTERN = re.compile(
    r'"(guard\.pid|guard-watch\.pid|guard-timeline\.pid|guard\.log)"')


class ObservableNameTest(unittest.TestCase):
    def test_no_hardcoded_names_outside_config(self):
        bad = []
        for f in sorted(os.listdir(ROOT)):
            if not f.endswith(".py") or f in ALLOW:
                continue
            with open(os.path.join(ROOT, f), encoding="utf-8") as fh:
                for m in PATTERN.finditer(fh.read()):
                    bad.append("%s: %s" % (f, m.group(1)))
        self.assertEqual(bad, [], "文件名只应在 wdconfig 定义，发现硬编码：%s" % bad)

    def test_candidates_cover_legacy_layout(self):
        # 读方遍历整组，必须同时认兼容名与当前名；否则会出现「守护在跑、
        # 体检报警」的自我否定。
        self.assertIn("guard-watch.pid", C.PID_GUARD)

    def test_writer_uses_first_candidate(self):
        # 写方取 [0]，读方遍历：新名字排第一位，旧名字只用于兼容。
        self.assertEqual(C.PID_GUARD[0], "guard.pid")


if __name__ == "__main__":
    unittest.main()
