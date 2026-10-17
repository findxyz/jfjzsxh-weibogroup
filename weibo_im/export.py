"""成员发言导出 — 用于 AI 分析的数据导出模块

纯读操作：不修改任何表数据，不依赖 Cookie，不涉及网络请求。
供 crawl.py CLI 或程序化调用。
"""
from __future__ import annotations

import csv
import json
import logging
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from .db import set_db_path, init_db, get_conn, get_group_list

CST = timezone(timedelta(hours=8))
log = logging.getLogger("weibo_im.export")

# 导出文件默认写入系统临时目录，避免污染项目目录。
# 子目录名前缀 weibo_export_ 便于识别与批量清理。
_EXPORT_DIR = Path(tempfile.gettempdir()) / "weibo_export"


# ── 工具函数 ────────────────────────────────────────────────


def ms_to_cst(ms: int | None) -> str | None:
    """毫秒时间戳转 CST 格式化字符串；空则返回 None。"""
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=CST).strftime("%Y-%m-%d %H:%M:%S")


# ── 数据库查询（只读）───────────────────────────────────────


def get_members(gid: int) -> list[dict]:
    """返回群内发过言的成员列表（sender_id + sender_name），按最近发言倒序。

    沿用 server.py query_members_list 的口径：仅统计 msg_type IN (100, 321) 的
    真实用户发言，GROUP BY sender_id，给出 msg_count 与最近发言时间，便于定位
    准确昵称/ID。
    """
    conn = get_conn()
    rows = conn.execute(
        "SELECT sender_id, sender_name, MAX(created_at) AS last_msg_at, "
        "COUNT(*) AS msg_count FROM messages "
        "WHERE gid=? AND msg_type IN (100, 321) "
        "GROUP BY sender_id ORDER BY last_msg_at DESC",
        (gid,),
    ).fetchall()
    return [{"sender_id": r["sender_id"],
             "sender_name": r["sender_name"],
             "msg_count": r["msg_count"],
             "last_msg_at": r["last_msg_at"]} for r in rows]


def get_member_messages(
    gid: int,
    sender_id: int | None = None,
    sender_name: str | None = None,
    since_ms: int | None = None,
    until_ms: int | None = None,
    since_mid: str | None = None,
    msg_types: tuple[int, ...] = (100, 321),
) -> list[dict]:
    """返回指定群内某成员本人发言（默认普通消息 321 + 微博分享 100），按时间升序。

    - sender_id 与 sender_name 可任传其一或同时传入，按传入情况拼接 WHERE；
      优先用 sender_id（稳定），sender_name 便于按可感知昵称导出。
    - since_ms/until_ms 为 created_at 毫秒范围：since 闭区间 (>=)、until 开区间 (<)，
      与项目内 CST 日期区间 [start, end) 约定一致。
    - since_mid 为增量水位线：只取 mid 严格大于此值的消息（mid 为字符串，字典序比较
      等价于时间顺序，见 API.md §7.5）。用于"只导出知识库未收录的新消息"。
    - msg_types 默认 (100, 321)；传空元组则不加该过滤（一般不必要）。

    返回每行 dict（含 mid/gid/msg_type/msg_type_name/media_type/sender_id/
    sender_name/text/fid/media_orig_url/url_objects/created_at/group_name），
    按 created_at ASC 排列，契合按时间线的 AI 序列分析。
    """
    conn = get_conn()
    conds = ["gid=?"]
    params: list[Any] = [gid]
    if msg_types:
        placeholders = ",".join("?" for _ in msg_types)
        conds.append(f"msg_type IN ({placeholders})")
        params.extend(msg_types)
    if sender_id is not None:
        conds.append("sender_id=?")
        params.append(sender_id)
    if sender_name:
        conds.append("sender_name=?")
        params.append(sender_name)
    if since_ms is not None:
        conds.append("created_at>=?")
        params.append(since_ms)
    if until_ms is not None:
        conds.append("created_at<?")
        params.append(until_ms)
    if since_mid:
        # mid 存为 TEXT，字典序比较等价于时间顺序（同长数字串）
        conds.append("mid>?")
        params.append(str(since_mid))
    sql = (
        "SELECT mid, gid, msg_type, msg_type_name, media_type, "
        "sender_id, sender_name, text, fid, media_orig_url, "
        "url_objects, created_at, group_name "
        "FROM messages WHERE " + " AND ".join(conds) +
        " ORDER BY created_at ASC"
    )
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# ── JSON 组装 ───────────────────────────────────────────────


