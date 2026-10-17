"""测试导出相关 API：成员发言导出、日报导出、token 下载、参数校验。

基于 _ServerTestBase 启动真实 server，构造临时小库。导出文件写入项目根
目录（export_*.json/csv、daily_*.csv），用 addCleanup 在测试结束后清理。
"""
import glob
import http.client
import json
import os
import unittest

from tests.conftest import make_test_db, insert_messages
import server


def _free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _ExportTestBase(unittest.TestCase):
    """导出测试基类：启动 server + 清理生成的导出文件。

    导出文件写在项目根目录（与 CLI 行为一致），文件名形如
    `export_<gid>_<sender>.json` / `daily_<gid>_<sender>_<date>.csv`。
    setUp 记录已有文件列表，tearDown 清理新增的。
    """

    def make_data(self, conn):
        pass

    def setUp(self):
        self.db_path = make_test_db()
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        self.make_data(conn)
        conn.close()
        self.port = _free_port()
        self.httpd = server.make_server("127.0.0.1", self.port, self.db_path)
        self.t = __import__("threading").Thread(
            target=self.httpd.serve_forever, daemon=True)
        self.t.start()
        # 记录已有导出文件，便于 tearDown 清理新增。
        # 导出文件写入系统临时目录下的 weibo_export 子目录，避免污染项目目录。
        import tempfile
        self._export_dir = os.path.join(tempfile.gettempdir(), "weibo_export")
        self._export_glob = [
            os.path.join(self._export_dir, "export_*"),
            os.path.join(self._export_dir, "daily_*"),
        ]
        self._pre_existing = set()
        for pat in self._export_glob:
            self._pre_existing.update(glob.glob(pat))

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.httpd.db_conn.close()
        # 关闭 export 模块 thread-local 连接，避免连接到已删除库
        try:
            from weibo_im import db as dbmod
            conn = getattr(dbmod._local, "conn", None)
            if conn is not None:
                conn.close()
                dbmod._local.conn = None
        except Exception:
            pass
        # 清理本测试生成的导出文件
        for pat in self._export_glob:
            for f in glob.glob(pat):
                if f not in self._pre_existing:
                    try:
                        os.remove(f)
                    except OSError:
                        pass
        # 删除临时库（含 WAL/SHM）
        for suffix in ("", "-wal", "-shm"):
            p = self.db_path + suffix
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
        # 清空 token 字典，避免跨用例污染
        server._EXPORT_TOKENS.clear()

    # ---- HTTP 辅助 ----
    def _get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        headers = {k.lower(): v for k, v in resp.getheaders()}
        conn.close()
        return resp.status, body, headers

    def _get_json(self, path):
        status, body, _ = self._get(path)
        return status, json.loads(body.decode("utf-8"))

    def _post_json(self, path, payload):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        body = json.dumps(payload).encode("utf-8")
        conn.request("POST", path, body=body,
                      headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, json.loads(data.decode("utf-8")) if data else {}


class ExportMemberApiTest(_ExportTestBase):
    BASE = 1750113600000  # 2025-06-17 00:00 CST

    def make_data(self, conn):
        conn.execute("INSERT INTO groups(gid,name) VALUES(100,'群A')")
        b = self.BASE
        insert_messages(conn, [
            {"mid": "e1", "gid": 100, "sender_id": 1, "sender_name": "甲",
             "msg_type": 321, "text": "hello", "created_at": b + 1000},
            {"mid": "e2", "gid": 100, "sender_id": 1, "sender_name": "甲",
             "msg_type": 321, "text": "world", "created_at": b + 2000},
            {"mid": "e3", "gid": 100, "sender_id": 2, "sender_name": "乙",
             "msg_type": 321, "text": "other", "created_at": b + 3000},
        ])

    def test_export_member_json(self):
        status, data = self._post_json("/api/export/member", {
            "gid": 100, "sender_name": "甲", "fmt": "json",
        })
        self.assertEqual(status, 200)
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["group_name"], "群A")
        self.assertTrue(data["start_cst"])
        self.assertTrue(data["end_cst"])
        self.assertTrue(data["download_token"])
        self.assertIn(".json", data["path"])
        # 预览取前 50 条
        self.assertEqual(data["preview_total"], 2)
        self.assertEqual(len(data["preview"]), 2)
        self.assertEqual(data["preview"][0]["sender_name"], "甲")
        self.assertEqual(data["preview"][0]["text"], "hello")

    def test_export_member_csv(self):
        status, data = self._post_json("/api/export/member", {
            "gid": 100, "sender_name": "甲", "fmt": "csv",
        })
        self.assertEqual(status, 200)
        self.assertEqual(data["count"], 2)
        self.assertIn(".csv", data["path"])
        # CSV 预览每行有 sender_name / text
        self.assertEqual(data["preview_total"], 2)
        self.assertEqual(data["preview"][0]["sender_name"], "甲")

    def test_export_member_by_sender_id(self):
        status, data = self._post_json("/api/export/member", {
            "gid": 100, "sender_id": 2, "fmt": "json",
        })
        self.assertEqual(status, 200)
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["preview"][0]["text"], "other")

    def test_export_member_time_range(self):
        # since=2025-06-17 (含), until=2025-06-17 (含当日，end=次日零点)
        # 甲在 b+1000, b+2000（都在 06-17）→ 都返回
        status, data = self._post_json("/api/export/member", {
            "gid": 100, "sender_name": "甲",
            "since": "2025-06-17", "until": "2025-06-17", "fmt": "json",
        })
        self.assertEqual(status, 200)
        self.assertEqual(data["count"], 2)

    def test_export_member_time_range_excludes(self):
        # until=2025-06-16（早于所有消息）→ 0 条
        status, data = self._post_json("/api/export/member", {
            "gid": 100, "sender_name": "甲",
            "until": "2025-06-16", "fmt": "json",
        })
        self.assertEqual(status, 200)
        self.assertEqual(data["count"], 0)
        self.assertEqual(data["preview"], [])
        # 空结果也应有合法 token 和文件
        self.assertTrue(data["download_token"])

    def test_export_member_empty_member(self):
        # 群内不存在的成员 → 0 条，仍生成文件
        status, data = self._post_json("/api/export/member", {
            "gid": 100, "sender_name": "不存在", "fmt": "json",
        })
        self.assertEqual(status, 200)
        self.assertEqual(data["count"], 0)
        self.assertEqual(data["preview"], [])

    def test_export_member_missing_gid(self):
        status, data = self._post_json("/api/export/member", {
            "sender_name": "甲", "fmt": "json",
        })
        self.assertEqual(status, 400)
        self.assertIn("gid", data["error"])

    def test_export_member_missing_sender(self):
        status, data = self._post_json("/api/export/member", {
            "gid": 100, "fmt": "json",
        })
        self.assertEqual(status, 400)
        self.assertIn("sender", data["error"])

    def test_export_member_invalid_fmt(self):
        status, data = self._post_json("/api/export/member", {
            "gid": 100, "sender_name": "甲", "fmt": "xml",
        })
        self.assertEqual(status, 400)
        self.assertIn("fmt", data["error"])

    def test_export_member_invalid_date(self):
        status, data = self._post_json("/api/export/member", {
            "gid": 100, "sender_name": "甲",
            "since": "not-a-date", "fmt": "json",
        })
        self.assertEqual(status, 400)
        self.assertIn("日期", data["error"])

    def test_export_member_compact(self):
        # compact=True 应生成可读性更差的紧凑 JSON（无缩进）
        status, data = self._post_json("/api/export/member", {
            "gid": 100, "sender_name": "甲", "fmt": "json", "compact": True,
        })
        self.assertEqual(status, 200)
        self.assertEqual(data["count"], 2)
        # 直接读取文件验证紧凑格式（无换行）
        token = data["download_token"]
        status, body, _ = self._get(
            f"/api/export/download?token={token}")
        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        # 紧凑 JSON 用 "," ":" 分隔，无缩进换行（数组内无 \n）
        # 至少应能被 json.loads
        parsed = json.loads(text)
        self.assertEqual(len(parsed), 2)


