# -*- coding: utf-8 -*-
"""合并判据：只合并不丢内容。

`wd_unify` 的合并动作是「unlink 副本 + link 正本」，一旦判错就是丢数据。
所以判据必须是可穷举的纯比较：一方内容是否为另一方的前缀。这里覆盖四个
分支（相同 / 正本覆盖副本 / 副本覆盖正本 / 真分叉）以及缺转录、已同 inode
两种短路情况。
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wd_unify as U


class RelationTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wd-rel-")
        self.a = self._w("a.jsonl", b'{"m":1}\n{"m":2}\n')
        self.b = self._w("b.jsonl", b'{"m":1}\n{"m":2}\n')
        self.long = self._w("long.jsonl", b'{"m":1}\n{"m":2}\n{"m":3}\n')
        self.other = self._w("other.jsonl", b'{"m":9}\n{"m":2}\n')

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _w(self, name, payload):
        p = os.path.join(self.dir, name)
        with open(p, "wb") as fh:
            fh.write(payload)
        return p

    def test_identical(self):
        self.assertEqual(U.relation(self.a, self.b), U.SAME)

    def test_base_extends_other(self):
        self.assertEqual(U.relation(self.long, self.a), U.BASE_COVERS)

    def test_other_extends_base(self):
        self.assertEqual(U.relation(self.a, self.long), U.OTHER_COVERS)

    def test_diverged_same_length(self):
        self.assertEqual(U.relation(self.a, self.other), U.DIVERGE)

    def test_missing_file_is_diverged(self):
        self.assertEqual(U.relation(self.a, os.path.join(self.dir, "nope")), U.DIVERGE)


def stat(path, ino=(0, 1)):
    return {"path": path, "lines": 1, "mtime": 0, "ino": ino, "size": 0}


def member(sid):
    return {"id": sid, "user_id": "u", "status": "completed",
            "updated_at": 0, "last_activity_at": 0, "title": "t"}


class MergeOneTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wd-merge-")
        self.p1 = self._w("one.jsonl", b"1\n2\n")
        self.p2 = self._w("two.jsonl", b"1\n2\n")
        self.p3 = self._w("three.jsonl", b"1\n2\n3\n")
        self.p4 = self._w("four.jsonl", b"9\n")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _w(self, name, payload):
        p = os.path.join(self.dir, name)
        with open(p, "wb") as fh:
            fh.write(payload)
        return p

    def _run(self, mpath, tpath):
        m, t = member("m"), member("t")
        st = {"m": stat(mpath, (0, 2)), "t": stat(tpath, (0, 1))}
        relink, diverged = [], []
        new_target = U._merge_one(m, t, st, relink, diverged)
        return relink, diverged, new_target

    def test_identical_is_merged(self):
        relink, diverged, target = self._run(self.p2, self.p1)
        self.assertEqual(len(relink), 1)
        self.assertEqual(diverged, [])
        self.assertEqual(target["id"], "t")

    def test_covered_copy_is_merged(self):
        relink, diverged, _ = self._run(self.p1, self.p3)
        self.assertEqual([x[0]["id"] for x in relink], ["m"])
        self.assertEqual(diverged, [])

    def test_longer_copy_takes_over(self):
        # 副本比正本长：正本反过来并过去，避免丢掉副本的独有内容
        relink, diverged, target = self._run(self.p3, self.p1)
        self.assertEqual([x[0]["id"] for x in relink], ["t"])
        self.assertEqual(target["id"], "m")
        self.assertEqual(diverged, [])

    def test_diverged_is_left_alone(self):
        relink, diverged, target = self._run(self.p4, self.p1)
        self.assertEqual(relink, [])
        self.assertEqual(len(diverged), 1)
        self.assertEqual(target["id"], "t")

    def test_same_inode_is_noop(self):
        m, t = member("m"), member("t")
        st = {"m": stat(self.p1, (0, 7)), "t": stat(self.p2, (0, 7))}
        relink, diverged = [], []
        U._merge_one(m, t, st, relink, diverged)
        self.assertEqual((relink, diverged), ([], []))


if __name__ == "__main__":
    unittest.main()