def build_export_json(rows: list[dict], with_meta: bool = False, slim: bool = False) -> list[dict]:
    """将查询结果组装为适合 AI 分析的导出数据。

    三种输出形态（按用途选择）：
    - 默认：每条含 sender_name / text 两项，聚焦发言内容。
    - with_meta=True：额外带 mid / created_at，含完整审计信息（本地核验用，
      **不建议上传知识库**——mid 是 18 位无意义数字，浪费 AI token）。
    - slim=True：仅 created_at / text 两项。用于上传知识库：保留时间线（宏观
      策略时效性必需），去掉 mid 与冗余 sender_name（单成员导出时 sender 全相同），
      体积比 with_meta 小约 40%。

    水位线（max_mid）追踪与文件列无关：export_member_messages 总会返回本批 max_mid，
    由调用方写回 .watermark.json，因此上传文件无需含 mid 列。

    按 created_at ASC 排列，契合按时间线的 AI 序列分析。
    """
    if with_meta:
        return [
            {
                "mid": str(r.get("mid") or ""),
                "created_at": ms_to_cst(r.get("created_at")) or "",
                "sender_name": r.get("sender_name") or "",
                "text": r.get("text") or "",
            }
            for r in rows
        ]
    if slim:
        return [
            {
                "created_at": ms_to_cst(r.get("created_at")) or "",
                "text": r.get("text") or "",
            }
            for r in rows
        ]
    return [
        {
            "sender_name": r.get("sender_name") or "",
            "text": r.get("text") or "",
        }
        for r in rows
    ]


# ── 水位线（增量导出游标）─────────────────────────────────


def _read_watermark(path: str) -> str | None:
    """从水位线文件读取上次已成功上传到知识库的 max_mid。

    文件格式: ``{"max_mid": "...", "updated_at": "...", "note": "..."}``。
    文件不存在或损坏返回 None（视为全量导出）。用于 --watermark-file 自动续跑。
    """
    p = Path(path)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        mid = data.get("max_mid")
        return str(mid) if mid else None
    except (OSError, ValueError):
        return None


