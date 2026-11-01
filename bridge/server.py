"""HTTP 服务：手机端主动来要任务的那一端。

端点（给手机）：
    POST /hb        心跳（10s 一次）
    GET  /poll      长轮询（最多挂 25s），有任务立刻返回
    POST /result    回传执行结果
    GET  /healthz   存活探测（无需 token，只回服务名和在线状态）

端点（给本机的 MCP 客户端，仅回环 + local_token）：
    POST /local/submit   下发一个动作并等结果（MCP 工具就是它的薄包装）
    GET  /local/state    设备状态快照

端点（给浏览器，可选）：
    GET  /console        管理控制台
    GET  /admin/*        控制台用的 JSON 接口

给手机的鉴权：请求头 X-Token，或 query 参数 ?token=（手机端有些库设头不方便时用）。
给本机的鉴权：X-Local-Token，且只接受 127.0.0.1 / ::1 的请求。
"""

from __future__ import annotations

import asyncio
import csv
import hmac
import io
import logging
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from aiohttp import web

from .hub import DeviceHub
from .protocol import dumps, loads, now_ts, truncate

log = logging.getLogger("dimensional_portal")

MAX_POLL_WAIT_S = 30
AUDIT_PAGE_DEFAULT = 50
AUDIT_PAGE_MAX = 200
DEFAULT_MAX_BODY = 16 * 1024 * 1024  # 截图 base64 走这条通道，留足余量

# 控制台「最近日志」：读 daemon 日志尾部，最多回 500 行
DAEMON_LOG_PATH = Path(__file__).resolve().parents[1] / "tmp" / "daemon.log"
LOG_READ_TAIL_BYTES = 256 * 1024
LOG_MAX_LINES = 500

# 日志里不该给只读会话看的密钥（dp_daemon 启动时会把整份配置打进去）
_LOG_SECRET_RE = re.compile(
    r'("(?:token|local_token|admin_token|session_secret|console_password|readonly_password)"'
    r'\s*:\s*")([^"]*)(")'
)

# 管理控制台：同一 IP 每分钟最多失败 10 次，超了锁 60 秒（内存计数，重启即清）
ADMIN_MAX_FAILS = 10
ADMIN_FAIL_WINDOW_S = 600.0  # 10 分钟内失败 10 次就锁 10 分钟
PREVIEW_MAX_EDGE = 720  # 预览图长边（缩过之后才发给浏览器）
SESSION_COOKIE = "dp_session"
SESSION_HOURS = 12
CONSOLE_HTML_PATH = Path(__file__).with_name("console.html")
CONSOLE_ASSET_DIR = Path(__file__).with_name("assets")
# 控制台只允许读这几张背景图（白名单即全部可公开的文件）
CONSOLE_ASSETS = ("bg-wide.jpg", "bg-tall.jpg")