class ExportMemberNoiseFilterTest(_ExportTestBase):
    """验证成员发言导出过滤三类系统噪声：粉丝群提示 / 纯 URL / 「快来围观吧」。"""

    BASE = 1750113600000  # 2025-06-17 00:00 CST

    def make_data(self, conn):
        conn.execute("INSERT INTO groups(gid,name) VALUES(100,'群A')")
        b = self.BASE
        insert_messages(conn, [
            # 正常发言
            {"mid": "m1", "gid": 100, "sender_id": 1, "sender_name": "甲",
             "msg_type": 321, "text": "今天大盘怎么样", "created_at": b + 1000},
            # 纯 URL 网页分享（甲自己发的，但仍应过滤）
            {"mid": "m2", "gid": 100, "sender_id": 1, "sender_name": "甲",
             "msg_type": 100, "text": "http://t.cn/AXNj6YrQ",
             "created_at": b + 2000},
            # 「快来围观吧」自动提示（与 m2 成对）
            {"mid": "m3", "gid": 100, "sender_id": 1, "sender_name": "甲",
             "msg_type": 100, "text": "我发布了新微博，快来围观吧！",
             "created_at": b + 2001},
            # 正常发言
            {"mid": "m4", "gid": 100, "sender_id": 1, "sender_name": "甲",
             "msg_type": 321, "text": "继续看涨", "created_at": b + 3000},
            # 注意：粉丝群消息 sender_id=1（误填或改名），仍按 sender_name 过滤
            {"mid": "m5", "gid": 100, "sender_id": 1, "sender_name": "粉丝群",
             "msg_type": 321,
             "text": "恭喜陈同辉-宏观策略今日获得\"百灵\"标识",
             "created_at": b + 4000},
        ])

    def test_member_noise_filtered_out(self):
        status, data = self._post_json("/api/export/member", {
            "gid": 100, "sender_name": "甲", "fmt": "json",
        })
        self.assertEqual(status, 200)
        # 原始 5 条 → 过滤 3 条噪声（m2 纯URL / m3 快来围观吧 / m5 粉丝群）
        # 仅剩 m1 / m4 共 2 条
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["preview_total"], 2)
        texts = [r.get("text", "") for r in data["preview"]]
        self.assertNotIn("http://t.cn/AXNj6YrQ", texts)
        self.assertNotIn("我发布了新微博，快来围观吧！", texts)
        self.assertNotIn('恭喜陈同辉-宏观策略今日获得"百灵"标识', texts)
        self.assertIn("今天大盘怎么样", texts)
        self.assertIn("继续看涨", texts)
        # 时间范围应基于过滤后的消息
        self.assertTrue(data["start_cst"])
        self.assertTrue(data["end_cst"])

    def test_member_noise_filtered_csv(self):
        status, data = self._post_json("/api/export/member", {
            "gid": 100, "sender_name": "甲", "fmt": "csv",
        })
        self.assertEqual(status, 200)
        self.assertEqual(data["count"], 2)
        self.assertIn(".csv", data["path"])
        self.assertEqual(data["preview_total"], 2)
        for r in data["preview"]:
            self.assertNotIn("快来围观吧", r.get("text", ""))
            self.assertNotIn("粉丝群", r.get("sender_name", ""))


