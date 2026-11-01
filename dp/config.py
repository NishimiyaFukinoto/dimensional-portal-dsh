"""dp.json 的读写：一份 JSON 配置，替代 AstrBot 的配置面板。

字段（全部可选，除 token）：

    token                     手机和电脑之间的共享密钥；必须和手机脚本里的 TOKEN 一致
    listen_host / listen_port 监听地址，默认 0.0.0.0:8766（要能被手机访问到）
                              默认用 8766 是为了避开 AstrBot 版插件占用的 8765，两个可以同时开
    local_token               本机 MCP 进程调 daemon 用的密钥；留空自动生成
    admin_token               管理控制台密钥；填 off 彻底关闭控制台；留空自动生成
    debug_token               POST /debug/submit 用的调试密钥；默认关闭
    allowed_user_ids          —— DSH 版没有 QQ，这个字段已废弃，读到会被忽略
    rate_limit_per_minute     每分钟最多下发多少动作，默认 20
    task_timeout_ms           单个动作默认超时，默认 20000
    offline_after_seconds     多久没心跳算离线，默认 40
    screenshot_quality        截图 JPEG 质量，默认 55
    max_screenshot_kb         单张截图上限，默认 1500
    allow_screenshot_sensitive 敏感界面是否允许截图，默认 false
    allow_sensitive_write     敏感界面是否允许点击/输入，默认 false
    sensitive_packages        敏感包名（覆盖默认表）
    package_blacklist         禁止操作的包名（覆盖默认表）
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = ROOT / "dp.json"

DEFAULTS: dict[str, Any] = {
    "token": "",
    "listen_host": "0.0.0.0",
    "listen_port": 8766,
    "local_token": "",
    "admin_token": "",
    "debug_token": "",
    "rate_limit_per_minute": 20,
    "task_timeout_ms": 20000,
    "offline_after_seconds": 40,
    "screenshot_quality": 55,
    "max_screenshot_kb": 1500,
    "allow_screenshot_sensitive": False,
    "allow_sensitive_write": False,
    # 应用访问：默认谁都放行，人可以在控制台里逐个禁掉，或切成只允许白名单
    "app_mode": "blocklist",
    "app_blocked": [],
    "app_allowed": [],
    # 控制台「应用访问」里的手动分类：id 固定，name 可改
    "app_categories": [
        {"id": "system", "name": "系统应用"},
        {"id": "social", "name": "社交聊天"},
        {"id": "tools", "name": "工具效率"},
        {"id": "media", "name": "影音娱乐"},
        {"id": "game", "name": "游戏"},
        {"id": "other", "name": "其他"},
    ],
    "app_category": {},
    "sensitive_packages": [],
    "package_blacklist": [],
}

# 这些字段由程序生成，不写进示例文件
GENERATED = ("token", "local_token", "admin_token")


class ConfigStore(dict[str, Any]):
    """一份普通 dict，但带 save_config()——沿用 AstrBot 那套写法也能直接落盘。"""

    def __init__(self, data: dict[str, Any], path: str | Path) -> None:
        super().__init__(data)
        self.path = Path(path)

    def save_config(self) -> Path:
        """把当前内容写回 dp.json。"""
        return save(self, self.path)

    def public_view(self) -> dict[str, Any]:
        return public_view(self)


def config_path(path: str | Path | None = None) -> Path:
    if path:
        return Path(path)
    override = os.environ.get("DP_CONFIG")
    if override:
        return Path(override)
    return DEFAULT_PATH


def load(path: str | Path | None = None, create: bool = True) -> dict[str, Any]:
    """读配置：缺文件就用默认值建一份；缺 token 就现场生成。"""
    target = config_path(path)
    cfg: dict[str, Any] = dict(DEFAULTS)
    if target.exists():
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SystemExit(f"读不了配置 {target}：{exc}") from exc
        if not isinstance(raw, dict):
            raise SystemExit(f"配置 {target} 必须是一个 JSON 对象")
        cfg.update({k: v for k, v in raw.items() if k in DEFAULTS})

    cfg.pop("allowed_user_ids", None)  # 老配置里可能还在，忽略掉

    changed = False
    if not str(cfg.get("local_token") or "").strip():
        cfg["local_token"] = secrets.token_urlsafe(24)
        changed = True
    if not str(cfg.get("token") or "").strip():
        # 手机端要和这里一致；生成一个方便直接复制到脚本里
        cfg["token"] = secrets.token_urlsafe(18)
        changed = True

    if create and (changed or not target.exists()):
        save(cfg, target)
    return ConfigStore(cfg, target)


def save(cfg: dict[str, Any], path: str | Path | None = None) -> Path:
    target = config_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    body = {k: cfg.get(k, DEFAULTS[k]) for k in DEFAULTS}
    target.write_text(
        json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return target


def public_view(cfg: dict[str, Any]) -> dict[str, Any]:
    """打日志用：把密钥遮掉。"""
    out = {}
    for key, value in cfg.items():
        text = str(value or "")
        if key in GENERATED and text:
            out[key] = text if key == "token" else f"{text[:6]}…"
        else:
            out[key] = value
    return out


__all__ = ["DEFAULTS", "ConfigStore", "config_path", "load", "save", "public_view", "ROOT"]
