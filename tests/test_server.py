import http.client
import json
import os
import socket
import sqlite3
import threading
import unittest

from tests.conftest import make_test_db, insert_messages
import server


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _ServerTestBase(unittest.TestCase):
    """启动一个 server 供子类测试。子类重写 make_data() 写入测试数据。"""

    def make_data(self, conn):
        pass

    def setUp(self):
        self.db_path = make_test_db()
        conn = sqlite3.connect(self.db_path)
        self.make_data(conn)
        conn.close()
        self.port = _free_port()
        self.httpd = server.make_server("127.0.0.1", self.port, self.db_path)
        self.t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.t.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.httpd.db_conn.close()
        os.remove(self.db_path)

    def _get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read().decode("utf-8")
        conn.close()
        return resp.status, body

    def _get_json(self, path):
        status, body = self._get(path)
        return status, json.loads(body)


class ServerSkeletonTest(_ServerTestBase):
    def test_index_html_served(self):
        status, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("<html", body)

    def test_unknown_api_returns_404(self):
        status, _ = self._get("/api/unknown")
        self.assertEqual(status, 404)

    def test_404_for_missing(self):
        status, _ = self._get("/nope")
        self.assertEqual(status, 404)


class MetadataApiTest(_ServerTestBase):
    def make_data(self, conn):
        conn.execute("INSERT INTO groups(gid,name) VALUES(100,'群A'),(200,'群B')")
        # 群100：1750200000000 → 2025-06-18 CST，1750113600000 → 2025-06-17 CST
        # 群200 一条
        insert_messages(conn, [
            {"mid": "m1", "gid": 100, "sender_id": 1, "sender_name": "甲",
             "text": "hi", "created_at": 1750200000000},  # 2025-06-18 CST
            {"mid": "m2", "gid": 100, "sender_id": 2, "sender_name": "乙",
             "text": "yo", "created_at": 1750113600000},  # 2025-06-17 CST
            {"mid": "m3", "gid": 100, "sender_id": 1, "sender_name": "甲",
             "text": "x", "created_at": 1750113600000},
            {"mid": "m4", "gid": 200, "sender_id": 9, "sender_name": "丙",
             "text": "z", "created_at": 1750113600000},
        ])

    def test_groups(self):
        status, data = self._get_json("/api/groups")
        self.assertEqual(status, 200)
        self.assertEqual(data, [
            {"gid": 100, "name": "群A", "msg_count": 3},
            {"gid": 200, "name": "群B", "msg_count": 1},
        ])

    def test_dates(self):
        status, data = self._get_json("/api/dates?gid=100")
        self.assertEqual(status, 200)
        self.assertEqual(data, [
            {"date": "2025-06-18", "count": 1},
            {"date": "2025-06-17", "count": 2},
        ])

    def test_senders(self):
        status, data = self._get_json("/api/senders?gid=100")
        self.assertEqual(status, 200)
        self.assertEqual(data, [
            {"sender_id": 1, "sender_name": "甲", "count": 2},
            {"sender_id": 2, "sender_name": "乙", "count": 1},
        ])