class ExportDailyApiTest(_ExportTestBase):
    BASE = 1750113600000  # 2025-06-17 00:00 CST

    def make_data(self, conn):
        conn.execute("INSERT INTO groups(gid,name) VALUES(100,'群A')")
        b = self.BASE
        insert_messages(conn, [
            {"mid": "d1", "gid": 100, "sender_id": 1, "sender_name": "甲",
             "msg_type": 321, "text": "早", "created_at": b + 1000},
            {"mid": "d2", "gid": 100, "sender_id": 2, "sender_name": "乙",
             "msg_type": 321, "text": "中", "created_at": b + 2000},
            {"mid": "d3", "gid": 100, "sender_id": 1, "sender_name": "甲",
             "msg_type": 321, "text": "晚", "created_at": b + 3000},
            # 次日，不应出现在 06-17 日报
            {"mid": "d4", "gid": 100, "sender_id": 1, "sender_name": "甲",
             "msg_type": 321, "text": "次日", "created_at": b + 86400000},
        ])

    def test_export_daily_with_target(self):
        status, data = self._post_json("/api/export/daily", {
            "gid": 100, "date_str": "2025-06-17", "sender_name": "甲",
        })
        self.assertEqual(status, 200)
        self.assertEqual(data["count"], 3)  # 当日全部 3 条
        self.assertEqual(data["target_count"], 2)  # 甲 2 条
        self.assertEqual(data["context_count"], 1)  # 乙 1 条
        self.assertEqual(data["date"], "2025-06-17")
        self.assertEqual(data["group_name"], "群A")
        self.assertEqual(data["target_sender_name"], "甲")
        self.assertTrue(data["download_token"])
        self.assertIn(".csv", data["path"])
        # 预览每行含 is_target 字段
        self.assertEqual(data["preview_total"], 3)
        target_rows = [r for r in data["preview"]
                       if str(r.get("is_target")) == "1"]
        self.assertEqual(len(target_rows), 2)

    def test_export_daily_no_target(self):
        # 不指定 sender → 仅导出当日上下文，所有 is_target=0
        status, data = self._post_json("/api/export/daily", {
            "gid": 100, "date_str": "2025-06-17",
        })
        self.assertEqual(status, 200)
        self.assertEqual(data["count"], 3)
        self.assertEqual(data["target_count"], 0)
        self.assertEqual(data["context_count"], 3)
        self.assertIsNone(data["target_sender_id"])
        # 所有预览行 is_target=0
        for r in data["preview"]:
            self.assertEqual(str(r.get("is_target")), "0")

    def test_export_daily_by_sender_id(self):
        status, data = self._post_json("/api/export/daily", {
            "gid": 100, "date_str": "2025-06-17", "sender_id": 2,
        })
        self.assertEqual(status, 200)
        self.assertEqual(data["target_count"], 1)  # 乙 1 条
        self.assertEqual(data["context_count"], 2)  # 甲 2 条

    def test_export_daily_empty_day(self):
        # 当日无消息
        status, data = self._post_json("/api/export/daily", {
            "gid": 100, "date_str": "2025-01-01", "sender_name": "甲",
        })
        self.assertEqual(status, 200)
        self.assertEqual(data["count"], 0)
        self.assertEqual(data["target_count"], 0)
        self.assertEqual(data["preview"], [])
        self.assertTrue(data["download_token"])

    def test_export_daily_missing_gid(self):
        status, data = self._post_json("/api/export/daily", {
            "date_str": "2025-06-17",
        })
        self.assertEqual(status, 400)
        self.assertIn("gid", data["error"])

    def test_export_daily_missing_date(self):
        status, data = self._post_json("/api/export/daily", {
            "gid": 100,
        })
        self.assertEqual(status, 400)
        self.assertIn("date_str", data["error"])

    def test_export_daily_invalid_date(self):
        status, data = self._post_json("/api/export/daily", {
            "gid": 100, "date_str": "not-a-date",
        })
        self.assertEqual(status, 400)


