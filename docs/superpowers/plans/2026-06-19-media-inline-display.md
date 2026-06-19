# 图片/视频内嵌显示与放大查看 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 聊天中的图片和视频在消息列表内以占位框展示，点击后后端按需下载（用 DB 中的微博 cookie 鉴权）并在 lightbox 中放大查看/内联播放。

**Architecture:** 新增 `/api/media/<fid>` 接口（按需下载 + 缓存 + 并发锁）和 `/media/` 静态服务；前端把图片/视频占位符改为可点击占位框，点击触发全局 lightbox（图片用 `<img>`，视频用 `<video controls autoplay>`），由 `/api/media/<fid>` 提供字节流。复用 `weibo_im.media.download_file()` 做实际下载。

**Tech Stack:** Python stdlib http.server、SQLite（只读）、requests（下载，已是项目依赖）、原生 JS/CSS（无框架）

**Spec:** `docs/superpowers/specs/2026-06-19-media-inline-display-design.md`

---

## File Structure

| 文件 | 责任 | 改动类型 |
|------|------|---------|
| `server.py` | 新增 `/api/media/<fid>` 路由、`/media/` 静态服务、cookie 读取、并发锁、Content-Type 推断 | 修改 |
| `tests/conftest.py` | 新增 `media_files` 与 `config` 表 DDL、`insert_media_files` 辅助 | 修改 |
| `tests/test_server.py` | 新增 `MediaApiTest` 测试类 | 修改 |
| `web/index.html` | 新增 lightbox DOM 容器 | 修改 |
| `web/style.css` | 占位框 + lightbox 样式 | 修改 |
| `web/app.js` | 改造 `renderMessageBody` 占位框、新增 lightbox 逻辑、事件委托 | 修改 |

---

## Task 1: 测试夹具支持 media_files 与 config 表

**Files:**
- Modify: `tests/conftest.py`

为媒体接口测试准备表结构。`media_files` DDL 复制自 `weibo_im/db.py:133-149`；`config` 表存 cookie。

- [ ] **Step 1: 在 conftest.py 顶部新增 MEDIA_FILES_DDL 与 CONFIG_DDL 常量**

在 `conftest.py` 的 `GROUPS_DDL` 常量之后、`INDEXES_DDL` 之前插入：

```python
MEDIA_FILES_DDL = """
CREATE TABLE media_files (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fid             TEXT NOT NULL UNIQUE,
    gid             INTEGER DEFAULT 0,
    mid             TEXT DEFAULT '',
    media_type      INTEGER DEFAULT 0,
    orig_url        TEXT DEFAULT '',
    local_path      TEXT DEFAULT '',
    file_size       INTEGER DEFAULT 0,
    width           INTEGER DEFAULT 0,
    height          INTEGER DEFAULT 0,
    md5             TEXT DEFAULT '',
    status          TEXT DEFAULT 'pending',
    downloaded_at   INTEGER DEFAULT 0,
    created_at      INTEGER NOT NULL
)
"""

CONFIG_DDL = """
CREATE TABLE config (
    key   TEXT PRIMARY KEY,
    value TEXT DEFAULT ''
)
"""
```

- [ ] **Step 2: 在 INDEXES_DDL 列表末尾追加 media_files 索引**

把 `INDEXES_DDL` 改为：

```python
INDEXES_DDL = [
    "CREATE INDEX idx_msg_gid   ON messages(gid)",
    "CREATE INDEX idx_msg_mtype ON messages(msg_type)",
    "CREATE INDEX idx_msg_ctime ON messages(created_at)",
    "CREATE INDEX idx_msg_mid   ON messages(mid)",
    "CREATE INDEX idx_msg_fid   ON messages(fid)",
    "CREATE INDEX idx_mf_fid    ON media_files(fid)",
    "CREATE INDEX idx_mf_status ON media_files(status)",
]
```

- [ ] **Step 3: 在 make_test_db() 中建 media_files 与 config 表**

在 `make_test_db()` 函数里，`conn.executescript(GROUPS_DDL)` 之后新增两行：

