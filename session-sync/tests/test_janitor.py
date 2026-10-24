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


class TestDirFallback(unittest.TestCase):
    """整目录删不掉时的降级：能删的子项要删掉，别被一个删不掉的文件拖住。

    真实案例：190 MB 的日期目录里有一个 0.9 MB 的文件被安全规则保护，整目录
    回收站操作整批回滚，一个字节都没收回来。降级后同一场景能收回另外的 189 MB。
    """

    def _mk(self, n=3, size=100):
        d = tempfile.mkdtemp(prefix="wd-test-")
        for i in range(n):
            with open(os.path.join(d, "a%d.log" % i), "w", encoding="utf-8") as fh:
                fh.write("x" * size)
        return d

    def test_sweep_removes_children_when_rmtree_fails(self):
        d = self._mk()
        keeper = os.path.join(d, "a1.log")

        def boom(path, *a, **k):
            raise OSError("整批回滚")

        real_remove = os.remove

        def fake_remove(p, *a, **k):
            if os.path.normcase(p) == os.path.normcase(keeper):
                raise OSError("protected")      # 模拟被规则保护的那个文件
            return real_remove(p, *a, **k)

        try:
            with mock.patch.object(J.shutil, "rmtree", side_effect=boom), \
                 mock.patch.object(J.os, "remove", side_effect=fake_remove):
                ok = J._delete_target(d)
            self.assertFalse(ok, "目录里还有删不掉的文件 → 不算删干净")
            self.assertTrue(os.path.exists(keeper), "受保护文件必须留下")
            self.assertFalse(os.path.exists(os.path.join(d, "a0.log")))
            self.assertFalse(os.path.exists(os.path.join(d, "a2.log")))
        finally:
            for n in os.listdir(d):
                try:
                    os.remove(os.path.join(d, n))
                except OSError:
                    pass
            try:
                os.rmdir(d)
            except OSError:
                pass

    def test_sweep_removes_empty_dir(self):
        d = self._mk(2)
        with mock.patch.object(J.shutil, "rmtree", side_effect=OSError("x")):
            ok = J._delete_target(d)
        self.assertTrue(ok)
        self.assertFalse(os.path.exists(d), "子项删完后应把空目录也收掉")

    def test_missing_target_is_success(self):
        d = tempfile.mkdtemp(prefix="wd-test-")
        try:
            self.assertTrue(J._delete_target(os.path.join(d, "nope")))
        finally:
            os.rmdir(d)


class TestPartialRetry(unittest.TestCase):
    """降级删除的两条硬要求：子目录要递归、失败项要重试。

    2026-10-24 实测的差距就在这里：10-21、10-23 两个日期目录合计 342 MB，
    `--apply` 报「占用中跳过」、一个字节没回收；一分钟后逐文件删，176 个
    文件 0 失败。旧实现对子目录直接调 `shutil.rmtree`，一次抛出就整块放弃，
    顶层删了、子目录里本来能删的全留着。
    """

    def test_subdir_lock_does_not_discard_siblings(self):
        d = tempfile.mkdtemp(prefix="wd-test-")
        sub = os.path.join(d, "sdk")
        os.makedirs(sub)
        paths = [os.path.join(d, "top.log"),
                 os.path.join(sub, "ok1.log"),
                 os.path.join(sub, "keep.log"),
                 os.path.join(sub, "ok2.log")]
        for p in paths:
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("x" * 100)
        keeper = os.path.join(sub, "keep.log")
        real_remove = os.remove

        def fake_remove(p, *a, **k):
            if os.path.normcase(p) == os.path.normcase(keeper):
                raise OSError("locked")
            return real_remove(p, *a, **k)

        try:
            with mock.patch.object(J.shutil, "rmtree", side_effect=OSError("整批回滚")), \
                 mock.patch.object(J.os, "remove", side_effect=fake_remove), \
                 mock.patch.object(J.time, "sleep", return_value=None):
                ok = J._delete_target(d)
            self.assertFalse(ok, "还有删不掉的子项 → 不算删干净")
            self.assertTrue(os.path.exists(keeper), "被占用的文件必须留下")
            for gone in (paths[0], paths[1], paths[3]):
                self.assertFalse(os.path.exists(gone), "同目录兄弟项必须删掉: %s" % gone)
        finally:
            for root, _dirs, files in os.walk(d, topdown=False):
                for n in files:
                    try:
                        real_remove(os.path.join(root, n))
                    except OSError:
                        pass
                try:
                    os.rmdir(root)
                except OSError:
                    pass

    def test_transient_lock_is_retried_and_dir_removed(self):
        """第一次抛占用、重试成功的目录，最终要删净并收掉空目录。"""
        d = tempfile.mkdtemp(prefix="wd-test-")
        files = [os.path.join(d, "a%d.log" % i) for i in range(3)]
        for p in files:
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("x" * 100)
        real_remove = os.remove
        state = {"n": 0}

        def flaky(p, *a, **k):
            if p in files and state["n"] == 0:
                state["n"] += 1            # 只在第一次删时占用
                raise OSError("busy")
            return real_remove(p, *a, **k)

        try:
            with mock.patch.object(J.shutil, "rmtree", side_effect=OSError("整批回滚")), \
                 mock.patch.object(J.os, "remove", side_effect=flaky), \
                 mock.patch.object(J.time, "sleep", return_value=None):
                ok = J._delete_target(d)
            self.assertTrue(ok, "重试后应删净")
            self.assertFalse(os.path.exists(d), "空目录也要收掉")
        finally:
            if os.path.isdir(d):
                for root, _dirs, fs in os.walk(d, topdown=False):
                    for n in fs:
                        try:
                            real_remove(os.path.join(root, n))
                        except OSError:
                            pass
                    try:
                        os.rmdir(root)
                    except OSError:
                        pass


class TestApplyIsolation(unittest.TestCase):
    """一项失败不能让整轮停摆。

    本机删除通道失败状态是进程级的：一个对象删失败后，同进程里后续删除全部
    被拒。2026-10-24 实测因此 21 项全跳过、回收 0 字节。所以 apply() 必须
    逐项隔离，并且遇到失败项要继续往下走。
    """

    def test_failed_item_does_not_stop_the_rest(self):
        d = tempfile.mkdtemp(prefix="wd-test-")
        f1, f2 = os.path.join(d, "a.log"), os.path.join(d, "b.log")
        for f in (f1, f2):
            with open(f, "w", encoding="utf-8") as fh:
                fh.write("x" * 50)
        calls = []

        def fake(path):
            calls.append(path)
            if path == f1:
                return False, 0
            os.remove(path)
            return True, 50

        try:
            with mock.patch.object(J, "_delete_one", side_effect=fake):
                freed, skipped = J.apply([(f1, 50, "坏"), (f2, 50, "好")])
            self.assertEqual(skipped, [f1])
            self.assertEqual(freed, 50)
            self.assertEqual(calls, [f1, f2], "失败项之后必须继续处理")
            self.assertTrue(os.path.exists(f2) is False)
        finally:
            for n in os.listdir(d):
                os.remove(os.path.join(d, n))
            os.rmdir(d)


class TestThresholds(unittest.TestCase):
    """体检与清理必须同一口径，否则会出现「体检达标但永远删不干净」。"""

    def test_doctor_budget_comes_from_janitor(self):
        import wd_doctor as D
        self.assertEqual(D.LOG_BUDGET, J.LOG_BUDGET_MB)
        self.assertEqual(D.HOT_IDLE_MIN, J.IDLE_MIN)


if __name__ == "__main__":
    unittest.main(verbosity=2)