class ExportDailyNoiseFilterTest(_ExportTestBase):
    """验证日报导出过滤三类系统噪声：粉丝群提示 / 纯 URL / 「快来围观吧」。"""

    BASE = 1750113600000  # 2025-06-17 00:00 CST

    def make_data(self, conn):
        conn.execute("INSERT INTO groups(gid,name) VALUES(100,'群A')")
        b = self.BASE
        insert_messages(conn, [
            # 正常发言
            {"mid": "n1", "gid": 100, "sender_id": 1, "sender_name": "甲",
             "msg_type": 321, "text": "今天大盘怎么样", "created_at": b + 1000},
            # 粉丝群系统提示
            {"mid": "n2", "gid": 100, "sender_id": 999, "sender_name": "粉丝群",
             "msg_type": 321,
             "text": "涉及资金问题请务必提高警惕，谨防诈骗。查看案例",
             "created_at": b + 2000},
            # 粉丝群系统提示
            {"mid": "n3", "gid": 100, "sender_id": 999, "sender_name": "粉丝群",
             "msg_type": 321,
             "text": "恭喜 荷里活的鱼 升级至LV5 活跃",
             "created_at": b + 3000},
            # 纯 URL 网页分享
            {"mid": "n4", "gid": 100, "sender_id": 1, "sender_name": "甲",
             "msg_type": 100, "text": "http://t.cn/AXNj6YrQ",
             "created_at": b + 4000},
            # 「快来围观吧」自动提示（与上面 n4 成对）
            {"mid": "n5", "gid": 100, "sender_id": 1, "sender_name": "甲",
             "msg_type": 100, "text": "我发布了新微博，快来围观吧！",
             "created_at": b + 4001},
            # 正常发言
            {"mid": "n6", "gid": 100, "sender_id": 2, "sender_name": "乙",
             "msg_type": 321, "text": "看涨", "created_at": b + 5000},
            # 粉丝群系统提示（带额外文字也应被过滤）
            {"mid": "n7", "gid": 100, "sender_id": 999, "sender_name": "粉丝群",
             "msg_type": 321,
             "text": "恭喜陈同辉-宏观策略今日获得\"百灵\"标识",
             "created_at": b + 6000},
        ])

    def test_noise_filtered_out(self):
        status, data = self._post_json("/api/export/daily", {
            "gid": 100, "date_str": "2025-06-17", "sender_name": "甲",
        })
        self.assertEqual(status, 200)
        # 原始 7 条 → 过滤 5 条噪声（n2/n3/n4/n5/n7）→ 仅剩 n1/n6 共 2 条
        self.assertEqual(data["count"], 2)
        # 甲仅剩 n1（n4/n5 为纯URL/快来围观吧 已过滤）
        self.assertEqual(data["target_count"], 1)
        self.assertEqual(data["context_count"], 1)
        # 预览不含任何噪声文本
        texts = [r.get("text", "") for r in data["preview"]]
        self.assertNotIn("涉及资金问题请务必提高警惕", "".join(texts))
        self.assertNotIn("快来围观吧", "".join(texts))
        self.assertNotIn("http://t.cn/AXNj6YrQ", texts)
        self.assertIn("今天大盘怎么样", texts)
        self.assertIn("看涨", texts)
        # 预览中不应有 sender_name="粉丝群" 的行
        senders = [r.get("sender_name", "") for r in data["preview"]]
        self.assertNotIn("粉丝群", senders)

    def test_noise_filter_unit(self):
        """直接单元测试 _is_noise_message 三类规则。"""
        from weibo_im.export import _is_noise_message
        # 粉丝群系统提示
        self.assertTrue(_is_noise_message({"sender_name": "粉丝群",
                                           "text": "恭喜升级至LV5"}))
        # 纯 URL
        self.assertTrue(_is_noise_message({"sender_name": "甲",
                                           "text": "http://t.cn/AXNj6YrQ"}))
        self.assertTrue(_is_noise_message({"sender_name": "甲",
                                           "text": "https://weibo.com/123"}))
        # 快来围观吧
        self.assertTrue(_is_noise_message({"sender_name": "甲",
                                           "text": "我发布了新微博，快来围观吧！"}))
        # 正常发言不应被过滤
        self.assertFalse(_is_noise_message({"sender_name": "甲",
                                             "text": "今天大盘怎么样"}))
        self.assertFalse(_is_noise_message({"sender_name": "乙",
                                             "text": "看涨"}))
        # text 为空不算噪声
        self.assertFalse(_is_noise_message({"sender_name": "甲", "text": ""}))
        # sender_name 非粉丝群、文本含 URL 但非纯 URL 不应过滤
        self.assertFalse(_is_noise_message({"sender_name": "甲",
                                            "text": "看看这个 https://t.cn/abc 吧"}))


