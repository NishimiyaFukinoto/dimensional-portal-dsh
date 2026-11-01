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
import logging.handlers
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

    port = int(cfg.get("listen_port") or 8766)
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


def _force_utf8_stdio() -> None:
    """把 stdout/stderr 固定成 UTF-8。

    Windows 上 `python daemon.py > log.txt` 默认按 GBK 写，控制台读日志就会乱码；
    这里显式改成 UTF-8，重定向到文件后也是干净的。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass


LOG_FILE = Path(os.environ.get("DP_LOG_FILE") or (ROOT / "tmp" / "daemon.log"))


def _setup_logging(level: str) -> None:
    """同时打到屏幕和文件。

    文件用 UTF-8 显式写（不依赖 shell 重定向的编码），并做 2 MB × 3 的轮转，
    这样控制台「最近日志」永远能读到干净的文本。
    """
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    root.addHandler(stream)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        rotating = logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        rotating.setFormatter(fmt)
        root.addHandler(rotating)
    except OSError as exc:  # 目录只读之类，不影响主流程
        root.warning("日志文件打不开（只打屏幕）：%s", exc)


def main() -> int:
    _force_utf8_stdio()
    _setup_logging(os.environ.get("DP_LOG_LEVEL", "INFO").upper())
    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
