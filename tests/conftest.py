"""测试夹具：构造临时小数据库，结构与生产 messages/groups 表一致。"""
import os
import sqlite3
import tempfile

# 与生产 messages 表完全一致的建表语句（复制自 weibo_im.db 实际 schema）
MESSAGES_DDL = """
CREATE TABLE messages (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    mid              TEXT NOT NULL UNIQUE,
    gid              INTEGER NOT NULL,
    msg_type         INTEGER NOT NULL DEFAULT 321,
    msg_type_name    TEXT NOT NULL DEFAULT '',
    media_type       INTEGER DEFAULT 0,
    sender_id        INTEGER NOT NULL DEFAULT 0,
    sender_name      TEXT DEFAULT '',
    text             TEXT DEFAULT '',
    fid              TEXT DEFAULT '',
    media_orig_url   TEXT DEFAULT '',
    media_local_path TEXT DEFAULT '',
    url_objects      TEXT DEFAULT '',
    pic_infos        TEXT DEFAULT '',
    template         TEXT DEFAULT '',
    template_data    TEXT DEFAULT '{}',
    recall_mids      TEXT DEFAULT '[]',
    recall_by        TEXT DEFAULT '',
    attitude_data    TEXT DEFAULT '{}',
    faith_status     INTEGER DEFAULT 0,
    faith_icon       TEXT DEFAULT '',
    group_name       TEXT DEFAULT '',
    annotations      TEXT DEFAULT '{}',
    created_at       INTEGER NOT NULL,
    saved_at         INTEGER NOT NULL,
    raw_json         TEXT DEFAULT ''
)
"""

GROUPS_DDL = """
CREATE TABLE groups (
    gid            INTEGER PRIMARY KEY,
    name           TEXT NOT NULL DEFAULT '',
    avatar         TEXT DEFAULT '',
    round_avatar   TEXT DEFAULT '',
    member_count   INTEGER DEFAULT 0,
    max_member     INTEGER DEFAULT 0,
    owner_id       INTEGER DEFAULT 0,
    admins         TEXT DEFAULT '[]',
    summary        TEXT DEFAULT '',
    group_type     INTEGER DEFAULT 0,
    super_group_type INTEGER DEFAULT 0,
    status         INTEGER DEFAULT 0,
    validate_type  INTEGER DEFAULT 0,
    raw_json       TEXT DEFAULT '',
    created_at     INTEGER DEFAULT 0,
    updated_at     INTEGER DEFAULT 0,
    min_mid        TEXT DEFAULT '',
    max_mid        TEXT DEFAULT ''
)
"""

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

INDEXES_DDL = [
    "CREATE INDEX idx_msg_gid   ON messages(gid)",
    "CREATE INDEX idx_msg_mtype ON messages(msg_type)",
    "CREATE INDEX idx_msg_ctime ON messages(created_at)",
    "CREATE INDEX idx_msg_mid   ON messages(mid)",
    "CREATE INDEX idx_msg_fid   ON messages(fid)",
    "CREATE INDEX idx_mf_fid    ON media_files(fid)",
    "CREATE INDEX idx_mf_status ON media_files(status)",
]


def make_test_db():
    """创建一个临时文件 SQLite，建表建索引，返回 db 路径。

    调用方负责在测试结束后删除该文件（unittest 的 addCleanup 或 tmp 目录）。
    """
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.executescript(MESSAGES_DDL)
    conn.executescript(GROUPS_DDL)
    conn.executescript(MEDIA_FILES_DDL)
    conn.executescript(CONFIG_DDL)
    for ddl in INDEXES_DDL:
        conn.execute(ddl)
    conn.commit()
    conn.close()
    return path


def insert_messages(conn, rows):
    """批量插入消息。rows 是 list[dict]，缺失字段用默认值。"""
    cols = [
        "mid", "gid", "msg_type", "msg_type_name", "media_type",
        "sender_id", "sender_name", "text", "fid", "media_orig_url",
        "url_objects", "pic_infos", "template", "template_data",
        "recall_by", "group_name", "created_at", "saved_at",
    ]
    defaults = {c: "" for c in cols}
    defaults.update({"msg_type": 321, "media_type": 0, "sender_id": 0,
                     "template_data": "{}", "created_at": 0, "saved_at": 0})
    placeholders = ",".join("?" for _ in cols)
    sql = f"INSERT INTO messages ({','.join(cols)}) VALUES ({placeholders})"
    conn.executemany(sql, [[r.get(c, defaults[c]) for c in cols] for r in rows])
    conn.commit()


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
