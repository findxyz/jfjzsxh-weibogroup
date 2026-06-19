"""微博群聊消息查看器 —— 本地只读 web 服务。

标准库实现，零外部依赖。只读打开 weibo_im.db，提供 JSON API 与静态前端。
启动：python server.py   访问：http://127.0.0.1:8765
"""
import argparse
import json
import mimetypes
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

# ---------- 数据库 ----------

def open_db(db_path):
    """以只读模式打开 SQLite，返回连接。设置 row_factory 便于按列名取值。"""
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


# ---------- 查询函数 ----------

def query_groups(conn):
    rows = conn.execute(
        "SELECT g.gid, g.name, COUNT(m.id) AS msg_count "
        "FROM groups g LEFT JOIN messages m ON m.gid = g.gid "
        "GROUP BY g.gid ORDER BY msg_count DESC, g.gid"
    ).fetchall()
    return [{"gid": r["gid"], "name": r["name"], "msg_count": r["msg_count"]}
            for r in rows]


def query_dates(conn, gid):
    rows = conn.execute(
        "SELECT date(datetime(created_at/1000,'unixepoch','+8 hours')) AS d, "
        "COUNT(*) AS c FROM messages WHERE gid=? "
        "GROUP BY d ORDER BY d DESC",
        (gid,),
    ).fetchall()
    return [{"date": r["d"], "count": r["c"]} for r in rows]


def query_senders(conn, gid):
    rows = conn.execute(
        "SELECT sender_id, sender_name, COUNT(*) AS c "
        "FROM messages WHERE gid=? AND sender_id<>0 "
        "GROUP BY sender_id ORDER BY c DESC, sender_id",
        (gid,),
    ).fetchall()
    return [{"sender_id": r["sender_id"],
             "sender_name": r["sender_name"] or str(r["sender_id"]),
             "count": r["c"]} for r in rows]


# ---------- HTTP Handler ----------

class Handler(BaseHTTPRequestHandler):
    # 子类在 make_server 中注入 db_path 与 conn
    db_path = None

    def log_message(self, *args):
        pass  # 静默，避免刷屏

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, body, status=200, content_type="text/plain; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/":
            self._serve_static("index.html")
            return
        if path.startswith("/web/"):
            self._serve_static(path[len("/web/"):])
            return
        if path.startswith("/api/"):
            self._route_api(path, qs)
            return
        self._send_text("Not Found", status=404)

    def _route_api(self, path, qs):
        conn = self.conn
        try:
            if path == "/api/groups":
                self._send_json(query_groups(conn))
            elif path == "/api/dates":
                gid = int(qs.get("gid", ["0"])[0])
                self._send_json(query_dates(conn, gid))
            elif path == "/api/senders":
                gid = int(qs.get("gid", ["0"])[0])
                self._send_json(query_senders(conn, gid))
            else:
                self._send_json({"error": "not found"}, status=404)
        except Exception as e:
            self._send_json({"error": str(e)}, status=500)

    def _serve_static(self, rel):
        # 防目录穿越
        rel = rel.replace("\\", "/").lstrip("/")
        full = os.path.normpath(os.path.join(WEB_DIR, rel))
        if not full.startswith(os.path.normpath(WEB_DIR)):
            self._send_text("Forbidden", status=403)
            return
        if not os.path.isfile(full):
            self._send_text("Not Found", status=404)
            return
        ctype, _ = mimetypes.guess_type(full)
        with open(full, "rb") as f:
            self._send_text(f.read(), content_type=ctype or "application/octet-stream")


# ---------- 工厂 ----------

def make_server(host, port, db_path):
    """构造 ThreadingHTTPServer，把 db_path 绑到 Handler 类上。

    返回的 httpd 附带 .db_conn 属性，便于测试在 shutdown 后关闭连接。
    """
    Handler.db_path = db_path
    Handler.conn = open_db(db_path)
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.db_conn = Handler.conn
    return httpd


def main():
    parser = argparse.ArgumentParser(description="微博群聊消息查看器")
    default_db = os.path.join(os.path.dirname(os.path.abspath(__file__)), "weibo_im.db")
    parser.add_argument("--db", default=default_db, help="SQLite 数据库路径")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    httpd = make_server(args.host, args.port, args.db)
    print(f"查看器已启动：http://{args.host}:{args.port}  (db={args.db})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
        httpd.shutdown()


if __name__ == "__main__":
    main()
