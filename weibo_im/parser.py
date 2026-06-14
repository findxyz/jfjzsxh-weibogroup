"""消息解析 — 将 API 原始消息解析为统一格式

支持两种来源：
  1. REST API (query_messages.json) — 主处理路径
  2. CometD 推送 (channel /im/) — 保留兼容
"""
from __future__ import annotations

import json
import hashlib
import logging
from typing import Any

from .types import (
    MSG_TYPES, SKIP_TYPES, msg_type_name, media_type_name, is_redpacket,
)

log = logging.getLogger("weibo_im.parser")


def resolve_fid(raw: dict, info: dict) -> str | None:
    """从消息中提取文件 ID（fid）"""
    fids = info.get("fids") or raw.get("fids") or []
    if isinstance(fids, list) and fids:
        return str(fids[0])
    # 一些消息把 fid 直接放在顶层
    fid = info.get("fid") or raw.get("fid") or ""
    return str(fid) if fid else None


def resolve_media_orig_url(fid: str, media_type: int, source: str = "209678993") -> str:
    """fid 转 upload API URL"""
    base = f"https://upload.api.weibo.com/2/mss/msget?fid={fid}&source={source}"
    if media_type == 1:  # 图片
        return f"{base}&imageType=origin"
    return base


def extract_pic_infos(raw: dict, info: dict) -> list[dict]:
    """提取小程序/卡片中的 pic_infos"""
    pi = info.get("pic_infos") or raw.get("pic_infos")
    if pi:
        if isinstance(pi, list):
            return pi
        if isinstance(pi, dict):
            return [pi]
    return []


def extract_url_objects(raw: dict, info: dict) -> list[dict]:
    """提取 url_objects"""
    uo = info.get("url_objects") or raw.get("url_objects")
    if isinstance(uo, list):
        return uo
    return []


def extract_template_data(info: dict) -> dict:
    """提取系统消息模板变量"""
    data = {}
    # 模板变量如 {{name.DATA}}, {{userA.DATA}}, {{operator.DATA}}
    d = info.get("data")
    if isinstance(d, dict):
        for k, v in d.items():
            if isinstance(v, dict):
                data[k] = {
                    "value": v.get("value", ""),
                    "scheme": v.get("scheme", ""),
                    "color": v.get("color", ""),
                }
            else:
                data[k] = v
    return data


def extract_attitude_data(info: dict) -> dict:
    """提取态度/点赞数据 (msg_type=9999)"""
    d = info.get("data", {})
    if isinstance(d, dict) and "attitudes" in d:
        return {
            "mid": d.get("mid", ""),
            "attitudes": d.get("attitudes", []),
            "users": d.get("users", {}),
        }
    att = info.get("attitude_info")
    if att:
        return att
    return {}


def extract_recall_data(info: dict) -> dict:
    """提取撤回信息 (msg_type=331)"""
    ids = info.get("ids") or []
    return {
        "mids": [str(i) for i in ids] if isinstance(ids, list) else [str(ids)],
        "recall_by": info.get("recall_sender_name", ""),
    }


def extract_annotations(info: dict) -> dict:
    ann = info.get("annotations", info.get("ext_text", {}))
    if isinstance(ann, dict):
        return ann
    return {}


# ── 主解析函数 ──────────────────────────────────────────────