```python
    conn.executescript(MEDIA_FILES_DDL)
    conn.executescript(CONFIG_DDL)
```

- [ ] **Step 4: 在文件末尾新增 insert_media_files 辅助函数**

```python
def insert_media_files(conn, rows):
    """批量插入 media_files。rows 是 list[dict]，缺失字段用默认值。"""
    cols = [
        "fid", "gid", "mid", "media_type", "orig_url", "local_path",
        "file_size", "width", "height", "md5", "status",
        "downloaded_at", "created_at",
    ]
    defaults = {c: "" for c in cols}
    defaults.update({"gid": 0, "media_type": 0, "file_size": 0,
                     "width": 0, "height": 0, "status": "pending",
                     "downloaded_at": 0, "created_at": 0})
    placeholders = ",".join("?" for _ in cols)
    sql = f"INSERT INTO media_files ({','.join(cols)}) VALUES ({placeholders})"
    conn.executemany(sql, [[r.get(c, defaults[c]) for c in cols] for r in rows])
    conn.commit()


def set_config(conn, key, value):
    """写入 config 表一条记录（用于测试 cookie）。"""
    conn.execute(
        "INSERT OR REPLACE INTO config(key, value) VALUES (?, ?)", (key, value))
    conn.commit()
```

- [ ] **Step 5: 验证夹具可用**

运行：

```bash
python -c "from tests.conftest import make_test_db; import sqlite3; p=make_test_db(); c=sqlite3.connect(p); print([r[0] for r in c.execute(\"SELECT name FROM sqlite_master WHERE type='table'\")]); import os; os.remove(p)"
```

Expected: 输出包含 `messages`, `groups`, `media_files`, `config`

- [ ] **Step 6: 运行现有测试确认无回归**

Run: `python -m unittest tests.test_server -v`
Expected: 26 tests pass（夹具改动不应破坏现有测试）

- [ ] **Step 7: Commit**

```bash
git add tests/conftest.py
git commit -m "test: 夹具支持 media_files 与 config 表"
```

---

## Task 2: server.py — cookie 读取与 Content-Type 推断工具

**Files:**
- Modify: `server.py`

这两个纯函数是媒体接口的基础，先实现并可直接在后续测试中验证。

- [ ] **Step 1: 在 server.py 顶部 import 区新增 threading import**

把第 12 行的 import 改为（新增 `threading`）：

```python
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
```

下方（`from urllib.parse...` 之后）新增：

```python
import threading
```

- [ ] **Step 2: 在 open_db() 函数之后新增 get_cookie 函数**

在 `server.py` 的 `open_db` 函数（约第 49 行）之后新增：

```python
_COOKIE_CACHE = {"value": None}


def get_cookie(conn):
    """从 config 表读取 weibo_cookie，进程内缓存。无则返回空串。"""
    if _COOKIE_CACHE["value"] is None:
        row = conn.execute(
            "SELECT value FROM config WHERE key='weibo_cookie'").fetchone()
        _COOKIE_CACHE["value"] = row["value"] if row else ""
    return _COOKIE_CACHE["value"]
```

- [ ] **Step 3: 在 get_cookie 之后新增 _guess_content_type 函数**

```python
def _guess_content_type(path):
    """根据文件扩展名推断 Content-Type，用于媒体文件返回。"""
    ct, _ = mimetypes.guess_type(path)
    return ct or "application/octet-stream"
```

- [ ] **Step 4: 验证函数可导入**

Run:

```bash
python -c "import server; print(server.get_cookie.__name__, server._guess_content_type('x.jpg'), server._guess_content_type('x.mp4'))"
```

Expected: `get_cookie image/jpeg video/mp4`

- [ ] **Step 5: 运行现有测试确认无回归**

Run: `python -m unittest tests.test_server -v`
Expected: 26 tests pass

- [ ] **Step 6: Commit**

```bash
git add server.py
git commit -m "feat(server): cookie 读取与媒体 Content-Type 推断工具"
```

---

## Task 3: server.py — 媒体按需下载核心函数