LOGIN_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>次元门 · 登录</title>
<style>
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 24px;
    font: 15px/1.6 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; color: #33224a;
    background:
      radial-gradient(1200px 600px at 20% -10%, #ffe9f3 0%, transparent 60%),
      radial-gradient(900px 500px at 100% 0%, #e8f1ff 0%, transparent 55%),
      linear-gradient(160deg, #fdf1f7 0%, #eef4ff 60%, #f7f2ff 100%);
  }
  .box {
    width: min(380px, 100%); padding: 28px 26px 24px; border-radius: 20px;
    background: rgba(255,255,255,.78); backdrop-filter: blur(14px) saturate(120%);
    box-shadow: 0 18px 50px rgba(122,92,168,.18), inset 0 1px 0 rgba(255,255,255,.9);
  }
  .eyebrow { letter-spacing: .34em; font-size: 11px; color: #a78bbf; text-align: center; }
  h1 { margin: 6px 0 2px; font-size: 26px; text-align: center; font-weight: 600;
       font-family: "Songti SC", "Noto Serif SC", serif; }
  p.sub { margin: 0 0 18px; text-align: center; font-size: 12.5px; color: #7d6b93; }
  label { display: block; font-size: 12.5px; color: #6b5b83; margin: 0 0 6px; }
  input[type=password] {
    width: 100%; padding: 11px 13px; border-radius: 12px; font: inherit;
    border: 1px solid rgba(122,92,168,.26); background: #fff; color: inherit;
  }
  input[type=password]:focus { outline: 2px solid rgba(200,140,200,.45); outline-offset: 1px; }
  button {
    margin-top: 16px; width: 100%; padding: 11px 14px; border: 0; border-radius: 999px;
    font: inherit; font-weight: 600; color: #fff; cursor: pointer;
    background: linear-gradient(135deg, #f6a9c8, #b79ce8);
    box-shadow: 0 8px 22px rgba(183,156,232,.4);
  }
  .err { color: #d2436b; font-size: 13px; margin: 10px 0 0; text-align: center; }
  .tip { margin-top: 14px; font-size: 11.5px; color: #8b7aa6; text-align: center; }
</style>
</head>
<body>
  <form class="box" method="post" action="/login">
    <div class="eyebrow">DIMENSIONAL PORTAL</div>
    <h1>次元门</h1>
    <p class="sub">输入访问密码，进控制台</p>
    <label for="pw">访问密码</label>
    <input id="pw" name="password" type="password" autocomplete="current-password" autofocus required>
    <button type="submit">进入</button>
    {{ERROR}}
    <p class="tip">忘了密码：改 dp.json 里的 console_password 为空，再用带 k= 的链接进来重设。</p>
  </form>
</body>
</html>
"""



def _shrink_jpeg(blob: bytes, max_edge: int = 720, quality: int = 72) -> bytes:
    """把手机截图缩到长边 max_edge，省带宽（手机上 q55 的 1080x2400 约 190KB → 720 约 55KB）。"""
    try:
        import io

        from PIL import Image
    except Exception:  # noqa: BLE001 - 没有 Pillow 就原样返回
        return blob
    try:
        with Image.open(io.BytesIO(blob)) as im:
            im = im.convert("RGB")
            w, h = im.size
            longest = max(w, h)
            if longest <= max_edge:
                return blob
            scale = max_edge / float(longest)
            im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
            out = io.BytesIO()
            im.save(out, "JPEG", quality=quality, optimize=True, progressive=True)
            return out.getvalue()
    except Exception:  # noqa: BLE001 - 缩图失败不影响预览
        return blob


def _token_match(supplied: str, expected: str) -> bool:
    """常量时间比较 token；任一侧含非 ASCII 字符时按不匹配处理。

    hmac.compare_digest 对 str 只接受 ASCII：手机端如果把中文 token 一类的值
    塞进 ?token=，直接比较会抛 TypeError 变成 HTTP 500。这里兜成 401。
    """
    try:
        return hmac.compare_digest(supplied, expected)
    except (TypeError, UnicodeError):
        return False


class PhoneBridgeServer:
    def __init__(
        self,
        hub: DeviceHub,
        token: str,
        host: str = "0.0.0.0",
        port: int = 8766,
        max_body: int = DEFAULT_MAX_BODY,
        policy: Any = None,
        debug_token: str = "",
        admin_token: str = "",
        console: Any = None,
        local_token: str = "",
        submitter: Any = None,
        stopper: Any = None,
        tool_target: Any = None,
    ) -> None:
        self.hub = hub
        self.token = token or ""
        self.host = host
        self.port = int(port)
        self.policy = policy
        self.debug_token = debug_token or ""
        # 管理控制台是可选件：admin_token 为空 / "off" / 没给适配器，就完全不注册路由。
        self.admin_token = (admin_token or "").strip()
        self.console = console
        self._admin_routes_added = False
        self._admin_fails: dict[str, list[float]] = {}
        self._console_html: str | None = None
        # 实时预览：同一时刻只打一张图，且记住上次成功的时刻做频率限制
        self._preview_lock = asyncio.Lock()
        self._preview_last_ok = 0.0
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self.started_at: float = 0.0
        # 本机 MCP 客户端通道：submitter 是 daemon 注入的「下发并等结果」回调
        self.local_token = (local_token or "").strip()
        self.submitter = submitter
        self.stopper = stopper
        self.tool_target = tool_target

        self.app = web.Application(client_max_size=int(max_body))
        self.app.router.add_post("/hb", self.handle_heartbeat)
        self.app.router.add_get("/poll", self.handle_poll)
        self.app.router.add_post("/result", self.handle_result)
        self.app.router.add_get("/healthz", self.handle_healthz)
        if self.debug_token:
            self.app.router.add_post("/debug/submit", self.handle_debug_submit)
        if self.local_token and callable(self.submitter):
            self.app.router.add_post("/local/submit", self.handle_local_submit)
            self.app.router.add_get("/local/state", self.handle_local_state)
            self.app.router.add_post("/local/stop", self.handle_local_stop)
        if self.local_token and self.tool_target is not None:
            self.app.router.add_post("/local/tool", self.handle_local_tool)
        self._ensure_admin_routes()

    # ------------------------------------------------------------ 本机 MCP 通道

    def _local_guard(self, request: web.Request) -> web.Response | None:
        """只认回环地址 + local_token：MCP 进程和 daemon 在同一台机器上。"""
        if not self._is_loopback(request):
            log.warning(
                "[dimensional-portal] 拒绝非本机访问 %s %s from %s",
                request.method,
                request.path,
                self._client_ip(request),
            )
            return web.json_response({"ok": False, "error": "forbidden"}, status=403)
        supplied = request.headers.get("X-Local-Token") or ""
        if not _token_match(supplied, self.local_token):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        return None

    async def handle_local_submit(self, request: web.Request) -> web.StreamResponse:
        guard = self._local_guard(request)
        if guard is not None:
            return guard
        try:
            payload = await request.json(loads=loads)
        except Exception:
            return web.json_response({"ok": False, "error": "请求体必须是 JSON"}, status=400)
        if not isinstance(payload, dict):
            return web.json_response({"ok": False, "error": "请求体必须是 JSON 对象"}, status=400)
        action = str(payload.get("action") or "").strip()
        if not action:
            return web.json_response({"ok": False, "error": "缺少 action"}, status=400)
        params = payload.get("params")
        if params is not None and not isinstance(params, dict):
            return web.json_response({"ok": False, "error": "params 必须是对象"}, status=400)
        timeout_ms = payload.get("timeout_ms")
        try:
            timeout_ms = int(timeout_ms) if timeout_ms is not None else None
        except (TypeError, ValueError):
            timeout_ms = None
        try:
            result = await self.submitter(action, params or {}, timeout_ms)
        except Exception as exc:  # 工具层把异常转成人话，返回给模型
            return web.json_response({"ok": False, "error": str(exc)}, status=200)
        return web.json_response({"ok": True, "result": result})

    async def handle_local_state(self, request: web.Request) -> web.StreamResponse:
        guard = self._local_guard(request)
        if guard is not None:
            return guard
        return web.json_response(
            {
                "ok": True,
                "online": self.hub.online,
                "device": self.hub.device,
                "queue": self.hub.queue_size,
                "last_heartbeat_age": self.hub.last_heartbeat_age,
                "uptime_s": round(time.monotonic() - self.started_at, 1) if self.started_at else 0.0,
                "stats": self.hub.stats,
            }
        )

    async def handle_local_stop(self, request: web.Request) -> web.StreamResponse:
        """急停：清空还没下发的任务。只清队列，不打断手机上正在跑的那一步。"""
        guard = self._local_guard(request)
        if guard is not None:
            return guard
        try:
            dropped = self.stopper() if callable(self.stopper) else self.hub.emergency_clear()
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=200)
        return web.json_response({"ok": True, "dropped": dropped})

    async def handle_local_tool(self, request: web.Request) -> web.StreamResponse:
        """按工具名调用 daemon 里的 phone_* 方法：逻辑只有一份，MCP 层只做转发。"""
        guard = self._local_guard(request)
        if guard is not None:
            return guard
        try:
            payload = await request.json(loads=loads)
        except Exception:
            return web.json_response({"ok": False, "error": "请求体必须是 JSON"}, status=400)
        if not isinstance(payload, dict):
            return web.json_response({"ok": False, "error": "请求体必须是 JSON 对象"}, status=400)
        name = str(payload.get("tool") or "").strip()
        args = payload.get("args") or {}
        if not name.startswith("phone_"):
            return web.json_response({"ok": False, "error": f"不认识的工具：{name}"}, status=400)
        if not isinstance(args, dict):
            return web.json_response({"ok": False, "error": "args 必须是对象"}, status=400)
        func = getattr(self.tool_target, name, None)
        if not callable(func):
            return web.json_response({"ok": False, "error": f"工具 {name} 不可用"}, status=404)
        try:
            result = await func(**args)
        except TypeError as exc:
            return web.json_response({"ok": False, "error": f"参数不对：{exc}"}, status=400)
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=200)
        return web.json_response({"ok": True, "result": result})

    # ------------------------------------------------------------ 管理控制台

    @property
    def admin_enabled(self) -> bool:
        return self._admin_routes_added

    def _ensure_admin_routes(self) -> None:
        """路由注册只做一次；没有适配器或 token 无效时保持 404。"""
        if self._admin_routes_added:
            return
        if self.console is None or not self.admin_token or self.admin_token.lower() == "off":
            return
        self.app.router.add_get("/console", self.handle_console)
        self.app.router.add_get("/login", self.handle_login)
        self.app.router.add_post("/login", self.handle_login_post)
        self.app.router.add_get("/logout", self.handle_logout)
        self.app.router.add_get("/console-assets/{name}", self.handle_console_asset)
        self.app.router.add_get("/admin/state", self.handle_admin_state)
        self.app.router.add_post("/admin/config", self.handle_admin_config)
        self.app.router.add_get("/admin/audit", self.handle_admin_audit)
        self.app.router.add_get("/admin/audit.csv", self.handle_admin_audit_csv)
        self.app.router.add_get("/admin/queue", self.handle_admin_queue)
        self.app.router.add_post("/admin/queue/cancel", self.handle_admin_queue_cancel)
        self.app.router.add_get("/admin/selfcheck", self.handle_admin_selfcheck)
        self.app.router.add_get("/admin/logs", self.handle_admin_logs)
        self.app.router.add_get("/admin/preview", self.handle_admin_preview)
        self.app.router.add_post("/admin/tap", self.handle_admin_tap)
        self.app.router.add_get("/admin/apps", self.handle_admin_apps)
        self.app.router.add_get("/admin/app-icon", self.handle_admin_app_icon)
        self.app.router.add_post("/admin/apps/refresh", self.handle_admin_apps_refresh)
        self.app.router.add_post("/admin/stop", self.handle_admin_stop)
        self._admin_routes_added = True
        log.info("[dimensional-portal] 管理控制台已启用（/console）")

    def enable_admin(self, admin_token: str) -> None:
        """initialize() 里自动生成 admin_token 后补注册控制台路由。"""
        self.admin_token = (admin_token or "").strip()
        self._ensure_admin_routes()

    def _session_role(self, request: web.Request) -> str | None:
        """从 cookie 里认会话；没有/过期返回 None。"""
        verify = getattr(self.console, "verify_session", None)
        if not callable(verify):
            return None
        token = request.cookies.get(SESSION_COOKIE) or ""
        if not token:
            return None
        try:
            return verify(token)
        except Exception:  # noqa: BLE001 - 会话坏了就当没登录
            return None

    def _admin_role(self, request: web.Request) -> str | None:
        """返回 'admin' / 'ro' / None。

        优先级：会话 cookie > 链接里的 k（管理密钥）> 只读口令。
        """
        role = self._session_role(request)
        if role:
            return role
        supplied = request.headers.get("X-Admin-Token") or request.query.get("k") or ""
        if supplied and _token_match(supplied, self.admin_token):
            return "admin"
        ro_supplied = request.headers.get("X-Readonly-Token") or request.query.get("rk") or ""
        if ro_supplied:
            checker = getattr(self.console, "check_readonly", None)
            if callable(checker) and checker(ro_supplied):
                return "ro"
        return None

    def _admin_authorized(self, request: web.Request) -> bool:
        return self._admin_role(request) is not None

    def _admin_throttled(self, ip: str) -> bool:
        """同一 IP 60 秒内失败满 10 次就锁住（锁随窗口过期自动解除）。"""
        now = time.monotonic()
        hits = [t for t in self._admin_fails.get(ip, []) if now - t < ADMIN_FAIL_WINDOW_S]
        self._admin_fails[ip] = hits
        return len(hits) >= ADMIN_MAX_FAILS

    def _admin_fail(self, ip: str) -> None:
        self._admin_fails.setdefault(ip, []).append(time.monotonic())
        # 顺手清掉过期的 IP，防止被人扫端口时 dict 无限涨
        if len(self._admin_fails) > 64:
            cutoff = time.monotonic() - ADMIN_FAIL_WINDOW_S
            for key in [k for k, v in self._admin_fails.items() if not v or v[-1] < cutoff]:
                self._admin_fails.pop(key, None)

    def _admin_guard(self, request: web.Request, write: bool = False) -> web.Response | None:
        ip = self._client_ip(request)
        if self._admin_throttled(ip):
            log.warning(
                "[dimensional-portal] 管理控制台尝试过多 %s %s from %s",
                request.method,
                request.path,
                ip,
            )
            return web.json_response({"ok": False, "error": "too many attempts"}, status=429)
        role = self._admin_role(request)
        if role is None:
            self._admin_fail(ip)
            return self._deny_admin(request)
        if write and role != "admin":
            return web.json_response({"ok": False, "error": "只读会话不能改东西"}, status=403)
        return None

    def _deny_admin(self, request: web.Request) -> web.Response:
        log.warning(
            "[dimensional-portal] 管理控制台鉴权失败 %s %s from %s",
            request.method,
            request.path,
            self._client_ip(request),
        )
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

    def _load_console_html(self) -> str | None:
        if self._console_html is not None:
            return self._console_html
        try:
            self._console_html = CONSOLE_HTML_PATH.read_text(encoding="utf-8")
        except OSError as exc:
            log.error("[dimensional-portal] 读不到控制台页面 %s：%s", CONSOLE_HTML_PATH, exc)
            return None
        return self._console_html

    # ------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        self._runner = web.AppRunner(self.app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()
        self.started_at = time.monotonic()
        log.info(
            "[dimensional-portal] HTTP 服务已启动 http://%s:%s（token %s）",
            self.host,
            self.port,
            "已设置" if self.token else "未设置（危险）",
        )

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._site = None
            log.info("[dimensional-portal] HTTP 服务已停止")

    @property
    def running(self) -> bool:
        return self._runner is not None

    # ------------------------------------------------------------ 工具

    def _authorized(self, request: web.Request) -> bool:
        if not self.token:
            return True
        supplied = request.headers.get("X-Token") or request.query.get("token") or ""
        return _token_match(supplied, self.token)

    @staticmethod
    def _client_ip(request: web.Request) -> str:
        # Tailscale/局域网都直连，peername 就是真实来源
        peer = request.transport.get_extra_info("peername") if request.transport else None
        return peer[0] if peer else "-"

    @classmethod
    def _is_loopback(cls, request: web.Request) -> bool:
        """是不是本机来的请求（127.0.0.1 / ::1）。"""
        return cls._client_ip(request) in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def _deny(self, request: web.Request, reason: str) -> web.Response:
        log.warning(
            "[dimensional-portal] 拒绝 %s %s from %s：%s",
            request.method,
            request.path,
            self._client_ip(request),
            reason,
        )
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

    # ------------------------------------------------------------ 路由

    async def handle_heartbeat(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._deny(request, "token 不匹配")
        raw = await request.read()
        payload = loads(raw) or {}
        if not isinstance(payload, dict):
            payload = {}

        info = {
            "device_id": str(payload.get("device_id") or "unknown")[:64],
            "device": str(payload.get("device") or "")[:64],
            "android": str(payload.get("android") or "")[:32],
            "w": _to_int(payload.get("w")),
            "h": _to_int(payload.get("h")),
            "battery": _to_int(payload.get("battery"), -1),
            "charging": bool(payload.get("charging")),
            "screen_on": bool(payload.get("screen_on")),
            "cur_pkg": str(payload.get("cur_pkg") or "")[:128],
            "accessibility": bool(payload.get("accessibility")),
            "screen_capture": bool(payload.get("screen_capture")),
            # 悬浮窗守卫决定「后台能不能打开微信/QQ」；旧脚本不上报，用 None 区分「没报」和「关了」。
            "overlay": None if payload.get("overlay") is None else bool(payload.get("overlay")),
            "overlay_note": str(payload.get("overlay_note") or "")[:128],
            "agent_version": str(payload.get("agent_version") or "")[:32],
            "protocol": _to_int(payload.get("protocol")),
            "ip": self._client_ip(request),
        }
        resp = self.hub.on_heartbeat(info)
        log.debug(
            "[dimensional-portal] 心跳 %s %sx%s 电量 %s%% 前台 %s",
            info["device_id"],
            info["w"],
            info["h"],
            info["battery"],
            info["cur_pkg"],
        )
        return web.json_response(resp)

    async def handle_poll(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._deny(request, "token 不匹配")
        # since 目前只作为手机端的游标回报，不参与任务回放：投递即出队（at-most-once）。
        try:
            since = int(request.query.get("since", "0"))
        except (TypeError, ValueError):
            since = 0
        try:
            wait_s = float(request.query.get("wait", "25"))
        except (TypeError, ValueError):
            wait_s = 25.0
        wait_s = max(0.5, min(wait_s, MAX_POLL_WAIT_S))

        payload = await self.hub.poll(since=since, wait_s=wait_s)
        return web.json_response(payload)

    async def handle_result(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._deny(request, "token 不匹配")
        payload = loads(await request.read()) or {}
        if not isinstance(payload, dict):
            return web.json_response({"ok": False, "error": "bad payload"}, status=400)

        task_id = str(payload.get("id") or "")
        if not task_id:
            return web.json_response({"ok": False, "error": "missing id"}, status=400)

        ok = payload.get("ok")
        if not isinstance(ok, bool):
            # 不能 bool() 兜底：字符串 "false" 会被当成 True，工具会把失败当成功返回。
            return web.json_response({"ok": False, "error": "ok must be a boolean"}, status=400)
        data = payload.get("data")
        error = payload.get("error")
        accepted = self.hub.on_result(task_id, ok, data, error)
        log.info(
            "[dimensional-portal] 结果 %s %s%s",
            task_id,
            "成功" if ok else "失败",
            "" if accepted else "（已被丢弃：超时或未知任务）",
        )
        return web.json_response({"ok": True, "accepted": accepted})

    async def handle_healthz(self, request: web.Request) -> web.Response:
        # 不带 token 时只证明「服务活着」，不暴露在线状态/队列长度/运行时长。
        body: dict[str, Any] = {"ok": True, "service": "dimensional-portal-phone-bridge"}
        if not self._authorized(request):
            return web.json_response(body)
        body.update(
            {
                "online": self.hub.online,
                "queue": self.hub.queue_size,
                "uptime_s": round(time.monotonic() - self.started_at, 1)
                if self.started_at
                else 0,
                "ts": now_ts(),
            }
        )
        return web.json_response(body)


    async def handle_debug_submit(self, request: web.Request) -> web.Response:
        """本地调试用：直接向手机提交一个任务并等结果，不经过 LLM。

        只有插件配置里 debug_token 非空时才会注册这个路由，且请求头必须带
        匹配的 X-Debug-Token。正式使用时请把 debug_token 清空，避免有人绕过
        白名单和限流直接驱动手机。
        """
        if not self.debug_token:
            return web.json_response(
                {"ok": False, "error": "debug endpoint disabled"}, status=404
            )
        supplied = request.headers.get("X-Debug-Token") or ""
        if not _token_match(supplied, self.debug_token):
            return self._deny(request, "debug token 不匹配")

        payload = loads(await request.read()) or {}
        if not isinstance(payload, dict):
            return web.json_response({"ok": False, "error": "bad payload"}, status=400)
        action = str(payload.get("action") or "")
        if not action:
            return web.json_response({"ok": False, "error": "missing action"}, status=400)
        params = payload.get("params") or {}
        if not isinstance(params, dict):
            return web.json_response({"ok": False, "error": "bad params"}, status=400)
        timeout_ms = _to_int(payload.get("timeout_ms"), 20000)

        log.info("[dimensional-portal] 调试任务 %s %s", action, params)
        try:
            data = await self.hub.submit(action, params, timeout_ms=timeout_ms)
        except Exception as exc:  # 离线/超时/手机端报错：原样回给调用方
            return web.json_response({"ok": False, "error": str(exc)})
        return web.json_response({"ok": True, "data": data})

    # ------------------------------------------------------------ 控制台接口

    def _login_page(self, error: str = "") -> str:
        """极简登录页：不引任何外部资源，和 CSP 对齐。"""
        hint = f'<p class="err">{error}</p>' if error else ""
        return LOGIN_HTML.replace("{{ERROR}}", hint)

    async def handle_login(self, request: web.Request) -> web.StreamResponse:
        ip = self._client_ip(request)
        if self._admin_throttled(ip):
            return web.Response(status=429, text="试得太多次了，过一会儿再来。")
        has_pw = bool(getattr(self.console, "has_password", lambda: False)())
        if not has_pw:
            return web.Response(status=400, text="还没有设置访问密码：请用带 k= 的链接打开控制台，在「安全」里设置。")
        return web.Response(text=self._login_page(), content_type="text/html", charset="utf-8")

    async def handle_login_post(self, request: web.Request) -> web.StreamResponse:
        ip = self._client_ip(request)
        if self._admin_throttled(ip):
            return web.Response(status=429, text="试得太多次了，过一会儿再来。")
        form = await request.post()
        password = str(form.get("password") or "")
        checker = getattr(self.console, "check_password", None)
        role = checker(password) if callable(checker) else None
        if not role:
            self._admin_fail(ip)
            log.warning("[dimensional-portal] 控制台登录失败 from %s", ip)
            return web.Response(text=self._login_page("口令不对。"), status=401,
                                content_type="text/html", charset="utf-8")
        issuer = getattr(self.console, "issue_session", None)
        token = issuer(role, SESSION_HOURS) if callable(issuer) else ""
        resp = web.HTTPFound("/console")
        resp.set_cookie(SESSION_COOKIE, token, max_age=SESSION_HOURS * 3600, httponly=True,
                        samesite="Lax", path="/")
        log.info("[dimensional-portal] 控制台登录成功 role=%s from %s", role, ip)
        raise resp

    async def handle_logout(self, request: web.Request) -> web.StreamResponse:
        resp = web.HTTPFound("/login")
        resp.del_cookie(SESSION_COOKIE, path="/")
        raise resp

    async def handle_console(self, request: web.Request) -> web.StreamResponse:
        ip = self._client_ip(request)
        if self._admin_throttled(ip):
            log.warning("[dimensional-portal] 管理控制台尝试过多 from %s", ip)
            return web.Response(status=429, text="Too Many Requests")

        # 登录机制：设了口令就用 session；没设口令时维持「带 k 的链接」老办法
        has_pw = bool(getattr(self.console, "has_password", lambda: False)())
        role = self._session_role(request)
        if has_pw and role is None:
            key = (request.query.get("k") or "").strip()
            if key and _token_match(key, self.admin_token):
                issuer = getattr(self.console, "issue_session", None)
                token = issuer("admin", SESSION_HOURS) if callable(issuer) else ""
                resp = web.HTTPFound("/console")  # 把 k 从地址栏里去掉
                resp.set_cookie(SESSION_COOKIE, token, max_age=SESSION_HOURS * 3600,
                                httponly=True, samesite="Lax", path="/")
                raise resp
            raise web.HTTPFound("/login")

        if not self._admin_authorized(request):
            # 「打开控制台」这种链接里没有 k：只对本机（回环地址）自动补上，
            # 免得用户还得去日志里抄一长串密钥；外网/局域网访问照旧 403。
            if not (request.query.get("k") or "").strip() and self._is_loopback(request):
                raise web.HTTPFound(f"/console?k={quote(self.admin_token, safe='')}")
            self._admin_fail(ip)
            log.warning("[dimensional-portal] 管理控制台鉴权失败 from %s", ip)
            return web.Response(status=403, text="Forbidden")
        html = self._load_console_html()
        if html is None:
            return web.json_response({"ok": False, "error": "console.html 缺失"}, status=500)
        resp = web.Response(text=html, content_type="text/html", charset="utf-8")
        resp.headers["Cache-Control"] = "no-store, max-age=0"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; "
            "script-src 'unsafe-inline'; connect-src 'self'"
        )
        return resp

    async def handle_console_asset(self, request: web.Request) -> web.StreamResponse:
        """控制台的背景图。只认白名单里的文件名，不带 token 也能取（纯装饰）。"""
        name = (request.match_info.get("name") or "").strip()
        if name not in CONSOLE_ASSETS:
            return web.Response(status=404, text="Not Found")
        path = CONSOLE_ASSET_DIR / name
        if not path.is_file():
            log.warning("[dimensional-portal] 控制台素材缺失：%s", path)
            return web.Response(status=404, text="Not Found")
        return web.FileResponse(
            path,
            headers={
                "Cache-Control": "public, max-age=86400",
                "Content-Type": "image/jpeg",
                "X-Content-Type-Options": "nosniff",
            },
        )

    async def handle_admin_state(self, request: web.Request) -> web.StreamResponse:
        guard = self._admin_guard(request)
        if guard is not None:
            return guard
        snap = self.console.snapshot() or {}
        return web.json_response(
            {
                "ok": True,
                "online": self.hub.online,
                "device": self.hub.device,
                "connection": self.hub.connection,
                "role": self._admin_role(request),
                "config": snap.get("config") or {},
                "editable": snap.get("editable") or {},
                "audit": snap.get("audit") or [],
                "queue": self.hub.queue_size,
                "uptime_s": round(time.monotonic() - self.started_at, 1) if self.started_at else 0.0,
            }
        )

    async def handle_admin_config(self, request: web.Request) -> web.StreamResponse:
        guard = self._admin_guard(request, write=True)
        if guard is not None:
            return guard
        try:
            payload = await request.json(loads=loads)
        except Exception:
            return web.json_response({"ok": False, "error": "请求体必须是 JSON"}, status=400)
        if not isinstance(payload, dict):
            return web.json_response({"ok": False, "error": "请求体必须是 JSON 对象"}, status=400)
        try:
            applied = self.console.apply_config(payload, ip=self._client_ip(request))
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        changed = [k for k in payload if k in applied]
        if changed:
            log.info("[dimensional-portal] 控制台改了安全策略：%s", ", ".join(changed))
        return web.json_response({"ok": True, "applied": applied})

    async def handle_admin_audit(self, request: web.Request) -> web.StreamResponse:
        guard = self._admin_guard(request)
        if guard is not None:
            return guard
        # 查询串都是字符串，先转成 int 并夹紧范围，免得下游实现拿到 "50" 去切片
        try:
            limit = int(request.query.get("limit", "50"))
        except (TypeError, ValueError):
            limit = AUDIT_PAGE_DEFAULT
        try:
            offset = int(request.query.get("offset", "0"))
        except (TypeError, ValueError):
            offset = 0
        limit = max(1, min(limit, AUDIT_PAGE_MAX))
        offset = max(0, offset)
        kind = str(request.query.get("kind") or "all").strip()
        keyword = str(request.query.get("q") or "").strip()[:120]
        page = self.console.audit_page(limit=limit, offset=offset, kind=kind, q=keyword)
        return web.json_response({"ok": True, **page})

    async def handle_admin_apps(self, request: web.Request) -> web.StreamResponse:
        """控制台「应用访问」卡：返回上次从手机读到的应用列表（可能为空）。"""
        guard = self._admin_guard(request)
        if guard is not None:
            return guard
        snapshot = self.console.snapshot()
        apps = list(snapshot.get("apps") or [])
        return web.json_response({"ok": True, "apps": apps, "count": len(apps)})

    async def handle_admin_app_icon(self, request: web.Request) -> web.StreamResponse:
        """控制台「应用访问」卡的应用图标（PNG，按需取，磁盘缓存）。"""
        guard = self._admin_guard(request)
        if guard is not None:
            return guard
        package = (request.query.get("pkg") or "").strip()
        fetcher = getattr(self.console, "fetch_icon", None)
        if not callable(fetcher):
            return web.json_response({"ok": False, "error": "这个版本不支持图标"}, status=404)
        blob = await fetcher(package)
        if not blob:
            return web.Response(status=404)
        return web.Response(
            body=blob,
            headers={
                "Content-Type": "image/png",
                "Cache-Control": "public, max-age=604800",
                "X-Content-Type-Options": "nosniff",
            },
        )

    async def handle_admin_apps_refresh(self, request: web.Request) -> web.StreamResponse:
        """去手机上现读一次应用列表（慢，几秒），读完返回。"""
        guard = self._admin_guard(request, write=True)
        if guard is not None:
            return guard
        refresher = getattr(self.console, "refresh_apps", None)
        if not callable(refresher):
            return web.json_response({"ok": False, "error": "这个版本不支持刷新应用列表"})
        try:
            apps = await refresher()
        except Exception as exc:  # noqa: BLE001 - 手机上任何异常都转成人话
            return web.json_response({"ok": False, "error": str(exc)}, status=200)
        return web.json_response({"ok": True, "apps": list(apps or []), "count": len(apps or [])})

    async def handle_admin_stop(self, request: web.Request) -> web.StreamResponse:
        guard = self._admin_guard(request, write=True)
        if guard is not None:
            return guard
        cleared = int(self.console.stop_all())
        log.info("[dimensional-portal] 控制台急停：清掉排队任务 %s 个", cleared)
        return web.json_response({"ok": True, "cleared": cleared})

    # ------------------------------------------------------------ 预览 / 点图

    async def handle_admin_preview(self, request: web.Request) -> web.StreamResponse:
        """实时预览：一张 JPEG。只读会话也能看；没开开关 / 手机离线不会打手机。"""
        guard = self._admin_guard(request)
        if guard is not None:
            return guard
        enabled = getattr(self.console, "preview_enabled", None)
        if not callable(enabled) or not enabled():
            return web.json_response(
                {"ok": False, "error": "实时预览没打开：先去「安全策略」里打开它"},
                status=403,
            )
        interval_getter = getattr(self.console, "preview_interval_ms", None)
        interval_ms = int(interval_getter()) if callable(interval_getter) else 1500
        wait_ms = int((interval_ms / 1000.0 - (time.monotonic() - self._preview_last_ok)) * 1000)
        if self._preview_last_ok and wait_ms > 0:
            seconds = max(1, round(wait_ms / 1000))
            return web.json_response(
                {
                    "ok": False,
                    "error": f"预览太快了，{seconds} 秒后再来",
                    "retry_after_ms": wait_ms,
                },
                status=429,
                headers={"Retry-After": str(seconds), "Cache-Control": "no-store"},
            )
        if self._preview_lock.locked():
            return web.json_response(
                {"ok": False, "error": "上一张还没回来，稍等一下"},
                status=429,
                headers={"Cache-Control": "no-store"},
            )
        grabber = getattr(self.console, "preview_frame", None)
        if not callable(grabber):
            return web.json_response({"ok": False, "error": "这个版本不支持实时预览"}, status=501)
        async with self._preview_lock:
            try:
                blob = await grabber()
            except Exception as exc:  # noqa: BLE001 - 手机端异常都转成人话
                offline = bool(getattr(exc, "offline", False))
                sensitive = bool(getattr(exc, "sensitive", False))
                status = 503 if offline else (409 if sensitive else 502)
                log.warning("[dimensional-portal] 预览失败：%s", exc)
                return web.json_response({"ok": False, "error": str(exc)}, status=status)
            self._preview_last_ok = time.monotonic()
        blob = _shrink_jpeg(blob, PREVIEW_MAX_EDGE)
        return web.Response(
            body=blob,
            headers={
                "Content-Type": "image/jpeg",
                "Cache-Control": "no-store, max-age=0",
                "X-Content-Type-Options": "nosniff",
            },
        )

    async def handle_admin_tap(self, request: web.Request) -> web.StreamResponse:
        """点图：前端按 device.w/h 换算好的手机坐标，走 phone_tap 同一条安全链路。"""
        guard = self._admin_guard(request, write=True)
        if guard is not None:
            return guard
        try:
            payload = await request.json(loads=loads)
        except Exception:
            return web.json_response({"ok": False, "error": "请求体必须是 JSON"}, status=400)
        if not isinstance(payload, dict):
            return web.json_response({"ok": False, "error": "请求体必须是 JSON 对象"}, status=400)
        tapper = getattr(self.console, "tap", None)
        if not callable(tapper):
            return web.json_response({"ok": False, "error": "这个版本不支持点图"}, status=501)
        try:
            result = await tapper(payload.get("x"), payload.get("y"))
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"ok": False, "error": str(exc)}, status=502)
        if not isinstance(result, dict) or not result.get("ok"):
            text = str((result or {}).get("text") or "没点下去")
            return web.json_response({"ok": False, "error": text}, status=409)
        log.info("[dimensional-portal] 控制台点图：(%s, %s)", payload.get("x"), payload.get("y"))
        return web.json_response(
            {"ok": True, "x": payload.get("x"), "y": payload.get("y"), "result": result.get("data") or {}}
        )

    # ------------------------------------------------------------ 队列 / 自检 / 日志

    async def handle_admin_queue(self, request: web.Request) -> web.StreamResponse:
        """排队中的任务摘要（急停卡里逐条取消用）。"""
        guard = self._admin_guard(request)
        if guard is not None:
            return guard
        items = self.hub.pending()
        return web.json_response({"ok": True, "items": items, "count": len(items)})

    async def handle_admin_queue_cancel(self, request: web.Request) -> web.StreamResponse:
        guard = self._admin_guard(request, write=True)
        if guard is not None:
            return guard
        try:
            payload = await request.json(loads=loads)
        except Exception:
            return web.json_response({"ok": False, "error": "请求体必须是 JSON"}, status=400)
        if not isinstance(payload, dict):
            return web.json_response({"ok": False, "error": "请求体必须是 JSON 对象"}, status=400)
        task_id = str(payload.get("id") or "").strip()
        if not task_id:
            return web.json_response({"ok": False, "error": "缺少任务 id"}, status=400)
        if not self.hub.cancel(task_id):
            return web.json_response(
                {"ok": False, "error": "这个任务已经不在队列里（可能刚被手机取走）"},
                status=404,
            )
        log.info("[dimensional-portal] 控制台取消排队任务：%s", task_id)
        return web.json_response({"ok": True, "cancelled": task_id})

    async def handle_admin_selfcheck(self, request: web.Request) -> web.StreamResponse:
        guard = self._admin_guard(request)
        if guard is not None:
            return guard
        checker = getattr(self.console, "selfcheck", None)
        if not callable(checker):
            return web.json_response({"ok": False, "error": "这个版本不支持自检"}, status=501)
        try:
            items = list(checker())
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"ok": False, "error": str(exc)}, status=500)
        passed = sum(1 for item in items if item.get("ok"))
        log.info("[dimensional-portal] 控制台自检：%s/%s 项通过", passed, len(items))
        return web.json_response({"ok": True, "items": items, "count": len(items), "passed": passed})

    async def handle_admin_logs(self, request: web.Request) -> web.StreamResponse:
        """daemon 最近日志（读 tmp/daemon.log 尾部；文件不存在返回空数组）。"""
        guard = self._admin_guard(request)
        if guard is not None:
            return guard
        count = _to_int(request.query.get("lines"), 80)
        count = max(1, min(count, LOG_MAX_LINES))
        lines = _tail_lines(DAEMON_LOG_PATH, count)
        return web.json_response(
            {"ok": True, "lines": lines, "count": len(lines), "path": str(DAEMON_LOG_PATH)}
        )

    async def handle_admin_audit_csv(self, request: web.Request) -> web.StreamResponse:
        """审计导出：带 BOM 的 UTF-8 CSV，Excel 直接打开不乱码。"""
        guard = self._admin_guard(request)
        if guard is not None:
            return guard
        kind = str(request.query.get("kind") or "all").strip()
        keyword = str(request.query.get("q") or "").strip()[:120]
        exporter = getattr(self.console, "audit_export", None)
        if callable(exporter):
            items = list(exporter(kind, keyword))
        else:
            page = self.console.audit_page(limit=AUDIT_PAGE_MAX, offset=0, kind=kind, q=keyword)
            items = list(page.get("items") or [])
        body = self._audit_csv(items)
        filename = quote("dimensional-portal-audit.csv")
        return web.Response(
            body=body,
            headers={
                "Content-Type": "text/csv; charset=utf-8",
                "Content-Disposition": f"attachment; filename=\"audit.csv\"; filename*=UTF-8''{filename}",
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @staticmethod
    def _audit_csv(items: list[dict[str, Any]]) -> bytes:
        columns = (
            ("ts", "时间戳"),
            ("kind", "类型"),
            ("action", "动作"),
            ("pkg", "应用"),
            ("user", "用户"),
            ("reason", "原因"),
            ("target", "目标"),
            ("dropped", "清掉任务"),
            ("bytes", "字节数"),
            ("ok", "成功"),
            ("error", "错误"),
            ("change", "改动"),
            ("params", "参数"),
        )
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([label for _key, label in columns])
        for item in items:
            row: list[str] = []
            for key, _label in columns:
                value = item.get(key, "")
                if isinstance(value, (dict, list)):
                    value = dumps(value)
                text = "" if value is None else str(value)
                # 防 CSV 公式注入：Excel 会把 = + - @ 开头的单元格当公式
                if text[:1] in ("=", "+", "-", "@"):
                    text = "'" + text
                row.append(text)
            writer.writerow(row)
        return ("\ufeff" + buf.getvalue()).encode("utf-8")


def _decode_log(raw: bytes) -> str:
    """日志编码兜底：正常是 UTF-8（PYTHONUTF8=1 启动），
    但如果 daemon 是用 `> daemon.log` 重定向启动的，Windows 会按 GBK 写，
    直接按 UTF-8 读就是一片乱码。所以按 UTF-8 → GB18030 → 替换 的顺序试。"""
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _tail_lines(path: Path, count: int) -> list[str]:
    """读文件尾部 count 行；不存在/读不了就返回空列表。"""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - LOG_READ_TAIL_BYTES))
            raw = fh.read()
    except OSError:
        return []
    lines = _decode_log(raw).splitlines()
    if size > LOG_READ_TAIL_BYTES and lines:
        lines = lines[1:]  # 第一行可能是半条
    safe = [_LOG_SECRET_RE.sub(r"\1***\3", line) for line in lines]
    return safe[-max(1, count):]


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


__all__ = ["PhoneBridgeServer", "dumps", "truncate"]
