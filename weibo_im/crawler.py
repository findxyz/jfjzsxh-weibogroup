"""爬虫核心 — HTTP API 客户端 + 爬取逻辑"""
from __future__ import annotations

import json
import os
import time
import random
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
import urllib3

from .types import msg_type_name
from .parser import parse_messages
from .db import (
    init_db, save_groups, save_message, save_media_file,
    get_latest_mid, get_skip_gids,
)
from .media import download_pending

urllib3.disable_warnings()
log = logging.getLogger("weibo_im.crawler")

API_BASE = "https://api.weibo.com"
SOURCE = "209678993"
CST = timezone(timedelta(hours=8))


def _midnight_today_ms() -> int:
    """今天零点 (CST) 的毫秒时间戳"""
    now = datetime.now(CST)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp() * 1000)

# 不爬取的群 gid 存储在 config 表 skip_gids 键中

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Encoding": "gzip, deflate, br",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Origin": "https://api.weibo.com",
    "Referer": "https://api.weibo.com/webim/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}


def _jitter_sleep(base: float, jitter: float = 0.2):
    """带抖动的等待，jitter 为 ±比例"""
    actual = base * (1 + random.uniform(-jitter, jitter))
    time.sleep(max(actual, 0.05))


def _request_with_retry(session: requests.Session, method: str, url: str,
                        max_retries: int = 3, **kwargs) -> requests.Response:
    """带退避重试的 HTTP 请求"""
    last_err = None
    for attempt in range(max_retries + 1):
        try:
            resp = session.request(method, url, **kwargs)
            # 5xx — 可重试
            if resp.status_code >= 500:
                if attempt < max_retries:
                    wait = (2 ** attempt) * (1 + random.uniform(0, 0.5))
                    log.warning("  ↻ 5xx(%d) 重试 %d/%d, 等待 %.1fs",
                                resp.status_code, attempt + 1, max_retries, wait)
                    time.sleep(wait)
                    continue
            # 429 — 限流，多等一会
            if resp.status_code == 429:
                if attempt < max_retries:
                    wait = (4 ** attempt) * (1 + random.uniform(0, 0.5))
                    log.warning("  ↻ 429 限流 重试 %d/%d, 等待 %.1fs",
                                attempt + 1, max_retries, wait)
                    time.sleep(wait)
                    continue
            resp.raise_for_status()
            return resp
        except (requests.ConnectionError, requests.Timeout) as e:
            last_err = e
            if attempt < max_retries:
                wait = (2 ** attempt) * (1 + random.uniform(0, 0.5))
                log.warning("  ↻ 连接错误(%s) 重试 %d/%d, 等待 %.1fs",
                            e.__class__.__name__, attempt + 1, max_retries, wait)
                time.sleep(wait)
                continue
        except requests.HTTPError as e:
            # 4xx 非 429 不重试
            if e.response is not None and 400 <= e.response.status_code < 500 \
               and e.response.status_code != 429:
                raise
            last_err = e
            if attempt < max_retries:
                wait = (2 ** attempt) * (1 + random.uniform(0, 0.5))
                log.warning("  ↻ HTTP错误(%s) 重试 %d/%d, 等待 %.1fs",
                            e.__class__.__name__, attempt + 1, max_retries, wait)
                time.sleep(wait)
                continue
    raise last_err or RuntimeError("请求失败（重试耗尽）")


def make_session(cookie: str) -> requests.Session:
    session = requests.Session()
    session.verify = False
    session.headers.update(HEADERS)
    session.headers["Cookie"] = cookie
    return session


# ── API 调用 ──────────────────────────────────────────────


