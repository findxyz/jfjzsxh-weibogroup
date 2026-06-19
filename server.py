"""微博群聊消息查看器 —— 本地只读 web 服务。

标准库实现，零外部依赖。只读打开 weibo_im.db，提供 JSON API 与静态前端。
启动：python server.py   访问：http://127.0.0.1:8765
"""
import argparse
import calendar
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


def _cst_month_bounds(month):
    """CST(+8) 某月（YYYY-MM）的 [start_ms, end_ms) 时间戳区间。

    created_at 存的是 UTC 毫秒，CST 整点零分对应 UTC-8h，故月首 CST 00:00
    的 UTC 毫秒 = (calendar.timegm(月首) - 8*3600) * 1000。end 为次月首，开区间。
    """
    y, m = map(int, month.split("-"))
    start_cst = calendar.timegm((y, m, 1, 0, 0, 0, 0, 0, 0))
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    end_cst = calendar.timegm((ny, nm, 1, 0, 0, 0, 0, 0, 0))
    return (start_cst - 8 * 3600) * 1000, (end_cst - 8 * 3600) * 1000


def _cst_day_bounds(date):
    """CST(+8) 某天（YYYY-MM-DD）的 [start_ms, end_ms) 时间戳区间。"""
    y, m, d = map(int, date.split("-"))
    start_cst = calendar.timegm((y, m, d, 0, 0, 0, 0, 0, 0))
    return (start_cst - 8 * 3600) * 1000, (start_cst - 8 * 3600 + 86400) * 1000

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
    """指定月份（YYYY-MM）的每日消息数，倒序返回。点击展开月份时按需查。

    用 CST 月区间 [start_ms, end_ms) 做 created_at 范围过滤，命中
    (gid, created_at) 复合索引；每日标签仍由 date() 表达式给出。
    """
    start_ms, end_ms = _cst_month_bounds(month)
    rows = conn.execute(
        "SELECT date(datetime(created_at/1000,'unixepoch','+8 hours')) AS d, "
        "COUNT(*) AS c FROM messages WHERE gid=? "
        "AND created_at>=? AND created_at<? "
        "GROUP BY d ORDER BY d DESC",
        (gid, start_ms, end_ms),
    ).fetchall()
    return [{"date": r["d"], "count": r["c"]} for r in rows]


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
    """检查指定方向是否还有更多消息。

    排序键仅为 created_at：同毫秒消息（多为抢红包）顺序不做保证，
    重复/省略不影响阅读。游标用 created_at 单值，命中 (gid,created_at)
    复合索引的范围扫描，无临时排序。
    """
    sql = (f"SELECT 1 FROM messages WHERE gid=? {sender_cond} {where_clause} "
           f"LIMIT 1")
    return conn.execute(sql, (gid,) + sender_params + params).fetchone() is not None


