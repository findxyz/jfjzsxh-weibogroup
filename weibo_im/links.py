"""链接文件下载 — 检测并下载外部链接指向的可下载文件（PDF/ZIP 等）"""
from __future__ import annotations

import os
import re
import time
import json
import hashlib
import logging
from pathlib import Path
from urllib.parse import urlparse

import requests
import urllib3

from .db import get_conn

urllib3.disable_warnings()
log = logging.getLogger("weibo_im.links")

# 下载根目录 — 文件存在 <project>/media/files/ 下
FILES_ROOT = Path(__file__).resolve().parent.parent / "media" / "files"

# 识别的文件类型
FILE_EXTENSIONS = {
    ".pdf": "pdf",
    ".zip": "zip",
    ".rar": "rar",
    ".7z": "7z",
    ".tar": "tar",
    ".gz": "gzip",
    ".doc": "doc",
    ".docx": "docx",
    ".xls": "xls",
    ".xlsx": "xlsx",
    ".ppt": "ppt",
    ".pptx": "pptx",
    ".txt": "text",
    ".csv": "csv",
    ".apk": "apk",
    ".exe": "exe",
    ".dmg": "dmg",
}

# Content-Type 到文件类型的映射（HEAD 请求用）
CONTENT_TYPE_MAP: dict[str, str] = {
    "application/pdf": "pdf",
    "application/zip": "zip",
    "application/x-rar-compressed": "rar",
    "application/x-7z-compressed": "7z",
    "application/gzip": "gzip",
    "application/x-gzip": "gzip",
    "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.ms-excel": "xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.ms-powerpoint": "ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/vnd.android.package-archive": "apk",
    "application/x-msdownload": "exe",
}

_COOKIE: str = ""
_HEADERS: dict = {}


def get_cookie_or_default() -> str:
    global _COOKIE
    if not _COOKIE:
        from .db import get_cookie
        _COOKIE = get_cookie()
    return _COOKIE


def set_cookie(c: str):
    global _COOKIE, _HEADERS
    _COOKIE = c
    _HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }


URL_PATTERN = re.compile(r'https?://[^\s，。、\'\"\]\)）】〞〟»›»]+')


def extract_urls(text: str) -> list[str]:
    """从文本中提取 URL"""
    return [m.group().rstrip(")）") for m in URL_PATTERN.finditer(text)]


def resolve_tcn(url: str, timeout: int = 10) -> str:
    """解析 t.cn 短链，返回真实 URL。非 t.cn 直接返回原 URL。"""
    if "t.cn" not in url:
        return url
    try:
        h = dict(_HEADERS) if _HEADERS else {}
        ck = get_cookie_or_default()
        if ck:
            h["Cookie"] = ck
        resp = requests.head(url, headers=h, timeout=timeout,
                             allow_redirects=True, verify=False)
        return resp.url or url
    except Exception:
        return url


def is_downloadable_file(url: str) -> tuple[str, str] | None:
    """检查 URL 是否指向可下载文件，返回 (文件类型, 真实URL) 或 None"""
    # 1. 先看后缀
    lower = url.lower().rstrip("/")
    for ext, ftype in FILE_EXTENSIONS.items():
        if lower.endswith(ext):
            # 去掉可能的 query string
            parsed = urlparse(lower)
            if parsed.path.endswith(ext):
                return (ftype, url)

    # 2. 没有后缀，发 HEAD 请求检查 Content-Type
    try:
        h = dict(_HEADERS) if _HEADERS else {}
        ck = get_cookie_or_default()
        if ck:
            h["Cookie"] = ck
        resp = requests.head(url, headers=h, timeout=10,
                             allow_redirects=True, verify=False)
        ct = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if ct in CONTENT_TYPE_MAP:
            return (CONTENT_TYPE_MAP[ct], resp.url or url)
    except Exception:
        pass

    return None


