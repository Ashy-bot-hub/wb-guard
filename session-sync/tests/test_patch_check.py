# -*- coding: utf-8 -*-
"""「补丁还活着吗」这一项，四种失效形态都要能报出来。

这一项的特殊之处在于：补丁失效时**其它 12 项全是绿的**。库没问题、进程没死、
日志没暴涨、索引干净 —— 唯一的现象是「删会话又退回旧行为」，而这要等到人
下一次删会话才看得见，往往已是几天后。所以它必须能自己证明「响得对」，
而不是靠跑一次看到「达标」就当它有效。

判据本身不写死在这里：三个用例故意把补丁目录推到旧版/异版，断言报的是
**对应的那一档**，因为「没标记」「版本旧」「上游变了」三种处置完全不同。
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wd_doctor as D


class PatchCheckTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wd_patch_")
        self.old_tool = D._patch_tool

    def tearDown(self):
        D._patch_tool = self.old_tool
        shutil.rmtree(self.dir, ignore_errors=True)

    def _pair(self, inject, daemon):
        for name, body in (("inject.js", inject), ("daemon.js", daemon)):
            with open(os.path.join(self.dir, name), "w", encoding="utf-8") as fh:
                fh.write(body)

    def _row(self):
        rows = D.chk_patch(scripts_dir=self.dir)
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_no_patch_tool_is_skip_not_warn(self):
        """只部署了体检脚本的机器，本来就不该有补丁：跳过，不制造噪声。

        误报比漏报更伤：一项天天喊的黄灯会让人整体忽略体检。
        """
        D._patch_tool = lambda: None
        self.assertEqual(D.chk_patch(scripts_dir=self.dir), [])

    def test_missing_marker_asks_for_reapply(self):
        """客户端更新覆盖 scripts/ —— 最可能发生的一种，必须给出动作。"""
        self._pair("var a = 1;", "const DAEMON_VERSION = '1.2.18';")
        row = self._row()
        self.assertEqual(row["status"], D.WARN)
        self.assertIn("无任何补丁标记", row["detail"])
        self.assertIn("apply.py", row["detail"])

    def test_older_marker_names_the_version_found(self):
        """报「旧版 r5」而不是笼统的「缺失」：能直接判断是哪次升级吃掉的。"""
        self._pair("[wbg-patch r5] x", "const DAEMON_VERSION = '1.2.18';\n[wbg-patch r5]")
        row = self._row()
        self.assertEqual(row["status"], D.WARN)
        self.assertIn("r5", row["detail"])

    def test_unknown_daemon_version_warns_even_when_marker_present(self):
        """标记在位但上游换了版本 —— 补丁行号可能已经错位，光看标记会误判。"""
        self._pair("[wbg-patch r6] x", "const DAEMON_VERSION = '1.2.99';\n[wbg-patch r6]")
        row = self._row()
        self.assertEqual(row["status"], D.WARN)
        self.assertIn("1.2.99", row["detail"])
        self.assertIn("适配清单", row["detail"])

    def test_current_pair_is_ok(self):
        self._pair("[wbg-patch r6] x", "const DAEMON_VERSION = '1.2.18';\n[wbg-patch r6]")
        self.assertEqual(self._row()["status"], D.OK)

    def test_missing_install_dir_warns(self):
        """找不到安装目录时不能静默「达标」——那是最危险的一种假绿。"""
        row = D.chk_patch(scripts_dir=os.path.join(self.dir, "nope"))
        self.assertEqual(row[0]["status"], D.WARN)
        self.assertIn("未找到 WorkDaddy 安装目录", row[0]["detail"])


class PatchToolDiscoveryTest(unittest.TestCase):
    def test_finds_repo_panel_patches(self):
        """仓库里 patch 目录与体检脚本不是同级（源码在 wb-guard 子目录），
        推导必须能穿过去；找不到等于这一项永远跳过。"""
        tool = D._patch_tool()
        self.assertIsNotNone(tool, "本仓库内应能定位到 panel-patches")
        self.assertTrue(os.path.isfile(os.path.join(tool, "apply.py")))


if __name__ == "__main__":
    unittest.main()
