#!/usr/bin/env python3
"""
微博群聊消息抓取 — 手动执行入口（本地版，无 Hermes 依赖）

用法:
    python crawl.py                                # 爬取新消息（不下载媒体）
    python crawl.py --download                     # 下载所有 pending 媒体文件
    python crawl.py --download-fid 5302496155143676  # 下载指定 fid 的媒体文件
    python crawl.py --group-only                   # 只刷新群列表
    python crawl.py --since 2026-01-01             # 回填到指定日期（不下载媒体）
    python crawl.py --set-cookie 'SUB=xxx; SUBP=yyy'   # 首次设置 cookie（手动）
    python crawl.py --probe-boundary                    # 盲测所有群最早边界
    python crawl.py --probe-boundary --gid 4761715839862414  # 只测指定群
    python crawl.py --renew-cookie                     # 打开浏览器扫码续期 cookie
    python crawl.py --check-playwright                 # 检查 Playwright 环境就绪
    python crawl.py --stats                            # 打印数据库统计
    python crawl.py --search 关键词                    # 全文搜索消息

扫码登录流程（--renew-cookie）：
    程序用 Playwright 打开微博扫码页，把二维码截图保存到本地文件并尝试用
    系统默认程序打开图片。你在控制台看到「请扫码」提示后，用微博 APP 扫码，
    程序检测到登录成功后会把 cookie 写入数据库。
"""
from __future__ import annotations

import os
import sys
import time
import argparse
import logging
import platform
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from weibo_im.crawler import Crawler

CST = timezone(timedelta(hours=8))

# 二维码截图保存路径（跨平台）
QRCODE_PATH = str(Path(__file__).resolve().parent / "qrcode.png")


def _parse_since(value: str) -> int | None:
    """解析 --since 参数: 日期字符串或毫秒时间戳，返回 ms"""
    if not value:
        return None
    # 纯数字 → 时间戳
    if value.isdigit() or (value.startswith("-") and value[1:].isdigit()):
        return int(value)
    # 日期字符串
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d"):
        try:
            dt = datetime.strptime(value, fmt)
            return int(dt.replace(tzinfo=CST).timestamp() * 1000)
        except ValueError:
            continue
    raise ValueError(f"无法解析日期: {value}（支持 2026-01-01 或毫秒时间戳）")


def _open_image_cross_platform(path: str):
    """用系统默认程序打开图片，失败静默忽略（仅扫码便利性，不影响功能）"""
    if not os.path.isfile(path):
        return
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.run(["open", path], check=False)
        else:
            subprocess.run(["xdg-open", path], check=False)
    except Exception:
        # 打不开没关系，用户也能在文件管理器里手动看
        pass


def _check_playwright(log: logging.Logger) -> bool:
    """检查 Playwright + Chromium 环境是否就绪。打印诊断信息，返回是否通过。"""
    ok = True

    # 第一层：Python 包
    try:
        from playwright.sync_api import sync_playwright  # noqa: F811
        log.info("✅ playwright Python 包可导入")
    except ImportError:
        log.error("❌ playwright Python 包未安装（当前解释器: %s）", sys.executable)
        log.error("   → uv add playwright && uv run playwright install chromium")
        return False

    # 第二层：浏览器能启动
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
            browser.close()
        log.info("✅ Chromium 启动正常")
    except Exception as e:
        log.error("❌ Chromium 启动失败: %s", str(e).split("\n")[0])
        ok = False

    return ok


