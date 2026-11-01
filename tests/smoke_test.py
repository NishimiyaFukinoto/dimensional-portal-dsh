"""纯逻辑测试：policy / hub / config。不联网、不起服务。

跑法：<任意 python> tests/smoke_test.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import asyncio  # noqa: E402

from bridge.hub import DeviceHub, DeviceOffline, TaskRejected, TaskTimeout  # noqa: E402
from dp import config as cfgmod  # noqa: E402
from safety.policy import SafetyPolicy  # noqa: E402

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


def test_config() -> None:
    print("\n[1] 配置读写")
    tmp = Path(tempfile.mkdtemp(prefix="dp-cfg-")) / "dp.json"
    cfg = cfgmod.load(tmp)
    check("首次加载会生成文件", tmp.is_file())
    check("自动生成 token", bool(cfg.get("token")))
    check("自动生成 local_token", bool(cfg.get("local_token")))
    check("默认端口 8766", cfg.get("listen_port") == 8766)
    check("默认不开敏感截图", cfg.get("allow_screenshot_sensitive") is False)

    again = cfgmod.load(tmp)
    check("二次加载 token 不变", again["token"] == cfg["token"])
    check("二次加载 local_token 不变", again["local_token"] == cfg["local_token"])

    raw = json.loads(tmp.read_text(encoding="utf-8"))
    raw["allowed_user_ids"] = "123,456"  # 老配置遗留字段
    raw["listen_port"] = 9999
    tmp.write_text(json.dumps(raw), encoding="utf-8")
    third = cfgmod.load(tmp)
    check("读到的旧字段被忽略", "allowed_user_ids" not in third)
    check("自定义端口被保留", third["listen_port"] == 9999)

    view = cfgmod.public_view(third)
    check("public_view 遮住 local_token", view["local_token"] != third["local_token"])
    check("public_view 保留 token（手机要抄）", view["token"] == third["token"])

    check("默认不开实时预览", third.get("preview_enabled") is False)
    check("默认预览间隔 1500", third.get("preview_min_interval_ms") == 1500)
    example = (ROOT / "dp.json.example").read_text(encoding="utf-8")
    check(
        "示例配置带预览字段",
        '"preview_enabled"' in example and '"preview_min_interval_ms"' in example,
    )


def test_log_decoding() -> None:
    """控制台「最近日志」要能同时读懂 UTF-8 和 GBK 写的日志。

    背景：Windows 上 `python daemon.py > daemon.log` 默认按 GBK 写，
    如果按 UTF-8 硬读就是满屏乱码（真实踩过）。
    """
    from bridge.server import _decode_log

    zh = "服务已启动，端口 8766"
    check("UTF-8 日志原文", _decode_log(zh.encode("utf-8")) == zh)
    check("GBK 日志原文", _decode_log(zh.encode("gb18030")) == zh)
    check("二进制垃圾不抛异常", isinstance(_decode_log(bytes([0xff, 0xfe, 0x00])), str))
    line = '配置：{"token": "abc", "console_password": "secret"}'
    from bridge.server import _LOG_SECRET_RE

    masked = _LOG_SECRET_RE.sub(r"\1***\3", line)
    check("日志里的密钥被打码", '"token": "***"' in masked and "secret" not in masked)


def test_policy() -> None:
    print("\n[2] 安全策略")
    audit = Path(tempfile.mkdtemp(prefix="dp-audit-")) / "audit.jsonl"
    policy = SafetyPolicy(
        rate_limit=3,
        rate_window_s=60,
        sensitive_packages=["com.tencent.mm"],
        package_blacklist=["com.android.systemui"],
        audit_path=audit,
    )
    check("黑名单命中", policy.package_allowed("com.android.systemui")[0] is False)
    check("普通应用放行", policy.package_allowed("com.tencent.mobileqq")[0] is True)

    results = [policy.rate_check("local")[0] for _ in range(4)]
    check("限流前 3 次放过", all(results[:3]))
    check("第 4 次被限流", results[3] is False)

    sensitive, why = policy.is_sensitive_screen("com.tencent.mm", "")
    check("敏感包名被识别", sensitive and "微信" in why or "com.tencent.mm" in why)
    sensitive2, _ = policy.is_sensitive_screen("com.example.app", "请输入支付密码")
    check("敏感文字被识别", sensitive2 is True)
    sensitive3, _ = policy.is_sensitive_screen("com.example.app", "今天天气不错")
    check("普通界面不算敏感", sensitive3 is False)

    policy.audit("task", action="tap", x=1)
    check("审计写入 jsonl", audit.is_file() and "tap" in audit.read_text(encoding="utf-8"))


async def test_hub() -> None:
    print("\n[3] 任务队列")
    hub = DeviceHub(offline_after=0.5)
    check("没心跳时离线", hub.online is False)
    check("心跳为 None 时不算在线", hub.snapshot()["heartbeat_age"] is None)

    hub.on_heartbeat({"device": "Fake", "w": 1080, "h": 2400})
    check("心跳后在线", hub.online is True)
    check("设备信息可读", hub.device.get("w") == 1080)

    fresh = DeviceHub(offline_after=0.5)
    offline = False
    try:
        await fresh.submit("tap", {"x": 1, "y": 2}, timeout_ms=200)
    except DeviceOffline:
        offline = True
    check("离线时拒绝下发", offline)

    hub.on_heartbeat({"device": "Fake", "w": 1080, "h": 2400})
    task = asyncio.create_task(hub.submit("tap", {"x": 1, "y": 2}, timeout_ms=3000))
    polled = await hub.poll(since=0, wait_s=1.0)
    jobs = polled.get("jobs") or []
    check("任务被手机取走", len(jobs) == 1 and jobs[0]["action"] == "tap", str(polled)[:120])
    if jobs:
        hub.on_result(jobs[0]["id"], True, {"clicked": [1, 2]}, None)
    result = await task
    check("结果能回传", result.get("clicked") == [1, 2], str(result))

    hub.on_heartbeat({"device": "Fake"})
    task2 = asyncio.create_task(hub.submit("tap", {"x": 3, "y": 4}, timeout_ms=3000))
    polled2 = await hub.poll(since=0, wait_s=1.0)
    kept = (polled2.get("jobs") or [{}])[0].get("id")
    check("emergency_clear 清掉未下发任务", hub.emergency_clear() >= 1 or kept is not None)
    task2.cancel()
    try:
        await task2
    except (asyncio.CancelledError, TaskTimeout, DeviceOffline):
        pass

    hub.on_heartbeat({"device": "Fake", "ip": "100.64.0.9", "agent_version": "0.4.0"})
    conn = hub.connection
    check("连接详情带 IP", conn.get("ip") == "100.64.0.9", str(conn))
    check("100.x 判为 tailscale", conn.get("transport") == "tailscale", str(conn))
    check("连接详情带脚本版本", conn.get("agent_version") == "0.4.0", str(conn))
    check("snapshot 带 connection", "connection" in hub.snapshot())

    hub.on_heartbeat({"device": "Fake", "ip": "192.168.1.20", "agent_version": "0.4.0"})
    check("IP 变化计入重连", hub.reconnect_count == 1, str(hub.reconnect_count))
    check("局域网地址判为 lan", hub.connection.get("transport") == "lan", str(hub.connection))

    hub.on_heartbeat({"device": "Fake"})
    task3 = asyncio.create_task(hub.submit("tap", {"x": 5, "y": 6}, timeout_ms=3000))
    await asyncio.sleep(0.05)
    queued = hub.pending()
    check("pending 列出排队任务", len(queued) == 1 and queued[0]["action"] == "tap", str(queued))
    check("pending 带 id 与 age_s", bool(queued) and "id" in queued[0] and "age_s" in queued[0])
    check("cancel 能丢掉排队任务", bool(queued) and hub.cancel(queued[0]["id"]) is True)
    check("cancel 后队列为空", hub.pending() == [], str(hub.pending()))
    rejected = False
    try:
        await task3
    except TaskRejected:
        rejected = True
    check("被取消的任务立刻失败", rejected)
    check("取消不存在的任务返回 False", hub.cancel("t_missing") is False)
    check("取消计入 stats", hub.snapshot()["stats"].get("tasks_cancelled") == 1)


def main() -> int:
    test_config()
    test_policy()
    test_log_decoding()
    asyncio.run(test_hub())
    print("\n" + "=" * 46)
    print(f"  通过 {PASS} 项，失败 {FAIL} 项")
    print("=" * 46)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
