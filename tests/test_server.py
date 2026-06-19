import http.client
import json
import os
import socket
import sqlite3
import threading
import unittest
from unittest import mock

from tests.conftest import make_test_db, insert_messages, insert_media_files, set_config
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

    def test_dates_returns_monthly(self):
        # 不带 month：按月聚合，倒序
        status, data = self._get_json("/api/dates?gid=100")
        self.assertEqual(status, 200)
        self.assertEqual(data, [{"month": "2025-06", "count": 3}])

    def test_dates_returns_days_for_month(self):
        # 带 month：该月每日条数，倒序
        status, data = self._get_json("/api/dates?gid=100&month=2025-06")
        self.assertEqual(status, 200)
        self.assertEqual(data, [
            {"date": "2025-06-18", "count": 1},
            {"date": "2025-06-17", "count": 2},
        ])


class MessagesCursorTest(_ServerTestBase):
    def make_data(self, conn):
        conn.execute("INSERT INTO groups(gid,name) VALUES(100,'群A')")
        # 5 条：created_at 严格递增（纯 created_at 排序，同毫秒顺序不保证，
        # 故用唯一时间戳使顺序确定，聚焦测游标方向而非 tiebreak）。
        # sender_id=1 → "甲"，sender_id=2 → "乙"，便于按名过滤测试
        insert_messages(conn, [
            {"mid": "m1", "gid": 100, "sender_id": 1, "sender_name": "甲", "text": "a", "created_at": 1000},
            {"mid": "m2", "gid": 100, "sender_id": 1, "sender_name": "甲", "text": "b", "created_at": 2000},
            {"mid": "m3", "gid": 100, "sender_id": 2, "sender_name": "乙", "text": "c", "created_at": 3000},
            {"mid": "m4", "gid": 100, "sender_id": 1, "sender_name": "甲", "text": "d", "created_at": 4000},
            {"mid": "m5", "gid": 100, "sender_id": 2, "sender_name": "乙", "text": "e", "created_at": 5000},
        ])

    def test_after_cursor_loads_newer(self):
        # after 游标 = ts=2000，取更晚（created_at>2000）：m3,m4,m5
        path = "/api/messages?gid=100&after_ts=2000&limit=500"
        status, data = self._get_json(path)
        self.assertEqual(status, 200)
        mids = [m["mid"] for m in data["messages"]]
        self.assertEqual(mids, ["m3", "m4", "m5"])
        self.assertFalse(data["has_more_newer"])
        self.assertEqual(data["newest"], {"ts": 5000})

    def test_before_cursor_loads_older(self):
        # before 游标 = ts=4000，取更早（created_at<4000）紧邻的：m1,m2,m3（升序）
        path = "/api/messages?gid=100&before_ts=4000&limit=500"
        status, data = self._get_json(path)
        self.assertEqual(status, 200)
        mids = [m["mid"] for m in data["messages"]]
        self.assertEqual(mids, ["m1", "m2", "m3"])
        self.assertFalse(data["has_more_older"])
        self.assertEqual(data["oldest"], {"ts": 1000})

    def test_before_returns_adjacent_not_oldest(self):
        # 关键回归：before 用 DESC LIMIT 取紧邻游标的 limit 条，而非全局最旧。
        # before=ts4000, limit=2 应返回 m2,m3（紧邻4000之下），不是 m1,m2（全局最旧）。
        path = "/api/messages?gid=100&before_ts=4000&limit=2"
        status, data = self._get_json(path)
        self.assertEqual(status, 200)
        mids = [m["mid"] for m in data["messages"]]
        self.assertEqual(mids, ["m2", "m3"])

    def test_limit_caps_results_and_has_more(self):
        # after=ts1000，limit=2，返回 m2,m3，has_more_newer=True
        path = "/api/messages?gid=100&after_ts=1000&limit=2"
        status, data = self._get_json(path)
        self.assertEqual(status, 200)
        mids = [m["mid"] for m in data["messages"]]
        self.assertEqual(mids, ["m2", "m3"])
        self.assertTrue(data["has_more_newer"])

    def test_sender_filter(self):
        # after=ts1000，只看 sender_name=乙：m3,m5
        from urllib.parse import quote
        path = f"/api/messages?gid=100&after_ts=1000&sender_name={quote('乙')}&limit=500"
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
            {"mid": "d1", "gid": 100, "sender_id": 1, "sender_name": "甲", "text": "1", "created_at": base + 1000},
            {"mid": "d2", "gid": 100, "sender_id": 1, "sender_name": "甲", "text": "2", "created_at": base + 2000},
            {"mid": "d3", "gid": 100, "sender_id": 2, "sender_name": "乙", "text": "3", "created_at": base + 3000},
            {"mid": "d4", "gid": 100, "sender_id": 1, "sender_name": "甲", "text": "4", "created_at": base + 86400000},  # 次日
        ])

    def test_by_date_returns_latest_of_day_ascending(self):
        # 2025-06-17 有 d1,d2,d3；取最新 limit 条（全部），升序返回
        path = "/api/messages/by_date?gid=100&date=2025-06-17&limit=500"
        status, data = self._get_json(path)
        self.assertEqual(status, 200)
        mids = [m["mid"] for m in data["messages"]]
        self.assertEqual(mids, ["d1", "d2", "d3"])
        self.assertEqual(data["newest"], {"ts": self.BASE + 3000})

    def test_by_date_limit_caps_to_latest(self):
        # limit=2：取最新 2 条（d2,d3），升序返回
        path = "/api/messages/by_date?gid=100&date=2025-06-17&limit=2"
        status, data = self._get_json(path)
        self.assertEqual(status, 200)
        mids = [m["mid"] for m in data["messages"]]
        self.assertEqual(mids, ["d2", "d3"])

    def test_by_date_sender_filter(self):
        from urllib.parse import quote
        path = f"/api/messages/by_date?gid=100&date=2025-06-17&sender_name={quote('乙')}&limit=500"
        status, data = self._get_json(path)
        self.assertEqual(status, 200)
        mids = [m["mid"] for m in data["messages"]]
        self.assertEqual(mids, ["d3"])

    def test_around_anchors_at_mid(self):
        # 以 d3 为锚，单侧 limit=500：之前 d1,d2 + 锚点 d3 + 之后 d4，升序
        path = "/api/messages/around?gid=100&mid=d3&limit=500"
        status, data = self._get_json(path)
        self.assertEqual(status, 200)
        mids = [m["mid"] for m in data["messages"]]
        self.assertEqual(mids, ["d1", "d2", "d3", "d4"])
        self.assertEqual(data["anchor_mid"], "d3")
        self.assertFalse(data["has_more_older"])
        self.assertFalse(data["has_more_newer"])

    def test_around_has_more_newer(self):
        # 以 d2 为锚，单侧 limit=1：之前 d1 + 锚点 d2 + 之后 d3（d4 被截断）
        # → [d1,d2,d3]，oldest=d1（前面无更早→has_more_older False），
        # newest=d3（后面还有 d4→has_more_newer True）
        path = "/api/messages/around?gid=100&mid=d2&limit=1"
        status, data = self._get_json(path)
        self.assertEqual(status, 200)
        mids = [m["mid"] for m in data["messages"]]
        self.assertEqual(mids, ["d1", "d2", "d3"])
        self.assertTrue(data["has_more_newer"])
        self.assertFalse(data["has_more_older"])


