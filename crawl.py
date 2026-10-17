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
    python crawl.py --renew-cookie                     # 打开本机 Chrome 扫码续期 cookie
    python crawl.py --renew-cookie --no-chrome         # 强制用 Playwright 自带 Chromium 扫码
    python crawl.py --renew-cookie --chrome-path /path/to/chrome  # 指定 Chrome 路径
    python crawl.py --check-playwright                 # 检查 Playwright 环境就绪
    python crawl.py --stats                            # 打印数据库统计
    python crawl.py --search 关键词                    # 全文搜索消息
    python crawl.py --diff --old 旧包.json --new 新包.json          # 对比两个导出包，输出新增补充包
    python crawl.py --diff --old old.csv --new new.csv --diff-format csv -O delta.csv  # 指定格式与输出路径

扫码登录流程（--renew-cookie）：
    程序优先调用本机已安装的 Chrome（Google Chrome）打开微博扫码页；若本机
    没有 Chrome 则回退到 Playwright 自带的 Chromium。二维码截图保存到本地文件
    并尝试用系统默认程序打开图片。你在控制台看到「请扫码」提示后，用微博 APP
    扫码，程序检测到登录成功后会把 cookie 写入数据库。
    也可用 --no-chrome 强制走自带 Chromium，或 --chrome-path 指定 Chrome 路径。