def query_messages(conn, gid, sender_name, before_ts, after_ts, limit):
    """双向游标分页。before/after 二选一，无则从最旧开始升序取 limit。

    排序键仅 created_at，游标用 created_at 单值。同毫秒消息（抢红包等）
    顺序不保证且可能在分页边界重复，符合预期。不用入库自增 id（其顺序
    与消息时间无关）。sender_name 非空时按 sender_name 精确匹配过滤。

    方向：before_ts（取更旧）需用 DESC LIMIT 取紧邻游标的 limit 条再
    反转为升序；after_ts/无游标用 ASC LIMIT。若 before 也用 ASC LIMIT，
    会取到整个范围内最旧的 limit 条（远早于游标），导致向上加载跳到很早
    的数据。
    """
    sender_cond = ""
    sender_params = ()
    if sender_name:
        sender_cond = "AND sender_name=?"
        sender_params = (sender_name,)

    where = ""
    params = ()
    order = "ASC"
    reverse = False
    if before_ts is not None:
        where = "AND created_at < ?"
        params = (before_ts,)
        order = "DESC"   # 取紧邻游标的 limit 条
        reverse = True   # 反转为升序返回
    elif after_ts is not None:
        where = "AND created_at > ?"
        params = (after_ts,)

    sql = (f"SELECT {MSG_COLUMNS} FROM messages "
           f"WHERE gid=? {sender_cond} {where} "
           f"ORDER BY created_at {order} LIMIT ?")
    rows = conn.execute(sql, (gid,) + sender_params + params + (limit,)).fetchall()
    msgs = [row_to_msg(r) for r in rows]
    if reverse:
        msgs.reverse()

    if not msgs:
        return {"messages": [], "oldest": None, "newest": None,
                "has_more_older": False, "has_more_newer": False}

    oldest = {"ts": msgs[0]["created_at"]}
    newest = {"ts": msgs[-1]["created_at"]}

    has_more_older = _has_more(
        conn, gid, sender_cond, sender_params,
        "AND created_at < ?", (oldest["ts"],))
    has_more_newer = _has_more(
        conn, gid, sender_cond, sender_params,
        "AND created_at > ?", (newest["ts"],))

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
    oldest = {"ts": msgs[0]["created_at"]}
    newest = {"ts": msgs[-1]["created_at"]}
    has_more_older = _has_more(
        conn, gid, sender_cond, sender_params,
        "AND created_at < ?", (oldest["ts"],))
    has_more_newer = _has_more(
        conn, gid, sender_cond, sender_params,
        "AND created_at > ?", (newest["ts"],))
    resp = {"messages": msgs, "oldest": oldest, "newest": newest,
            "has_more_older": has_more_older, "has_more_newer": has_more_newer}
    if anchor_mid is not None:
        resp["anchor_mid"] = anchor_mid
    return resp


def query_by_date(conn, gid, date, sender_name, limit):
    """取某 CST 日期最新 limit 条，反转为升序返回。

    sender_name 非空时按 sender_name 精确匹配过滤。
    用 CST 当日区间 [start_ms, end_ms) 做 created_at 范围过滤，命中
    (gid, created_at) 复合索引。
    """
    sender_cond = "AND sender_name=?" if sender_name else ""
    sender_params = (sender_name,) if sender_name else ()
    start_ms, end_ms = _cst_day_bounds(date)
    sql = (f"SELECT {MSG_COLUMNS} FROM messages "
           f"WHERE gid=? AND created_at>=? AND created_at<? "
           f"{sender_cond} ORDER BY created_at DESC LIMIT ?")
    rows = conn.execute(sql, (gid, start_ms, end_ms) + sender_params + (limit,)).fetchall()
    msgs = [row_to_msg(r) for r in rows]
    msgs.reverse()  # 反转为升序
    return _build_response(conn, msgs, gid, sender_cond, sender_params)


def query_around(conn, gid, mid, limit):
    """以 mid 对应消息为锚，取它之前 limit 条 + 锚点 + 之后 limit 条，升序返回。

    limit 为单侧条数（前后各取 limit 条，含锚点共约 2*limit+1 条），
    使命中消息大致位于返回列表中间，便于查看上下文。

    排序键仅 created_at：锚点之前取 created_at < 锚点时间（倒序 limit 条），
    锚点本身，之后取 created_at > 锚点时间（升序 limit 条）。同毫秒消息
    （抢红包等）顺序不保证，锚点同毫秒的其它消息可能落在前或后任一侧。
    """
    anchor = conn.execute(
        f"SELECT {MSG_COLUMNS} FROM messages WHERE mid=?", (mid,)).fetchone()
    if anchor is None:
        return _build_response(conn, [], gid, "", (), anchor_mid=mid)
    a = row_to_msg(anchor)
    # 锚点之前 limit 条（不含锚点及同毫秒），倒序取再反转为升序
    before_sql = (f"SELECT {MSG_COLUMNS} FROM messages WHERE gid=? "
                  f"AND created_at < ? "
                  f"ORDER BY created_at DESC LIMIT ?")
    before_rows = conn.execute(
        before_sql, (gid, a["created_at"], limit)).fetchall()
    # 锚点之后 limit 条（不含锚点及同毫秒），升序取
    after_sql = (f"SELECT {MSG_COLUMNS} FROM messages WHERE gid=? "
                 f"AND created_at > ? "
                 f"ORDER BY created_at ASC LIMIT ?")
    after_rows = conn.execute(
        after_sql, (gid, a["created_at"], limit)).fetchall()
    msgs = [row_to_msg(r) for r in before_rows]
    msgs.reverse()
    msgs += [a]
    msgs += [row_to_msg(r) for r in after_rows]
    return _build_response(conn, msgs, gid, "", (), anchor_mid=mid)