def _renew_cookie(db_path: str, log: logging.Logger, headless: bool):
    """用 Playwright 打开 api.weibo.com/chat → 截图二维码 → 等你扫码 → 存 cookie

    Args:
        headless: True=无头模式（截图保存文件，靠文件查看二维码）；
                  False=有头模式（直接弹出浏览器窗口扫码，体验更直观）。
    """
    if not _check_playwright(log):
        log.error("请先解决以上问题后重试: uv run python crawl.py --renew-cookie")
        sys.exit(1)

    from playwright.sync_api import sync_playwright  # noqa: F811

    with sync_playwright() as pw:
        # Windows / macOS 上推荐有头模式，扫码更方便；Linux 无桌面用无头 + 截图
        launch_args = ["--disable-blink-features=AutomationControlled"]
        if headless:
            launch_args.append("--no-sandbox")
            launch_args.append("--disable-setuid-sandbox")
        browser = pw.chromium.launch(headless=headless, args=launch_args)
        try:
            ctx = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
                locale="zh-CN",
            )
            page = ctx.new_page()

            log.info("打开 api.weibo.com/chat ...")
            page.goto("https://api.weibo.com/chat", wait_until="domcontentloaded", timeout=30000)
            time.sleep(3)

            # 判断是否已登录（用 JS 读 location.href，page.url 检测不到 hash 路由）
            current_href = page.evaluate("window.location.href")
            log.info("当前 URL: %s", current_href)

            if "#/chat" in current_href:
                log.info("已有有效 cookie，直接提取")
            else:
                # 截图发微信（原版）→ 改为本地截图 + 尝试打开
                page.screenshot(path=QRCODE_PATH)
                log.info("=" * 60)
                log.info("📱 二维码已截图 -> %s", QRCODE_PATH)
                log.info("请用微博 APP 扫码登录（等待最多 120 秒）")
                if headless:
                    _open_image_cross_platform(QRCODE_PATH)
                else:
                    log.info("（如未自动弹出二维码，请查看上面的图片路径）")
                log.info("=" * 60)

                log.info("等待扫码...")
                detected = False
                for i in range(120):
                    time.sleep(1)
                    try:
                        current = page.evaluate("window.location.href")
                        if current != current_href:
                            log.info("检测到页面跳转: %s → %s", current_href, current)
                            log.info("🔍 检测到扫码登录，正在处理...")
                            try:
                                page.wait_for_load_state("networkidle", timeout=15000)
                            except Exception:
                                pass
                            time.sleep(3)
                            detected = True
                            break
                    except Exception:
                        break
                if not detected:
                    log.error("扫码超时（120 秒），最终 URL: %s", page.url)
                    log.error("⏰ 扫码超时或失败，请重新运行 --renew-cookie")
                    sys.exit(1)

            # 只拿 .weibo.com 域名的 cookie，直接存
            raw_cookies = ctx.cookies()
            deduped: dict[str, str] = {}
            for c in raw_cookies:
                domain = c.get("domain", "")
                if not domain.endswith(".weibo.com") and domain != "weibo.com":
                    continue
                deduped[c["name"]] = c["value"]

            cookie_str = "; ".join(f"{k}={v}" for k, v in sorted(deduped.items()))
        finally:
            browser.close()

    # 存入数据库
    from weibo_im.db import set_db_path, init_db, set_cookie
    set_db_path(db_path)
    init_db()
    set_cookie(cookie_str)
    log.info("✅ Cookie 已存入数据库")

    # 验证爬取
    log.info("验证新 cookie ...")
    crawler = Crawler(db_path)
    groups = crawler.sync_groups()
    log.info("验证通过: 群列表 %d 个群", len(groups))
    log.info("💬 已成功登录，可开始爬取。运行 `python crawl.py` 抓取消息。")


def _print_search_results(rows, keyword: str, limit: int):
    """格式化打印搜索结果"""
    if not rows:
        print(f"未找到包含「{keyword}」的消息")
        return
    print(f"找到 {len(rows)} 条包含「{keyword}」的消息（最多显示 {limit} 条）:")
    print("-" * 70)
    for r in rows:
        ts_ms = r[0]
        ts_str = datetime.fromtimestamp(ts_ms / 1000, tz=CST).strftime(
            "%Y-%m-%d %H:%M:%S"
        ) if ts_ms else "(无时间)"
        group = r[1] or "(未知群)"
        sender = r[2] or "(未知)"
        text = (r[3] or "").replace("\n", " ")
        if len(text) > 200:
            text = text[:200] + "..."
        print(f"[{ts_str}] [{group}] {sender}: {text}")
    print("-" * 70)


