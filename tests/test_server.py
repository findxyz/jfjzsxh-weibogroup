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


if __name__ == "__main__":
    unittest.main()
