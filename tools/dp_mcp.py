#!/usr/bin/env python
"""次元门 DSH 版 · MCP 工具层。

DSH 通过 stdio 起这个进程，它本身不碰手机，只把工具调用转发给同机的 daemon：

    DSH 会话 --MCP(stdio)--> 本进程 --HTTP(回环+X-Local-Token)--> daemon(:8766) <--长轮询-- 手机

工具名和 AstrBot 版一致（phone_see / phone_tap / ...），逻辑全在 daemon 里，
所以这里只做「参数打包 + 把返回文本原样交回去」。

用法：把自己挂进 DSH profile（见 README），或者手动跑 `python tools/dp_mcp.py` 调试。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from dp.config import load  # noqa: E402

CFG = load(create=False)
HOST = os.environ.get("DP_HOST", "127.0.0.1")
PORT = int(os.environ.get("DP_PORT") or CFG.get("listen_port") or 8766)
LOCAL_TOKEN = os.environ.get("DP_LOCAL_TOKEN") or str(CFG.get("local_token") or "")

mcp = FastMCP("dimensional-portal")


def _request(method: str, path: str, payload: dict[str, Any] | None = None,
             timeout_s: float = 60.0) -> dict[str, Any]:
    url = f"http://{HOST}:{PORT}{path}"
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("X-Local-Token", LOCAL_TOKEN)
    if body is not None:
        req.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            data = json.loads(raw)
        except ValueError:
            data = {"ok": False, "error": f"HTTP {exc.code}：{raw[:200]}"}
        return data if isinstance(data, dict) else {"ok": False, "error": raw}
    except urllib.error.URLError as exc:
        return {
            "ok": False,
            "error": (
                f"连不上本机 daemon（http://{HOST}:{PORT}）：{exc.reason}。"
                "先运行 tools/dp_daemon.py 把它起起来。"
            ),
        }
    try:
        data = json.loads(raw)
    except ValueError:
        return {"ok": False, "error": f"daemon 返回的不是 JSON：{raw[:200]}"}
    return data if isinstance(data, dict) else {"ok": False, "error": str(data)}


def _call(tool: str, args: dict[str, Any] | None = None, timeout_s: float = 60.0) -> str:
    """把一次工具调用转给 daemon，并把结果压成好看的文本。"""
    data = _request("POST", "/local/tool", {"tool": tool, "args": args or {}}, timeout_s)
    if not data.get("ok"):
        return f"❌ {data.get('error') or '调用失败'}"
    result = data.get("result")
    if isinstance(result, str):
        return result
    return json.dumps(result, ensure_ascii=False, indent=2)


# ============================================================ 工具

@mcp.tool()
async def phone_status() -> str:
    """查看手机连接状态：是否在线、电量、屏幕分辨率、当前应用、无障碍/截图权限。

    不需要手机在线也能调用，掉线时用它排查问题（会告诉你该检查什么）。
    """
    state = _request("GET", "/local/state", None, 15.0)
    if not state.get("ok"):
        return f"❌ {state.get('error') or '查不到状态'}"
    if not state.get("online"):
        age = state.get("last_heartbeat_age")
        hint = f"最后一次心跳 {age:.0f} 秒前。" if isinstance(age, (int, float)) else "还没收到过心跳。"
        return (
            "🔌 手机离线。" + hint + "\n"
            "检查：①手机上 AutoX 里脚本还在运行吗（被系统杀了要重新点「运行」）\n"
            "②手机和这台电脑在同一网络/Tailscale 里吗 ③手机脚本里的 BASES/TOKEN 对得上吗"
        )
    dev = state.get("device") or {}
    battery = dev.get("battery")
    charge = "·充电中" if dev.get("charging") else ""
    return (
        f"✅ 手机在线：{dev.get('device') or '?'} / Android {dev.get('android') or '?'}"
        f" / {dev.get('w')}x{dev.get('h')}\n"
        f"电量 {battery if battery is not None else '?'}%{charge}"
        f"，前台 {dev.get('cur_pkg') or '?'}，队列 {state.get('queue')} 个任务"
    )


@mcp.tool()
async def phone_see(with_ui: bool = True) -> str:
    """看一眼手机现在显示什么：截一张图保存到本地，返回图片路径和界面文字。

    这是最常用的第一个动作。拿到路径后用 read 工具把那张图读出来看，
    再用 phone_tap 点坐标、phone_type 输入。敏感界面（微信/支付宝/银行/设置）
    默认不截图，只返回控件文字。

    Args:
        with_ui (bool): 是否同时返回界面文字摘要，默认 True
    """
    return _call("phone_see", {"with_ui": bool(with_ui)}, 90.0)


@mcp.tool()
async def phone_ui(max_nodes: int = 80) -> str:
    """读手机当前界面的控件树（文字、坐标、是否可点），不截图。

    适合看不清截图、或者敏感界面不方便截图时用。每行给出中心坐标 @(x,y)，
    可以直接拿去 phone_tap。

    Args:
        max_nodes (int): 最多显示多少个节点，默认 80
    """
    return _call("phone_ui", {"max_nodes": int(max_nodes)}, 45.0)


@mcp.tool()
async def phone_tap(x: int, y: int) -> str:
    """点击屏幕上一个点。坐标是真实像素，原点在左上角。

    坐标从 phone_see 的截图或 phone_ui 的 @(x,y) 里取。

    Args:
        x (int): 横坐标像素
        y (int): 纵坐标像素
    """
    return _call("phone_tap", {"x": int(x), "y": int(y)}, 45.0)


@mcp.tool()
async def phone_long_press(x: int, y: int, duration_ms: int = 800) -> str:
    """长按一个点（默认 800ms），常用于弹出菜单、拖动前抓取。

    Args:
        x (int): 横坐标像素
        y (int): 纵坐标像素
        duration_ms (int): 按住时长，默认 800
    """
    return _call("phone_long_press", {"x": int(x), "y": int(y), "duration_ms": int(duration_ms)}, 45.0)


@mcp.tool()
async def phone_swipe(x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> str:
    """滑动/滚动屏幕：从 (x1,y1) 划到 (x2,y2)。向上划 = 看下面的内容。

    Args:
        x1 (int): 起点横坐标
        y1 (int): 起点纵坐标
        x2 (int): 终点横坐标
        y2 (int): 终点纵坐标
        duration_ms (int): 滑动时长，默认 300
    """
    return _call(
        "phone_swipe",
        {"x1": int(x1), "y1": int(y1), "x2": int(x2), "y2": int(y2), "duration_ms": int(duration_ms)},
        45.0,
    )


@mcp.tool()
async def phone_type(text: str, submit: bool = False) -> str:
    """往当前焦点输入框里打字。先 phone_tap 点一下输入框，再调这个。

    如果 setText 注入失败，手机端会自动退回剪贴板粘贴。

    Args:
        text (str): 要输入的文字
        submit (bool): 输完是否立刻回车提交（搜索框/聊天框常用），默认 False
    """
    return _call("phone_type", {"text": str(text), "submit": bool(submit)}, 45.0)


@mcp.tool()
async def phone_key(key: str = "back") -> str:
    """按系统键：back（返回）/ home（回桌面）/ recents（最近任务）/ enter / delete / volume_up / volume_down。

    卡在某个页面时用 back 退出；想回桌面用 home。

    Args:
        key (str): 按键名，默认 back
    """
    return _call("phone_key", {"key": str(key)}, 30.0)


@mcp.tool()
async def phone_open_app(name: str) -> str:
    """打开手机上的一个应用。可以给应用名（中文名如「微信」「相机」）或包名（com.tencent.mm）。

    会等一会儿再返回前台包名，用来确认真的打开了。

    Args:
        name (str): 应用名或包名
    """
    return _call("phone_open_app", {"name": str(name)}, 60.0)


@mcp.tool()
async def phone_find_tap(text: str, index: int = 0, exact: bool = False) -> str:
    """在屏幕上找一段文字并点它（比算坐标稳）。找不到会告诉你当前界面有哪些文字。

    Args:
        text (str): 要找的文字，比如「设置」「发送」「允许」
        index (int): 有多个匹配时点第几个，从 0 开始
        exact (bool): True = 文字完全相等；False = 包含即可
    """
    return _call(
        "phone_find_tap",
        {"text": str(text), "index": int(index), "exact": bool(exact)},
        45.0,
    )


@mcp.tool()
async def phone_stop() -> str:
    """急停：清空所有还没发出去的任务。用户说「停下」「别动了」时立刻调用。"""
    data = _request("POST", "/local/stop", {}, 20.0)
    if not data.get("ok"):
        return f"❌ {data.get('error') or '急停失败'}"
    return f"🛑 已急停，清空 {data.get('dropped', 0)} 个待执行任务。手机端正在跑的那一步会做完。"


def main() -> None:
    if not LOCAL_TOKEN:
        sys.stderr.write("dp_mcp: dp.json 里没有 local_token，无法和 daemon 通信\n")
        raise SystemExit(2)
    mcp.run()


if __name__ == "__main__":
    main()