**Files:**
- Modify: `server.py`
- Test: `tests/test_server.py`

实现 `serve_media(conn, fid)`：查表 → 命中缓存返回 local_path → 否则调 `download_file` 下载并回写。返回 `(local_path, None)` 成功或 `(None, error_msg)` 失败。下载部分用 mock 测试，不真正联网。

- [ ] **Step 1: 在 test_server.py 顶部新增 import**

把 test_server.py 顶部的 import 区（`import server` 之后）新增：

```python
from unittest import mock
```

- [ ] **Step 2: 写失败测试 — fid 不存在**

在 test_server.py 末尾新增 `MediaApiTest` 测试类：

```python
class MediaApiTest(_ServerTestBase):
    def make_data(self, conn):
        set_config(conn, "weibo_cookie", "FAKE_COOKIE=1")

    def test_media_not_found(self):
        status, body = self._get_json("/api/media/nonexistent_fid")
        self.assertEqual(status, 404)
        self.assertIn("not found", body["error"])
```

并在 test_server.py 顶部 import 行修改为同时导入 `set_config`：

```python
from tests.conftest import make_test_db, insert_messages, insert_media_files, set_config
```

- [ ] **Step 3: 运行测试确认失败**

Run: `python -m unittest tests.test_server.MediaApiTest.test_media_not_found -v`
Expected: FAIL（路由未实现，返回 404 但 error 是 "not found"——实际会命中 else 分支返回 404 `{"error":"not found"}`，需确认断言。若因路由未加而通过/失败，继续下一步实现）

- [ ] **Step 4: 在 server.py 实现 serve_media 函数**

在 `get_cookie` / `_guess_content_type` 之后新增：

```python
_MEDIA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "media")
_fid_locks = {}
_fid_locks_guard = threading.Lock()


def _get_fid_lock(fid):
    with _fid_locks_guard:
        if fid not in _fid_locks:
            _fid_locks[fid] = threading.Lock()
        return _fid_locks[fid]


def serve_media(conn, fid):
    """按需下载并返回媒体文件本地路径。

    返回 (local_path, None) 成功；返回 (None, error_msg) 失败。
    命中缓存（local_path 存在且文件在）直接返回；否则用 DB cookie 调
    weibo_im.media.download_file 下载，回写 media_files 与 messages。
    同一 fid 用进程内锁串行化，避免并发重复下载。
    """
    row = conn.execute(
        "SELECT media_type, orig_url, local_path, mid FROM media_files WHERE fid=?",
        (fid,)).fetchone()
    if row is None:
        return None, "media not found"

    # 命中缓存
    lp = row["local_path"]
    if lp and os.path.isfile(lp) and os.path.getsize(lp) > 0:
        return lp, None

    lock = _get_fid_lock(fid)
    with lock:
        # double-check：锁内再查一次，可能已被并发请求下载好
        row2 = conn.execute(
            "SELECT local_path FROM media_files WHERE fid=?", (fid,)).fetchone()
        lp2 = row2["local_path"] if row2 else ""
        if lp2 and os.path.isfile(lp2) and os.path.getsize(lp2) > 0:
            return lp2, None

        # 下载
        from weibo_im.media import download_file
        cookie = get_cookie(conn)
        if cookie:
            from weibo_im.media import set_cookie
            set_cookie(cookie)
        result = download_file(fid, row["orig_url"], row["media_type"])
        if result.get("status") != "done" or not result.get("local_path"):
            # 回写失败状态
            conn.execute(
                "UPDATE media_files SET status='failed' WHERE fid=?", (fid,))
            conn.commit()
            return None, "download failed"

        new_path = result["local_path"]
        conn.execute(
            "UPDATE media_files SET status='done', local_path=?, "
            "file_size=?, md5=? WHERE fid=?",
            (new_path, result.get("file_size", 0),
             result.get("md5", ""), fid))
        if row["mid"]:
            conn.execute(
                "UPDATE messages SET media_local_path=? WHERE mid=?",
                (new_path, row["mid"]))
        conn.commit()
        return new_path, None
```