def fetch_contacts(session: requests.Session) -> list[dict]:
    """获取所有群聊列表（来自 contacts.json）"""
    ts = int(time.time() * 1000)
    resp = _request_with_retry(
        session, "GET", f"{API_BASE}/webim/2/direct_messages/contacts.json",
        params={
            "special_source": "3",
            "add_virtual_user": "3,4",
            "is_include_group": "0",
            "need_back": "0,0",
            "is_include_folder": "1",
            "count": "50",
            "source": SOURCE,
            "t": str(ts),
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    groups = []
    for c in data.get("contacts", []):
        user = c.get("user", {})
        if user.get("type") != 2:
            continue
        groups.append({
            "gid": user["id"],
            "name": user.get("name", ""),
            "member_count": user.get("member_count", 0),
            "max_member": user.get("max_member_count", 0),
            "avatar": user.get("avatar_large", ""),
            "round_avatar": user.get("round_avatar_large", ""),
            "owner_id": user.get("creator", 0),
            "summary": user.get("description", ""),
            "group_type": user.get("group_type", 0),
            "super_group_type": user.get("super_group_type", 0),
            "status": user.get("group_status", 0),
            "validate_type": user.get("validateType", 0),
            "raw_json": json.dumps(c, ensure_ascii=False),
        })
    return groups


def fetch_messages(session: requests.Session, gid: int,
                   count: int = 50, max_mid: str = None) -> list[dict]:
    """获取群聊消息列表。max_mid 为空时取最新，有值时取该 mid 之前的更早消息。

    注意: API 返回的消息按 从旧到新 (oldest first) 排列。
          msgs[0]  = 页面上最旧的消息
          msgs[-1] = 页面上最新的消息
          翻页游标用 msgs[0].id（最旧的 id 传给 max_mid = "取比这个更旧的"）。
          停止条件用 msgs[-1].id 和已知 max_mid 比较。
    """
    ts = int(time.time() * 1000)
    params = {
        "id": str(gid),
        "count": str(count),
        "convert_emoji": "1",
        "query_sender": "1",
        "source": SOURCE,
        "t": str(ts),
    }
    if max_mid:
        params["max_mid"] = str(max_mid)

    resp = _request_with_retry(
        session, "GET", f"{API_BASE}/webim/groupchat/query_messages.json",
        params=params,
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("result"):
        log.warning("[群%s] query_messages 返回 result=false (可能被限流或 cookie 过期)", gid)
        return []
    return data.get("messages", [])


# ── 爬取逻辑 ──────────────────────────────────────────────


class Crawler:
    """爬虫核心类"""

    def __init__(self, db_path: str | Path, cookie: str = ""):
        """初始化爬虫

        Args:
            db_path: 数据库路径
            cookie: 可选，如提供则存入 DB；否则从 DB 读取
        """
        from .db import set_db_path, set_cookie as db_set_cookie, get_cookie
        set_db_path(str(db_path))
        init_db()
        if cookie:
            db_set_cookie(cookie)
            self.cookie = cookie
        else:
            self.cookie = get_cookie()
        if not self.cookie:
            raise RuntimeError(
                "Cookie 未设置。请先运行: python crawl.py --set-cookie "
                "'SUB=xxx; SUBP=yyy'"
            )
        self.session = make_session(self.cookie)

    def sync_groups(self) -> list[dict]:
        """刷新群列表，返回群列表（过滤掉 config skip_gids）"""
        groups = fetch_contacts(self.session)
        groups = [g for g in groups if g["gid"] not in get_skip_gids()]
        save_groups(groups)
        log.info("群列表更新: %d 个群", len(groups))
        return groups

    def crawl_group(self, gid: int, name: str,
                    download_media: bool = True,
                    since_time: int | None = None) -> dict:
        """爬取单个群的消息

        Args:
            since_time: 毫秒时间戳，消息早于此时停止翻页。
                        None → 首次运行默认回到今天零点
        """
        last_mid = get_latest_mid(gid)
        first_run = not last_mid

        if first_run:
            # 首次运行：默认回到今天零点
            if since_time is None:
                since_time = _midnight_today_ms()
            return self._backfill_group(gid, name,
                                        since_time=since_time)

        # 非首次运行 + 指定时间 → 填补空隙（从 min_mid 往前翻到 since_time）
        if since_time is not None:
            from .db import get_group_mid_range
            min_mid, _ = get_group_mid_range(gid)
            return self._backfill_group(gid, name,
                                        since_time=since_time,
                                        start_mid=min_mid)

        # 后续运行：翻页拉取新增消息（右向扩展线段）
        #
        # API 返回顺序：从旧到新（oldest first）。
        #   msgs[0]  = 最旧的  → 翻页游标：传 msgs[0].id 给 max_mid 以取更早消息
        #   msgs[-1] = 最新的  → 停止条件：msgs[-1].id ≤ max_mid 则整页已知
        new_count = 0
        media_count = 0
        cursor = ""  # 从最新开始
        from .db import get_group_mid_range
        _, max_mid = get_group_mid_range(gid)

        for page in range(100):
            msgs = fetch_messages(self.session, gid, count=50,
                                  max_mid=cursor or None)
            if not msgs:
                break

            # 检查最新一条消息是否 ≤ max_mid（整页都在已知范围内）
            # API 从旧到新，msgs[-1] 是页面上最新的
            last_id = str(msgs[-1].get("id", ""))
            if max_mid and last_id <= max_mid:
                break  # 最新一条 ≤ max_mid，整页已知

            # 过滤掉已知旧消息，只处理新消息
            parsed = parse_messages(msgs, default_gid=gid)
            for pm in parsed:
                if max_mid and pm["mid"] <= max_mid:
                    continue  # 跳过已知消息
                if save_message(pm):
                    new_count += 1
                    if pm["fid"] and pm["media_orig_url"]:
                        save_media_file(
                            fid=pm["fid"], gid=gid, mid=pm["mid"],
                            media_type=pm["media_type"],
                            orig_url=pm["media_orig_url"],
                        )
                        media_count += 1

            if len(msgs) < 50:
                break  # 无更多页
            # msgs[0] = 本页最旧消息，传给 max_mid 以翻到更早的下一页
            cursor = str(msgs[0].get("id", ""))
            _jitter_sleep(0.3)

        # 刷新线段范围
        if new_count > 0:
            from .db import refresh_group_range
            refresh_group_range(gid)
            log.info("  ⇣ [%s] +%d 条新消息 (%d 个文件)",
                     name, new_count, media_count)
        return {"new": new_count, "total": new_count, "media": media_count}

    def _backfill_group(self, gid: int, name: str,
                        since_time: int | None = None,
                        start_mid: str | None = None) -> dict:
        """翻页回填历史消息

        API 返回顺序：从旧到新（oldest first）。
          msgs[0]  = 最旧的 → 翻页游标
          msgs[-1] = 最新的 → 停止条件（检查是否已达 since_time）

        停止条件：API 返回空页 或 消息时间早于 since_time。
        无数量/页数上限。

        Args:
            since_time: 毫秒时间戳，消息早于此时停止翻页
        """
        new_count = 0
        media_count = 0
        cursor = start_mid or ""  # 有 start_mid 则从其往前翻，否则从最新开始

        while True:
            msgs = fetch_messages(self.session, gid, count=50,
                                  max_mid=cursor or None)
            if not msgs:
                break

            # 检查最旧的消息是否已达截止时间
            # API 从旧到新，msgs[-1] 是页面上最新的
            oldest_ts = (msgs[-1].get("time") or 0)
            if since_time and oldest_ts:
                if oldest_ts < 1_000_000_000_000:
                    oldest_ts *= 1000
                if oldest_ts < since_time:
                    # 最新消息已早于截止时间，但这一页里可能还有部分要存的
                    pass  # 继续处理本页，后面用 break 退出循环

            parsed = parse_messages(msgs, default_gid=gid)
            for pm in reversed(parsed):
                if since_time and pm["created_at"] < since_time:
                    # 这条消息已经太旧了，停止
                    cursor = ""
                    break
                if save_message(pm):
                    new_count += 1
                    if pm["fid"] and pm["media_orig_url"]:
                        save_media_file(
                            fid=pm["fid"], gid=gid, mid=pm["mid"],
                            media_type=pm["media_type"],
                            orig_url=pm["media_orig_url"],
                        )
                        media_count += 1
            else:
                # 没有触发 time break，继续翻页
                # msgs[0] = 本页最旧消息，传给 max_mid 以翻到更早的下一页
                cursor = str(msgs[0].get("id", ""))

                if len(msgs) < 50:
                    break

                # 本页全部入库，左边界前移
                from .db import refresh_group_range
                refresh_group_range(gid)
                _jitter_sleep(1.0)
                continue

            # 触发了 time break，退出
            break

        if new_count:
            from .db import refresh_group_range
            refresh_group_range(gid)
            log.info("  ⬇ [%s] 回填 %d 条历史 (%d 个文件)",
                     name, new_count, media_count)
        return {"new": new_count, "total": new_count, "media": media_count}

    def crawl_all(self, download_media: bool = False,
                  group_interval: float = 1.5,
                  since_time: int | None = None,
                  gid: int = 0) -> dict:
        """爬取所有群的消息

        Args:
            since_time: 毫秒时间戳，消息早于此时停止翻页。
                        None → 首次运行默认回到今天零点
            gid: 指定群 gid，0 表示爬取所有群
        """
        groups = self.sync_groups()
        if gid:
            groups = [g for g in groups if g["gid"] == gid]
            if not groups:
                log.warning("gid=%d 不在群列表中", gid)
                return {"groups": 0, "groups_with_new": 0,
                        "new_messages": 0, "media_pending": 0}
        total_new = 0
        total_media = 0
        groups_with_new = 0

        for g in groups:
            try:
                result = self.crawl_group(g["gid"], g["name"],
                                          download_media=False,
                                          since_time=since_time)
                if result["new"] > 0:
                    total_new += result["new"]
                    total_media += result["media"]
                    groups_with_new += 1
                _jitter_sleep(group_interval, 0.15)
            except Exception as e:
                log.warning("  ✗ [%s] 错误: %s", g["name"], e)
                _jitter_sleep(group_interval, 0.15)

        # 统一下载媒体文件
        if download_media and total_media > 0:
            self.download_all_media()

        # 扫描链接文件
        if download_media:
            self.scan_links(limit=500)

        return {
            "groups": len(groups),
            "groups_with_new": groups_with_new,
            "new_messages": total_new,
            "media_pending": total_media,
        }

    def probe_boundary(self, gid: int, name: str = "") -> dict:
        """盲测群聊最早可爬取边界（即入群时间），仅输出不入库（2年限制会漂移）"""
        # mid → 时间戳 反推系数。
        # 微博 WebIM 的 mid 是单调递增整数，时间戳隐编码在其中（每 ms 增长约 4194）。
        # 通过 API 取两条已知时间消息的 mid，线性回归得到近似映射。
        # 精度足够做指数后退和二分查找，不需要分布式 ID 生成器细节。
        SLOPE = 4194.3044
        INTERCEPT = -2162095133480728

        def ts_to_mid(ts_ms: int) -> int:
            return int(SLOPE * ts_ms + INTERCEPT)

        # 找到当前最早消息时间
        from .db import get_group_mid_range
        min_mid_str, _ = get_group_mid_range(gid)
        if not min_mid_str:
            # 群里还没有消息，先拉一页最新的
            msgs = fetch_messages(self.session, gid, count=50)
            if not msgs:
                return {"boundary_mid": None, "boundary_ts": None, "error": "群没有消息"}
            min_mid_str = msgs[-1].get("idstr") or str(msgs[-1].get("id", ""))

        # 用 mid 反推当前最早时间
        earliest_known_ts = int(min_mid_str)
        earliest_known_ts = int((earliest_known_ts - INTERCEPT) / SLOPE)

        low_ts = earliest_known_ts
        high_ts = None
        low_mid = int(min_mid_str)  # 已知最旧消息的实际 mid

        # Phase 1: 指数后退
        log.info("  Phase 1: 指数后退 — 从最早已知时间往回探")
        intervals_days = [1, 3, 7, 14, 30, 90, 180, 365, 730, 1095]

        for days in intervals_days:
            probe_ts = low_ts - days * 86400_000
            probe_mid = ts_to_mid(probe_ts)
            msgs = fetch_messages(self.session, gid, count=50, max_mid=str(probe_mid))
            _jitter_sleep(0.3)
            if msgs:
                oldest = msgs[-1]
                o_ts = (oldest.get("time") or 0)
                if o_ts and o_ts < 1_000_000_000_000:
                    o_ts *= 1000
                low_ts = o_ts
                low_mid = int(oldest.get("id", 0))
            else:
                high_ts = probe_ts
                break

        if high_ts is None:
            return {"boundary_mid": None, "boundary_ts": None, "error": "后退3年仍未探空"}

        # Phase 2: 二分
        log.info("  Phase 2: 二分查找 — 窗口 %.1f 天", (low_ts - high_ts) / 86400000)
        for _ in range(30):
            if (low_ts - high_ts) < 3600 * 1000:
                break
            mid_ts = (low_ts + high_ts) // 2
            probe_mid = ts_to_mid(mid_ts)
            msgs = fetch_messages(self.session, gid, count=50, max_mid=str(probe_mid))
            _jitter_sleep(0.3)
            if msgs:
                oldest = msgs[-1]
                o_ts = (oldest.get("time") or 0)
                if o_ts and o_ts < 1_000_000_000_000:
                    o_ts *= 1000
                low_ts = o_ts
                low_mid = int(oldest.get("id", 0))
            else:
                high_ts = mid_ts

        # Phase 3: 翻页到空
        log.info("  Phase 3: 翻页到空")
        cursor = low_mid

        if cursor == 0:
            return {"boundary_mid": None, "boundary_ts": None, "error": "起点即空"}

        boundary = {"mid": cursor, "ts": low_ts, "sender": "", "text": ""}
        for page in range(100):
            msgs = fetch_messages(self.session, gid, count=50, max_mid=str(cursor))
            if not msgs:
                break

            oldest = msgs[-1]
            cursor = int(oldest.get("id", cursor))
            boundary["mid"] = cursor
            o_ts = oldest.get("time", 0) or 0
            if o_ts < 1_000_000_000_000:
                o_ts *= 1000
            boundary["ts"] = o_ts
            boundary["sender"] = oldest.get("sender_screen_name", "") or ""
            boundary["text"] = (oldest.get("text", "") or "")[:120]
            _jitter_sleep(0.3)

        import datetime as dt_module
        dt_cst = dt_module.datetime.fromtimestamp(
            boundary["ts"] / 1000 + 8 * 3600, tz=dt_module.timezone.utc
        )
        log.info("  → 最早边界: %s CST  mid=%s  sender=%s  text=%s",
                 dt_cst.isoformat(), boundary["mid"],
                 boundary["sender"] or "(空)", boundary["text"] or "(空)")
        return boundary

    def download_all_media(self, skip_video: bool = False):
        """下载所有待处理的媒体文件（逐个下载直到队列清空）

        Args:
            skip_video: True 时跳过视频 (media_type ∈ {10,13})，直接标 skipped。
        """
        total = 0
        while True:
            count = download_pending(limit=10, skip_video=skip_video)
            if count == 0:
                break
            total += count
            # 首轮已处理 skip_video，后续轮次无需再标记
            skip_video = False
            _jitter_sleep(0.3)
        if total > 0:
            log.info("媒体下载完成: %d 个", total)

    def download_fid(self, fid: str) -> dict:
        """下载指定 fid 的单个媒体文件"""
        from .db import get_conn
        from .media import download_file, update_media_status, update_message_media_local_path

        conn = get_conn()
        row = conn.execute(
            "SELECT * FROM media_files WHERE fid=?", (fid,)
        ).fetchone()
        if not row:
            return {"error": f"fid={fid} 不存在于 media_files 表中"}

        r = dict(row)
        if r["status"] == "done" and r["local_path"] and os.path.isfile(r["local_path"]):
            return {"status": "already_done", "local_path": r["local_path"]}
        # 下载文件
        result = download_file(r["fid"], r["orig_url"], r["media_type"])
        update_media_status(
            r["fid"],
            status=result["status"],
            local_path=result["local_path"],
            file_size=result["file_size"],
            md5=result["md5"],
        )
        if result["status"] == "done" and r["mid"]:
            update_message_media_local_path(r["mid"], result["local_path"])

        return result

    def scan_links(self, limit: int = 500):
        """扫描最近消息中的外部链接，下载文件（PDF/ZIP 等）"""
        from .links import scan_and_download_messages
        return scan_and_download_messages(limit=limit)

    def stats(self) -> dict:
        from .db import get_stats
        return get_stats()
