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


def _opt_int(qs, key):
    v = qs.get(key, [None])[0]
    return int(v) if v not in (None, "") else None

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
    """按月聚合消息数，倒序返回。供左栏初始加载（只 ~24 行，不全表铺每日）。"""
    rows = conn.execute(
        "SELECT strftime('%Y-%m', datetime(created_at/1000,'unixepoch','+8 hours')) AS m, "
        "COUNT(*) AS c FROM messages WHERE gid=? "
        "GROUP BY m ORDER BY m DESC",
        (gid,),
    ).fetchall()
    return [{"month": r["m"], "count": r["c"]} for r in rows]


def query_month_days(conn, gid, month):
    """指定月份（YYYY-MM）的每日消息数，倒序返回。点击展开月份时按需查。"""
    rows = conn.execute(
        "SELECT date(datetime(created_at/1000,'unixepoch','+8 hours')) AS d, "
        "COUNT(*) AS c FROM messages WHERE gid=? "
        "AND strftime('%Y-%m', datetime(created_at/1000,'unixepoch','+8 hours'))=? "
        "GROUP BY d ORDER BY d DESC",
        (gid, month),
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


# messages 查询选取的列（供 row_to_msg 使用，保持一致）
MSG_COLUMNS = (
    "id, mid, gid, msg_type, msg_type_name, media_type, "
    "sender_id, sender_name, text, fid, media_orig_url, "
    "url_objects, pic_infos, template, template_data, recall_by, "
    "group_name, created_at"
)


def row_to_msg(r):
    return {
        "id": r["id"],
        "mid": r["mid"],
        "gid": r["gid"],
        "msg_type": r["msg_type"],
        "msg_type_name": r["msg_type_name"],
        "media_type": r["media_type"],
        "sender_id": r["sender_id"],
        "sender_name": r["sender_name"],
        "text": r["text"],
        "fid": r["fid"],
        "media_orig_url": r["media_orig_url"],
        "url_objects": r["url_objects"],
        "pic_infos": r["pic_infos"],
        "template": r["template"],
        "template_data": r["template_data"],
        "recall_by": r["recall_by"],
        "group_name": r["group_name"],
        "created_at": r["created_at"],
    }


def _has_more(conn, gid, sender_cond, sender_params, where_clause, params):
    """检查指定方向是否还有更多消息。"""
    sql = (f"SELECT 1 FROM messages WHERE gid=? {sender_cond} {where_clause} "
           f"LIMIT 1")
    return conn.execute(sql, (gid,) + sender_params + params).fetchone() is not None


def query_messages(conn, gid, sender_id, before_ts, before_id,
                   after_ts, after_id, limit):
    """双向游标分页。before/after 二选一，无则从最旧开始升序取 limit。"""
    sender_cond = ""
    sender_params = ()
    if sender_id:
        sender_cond = "AND sender_id=?"
        sender_params = (sender_id,)

    where = ""
    params = ()
    if before_ts is not None:
        where = ("AND (created_at < ? OR (created_at = ? AND id < ?))")
        params = (before_ts, before_ts, before_id)
    elif after_ts is not None:
        where = ("AND (created_at > ? OR (created_at = ? AND id > ?))")
        params = (after_ts, after_ts, after_id)

    sql = (f"SELECT {MSG_COLUMNS} FROM messages "
           f"WHERE gid=? {sender_cond} {where} "
           f"ORDER BY created_at ASC, id ASC LIMIT ?")
    rows = conn.execute(sql, (gid,) + sender_params + params + (limit,)).fetchall()
    msgs = [row_to_msg(r) for r in rows]

    if not msgs:
        return {"messages": [], "oldest": None, "newest": None,
                "has_more_older": False, "has_more_newer": False}

    oldest = {"ts": msgs[0]["created_at"], "id": msgs[0]["id"]}
    newest = {"ts": msgs[-1]["created_at"], "id": msgs[-1]["id"]}

    has_more_older = _has_more(
        conn, gid, sender_cond, sender_params,
        "AND (created_at < ? OR (created_at = ? AND id < ?))",
        (oldest["ts"], oldest["ts"], oldest["id"]))
    has_more_newer = _has_more(
        conn, gid, sender_cond, sender_params,
        "AND (created_at > ? OR (created_at = ? AND id > ?))",
        (newest["ts"], newest["ts"], newest["id"]))

    return {"messages": msgs, "oldest": oldest, "newest": newest,
            "has_more_older": has_more_older, "has_more_newer": has_more_newer}


def _build_response(conn, msgs, gid, sender_cond, sender_params, anchor_mid=None):
    """从升序 msgs 构造与 /api/messages 一致的响应结构。"""
    if not msgs:
        resp = {"messages": [], "oldest": None, "newest": None,
                "has_more_older": False, "has_more_newer": False}
        if anchor_mid is not None:
            resp["anchor_mid"] = anchor_mid
        return resp
    oldest = {"ts": msgs[0]["created_at"], "id": msgs[0]["id"]}
    newest = {"ts": msgs[-1]["created_at"], "id": msgs[-1]["id"]}
    has_more_older = _has_more(
        conn, gid, sender_cond, sender_params,
        "AND (created_at < ? OR (created_at = ? AND id < ?))",
        (oldest["ts"], oldest["ts"], oldest["id"]))
    has_more_newer = _has_more(
        conn, gid, sender_cond, sender_params,
        "AND (created_at > ? OR (created_at = ? AND id > ?))",
        (newest["ts"], newest["ts"], newest["id"]))
    resp = {"messages": msgs, "oldest": oldest, "newest": newest,
            "has_more_older": has_more_older, "has_more_newer": has_more_newer}
    if anchor_mid is not None:
        resp["anchor_mid"] = anchor_mid
    return resp


def query_by_date(conn, gid, date, sender_id, limit):
    """取某 CST 日期最新 limit 条，反转为升序返回。"""
    sender_cond = "AND sender_id=?" if sender_id else ""
    sender_params = (sender_id,) if sender_id else ()
    sql = (f"SELECT {MSG_COLUMNS} FROM messages "
           f"WHERE gid=? AND date(datetime(created_at/1000,'unixepoch','+8 hours'))=? "
           f"{sender_cond} ORDER BY created_at DESC, id DESC LIMIT ?")
    rows = conn.execute(sql, (gid, date) + sender_params + (limit,)).fetchall()
    msgs = [row_to_msg(r) for r in rows]
    msgs.reverse()  # 反转为升序
    return _build_response(conn, msgs, gid, sender_cond, sender_params)


def query_around(conn, gid, mid, limit):
    """以 mid 对应消息为锚，取它及之前 limit 条，反转为升序返回。"""
    anchor = conn.execute(
        f"SELECT {MSG_COLUMNS} FROM messages WHERE mid=?", (mid,)).fetchone()
    if anchor is None:
        return _build_response(conn, [], gid, "", (), anchor_mid=mid)
    a = row_to_msg(anchor)
    sql = (f"SELECT {MSG_COLUMNS} FROM messages WHERE gid=? "
           f"AND (created_at < ? OR (created_at = ? AND id <= ?)) "
           f"ORDER BY created_at DESC, id DESC LIMIT ?")
    rows = conn.execute(sql, (gid, a["created_at"], a["created_at"], a["id"], limit)).fetchall()
    msgs = [row_to_msg(r) for r in rows]
    msgs.reverse()
    return _build_response(conn, msgs, gid, "", (), anchor_mid=mid)


def _escape_like(s):
    """转义 LIKE 通配符 % _ \\，配合 ESCAPE '\\' 使用。"""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _snippet(text, q, span=30):
    """截取关键词前后各 span 字，关键词用 \\x00/\\x01 包裹供前端转 <mark>。"""
    if not text:
        return ""
    idx = text.find(q)
    if idx < 0:
        return text[:span * 2]
    start = max(0, idx - span)
    end = min(len(text), idx + len(q) + span)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return (prefix + text[start:idx] + "\x00" + q + "\x01"
            + text[idx + len(q):end] + suffix)


def query_search(conn, gid, q, days, limit):
    if not q:
        return {"results": []}
    # 该群最新消息时间作为范围上界基准
    max_row = conn.execute(
        "SELECT MAX(created_at) AS mx FROM messages WHERE gid=?", (gid,)).fetchone()
    max_ts = max_row["mx"] if max_row and max_row["mx"] else 0
    min_ts = max_ts - days * 86400000
    like = "%" + _escape_like(q) + "%"
    rows = conn.execute(
        f"SELECT {MSG_COLUMNS} FROM messages WHERE gid=? "
        f"AND created_at >= ? AND text LIKE ? ESCAPE '\\' "
        f"ORDER BY created_at DESC, id DESC LIMIT ?",
        (gid, min_ts, like, limit)).fetchall()
    results = []
    for r in rows:
        m = row_to_msg(r)
        results.append({
            "mid": m["mid"],
            "sender_id": m["sender_id"],
            "sender_name": m["sender_name"] or str(m["sender_id"]),
            "created_at": m["created_at"],
            "text": m["text"],
            "snippet": _snippet(m["text"], q),
        })
    return {"results": results}


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
                month = qs.get("month", [None])[0]
                if month:
                    self._send_json(query_month_days(conn, gid, month))
                else:
                    self._send_json(query_dates(conn, gid))
            elif path == "/api/senders":
                gid = int(qs.get("gid", ["0"])[0])
                self._send_json(query_senders(conn, gid))
            elif path == "/api/messages":
                gid = int(qs.get("gid", ["0"])[0])
                sender_id = int(qs.get("sender_id", ["0"])[0]) or 0
                limit = int(qs.get("limit", ["500"])[0])
                before_ts = _opt_int(qs, "before_ts")
                before_id = _opt_int(qs, "before_id")
                after_ts = _opt_int(qs, "after_ts")
                after_id = _opt_int(qs, "after_id")
                self._send_json(query_messages(
                    conn, gid, sender_id, before_ts, before_id,
                    after_ts, after_id, limit))
            elif path == "/api/messages/by_date":
                gid = int(qs.get("gid", ["0"])[0])
                date = qs.get("date", [""])[0]
                sender_id = int(qs.get("sender_id", ["0"])[0]) or 0
                limit = int(qs.get("limit", ["500"])[0])
                self._send_json(query_by_date(conn, gid, date, sender_id, limit))
            elif path == "/api/messages/around":
                gid = int(qs.get("gid", ["0"])[0])
                mid = qs.get("mid", [""])[0]
                limit = int(qs.get("limit", ["500"])[0])
                self._send_json(query_around(conn, gid, mid, limit))
            elif path == "/api/search":
                gid = int(qs.get("gid", ["0"])[0])
                q = qs.get("q", [""])[0]
                days = int(qs.get("days", ["90"])[0])
                limit = int(qs.get("limit", ["200"])[0])
                self._send_json(query_search(conn, gid, q, days, limit))
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
