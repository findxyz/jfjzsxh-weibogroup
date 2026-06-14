"""媒体文件下载 — 从 upload API 下载图片/视频"""
from __future__ import annotations

import os
import time
import json
import hashlib
import logging
from pathlib import Path

import requests
import urllib3

from .db import (
    update_media_status, update_message_media_local_path,
    get_pending_media,
)

urllib3.disable_warnings()
log = logging.getLogger("weibo_im.media")

# 下载根目录 — 相对于本文件: <project>/media/
MEDIA_ROOT = Path(__file__).resolve().parent.parent / "media"

# User-Agent 与 API 一致
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Origin": "https://web.im.weibo.com",
    "Referer": "https://web.im.weibo.com/",
}


def _file_ext_from_url(url: str, media_type: int) -> tuple[str, str]:
    """根据 media_type 和 URL 确定文件类型和扩展名"""
    if media_type in (10, 13):
        return ("video", ".mp4")
    if media_type == 1:
        return ("image", ".jpg")
    elif media_type == 16:
        return ("redpacket", ".png")
    elif media_type == 5:
        return ("file", ".bin")  # 待响应时根据 Content-Type 修正
    else:
        return ("unknown", ".bin")


def _patch_ext_from_response(local_path: str, content_type: str,
                              content_disposition: str, fid: str = "") -> str:
    """根据响应头修正文件扩展名，优先使用 Content-Disposition 中的原始文件名"""
    import re
    from urllib.parse import unquote
    save_dir = os.path.dirname(local_path)
    # 1. 从 Content-Disposition 提取原始文件名
    if content_disposition:
        m = re.search(r'filename\*?=(?:UTF-8\'\')?\"?([^\"\n;]+)', content_disposition)
        if m:
            name = m.group(1)
            name = unquote(name)
            if name:
                new_path = os.path.join(save_dir, name)
                if not os.path.isfile(new_path) and os.path.isfile(local_path):
                    os.rename(local_path, new_path)
                    return new_path
                # 冲突: {文件名}.{fid}.{ext}
                stem, ext = os.path.splitext(name)
                fallback = os.path.join(save_dir, f"{stem}.{fid}{ext}")
                if not os.path.isfile(fallback) and os.path.isfile(local_path):
                    os.rename(local_path, fallback)
                    return fallback
                # fallback 也冲突？极小概率，放弃改名
                return local_path
    # 2. 降级：从 Content-Type 判断扩展名
    ct_map = {
        "application/pdf": ".pdf",
        "application/zip": ".zip",
        "application/x-rar-compressed": ".rar",
        "application/x-7z-compressed": ".7z",
        "application/msword": ".doc",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "application/vnd.ms-excel": ".xls",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
        "application/vnd.android.package-archive": ".apk",
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "video/mp4": ".mp4",
    }
    ct = content_type.split(";")[0].strip().lower()
    if ct in ct_map:
        ext = ct_map[ct]
        new_path = os.path.splitext(local_path)[0] + ext
        if not os.path.isfile(new_path) and os.path.isfile(local_path):
            os.rename(local_path, new_path)
        return new_path
    return local_path


_COOKIE: str = ""


def get_cookie_or_default() -> str:
    """返回已设置的 cookie，未设置时从 DB 读取"""
    global _COOKIE
    if not _COOKIE:
        from .db import get_cookie
        _COOKIE = get_cookie()
    return _COOKIE


def set_cookie(c: str):
    global _COOKIE
    _COOKIE = c


def download_file(fid: str, url: str, media_type: int,
                  timeout: int = 60) -> dict:
    """下载单个媒体文件，返回 {status, local_path, file_size, md5}"""
    # 确定文件类型和扩展名
    file_type, ext = _file_ext_from_url(url, media_type)

    # 目标路径
    sub = "files" if file_type == "file" else ("videos" if file_type == "video" else "images")
    save_dir = MEDIA_ROOT / sub
    save_dir.mkdir(parents=True, exist_ok=True)
    local_path = str(save_dir / f"{fid}{ext}")

    # 如果已存在则跳过
    if os.path.isfile(local_path) and os.path.getsize(local_path) > 0:
        file_size = os.path.getsize(local_path)
        md5 = _calc_md5(local_path)
        log.debug("已存在: %s (%d bytes)", local_path, file_size)
        return {"status": "done", "local_path": local_path,
                "file_size": file_size, "md5": md5}

    if file_type == "redpacket":
        with open(local_path, "w") as f:
            f.write(f"redpacket fid={fid}")
        return {"status": "done", "local_path": local_path, "file_size": 0, "md5": ""}

    try:
        log.info("下载: %s", url)
        h = dict(HEADERS)
        ck = get_cookie_or_default()
        if ck:
            h["Cookie"] = ck
        resp = requests.get(
            url,
            headers=h,
            timeout=timeout,
            verify=False,
            stream=True,
        )
        if resp.status_code != 200:
            log.warning("下载失败 [HTTP %d]: %s", resp.status_code, url)
            return {"status": "failed", "local_path": "", "file_size": 0, "md5": ""}

        # 写入文件
        file_size = 0
        with open(local_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    file_size += len(chunk)

        # 根据响应头修正扩展名
        ct = resp.headers.get("Content-Type", "")
        cd = resp.headers.get("Content-Disposition", "")
        corrected_path = _patch_ext_from_response(local_path, ct, cd, fid)
        if corrected_path != local_path:
            local_path = corrected_path

        md5 = _calc_md5(local_path)
        log.info("下载完成: %s (%d bytes)", local_path, file_size)
        return {"status": "done", "local_path": local_path,
                "file_size": file_size, "md5": md5}

    except Exception as e:
        log.error("下载异常: %s — %s", url, e)
        return {"status": "failed", "local_path": "", "file_size": 0, "md5": ""}


def _mark_videos_skipped() -> int:
    """把所有 pending 状态的视频 (media_type ∈ VIDEO_MEDIA_TYPES) 标记为 skipped，
    使其不再进入下载队列。返回处理的条数。

    skipped 与 done/failed 同级，仅作「主动放弃」的语义标记，
    --download 时会被 get_pending_media 过滤掉，不会重复尝试。
    """
    from .types import VIDEO_MEDIA_TYPES
    conn = get_conn()
    placeholders = ",".join("?" * len(VIDEO_MEDIA_TYPES))
    cursor = conn.execute(
        f"UPDATE media_files SET status='skipped' "
        f"WHERE status='pending' AND media_type IN ({placeholders})",
        tuple(VIDEO_MEDIA_TYPES),
    )
    conn.commit()
    return cursor.rowcount


def download_pending(limit: int = 10, cookie: str = "",
                     skip_video: bool = False):
    """下载 pending 状态的媒体文件

    Args:
        skip_video: True 时把所有视频 (media_type ∈ {10,13}) 直接标 skipped，
                    不下载（视频体积大，避免占存储）。
    """
    if cookie:
        set_cookie(cookie)
    get_cookie_or_default()  # 确保 cookie 被加载

    if skip_video:
        n = _mark_videos_skipped()
        if n > 0:
            log.info("跳过视频: %d 个标记为 skipped", n)

    files = get_pending_media(limit)
    for f in files:
        result = download_file(f["fid"], f["orig_url"], f["media_type"])
        update_media_status(
            f["fid"],
            status=result["status"],
            local_path=result["local_path"],
            file_size=result["file_size"],
            md5=result["md5"],
        )
        # 回写 messages 的 media_local_path
        if result["status"] == "done" and f["mid"]:
            update_message_media_local_path(f["mid"], result["local_path"])
    return len(files)


def _calc_md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