class MessagesCursorTest(_ServerTestBase):
    def make_data(self, conn):
        conn.execute("INSERT INTO groups(gid,name) VALUES(100,'群A')")
        # 5 条：ts 递增；m2/m3 同毫秒、m4/m5 同毫秒，测 tiebreaker
        # id 顺序与 ts 顺序一致，便于预期
        insert_messages(conn, [
            {"mid": "m1", "gid": 100, "sender_id": 1, "text": "a", "created_at": 1000},
            {"mid": "m2", "gid": 100, "sender_id": 1, "text": "b", "created_at": 2000},
            {"mid": "m3", "gid": 100, "sender_id": 2, "text": "c", "created_at": 2000},
            {"mid": "m4", "gid": 100, "sender_id": 1, "text": "d", "created_at": 3000},
            {"mid": "m5", "gid": 100, "sender_id": 2, "text": "e", "created_at": 3000},
        ])

    def test_after_cursor_loads_newer(self):
        # after 游标 = m2(ts=2000,id=2)，取更新：m3,m4,m5
        path = "/api/messages?gid=100&after_ts=2000&after_id=2&limit=500"
        status, data = self._get_json(path)
        self.assertEqual(status, 200)
        mids = [m["mid"] for m in data["messages"]]
        self.assertEqual(mids, ["m3", "m4", "m5"])
        self.assertFalse(data["has_more_newer"])
        self.assertEqual(data["newest"], {"ts": 3000, "id": 5})

    def test_before_cursor_loads_older(self):
        # before 游标 = m4(ts=3000,id=4)，取更早：m1,m2,m3（升序）
        path = "/api/messages?gid=100&before_ts=3000&before_id=4&limit=500"
        status, data = self._get_json(path)
        self.assertEqual(status, 200)
        mids = [m["mid"] for m in data["messages"]]
        self.assertEqual(mids, ["m1", "m2", "m3"])
        self.assertFalse(data["has_more_older"])
        self.assertEqual(data["oldest"], {"ts": 1000, "id": 1})

    def test_limit_caps_results_and_has_more(self):
        # after=m1，limit=2，返回 m2,m3，has_more_newer=True
        path = "/api/messages?gid=100&after_ts=1000&after_id=1&limit=2"
        status, data = self._get_json(path)
        self.assertEqual(status, 200)
        mids = [m["mid"] for m in data["messages"]]
        self.assertEqual(mids, ["m2", "m3"])
        self.assertTrue(data["has_more_newer"])

    def test_sender_filter(self):
        # after=m1，只看 sender_id=2：m3,m5
        path = "/api/messages?gid=100&after_ts=1000&after_id=1&sender_id=2&limit=500"
        status, data = self._get_json(path)
        self.assertEqual(status, 200)
        mids = [m["mid"] for m in data["messages"]]
        self.assertEqual(mids, ["m3", "m5"])

    def test_no_cursor_returns_from_oldest(self):
        path = "/api/messages?gid=100&limit=500"
        status, data = self._get_json(path)
        self.assertEqual(status, 200)
        mids = [m["mid"] for m in data["messages"]]
        self.assertEqual(mids, ["m1", "m2", "m3", "m4", "m5"])
        self.assertFalse(data["has_more_older"])

    def test_empty_when_no_match(self):
        path = "/api/messages?gid=999&limit=500"
        status, data = self._get_json(path)
        self.assertEqual(status, 200)
        self.assertEqual(data["messages"], [])
        self.assertIsNone(data["oldest"])
        self.assertIsNone(data["newest"])
        self.assertFalse(data["has_more_older"])
        self.assertFalse(data["has_more_newer"])


class AnchorApiTest(_ServerTestBase):
    BASE = 1750113600000  # 2025-06-17 00:00 CST

    def make_data(self, conn):
        conn.execute("INSERT INTO groups(gid,name) VALUES(100,'群A')")
        base = self.BASE
        insert_messages(conn, [
            {"mid": "d1", "gid": 100, "sender_id": 1, "text": "1", "created_at": base + 1000},
            {"mid": "d2", "gid": 100, "sender_id": 1, "text": "2", "created_at": base + 2000},
            {"mid": "d3", "gid": 100, "sender_id": 2, "text": "3", "created_at": base + 3000},
            {"mid": "d4", "gid": 100, "sender_id": 1, "text": "4", "created_at": base + 86400000},  # 次日
        ])

    def test_by_date_returns_latest_of_day_ascending(self):
        # 2025-06-17 有 d1,d2,d3；取最新 limit 条（全部），升序返回
        path = "/api/messages/by_date?gid=100&date=2025-06-17&limit=500"
        status, data = self._get_json(path)
        self.assertEqual(status, 200)
        mids = [m["mid"] for m in data["messages"]]
        self.assertEqual(mids, ["d1", "d2", "d3"])
        self.assertEqual(data["newest"], {"ts": self.BASE + 3000, "id": 3})

    def test_by_date_limit_caps_to_latest(self):
        # limit=2：取最新 2 条（d2,d3），升序返回
        path = "/api/messages/by_date?gid=100&date=2025-06-17&limit=2"
        status, data = self._get_json(path)
        self.assertEqual(status, 200)
        mids = [m["mid"] for m in data["messages"]]
        self.assertEqual(mids, ["d2", "d3"])

    def test_by_date_sender_filter(self):
        path = "/api/messages/by_date?gid=100&date=2025-06-17&sender_id=2&limit=500"
        status, data = self._get_json(path)
        self.assertEqual(status, 200)
        mids = [m["mid"] for m in data["messages"]]
        self.assertEqual(mids, ["d3"])

    def test_around_anchors_at_mid(self):
        # 以 d3 为锚，取它及之前 limit 条，升序：d1,d2,d3
        path = "/api/messages/around?gid=100&mid=d3&limit=500"
        status, data = self._get_json(path)
        self.assertEqual(status, 200)
        mids = [m["mid"] for m in data["messages"]]
        self.assertEqual(mids, ["d1", "d2", "d3"])
        self.assertEqual(data["anchor_mid"], "d3")
        self.assertFalse(data["has_more_older"])

    def test_around_has_more_newer(self):
        # 以 d2 为锚：d1,d2 返回，d3/d4 在后 → has_more_newer True
        path = "/api/messages/around?gid=100&mid=d2&limit=500"
        status, data = self._get_json(path)
        self.assertEqual(status, 200)
        self.assertTrue(data["has_more_newer"])


if __name__ == "__main__":
    unittest.main()
