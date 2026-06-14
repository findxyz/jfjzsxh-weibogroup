"""数据库设计 — SQLite + FTS5"""
from __future__ import annotations

import json
import time
import logging
import sqlite3
import threading
from typing import Any

from .types import (
    msg_type_name, msg_type_slug, media_type_name, is_redpacket,
)

log = logging.getLogger("weibo_im.db")

_local = threading.local()
_DB_PATH: str | None = None


def set_db_path(path: str):
    global _DB_PATH
    _DB_PATH = str(path)


def get_conn() -> sqlite3.Connection:
    if not hasattr(_local, "conn") or _local.conn is None:
        if not _DB_PATH:
            raise RuntimeError("DB path not set")
        conn = sqlite3.connect(_DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return _local.conn


def _ensure_group_mid_columns(conn):
    """为已有 groups 表补 min_mid/max_mid 列（表不存在则跳过，由后续 CREATE 处理）"""
    cols = set(r[1] for r in conn.execute("PRAGMA table_info(groups)").fetchall())
    if not cols:
        # 表还没建（首次初始化），CREATE TABLE 里已含 min_mid/max_mid，无需补
        return
    if "min_mid" not in cols:
        conn.execute("ALTER TABLE groups ADD COLUMN min_mid TEXT DEFAULT ''")
    if "max_mid" not in cols:
        conn.execute("ALTER TABLE groups ADD COLUMN max_mid TEXT DEFAULT ''")
    conn.commit()


def init_db():
    conn = get_conn()

    # 补 mid 范围列（仅对已存在的旧表生效；新表由 CREATE 语句直接含这两列）
    _ensure_group_mid_columns(conn)
    conn.executescript(f"""
        -- ── 配置表（key-value 存储，如 cookie）─────────────
        CREATE TABLE IF NOT EXISTS config (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL DEFAULT '',
            updated_at INTEGER NOT NULL DEFAULT 0
        );

        -- ── 群聊列表 ─────────────────────────────────────
        CREATE TABLE IF NOT EXISTS groups (
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
            created_at     INTEGER DEFAULT 0,   -- ms
            updated_at     INTEGER DEFAULT 0,
            min_mid        TEXT DEFAULT '',      -- 已存的最旧消息 mid
            max_mid        TEXT DEFAULT ''       -- 已存的最新消息 mid
        );

        -- ── 消息表 ───────────────────────────────────────
        CREATE TABLE IF NOT EXISTS messages (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            mid              TEXT NOT NULL UNIQUE,
            gid              INTEGER NOT NULL,

            -- 类型
            msg_type         INTEGER NOT NULL DEFAULT 321,
            msg_type_name    TEXT NOT NULL DEFAULT '',
            media_type       INTEGER DEFAULT 0,

            -- 发送者
            sender_id        INTEGER NOT NULL DEFAULT 0,
            sender_name      TEXT DEFAULT '',

            -- 文本内容
            text             TEXT DEFAULT '',

            -- 文件
            fid              TEXT DEFAULT '',      -- fids[0] 文件 ID
            media_orig_url   TEXT DEFAULT '',      -- 文件原始 URL
            media_local_path TEXT DEFAULT '',      -- 下载后的本地路径

            -- 结构化数据
            url_objects      TEXT DEFAULT '',      -- JSON: 卡片分享链接
            pic_infos        TEXT DEFAULT '',      -- JSON: 小程序图片
            template         TEXT DEFAULT '',      -- 系统消息模板文本
            template_data    TEXT DEFAULT '{{}}',  -- JSON: 模板变量
            recall_mids      TEXT DEFAULT '[]',    -- JSON: 被撤回的消息ID列表
            recall_by        TEXT DEFAULT '',      -- 撤回者
            attitude_data    TEXT DEFAULT '{{}}',  -- JSON: 点赞数据

            -- 元信息
            faith_status     INTEGER DEFAULT 0,
            faith_icon       TEXT DEFAULT '',
            group_name       TEXT DEFAULT '',
            annotations      TEXT DEFAULT '{{}}',  -- JSON

            -- 时间
            created_at       INTEGER NOT NULL,     -- 消息时间(ms)
            saved_at         INTEGER NOT NULL,     -- 入库时间(ms)

            -- 原始数据
            raw_json         TEXT DEFAULT ''
        );

        -- ── 已下载媒体文件 ───────────────────────────────
        CREATE TABLE IF NOT EXISTS media_files (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            fid             TEXT NOT NULL,
            gid             INTEGER DEFAULT 0,
            mid             TEXT DEFAULT '',
            media_type      INTEGER DEFAULT 0,
            orig_url        TEXT DEFAULT '',
            local_path      TEXT DEFAULT '',
            file_size       INTEGER DEFAULT 0,
            width           INTEGER DEFAULT 0,
            height          INTEGER DEFAULT 0,
            md5             TEXT DEFAULT '',
            status          TEXT DEFAULT 'pending',  -- pending/done/failed
            downloaded_at   INTEGER DEFAULT 0,
            created_at      INTEGER NOT NULL,
            UNIQUE(fid)
        );

        -- 索引
        CREATE INDEX IF NOT EXISTS idx_msg_gid   ON messages(gid);
        CREATE INDEX IF NOT EXISTS idx_msg_mtype ON messages(msg_type);
        CREATE INDEX IF NOT EXISTS idx_msg_ctime ON messages(created_at);
        CREATE INDEX IF NOT EXISTS idx_msg_mid   ON messages(mid);
        CREATE INDEX IF NOT EXISTS idx_msg_fid   ON messages(fid);
        CREATE INDEX IF NOT EXISTS idx_mf_fid    ON media_files(fid);
        CREATE INDEX IF NOT EXISTS idx_mf_status ON media_files(status);

        -- FTS5 全文搜索
        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
            text, sender_name, group_name,
            content='messages', content_rowid='id'
        );

        -- 触发器自动同步 FTS
        CREATE TRIGGER IF NOT EXISTS msg_ai AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(rowid, text, sender_name, group_name)
            VALUES (new.id, new.text, new.sender_name, new.group_name);
        END;

        CREATE TRIGGER IF NOT EXISTS msg_ad AFTER DELETE ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, text, sender_name, group_name)
            VALUES ('delete', old.id, old.text, old.sender_name, old.group_name);
        END;

        CREATE TRIGGER IF NOT EXISTS msg_au AFTER UPDATE ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, text, sender_name, group_name)
            VALUES ('delete', old.id, old.text, old.sender_name, old.group_name);
            INSERT INTO messages_fts(rowid, text, sender_name, group_name)
            VALUES (new.id, new.text, new.sender_name, new.group_name);
        END;
    """)
    conn.commit()


# ── 配置读写 ──────────────────────────────────────────────


def get_config(key: str, default: str = "") -> str:
    conn = get_conn()
    row = conn.execute(
        "SELECT value FROM config WHERE key=?", (key,)
    ).fetchone()
    return row[0] if row else default


def set_config(key: str, value: str):
    conn = get_conn()
    now = int(time.time() * 1000)
    conn.execute("""
        INSERT INTO config (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
    """, (key, value, now))
    conn.commit()


# ── cookie ────────────────────────────────────────────────


def get_cookie() -> str:
    return get_config("weibo_cookie", "")


def set_cookie(cookie: str):
    set_config("weibo_cookie", cookie)


def get_skip_gids() -> set[int]:
    """从 config 表读取跳过爬取的群 GID 集合"""
    raw = get_config("skip_gids", "")
    if not raw:
        return set()
    return {int(x) for x in raw.split(",") if x.strip()}


def set_skip_gids(gids: set[int]):
    """将跳过爬取的群 GID 集合写入 config 表"""
    value = ",".join(str(g) for g in sorted(gids))
    set_config("skip_gids", value)


def add_skip_gid(gid: int):
    """添加一个 GID 到跳过列表"""
    gids = get_skip_gids()
    gids.add(gid)
    set_skip_gids(gids)


def remove_skip_gid(gid: int):
    """从跳过列表移除一个 GID"""
    gids = get_skip_gids()
    gids.discard(gid)
    set_skip_gids(gids)


# ── 写入 ──────────────────────────────────────────────────


def save_groups(groups: list[dict]) -> int:
    """批量写入/更新群信息，返回处理条数"""
    conn = get_conn()
    now = int(time.time() * 1000)
    count = 0
    for g in groups:
        conn.execute("""
            INSERT INTO groups
                (gid, name, avatar, round_avatar, member_count, max_member,
                 owner_id, admins, summary, group_type, super_group_type,
                 status, validate_type, raw_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(gid) DO UPDATE SET
                name=excluded.name,
                avatar=COALESCE(excluded.avatar, groups.avatar),
                round_avatar=COALESCE(excluded.round_avatar, groups.round_avatar),
                member_count=COALESCE(excluded.member_count, groups.member_count),
                max_member=COALESCE(excluded.max_member, groups.max_member),
                owner_id=COALESCE(excluded.owner_id, groups.owner_id),
                admins=COALESCE(excluded.admins, groups.admins),
                summary=COALESCE(excluded.summary, groups.summary),
                group_type=COALESCE(excluded.group_type, groups.group_type),
                super_group_type=COALESCE(excluded.super_group_type, groups.super_group_type),
                status=COALESCE(excluded.status, groups.status),
                updated_at=excluded.updated_at
        """, (
            g.get("gid", 0),
            g.get("name", ""),
            g.get("avatar", ""),
            g.get("round_avatar", ""),
            g.get("member_count", 0),
            g.get("max_member", 0),
            g.get("owner_id", 0),
            json.dumps(g.get("admins", []), ensure_ascii=False),
            g.get("summary", ""),
            g.get("group_type", 0),
            g.get("super_group_type", 0),
            g.get("status", 0),
            g.get("validate_type", 0),
            g.get("raw_json", ""),
            now,
            now,
        ))
        count += 1
    conn.commit()
    return count


def _update_group_mid(conn, gid: int, mid: str):
    """更新群的 min_mid / max_mid 范围"""
    conn.execute("""
        UPDATE groups SET
            min_mid = CASE
                WHEN min_mid = '' OR min_mid > ? THEN ?
                ELSE min_mid
            END,
            max_mid = CASE
                WHEN max_mid = '' OR max_mid < ? THEN ?
                ELSE max_mid
            END
        WHERE gid = ?
    """, (mid, mid, mid, mid, gid))


def save_message(msg: dict) -> bool:
    """保存一条消息，已存在则忽略"""
    conn = get_conn()
    try:
        cursor = conn.execute("""
            INSERT OR IGNORE INTO messages
                (mid, gid, msg_type, msg_type_name, media_type,
                 sender_id, sender_name, text,
                 fid, media_orig_url, media_local_path,
                 url_objects, pic_infos,
                 template, template_data,
                 recall_mids, recall_by,
                 attitude_data,
                 faith_status, faith_icon, group_name, annotations,
                 created_at, saved_at, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            msg["mid"],
            msg.get("gid", 0),
            msg.get("msg_type", 321),
            msg.get("msg_type_name", ""),
            msg.get("media_type", 0),
            msg.get("sender_id", 0),
            msg.get("sender_name", ""),
            msg.get("text", ""),
            msg.get("fid", ""),
            msg.get("media_orig_url", ""),
            msg.get("media_local_path", ""),
            msg.get("url_objects", ""),
            msg.get("pic_infos", ""),
            msg.get("template", ""),
            msg.get("template_data", "{}"),
            msg.get("recall_mids", "[]"),
            msg.get("recall_by", ""),
            msg.get("attitude_data", "{}"),
            msg.get("faith_status", 0),
            msg.get("faith_icon", ""),
            msg.get("group_name", ""),
            msg.get("annotations", "{}"),
            int(msg.get("created_at", 0)),
            int(time.time() * 1000),
            msg.get("raw_json", ""),
        ))
        conn.commit()
        return cursor.rowcount > 0
    except Exception as e:
        log.debug("save_message error: %s", e)
        return False


def refresh_group_range(gid: int):
    """爬取完成后刷新群的 min_mid/max_mid 范围（从 messages 表实际数据计算）"""
    conn = get_conn()
    row = conn.execute(
        "SELECT MIN(mid), MAX(mid) FROM messages WHERE gid=?", (gid,)
    ).fetchone()
    if row and row[0]:
        conn.execute("""
            UPDATE groups SET min_mid=?, max_mid=? WHERE gid=?
        """, (row[0], row[1], gid))
        conn.commit()


def save_media_file(fid: str, gid: int = 0, mid: str = "",
                    media_type: int = 0, orig_url: str = "") -> bool:
    conn = get_conn()
    now = int(time.time() * 1000)
    try:
        conn.execute("""
            INSERT OR IGNORE INTO media_files
                (fid, gid, mid, media_type, orig_url, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (fid, gid, mid, media_type, orig_url, "pending", now))
        conn.commit()
        return True
    except Exception as e:
        log.debug("save_media_file error: %s", e)
        return False


def update_media_file(fid: str, **kwargs):
    conn = get_conn()
    sets = []
    vals = []
    for k, v in kwargs.items():
        if k in ("local_path", "file_size", "width", "height", "md5", "status", "downloaded_at"):
            sets.append(f"{k}=?")
            vals.append(v)
    if sets:
        vals.append(fid)
        conn.execute(f"UPDATE media_files SET {', '.join(sets)} WHERE fid=?", vals)
        conn.commit()


def update_media_status(fid: str, status: str, local_path: str = "",
                        file_size: int = 0, md5: str = ""):
    now = int(time.time() * 1000)
    update_media_file(fid, status=status, local_path=local_path,
                      file_size=file_size, md5=md5, downloaded_at=now)


def update_message_media_local_path(mid: str, local_path: str):
    conn = get_conn()
    conn.execute("UPDATE messages SET media_local_path=? WHERE mid=?", (local_path, mid))
    conn.commit()


def set_group_last_fetch(gid: int):
    conn = get_conn()
    now = int(time.time())
    conn.execute("UPDATE groups SET updated_at=? WHERE gid=?", (now * 1000, gid))
    conn.commit()


# ── 读取 ──────────────────────────────────────────────────


def get_group_list() -> list[dict]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM groups ORDER BY name").fetchall()
    return [dict(r) for r in rows]


def get_group_mids(gid: int) -> set[str]:
    """获取群已存储的全部 mid"""
    conn = get_conn()
    rows = conn.execute("SELECT mid FROM messages WHERE gid=?", (gid,)).fetchall()
    return {r[0] for r in rows}


def get_latest_mid(gid: int) -> str | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT mid FROM messages WHERE gid=? ORDER BY created_at DESC LIMIT 1",
        (gid,),
    ).fetchone()
    return row[0] if row else None


def get_group_mid_range(gid: int) -> tuple[str | None, str | None]:
    """获取群的 mid 范围 (min_mid, max_mid)"""
    conn = get_conn()
    row = conn.execute(
        "SELECT min_mid, max_mid FROM groups WHERE gid=?", (gid,)
    ).fetchone()
    if row and row[0]:
        return (row[0], row[1])
    return (None, None)


def get_pending_media(limit: int = 20) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM media_files WHERE status='pending' ORDER BY id LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_stats() -> dict:
    conn = get_conn()
    msgs = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    groups = conn.execute("SELECT COUNT(DISTINCT gid) FROM messages").fetchone()[0]
    groups_total = conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0]
    media_done = conn.execute("SELECT COUNT(*) FROM media_files WHERE status='done'").fetchone()[0]
    media_pending = conn.execute("SELECT COUNT(*) FROM media_files WHERE status='pending'").fetchone()[0]
    media_failed = conn.execute("SELECT COUNT(*) FROM media_files WHERE status='failed'").fetchone()[0]
    media_skipped = conn.execute("SELECT COUNT(*) FROM media_files WHERE status='skipped'").fetchone()[0]
    return {
        "messages": msgs,
        "groups_with_msgs": groups,
        "groups_total": groups_total,
        "media_done": media_done,
        "media_pending": media_pending,
        "media_failed": media_failed,
        "media_skipped": media_skipped,
    }