class SearchApiTest(_ServerTestBase):
    BASE = 1750113600000  # 2025-06-17 00:00 CST
    # 覆盖全部测试数据的宽区间：起=base前一天，止=base后一天（开区间用次日零点）
    START = BASE - 86400000          # 2025-06-16 00:00 CST
    END = BASE + 86400000            # 2025-06-18 00:00 CST（开区间，含 06-17 全天）

    def make_data(self, conn):
        conn.execute("INSERT INTO groups(gid,name) VALUES(100,'群A')")
        base = self.BASE
        insert_messages(conn, [
            {"mid": "s1", "gid": 100, "sender_id": 1, "sender_name": "甲",
             "text": "今天天气不错", "created_at": base},
            {"mid": "s2", "gid": 100, "sender_id": 2, "sender_name": "乙",
             "text": "天气真好啊天气", "created_at": base + 1000},
            {"mid": "s3", "gid": 100, "sender_id": 1, "sender_name": "甲",
             "text": "含通配符 50% 折扣", "created_at": base + 2000},
        ])

    def test_search_basic(self):
        from urllib.parse import quote
        path = f"/api/search?gid=100&q={quote('天气')}&start_ts={self.START}&end_ts={self.END}&limit=1000"
        status, data = self._get_json(path)
        self.assertEqual(status, 200)
        mids = [r["mid"] for r in data["results"]]
        self.assertEqual(mids, ["s2", "s1"])  # 倒序
        self.assertIn("天气", data["results"][0]["snippet"])

    def test_search_escapes_like_wildcards(self):
        # 搜 "50%"，% 应被转义为字面量，只匹配 s3
        from urllib.parse import quote
        path = f"/api/search?gid=100&q={quote('50%')}&start_ts={self.START}&end_ts={self.END}&limit=1000"
        status, data = self._get_json(path)
        self.assertEqual(status, 200)
        mids = [r["mid"] for r in data["results"]]
        self.assertEqual(mids, ["s3"])

    def test_search_no_match(self):
        from urllib.parse import quote
        path = f"/api/search?gid=100&q={quote('不存在')}&start_ts={self.START}&end_ts={self.END}&limit=1000"
        status, data = self._get_json(path)
        self.assertEqual(status, 200)
        self.assertEqual(data["results"], [])

    def test_search_range_filter(self):
        # 起止区间收紧到只含 s3（base+2000）：start=base+2000, end=base+2001
        # s1(base)、s2(base+1000) 被排除，s3 不含"天气" → 空
        from urllib.parse import quote
        path = (f"/api/search?gid=100&q={quote('天气')}"
                f"&start_ts={self.BASE+2000}&end_ts={self.BASE+2001}&limit=1000")
        status, data = self._get_json(path)
        self.assertEqual(status, 200)
        mids = [r["mid"] for r in data["results"]]
        self.assertEqual(mids, [])

    def test_search_no_range_returns_all(self):
        # 不传 start_ts/end_ts → 不限时间，返回全部匹配（按倒序）
        from urllib.parse import quote
        path = f"/api/search?gid=100&q={quote('天气')}&limit=1000"
        status, data = self._get_json(path)
        self.assertEqual(status, 200)
        mids = [r["mid"] for r in data["results"]]
        self.assertEqual(mids, ["s2", "s1"])

    def test_search_sender_only(self):
        # 仅 sender_name=甲（无关键词）：返回甲的全部消息，倒序 s3,s1
        from urllib.parse import quote
        path = f"/api/search?gid=100&sender_name={quote('甲')}&start_ts={self.START}&end_ts={self.END}&limit=1000"
        status, data = self._get_json(path)
        self.assertEqual(status, 200)
        mids = [r["mid"] for r in data["results"]]
        self.assertEqual(mids, ["s3", "s1"])

    def test_search_sender_and_keyword(self):
        # sender_name=甲 AND q=天气：只 s1（甲且含"天气"）
        from urllib.parse import quote
        path = (f"/api/search?gid=100&sender_name={quote('甲')}"
                f"&q={quote('天气')}&start_ts={self.START}&end_ts={self.END}&limit=1000")
        status, data = self._get_json(path)
        self.assertEqual(status, 200)
        mids = [r["mid"] for r in data["results"]]
        self.assertEqual(mids, ["s1"])

    def test_search_both_empty(self):
        # 既无 q 也无 sender_name → 空
        path = f"/api/search?gid=100&start_ts={self.START}&end_ts={self.END}&limit=1000"
        status, data = self._get_json(path)
        self.assertEqual(status, 200)
        self.assertEqual(data["results"], [])


