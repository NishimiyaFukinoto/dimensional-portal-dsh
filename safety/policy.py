"""安全策略：限流、包名管控、敏感界面判定、审计日志。

DSH 版没有 QQ，也就没有「QQ 号白名单」——能不能用这些工具，取决于
能不能在这台机器的 DSH 会话里说话。所以这里只管三件事：动作频率、
包名黑名单、敏感界面（截图与写操作），外加把每次动作写进审计日志。

只依赖标准库，方便单测。
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

log = logging.getLogger("dimensional_portal")

AUDIT_MAX_BYTES = 5 * 1024 * 1024  # 超过就轮转，避免审计日志无限涨
AUDIT_BACKUPS = 1                  # 只留 1 份旧日志（.1）

# 默认敏感包名：这些应用里默认不截图、也不让点/输入（微信/支付宝/银行/密码管理器等）。
DEFAULT_SENSITIVE_PACKAGES = (
    "com.tencent.mm",            # 微信
    "com.tencent.mobileqq",      # QQ 本体（聊天内容）
    "com.eg.android.AlipayGphone",
    "com.icbc",
    "com.chinamworld.main",
    "com.cmbchina.ccd.pluto.cmbActivity",
    "com.android.settings",      # 设置里有账号信息
    "com.google.android.apps.authenticator2",
    "com.microsoft.authenticator",
    "com.bitwarden.authenticator",
)

# 默认禁止启动/安装的包名（可被配置覆盖）
DEFAULT_PACKAGE_BLACKLIST = (
    "com.android.systemui",
    "com.android.settings.intelligence",
)


def _as_list(value: Any) -> list[str]:
    """把配置里的 "a,b,c" / ["a","b"] / None 统一成 list[str]。"""
    if value is None:
        return []
    if isinstance(value, str):
        parts = value.replace("，", ",").replace("\n", ",").split(",")
        return [p.strip() for p in parts if p.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(x).strip() for x in value if str(x).strip()]
    return [str(value).strip()]


class SafetyPolicy:
    def __init__(
        self,
        rate_limit: int = 20,
        rate_window_s: float = 60.0,
        sensitive_packages: Any = None,
        package_blacklist: Any = None,
        app_mode: str = "blocklist",
        app_blocked: Any = None,
        app_allowed: Any = None,
        audit_path: str | Path | None = None,
    ) -> None:
        self.rate_limit = max(1, int(rate_limit))
        self.rate_window_s = float(rate_window_s)
        self.sensitive_packages = set(_as_list(sensitive_packages)) or set(
            DEFAULT_SENSITIVE_PACKAGES
        )
        self.package_blacklist = set(_as_list(package_blacklist)) or set(
            DEFAULT_PACKAGE_BLACKLIST
        )
        # 「应用访问」：blocklist = 默认都行、只禁个别的；allowlist = 只放行白名单。
        self.app_mode = "allowlist" if str(app_mode or "").strip().lower() == "allowlist" else "blocklist"
        self.app_blocked = set(_as_list(app_blocked))
        self.app_allowed = set(_as_list(app_allowed))

        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self.audit_path = Path(audit_path) if audit_path else None
        if self.audit_path:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ 限流

    def rate_check(self, key: str) -> tuple[bool, str]:
        now = time.monotonic()
        bucket = self._hits[key]
        while bucket and now - bucket[0] > self.rate_window_s:
            bucket.popleft()
        if not bucket:
            # 空 deque 不留在 defaultdict 里，否则每个用过的用户 ID 都会常驻内存。
            self._hits.pop(key, None)
            bucket = self._hits[key]
        if len(bucket) >= self.rate_limit:
            wait = self.rate_window_s - (now - bucket[0])
            return False, f"触发限流（{self.rate_limit} 次/{int(self.rate_window_s)}s），请 {wait:.0f}s 后再试"
        bucket.append(now)
        return True, "ok"

    # ------------------------------------------------------------ 包名

    def package_allowed(self, package: str | None) -> tuple[bool, str]:
        pkg = (package or "").strip()
        if not pkg:
            return True, "ok"
        if pkg in self.package_blacklist:
            return False, f"包名 {pkg} 在禁止列表里"
        return True, "ok"

    def app_access(self, package: str | None) -> tuple[bool, str]:
        """人定的「这个应用能不能被碰」。空包名一律放行（桌面、系统界面）。"""
        pkg = (package or "").strip()
        if not pkg:
            return True, "ok"
        if self.app_mode == "allowlist":
            if pkg in self.app_allowed:
                return True, "ok"
            return False, f"现在是「只允许白名单」模式，{pkg} 不在允许名单里"
        if pkg in self.app_blocked:
            return False, f"{pkg} 被设为「禁止访问」"
        return True, "ok"

    def is_sensitive_package(self, package: str | None) -> bool:
        return bool(package) and str(package).strip() in self.sensitive_packages

    def is_sensitive_screen(self, current_package: str | None = None, ui_text: str = "") -> tuple[bool, str]:
        """判定当前界面是否敏感：包名命中黑名单，或者界面文本里出现密码框字样。"""
        if self.is_sensitive_package(current_package):
            return True, f"当前应用 {current_package} 属于敏感应用"
        lowered = (ui_text or "").lower()
        for word in ("支付密码", "请输入密码", "验证码", "password", "passcode", "银行卡号"):
            if word.lower() in lowered:
                return True, f"界面疑似含敏感字段（{word}）"
        return False, "ok"

    # ------------------------------------------------------------ 审计

    def audit(self, kind: str, **fields: Any) -> None:
        """追加一行 jsonl。写失败不能影响主流程。"""
        if not self.audit_path:
            return
        record = {"ts": round(time.time(), 3), "kind": kind, **fields}
        try:
            self._rotate_audit()
            with self.audit_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            # 审计写失败不该打断工具调用，但也不能完全静默——排查时要能看见。
            log.warning("[dimensional-portal] 审计日志写入失败（%s）：%s", self.audit_path, exc)

    def _rotate_audit(self) -> None:
        """审计日志超过阈值就把当前文件改成 .1（只保留 1 份备份）。"""
        try:
            if self.audit_path is None or not self.audit_path.exists():
                return
            if self.audit_path.stat().st_size < AUDIT_MAX_BYTES:
                return
        except OSError:
            return
        for idx in range(AUDIT_BACKUPS, 0, -1):
            src = self.audit_path if idx == 1 else Path(f"{self.audit_path}.{idx - 1}")
            dst = Path(f"{self.audit_path}.{idx}")
            try:
                if src.exists():
                    dst.unlink(missing_ok=True)
                    src.replace(dst)
            except OSError as exc:
                log.warning("[dimensional-portal] 审计日志轮转失败（%s -> %s）：%s", src, dst, exc)
