"""测试 weibo_im.export 模块的过滤逻辑与空结果行为。

基于 conftest.make_test_db 构造临时库（schema 与生产一致），不依赖网络/cookie。
重点验证：gid 过滤、成员(name/id)过滤、msg_type 默认仅 100/321、时间范围、
升序排列、空结果。
"""
import os
import sqlite3
import unittest

from tests.conftest import make_test_db, insert_messages
import weibo_im.db as dbmod
import weibo_im.export as exportmod


class _DbTestBase(unittest.TestCase):
    """每个用例独立临时库；重置 db 模块 thread-local 连接避免跨用例串。

    weibo_im.db.get_conn() 用 threading.local 缓存连接，且 _DB_PATH 为全局变量，
    若不重置，前一个用例的连接会指向已删除的旧库。setUp 里把 _local.conn 置
    None 强制下次 get_conn() 重建，并 set_db_path 到当前临时库。
    """

    def make_data(self, conn):
        pass

    def setUp(self):
        self.db_path = make_test_db()
        conn = sqlite3.connect(self.db_path)
        self.make_data(conn)
        conn.close()
        dbmod._local.conn = None
        dbmod.set_db_path(self.db_path)
        dbmod.init_db()

    def tearDown(self):
        conn = getattr(dbmod._local, "conn", None)
        if conn is not None:
            conn.close()
        dbmod._local.conn = None
        for suffix in ("", "-wal", "-shm"):
            p = self.db_path + suffix
            if os.path.exists(p):
                try:
                    os.remove(p)
                except PermissionError:
                    pass


class GetMembersTest(_DbTestBase):
    def make_data(self, conn):
        conn.execute("INSERT INTO groups(gid,name) VALUES(100,'群A'),(200,'群B')")
        insert_messages(conn, [
            {"mid": "m1", "gid": 100, "sender_id": 1, "sender_name": "甲",
             "msg_type": 321, "text": "hi", "created_at": 1000},
            {"mid": "m2", "gid": 100, "sender_id": 2, "sender_name": "乙",
             "msg_type": 321, "text": "yo", "created_at": 2000},
            {"mid": "m3", "gid": 100, "sender_id": 1, "sender_name": "甲",
             "msg_type": 100, "text": "分享", "created_at": 3000},
            # 系统消息(322 入群)不应计入成员发言统计
            {"mid": "m4", "gid": 100, "sender_id": 3, "sender_name": "丙",
             "msg_type": 322, "text": "入群", "created_at": 4000},
            # 另一群，不应出现
            {"mid": "m5", "gid": 200, "sender_id": 1, "sender_name": "甲",
             "msg_type": 321, "text": "other", "created_at": 5000},
        ])

    def test_excludes_system_msgs_and_other_groups(self):
        members = exportmod.get_members(100)
        # 丙(322系统消息)与群200被排除；按最近发言倒序：甲(3000) > 乙(2000)
        self.assertEqual([m["sender_id"] for m in members], [1, 2])
        m_jia = next(m for m in members if m["sender_id"] == 1)
        self.assertEqual(m_jia["msg_count"], 2)  # m1(321) + m3(100)
        self.assertEqual(m_jia["sender_name"], "甲")

    def test_empty_when_group_has_no_user_msgs(self):
        self.assertEqual(exportmod.get_members(999), [])


