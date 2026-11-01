"""次元门的执行层：设备状态机 + 11 个动作，进程内被 daemon 和工具层共用。

DSH 版没有 QQ 也没有 AstrBot：daemon 起 HTTP 服务等手机来长轮询，
MCP 工具层通过 daemon 的本机接口下发动作。这个文件只负责业务语义：
1. 起停 HTTP 桥接服务；
2. 把动作下发下去、等结果、翻译成模型/人话都能读懂的中文；
3. 截图落盘（不再推 QQ，改成给调用方一个文件路径）。

真正的协议与状态机在 bridge/ 和 safety/ 里，方便脱离任何宿主单测。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import socket
import time
from pathlib import Path
from typing import Any

from bridge.hub import DeviceHub, DeviceOffline, TaskRejected, TaskTimeout
from bridge.protocol import (
    ACT_APPS,
    ACT_ICON,
    ACT_FIND_TEXT,
    ACT_KEY,
    ACT_LONG_PRESS,
    ACT_OPEN_APP,
    ACT_SCREEN,
    ACT_SWIPE,
    ACT_TAP,
    ACT_TEXT,
    ACT_UI,
    WRITE_ACTIONS,
)
from bridge.server import PhoneBridgeServer
from safety.policy import SafetyPolicy

logger = logging.getLogger("dimensional_portal")

DATA_DIR = Path(os.environ.get("DP_DATA_DIR") or (Path(__file__).resolve().parents[1] / "data"))
SHOT_DIR = DATA_DIR / "shots"
SHOT_KEEP_S = 3600  # 截图保留 1 小时（避免无限涨）
ICON_DIR = DATA_DIR / "icons"  # 应用图标缓存（按需拉取，一个包一个 PNG）
# 包名 / 分类 id 的形状校验：只用来拒绝奇怪输入，不追求完美
_SAFE_PKG = re.compile(r"^[A-Za-z0-9_.]{2,128}$")
_SAFE_CAT = re.compile(r"^[A-Za-z0-9_-]{1,24}$")

# 手机端 actKey 只支持这些；menu/power 等无障碍注入不了的键不要写进来，
# 否则插件放行、手机端抛「不认识的按键」，模型看到的是假支持。
VALID_KEYS = {
    "back", "home", "recents", "enter", "delete", "volume_up", "volume_down",
}


def _audit_path() -> Path:
    """审计日志放项目 data/ 下；升级代码不会丢。"""
    import os

    override = os.environ.get("DIMENSIONAL_PORTAL_AUDIT_PATH")
    if override:
        return Path(override)
    base = DATA_DIR
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        base = Path(__file__).resolve().parent
    return base / "audit.jsonl"


def _cfg_int(value: Any, default: int, key: str) -> int:
    """配置里的数值可能是面板填错的字符串；别让一个错字把整个插件搞挂。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.warning(
            "[dimensional-portal] 配置 %s=%r 不是整数，已回退默认值 %s（去面板改一下）", key, value, default
        )
        return default


