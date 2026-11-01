#!/usr/bin/env python
"""次元门 DSH 版 · 常驻 daemon。

跑法（建议用实例 venv 的 python，它已经有 aiohttp）：

    <venv>/Scripts/python.exe tools/dp_daemon.py

它会读仓库根目录的 dp.json（不存在就生成一份），把 HTTP 服务起在 dp.json 里
的 listen_host:listen_port，然后一直等手机来长轮询。

手机那端只要把脚本里的 BASES 指向本机地址、TOKEN 抄 dp.json 里的 token。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dp.config import config_path, load, public_view  # noqa: E402
from dp.service import PhoneService  # noqa: E402

LOG = logging.getLogger("dimensional_portal")


def _local_ips() -> list[str]:
    """猜本机可被手机访问到的 IPv4（不含 127.*）。"""
    ips: set[str] = set()
    try:
        host = socket.gethostname()
        for info in socket.getaddrinfo(host, None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    # 再补一个「默认出口网卡」的地址（UDP connect 不会真的发包）
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))
        ips.add(probe.getsockname()[0])
        probe.close()
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))


async def _run() -> int:
    cfg = load()
    LOG.info("配置：%s", json.dumps(public_view(cfg), ensure_ascii=False))

    service = PhoneService(cfg)
    if not await service.start():
        LOG.error("启动失败：检查 dp.json 里的 token 与 listen_port 是否可用")
        return 2

    port = int(cfg.get("listen_port") or 8765)
    host = str(cfg.get("listen_host") or "0.0.0.0")
    ips = _local_ips()

    print("", flush=True)
    print("=" * 62, flush=True)
    print(" 次元门 · DSH 版已启动", flush=True)
    print("=" * 62, flush=True)
    print(f" 监听       {host}:{port}", flush=True)
    for ip in ips or ["<本机IP>"]:
        print(f" 手机连这个 http://{ip}:{port}", flush=True)
    token = str(cfg.get("token") or "")
    print(f" token      {token}", flush=True)
    print(" 手机脚本   BASES 填上面地址，TOKEN 填上面这串", flush=True)
    admin = str(cfg.get("admin_token") or "")
    if admin and admin != "off":
        for ip in ips or ["127.0.0.1"]:
            print(f" 控制台     http://{ip}:{port}/console?k={admin}", flush=True)
    else:
        print(" 控制台     已关闭（dp.json 的 admin_token = off）", flush=True)
    print(f" dp.json    {config_path()}", flush=True)
    print(" 停止       Ctrl+C", flush=True)
    print("=" * 62, flush=True)

    stop = asyncio.Event()

    def _ask_stop(*_args: object) -> None:
        LOG.info("收到退出信号，正在停止…")
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _ask_stop)
        except (NotImplementedError, RuntimeError, ValueError):
            signal.signal(sig, lambda *_: _ask_stop())

    try:
        await stop.wait()
    finally:
        await service.stop()
        LOG.info("已停止")
    return 0


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("DP_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
