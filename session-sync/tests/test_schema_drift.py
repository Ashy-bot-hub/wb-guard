# -*- coding: utf-8 -*-
"""库结构漂移：客户端升级改了列名，脚本会在无人值守时失效。

`wd_unify` 用 `dict(row)` 取列、`build_plan` 直接读 `last_activity_at`。
这些名字写死在代码里，客户端一改结构，症状是「守护某天起不再工作」，而不是
任何一条报错 —— 因为异常发生在定时任务里。
这组用例把「缺表」「缺列」「列齐」三种情况钉住。
"""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wd_doctor as D
import wdconfig as C


class SchemaDriftTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wd_schema_")
        self.db = os.path.join(self.dir, "workbuddy.db")
        self.old = C.DB
        C.DB = self.db

    def tearDown(self):
        C.DB = self.old
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def _make(self, tables):
        conn = sqlite3.connect(self.db)
        for name, cols in tables.items():
            conn.execute("CREATE TABLE %s (%s)" % (name, ", ".join(cols)))
        conn.commit()
        conn.close()

    def _full(self):
        return {t: list(cols) for t, cols in D.REQUIRED_SCHEMA.items()}

    def test_complete_schema_ok(self):
        self._make(self._full())
        row = D.chk_schema()[0]
        self.assertEqual(row["status"], D.OK)
        self.assertIn("4 张表", row["detail"])

    def test_missing_column_fails(self):
        t = self._full()
        t["sessions"].remove("last_activity_at")
        self._make(t)
        row = D.chk_schema()[0]
        self.assertEqual(row["status"], D.FAIL)
        self.assertIn("sessions.last_activity_at", row["detail"])

    def test_missing_table_fails(self):
        t = self._full()
        del t["workspaces"]
        self._make(t)
        row = D.chk_schema()[0]
        self.assertEqual(row["status"], D.FAIL)
        self.assertIn("表 workspaces", row["detail"])

    def test_absent_db_is_warn(self):
        os.remove(self.db) if os.path.exists(self.db) else None
        self.assertEqual(D.chk_schema()[0]["status"], D.WARN)

    def test_extra_columns_are_fine(self):
        # 客户端新增列是正常升级，不该报错
        t = self._full()
        t["sessions"].append("brand_new_field")
        self._make(t)
        self.assertEqual(D.chk_schema()[0]["status"], D.OK)

    def test_required_table_is_a_constant(self):
        # 契约：必需列清单只在这里维护，散落到别处就等于没清单
        self.assertEqual(set(D.REQUIRED_SCHEMA), {"sessions", "automations",
                                                 "automation_runs", "workspaces"})


if __name__ == "__main__":
    unittest.main()