- [ ] **Step 5: 在 _route_api 中新增 /api/media/<fid> 路由**

在 `_route_api` 方法里，`elif path == "/api/search":` 分支之后、`else:` 之前新增：

```python
            elif path.startswith("/api/media/"):
                fid = path[len("/api/media/"):]
                local_path, err = serve_media(conn, fid)
                if err:
                    self._send_json({"error": err}, status=404)
                else:
                    self._send_file(local_path)
```

并在 `do_GET` 中，`if path.startswith("/api/"):` 分支之前新增 `/media/` 静态服务分支：

```python
        if path.startswith("/media/"):
            self._serve_media_static(path)
            return
```

- [ ] **Step 6: 实现 _send_file 与 _serve_media_static 方法**

在 Handler 类的 `_send_text` 方法之后新增：

```python
    def _send_file(self, local_path, cache=True):
        with open(local_path, "rb") as f:
            data = f.read()
        ctype = _guess_content_type(local_path)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if cache:
            self.send_header("Cache-Control", "max-age=31536000")
        self.end_headers()
        self.wfile.write(data)

    def _serve_media_static(self, path):
        """服务 media/ 下已下载文件，防目录穿越。"""
        rel = path[len("/media/"):].replace("\\", "/").lstrip("/")
        full = os.path.normpath(os.path.join(_MEDIA_DIR, rel))
        if not full.startswith(os.path.normpath(_MEDIA_DIR) + os.sep):
            self._send_text("Forbidden", status=403)
            return
        if not os.path.isfile(full):
            self._send_text("Not Found", status=404)
            return
        self._send_file(full)
```

- [ ] **Step 7: 运行 test_media_not_found 确认通过**

Run: `python -m unittest tests.test_server.MediaApiTest.test_media_not_found -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add server.py tests/test_server.py
git commit -m "feat(server): /api/media/<fid> 按需下载接口 + /media/ 静态服务"
```

---

## Task 4: 媒体接口测试 — 缓存命中与下载成功

**Files:**
- Test: `tests/test_server.py`

- [ ] **Step 1: 在 MediaApiTest 中重写 _get/_get_json 以返回原始字节**

`_ServerTestBase._get` 返回 `body.decode("utf-8")`，但媒体接口返回二进制字节。在 `MediaApiTest` 类内 `make_data` 方法之后、其他测试方法之前，重写 `_get`（返回字节并记录 Content-Type）与 `_get_json`（解码为 JSON）：

```python
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
```

- [ ] **Step 2: 写测试 — 缓存命中直接返回文件**

在 `MediaApiTest` 中新增测试方法。准备一个真实小文件作为 local_path：

```python
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
```

- [ ] **Step 3: 运行测试确认通过（缓存命中已是实现行为）**

Run: `python -m unittest tests.test_server.MediaApiTest.test_media_cached_returns_file -v`
Expected: PASS

- [ ] **Step 4: 写测试 — 按需下载成功**

在 `MediaApiTest` 中新增（mock download_file 返回成功）：

```python
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
```

- [ ] **Step 5: 运行测试确认通过**

Run: `python -m unittest tests.test_server.MediaApiTest.test_media_download_on_demand -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add tests/test_server.py
git commit -m "test(server): 媒体接口缓存命中与按需下载成功"
```

---

## Task 5: 媒体接口测试 — 下载失败与静态服务防穿越

**Files:**
- Test: `tests/test_server.py`

- [ ] **Step 1: 写测试 — 下载失败返回 404 并回写 failed**

```python
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
```

- [ ] **Step 2: 写测试 — /media/ 静态服务路径穿越被拒**

```python
    def test_media_static_path_traversal(self):
        status, body = self._get("/media/../../../etc/passwd")
        self.assertIn(status, (403, 404))
```

- [ ] **Step 3: 写测试 — /media/ 静态服务正常返回文件**