class ExportDownloadApiTest(_ExportTestBase):
    BASE = 1750113600000

    def make_data(self, conn):
        conn.execute("INSERT INTO groups(gid,name) VALUES(100,'群A')")
        b = self.BASE
        insert_messages(conn, [
            {"mid": "e1", "gid": 100, "sender_id": 1, "sender_name": "甲",
             "msg_type": 321, "text": "hello", "created_at": b + 1000},
        ])

    def test_download_with_valid_token(self):
        # 先导出拿 token
        status, data = self._post_json("/api/export/member", {
            "gid": 100, "sender_name": "甲", "fmt": "json",
        })
        self.assertEqual(status, 200)
        token = data["download_token"]

        # 下载
        status, body, headers = self._get(
            f"/api/export/download?token={token}")
        self.assertEqual(status, 200)
        # Content-Type 应为 application/json
        self.assertIn("application/json", headers.get("content-type", ""))
        # Content-Disposition: attachment; filename=...
        cd = headers.get("content-disposition", "")
        self.assertIn("attachment", cd)
        self.assertIn("filename", cd)
        # 文件内容应是合法 JSON 数组
        parsed = json.loads(body.decode("utf-8"))
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["text"], "hello")

    def test_download_with_invalid_token(self):
        status, body, _ = self._get("/api/export/download?token=nonexistent")
        self.assertEqual(status, 404)
        data = json.loads(body.decode("utf-8"))
        self.assertIn("token", data["error"])

    def test_download_with_empty_token(self):
        status, _, _ = self._get("/api/export/download?token=")
        self.assertEqual(status, 404)

    def test_download_token_reusable_within_ttl(self):
        # 同一 token 可多次下载（在 TTL 内）
        status, data = self._post_json("/api/export/member", {
            "gid": 100, "sender_name": "甲", "fmt": "json",
        })
        token = data["download_token"]
        # 第一次
        s1, _, _ = self._get(f"/api/export/download?token={token}")
        self.assertEqual(s1, 200)
        # 第二次（应仍可用）
        s2, body2, _ = self._get(f"/api/export/download?token={token}")
        self.assertEqual(s2, 200)
        self.assertTrue(len(body2) > 0)

    def test_download_expired_token(self):
        # 模拟 token 过期：手动改 created_at 为很久以前
        status, data = self._post_json("/api/export/member", {
            "gid": 100, "sender_name": "甲", "fmt": "json",
        })
        token = data["download_token"]
        # 篡改 created_at
        with server._export_tokens_lock:
            entry = server._EXPORT_TOKENS.get(token)
            if entry:
                entry["created_at"] = 0  # 很久以前
        status, _, _ = self._get(f"/api/export/download?token={token}")
        self.assertEqual(status, 404)


class ExportPageTest(_ExportTestBase):
    """导出页面静态文件可访问。"""

    def test_export_html_served(self):
        status, body, _ = self._get("/export.html")
        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        self.assertIn("<html", text)
        self.assertIn("数据导出", text)

    def test_export_css_served(self):
        status, body, _ = self._get("/web/export.css")
        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        self.assertIn("export-main", text)

    def test_export_js_served(self):
        status, body, _ = self._get("/web/export.js")
        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        self.assertIn("doExportMember", text)


if __name__ == "__main__":
    unittest.main()
