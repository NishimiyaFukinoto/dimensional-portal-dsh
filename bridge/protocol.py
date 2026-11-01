"""次元门 · 手机桥接 —— 协议常量与轻量工具函数。

这个模块只依赖标准库，方便脱离 AstrBot 单独跑测试。
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

# 协议版本：手机端 /hb 里会带 agent_version 与 protocol 字段，服务端据此判断兼容性。
PROTOCOL_VERSION = 1

# ---------------------------------------------------------------- 动作名
# 手机端 executor 里的 switch 分支必须和这里一一对应（见 phone/dimensional_portal_agent.js）。
ACT_SCREEN = "screen"
ACT_UI = "ui"
ACT_TAP = "tap"
ACT_LONG_PRESS = "long_press"
ACT_SWIPE = "swipe"
ACT_TEXT = "text"
ACT_KEY = "key"
ACT_OPEN_APP = "open_app"
ACT_FIND_TEXT = "find_text"
ACT_WAIT = "wait"
ACT_STATUS = "status"
ACT_APPS = "apps"  # 读手机上的应用列表（控制台「应用访问」用）

# M3 / M4 预留（手机端先返回 not_implemented）
ACT_CAMERA_PHOTO = "camera_photo"
ACT_CAMERA_VIDEO = "camera_video"
ACT_DOWNLOAD_APK = "download_apk"
ACT_INSTALL_APK = "install_apk"

M1_ACTIONS = frozenset(
    {
        ACT_SCREEN,
        ACT_UI,
        ACT_TAP,
        ACT_LONG_PRESS,
        ACT_SWIPE,
        ACT_TEXT,
        ACT_KEY,
        ACT_OPEN_APP,
        ACT_FIND_TEXT,
        ACT_WAIT,
        ACT_STATUS,
        ACT_APPS,
    }
)

# 这些动作会在手机屏幕上留下痕迹，审计日志里标记为 write
WRITE_ACTIONS = frozenset(
    {
        ACT_TAP,
        ACT_LONG_PRESS,
        ACT_SWIPE,
        ACT_TEXT,
        ACT_KEY,
        ACT_OPEN_APP,
        ACT_FIND_TEXT,
        ACT_INSTALL_APK,
    }
)

# ---------------------------------------------------------------- 小工具


def new_task_id() -> str:
    """任务 id：t_ + 12 位十六进制，足够避免碰撞且便于日志检索。"""
    return "t_" + uuid.uuid4().hex[:12]


def now_ts() -> float:
    """秒级浮点时间戳（保留 3 位小数）。"""
    return round(time.time(), 3)


def dumps(obj: Any) -> str:
    """紧凑 JSON，中文不转义，方便日志直接看。"""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def loads(raw: bytes | str | None) -> Any:
    """宽松解析：空内容返回 None，非法 JSON 返回 None（由调用方决定如何报错）。"""
    if not raw:
        return None
    if isinstance(raw, bytes | bytearray):
        try:
            raw = bytes(raw).decode("utf-8")
        except UnicodeDecodeError:
            return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def truncate(text: str, limit: int, suffix: str = "…") -> str:
    """按字符截断，超长时补省略号。"""
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + suffix