```python
    def test_media_static_serves_file(self):
        import tempfile, os
        # 在 media/images 下放一个测试文件
        img_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "media", "images")
        os.makedirs(img_dir, exist_ok=True)
        fpath = os.path.join(img_dir, "_test_static.jpg")
        with open(fpath, "wb") as f:
            f.write(b"\xff\xd8STATIC")
        self.addCleanup(os.remove, fpath)

        status, body = self._get("/media/images/_test_static.jpg")
        self.assertEqual(status, 200)
        self.assertIn("image", self._last_content_type)
```

- [ ] **Step 4: 运行全部 MediaApiTest**

Run: `python -m unittest tests.test_server.MediaApiTest -v`
Expected: 5 tests pass（not_found, cached, download_on_demand, download_fails, static_traversal, static_serves_file —— 实为 6 项）

- [ ] **Step 5: 运行全部测试确认无回归**

Run: `python -m unittest tests.test_server -v`
Expected: 全部 pass（原 26 + 新增 6 = 32）

- [ ] **Step 6: Commit**

```bash
git add tests/test_server.py
git commit -m "test(server): 媒体下载失败回写与静态服务防穿越"
```

---

## Task 6: 前端 — lightbox DOM 与样式

**Files:**
- Modify: `web/index.html`
- Modify: `web/style.css`

- [ ] **Step 1: 在 index.html 的 </body> 前新增 lightbox 容器**

把 `web/index.html` 中的：

```html
  <script src="/web/app.js"></script>
</body>
```

改为：

```html
  <!-- 图片/视频放大查看 -->
  <div id="lightbox" class="lightbox hidden">
    <div class="lightbox-backdrop"></div>
    <div class="lightbox-content">
      <button class="lightbox-close" type="button" title="关闭">×</button>
      <div class="lightbox-stage"></div>
      <div class="lightbox-status"></div>
    </div>
  </div>

  <script src="/web/app.js"></script>
</body>
```

- [ ] **Step 2: 在 style.css 末尾新增占位框样式**

```css
/* 媒体占位框 */
.media-ph {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  max-width: 200px;
  padding: 24px 16px;
  border: 1px solid #e0e0e0;
  border-radius: 8px;
  background: #fafafa;
  color: #888;
  cursor: pointer;
  user-select: none;
  transition: background .15s, border-color .15s;
}
.media-ph:hover {
  background: #f0f0f0;
  border-color: #bbb;
}
.media-ph .media-icon {
  font-size: 20px;
}
```

- [ ] **Step 3: 在 style.css 末尾新增 lightbox 样式**

```css
/* Lightbox 放大查看 */
.lightbox {
  position: fixed;
  inset: 0;
  z-index: 1000;
  display: flex;
  align-items: center;
  justify-content: center;
}
.lightbox.hidden {
  display: none;
}
.lightbox-backdrop {
  position: absolute;
  inset: 0;
  background: rgba(0, 0, 0, .85);
}
.lightbox-content {
  position: relative;
  z-index: 1;
  max-width: 90vw;
  max-height: 90vh;
  display: flex;
  flex-direction: column;
  align-items: center;
}
.lightbox-stage img,
.lightbox-stage video {
  max-width: 90vw;
  max-height: 80vh;
  object-fit: contain;
}
.lightbox-close {
  position: absolute;
  top: -40px;
  right: -10px;
  font-size: 28px;
  line-height: 1;
  width: 36px;
  height: 36px;
  border: none;
  border-radius: 50%;
  background: rgba(255, 255, 255, .15);
  color: #fff;
  cursor: pointer;
}
.lightbox-close:hover {
  background: rgba(255, 255, 255, .3);
}
.lightbox-status {
  color: #ccc;
  font-size: 14px;
  margin-top: 12px;
  text-align: center;
}
.lightbox-status .retry-btn {
  margin-left: 8px;
  padding: 2px 10px;
  border: 1px solid #888;
  border-radius: 4px;
  background: transparent;
  color: #ccc;
  cursor: pointer;
}
.lightbox-status .retry-btn:hover {
  background: rgba(255, 255, 255, .1);
}
```

- [ ] **Step 4: 验证 HTML 可被服务**

Run:

```bash
python -c "import urllib.request; h=urllib.request.urlopen('http://127.0.0.1:8765/').read().decode(); print('lightbox' in h, 'media-ph' in h)"
```

（若服务器未运行，先 `python server.py` 后台启动）
Expected: `True True`（需重启服务器以加载新 HTML）

- [ ] **Step 5: Commit**

```bash
git add web/index.html web/style.css
git commit -m "feat(web): lightbox DOM 与占位框/lightbox 样式"
```

---

## Task 7: 前端 — 占位框渲染与事件委托

**Files:**
- Modify: `web/app.js`

- [ ] **Step 1: 改造 renderMessageBody 的图片/视频分支**

把 `web/app.js` 中 `renderMessageBody` 函数的：

```js
  if (mt === 1) return `🖼 [图片]${link}`;
  if (mt === 5) return `📎 [文件]${link}`;
  if (mt === 10) return `🎬 [视频]${link}`;
  if (mt === 13) {
    if ((m.text || "").includes("红包")) return `🧧 [红包]${link}`;
    return `🎬 [视频]${link}`;
  }
```

改为：

```js
  if (mt === 1) {
    return m.fid
      ? `<div class="media-ph" data-fid="${escapeHtml(m.fid)}" data-mtype="1"><span class="media-icon">🖼</span><span>图片</span></div>`
      : `🖼 [图片]${link}`;
  }
  if (mt === 5) return `📎 [文件]${link}`;
  if (mt === 10) {
    return m.fid
      ? `<div class="media-ph" data-fid="${escapeHtml(m.fid)}" data-mtype="10"><span class="media-icon">🎬</span><span>视频</span></div>`
      : `🎬 [视频]${link}`;
  }
  if (mt === 13) {
    if ((m.text || "").includes("红包")) return `🧧 [红包]${link}`;
    return m.fid
      ? `<div class="media-ph" data-fid="${escapeHtml(m.fid)}" data-mtype="10"><span class="media-icon">🎬</span><span>视频</span></div>`
      : `🎬 [视频]${link}`;
  }
```

（无 fid 的兜底走原文本占位，保持兼容。）

- [ ] **Step 2: 在 app.js DOM 引用区新增 lightbox 元素引用**

在 `const elSentinelBottom = $("sentinel-bottom");` 之后新增：

```js
const elLightbox = $("lightbox");
const elLbStage = document.querySelector(".lightbox-stage");
const elLbStatus = document.querySelector(".lightbox-status");
const elLbClose = document.querySelector(".lightbox-close");
const elLbBackdrop = document.querySelector(".lightbox-backdrop");
```

- [ ] **Step 3: 新增 lightbox 逻辑函数**

在 `setupSentinels()` 函数之前新增：

```js
// ---------- 图片/视频放大查看 ----------
let lbCurrent = null; // 'img' | 'video' | null

function openLightbox(loading) {
  elLbStage.innerHTML = "";
  elLbStatus.textContent = loading ? "加载中…" : "";
  elLbStatus.className = "lightbox-status";
  elLightbox.classList.remove("hidden");
}

function closeLightbox() {
  const v = elLbStage.querySelector("video");
  if (v) { v.pause(); v.removeAttribute("src"); v.load(); }
  elLbStage.innerHTML = "";
  elLbStatus.textContent = "";
  elLbStatus.className = "lightbox-status";
  elLightbox.classList.add("hidden");
  lbCurrent = null;
}

function openImage(fid) {
  lbCurrent = "img";
  openLightbox(true);
  const img = new Image();
  img.onload = () => {
    if (lbCurrent !== "img") return;
    elLbStage.innerHTML = "";
    elLbStage.appendChild(img);
    elLbStatus.textContent = "";
  };
  img.onerror = () => {
    if (lbCurrent !== "img") return;
    elLbStage.innerHTML = "";
    elLbStatus.innerHTML = "加载失败<button class='retry-btn' type='button'>重试</button>";
    elLbStatus.querySelector(".retry-btn").onclick = () => openImage(fid);
  };
  img.src = `/api/media/${encodeURIComponent(fid)}?t=${Date.now()}`;
}

function openVideo(fid) {
  lbCurrent = "video";
  openLightbox(true);
  const v = document.createElement("video");
  v.controls = true;
  v.autoplay = true;
  v.oncanplay = () => {
    if (lbCurrent !== "video") return;
    elLbStatus.textContent = "";
  };
  v.onerror = () => {
    if (lbCurrent !== "video") return;
    elLbStage.innerHTML = "";
    elLbStatus.innerHTML = "加载失败<button class='retry-btn' type='button'>重试</button>";
    elLbStatus.querySelector(".retry-btn").onclick = () => openVideo(fid);
  };
  v.src = `/api/media/${encodeURIComponent(fid)}?t=${Date.now()}`;
  elLbStage.innerHTML = "";
  elLbStage.appendChild(v);
}
```