def parse_message(raw: dict, default_gid: int | None = None) -> dict | None:
    """将 API 原始消息解析为统一字典格式。

    Args:
        raw: API 返回的原始消息字典
        default_gid: 如果消息没有 gid 时的默认值

    Returns:
        解析后的消息字典，或 None（应跳过的消息）
    """
    info = raw.get("info", {})
    # 兼容 REST API 直接平铺的格式
    # REST API: msg={type, content, from_user, ...} 无 info 包装
    # CometD:   msg={type, sub_type, info={...}}
    is_cometd = bool(info)

    # ── 类型 ─────────────────────────────────────────────
    if is_cometd:
        # CometD 推送：顶层 type = "groupchat"/"msg"
        # sub_type = 具体消息码，可能在顶层或在 info.type
        msg_type = raw.get("sub_type") or info.get("type", 321)
    else:
        # REST API：type = 消息码
        msg_type = raw.get("type", 321)
    msg_type = int(msg_type)

    if msg_type in SKIP_TYPES:
        return None

    # ── 发送者 ────────────────────────────────────────────
    from_user = info.get("from_user") or raw.get("from_user", {})
    if isinstance(from_user, dict):
        sender_id = int(from_user.get("id", info.get("from_uid", raw.get("from_uid", 0))) or 0)
        sender_name = from_user.get("screen_name", "")
    else:
        sender_id = int(info.get("from_uid", raw.get("from_uid", 0)))
        sender_name = ""

    # ── 内容 ─────────────────────────────────────────────
    content = info.get("content") or raw.get("content", "")
    text = str(content) if content else ""

    # ── 媒体类型 ──────────────────────────────────────────
    media_type = int(info.get("media_type") or raw.get("media_type", 0))

    # 区分视频和红包
    if media_type == 13 and is_redpacket(raw):
        media_type = 16  # 自定义：红包

    # ── 文件 ──────────────────────────────────────────────
    fid = resolve_fid(raw, info)
    media_orig_url = ""
    if fid:
        media_orig_url = resolve_media_orig_url(fid, media_type)

    # 对于 media_type=14, url_objects 中可能含 pic_ids
    # 也用 fids，但有的分享用 url_objects.status.pic_ids
    # 这里仅提取顶层 fids

    # ── 结构化数据 ────────────────────────────────────────
    url_objects = json.dumps(extract_url_objects(raw, info), ensure_ascii=False)
    pic_infos = json.dumps(extract_pic_infos(raw, info), ensure_ascii=False)

    template = info.get("template", "")
    template_data = json.dumps(extract_template_data(info), ensure_ascii=False)

    recall_data = extract_recall_data(info) if msg_type == 331 else {}
    recall_mids = json.dumps(recall_data.get("mids", []), ensure_ascii=False)
    recall_by = recall_data.get("recall_by", from_user.get("screen_name", ""))

    attitude_data = json.dumps(extract_attitude_data(info), ensure_ascii=False)

    # ── 元信息 ────────────────────────────────────────────
    gid = int(info.get("gid") or raw.get("gid", default_gid or 0))
    mid = str(info.get("id") or raw.get("id") or info.get("current_version") or "")
    if not mid and msg_type == 331:
        mid = str(info.get("time", raw.get("time", 0)))
    if not mid:
        return None

    group_name = info.get("group_name", "")
    faith_status = int(info.get("faith_status", 0) or 0)
    faith_icon = info.get("faith_icon", "")
    annotations = json.dumps(extract_annotations(info), ensure_ascii=False)

    created_at = info.get("time") or raw.get("time", 0)
    if isinstance(created_at, str):
        created_at = 0
    created_at = int(created_at)
    if created_at and created_at < 1_000_000_000_000:
        created_at = created_at * 1000

    # ── raw_json ──────────────────────────────────────────
    # 对 CometD 推送保留完整 info
    raw_json = json.dumps(raw, ensure_ascii=False)

    return {
        "mid": mid,
        "gid": gid,
        "msg_type": msg_type,
        "msg_type_name": msg_type_name(msg_type),
        "media_type": media_type,
        "sender_id": sender_id,
        "sender_name": sender_name,
        "text": text,
        "fid": fid or "",
        "media_orig_url": media_orig_url,
        "media_local_path": "",
        "url_objects": url_objects,
        "pic_infos": pic_infos,
        "template": template,
        "template_data": template_data,
        "recall_mids": recall_mids,
        "recall_by": recall_by,
        "attitude_data": attitude_data,
        "faith_status": faith_status,
        "faith_icon": faith_icon,
        "group_name": group_name,
        "annotations": annotations,
        "created_at": created_at,
        "raw_json": raw_json,
    }


def parse_messages(messages: list[dict], default_gid: int | None = None) -> list[dict]:
    """批量解析消息列表"""
    result = []
    for raw in messages:
        try:
            pm = parse_message(raw, default_gid)
            if pm:
                result.append(pm)
        except Exception as e:
            log.warning("parse_message error: %s | raw=%s...", e,
                        json.dumps(raw, ensure_ascii=False)[:200])
    return result
