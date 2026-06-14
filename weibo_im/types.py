"""
消息类型定义 — 从实际数据库中分析归纳
"""
from __future__ import annotations

# ── 消息类型码 (msg_type) ──────────────────────────────────
# 对应 API 响应中的 type 字段 (REST API) / info.type 字段 (CometD)

MSG_TYPES: dict[int, tuple[str, str]] = {
    100: ("weibo_share",      "微博分享"),
    320: ("invite",           "邀请入群"),
    321: ("normal",           "普通消息"),
    322: ("join",             "新人入群"),
    323: ("leave",            "退群"),
    324: ("kick",             "被踢出群"),
    325: ("rename",           "群名修改"),
    327: ("transfer",         "群主转让"),
    331: ("recall",           "消息撤回"),
    332: ("sync",             "协议同步"),   # 心跳包，无用户内容
    333: ("silent_change",    "免打扰变更"),
    335: ("group_update",     "群信息更新"),
    337: ("admin_change",     "管理员变更"),
    421: ("join_request",     "入群申请"),
    429: ("removed",          "被移出群"),
    499: ("notice",           "群通知"),
    9999: ("attitude",        "态度更新"),
}

# 需要跳过（无用户消息内容）
SKIP_TYPES = {332, 9999}

# ── 媒体类型 (media_type) ──────────────────────────────────

MEDIA_TYPES: dict[int, tuple[str, str]] = {
    0:  ("text",           "纯文本"),
    1:  ("image",          "图片"),
    4:  ("unknown_4",      "未知-4"),
    5:  ("file",           "文件"),     # PDF/DOC/ZIP 等附件
    9:  ("unknown_9",      "未知-9"),
    10: ("video",          "视频"),     # media_type=10 也是视频
    11: ("unknown_11",     "未知-11"),
    13: ("video_or_rp",    "视频/红包"),
    14: ("link",           "链接/卡片分享"),
    15: ("miniprogram",    "小程序卡片"),
}

# ── 工具函数 ────────────────────────────────────────────────


def msg_type_name(code: int) -> str:
    return MSG_TYPES.get(code, ("unknown", f"未知({code})"))[1]


def msg_type_slug(code: int) -> str:
    return MSG_TYPES.get(code, ("unknown", "未知"))[0]


def media_type_name(code: int) -> str:
    return MEDIA_TYPES.get(code, ("unknown", f"未知({code})"))[1]


def is_redpacket(msg: dict) -> bool:
    """media_type=13 时判断是否红包"""
    content = msg.get("content", "")
    return "收到红包消息" in content


# 视频媒体类型集合（体积大，可配置跳过下载）
VIDEO_MEDIA_TYPES: set[int] = {10, 13}