- [ ] **Step 4: 新增事件委托与关闭绑定**

在文件底部事件绑定区（`elGroup.onchange = ...` 之前）新增：

```js
// 媒体占位框点击（事件委托）
elMsgList.addEventListener("click", (e) => {
  const ph = e.target.closest(".media-ph");
  if (!ph) return;
  const fid = ph.dataset.fid;
  const mtype = ph.dataset.mtype;
  if (mtype === "1") openImage(fid);
  else if (mtype === "10") openVideo(fid);
});

// lightbox 关闭
elLbClose.onclick = closeLightbox;
elLbBackdrop.addEventListener("click", closeLightbox);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !elLightbox.classList.contains("hidden")) closeLightbox();
});
```

- [ ] **Step 5: 验证 JS 语法与服务**

Run:

```bash
node --check web/app.js && echo "syntax ok"
```

（若无 node，跳过；启动服务器后浏览器控制台确认无报错）

- [ ] **Step 6: 手动验证（需运行服务器）**

重启服务器后浏览器 Ctrl+F5：
1. 选择群、选一天有图片消息的日期 → 列表显示占位框
2. 点击图片占位框 → lightbox 弹出，loading 后显示大图
3. 点击视频占位框 → lightbox 弹出，视频可播放
4. ESC / 点遮罩 / 点 × → 关闭
5. 再次点击同一图 → 秒开（命中缓存）

- [ ] **Step 7: Commit**

```bash
git add web/app.js
git commit -m "feat(web): 占位框渲染 + lightbox 图片放大/视频播放"
```

---

## Task 8: 端到端验证与清理

**Files:** 无（验证 + 收尾）

- [ ] **Step 1: 运行全部后端测试**

Run: `python -m unittest tests.test_server -v`
Expected: 全部 pass（32 项）

- [ ] **Step 2: 重启服务器并验证媒体接口**

```bash
# 杀掉旧服务器（Windows）
powershell -Command "Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }"
# 后台启动
python server.py
```

验证一个真实图片 fid：

```bash
python -c "import urllib.request; r=urllib.request.urlopen('http://127.0.0.1:8765/api/media/5309760582717320'); print(r.status, r.getheader('Content-Type'))"
```

Expected: `200 image/jpeg`（首次可能需几秒下载）

- [ ] **Step 3: 验证缓存命中（第二次秒开）**

再次请求同一 fid，确认 DB 已回写：

```bash
python -c "import sqlite3; c=sqlite3.connect('weibo_im.db'); print(c.execute(\"SELECT status, local_path FROM media_files WHERE fid='5309760582717320'\").fetchone())"
```

Expected: `('done', '...\\media\\images\\5309760582717320.jpg')`

- [ ] **Step 4: 浏览器端到端验证**

Ctrl+F5 刷新，按 Task 7 Step 6 清单验证图片/视频点击放大播放。

- [ ] **Step 5: 清理临时文件**

```bash
rm -f nul
git status
```

Expected: 工作区干净（除预期的改动已提交外）

- [ ] **Step 6: 最终提交（如有遗漏改动）**

```bash
git add -A
git commit -m "chore: 媒体内嵌显示端到端验证" --allow-empty
```

（无改动则跳过）
