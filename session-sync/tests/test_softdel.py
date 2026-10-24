# -*- coding: utf-8 -*-
"""软删收敛的边界：该删的删干净，不该碰的一个也不许碰。

这一项的风险等级与其它脚本不同 —— 它做的是**不可逆的物理删除**。所以用例
重点不在「能删掉」，而在「拦得住」：活跃会话、未到期软删、名字相近的文件、
wbHome 之外的路径，四种情况逐一验它不动手。
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wd_softdel as S


def sid():
    return str(uuid.uuid4())


class Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="wd_softdel_")
        self.wb = self.root                      # 假的 ~/.workbuddy
        self.db = os.path.join(self.root, "workbuddy.db")
        self.bak = os.path.join(self.root, "backups")
        self._old = (S.C.DB, S.C.WORKBUDDY, S.C.BACKUP)
        S.C.DB, S.C.WORKBUDDY, S.C.BACKUP = self.db, self.wb, self.bak
        os.makedirs(self.bak, exist_ok=True)
        self.conn = sqlite3.connect(self.db)
        self.conn.executescript("""
          CREATE TABLE sessions (
            id TEXT PRIMARY KEY, cwd TEXT, user_id TEXT, title TEXT, custom_title TEXT,
            status TEXT, created_at TEXT, updated_at TEXT, last_activity_at TEXT,
            deleted_at INTEGER, is_playground INTEGER, is_background_automation INTEGER);
          CREATE TABLE session_usage (
            session_id TEXT PRIMARY KEY, used INTEGER NOT NULL, size INTEGER NOT NULL,
            updated_at INTEGER NOT NULL, credit_json TEXT);
        """)
        self.now = int(time.time() * 1000)

    def tearDown(self):
        self.conn.close()
        S.C.DB, S.C.WORKBUDDY, S.C.BACKUP = self._old
        shutil.rmtree(self.root, ignore_errors=True)

    def add(self, sid, deleted_days_ago=None, uid="u1", title="t"):
        deleted = (self.now - int(deleted_days_ago * 86400000)) if deleted_days_ago is not None else None
        self.conn.execute(
            "INSERT INTO sessions (id,cwd,user_id,title,status,created_at,updated_at,"
            "last_activity_at,deleted_at,is_playground,is_background_automation) "
            "VALUES (?,?,?,?,?,?,?,?,?,0,0)",
            (sid, "C:\\w", uid, title, "completed", str(self.now), str(self.now),
             str(self.now), deleted))
        self.conn.execute("INSERT INTO session_usage (session_id,used,size,updated_at) "
                          "VALUES (?,?,?,?)", (sid, 10, 20, self.now))
        self.conn.commit()

    def mkfile(self, *parts):
        p = os.path.join(self.wb, *parts)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("x" * 10)
        return p

    def mk_everything(self, sid, proj="p1"):
        """把 daemon 那 11 处落盘全部铺出来，并返回路径清单。"""
        made = [
            self.mkfile("projects", proj, sid + ".jsonl"),
            self.mkfile("projects", proj, sid, "m.jsonl"),
            self.mkfile("projects", proj, sid + ".meta.json"),
            self.mkfile("projects", proj, sid + ".file-rollback.ndjson"),
            self.mkfile("workspace", "sessions", sid, "a.json"),
            self.mkfile("tasks", sid, "a.json"),
            self.mkfile("file-history", sid, "a.json"),
            self.mkfile("artifact-index", sid + ".json"),
            self.mkfile("changes-detail", sid, "a.json"),
            self.mkfile("changes-index", sid + ".json"),
            self.mkfile("file-tree-manifests", sid + ".json"),
        ]
        return made


class IndexTest(Base):
    def test_index_finds_all_eleven_positions(self):
        s = sid()
        self.mk_everything(s)
        idx = S.build_index(self.wb)
        self.assertEqual(len(idx[s]), 11, "11 处落盘都应被索引到")
        # 项目内 <id>/ 等 5 处索引的是目录本身（整棵子树一起删），其余按文件名索引
        names = sorted(os.path.basename(p) for p in idx[s])
        self.assertEqual(names.count(s), 5,
                         "项目目录 + workspace/tasks/file-history/changes-detail")
        for suffix in (".jsonl", ".meta.json", ".file-rollback.ndjson", ".json"):
            self.assertTrue(any(p.endswith(s + suffix) for p in idx[s]), suffix)

    def test_index_ignores_lookalike_names(self):
        """前缀相同但 id 不同的文件不能被算进来 —— 那是别的会话的数据。"""
        s = sid()
        self.mk_everything(s)
        self.mkfile("projects", "p1", s + "-old.jsonl")     # 尾巴多一截，不是它
        idx = S.build_index(self.wb)
        self.assertEqual(len(idx[s]), 11)

    def test_node_size_sums_directory(self):
        d = os.path.join(self.wb, "changes-detail", sid())
        os.makedirs(d, exist_ok=True)
        for i in range(3):
            with open(os.path.join(d, "%d.json" % i), "w") as fh:
                fh.write("abc")
        self.assertEqual(S.node_size(d), 9)


class CountsTest(Base):
    def test_classifies_usage_three_ways(self):
        live, soft, orphan = sid(), sid(), sid()
        self.add(live)
        self.add(soft, deleted_days_ago=1)
        self.conn.execute("INSERT INTO session_usage (session_id,used,size,updated_at) "
                          "VALUES (?,?,?,?)", (orphan, 1, 1, self.now))
        self.conn.commit()
        n = S.counts(self.conn, index={})
        self.assertEqual((n["live"], n["soft"], n["orphan_usage"], n["soft_usage"]),
                         (1, 1, 1, 1))

    def test_due_respects_grace(self):
        self.add(sid(), deleted_days_ago=1)      # 未到期
        self.add(sid(), deleted_days_ago=30)     # 到期
        n = S.counts(self.conn, index={})
        self.assertEqual(n["due"], 1)

    def test_judge_warns_on_backlog(self):
        self.assertEqual(S.judge({"soft": 0, "orphan_usage": 0})[0], "ok")
        self.assertEqual(S.judge({"soft": 201, "orphan_usage": 0})[0], "warn")
        self.assertEqual(S.judge({"soft": 5, "orphan_usage": 101})[0], "warn")


class SafetyTest(Base):
    def test_live_session_never_touched(self):
        s = sid()
        self.add(s)                              # deleted_at IS NULL
        self.mk_everything(s)
        r = S.prune_soft(self.conn, S.now_ms() + 1, apply=True, do_backup=False)
        self.assertEqual(r["pruned"], 0)
        self.assertTrue(os.path.exists(os.path.join(self.wb, "projects", "p1", s + ".jsonl")))

    def test_recent_soft_session_kept(self):
        s = sid()
        self.add(s, deleted_days_ago=1)
        self.mk_everything(s)
        r = S.prune_soft(self.conn, S.now_ms() - 7 * 86400000, apply=True, do_backup=False)
        self.assertEqual(r["pruned"], 0)
        self.assertTrue(os.path.exists(os.path.join(self.wb, "tasks", s)))

    def test_due_soft_session_fully_removed(self):
        s = sid()
        self.add(s, deleted_days_ago=30)
        made = self.mk_everything(s)
        r = S.prune_soft(self.conn, S.now_ms() - 7 * 86400000, apply=True, do_backup=False)
        self.assertEqual(r["pruned"], 1)
        self.assertEqual(r["files"], 11)
        for p in made:
            self.assertFalse(os.path.exists(p), p)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM sessions WHERE id=?",
                                           (s,)).fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM session_usage WHERE session_id=?",
                                           (s,)).fetchone()[0], 0)

    def test_limit_is_honoured(self):
        for _ in range(5):
            self.add(sid(), deleted_days_ago=30)
        r = S.prune_soft(self.conn, S.now_ms() - 7 * 86400000, apply=True, limit=2,
                         do_backup=False)
        self.assertEqual(r["pruned"], 2)

    def test_refuses_path_outside_workbuddy(self):
        """白名单里混进越界路径时宁可不删：id 目录名是外部可控的。"""
        s = sid()
        self.add(s, deleted_days_ago=30)
        outside_root = tempfile.mkdtemp(prefix="wd_elsewhere_")
        try:
            outside = os.path.join(outside_root, s)
            os.makedirs(outside, exist_ok=True)
            keep = os.path.join(outside, "keep.txt")
            with open(keep, "w") as fh:
                fh.write("keep")
            removed, _ = S.remove_nodes(s, [outside], self.wb)
            self.assertEqual(removed, 0)
            self.assertTrue(os.path.exists(keep))
        finally:
            shutil.rmtree(outside_root, ignore_errors=True)

    def test_refuses_name_mismatch(self):
        s = sid()
        self.add(s, deleted_days_ago=30)
        other = self.mkfile("tasks", sid(), "a.json")
        removed, _ = S.remove_nodes(s, [os.path.dirname(other)], self.wb)
        self.assertEqual(removed, 0)
        self.assertTrue(os.path.exists(other))

    def test_dry_run_writes_nothing(self):
        s = sid()
        self.add(s, deleted_days_ago=30)
        self.mk_everything(s)
        S.prune_soft(self.conn, S.now_ms() - 7 * 86400000, apply=False)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 1)
        self.assertTrue(os.path.exists(os.path.join(self.wb, "tasks", s)))


class OrphanUsageTest(Base):
    def test_only_orphans_removed(self):
        live, soft = sid(), sid()
        self.add(live)
        self.add(soft, deleted_days_ago=1)
        gone = sid()
        self.conn.execute("INSERT INTO session_usage (session_id,used,size,updated_at) "
                          "VALUES (?,?,?,?)", (gone, 1, 1, self.now))
        self.conn.commit()
        n = S.purge_orphan_usage(self.conn, apply=True)
        self.assertEqual(n, 1)
        left = {r[0] for r in self.conn.execute("SELECT session_id FROM session_usage")}
        self.assertEqual(left, {live, soft}, "活跃与软删（行还在）的用量必须保留")

    def test_dry_run_keeps_rows(self):
        gone = sid()
        self.conn.execute("INSERT INTO session_usage (session_id,used,size,updated_at) "
                          "VALUES (?,?,?,?)", (gone, 1, 1, self.now))
        self.conn.commit()
        S.purge_orphan_usage(self.conn, apply=False)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM session_usage").fetchone()[0], 1)


class ParseAgeTest(unittest.TestCase):
    def test_days_and_hours(self):
        self.assertLess(S.parse_age("30d"), S.parse_age("7d"))
        self.assertLess(S.parse_age("7d"), S.parse_age("24h"))
        with self.assertRaises(Exception):
            S.parse_age("7")


if __name__ == "__main__":
    unittest.main()
