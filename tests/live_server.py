"""端到端：真起 daemon（aiohttp:18776）+ 假手机长轮询 + 真调 11 个工具方法。

和 smoke_test.py 的分工：
  smoke_test   只测纯逻辑（policy / hub / config），不联网
  本文件       起真 HTTP 服务、真的下发任务、真的收结果，顺带验证本机 MCP 通道

跑法（用装了 aiohttp 的 python）：
    <venv>/Scripts/python.exe tests/live_server.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

import aiohttp

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 截图和审计写到临时目录，别污染仓库
_TMP = Path(tempfile.mkdtemp(prefix="dp-live-"))
os.environ["DP_DATA_DIR"] = str(_TMP)
os.environ["DIMENSIONAL_PORTAL_AUDIT_PATH"] = str(_TMP / "audit.jsonl")

from dp.service import PhoneService  # noqa: E402

HOST = "127.0.0.1"
PORT = 18776
TOKEN = "live-token"
LOCAL_TOKEN = "live-local-token"

PASS = 0
FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {extra}")


TINY_JPG_B64 = (
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
    "HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAAAA"
    "AAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q=="
)

STATE = {"cur_pkg": "com.example.notes", "overlay": True}

UI_PAYLOAD = {
    "pkg": "com.example.notes",
    "w": 1080,
    "h": 2400,
    "nodes": [
        {"cls": "android.widget.TextView", "text": "我的笔记", "id": "title",
         "clickable": False, "editable": False, "left": 0, "top": 100,
         "right": 1080, "bottom": 200, "cx": 540, "cy": 150, "depth": 2},
        {"cls": "android.widget.Button", "text": "新建", "id": "fab",
         "clickable": True, "editable": False, "left": 800, "top": 2100,
         "right": 1000, "bottom": 2300, "cx": 900, "cy": 2200, "depth": 3},
    ],
}


async def fake_phone(session: aiohttp.ClientSession, stop: asyncio.Event, log: list) -> None:
    async def heartbeat_loop() -> None:
        while not stop.is_set():
            body = {
                "device_id": "fakephone1", "device": "Fake Pixel 9", "android": "15",
                "w": 1080, "h": 2400, "battery": 88, "charging": True, "screen_on": True,
                "cur_pkg": STATE["cur_pkg"], "accessibility": True, "screen_capture": True,
                "overlay": STATE["overlay"], "overlay_note": "ok(WindowManager)",
                "agent_version": "0.3.0", "protocol": 1,
            }
            try:
                async with session.post(
                    f"http://{HOST}:{PORT}/hb", json=body, headers={"X-Token": TOKEN}
                ) as resp:
                    await resp.read()
            except Exception as exc:
                log.append(f"hb-error {exc}")
            await asyncio.sleep(0.3)

    async def poll_loop() -> None:
        since = 0
        while not stop.is_set():
            try:
                async with session.get(
                    f"http://{HOST}:{PORT}/poll",
                    params={"since": since, "wait": 2},
                    headers={"X-Token": TOKEN},
                ) as resp:
                    data = await resp.json()
            except Exception as exc:
                log.append(f"poll-error {exc}")
                await asyncio.sleep(0.3)
                continue
            since = data.get("server_seq", since)
            for job in data.get("jobs") or []:
                action = job["action"]
                params = job.get("params") or {}
                out: dict = {"pkg": STATE["cur_pkg"]}
                if action == "screen":
                    out = {"image_b64": TINY_JPG_B64, "w": 1080, "h": 2400,
                           "pkg": STATE["cur_pkg"], "format": "jpg", "quality": 55}
                elif action == "ui":
                    out = dict(UI_PAYLOAD, pkg=STATE["cur_pkg"])
                elif action == "tap":
                    out = {"clicked": [params.get("x"), params.get("y")], "pkg": STATE["cur_pkg"]}
                elif action == "text":
                    out = {"method": "setText", "submitted": bool(params.get("submit")),
                           "pkg": STATE["cur_pkg"]}
                elif action == "key":
                    out = {"key": params.get("name"), "pkg": STATE["cur_pkg"]}
                elif action == "find_text":
                    out = {"found": True, "tapped": True, "total": 1,
                           "matched": params.get("text"), "cx": 900, "cy": 2200,
                           "pkg": STATE["cur_pkg"]}
                elif action == "apps":
                    out = {"apps": [{"package": "com.example.notes", "name": "我的笔记"},
                                    {"package": "com.tencent.mm", "name": "微信"},
                                    {"package": "com.android.bbkcalculator", "name": "计算器"}],
                           "total": 3, "w": 1080, "h": 2400}
                elif action == "open_app":
                    out = {"launched": True, "pkg": "com.tencent.mm",
                           "target": params.get("package") or params.get("name"),
                           "bounced": False, "seen": None, "warning": ""}
                async with session.post(
                    f"http://{HOST}:{PORT}/result",
                    json={"id": job["id"], "ok": True, "data": out, "error": None},
                    headers={"X-Token": TOKEN},
                ) as resp:
                    await resp.read()

    await asyncio.gather(heartbeat_loop(), poll_loop())


async def wait_online(hub, timeout: float = 6.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if hub.online:
            return True
        await asyncio.sleep(0.05)
    return False


async def main() -> int:
    cfg = {
        "token": TOKEN,
        "listen_host": HOST,
        "listen_port": PORT,
        "local_token": LOCAL_TOKEN,
        "admin_token": "live-admin",
        "debug_token": "",
        "rate_limit_per_minute": 100,
        "task_timeout_ms": 8000,
        "offline_after_seconds": 5,
        "screenshot_quality": 55,
        "max_screenshot_kb": 1500,
        "allow_screenshot_sensitive": False,
        "allow_sensitive_write": False,
        "package_blacklist": ["com.tencent.mm"],
        "sensitive_packages": [],
    }
    svc = PhoneService(cfg)

    print(f"\n[1] start() 真监听 {HOST}:{PORT}")
    ok = await svc.start()
    check("服务已启动", ok and svc.server.running)
    check("冷启动不算在线", svc.hub.online is False)
    check("冷启动心跳年龄为 None", svc.hub.snapshot()["heartbeat_age"] is None)

    stop = asyncio.Event()
    errors: list = []
    async with aiohttp.ClientSession() as session:
        phone = asyncio.create_task(fake_phone(session, stop, errors))
        try:
            print("\n[2] /healthz 免 token")
            async with session.get(f"http://{HOST}:{PORT}/healthz") as resp:
                hz = await resp.json()
            check("healthz 200", resp.status == 200, f"status={resp.status}")
            check("healthz 不泄露细节", "online" not in hz, str(hz))

            print("\n[3] 本机 MCP 通道鉴权")
            async with session.post(
                f"http://{HOST}:{PORT}/local/tool", json={"tool": "phone_ui", "args": {}}
            ) as resp:
                await resp.read()
            check("无 local_token 回 401", resp.status == 401, f"status={resp.status}")

            print("\n[4] 手机上线")
            check("手机上线", await wait_online(svc.hub), str(svc.hub.snapshot()))

            print("\n[5] 11 个工具方法（真走 _gate → _submit → HTTP）")
            st = await svc.phone_status()
            check("phone_status 报在线", "在线" in st and "1080x2400" in st, st)

            tap = await svc.phone_tap(540, 1200)
            check("phone_tap 成功", "点击" in tap, tap)

            see = await svc.phone_see()
            check("phone_see 返回图片路径", "SHOT_PATH:" in see, see[:120])
            shot = see.split("SHOT_PATH:")[1].strip().splitlines()[0] if "SHOT_PATH:" in see else ""
            check("截图文件真的落盘", bool(shot) and Path(shot).is_file(), shot)

            ui = await svc.phone_ui(20)
            check("phone_ui 读到文字", "新建" in ui, ui[:160])

            blocked = await svc.phone_open_app("微信")
            check("黑名单按真实 pkg 拦下", "拦截" in blocked, blocked[:160])

            typed = await svc.phone_type("你好", submit=True)
            check("phone_type 成功", "输入" in typed, typed)

            found = await svc.phone_find_tap("新建")
            check("phone_find_tap 命中", "找到" in found, found)

            key = await svc.phone_key("home")
            check("phone_key 成功", "home" in key, key)

            print("\n[6] 本机 MCP 通道（/local/tool、/local/state、/local/stop）")
            headers = {"X-Local-Token": LOCAL_TOKEN}
            async with session.post(
                f"http://{HOST}:{PORT}/local/tool",
                json={"tool": "phone_ui", "args": {"max_nodes": 5}},
                headers=headers,
            ) as resp:
                data = await resp.json()
            check("/local/tool 调通", resp.status == 200 and data.get("ok"), str(data)[:160])

            async with session.post(
                f"http://{HOST}:{PORT}/local/tool",
                json={"tool": "rm_rf", "args": {}},
                headers=headers,
            ) as resp:
                await resp.read()
            check("非 phone_ 工具回 400", resp.status == 400, f"status={resp.status}")

            async with session.get(
                f"http://{HOST}:{PORT}/local/state", headers=headers
            ) as resp:
                state = await resp.json()
            check("/local/state 带设备信息", state.get("online") is True
                  and "device" in state, str(state)[:160])

            async with session.post(
                f"http://{HOST}:{PORT}/local/stop", headers=headers
            ) as resp:
                stopped = await resp.json()
            check("/local/stop 可用", stopped.get("ok") is True, str(stopped))

            print("\n8")
            async with session.get(f"http://{HOST}:{PORT}/admin/apps", headers={}) as resp:
                await resp.read()
            check("/admin/apps 没 key 401", resp.status == 401, f"status={resp.status}")

            async with session.post(
                f"http://{HOST}:{PORT}/admin/apps/refresh?k=live-admin") as resp:
                payload = await resp.json()
            check("刷新应用列表拿到 3 个", payload.get("ok") is True
                  and len(payload.get("apps") or []) == 3, str(payload)[:200])

            async with session.get(f"http://{HOST}:{PORT}/admin/apps?k=live-admin") as resp:
                cached = await resp.json()
            check("/admin/apps 返回缓存", cached.get("count") == 3, str(cached)[:160])

            async with session.post(
                f"http://{HOST}:{PORT}/admin/config?k=live-admin",
                json={"app_blocked": ["com.android.bbkcalculator"]},
            ) as resp:
                applied = await resp.json()
            check("保存 app_blocked 生效", applied.get("ok") is True
                  and "com.android.bbkcalculator" in (applied.get("applied") or {}).get("app_blocked", []),
                  str(applied)[:200])
            blocked_text = await svc.submit_action("open_app", {"package": "com.android.bbkcalculator"})
            check("被禁用的应用打不开", blocked_text.get("ok") is False
                  and "禁止访问" in str(blocked_text.get("text")), str(blocked_text)[:200])

            async with session.post(
                f"http://{HOST}:{PORT}/admin/config?k=live-admin",
                json={"app_mode": "allowlist", "app_allowed": ["com.tencent.mm"],
                      "app_blocked": []},
            ) as resp:
                await resp.read()
            allowed_text = await svc.submit_action("open_app", {"package": "com.tencent.mm"})
            check("白名单里的应用可打开", allowed_text.get("ok") is True, str(allowed_text)[:200])
            denied_text = await svc.submit_action("open_app", {"package": "com.example.notes"})
            check("白名单外的应用被拦", denied_text.get("ok") is False, str(denied_text)[:200])

            async with session.post(
                f"http://{HOST}:{PORT}/admin/config?k=live-admin",
                json={"app_mode": "blocklist", "app_blocked": [], "app_allowed": []},
            ) as resp:
                await resp.read()

            print("\n7 phone_stop")
            text = await svc.phone_stop()
            check("phone_stop 返回急停文案", "急停" in text, text)
        finally:
            stop.set()
            phone.cancel()
            with_ctx = asyncio.gather(phone, return_exceptions=True)
            await with_ctx
            await svc.stop()

    print("\n" + "=" * 46)
    print(f"  通过 {PASS} 项，失败 {FAIL} 项")
    if errors:
        print(f"  假手机期间的报错（正常收尾时可能为空）：{errors[:3]}")
    print("=" * 46)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