def download_link_file(url: str, file_type: str, mid: str = "",
                       gid: int = 0, timeout: int = 120) -> dict:
    """下载链接文件，返回 {status, local_path, file_size, md5}"""
    FILES_ROOT.mkdir(parents=True, exist_ok=True)

    # 用 URL 的 MD5 作为文件名（避免长文件名问题）
    url_hash = hashlib.md5(url.encode()).hexdigest()[:16]
    filename = f"{url_hash}.{file_type}"
    local_path = str(FILES_ROOT / filename)

    if os.path.isfile(local_path) and os.path.getsize(local_path) > 0:
        return {
            "status": "done", "local_path": local_path,
            "file_size": os.path.getsize(local_path),
            "md5": _calc_md5(local_path),
        }

    try:
        h = dict(_HEADERS) if _HEADERS else {}
        ck = get_cookie_or_default()
        if ck:
            h["Cookie"] = ck
        log.info("下载链接文件: %s", url)
        resp = requests.get(url, headers=h, timeout=timeout,
                            stream=True, verify=False)
        if resp.status_code != 200:
            log.warning("链接文件下载失败 [HTTP %d]: %s", resp.status_code, url)
            return {"status": "failed", "local_path": "", "file_size": 0, "md5": ""}

        file_size = 0
        with open(local_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
                    file_size += len(chunk)

        md5 = _calc_md5(local_path)
        log.info("链接文件下载完成: %s (%d bytes)", local_path, file_size)

        # 存入 DB
        _save_link_file(gid, mid, url, url_hash, file_type,
                        local_path, file_size, md5, "done")
        return {"status": "done", "local_path": local_path,
                "file_size": file_size, "md5": md5}

    except Exception as e:
        log.error("链接文件下载异常: %s — %s", url, e)
        _save_link_file(gid, mid, url, url_hash, file_type,
                        "", 0, "", "failed")
        return {"status": "failed", "local_path": "", "file_size": 0, "md5": ""}


def _save_link_file(gid: int, mid: str, url: str, url_hash: str,
                    file_type: str, local_path: str,
                    file_size: int, md5: str, status: str):
    """写入 link_files 表"""
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS link_files (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            url_hash  TEXT NOT NULL,
            url       TEXT NOT NULL,
            gid       INTEGER DEFAULT 0,
            mid       TEXT DEFAULT '',
            file_type TEXT DEFAULT '',
            local_path TEXT DEFAULT '',
            file_size INTEGER DEFAULT 0,
            md5       TEXT DEFAULT '',
            status    TEXT DEFAULT 'pending',
            created_at INTEGER NOT NULL,
            UNIQUE(url_hash)
        )
    """)
    now = int(time.time() * 1000)
    conn.execute("""
        INSERT OR REPLACE INTO link_files
            (url_hash, url, gid, mid, file_type, local_path,
             file_size, md5, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (url_hash, url, gid, mid, file_type, local_path,
          file_size, md5, status, now))
    conn.commit()


def scan_and_download_messages(limit: int = 200):
    """扫描消息中的外部链接，检测并下载文件"""
    conn = get_conn()
    # 确保表存在
    conn.execute("""
        CREATE TABLE IF NOT EXISTS link_files (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            url_hash  TEXT NOT NULL,
            url       TEXT NOT NULL,
            gid       INTEGER DEFAULT 0,
            mid       TEXT DEFAULT '',
            file_type TEXT DEFAULT '',
            local_path TEXT DEFAULT '',
            file_size INTEGER DEFAULT 0,
            md5       TEXT DEFAULT '',
            status    TEXT DEFAULT 'pending',
            created_at INTEGER NOT NULL,
            UNIQUE(url_hash)
        )
    """)
    conn.commit()

    # 获取已扫描过的 url_hash
    scanned = set(r[0] for r in conn.execute(
        "SELECT url_hash FROM link_files").fetchall())

    rows = conn.execute("""
        SELECT mid, gid, text, url_objects, media_type
        FROM messages
        ORDER BY created_at DESC
        LIMIT ?
    """, (limit,)).fetchall()

    found = 0
    for row in rows:
        mid, gid, text, uo_json, media_type = row
        urls = set()

        # 1. 从 text 提取
        if text and ("http" in text):
            for u in extract_urls(text):
                if "weibo.com" not in u and "sinaimg" not in u:
                    urls.add(u.strip())

        # 2. 从 url_objects 提取
        if uo_json and uo_json not in ("[]", "{}", ""):
            try:
                uo = json.loads(uo_json)
                if isinstance(uo, list):
                    for item in uo:
                        info = item.get("info", {})
                        ul = info.get("url_long", "")
                        if ul and "weibo.com" not in ul and "sinaimg" not in ul:
                            urls.add(ul)
            except Exception:
                pass

        for url in urls:
            url_hash = hashlib.md5(url.encode()).hexdigest()[:16]
            if url_hash in scanned:
                continue
            scanned.add(url_hash)

            # 解析 t.cn
            real_url = resolve_tcn(url)
            if real_url != url:
                # 也检查 real_url 的 hash
                rh = hashlib.md5(real_url.encode()).hexdigest()[:16]
                if rh in scanned:
                    continue
                scanned.add(rh)

            result = is_downloadable_file(real_url)
            if result:
                file_type, target_url = result
                download_link_file(target_url, file_type, mid, gid)
                found += 1
                # 慢一点，别打太多请求
                time.sleep(1)

    log.info("链接文件扫描完成: 检查 %d 条消息, 发现 %d 个文件", len(rows), found)
    return found


def _calc_md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