class GetMemberMessagesTest(_DbTestBase):
    BASE = 1750113600000  # 2025-06-17 00:00 CST

    def make_data(self, conn):
        conn.execute("INSERT INTO groups(gid,name) VALUES(100,'群A'),(200,'群B')")
        b = self.BASE
        insert_messages(conn, [
            {"mid": "e1", "gid": 100, "sender_id": 1, "sender_name": "甲",
             "msg_type": 321, "text": "a", "created_at": b + 1000},
            {"mid": "e2", "gid": 100, "sender_id": 2, "sender_name": "乙",
             "msg_type": 321, "text": "b", "created_at": b + 2000},
            {"mid": "e3", "gid": 100, "sender_id": 1, "sender_name": "甲",
             "msg_type": 100, "text": "c", "created_at": b + 3000},
            {"mid": "e4", "gid": 100, "sender_id": 1, "sender_name": "甲",
             "msg_type": 322, "text": "系统", "created_at": b + 4000},  # 系统消息
            {"mid": "e5", "gid": 200, "sender_id": 1, "sender_name": "甲",
             "msg_type": 321, "text": "other-group", "created_at": b + 5000},
        ])

    def test_filter_by_sender_name(self):
        rows = exportmod.get_member_messages(100, sender_name="甲")
        # 升序，排除 e4(322 系统消息) 与 e5(另一群)
        self.assertEqual([r["mid"] for r in rows], ["e1", "e3"])
        self.assertTrue(all(r["sender_name"] == "甲" for r in rows))

    def test_filter_by_sender_id(self):
        rows = exportmod.get_member_messages(100, sender_id=2)
        self.assertEqual([r["mid"] for r in rows], ["e2"])

    def test_filter_by_both_name_and_id(self):
        # name=甲 + id=1：交集即甲(id=1)
        rows = exportmod.get_member_messages(100, sender_name="甲", sender_id=1)
        self.assertEqual(sorted(r["mid"] for r in rows), ["e1", "e3"])
        # name=甲 但 id=2 不匹配（甲的 id 是 1）→ 空
        rows2 = exportmod.get_member_messages(100, sender_name="甲", sender_id=2)
        self.assertEqual(rows2, [])

    def test_time_range_filter(self):
        # [b+2000, b+3000)：甲的 e1(1000)<2000 排除，e3(3000) 不<3000 排除 → 空
        rows = exportmod.get_member_messages(100, sender_name="甲",
                                         since_ms=self.BASE + 2000,
                                         until_ms=self.BASE + 3000)
        self.assertEqual(rows, [])
        # 放宽 until=b+4000 → [2000,4000)：甲有 e3(3000)
        rows2 = exportmod.get_member_messages(100, sender_name="甲",
                                          since_ms=self.BASE + 2000,
                                          until_ms=self.BASE + 4000)
        self.assertEqual([r["mid"] for r in rows2], ["e3"])

    def test_ascending_order(self):
        rows = exportmod.get_member_messages(100, sender_name="甲")
        ts = [r["created_at"] for r in rows]
        self.assertEqual(ts, sorted(ts))

    def test_empty_when_no_match(self):
        self.assertEqual(exportmod.get_member_messages(100, sender_name="不存在"), [])
        self.assertEqual(exportmod.get_member_messages(999, sender_name="甲"), [])

    def test_default_msg_types_excludes_system(self):
        rows = exportmod.get_member_messages(100, sender_name="甲")
        self.assertNotIn("e4", [r["mid"] for r in rows])  # 322 入群被排除
        # 显式放宽 msg_types 可含系统消息
        rows2 = exportmod.get_member_messages(100, sender_name="甲",
                                          msg_types=(321, 100, 322))
        self.assertIn("e4", [r["mid"] for r in rows2])

    def test_no_sender_filter_returns_all_user_msgs_in_group(self):
        # 不传 name/id：返回该群全部用户发言(100/321)，排除系统消息与另一群
        rows = exportmod.get_member_messages(100)
        self.assertEqual(sorted(r["mid"] for r in rows), ["e1", "e2", "e3"])

    def test_returns_expected_columns(self):
        rows = exportmod.get_member_messages(100, sender_name="甲")
        r = rows[0]
        for col in ("mid", "gid", "msg_type", "msg_type_name", "media_type",
                    "sender_id", "sender_name", "text", "fid", "media_orig_url",
                    "url_objects", "created_at", "group_name"):
            self.assertIn(col, r)


if __name__ == "__main__":
    unittest.main()
