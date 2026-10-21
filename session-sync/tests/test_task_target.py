# -*- coding: utf-8 -*-
"""计划任务指向判定的单测。

为什么写
--------
只看「任务定义文件在不在、能不能解析出 Command」，不看它到底跑哪个脚本，
就会出现：任务指向别的脚本，包里明明是 `wd_watch.py`，体检却全绿。

后果很具体：改名后漏改任务 → 守护继续跑别的代码 → 体检说达标 → 没人会发现。
这里把判定边界固定下来，包括「路径带空格」「引号」「前面还有开关」这些
真实命令行里都会出现的形态。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import wdrules as R   # noqa: E402


class TestTaskScriptPath(unittest.TestCase):
    def test_quoted_path_with_spaces(self):
        args = '"C:\\Program Files\\wd\\_session-sync\\wd_watch.py"'
        self.assertEqual(R.task_script_path(args),
                         "C:\\Program Files\\wd\\_session-sync\\wd_watch.py")

    def test_unquoted_path(self):
        self.assertEqual(R.task_script_path(r"C:\tools\wd_watch.py"),
                         r"C:\tools\wd_watch.py")

    def test_leading_switches_are_skipped(self):
        args = '-NoProfile -ExecutionPolicy Bypass -File "C:\\x\\install.ps1"'
        self.assertEqual(R.task_script_path(args), "C:\\x\\install.ps1")

    def test_mjs_is_recognized(self):
        args = r'"C:\wb\cdp.mjs"'
        self.assertEqual(R.task_script_path(args), r"C:\wb\cdp.mjs")

    def test_arguments_is_optional(self):
        # 任务的 Arguments 可以为空（Command 自带参数时），此时不该抛异常
        self.assertEqual(R.task_script_path(None), "")
        self.assertEqual(R.task_script_path(""), "")

    def test_no_script_in_command_line(self):
        # pythonw.exe 后面什么都没跟：看不出脚本，交给 judge 报告警
        self.assertEqual(R.task_script_path("--port 9222"), "")

    def test_json_is_not_mistaken_for_js(self):
        # ".json" 不该被当成 ".js" —— 否则配置路径会被误认成脚本
        self.assertEqual(R.task_script_path(r'--config "C:\a\b.json"'), "")


class TestJudgeTaskTarget(unittest.TestCase):
    def _exists_yes(self, _p):
        return True

    def _exists_no(self, _p):
        return False

    def test_match_and_file_present(self):
        state, why = R.judge_task_target(r"C:\wb\wd_watch.py", "wd_watch.py",
                                         exists=self._exists_yes)
        self.assertEqual(state, "ok")
        self.assertEqual(why, "wd_watch.py")

    def test_match_is_case_insensitive(self):
        state, _ = R.judge_task_target(r"C:\wb\WD_WATCH.PY", "wd_watch.py",
                                       exists=self._exists_yes)
        self.assertEqual(state, "ok")

    def test_old_script_present_is_warn(self):
        # 任务指向别的脚本、文件还在 → 告警而不是故障，
        # 因为它确实能跑，只是跑的不是当前包的代码
        state, why = R.judge_task_target(
            r"C:\wb\_session-sync\watch_account_switch.py", "wd_watch.py",
            exists=self._exists_yes)
        self.assertEqual(state, "warn")
        self.assertIn("watch_account_switch.py", why)
        self.assertIn("wd_watch.py", why)

    def test_expected_name_but_missing_file_is_fail(self):
        state, why = R.judge_task_target(r"C:\gone\wd_watch.py", "wd_watch.py",
                                         exists=self._exists_no)
        self.assertEqual(state, "fail")
        self.assertIn("不存在", why)

    def test_other_name_and_missing_file_is_fail(self):
        state, _ = R.judge_task_target(r"C:\gone\old.py", "wd_watch.py",
                                       exists=self._exists_no)
        self.assertEqual(state, "fail")

    def test_empty_target_is_warn(self):
        state, why = R.judge_task_target("", "wd_watch.py",
                                         exists=self._exists_yes)
        self.assertEqual(state, "warn")
        self.assertIn("看不出", why)


if __name__ == "__main__":
    unittest.main()