def _escape_like(s):
    """转义 LIKE 通配符 % _ \\，配合 ESCAPE '\\' 使用。"""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _snippet(text, q, span=30):
    """截取关键词前后各 span 字，关键词用 \\x00/\\x01 包裹供前端转 <mark>。

    q 为空（仅按发送者搜索）时返回文本前缀，不加高亮标记。
    """
    if not text:
        return ""
    if not q:
        return text[:span * 2]
    idx = text.find(q)
    if idx < 0:
        return text[:span * 2]
    start = max(0, idx - span)
    end = min(len(text), idx + len(q) + span)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return (prefix + text[start:idx] + "\x00" + q + "\x01"
            + text[idx + len(q):end] + suffix)


def query_search(conn, gid, q, sender_name, start_ts, end_ts, limit):
    """跨日期搜索消息。

    - 仅 q（模糊关键词）：text LIKE，按时间倒序取最新 limit 条。
    - 仅 sender_name（精确）：该发送者最新 limit 条。
    - 两者皆有：sender_name 精确 AND text LIKE。
    - 均空：返回空。
    时间范围 [start_ts, end_ts) 均为 CST 当日零点对应的毫秒，开区间。
    start_ts 为空表示不设下界，end_ts 为空表示不设上界。
    snippet 用 _snippet 生成（无 q 时返回文本前缀，无高亮标记）。
    """
    if not q and not sender_name:
        return {"results": []}
    conds = ["gid=?"]
    params = [gid]
    if start_ts is not None:
        conds.append("created_at >= ?")
        params.append(start_ts)
    if end_ts is not None:
        conds.append("created_at < ?")
        params.append(end_ts)
    if sender_name:
        conds.append("sender_name=?")
        params.append(sender_name)
    if q:
        like = "%" + _escape_like(q) + "%"
        conds.append("text LIKE ? ESCAPE '\\'")
        params.append(like)
    sql = (f"SELECT {MSG_COLUMNS} FROM messages WHERE "
           + " AND ".join(conds) +
           " ORDER BY created_at DESC LIMIT ?")
    rows = conn.execute(sql, params + [limit]).fetchall()
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
            elif path == "/api/messages":
                gid = int(qs.get("gid", ["0"])[0])
                sender_name = qs.get("sender_name", [""])[0] or ""
                limit = int(qs.get("limit", ["500"])[0])
                before_ts = _opt_int(qs, "before_ts")
                after_ts = _opt_int(qs, "after_ts")
                self._send_json(query_messages(
                    conn, gid, sender_name, before_ts, after_ts, limit))
            elif path == "/api/messages/by_date":
                gid = int(qs.get("gid", ["0"])[0])
                date = qs.get("date", [""])[0]
                sender_name = qs.get("sender_name", [""])[0] or ""
                limit = int(qs.get("limit", ["500"])[0])
                self._send_json(query_by_date(conn, gid, date, sender_name, limit))
            elif path == "/api/messages/around":
                gid = int(qs.get("gid", ["0"])[0])
                mid = qs.get("mid", [""])[0]
                limit = int(qs.get("limit", ["500"])[0])
                self._send_json(query_around(conn, gid, mid, limit))
            elif path == "/api/search":
                gid = int(qs.get("gid", ["0"])[0])
                q = qs.get("q", [""])[0]
                sender_name = qs.get("sender_name", [""])[0]
                start_ts = _opt_int(qs, "start_ts")
                end_ts = _opt_int(qs, "end_ts")
                limit = int(qs.get("limit", ["1000"])[0])
                self._send_json(query_search(
                    conn, gid, q, sender_name, start_ts, end_ts, limit))
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