class MediaApiTest(_ServerTestBase):
    def make_data(self, conn):
        set_config(conn, "weibo_cookie", "FAKE_COOKIE=1")

    def _get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        self._last_content_type = resp.getheader("Content-Type")
        conn.close()
        return resp.status, body

    def _get_json(self, path):
        status, body = self._get(path)
        return status, json.loads(body.decode("utf-8"))

    def test_media_not_found(self):
        status, body = self._get_json("/api/media/nonexistent_fid")
        self.assertEqual(status, 404)
        self.assertIn("not found", body["error"])

    def test_media_cached_returns_file(self):
        import tempfile
        fd, img_path = tempfile.mkstemp(suffix=".jpg")
        os.write(fd, b"\xff\xd8\xff\xe0FAKEJPEG")
        os.close(fd)
        self.addCleanup(os.remove, img_path)

        conn = sqlite3.connect(self.db_path)
        insert_media_files(conn, [{
            "fid": "img_cached", "gid": 1, "mid": "m1", "media_type": 1,
            "orig_url": "http://example.com/img", "local_path": img_path,
            "status": "done", "created_at": 0,
        }])
        conn.close()

        status, body = self._get("/api/media/img_cached")
        self.assertEqual(status, 200)
        self.assertIn("image/jpeg", self._last_content_type)
        self.assertTrue(body.startswith(b"\xff\xd8"))

    def test_media_download_on_demand(self):
        import tempfile
        fd, new_path = tempfile.mkstemp(suffix=".jpg")
        os.write(fd, b"\xff\xd8DOWNLOADED")
        os.close(fd)
        self.addCleanup(os.remove, new_path)

        conn = sqlite3.connect(self.db_path)
        insert_media_files(conn, [{
            "fid": "img_dl", "gid": 1, "mid": "m2", "media_type": 1,
            "orig_url": "http://example.com/img_dl", "local_path": "",
            "status": "pending", "created_at": 0,
        }])
        conn.close()

        with mock.patch("weibo_im.media.download_file",
                        return_value={"status": "done", "local_path": new_path,
                                      "file_size": 12, "md5": "abc"}):
            status, body = self._get("/api/media/img_dl")
        self.assertEqual(status, 200)
        self.assertIn("image/jpeg", self._last_content_type)

        # 验证 DB 已回写
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        r = conn.execute("SELECT local_path, status FROM media_files WHERE fid='img_dl'").fetchone()
        self.assertEqual(r["status"], "done")
        self.assertEqual(r["local_path"], new_path)
        conn.close()

    def test_media_download_fails(self):
        conn = sqlite3.connect(self.db_path)
        insert_media_files(conn, [{
            "fid": "img_fail", "gid": 1, "mid": "m3", "media_type": 1,
            "orig_url": "http://example.com/fail", "local_path": "",
            "status": "pending", "created_at": 0,
        }])
        conn.close()

        with mock.patch("weibo_im.media.download_file",
                        return_value={"status": "failed", "local_path": "",
                                      "file_size": 0, "md5": ""}):
            status, body = self._get_json("/api/media/img_fail")
        self.assertEqual(status, 404)
        self.assertIn("download failed", body["error"])

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        r = conn.execute("SELECT status FROM media_files WHERE fid='img_fail'").fetchone()
        self.assertEqual(r["status"], "failed")
        conn.close()

    def test_media_static_path_traversal(self):
        status, body = self._get("/media/../../../etc/passwd")
        self.assertIn(status, (403, 404))

    def test_media_static_serves_file(self):
        import os
        img_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "media", "images")
        os.makedirs(img_dir, exist_ok=True)
        fpath = os.path.join(img_dir, "_test_static.jpg")
        with open(fpath, "wb") as f:
            f.write(b"\xff\xd8STATIC")
        self.addCleanup(os.remove, fpath)

        status, body = self._get("/media/images/_test_static.jpg")
        self.assertEqual(status, 200)
        self.assertIn("image", self._last_content_type)


if __name__ == "__main__":
    unittest.main()