def write_watermark(path: str, max_mid: str, *, note: str = "") -> None:
    """上传成功后推进水位线。

    在确认本批导出已成功入库知识库后调用，把 max_mid 写回水位线文件；
    下次导出会从该值之后取增量。**上传失败时不要调用**，否则会漏数据。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "max_mid": str(max_mid),
        "updated_at": datetime.now(tz=CST).strftime("%Y-%m-%d %H:%M:%S"),
        "note": note,
    }
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("水位线已推进: max_mid=%s → %s", max_mid, path)


# ── 高级编排（含 DB 初始化 + 文件写入）─────────────────────


def export_member_messages(
    db_path: str,
    gid: int,
    sender_name: str | None = None,
    sender_id: int | None = None,
    since_ms: int | None = None,
    until_ms: int | None = None,
    output_path: str | None = None,
    compact: bool = False,
    fmt: str = "json",
    since_mid: str | None = None,
    with_meta: bool = False,
    slim: bool = False,
    watermark_file: str | None = None,
) -> dict:
    """导出指定群内某成员本人发言为文件（JSON 或 CSV）。

    完整流程：初始化 DB 连接 → 查询消息 → 查询群名 → 组装数据 → 写入文件。

    增量导出（水位线）：
      - since_mid：只导出 mid 严格大于此值的消息（知识库未收录的新消息）。
      - watermark_file：未显式给 since_mid 时，从此文件读取 max_mid 作为水位线；
        文件不存在则全量导出（首次运行）。
      - 返回结果含 max_mid（本批已去噪导出的最大 mid），上传成功后用
        write_watermark 写回水位线文件，下次自动续跑。

    fmt="json"（默认）：输出纯 JSON 数组，每条含 sender_name / text 两项
        （with_meta=True 时额外带 mid / created_at）。compact=True 输出紧凑 JSON。
    fmt="csv"：CSV 文件，with_meta=False 表头 sender_name,text；with_meta=True
        表头 mid,created_at,sender_name,text（UTF-8-SIG，Excel 友好）。

    返回结果字典:
        {"path": str, "count": int, "start_cst": str, "end_cst": str,
         "group_name": str, "max_mid": str, "since_mid": str}
    """
    set_db_path(db_path)
    init_db()

    sid = sender_id if sender_id else None
    sname = sender_name or None

    # 水位线解析：显式 since_mid 优先；否则从 watermark_file 读取
    effective_since_mid = since_mid
    if not effective_since_mid and watermark_file:
        effective_since_mid = _read_watermark(watermark_file)
        if effective_since_mid:
            log.info("从水位线文件读取基线 max_mid=%s", effective_since_mid)
        else:
            log.info("水位线文件不存在或为空，将全量导出（首次运行）")

    rows = get_member_messages(
        gid=gid,
        sender_id=sid,
        sender_name=sname,
        since_ms=since_ms,
        until_ms=until_ms,
        since_mid=effective_since_mid,
    )

    # 过滤系统噪声（粉丝群提示、纯 URL 网页分享、"快来围观吧"自动提示）
    total_before = len(rows)
    rows = [r for r in rows if not _is_noise_message(r)]
    filtered_count = total_before - len(rows)
    if filtered_count:
        log.info("过滤 %d 条噪声消息（粉丝群/纯URL/快来围观吧）", filtered_count)

    # 查群名称
    group_name = ""
    for g in get_group_list():
        if g["gid"] == gid:
            group_name = g.get("name", "")
            break

    msgs = build_export_json(rows, with_meta=with_meta, slim=slim)

    # 确定输出路径（CSV 用 .csv 扩展名）
    if not output_path:
        sender_slug = sname or (str(sid) if sid else "member")
        safe = "".join(c for c in sender_slug if c.isalnum() or c in "._-") or "member"
        ext = "csv" if fmt == "csv" else "json"
        _EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        output_path = str(_EXPORT_DIR / f"export_{gid}_{safe}.{ext}")

    if fmt == "csv":
        if with_meta:
            fieldnames = ["mid", "created_at", "sender_name", "text"]
        elif slim:
            fieldnames = ["created_at", "text"]
        else:
            fieldnames = ["sender_name", "text"]
        with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for m in msgs:
                writer.writerow({k: m.get(k, "") for k in fieldnames})
    else:
        with open(output_path, "w", encoding="utf-8") as f:
            if compact:
                json.dump(msgs, f, ensure_ascii=False, separators=(",", ":"))
            else:
                json.dump(msgs, f, ensure_ascii=False, indent=2)

    start_cst = ms_to_cst(rows[0].get("created_at")) if rows else ""
    end_cst = ms_to_cst(rows[-1].get("created_at")) if rows else ""
    # 本批导出（已去噪）的最大 mid；上传成功后用 write_watermark 推进水位线
    max_mid = max((str(r.get("mid") or "") for r in rows), default="")

    if filtered_count:
        log.info(
            "已导出 %d 条发言（过滤噪声 %d 条）→ %s",
            len(msgs), filtered_count, output_path,
        )
    else:
        log.info("已导出 %d 条发言 → %s", len(msgs), output_path)

    return {
        "path": output_path,
        "count": len(msgs),
        "start_cst": start_cst,
        "end_cst": end_cst,
        "group_name": group_name,
        "max_mid": max_mid,
        "since_mid": effective_since_mid or "",
    }


def list_members(db_path: str, gid: int) -> list[dict]:
    """列出指定群内发过言的成员信息。

    初始化 DB → 查询成员列表 → 返回 dict 列表。
    每条含 sender_id / sender_name / msg_count / last_msg_at。
    """
    set_db_path(db_path)
    init_db()
    members = get_members(gid)
    if not members:
        log.info("该群还没有任何用户发言记录")
    else:
        log.info("群 %d 的发言成员（%d 人）:", gid, len(members))
        for m in members:
            ts = m.get("last_msg_at") or 0
            last = ms_to_cst(ts) or "(无)"
            log.info("  sender_id=%-12d 发言=%-5d 最近=%s  %s",
                     m["sender_id"], m["msg_count"], last, m["sender_name"] or "(未知)")
    return members


# ── 每日群聊日报导出 ───────────────────────────────────────


def _resolve_target_sender_id(
    gid: int,
    sender_name: str | None,
    sender_id: int | None,
) -> int | None:
    """解析目标成员的 sender_id（用于 is_target 标记）。

    - sender_id 已提供 → 直接返回。
    - 仅 sender_name → 查该群内该昵称最近一条发言的 sender_id（取最新值，避免改名漂移）。
    - 都未提供 → 返回 None（is_target 全为 0，仅导出当日群聊上下文）。
    """
    if sender_id:
        return int(sender_id)
    if not sender_name:
        return None
    conn = get_conn()
    row = conn.execute(
        "SELECT sender_id FROM messages "
        "WHERE gid=? AND sender_name=? AND msg_type IN (100, 321) "
        "ORDER BY created_at DESC LIMIT 1",
        (gid, sender_name),
    ).fetchone()
    return int(row["sender_id"]) if row else None


def _parse_date_to_day_range(date_str: str) -> tuple[int, int]:
    """将日期字符串解析为 CST 当日 [start_ms, end_ms) 毫秒区间。

    支持 "YYYY-MM-DD" 和 "YYYY/MM/DD" 两种格式。返回 (当日零点ms, 次日零点ms)。
    """
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            dt = datetime.strptime(date_str, fmt)
        except ValueError:
            continue
        start_dt = dt.replace(tzinfo=CST)
        start_ms = int(start_dt.timestamp() * 1000)
        end_ms = start_ms + 86400000
        return start_ms, end_ms
    raise ValueError(
        f"无法解析日期: {date_str}（支持格式 YYYY-MM-DD 或 YYYY/MM/DD）"
    )


def get_daily_context_messages(
    gid: int,
    date_str: str,
) -> list[dict]:
    """返回指定群当日全部用户发言（msg_type 100/321），按时间升序。

    作为日报导出的数据源：包含目标成员当日发言 + 其他成员当日发言（上下文）。
    每行 dict 含 mid/created_at/sender_id/sender_name/text/group_name。
    """
    start_ms, end_ms = _parse_date_to_day_range(date_str)
    conn = get_conn()
    rows = conn.execute(
        "SELECT mid, created_at, sender_id, sender_name, text, group_name "
        "FROM messages "
        "WHERE gid=? AND created_at>=? AND created_at<? "
        "AND msg_type IN (100, 321) "
        "ORDER BY created_at ASC",
        (gid, start_ms, end_ms),
    ).fetchall()
    return [dict(r) for r in rows]


def _is_noise_message(row: dict) -> bool:
    """判断消息是否为应剔除的系统噪声。

    规则：
    - sender_name == "粉丝群" → 系统自动提示（如"恭喜…获得标识"、"涉及资金问题请务必提高警惕"等）
    - text 去首尾空白后以 http:// 或 https:// 开头 → 纯 URL 网页分享（微博分享自动生成的纯链接）
    - text 包含 "快来围观吧" → 微博分享时系统自动生成的提示（"我发布了新微博，快来围观吧！"）

    返回 True 表示该条消息应被剔除。
    """
    sender_name = (row.get("sender_name") or "").strip()
    if sender_name == "粉丝群":
        return True
    text = (row.get("text") or "").strip()
    if not text:
        return False
    if text.startswith(("http://", "https://")):
        return True
    if "快来围观吧" in text:
        return True
    return False


def export_daily_report(
    db_path: str,
    gid: int,
    date_str: str,
    sender_name: str | None = None,
    sender_id: int | None = None,
    output_path: str | None = None,
) -> dict:
    """导出指定群+成员+日期的当日群聊上下文 CSV（用于 AI 生成成员日报）。

    完整流程：初始化 DB → 解析目标 sender_id → 查询当日全部用户发言 →
    标记 is_target → 查群名 → 写 CSV（utf-8-sig）。

    CSV 四列：created_at(CST字符串), sender_name, text, is_target(1/0)。

    返回结果字典:
        {"path": str,           # 输出文件路径
         "count": int,          # 当日消息总数
         "target_count": int,   # 目标成员发言数
         "context_count": int,  # 其他成员发言数（上下文）
         "date": str,           # 日期字符串
         "group_name": str,     # 群名
         "target_sender_id": int|None,  # 目标成员 sender_id
         "target_sender_name": str}     # 目标成员昵称（输入或由 sender_id 反查）
    """
    set_db_path(db_path)
    init_db()

    target_sid = _resolve_target_sender_id(gid, sender_name, sender_id)

    rows = get_daily_context_messages(gid, date_str)

    # 目标昵称：优先用输入；若仅传 sender_id 则反查最新昵称
    target_sname = sender_name or ""
    if not target_sname and target_sid:
        for r in rows:
            if r["sender_id"] == target_sid:
                target_sname = r.get("sender_name") or ""
                break
        if not target_sname:
            # 当日无发言，回退查历史最新昵称
            conn = get_conn()
            row = conn.execute(
                "SELECT sender_name FROM messages "
                "WHERE gid=? AND sender_id=? AND msg_type IN (100, 321) "
                "ORDER BY created_at DESC LIMIT 1",
                (gid, target_sid),
            ).fetchone()
            target_sname = row["sender_name"] if row else ""

    # 过滤系统噪声（粉丝群提示、纯 URL 网页分享、"快来围观吧"自动提示）
    total_before = len(rows)
    rows = [r for r in rows if not _is_noise_message(r)]
    filtered_count = total_before - len(rows)
    if filtered_count:
        log.info("过滤 %d 条噪声消息（粉丝群/纯URL/快来围观吧）", filtered_count)

    # 标记 is_target 并组装 CSV 行
    csv_rows = []
    target_count = 0
    for r in rows:
        is_target = 1 if (target_sid is not None and r["sender_id"] == target_sid) else 0
        if is_target:
            target_count += 1
        csv_rows.append({
            "created_at": ms_to_cst(r.get("created_at")) or "",
            "sender_name": r.get("sender_name") or "",
            "text": r.get("text") or "",
            "is_target": is_target,
        })

    # 查群名
    group_name = ""
    for g in get_group_list():
        if g["gid"] == gid:
            group_name = g.get("name", "")
            break

    # 确定输出路径
    if not output_path:
        sender_slug = target_sname or (str(target_sid) if target_sid else "member")
        safe = "".join(c for c in sender_slug if c.isalnum() or c in "._-") or "member"
        # 文件名中日期统一用 YYYY-MM-DD
        date_slug = date_str.replace("/", "-")
        _EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        output_path = str(
            _EXPORT_DIR / f"daily_{gid}_{safe}_{date_slug}.csv"
        )

    # 写 CSV（utf-8-sig，Excel 友好）
    with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["created_at", "sender_name", "text", "is_target"]
        )
        writer.writeheader()
        for row in csv_rows:
            writer.writerow(row)

    context_count = len(csv_rows) - target_count
    if filtered_count:
        log.info(
            "已导出日报 %d 条消息（目标 %d / 上下文 %d，过滤噪声 %d 条）→ %s",
            len(csv_rows), target_count, context_count,
            filtered_count, output_path,
        )
    else:
        log.info(
            "已导出日报 %d 条消息（目标 %d / 上下文 %d）→ %s",
            len(csv_rows), target_count, context_count, output_path,
        )

    return {
        "path": output_path,
        "count": len(csv_rows),
        "target_count": target_count,
        "context_count": context_count,
        "date": date_str,
        "group_name": group_name,
        "target_sender_id": target_sid,
        "target_sender_name": target_sname,
    }


# ── 增量对比（两个导出数据包求差集）────────────────────────


def _detect_format(filename: str, text: str) -> str:
    """检测文件格式：json 或 csv。优先扩展名，兜底看内容首字符。"""
    lower = (filename or "").lower()
    if lower.endswith(".json"):
        return "json"
    if lower.endswith(".csv"):
        return "csv"
    trimmed = text.lstrip()
    if trimmed.startswith(("[", "{")):
        return "json"
    return "csv"


def _parse_export_text(filename: str, text: str) -> list[dict]:
    """把导出文件文本解析为 [{sender_name, text}, ...] 数组。

    支持 JSON 数组 与 CSV（表头 sender_name,text，utf-8-sig 兼容）。
    """
    fmt = _detect_format(filename, text)
    if fmt == "json":
        data = json.loads(text)
        if not isinstance(data, list):
            raise ValueError("JSON 文件不是数组格式")
        return [{"sender_name": str(m.get("sender_name", "")),
                 "text": str(m.get("text", ""))} for m in data]
    return _parse_export_csv(text)


def _parse_export_csv(text: str) -> list[dict]:
    """轻量 CSV 解析：支持 BOM、引号包裹、字段内逗号/换行/引号转义。

    仅取前两个字段 sender_name, text；跳过表头（含 sender_name）。
    """
    if text and text[0] == "\ufeff":
        text = text[1:]
    rows: list[list[str]] = []
    i, n = 0, len(text)
    field, row, in_quotes = "", [], False
    while i < n:
        c = text[i]
        if in_quotes:
            if c == '"':
                if i + 1 < n and text[i + 1] == '"':
                    field += '"'
                    i += 2
                    continue
                in_quotes = False
                i += 1
                continue
            if c == "\r" and i + 1 < n and text[i + 1] == "\n":
                field += "\n"
                i += 2
                continue
            field += c
            i += 1
            continue
        if c == '"':
            in_quotes = True
            i += 1
            continue
        if c == ",":
            row.append(field)
            field = ""
            i += 1
            continue
        if c == "\r":
            i += 1
            continue
        if c == "\n":
            row.append(field)
            field = ""
            if row and not (len(row) == 1 and row[0] == ""):
                rows.append(row)
            row = []
            i += 1
            continue
        field += c
        i += 1
    if field != "" or row:
        row.append(field)
        if row and not (len(row) == 1 and row[0] == ""):
            rows.append(row)

    start = 0
    if rows and len(rows[0]) >= 2 and "sender_name" in rows[0][0].lower():
        start = 1
    result = []
    for r in rows[start:]:
        if len(r) < 2:
            continue
        result.append({"sender_name": r[0], "text": r[1]})
    return result


def compute_delta(old_rows: list[dict], new_rows: list[dict]) -> list[dict]:
    """组合键差集：返回新包有但旧包没有的消息。

    去重键 = (sender_name, text, 出现序号)。旧包建 baseKey->count 映射，
    新包按序消费配额，超出部分即为新增（正确处理"同文本多发多次"）。
    """
    old_counts: dict[tuple[str, str], int] = {}
    for r in old_rows:
        base = (r["sender_name"], r["text"])
        old_counts[base] = old_counts.get(base, 0) + 1
    cursor: dict[tuple[str, str], int] = {}
    delta = []
    for r in new_rows:
        base = (r["sender_name"], r["text"])
        used = cursor.get(base, 0)
        limit = old_counts.get(base, 0)
        if used < limit:
            cursor[base] = used + 1
        else:
            delta.append(r)
    return delta


def diff_export_files(
    old_path: str,
    new_path: str,
    output_path: str | None = None,
    fmt: str = "json",
) -> dict:
    """对比两个导出数据包，输出新增消息的补充包。

    读取旧包（上次导出）与新包（本次导出），以 (sender_name, text, 出现序号)
    组合键求差集，得到新包有而旧包没有的增量消息。结果写入补充包文件，
    格式与 --export 一致（json/csv），可直接上传 NotebookLM。

    参数:
        old_path: 旧数据包路径
        new_path: 新数据包路径
        output_path: 输出路径（默认 系统临时目录/delta_<时间戳>.<ext>）
        fmt: 输出格式 json（默认）或 csv

    返回:
        {"old_count", "new_count", "delta_count", "dup_count",
         "same_sender", "path"}
    """
    old_text = Path(old_path).read_text(encoding="utf-8-sig")
    new_text = Path(new_path).read_text(encoding="utf-8-sig")

    old_rows = _parse_export_text(old_path, old_text)
    new_rows = _parse_export_text(new_path, new_text)

    # 前 10 条 sender_name 核对（同一份导出文件里发言者通常一致）
    k = min(10, len(old_rows), len(new_rows))
    old_top = [r["sender_name"] for r in old_rows[:k]]
    new_top = [r["sender_name"] for r in new_rows[:k]]
    same_sender = old_top == new_top

    delta = compute_delta(old_rows, new_rows)

    if not output_path:
        ts = datetime.now(tz=CST)
        stamp = ts.strftime("%Y%m%d_%H%M")
        _EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        output_path = str(_EXPORT_DIR / f"delta_{stamp}.{fmt}")

    if fmt == "csv":
        with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["sender_name", "text"])
            writer.writeheader()
            for m in delta:
                writer.writerow(m)
    else:
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(delta, f, ensure_ascii=False, indent=2)

    log.info("旧包: %d 条 | 新包: %d 条 | 新增: %d 条 | 重复: %d 条",
             len(old_rows), len(new_rows), len(delta),
             len(new_rows) - len(delta))
    if not same_sender:
        log.warning("两包前 10 条发言者不一致，请确认是同一群同一成员")
    log.info("补充包已写入: %s", output_path)

    return {
        "old_count": len(old_rows),
        "new_count": len(new_rows),
        "delta_count": len(delta),
        "dup_count": len(new_rows) - len(delta),
        "same_sender": same_sender,
        "path": output_path,
    }