def _do_search(db_path: str, keyword: str, limit: int):
    """FTS5 全文搜索"""
    import sqlite3
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT m.created_at, m.group_name, m.sender_name, m.text
            FROM messages_fts f
            JOIN messages m ON m.id = f.rowid
            WHERE messages_fts MATCH ?
            ORDER BY m.created_at DESC
            LIMIT ?
            """,
            (keyword, limit),
        ).fetchall()
    finally:
        conn.close()
    _print_search_results(rows, keyword, limit)


def main():
    parser = argparse.ArgumentParser(description="微博群聊消息抓取（本地版）")
    parser.add_argument("--db", default="", help="数据库路径（默认 <项目>/weibo_im.db）")
    parser.add_argument("--download", action="store_true", help="下载所有 pending 媒体文件")
    parser.add_argument("--download-fid", default="", help="下载指定 fid 的媒体文件")
    parser.add_argument("--since", default="",
                        help="回填起始日期（如 2026-01-01）或时间戳(ms)，0=全部历史")
    parser.add_argument("--group-only", action="store_true", help="只刷新群列表，不爬消息")
    parser.add_argument("--set-cookie", default="", help="设置 cookie 到数据库后退出")
    parser.add_argument("--renew-cookie", action="store_true", help="打开浏览器扫码续期 cookie")
    parser.add_argument("--check-playwright", action="store_true",
                        help="检查 Playwright + Chromium 环境是否就绪（部署前验证）")
    parser.add_argument("--headless", action="store_true",
                        help="--renew-cookie 时使用无头模式（仅截图，不弹窗；默认有头弹窗）")
    parser.add_argument("--verbose", action="store_true", help="详细日志")
    parser.add_argument("--probe-boundary", action="store_true",
                        help="盲测群聊最早可爬取边界（入群时间）")
    parser.add_argument("--gid", type=int, default=0,
                        help="指定群 gid（与 --probe-boundary 或 --since 配合，缺省则爬所有群）")
    parser.add_argument("--add-skip-gid", type=int, default=0,
                        help="将指定 gid 加入不爬取列表")
    parser.add_argument("--remove-skip-gid", type=int, default=0,
                        help="从不爬取列表中移除指定 gid")
    parser.add_argument("--list-skip", action="store_true",
                        help="列出当前不爬取的群")
    parser.add_argument("--list-groups", action="store_true",
                        help="列出已知的所有群（gid / 群名 / 成员数）")
    parser.add_argument("--stats", action="store_true",
                        help="打印数据库统计（消息数 / 群数 / 媒体数）")
    parser.add_argument("--search", default="",
                        help="全文搜索消息（FTS5 关键词）")
    parser.add_argument("--search-limit", type=int, default=50,
                        help="--search 的最大返回条数（默认 50）")
    parser.add_argument("--no-video", action="store_true",
                        help="下载媒体时跳过视频 (media_type 10/13)，直接标 skipped 不占存储")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("crawl")

    db_path = args.db or str(Path(__file__).parent / "weibo_im.db")
    db_path = str(Path(db_path).expanduser())
    log.info("数据库: %s", db_path)
    log.info("平台: %s %s", platform.system(), platform.machine())

    # --renew-cookie: 打开浏览器扫码续期
    if args.renew_cookie:
        _renew_cookie(db_path, log, headless=args.headless)
        return

    # --check-playwright: 验证 Playwright + Chromium 环境
    if args.check_playwright:
        ok = _check_playwright(log)
        sys.exit(0 if ok else 1)

    # --set-cookie: 写入 DB 后直接退出
    if args.set_cookie:
        from weibo_im.db import set_db_path, init_db, set_cookie
        set_db_path(db_path)
        init_db()
        set_cookie(args.set_cookie)
        log.info("Cookie 已存入数据库")
        return

    # 设置 skip_gids 相关 + stats / list-groups / search 都需要先初始化 DB
    from weibo_im.db import (
        set_db_path, init_db, get_skip_gids, add_skip_gid, remove_skip_gid,
        get_stats, get_group_list,
    )
    set_db_path(db_path)
    init_db()

    if args.list_skip:
        gids = get_skip_gids()
        if not gids:
            log.info("当前没有不爬取的群")
            return
        # 查 groups 表获取群名
        import sqlite3
        conn = sqlite3.connect(db_path)
        names = dict(conn.execute("SELECT gid, name FROM groups").fetchall())
        conn.close()
        log.info("不爬取的群（%d 个）:", len(gids))
        for gid in sorted(gids):
            name = names.get(gid, "未知群")
            log.info("  %20d  %s", gid, name)
        return

    if args.add_skip_gid:
        add_skip_gid(args.add_skip_gid)
        log.info("已将 gid=%d 加入不爬取列表", args.add_skip_gid)
        return

    if args.remove_skip_gid:
        remove_skip_gid(args.remove_skip_gid)
        log.info("已将 gid=%d 从不爬取列表中移除", args.remove_skip_gid)
        return

    if args.stats:
        s = get_stats()
        log.info("数据库统计:")
        log.info("  消息总数:     %d", s["messages"])
        log.info("  有消息的群:   %d", s["groups_with_msgs"])
        log.info("  群总数:       %d", s["groups_total"])
        log.info("  媒体已下载:   %d", s["media_done"])
        log.info("  媒体待下载:   %d", s["media_pending"])
        log.info("  媒体失败:     %d", s["media_failed"])
        log.info("  媒体跳过:     %d", s["media_skipped"])
        return

    if args.list_groups:
        groups = get_group_list()
        if not groups:
            log.info("数据库中还没有任何群记录，先运行 `python crawl.py` 拉一次群列表")
            return
        log.info("已知的群（%d 个）:", len(groups))
        for g in groups:
            log.info("  gid=%-18d  成员=%-5d  %s",
                     g["gid"], g.get("member_count", 0), g.get("name", ""))
        return

    if args.search:
        _do_search(db_path, args.search, args.search_limit)
        return

    try:
        crawler = Crawler(db_path)
    except RuntimeError as e:
        log.error("Cookie 未设置: %s", e)
        log.error("请运行: python crawl.py --renew-cookie（扫码）")
        log.error("  或: python crawl.py --set-cookie 'SUB=xxx; SUBP=yyy'（手动）")
        return

    # --download-fid: 下载单个媒体文件
    if args.download_fid:
        result = crawler.download_fid(args.download_fid)
        if "error" in result:
            log.error(result["error"])
        elif result.get("status") == "already_done":
            log.info("已下载: %s", result["local_path"])
        elif result.get("status") == "done":
            log.info("下载完成: %s (%d bytes)", result["local_path"], result["file_size"])
        else:
            log.error("下载失败: fid=%s", args.download_fid)
        return

    # --download: 下载所有 pending 媒体文件
    if args.download:
        if args.no_video:
            log.info("下载所有 pending 媒体文件（跳过视频）...")
        else:
            log.info("下载所有 pending 媒体文件...")
        crawler.download_all_media(skip_video=args.no_video)
        stats = crawler.stats()
        log.info("媒体统计: %d done, %d pending, %d failed, %d skipped",
                 stats["media_done"], stats["media_pending"],
                 stats["media_failed"], stats["media_skipped"])
        return

    # --probe-boundary: 盲测最早边界
    if args.probe_boundary:
        log.info("盲测最早可爬取边界...")
        groups = crawler.sync_groups()
        targets = [g for g in groups if args.gid == 0 or g["gid"] == args.gid]
        if not targets:
            log.warning("没有匹配的群")
            return
        for g in targets:
            log.info("→ [%s] gid=%d", g["name"], g["gid"])
            boundary = crawler.probe_boundary(g["gid"], g["name"])
            if boundary.get("error"):
                log.warning("  ✗ %s", boundary["error"])
        return

    if args.group_only:
        groups = crawler.sync_groups()
        log.info("群列表已刷新: %d 个群", len(groups))
        return

    # 解析 --since 参数
    since_time: int | None = None
    if args.since:
        since_time = _parse_since(args.since)
        log.info("回填起始: %s (%d)", args.since, since_time)

    # 常规模式：爬取所有群新消息
    try:
        result = crawler.crawl_all(download_media=False,
                                   since_time=since_time,
                                   gid=args.gid)
    except Exception as e:
        log.error("爬取失败: %s", e)
        log.error("⚠️ 微博 cookie 可能失效，请运行: python crawl.py --renew-cookie")
        return

    log.info("爬取完成: %d 个群, %d 个有新消息, +%d 条, %d 个媒体文件待下载",
             result["groups"], result["groups_with_new"],
             result["new_messages"], result["media_pending"])

    stats = crawler.stats()
    log.info("数据库统计: %d 条消息, %d 个群, %d 个媒体已下载",
             stats["messages"], stats["groups_with_msgs"], stats["media_done"])


if __name__ == "__main__":
    main()