def _cfg_float(value: Any, default: float, key: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        logger.warning(
            "[dimensional-portal] 配置 %s=%r 不是数字，已回退默认值 %s（去面板改一下）", key, value, default
        )
        return default


def _local_ip() -> str:
    """取本机在局域网/Tailscale 里的地址，只用来打印控制台网址。"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("10.255.255.255", 1))  # 不会真的发包，只是让内核选出口网卡
            return str(sock.getsockname()[0])
        finally:
            sock.close()
    except OSError:
        pass
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return "127.0.0.1"


_EDITABLE_INTS: dict[str, tuple[int, int, str]] = {
    "rate_limit_per_minute": (1, 600, "每分钟操作上限"),
    "task_timeout_ms": (1000, 120000, "任务超时（毫秒）"),
    "offline_after_seconds": (5, 600, "离线判定（秒）"),
    "screenshot_quality": (1, 95, "截图质量"),
    "max_screenshot_kb": (100, 20000, "单图上限（KB）"),
    "preview_min_interval_ms": (200, 60000, "预览最小间隔（毫秒）"),
}

_EDITABLE_BOOLS: dict[str, str] = {
    "allow_screenshot_sensitive": "敏感界面允许截图",
    "allow_sensitive_write": "敏感界面允许点击输入",
    "preview_enabled": "实时预览",
}

AUDIT_PAGE_DEFAULT = 50
AUDIT_PAGE_MAX = 200
AUDIT_READ_TAIL_BYTES = 512 * 1024

# 控制台实时预览：手机端按 720 宽出图，控制台再等比放大到「你点的那一点」。
PREVIEW_MAX_EDGE = 720
PREVIEW_TIMEOUT_MS = 15000


class _PreviewError(Exception):
    """预览取不到图。offline / sensitive 让 HTTP 层给出不同的状态码。"""

    def __init__(
        self,
        message: str,
        *,
        offline: bool = False,
        sensitive: bool = False,
    ) -> None:
        super().__init__(message)
        self.offline = offline
        self.sensitive = sensitive


def _audit_match(item: dict[str, Any], kind: str, keyword: str) -> bool:
    """审计筛选：kind 支持 all / task / admin / deny / emergency_stop / preview。

    deny 是一组（deny_rate、deny_screenshot、deny_sensitive_write、deny_app、
    deny_blacklist 都算拦截）；关键字在整条记录里做不区分大小写的包含匹配。
    """
    if kind and kind not in ("all", "*"):
        item_kind = str(item.get("kind") or "")
        if kind == "deny":
            if not item_kind.startswith("deny"):
                return False
        elif item_kind != kind:
            return False
    if keyword:
        try:
            hay = json.dumps(item, ensure_ascii=False).lower()
        except (TypeError, ValueError):
            hay = str(item).lower()
        if keyword not in hay:
            return False
    return True


def _path_writable(path: Path) -> bool:
    """文件可写判断：文件存在就看它，不存在就看父目录（不会真的写东西）。"""
    try:
        if path.is_dir():
            return False
        if path.exists():
            return os.access(str(path), os.W_OK)
        return path.parent.exists() and os.access(str(path.parent), os.W_OK)
    except OSError:
        return False



class ConsoleAdapter:
    """管理控制台需要的能力：读快照、改安全策略、读审计、急停。

    隔这一层是为了让 bridge/server.py 完全不碰 AstrBot 的 self.config；
    HTTP 层只负责鉴权和 JSON，业务语义都在这里。
    """

    def __init__(self, plugin: "PhoneService") -> None:
        self.plugin = plugin
        self._audit_cache: tuple[float, int, list[dict[str, Any]]] | None = None

    # ---------------------------------------------------------------- 读

    def snapshot(self) -> dict[str, Any]:
        plugin = self.plugin
        return {
            "config": {
                "rate_limit_per_minute": int(plugin.policy.rate_limit),
                "task_timeout_ms": int(plugin.default_timeout_ms),
                "offline_after_seconds": int(round(plugin.hub.offline_after)),
                "screenshot_quality": int(plugin.quality),
                "max_screenshot_kb": int(round(plugin.max_shot_bytes / 1024)),
                "allow_screenshot_sensitive": bool(plugin.allow_sensitive_shot),
                "allow_sensitive_write": bool(plugin.allow_sensitive_write),
                "preview_enabled": bool(plugin.preview_enabled),
                "preview_min_interval_ms": int(plugin.preview_min_interval_ms),
                "sensitive_packages": list(plugin.policy.sensitive_packages),
                "package_blacklist": list(plugin.policy.package_blacklist),
                "app_mode": plugin.policy.app_mode,
                "app_blocked": sorted(plugin.policy.app_blocked),
                "app_allowed": sorted(plugin.policy.app_allowed),
            },
            "apps": list(getattr(plugin, "_apps", [])),
            "categories": list(plugin.config.get("app_categories") or []),
            "security": {
                "has_password": bool(str(plugin.config.get("console_password") or "").strip()),
                "has_readonly_password": bool(str(plugin.config.get("readonly_password") or "").strip()),
                "sensitive_packages": sorted(plugin.config.get("sensitive_packages") or []),
                "package_blacklist": sorted(plugin.config.get("package_blacklist") or []),
                "allow_sensitive_shot": bool(plugin.config.get("allow_screenshot_sensitive")),
                "allow_sensitive_write": bool(plugin.config.get("allow_sensitive_write")),
            },
            "category_map": dict(plugin.config.get("app_category") or {}),
            "audit": self.audit_page(limit=20)["items"],
            "editable": {
                "ints": {key: [lo, hi] for key, (lo, hi, _label) in _EDITABLE_INTS.items()},
                "bools": sorted(_EDITABLE_BOOLS),
            },
        }

    # ---- 控制台登录（A1/A3/A4） ----

    def has_password(self) -> bool:
        return bool(str(self.plugin.config.get("console_password") or "").strip())

    def has_readonly_password(self) -> bool:
        return bool(str(self.plugin.config.get("readonly_password") or "").strip())

    def check_password(self, password: str) -> str | None:
        """口令对了返回角色 'admin' / 'ro'，否则 None。"""
        supplied = str(password or "")
        if not supplied:
            return None
        admin_pw = str(self.plugin.config.get("console_password") or "")
        if admin_pw and hmac.compare_digest(supplied, admin_pw):
            return "admin"
        ro_pw = str(self.plugin.config.get("readonly_password") or "")
        if ro_pw and hmac.compare_digest(supplied, ro_pw):
            return "ro"
        return None

    def check_readonly(self, password: str) -> bool:
        """只读口令校验（给「别人只想看」的场景）。"""
        ro_pw = str(self.plugin.config.get("readonly_password") or "")
        if not ro_pw:
            return False
        try:
            return hmac.compare_digest(str(password or ""), ro_pw)
        except (TypeError, UnicodeError):
            return False

    def _session_key(self) -> bytes:
        return str(self.plugin.config.get("session_secret") or "").encode("utf-8")

    def issue_session(self, role: str, hours: int = 12) -> str:
        """签一个 session 串：base64(payload).hexsig，payload 带角色和过期时间。"""
        exp = int(time.time()) + max(1, hours) * 3600
        payload = f"{role}|{exp}"
        mac = hmac.new(self._session_key(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=") + "." + mac

    def verify_session(self, token: str) -> str | None:
        """校验 session，返回角色；过期或签名不对返回 None。"""
        raw = str(token or "")
        if "." not in raw:
            return None
        body, _, mac = raw.rpartition(".")
        try:
            payload = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode("utf-8")
        except Exception:  # noqa: BLE001
            return None
        expect = hmac.new(self._session_key(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(mac, expect):
            return None
        role, _, exp = payload.partition("|")
        try:
            if int(exp) < int(time.time()):
                return None
        except ValueError:
            return None
        if role not in ("admin", "ro"):
            return None
        return role

    async def fetch_icon(self, package: str) -> bytes | None:
        """控制台按需取一个应用图标（转发给 PhoneService）。"""
        return await self.plugin.fetch_icon(package)

    async def refresh_apps(self) -> list[dict[str, Any]]:
        """控制台点「刷新应用列表」时走这里：让手机上报一次。"""
        return await self.plugin.refresh_apps()

    # ---- 实时预览 / 点图（B1、B2） ----

    def preview_enabled(self) -> bool:
        return bool(getattr(self.plugin, "preview_enabled", False))

    def preview_interval_ms(self) -> int:
        return int(getattr(self.plugin, "preview_min_interval_ms", 1500) or 1500)

    async def preview_frame(self) -> bytes:
        """实时预览的一张图（JPEG 字节）；取不到时抛带 offline/sensitive 标记的异常。"""
        return await self.plugin.preview_frame()

    async def tap(self, x: Any, y: Any) -> dict[str, Any]:
        """控制台点图：坐标已经换算成手机真实像素，走 phone_tap 同一条安全链路。"""
        xi, yi, err = self.plugin._check_point(x, y)
        if err:
            raise ValueError(err)
        return await self.plugin.submit_action(ACT_TAP, {"x": xi, "y": yi}, 8000)

    # ---- 一键自检（C2） ----

    def selfcheck(self) -> list[dict[str, Any]]:
        plugin = self.plugin
        server = getattr(plugin, "server", None)
        hub = plugin.hub
        dev = hub.device
        out: list[dict[str, Any]] = []

        def add(name: str, ok: bool, detail: str) -> None:
            out.append({"name": name, "ok": bool(ok), "detail": detail})

        running = bool(getattr(server, "running", False))
        host = str(getattr(server, "host", "") or "0.0.0.0")
        port = int(getattr(server, "port", 0) or 0)
        add(
            "监听端口",
            running,
            f"{host}:{port} 正在监听" if running else "HTTP 服务没起来，看看 daemon 日志",
        )

        age = hub.last_heartbeat_age
        if hub.online:
            detail = f"心跳 {age} 秒前" if age is not None else "在线"
        elif age is None:
            detail = "还没收到过心跳：确认手机脚本在跑"
        else:
            detail = f"最后一次心跳 {age} 秒前，已经掉线"
        add("手机在线", hub.online, detail)

        add(
            "无障碍",
            bool(dev.get("accessibility")),
            "已开启" if dev.get("accessibility") else "未开启：在 AutoX.js 里打开无障碍服务",
        )
        add(
            "录屏权限",
            bool(dev.get("screen_capture")),
            "已授权" if dev.get("screen_capture") else "未授权：在 AutoX.js 里授予截图权限",
        )
        overlay = dev.get("overlay")
        if overlay is None:
            add("悬浮窗", False, "手机端没上报（脚本版本可能太旧）")
        else:
            overlay_note = str(dev.get("overlay_note") or "").strip()
            add("悬浮窗", bool(overlay), "守卫已开启" if overlay else (overlay_note or "守卫未开启"))

        try:
            icons = len(list(ICON_DIR.glob("*.png")))
        except OSError:
            icons = 0
        add("图标缓存", True, f"已缓存 {icons} 个应用图标")

        audit_file = Path(str(getattr(plugin, "audit_path", "") or "audit.jsonl"))
        audit_ok = _path_writable(audit_file)
        add("审计文件可写", audit_ok, str(audit_file) if audit_ok else f"{audit_file}（不可写）")

        cfg_path = getattr(plugin.config, "path", None)
        if cfg_path is None:
            add("dp.json 可写", False, "配置不是从文件加载的，没法判断")
        else:
            file_path = Path(cfg_path)
            file_ok = _path_writable(file_path)
            add("dp.json 可写", file_ok, str(file_path) if file_ok else f"{file_path}（不可写）")
        return out

    def audit_page(
        self,
        limit: Any = AUDIT_PAGE_DEFAULT,
        offset: Any = 0,
        kind: str = "all",
        q: str = "",
    ) -> dict[str, Any]:
        try:
            size = max(1, min(int(limit), AUDIT_PAGE_MAX))
        except (TypeError, ValueError):
            size = AUDIT_PAGE_DEFAULT
        try:
            skip = max(0, int(offset))
        except (TypeError, ValueError):
            skip = 0
        items = self._audit_filtered(kind, q)
        return {"items": items[skip:skip + size], "total": len(items), "offset": skip}

    def audit_export(self, kind: str = "all", q: str = "") -> list[dict[str, Any]]:
        """导出用：返回筛选后的全部审计记录（新 → 旧）。"""
        return list(self._audit_filtered(kind, q))

    def _audit_filtered(self, kind: str = "all", q: str = "") -> list[dict[str, Any]]:
        kind_s = str(kind or "all").strip().lower()
        keyword = str(q or "").strip().lower()
        items = self._audit_all()
        if kind_s in ("", "all") and not keyword:
            return items
        out: list[dict[str, Any]] = []
        for item in items:
            if not _audit_match(item, kind_s, keyword):
                continue
            out.append(item)
        return out

    def _audit_all(self) -> list[dict[str, Any]]:
        """读审计日志，新的在前；按 (mtime, size) 缓存，避免每次轮询都重读。"""
        path = getattr(self.plugin, "audit_path", None)
        if not path:
            return []
        try:
            stat = Path(path).stat()
        except OSError:
            return []
        cached = self._audit_cache
        if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
            return cached[2]
        try:
            with open(path, "rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - AUDIT_READ_TAIL_BYTES))
                raw = fh.read()
        except OSError:
            return []
        lines = raw.decode("utf-8", errors="replace").splitlines()
        if size > AUDIT_READ_TAIL_BYTES and lines:
            lines = lines[1:]  # 第一行可能是半条
        out: list[dict[str, Any]] = []
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                out.append(item)
        self._audit_cache = (stat.st_mtime, stat.st_size, out)
        return out

    # ---------------------------------------------------------------- 写

    def apply_config(self, payload: dict[str, Any], ip: str = "-") -> dict[str, Any]:
        """白名单 + 安全策略；未知字段忽略，越界直接 ValueError（HTTP 400）。"""
        plugin = self.plugin
        change: dict[str, Any] = {}
        # 前端保存时会提交整份策略，这里记下改之前的值，最后只把真正变动的写进审计
        before: dict[str, Any] = {
            **{key: plugin.config.get(key) for key in _EDITABLE_INTS},
            **{key: plugin.config.get(key) for key in _EDITABLE_BOOLS},
        }

        for key, (lo, hi, label) in _EDITABLE_INTS.items():
            if key not in payload:
                continue
            try:
                value = int(payload.get(key))
            except (TypeError, ValueError):
                raise ValueError(f"{label}必须是整数") from None
            if not lo <= value <= hi:
                raise ValueError(f"{label}要在 {lo}–{hi} 之间")
            plugin.config[key] = value
            change[key] = value

        for key, label in _EDITABLE_BOOLS.items():
            if key not in payload:
                continue
            value = payload.get(key)
            if not isinstance(value, bool):
                raise ValueError(f"{label}只能是 true/false")
            plugin.config[key] = value
            change[key] = value

        # 应用访问（控制台「应用访问」卡）：blocklist / allowlist + 两个包名列表
        if "app_mode" in payload:
            mode = str(payload.get("app_mode") or "").strip().lower()
            if mode not in ("blocklist", "allowlist"):
                raise ValueError("应用访问模式只能是 blocklist 或 allowlist")
            plugin.config["app_mode"] = mode
            change["app_mode"] = mode
        for key, label in (("app_blocked", "禁止访问列表"), ("app_allowed", "允许访问列表")):
            if key not in payload:
                continue
            raw = payload.get(key)
            if not isinstance(raw, list) or any(not isinstance(x, str) for x in raw):
                raise ValueError(f"{label}必须是包名字符串数组")
            cleaned = sorted({x.strip() for x in raw if x.strip()})
            plugin.config[key] = cleaned
            change[key] = cleaned

        # 敏感包名 / 黑名单（A3：以前只能改 dp.json）
        for key, label in (("sensitive_packages", "敏感应用名单"), ("package_blacklist", "黑名单")):
            if key not in payload:
                continue
            raw = payload.get(key)
            if raw is None:
                raw = []
            if isinstance(raw, str):
                raw = [line for line in raw.replace("，", ",").replace(chr(10), ",").split(",")]
            if not isinstance(raw, list):
                raise ValueError(f"{label}必须是包名数组或换行文本")
            cleaned_pkgs = []
            for item in raw:
                pkg_clean = str(item or "").strip()
                if not pkg_clean:
                    continue
                if not _SAFE_PKG.match(pkg_clean):
                    raise ValueError(f"{label}里有不像包名的值：{pkg_clean[:40]}")
                if pkg_clean not in cleaned_pkgs:
                    cleaned_pkgs.append(pkg_clean)
            plugin.config[key] = cleaned_pkgs
            change[key] = len(cleaned_pkgs)

        # 控制台口令（A1）：设置/修改/清空
        if "console_password" in payload:
            raw_pw = str(payload.get("console_password") or "")
            if raw_pw and len(raw_pw) < 6:
                raise ValueError("访问密码至少 6 位")
            if len(raw_pw) > 128:
                raise ValueError("访问密码太长了")
            plugin.config["console_password"] = raw_pw
            change["console_password"] = "set" if raw_pw else "cleared"

        if "readonly_password" in payload:
            raw_ro = str(payload.get("readonly_password") or "")
            if raw_ro and len(raw_ro) < 6:
                raise ValueError("只读口令至少 6 位")
            if raw_ro and raw_ro == str(payload.get("console_password") or plugin.config.get("console_password") or ""):
                raise ValueError("只读口令不能和访问密码一样")
            plugin.config["readonly_password"] = raw_ro
            change["readonly_password"] = "set" if raw_ro else "cleared"

        # 手动分类：列表（id+name）与「包名 → 分类 id」映射
        if "app_categories" in payload:
            raw = payload.get("app_categories")
            if not isinstance(raw, list):
                raise ValueError("分类列表必须是数组")
            cleaned_cats = []
            seen_ids = set()
            for item in raw:
                if not isinstance(item, dict):
                    raise ValueError("每个分类都要是 {id, name}")
                cid = str(item.get("id") or "").strip()
                cname = str(item.get("name") or "").strip()
                if not cid or not cname:
                    raise ValueError("分类的 id 和 name 都不能为空")
                if not _SAFE_CAT.match(cid):
                    raise ValueError("分类 id 只能用字母、数字、下划线、短横线")
                if cid in seen_ids:
                    raise ValueError("分类 id 不能重复")
                seen_ids.add(cid)
                cleaned_cats.append({"id": cid, "name": cname[:20]})
            if len(cleaned_cats) > 40:
                raise ValueError("分类最多 40 个")
            plugin.config["app_categories"] = cleaned_cats
            change["app_categories"] = cleaned_cats

        if "app_category" in payload:
            raw = payload.get("app_category")
            if not isinstance(raw, dict):
                raise ValueError("分类映射必须是对象")
            valid_ids = {c["id"] for c in (plugin.config.get("app_categories") or [])}
            cleaned_map = {}
            for pkg, cid in raw.items():
                pkg_s = str(pkg).strip()
                cid_s = str(cid).strip()
                if not pkg_s or not cid_s:
                    continue
                if not _SAFE_PKG.match(pkg_s):
                    continue
                if cid_s not in valid_ids:
                    continue
                cleaned_map[pkg_s] = cid_s
            plugin.config["app_category"] = cleaned_map
            change["app_category"] = len(cleaned_map)

        change = {key: value for key, value in change.items() if before.get(key) != value}
        if not change:
            return self.snapshot()["config"]

        saver = getattr(plugin.config, "save_config", None)
        if callable(saver):
            saver()

        # 让改动立刻生效（不重载插件、不断开手机）
        if change.keys() & {"rate_limit_per_minute", "app_mode", "app_blocked", "app_allowed",
                          "sensitive_packages", "package_blacklist"}:
            plugin._rebuild_policy()
            server = getattr(plugin, "server", None)
            if server is not None:
                server.policy = plugin.policy
        if "task_timeout_ms" in change:
            plugin.default_timeout_ms = int(change["task_timeout_ms"])
        if "offline_after_seconds" in change:
            plugin.hub.offline_after = float(change["offline_after_seconds"])
        if "screenshot_quality" in change:
            plugin.quality = int(change["screenshot_quality"])
        if "max_screenshot_kb" in change:
            plugin.max_shot_bytes = int(change["max_screenshot_kb"]) * 1024
        if "allow_screenshot_sensitive" in change:
            plugin.allow_sensitive_shot = bool(change["allow_screenshot_sensitive"])
        if "allow_sensitive_write" in change:
            plugin.allow_sensitive_write = bool(change["allow_sensitive_write"])
        if "preview_enabled" in change:
            plugin.preview_enabled = bool(change["preview_enabled"])
        if "preview_min_interval_ms" in change:
            plugin.preview_min_interval_ms = int(change["preview_min_interval_ms"])

        plugin.policy.audit("admin", ip=ip, change=change)
        return self.snapshot()["config"]

    def stop_all(self) -> int:
        return int(self.plugin.hub.emergency_clear())


# 「从敏感界面逃出去」的动作：不碰应用内容，只做导航。
# 否则一旦进了设置/微信，连 home 都被拦，手机就再也动不了了（真机测出来的坑）。
_ESCAPE_ACTIONS = ("open_app",)
_ESCAPE_KEYS = ("back", "home", "recents")


def _is_escape(action: str, params: dict[str, Any] | None) -> bool:
    if action in _ESCAPE_ACTIONS:
        return True
    if action == "key":
        name = str((params or {}).get("name") or "back").strip().lower()
        return name in _ESCAPE_KEYS
    return False


class PhoneService:
    """次元门的执行层：一台手机、一个队列、一套安全策略。

    daemon 进程里只实例化一次；MCP 工具层通过它的 submit_action 下发动作。
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config if config is not None else {}

        token = str(self._cfg("token", "") or "").strip()
        host = str(self._cfg("listen_host", "0.0.0.0"))
        port = max(1, min(_cfg_int(self._cfg("listen_port", 8766), 8766, "listen_port"), 65535))
        offline_after = max(
            5.0,
            min(
                _cfg_float(self._cfg("offline_after_seconds", 40), 40.0, "offline_after_seconds"),
                600.0,
            ),
        )

        self.audit_path = _audit_path()
        self.policy = self._make_policy()
        # 手机上报的应用列表（控制台「应用访问」用），只在内存里放，重启重读
        self._apps: list[dict[str, Any]] = []
        self._apps_at: float = 0.0
        self.hub = DeviceHub(offline_after=offline_after)
        self.console = ConsoleAdapter(self)
        self.server = PhoneBridgeServer(
            hub=self.hub,
            token=token,
            host=host,
            port=port,
            policy=self.policy,
            debug_token=self._cfg("debug_token", ""),
            admin_token=str(self.config.get("admin_token", "") or ""),
            console=self.console,
            local_token=str(self.config.get("local_token", "") or ""),
            submitter=self.submit_action,
            stopper=self.emergency_stop,
            tool_target=self,
        )
        self.default_timeout_ms = max(
            1000,
            min(_cfg_int(self._cfg("task_timeout_ms", 20000), 20000, "task_timeout_ms"), 120000),
        )
        self.quality = max(
            1, min(_cfg_int(self._cfg("screenshot_quality", 55), 55, "screenshot_quality"), 95)
        )
        self.max_shot_bytes = (
            max(100, min(_cfg_int(self._cfg("max_screenshot_kb", 1500), 1500, "max_screenshot_kb"), 20000))
            * 1024
        )
        self.allow_sensitive_shot = bool(self._cfg("allow_screenshot_sensitive", False))
        self.allow_sensitive_write = bool(self._cfg("allow_sensitive_write", False))
        # 控制台实时预览：默认关，关着时 /admin/preview 直接 403
        self.preview_enabled = bool(self._cfg("preview_enabled", False))
        self.preview_min_interval_ms = max(
            200,
            min(
                _cfg_int(
                    self._cfg("preview_min_interval_ms", 1500),
                    1500,
                    "preview_min_interval_ms",
                ),
                60000,
            ),
        )
        self.paths: list[str] = []

    # ------------------------------------------------------------ 生命周期

    async def start(self) -> bool:
        """启动 HTTP 服务。token 为空时拒绝启动（防裸奔）。返回是否真的起来了。"""
        if not self.server.token:
            logger.error(
                "[dimensional-portal] 还没填 token：出于安全考虑不启动 HTTP 服务。"
                "请在 dp.json 里填一个别人猜不到的字符串（手机端 dimensional_portal_agent.js 的 TOKEN 必须一致），"
                "然后重启 daemon。"
            )
            return False
        self._ensure_admin()
        try:
            await self.server.start()
        except Exception as exc:  # 端口占用、权限等都算
            logger.error(
                "[dimensional-portal] 无法监听 %s:%s（%s）。换个 listen_port 再启动。",
                self.server.host,
                self.server.port,
                exc,
            )
            # 启动失败必须把半开的 socket 收掉，否则端口会被这个死实例一直占着，
            # 下次重载只会记录“端口被占用”而永远起不来。
            try:
                await self.server.stop()
            except Exception:
                pass
            return False
        logger.info(
            "[dimensional-portal] 就绪：请让手机上的 AutoX.js 脚本连 http://<电脑IP>:%s",
            self.server.port,
        )
        if self.server.admin_enabled:
            logger.info("[dimensional-portal] 管理控制台：%s", self._console_url())
        return True

    async def stop(self) -> None:
        await self.server.stop()

    # ------------------------------------------------------------ 配置/工具函数

    def _cfg(self, key: str, default: Any) -> Any:
        try:
            value = self.config.get(key, default)
        except AttributeError:
            return default
        if value is None or value == "":
            return default
        return value

    def _make_policy(self) -> SafetyPolicy:
        return SafetyPolicy(
            rate_limit=_cfg_int(
                self._cfg("rate_limit_per_minute", 20), 20, "rate_limit_per_minute"
            ),
            rate_window_s=60.0,
            sensitive_packages=self._cfg("sensitive_packages", None) or None,
            package_blacklist=self._cfg("package_blacklist", None) or None,
            app_mode=str(self._cfg("app_mode", "blocklist") or "blocklist"),
            app_blocked=self._cfg("app_blocked", None) or None,
            app_allowed=self._cfg("app_allowed", None) or None,
            audit_path=self.audit_path,
        )

    def _rebuild_policy(self) -> None:
        """白名单改了：就地重建 SafetyPolicy，不重载插件（会话和队列都还在）。"""
        self.policy = self._make_policy()

    def _ensure_admin(self) -> None:
        """admin_token 为空就现场生成并写盘；填 off 就彻底不做控制台。"""
        raw = str(self.config.get("admin_token", "") or "").strip()
        if raw.lower() == "off":
            logger.info("[dimensional-portal] 管理控制台已关闭（admin_token=off）")
            return
        if raw:
            self.server.enable_admin(raw)
            return
        token = secrets.token_urlsafe(24)
        try:
            self.config["admin_token"] = token
            saver = getattr(self.config, "save_config", None)
            if callable(saver):
                saver()
            else:
                logger.warning(
                    "[dimensional-portal] 配置不支持 save_config，自动生成的 admin_token 只在本次运行有效"
                )
            self.server.enable_admin(token)
            logger.info("[dimensional-portal] 已自动生成 admin_token 并写入插件配置")
        except Exception as exc:  # 写盘失败也别影响手机桥接
            logger.error("[dimensional-portal] 自动生成 admin_token 失败：%s", exc)

    def _console_url(self) -> str:
        host = self.server.host
        if host in ("", "0.0.0.0", "::"):
            host = _local_ip()
        return f"http://{host}:{self.server.port}/console?k={self.server.admin_token}"

    def _gate(self, action: str, params: dict[str, Any] | None = None) -> str | None:
        """限流 + 在线 + 敏感界面写保护。返回非 None 就是拒绝理由。"""
        ok, msg = self.policy.rate_check(self._rate_key())
        if not ok:
            self.policy.audit("deny_rate", action=action, reason=msg)
            return f"⏳ 操作太频繁：{msg}"
        if not self.hub.online:
            return (
                "📴 手机不在线。请确认：① AutoX.js 里 dimensional_portal_agent.js 正在运行；"
                "② 无障碍服务已开启；③ 手机和电脑在同一网络（或 Tailscale 已连）。"
            )
        # 敏感界面默认不让动手：不截图保护的是画面，拦不住往密码框里打字/点「确认支付」。
        # 只拦 WRITE_ACTIONS；phone_see / phone_ui 这类只读动作照常放行。
        if action in WRITE_ACTIONS and not self.allow_sensitive_write and not _is_escape(action, params):
            cur = str(self.hub.device.get("cur_pkg") or "")
            sensitive, why = self.policy.is_sensitive_screen(cur)
            if sensitive:
                self.policy.audit(
                    "deny_sensitive_write",
                    action=action,
                    pkg=cur,
                    reason=why,
                )
                return (
                    f"🔒 {why}，已拦截写操作（{action}），没有点、没有输入。\n"
                    "敏感界面默认只能看（phone_see / phone_ui）。确实需要操作时，"
                    "在控制台「安全策略」里打开「敏感界面允许点击输入」再试。"
                )
        # 应用访问：人可以在控制台里把某个应用设成「禁止访问」，
        # 或切成「只允许白名单」。open_app 看目标包名，其它动作看当前前台包名。
        target = self._target_package(action, params)
        allowed, why = self.policy.app_access(target)
        if not allowed:
            self.policy.audit("deny_app", action=action, pkg=target or "", reason=why)
            return (
                f"🚫 {why}，已拦截（{action}）。\n"
                "要改的话去控制台 →「应用访问」里放开这个应用。"
            )
        return None

    def _target_package(self, action: str, params: dict[str, Any] | None) -> str | None:
        if action == ACT_OPEN_APP:
            pkg = str((params or {}).get("package") or "").strip()
            if pkg:
                return pkg
            # 只给了应用名（「计算器」）时，用手机报上来的应用列表反查包名，
            # 否则「禁止访问」对按名字打开的操作形同虚设。
            name = str((params or {}).get("name") or "").strip()
            if name and self._apps:
                for item in self._apps:
                    item_pkg = str(item.get("package") or "").strip()
                    item_name = str(item.get("name") or "").strip()
                    if name in (item_pkg, item_name):
                        return item_pkg or None
            return None
        return str(self.hub.device.get("cur_pkg") or "").strip() or None

    @staticmethod
    def _rate_key() -> str:
        """限流的桶：DSH 版只有一个调用方（本机 MCP），一个桶就够。"""
        return "local"

    async def _submit(
        self,
        action: str,
        params: dict[str, Any] | None = None,
        timeout_ms: int | None = None,
        audit_task: bool = True,
    ) -> dict[str, Any]:
        """下发任务；异常统一在这里转成中文提示，返回值是手机回传的 data。"""
        if audit_task:
            self.policy.audit("task", action=action, params=params or {})
        try:
            return await self.hub.submit(
                action,
                params or {},
                timeout_ms=timeout_ms or self.default_timeout_ms,
            )
        except DeviceOffline as exc:
            raise _ToolError(f"📴 手机不在线：{exc}") from exc
        except TaskTimeout as exc:
            raise _ToolError(f"⌛ {exc}（手机可能卡住或锁屏）") from exc
        except TaskRejected as exc:
            raise _ToolError(f"❌ 手机端执行失败：{exc}") from exc

    async def submit_action(
        self,
        action: str,
        params: dict[str, Any] | None = None,
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        """daemon 的本机接口用它：过一遍 _gate，再下发。异常转成人话文本。"""
        reason = self._gate(action, params)
        if reason:
            return {"ok": False, "text": reason}
        try:
            data = await self._submit(action, params or {}, timeout_ms)
        except _ToolError as exc:
            return {"ok": False, "text": str(exc)}
        return {"ok": True, "data": data}

    async def preview_frame(self) -> bytes:
        """控制台实时预览：截一张 720 宽的图，只回字节，不落盘、不记图片内容。

        预览不占「每分钟操作上限」的配额（否则一边看一边操作很快就被限流），
        但敏感界面照样遵守 allow_screenshot_sensitive。
        """
        if not self.preview_enabled:
            raise _PreviewError("实时预览没打开")
        if not self.hub.online:
            raise _PreviewError("手机不在线", offline=True)
        cur = str(self.hub.device.get("cur_pkg") or "")
        sensitive, why = self.policy.is_sensitive_screen(cur)
        if sensitive and not self.allow_sensitive_shot:
            self.policy.audit("preview", ok=False, error="sensitive", pkg=cur)
            raise _PreviewError(f"{why}，按安全策略不预览", sensitive=True)
        try:
            data = await self._submit(
                ACT_SCREEN,
                {"quality": self.quality, "max_edge": PREVIEW_MAX_EDGE},
                PREVIEW_TIMEOUT_MS,
                audit_task=False,
            )
        except _ToolError as exc:
            message = str(exc)
            self.policy.audit("preview", ok=False, error=message[:120])
            raise _PreviewError(message, offline="不在线" in message) from exc
        raw_b64 = str(data.get("image_b64") or "")
        if not raw_b64:
            self.policy.audit("preview", ok=False, error="手机没回图片数据")
            raise _PreviewError("手机没回图片数据")
        try:
            raw = base64.b64decode(raw_b64, validate=False)
        except (ValueError, TypeError) as exc:
            self.policy.audit("preview", ok=False, error="图片数据坏了")
            raise _PreviewError("手机回的图片数据坏了") from exc
        if not raw:
            self.policy.audit("preview", ok=False, error="图片是空的")
            raise _PreviewError("手机回的图片是空的")
        if len(raw) > self.max_shot_bytes:
            self.policy.audit("preview", ok=False, bytes=len(raw), error="too_big")
            raise _PreviewError(
                f"预览图太大（{self._fmt_size(len(raw))}），把「截图质量」调低一点",
            )
        self.policy.audit("preview", ok=True, bytes=len(raw))
        return raw

    def _save_shot(self, raw: bytes) -> Path:
        SHOT_DIR.mkdir(parents=True, exist_ok=True)
        path = SHOT_DIR / f"shot_{int(time.time() * 1000)}.jpg"
        path.write_bytes(raw)
        cutoff = time.time() - SHOT_KEEP_S
        for old in SHOT_DIR.glob("shot_*.jpg"):
            try:
                if old.stat().st_mtime < cutoff:
                    old.unlink()
            except OSError:
                pass
        return path

    @staticmethod
    def _fmt_size(n: int) -> str:
        if n < 1024:
            return f"{n}B"  # 1x1 缩略图别显示成「0KB」
        if n < 1024 * 1024:
            return f"{n / 1024:.0f}KB"
        return f"{n / 1048576:.1f}MB"

    @staticmethod
    def _render_ui(data: dict[str, Any], limit: int) -> str:
        nodes = data.get("nodes") or []
        pkg = data.get("pkg") or "?"
        w, h = data.get("w"), data.get("h")
        lines: list[str] = []
        shown = 0
        for node in nodes:
            text = (node.get("text") or "").strip()
            desc = (node.get("desc") or "").strip()
            label = text or desc
            cls = str(node.get("cls") or "").split(".")[-1]
            clickable = node.get("clickable")
            editable = node.get("editable")
            # 没字、不点、不可输入的纯容器节点对模型没用，跳过（但保留有 id 的）
            if not label and not clickable and not editable and not node.get("id"):
                continue
            if shown >= limit:
                lines.append(f"…（还有 {len(nodes) - shown} 个节点未显示）")
                break
            cx, cy = node.get("cx"), node.get("cy")
            parts = [f"{shown:>3}", cls or "?"]
            if label:
                parts.append(f'"{label[:60]}"')
            if node.get("id"):
                parts.append(f"#{str(node['id']).split('/')[-1]}")
            if cx is not None and cy is not None:
                parts.append(f"@({cx},{cy})")
            if clickable:
                parts.append("[可点]")
            if editable:
                parts.append("[输入框]")
            if node.get("selected"):
                parts.append("[选中]")
            lines.append(" ".join(parts))
            shown += 1
        # 表头用实际渲染条数：被跳过的空容器也算进「共 N 个」只会让数字对不上。
        header = f"界面 {pkg}  {w}x{h}（列出 {shown} 个控件，手机共报 {len(nodes)} 个节点）"
        return "\n".join([header, *lines])

    @staticmethod
    def _brief(data: dict[str, Any]) -> str:
        pkg = data.get("pkg") or data.get("cur_pkg") or ""
        extra = f"，当前界面 {pkg}" if pkg else ""
        return extra

    @staticmethod
    def _render_seen(seen: Any) -> str:
        """把 open_app 没打开目标那一刻的界面快照渲染成一句人话。"""
        if not isinstance(seen, dict):
            return ""
        pkg = str(seen.get("pkg") or "?")
        win = seen.get("win")
        if isinstance(win, dict):
            win = win.get("pkg") or win.get("type") or ""
        win = str(win or "").strip()
        texts = [str(t).strip() for t in (seen.get("texts") or []) if str(t).strip()]
        parts = [f"当时前台：{pkg}"]
        if win:
            parts.append(f"窗口：{win}")
        if seen.get("popup_maybe"):
            parts.append("可能有弹窗挡住")
        if texts:
            parts.append("可见文字：" + " | ".join(texts[:10]))
        return "；".join(parts)

    def _check_point(self, x: Any, y: Any) -> tuple[int, int, str | None]:
        """校验点击坐标在屏幕内（用手机报的 w/h）；手机没报分辨率时不拦。"""
        try:
            xi, yi = int(x), int(y)
        except (TypeError, ValueError):
            return 0, 0, "坐标必须是整数像素"
        dev = self.hub.device
        try:
            wi, hi = int(dev.get("w") or 0), int(dev.get("h") or 0)
        except (TypeError, ValueError):
            return xi, yi, None
        if wi > 0 and hi > 0 and not (0 <= xi < wi and 0 <= yi < hi):
            return xi, yi, (
                f"坐标 ({xi},{yi}) 超出屏幕范围（{wi}x{hi}），左上角是 (0,0)，"
                "请从 phone_see / phone_ui 里取坐标"
            )
        return xi, yi, None

    # ============================================================ 工具：状态

    async def phone_status(self) -> str:
        """查看手机连接状态：是否在线、电量、屏幕分辨率、当前应用、无障碍/截图权限。

        不需要在线也能调用，掉线时用它排查问题。手机离线时会告诉你该检查什么。
        """
        snap = self.hub.snapshot()
        dev = snap["device"]
        if not snap["online"]:
            age = snap["heartbeat_age"]
            hint = f"最后一次心跳 {age} 秒前。" if age is not None else "还没收到过任何心跳。"
            # 0.0.0.0 是监听地址，不是手机能连的地址；提示用户换真实 IP。
            host = self.server.host
            addr = (
                "电脑的真实局域网 IP 或 Tailscale IP"
                if host in ("0.0.0.0", "::", "")
                else host
            )
            return (
                f"📴 手机离线。{hint}\n"
                "排查：① AutoX.js 脚本是否在跑（通知栏应有「脚本运行中」）；"
                "② 无障碍服务是否开启；③ 手机能不能访问电脑的 "
                f"{addr} 的 {self.server.port} 端口（浏览器打开 /healthz 试试）。"
            )
        battery = dev.get("battery", -1)
        bat = f"{battery}%{'（充电中）' if dev.get('charging') else ''}" if battery >= 0 else "未知"
        # 悬浮窗守卫是「后台能打开微信/QQ」的前提（Android 10+ 的后台启动豁免），
        # 旧脚本不上报这个字段，别把「不知道」说成「关了」。
        overlay = dev.get("overlay")
        if overlay is True:
            overlay_line = "悬浮窗守卫：开（后台启动应用可用）"
        elif overlay is False:
            note = str(dev.get("overlay_note") or "").strip()
            overlay_line = "悬浮窗守卫：关" + (f"（{note}）" if note else "")
            overlay_line += " ⚠️ 微信/QQ 这类应用会打不开，请给 AutoX.js 悬浮窗权限并重启脚本"
        else:
            overlay_line = "悬浮窗守卫：未上报（手机端脚本版本较旧）"
        return (
            f"📱 手机在线（{snap['heartbeat_age']}s 前心跳）\n"
            f"设备：{dev.get('device') or '?'} / Android {dev.get('android') or '?'}"
            f" / {dev.get('w')}x{dev.get('h')}\n"
            f"电量：{bat}　屏幕：{'亮' if dev.get('screen_on') else '灭'}\n"
            f"前台应用：{dev.get('cur_pkg') or '?'}\n"
            f"{overlay_line}\n"
            f"无障碍：{'开' if dev.get('accessibility') else '关'}"
            f"　截图权限：{'开' if dev.get('screen_capture') else '关'}\n"
            f"脚本版本：{dev.get('agent_version') or '?'}"
            f"　队列：{snap['queue']}　"
            f"累计成功 {snap['stats']['tasks_succeeded']} / 失败 {snap['stats']['tasks_failed']}"
        )

    # ============================================================ 工具：看

    async def phone_see(self, with_ui: bool = True) -> str:
        """看一眼手机现在显示什么：截一张图发到当前聊天，同时返回界面上的文字。

        这是最常用的第一个动作。看到图后，用 phone_tap 点坐标、phone_type 输入。
        敏感界面（微信、支付宝、银行、设置等）默认不截图，只返回控件文字。

        Args:
            with_ui (bool): 是否同时返回界面文字摘要，默认 True
        """
        gate = self._gate(ACT_SCREEN)
        if gate:
            return gate

        cur = str(self.hub.device.get("cur_pkg") or "")
        sensitive, why = self.policy.is_sensitive_screen(cur)
        if sensitive and not self.allow_sensitive_shot:
            return (
                f"🙈 {why}，按安全策略不把截图推到 QQ。\n"
                "改用 phone_ui 读界面文字，或者先 phone_open_app 切到其他应用。"
            )

        try:
            data = await self._submit(ACT_SCREEN, {"quality": self.quality}, 20000)
        except _ToolError as exc:
            return str(exc)

        b64 = data.get("image_b64") or ""
        if not b64:
            return "截图失败：手机没返回图像数据（可能没给「屏幕截图」权限）"

        try:
            raw = base64.b64decode(b64)
        except (ValueError, TypeError):
            return "截图失败：图像数据损坏"
        if len(raw) > self.max_shot_bytes:
            return (
                f"截图太大（{self._fmt_size(len(raw))}），已超过配置上限。"
                "把 screenshot_quality 调低（比如 40）再试。"
            )

        # 心跳里的 cur_pkg 最长 10 秒旧；手机回传的 pkg 才是按下快门那一刻的真实前台。
        # 拿它再判一次（with_ui 时连界面文字一起），否则「心跳后切到微信 → 立刻截图」会漏过。
        pkg = str(data.get("pkg") or cur or "?")
        ui_data: dict[str, Any] | None = None
        ui_text = ""
        if with_ui:
            try:
                ui_data = await self._submit(ACT_UI, {"max_nodes": 400}, 12000)
                ui_text = " ".join(
                    (n.get("text") or n.get("desc") or "").strip()
                    for n in (ui_data.get("nodes") or [])
                )[:3000]
            except _ToolError:
                ui_data = None

        sensitive, why = self.policy.is_sensitive_screen(pkg, ui_text)
        if sensitive and not self.allow_sensitive_shot:
            # 到这一步还没落盘、更没 send：直接拒绝，不把敏感界面推给聊天。
            self.policy.audit(
                "deny_screenshot",
                pkg=pkg,
                reason=why,
            )
            return (
                f"🙈 {why}，截图已拦截，未保存（按安全策略不把敏感界面落盘）。"
                "改用 phone_ui 读界面文字。"
            )

        w, h = data.get("w"), data.get("h")
        try:
            path = self._save_shot(raw)
        except Exception as exc:  # 落盘失败不影响已拿到的画面
            logger.error("[dimensional-portal] 截图落盘失败：%s", exc)
            return f"截图已拿到（{w}x{h}，{pkg}），但保存到磁盘失败：{exc}"

        summary = (
            f"📸 截图已保存（{w}x{h}，{self._fmt_size(len(raw))}，前台 {pkg}）\n"
            f"SHOT_PATH: {path}\n"
            "（想看画面就把这个路径当成图片读出来）"
        )
        if ui_data is not None:
            texts = [
                (n.get("text") or n.get("desc") or "").strip()
                for n in (ui_data.get("nodes") or [])
            ]
            texts = [t for t in texts if t]
            seen: list[str] = []
            for t in texts:
                if t not in seen:
                    seen.append(t)
                if len(seen) >= 12:
                    break
            if seen:
                summary += "\n界面上能看到的文字：" + " | ".join(seen)
        return summary

    async def phone_ui(self, max_nodes: int = 80) -> str:
        """读手机当前界面的控件树（文字、坐标、是否可点），不截图。

        适合看不清截图、或者敏感界面（微信/银行）不方便截图时用。
        每行给出中心坐标 @(x,y)，可以直接拿去 phone_tap。

        Args:
            max_nodes (int): 最多显示多少个节点，默认 80
        """
        gate = self._gate(ACT_UI)
        if gate:
            return gate
        limit = max(10, min(int(max_nodes or 80), 200))
        try:
            data = await self._submit(ACT_UI, {"max_nodes": limit * 3}, 15000)
        except _ToolError as exc:
            return str(exc)
        if data.get("error"):
            return f"读取界面失败：{data['error']}"
        return self._render_ui(data, limit)

    # ============================================================ 工具：动手

    async def phone_tap(self, x: int, y: int) -> str:
        """点击屏幕上一个点。坐标是真实像素，原点在左上角。

        坐标从 phone_see 的截图或 phone_ui 的 @(x,y) 里取。

        Args:
            x (int): 横坐标像素
            y (int): 纵坐标像素
        """
        gate = self._gate(ACT_TAP)
        if gate:
            return gate
        xi, yi, err = self._check_point(x, y)
        if err:
            return err
        try:
            data = await self._submit(ACT_TAP, {"x": xi, "y": yi}, 8000)
        except _ToolError as exc:
            return str(exc)
        return f"👆 已点击 ({xi}, {yi}){self._brief(data)}"

    async def phone_long_press(self, x: int, y: int, duration_ms: int = 800
    ) -> str:
        """长按一个点（默认 800ms），常用于弹出菜单、拖动前抓取。

        Args:
            x (int): 横坐标像素
            y (int): 纵坐标像素
            duration_ms (int): 按住时长，默认 800
        """
        gate = self._gate(ACT_LONG_PRESS)
        if gate:
            return gate
        xi, yi, err = self._check_point(x, y)
        if err:
            return err
        duration = max(300, min(_cfg_int(duration_ms or 800, 800, "duration_ms"), 5000))
        try:
            data = await self._submit(
                ACT_LONG_PRESS, {"x": xi, "y": yi, "duration": duration}, 10000
            )
        except _ToolError as exc:
            return str(exc)
        return f"👆 已长按 ({xi}, {yi}) {duration}ms{self._brief(data)}"

    async def phone_swipe(self, x1: int,
        y1: int,
        x2: int,
        y2: int,
        duration_ms: int = 300,
    ) -> str:
        """滑动/滚动屏幕：从 (x1,y1) 划到 (x2,y2)。向上划 = 看下面内容。

        Args:
            x1 (int): 起点横坐标
            y1 (int): 起点纵坐标
            x2 (int): 终点横坐标
            y2 (int): 终点纵坐标
            duration_ms (int): 滑动时长，默认 300，越大越慢越不容易误触
        """
        gate = self._gate(ACT_SWIPE)
        if gate:
            return gate
        duration = max(100, min(int(duration_ms or 300), 3000))
        params = {
            "x1": int(x1), "y1": int(y1), "x2": int(x2), "y2": int(y2),
            "duration": duration,
        }
        try:
            data = await self._submit(ACT_SWIPE, params, 12000)
        except _ToolError as exc:
            return str(exc)
        return f"👉 已滑动 ({x1},{y1})→({x2},{y2}) {duration}ms{self._brief(data)}"

    async def phone_type(self, text: str, submit: bool = False
    ) -> str:
        """往当前焦点输入框里打字。先 phone_tap 点一下输入框，再调这个。

        如果 setText 注入失败，手机端会自动退回剪贴板粘贴。

        Args:
            text (str): 要输入的文字
            submit (bool): 输完是否立刻回车提交（搜索框/聊天框常用），默认 False
        """
        gate = self._gate(ACT_TEXT)
        if gate:
            return gate
        raw = str(text or "")
        if not raw.strip():  # 纯空格不算输入内容
            return "输入内容为空"
        try:
            data = await self._submit(
                ACT_TEXT, {"text": raw, "submit": bool(submit)}, 15000
            )
        except _ToolError as exc:
            return str(exc)
        how = data.get("method") or "setText"
        return f"⌨️ 已输入「{raw[:40]}」（{how}）{'，并已提交' if submit else ''}{self._brief(data)}"

    async def phone_key(self, key: str = "back") -> str:
        """按系统键：back（返回）/ home（回桌面）/ recents（最近任务）/ enter / delete / volume_up / volume_down。

        卡在某个页面时用 back 退出；想回桌面用 home。

        Args:
            key (str): 按键名，默认 back
        """
        gate = self._gate(ACT_KEY, {"name": key})
        if gate:
            return gate
        name = str(key or "back").strip().lower()
        if name not in VALID_KEYS:
            return f"不认识的按键「{name}」，可用：{'、'.join(sorted(VALID_KEYS))}"
        try:
            await self._submit(ACT_KEY, {"name": name}, 8000)
        except _ToolError as exc:
            return str(exc)
        return f"🔘 已按 {name}"

    async def phone_open_app(self, name: str) -> str:
        """打开手机上的一个应用。可以给应用名（中文名如「微信」「相机」）或包名（com.tencent.mm）。

        会等 1.5 秒再返回前台包名，用来确认真的打开了。

        Args:
            name (str): 应用名或包名
        """
        gate = self._gate(ACT_OPEN_APP, {"name": name})
        if gate:
            return gate
        target = str(name or "").strip()
        if not target:
            return "要打开哪个应用？给应用名或包名"
        if "." in target:
            ok, why = self.policy.package_allowed(target)
            if not ok:
                return f"⛔ {why}"
        params = {"package": target} if "." in target else {"name": target}
        try:
            data = await self._submit(ACT_OPEN_APP, params, 20000)
        except _ToolError as exc:
            return str(exc)

        # 中文应用名在服务端解析不成包名，只能等手机回报真实前台再查一次黑名单，
        # 否则「打开微信」会绕过只认包名的 package_blacklist。
        pkg = str(data.get("pkg") or "").strip()
        ok, why = self.policy.package_allowed(pkg)
        if not ok:
            self.policy.audit(
                "deny_blacklist",
                action=ACT_OPEN_APP,
                target=target,
                pkg=pkg,
                reason=why,
            )
            return (
                f"⛔ 已拦截：「{target}」打开后前台是 {pkg}，{why}。"
                "按安全策略不能停留在该应用，建议立刻用 phone_key home 返回桌面。"
            )

        launched = data.get("launched")
        warning = str(data.get("warning") or "").strip()
        seen_note = self._render_seen(data.get("seen"))
        if launched is False:
            text = f"❌ 没能打开「{target}」"
            text += f"：{warning}" if warning else "：系统没接受启动请求，可能没装或应用名不对"
        else:
            text = f"🚀 已打开「{target}」，当前前台：{pkg or '?'}"
            if data.get("bounced"):
                text += "\n（走了兜底路径：先把 AutoX 顶到前台再唤起，屏幕闪一下 AutoX 是正常的）"
            if warning:
                # 启动请求发出去了但前台没变，warning 里是手机端的具体原因，原样带给模型。
                text += f"\n⚠️ 手机端提示：{warning}"
        if seen_note and (launched is False or warning):
            text += f"\n{seen_note}"
        return text

    async def phone_find_tap(self, text: str, index: int = 0, exact: bool = False
    ) -> str:
        """在屏幕上找一段文字并点它（比算坐标稳）。找不到会告诉你当前界面有哪些文字。

        Args:
            text (str): 要找的文字，比如「设置」「发送」「允许」
            index (int): 有多个匹配时点第几个，从 0 开始
            exact (bool): True = 文字完全相等；False = 包含即可
        """
        gate = self._gate(ACT_FIND_TEXT)
        if gate:
            return gate
        params = {"text": str(text), "index": int(index or 0), "exact": bool(exact), "tap": True}
        try:
            data = await self._submit(ACT_FIND_TEXT, params, 12000)
        except _ToolError as exc:
            return str(exc)
        if not data.get("found"):
            nearby = data.get("texts") or []
            hint = "、".join(str(t) for t in nearby[:15])
            return f"🔍 界面上没找到「{text}」" + (f"。当前文字：{hint}" if hint else "")
        cx, cy = data.get("cx"), data.get("cy")
        return f"🔍 找到「{data.get('matched') or text}」并点击 @({cx},{cy}){self._brief(data)}"

    # ============================================================ 工具：急停

    def emergency_stop(self) -> int:
        """清空还没下发的任务（急停）。已被手机上拿走的那一步无法撤回。"""
        dropped = self.hub.emergency_clear()
        self.policy.audit("emergency_stop", dropped=dropped)
        logger.warning("[dimensional-portal] 急停已执行，清空 %s 个任务", dropped)
        return dropped

    async def fetch_icon(self, package: str, size: int = 64) -> bytes | None:
        """取一个应用图标（PNG 字节）。磁盘缓存，取过就不再问手机。"""
        package = (package or "").strip()
        if not package or not _SAFE_PKG.match(package):
            return None
        ICON_DIR.mkdir(parents=True, exist_ok=True)
        cached = ICON_DIR / f"{package}.png"
        if cached.is_file() and cached.stat().st_size > 0:
            return cached.read_bytes()
        try:
            data = await self._submit(ACT_ICON, {"package": package, "size": size}, 15000)
        except (_ToolError, Exception):  # noqa: BLE001 - 图标失败不该影响列表
            return None
        raw = (data or {}).get("png_b64") if isinstance(data, dict) else None
        if not raw:
            return None
        try:
            blob = base64.b64decode(raw)
        except Exception:  # noqa: BLE001
            return None
        if len(blob) < 64 or len(blob) > 512 * 1024:
            return None
        try:
            cached.write_bytes(blob)
        except OSError:
            pass
        return blob

    async def refresh_apps(self, timeout_ms: int = 25000) -> list[dict[str, Any]]:
        """让手机把装了什么报上来，缓存在内存里给控制台用。"""
        data = await self._submit(ACT_APPS, {}, timeout_ms)
        apps = data.get("apps") if isinstance(data, dict) else None
        self._apps = list(apps or [])
        self._apps_at = time.time()
        return self._apps

    async def phone_apps(self, refresh: bool = False) -> str:
        """列出手机上的应用（名字 + 包名），并标出哪些被禁止访问。

        用来查包名（phone_open_app 需要），或确认「应用访问」里挡掉了什么。
        Args:
            refresh (bool): 重新从手机读一次（默认用上次读到的缓存）
        """
        refused = self._gate(ACT_APPS)
        if refused:
            return refused
        if refresh or not self._apps:
            try:
                await self.refresh_apps()
            except _ToolError as exc:
                return str(exc)
        if not self._apps:
            return "没读到应用列表（手机上可能不允许读取应用信息）"
        lines = []
        for item in self._apps:
            pkg = str(item.get("package") or "")
            name = str(item.get("name") or pkg)
            allowed, _why = self.policy.app_access(pkg)
            flag = "" if allowed else "  ⛔"
            lines.append(f"{name}{flag}\n    {pkg}")
        head = f"📱 手机上共 {len(self._apps)} 个应用"
        if self.policy.app_mode == "allowlist":
            head += "（当前是「只允许白名单」模式）"
        return head + "\n" + "\n".join(lines)

    async def phone_stop(self) -> str:
        """急停：清空所有还没发出去的任务。用户说「停下」「别动了」时立刻调用。"""
        dropped = self.emergency_stop()
        return f"🛑 已急停，清空 {dropped} 个待执行任务。手机端正在跑的那一步会做完。"


class _ToolError(Exception):
    """工具内部错误，message 直接展示给模型。"""


__all__ = ["PhoneService", "ConsoleAdapter"]