"""
from __future__ import annotations

import os
import sys
import time
import argparse
import logging
import platform
import shutil
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from weibo_im.crawler import Crawler, CookieExpiredError

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


def _find_chrome_executable() -> str | None:
    """跨平台探测本机已安装的 Google Chrome 可执行文件路径。

    返回首个存在的路径；找不到返回 None（不影响回退到 Playwright 自带 Chromium）。
    """
    candidates: list[str] = []
    if sys.platform == "darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ]
    elif sys.platform.startswith("win"):
        candidates = [
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]
    else:
        candidates = [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/opt/google/chrome/chrome",
            "/snap/bin/chromium",
        ]
    # PATH 兜底
    which = shutil.which("google-chrome") or shutil.which("google-chrome-stable")
    if which:
        candidates.append(which)
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def _launch_browser(pw, *, headless: bool, chrome_path: str, no_chrome: bool, log: logging.Logger):
    """启动浏览器用于扫码续期，优先调用本机 Chrome。

    优先级：
      1. ``--chrome-path`` 显式指定的本机 Chrome 可执行文件
      2. 本机已安装的 Chrome（Playwright ``channel="chrome"``）
      3. Playwright 自带 Chromium（``--no-chrome`` 或前两者均不可用时回退）

    Returns:
        playwright 的 Browser 实例
    """
    launch_args = ["--disable-blink-features=AutomationControlled"]
    if headless:
        launch_args += ["--no-sandbox", "--disable-setuid-sandbox"]

    # 1) 显式路径
    if chrome_path:
        chrome_path = os.path.expanduser(chrome_path)
        if not os.path.isfile(chrome_path):
            log.warning("指定的 --chrome-path 不存在: %s，回退其它方式", chrome_path)
        else:
            log.info("使用本机 Chrome（--chrome-path）: %s", chrome_path)
            return pw.chromium.launch(executable_path=chrome_path, headless=headless, args=launch_args)

    # 2) 本机已安装 Chrome
    if not no_chrome:
        try:
            log.info("尝试调用本机已安装的 Chrome（channel=chrome）...")
            return pw.chromium.launch(channel="chrome", headless=headless, args=launch_args)
        except Exception as e:
            log.warning("未找到本机 Chrome（%s），回退 Playwright 自带 Chromium", str(e).split("\n")[0])

    # 3) 自带 Chromium
    log.info("使用 Playwright 自带 Chromium")
    return pw.chromium.launch(headless=headless, args=launch_args)


def _check_playwright(log: logging.Logger) -> bool:
    """检查 Playwright 环境是否就绪（浏览器能启动即可）。

    优先检测本机已安装的 Chrome，其次回退到 Playwright 自带 Chromium。
    打印诊断信息，返回是否通过。
    """
    # 第一层：Python 包
    try:
        from playwright.sync_api import sync_playwright  # noqa: F811
        log.info("✅ playwright Python 包可导入")
    except ImportError:
        log.error("❌ playwright Python 包未安装（当前解释器: %s）", sys.executable)
        log.error("   → uv add playwright && uv run playwright install")
        return False

    chrome_path = _find_chrome_executable()
    if chrome_path:
        log.info("✅ 检测到本机 Chrome: %s", chrome_path)

    # 第二层：浏览器能启动（本机 Chrome 优先，Chromium 兜底）
    with sync_playwright() as pw:
        try:
            if chrome_path:
                b = pw.chromium.launch(executable_path=chrome_path, headless=True, args=["--no-sandbox"])
            else:
                b = pw.chromium.launch(channel="chrome", headless=True, args=["--no-sandbox"])
            b.close()
            log.info("✅ 本机 Chrome 启动正常")
            return True
        except Exception as e:
            log.info("本机 Chrome 不可用（%s），尝试自带 Chromium", str(e).split("\n")[0])

        try:
            b = pw.chromium.launch(headless=True, args=["--no-sandbox"])
            b.close()
            log.info("✅ Playwright 自带 Chromium 启动正常")
            return True
        except Exception as e:
            log.error("❌ Chromium 启动失败: %s", str(e).split("\n")[0])
            log.error("   → uv run playwright install chromium")
            return False


def _is_logged_in(page) -> bool:
    """判断 api.weibo.com/chat 当前是否处于登录态。

    判据：未登录时 URL 为 ``https://api.weibo.com/chat#/``，
    扫码登录成功后 hash 路由跳转为 ``.../#/chat``。
    用 URL hash 变化作为唯一可靠判据（page.url 检测不到 hash 路由，必须用
    JS 读 location.href）；evaluate 抛异常（页面已关闭等）安全降级为未登录。
    """
    try:
        href = page.evaluate("window.location.href") or ""
    except Exception:
        return False
    return "#/chat" in href


def _renew_cookie(db_path: str, log: logging.Logger, headless: bool, chrome_path: str = "", no_chrome: bool = False):
    """用 Playwright 打开 api.weibo.com/chat → 截图二维码 → 等你扫码 → 存 cookie

    浏览器默认优先调用本机已安装的 Chrome（更顺手、可复用你 Chrome 里的登录态），
    找不到时回退到 Playwright 自带 Chromium。

    Args:
        headless: True=无头模式（截图保存文件，靠文件查看二维码）；
                  False=有头模式（直接弹出浏览器窗口扫码，体验更直观）。
        chrome_path: 显式指定本机 Chrome 可执行文件路径（为空则自动探测）。
        no_chrome: True 时强制使用 Playwright 自带 Chromium，不调用本机 Chrome。
    """
    if not _check_playwright(log):
        log.error("请先解决以上问题后重试: uv run python crawl.py --renew-cookie")
        sys.exit(1)

    from playwright.sync_api import sync_playwright  # noqa: F811

    with sync_playwright() as pw:
        browser = _launch_browser(pw, headless=headless, chrome_path=chrome_path, no_chrome=no_chrome, log=log)
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

            current_href = page.evaluate("window.location.href")
            log.info("当前 URL: %s", current_href)

            if _is_logged_in(page):
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
                        if _is_logged_in(page):
                            log.info("检测到页面跳转: %s → %s", current_href, page.evaluate("window.location.href"))
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

    if "SUB" not in deduped:
        log.error("未提取到 SUB cookie，登录可能未成功，请重新扫码")
        sys.exit(1)

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


def _do_list_members(db_path: str, gid: int, log: logging.Logger):
    """列出指定群内发过言的成员（sender_id / 昵称 / 发言数 / 最近发言时间）。"""
    if not gid:
        log.error("--list-members 需要配合 --gid 指定群")
        return
    from weibo_im.export import list_members
    members = list_members(db_path, gid)
    if members:
        from weibo_im.export import ms_to_cst
        for m in members:
            ts = m.get("last_msg_at") or 0
            last = ms_to_cst(ts) or "(无)"
            log.info("  sender_id=%-12d 发言=%-5d 最近=%s  %s",
                     m["sender_id"], m["msg_count"], last, m["sender_name"] or "(未知)")


def _do_export(db_path: str, gid: int, sender_name: str, sender_id: int,
               since: str, until: str, output: str, compact: bool, fmt: str, log: logging.Logger,
               since_mid: str = "", with_meta: bool = False, slim: bool = False,
               watermark_file: str = ""):
    """导出指定群内某成员本人发言为文件（JSON 或 CSV，用于 AI 分析）。

    参数校验后委托 weibo_im.export 模块完成 DB 查询、数据组装、文件写入。

    增量导出（水位线）：
      - since_mid：只导出 mid 大于此值的消息（知识库未收录的新消息）。
      - watermark_file：未给 since_mid 时从此文件读取 max_mid 自动续跑。
      - with_meta：输出带 mid + created_at 列，便于本地核验（不建议上传知识库，mid 浪费 token）。
      - slim：输出 created_at,text 两列，上传知识库推荐用此格式（体积小、保留时间线）。
    """
    if not gid:
        log.error("--export 需要配合 --gid 指定群")
        return
    if not sender_name and not sender_id:
        log.error("--export 需要指定成员：--sender-name 或 --sender-id 至少给一个")
        log.error("提示：用 --list-members --gid GID 查看群内成员昵称/ID")
        return

    from weibo_im.export import export_member_messages
    since_ms = _parse_since(since) if since else None
    until_ms = _parse_since(until) if until else None

    result = export_member_messages(
        db_path=db_path, gid=gid,
        sender_name=sender_name or None,
        sender_id=sender_id or None,
        since_ms=since_ms, until_ms=until_ms,
        output_path=output or None,
        compact=compact,
        fmt=fmt,
        since_mid=since_mid or None,
        with_meta=with_meta,
        slim=slim,
        watermark_file=watermark_file or None,
    )

    count = result["count"]
    output_path = result["path"]
    log.info("已导出 %d 条发言 → %s", count, output_path)
    if count:
        log.info("时间范围: %s ~ %s (CST)", result["start_cst"], result["end_cst"])
        if result.get("max_mid"):
            log.info("本批最大 mid: %s（上传成功后用此值推进水位线）", result["max_mid"])
    else:
        log.warning("未找到匹配的发言，请用 --list-members 核对成员标识与 --gid")


def _do_daily_export(db_path: str, gid: int, date_str: str,
                     sender_name: str, sender_id: int,
                     output: str, log: logging.Logger):
    """导出每日群聊日报 CSV（含当日群全部消息作为上下文，用于 AI 生成成员日报）。

    参数校验后委托 weibo_im.export.export_daily_report 完成。
    """
    if not gid:
        log.error("--daily 需要配合 --gid 指定群")
        return
    if not date_str:
        log.error("--daily 需要配合 --date 指定日期（格式 YYYY-MM-DD）")
        return
    if not sender_name and not sender_id:
        log.error("--daily 需要指定成员：--sender-name 或 --sender-id 至少给一个")
        log.error("提示：用 --list-members --gid GID 查看群内成员昵称/ID")
        return

    from weibo_im.export import export_daily_report
    try:
        result = export_daily_report(
            db_path=db_path, gid=gid, date_str=date_str,
            sender_name=sender_name or None,
            sender_id=sender_id or None,
            output_path=output or None,
        )
    except ValueError as e:
        log.error("日期解析失败: %s", e)
        return

    count = result["count"]
    log.info("已导出日报 %d 条消息 → %s", count, result["path"])
    log.info("群聊: %s", result["group_name"] or "(未知群)")
    log.info("日期: %s", result["date"])
    target_name = result["target_sender_name"] or "(未知)"
    target_sid = result["target_sender_id"]
    log.info("目标成员: %s (sender_id=%s)", target_name, target_sid if target_sid else "无")
    log.info("  本人发言: %d 条", result["target_count"])
    log.info("  上下文消息: %d 条", result["context_count"])
    if result["target_count"] == 0:
        log.warning("目标成员当日无发言，CSV 中 is_target 全为 0")


def _do_diff(old_path: str, new_path: str, output: str, fmt: str, log: logging.Logger):
    """对比两个导出数据包，输出新增消息的补充包（用于增量更新 NotebookLM 等）。

    委托 weibo_im.export.diff_export_files 完成文件解析（JSON/CSV 自适应）、
    组合键差集（sender_name+text+出现序号）、补充包写出。
    """
    if not old_path:
        log.error("--diff 需要配合 --old 指定旧数据包路径")
        return
    if not new_path:
        log.error("--diff 需要配合 --new 指定新数据包路径")
        return
    from weibo_im.export import diff_export_files
    try:
        result = diff_export_files(
            old_path=old_path,
            new_path=new_path,
            output_path=output or None,
            fmt=fmt,
        )
    except FileNotFoundError as e:
        log.error("文件不存在: %s", e)
        return
    except (ValueError, OSError) as e:
        log.error("文件解析失败: %s", e)
        return
    log.info("对比完成：旧=%d 新=%d 新增=%d 重复=%d → %s",
             result["old_count"], result["new_count"],
             result["delta_count"], result["dup_count"], result["path"])
    if not result["same_sender"]:
        log.warning("两包前 10 条发言者不一致，请确认是同一群同一成员")
    if result["delta_count"] == 0:
        log.info("无新增消息")


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
    parser.add_argument("--chrome-path", default="",
                        help="--renew-cookie 时指定本机 Chrome 可执行文件路径（默认自动探测）")
    parser.add_argument("--no-chrome", action="store_true",
                        help="--renew-cookie 时强制使用 Playwright 自带 Chromium（不调用本机 Chrome）")
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
    parser.add_argument("--list-members", action="store_true",
                        help="列出指定群内发过言的成员（需配合 --gid）")
    parser.add_argument("--export", action="store_true",
                        help="导出指定群某成员的发言为 JSON（用于 AI 分析）；需配合 --gid 与成员标识")
    parser.add_argument("--sender-name", default="",
                        help="--export 时按发送者昵称筛选")
    parser.add_argument("--sender-id", type=int, default=0,
                        help="--export 时按发送者 ID 筛选（比昵称更稳定）")
    parser.add_argument("--until", default="",
                        help="--export 的结束时间（日期如 2026-07-14 或毫秒时间戳，CST 口径，开区间）")
    parser.add_argument("--output", "-o", default="",
                        help="--export 的输出文件路径（默认 项目目录/export_<gid>_<sender>.json）")
    parser.add_argument("--compact", action="store_true",
                        help="--export 时输出紧凑（minified）JSON，去掉缩进与多余空白，减小文件体积")
    parser.add_argument("--format", choices=["json", "csv"], default="json",
                        help="--export 的输出格式：json（默认，纯数组）或 csv（表头 sender_name,text）")
    parser.add_argument("--daily", action="store_true",
                        help="导出每日群聊日报 CSV（含当日群全部消息作为上下文）；需配合 --gid --date --sender-name/--sender-id")
    parser.add_argument("--date", default="",
                        help="--daily 的日期，格式 YYYY-MM-DD 或 YYYY/MM/DD（CST 当日）")
    parser.add_argument("--diff", action="store_true",
                        help="对比两个导出数据包，输出新增消息的补充包；需配合 --old / --new")
    parser.add_argument("--old", default="",
                        help="--diff 的旧数据包路径（上次导出）")
    parser.add_argument("--new", default="",
                        help="--diff 的新数据包路径（本次导出）")
    parser.add_argument("--diff-format", choices=["json", "csv"], default="json",
                        help="--diff 补充包输出格式（默认 json，与 --export 一致）")
    parser.add_argument("--diff-output", "-O", default="",
                        help="--diff 补充包输出路径（默认 系统临时目录/delta_<时间戳>.<ext>）")
    parser.add_argument("--since-mid", default="",
                        help="--export 时只导出 mid 大于此值的消息（增量导出，配合水位线使用）")
    parser.add_argument("--with-meta", action="store_true",
                        help="--export 时输出带 mid 与 created_at 列（本地核验用，不建议上传知识库）")
    parser.add_argument("--slim", action="store_true",
                        help="--export 时输出 created_at,text 两列（上传知识库推荐：体积小、保留时间线、去冗余）")
    parser.add_argument("--watermark-file", default="",
                        help="--export 时从此文件读取 max_mid 作为 --since-mid（文件不存在则全量导出）")
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
        _renew_cookie(db_path, log, headless=args.headless,
                      chrome_path=args.chrome_path, no_chrome=args.no_chrome)
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

    if args.list_members:
        _do_list_members(db_path, args.gid, log)
        return

    if args.export:
        _do_export(db_path, args.gid, args.sender_name, args.sender_id,
                   args.since, args.until, args.output, args.compact, args.format, log,
                   since_mid=args.since_mid, with_meta=args.with_meta,
                   slim=args.slim, watermark_file=args.watermark_file)
        return

    if args.daily:
        _do_daily_export(db_path, args.gid, args.date,
                         args.sender_name, args.sender_id, args.output, log)
        return

    if args.diff:
        _do_diff(args.old, args.new, args.diff_output, args.diff_format, log)
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
        log.info("下载所有 pending 媒体文件...")
        crawler.download_all_media()
        stats = crawler.stats()
        log.info("媒体统计: %d done, %d pending, %d failed",
                 stats["media_done"], stats["media_pending"], stats["media_failed"])
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
    except CookieExpiredError:
        raise
    except Exception as e:
        log.error("爬取失败: %s", e)
        return

    log.info("爬取完成: %d 个群, %d 个有新消息, +%d 条, %d 个媒体文件待下载",
             result["groups"], result["groups_with_new"],
             result["new_messages"], result["media_pending"])

    stats = crawler.stats()
    log.info("数据库统计: %d 条消息, %d 个群, %d 个媒体已下载",
             stats["messages"], stats["groups_with_msgs"], stats["media_done"])


if __name__ == "__main__":
    try:
        main()
    except CookieExpiredError:
        logging.error("Cookie 已过期，请运行: uv run crawl.py --renew-cookie")
        raise SystemExit(2)
