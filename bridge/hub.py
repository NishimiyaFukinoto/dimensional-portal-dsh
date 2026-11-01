"""设备中枢：任务队列 + 长轮询唤醒 + 结果回传 + 心跳状态。

设计约束（很重要）：
- 手机在 CGNAT 后面，不能当服务端，所以永远是「手机主动来要活」。
- 只有一个手机设备（后续要多设备就按 device_id 分桶，现在先不做）。
- 所有状态都在 AstrBot 的事件循环里改，不加锁；`poll()` 会 await，但每次
  await 之后都重新读状态，避免竞态。

对外 API：
    hub.submit(action, params, timeout_ms)  -> dict   发任务并等结果
    hub.poll(since, wait_s)                -> dict   手机端长轮询
    hub.on_result(...)                     -> bool   手机回传结果
    hub.on_heartbeat(info)                 -> dict   心跳
    hub.snapshot()                         -> dict   给 phone_status 用
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .protocol import new_task_id, now_ts

try:
    # 和 server.py 一样：在 AstrBot 里用它的日志代理，脱离 AstrBot 跑测试时退回标准库。
    from astrbot.api import logger as log  # type: ignore[assignment]
except Exception:  # pragma: no cover - 只在独立测试环境走到
    log = logging.getLogger("astrbot.phone_bridge")

# 心跳间隔 10s，40s 没心跳就算掉线（容忍一次丢包 + 一次重连等待）
DEFAULT_OFFLINE_AFTER = 40.0
# 排队里的任务最多等这么久还没被取走就丢弃，避免手机重连后执行一堆陈旧操作
MAX_QUEUE_AGE = 90.0


class DeviceOffline(Exception):
    """手机没有心跳，或者掉线太久。"""


class TaskRejected(Exception):
    """服务端主动拒绝（限流、策略、包名黑名单等）。"""


class TaskTimeout(Exception):
    """任务发出去了但手机没在超时前回传结果。"""


@dataclass
class PendingTask:
    id: str
    action: str
    params: dict[str, Any]
    timeout_ms: int
    created_at: float
    future: asyncio.Future[dict[str, Any]] = field(repr=False, default=None)  # type: ignore[assignment]

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "action": self.action,
            "params": self.params,
            "timeout_ms": self.timeout_ms,
            "created_at": self.created_at,
        }


class DeviceHub:
    """单设备任务中枢。"""

    def __init__(self, offline_after: float = DEFAULT_OFFLINE_AFTER) -> None:
        self.offline_after = float(offline_after)

        self._queue: deque[PendingTask] = deque()
        self._waiters: dict[str, asyncio.Future[dict[str, Any]]] = {}
        # 每次有新任务就 set 并换一个新的 Event：等在旧 Event 上的 poll 会被叫醒，
        # 叫醒后重新看队列，不会丢唤醒。
        self._wakeup = asyncio.Event()

        self._device: dict[str, Any] = {}
        # None = 还没收到过心跳。用 0.0 当哨兵会让冷启动（monotonic 从 0 开始）误判在线。
        self._last_heartbeat: float | None = None
        self._last_poll = 0.0
        self._seq = 0

        self.stats = {
            "heartbeats": 0,
            "polls": 0,
            "tasks_queued": 0,
            "tasks_delivered": 0,
            "tasks_succeeded": 0,
            "tasks_failed": 0,
            "tasks_timeout": 0,
            "tasks_expired": 0,
        }

    # ------------------------------------------------------------ 状态

    @property
    def online(self) -> bool:
        if self._last_heartbeat is None:
            return False
        return (time.monotonic() - self._last_heartbeat) < self.offline_after

    @property
    def last_heartbeat_age(self) -> float | None:
        if self._last_heartbeat is None:
            return None
        return round(time.monotonic() - self._last_heartbeat, 1)

    @property
    def device(self) -> dict[str, Any]:
        return dict(self._device)

    @property
    def queue_size(self) -> int:
        return len(self._queue)

    def snapshot(self) -> dict[str, Any]:
        """给 phone_status 工具用的完整快照。"""
        return {
            "online": self.online,
            "heartbeat_age": self.last_heartbeat_age,
            "device": dict(self._device),
            "queue": len(self._queue),
            "stats": dict(self.stats),
        }

    # ------------------------------------------------------------ 任务

    def _wake(self) -> None:
        self._wakeup.set()
        self._wakeup = asyncio.Event()

    def _expire(self) -> int:
        """丢掉排队太久的任务，返回丢弃数量。"""
        if not self._queue:
            return 0
        now = time.monotonic()
        kept: deque[PendingTask] = deque()
        dropped = 0
        for task in self._queue:
            if now - task.created_at > MAX_QUEUE_AGE:
                fut = self._waiters.pop(task.id, None)
                if fut is not None and not fut.done():
                    fut.set_exception(TaskTimeout(f"任务 {task.id} 排队超时被丢弃"))
                dropped += 1
            else:
                kept.append(task)
        self._queue = kept
        self.stats["tasks_expired"] += dropped
        return dropped

    async def submit(
        self,
        action: str,
        params: dict[str, Any] | None = None,
        timeout_ms: int = 20000,
        require_online: bool = True,
    ) -> dict[str, Any]:
        """下发一个任务并等手机回传结果。

        返回手机回传的 data 字段（dict）；失败抛 DeviceOffline / TaskTimeout。
        """
        if require_online and not self.online:
            raise DeviceOffline("手机不在线（没有心跳）")

        self._expire()
        loop = asyncio.get_running_loop()
        task = PendingTask(
            id=new_task_id(),
            action=action,
            params=dict(params or {}),
            timeout_ms=int(timeout_ms),
            created_at=round(time.monotonic(), 3),
        )
        task.future = loop.create_future()
        self._queue.append(task)
        self._waiters[task.id] = task.future
        self.stats["tasks_queued"] += 1
        self._wake()

        # +3s 是给「手机拿到任务但执行卡住」留的余量，超时后手机可能还在跑，
        # 但这边不再等（结果回来会因为 waiter 不存在而被丢弃，日志里会记一笔）。
        try:
            result = await asyncio.wait_for(task.future, timeout_ms / 1000 + 3.0)
        except TimeoutError as exc:
            self.stats["tasks_timeout"] += 1
            raise TaskTimeout(
                f"任务 {task.id}({action}) 超时：{timeout_ms}ms 内手机没回结果；"
                "手机端可能已经开始执行了，先 phone_status / phone_see 确认真实状态再决定要不要重试"
            ) from exc
        finally:
            self._waiters.pop(task.id, None)
            if task in self._queue:  # 还没送出去就超时了
                try:
                    self._queue.remove(task)
                except ValueError:
                    pass

        if not result.get("ok", False):
            self.stats["tasks_failed"] += 1
            raise TaskRejected(str(result.get("error") or "手机端执行失败"))

        self.stats["tasks_succeeded"] += 1
        return result.get("data") or {}

    async def poll(self, since: int = 0, wait_s: float = 25.0) -> dict[str, Any]:
        """手机端长轮询：有任务立刻返回，没有就挂起最多 wait_s 秒。

        投递即出队（at-most-once）：任务一旦交给手机，服务端就不再保留，
        `since` 只作为「我收到的最后一个 server_seq」的游标回报给手机，
        **不参与回放**；断线/超时那一份任务会丢，不会重发。
        """
        self.stats["polls"] += 1
        self._last_poll = time.monotonic()
        deadline = time.monotonic() + max(0.5, float(wait_s))

        while True:
            self._expire()
            if self._queue:
                batch = list(self._queue)
                self._queue.clear()
                self._seq += 1
                self.stats["tasks_delivered"] += len(batch)
                return {
                    "ok": True,
                    "server_seq": self._seq,
                    "jobs": [t.to_wire() for t in batch],
                }

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"ok": True, "server_seq": self._seq, "jobs": [], "timeout": True}

            waiter = self._wakeup
            try:
                await asyncio.wait_for(waiter.wait(), remaining)
            except TimeoutError:
                pass

    def on_result(
        self,
        task_id: str,
        ok: bool,
        data: Any = None,
        error: str | None = None,
    ) -> bool:
        """手机回传结果。返回 False 表示这个任务已经超时/未知（结果被丢弃）。"""
        fut = self._waiters.pop(task_id, None)
        if fut is None or fut.done():
            # 超时后手机才回结果属于正常现象（手机端可能已经执行过），记一笔方便排查。
            log.warning(
                "[dimensional-portal] 迟到结果被丢弃：任务 %s（已超时或不存在，手机端可能已执行）",
                task_id,
            )
            return False
        fut.set_result({"ok": bool(ok), "data": data, "error": error})
        return True

    # ------------------------------------------------------------ 心跳

    def on_heartbeat(self, info: dict[str, Any]) -> dict[str, Any]:
        self._last_heartbeat = time.monotonic()
        self.stats["heartbeats"] += 1
        clean = {k: v for k, v in info.items() if k != "ts"}
        clean["last_seen_ts"] = now_ts()
        self._device = clean
        return {
            "ok": True,
            "server_ts": now_ts(),
            "server_seq": self._seq,
            "queue": len(self._queue),
            "poll_interval_ms": 1000,
            "protocol": 1,
        }

    def emergency_clear(self) -> int:
        """急停：清空队列并把等待中的调用方全部打断。"""
        count = len(self._queue)
        for task in list(self._queue):
            fut = self._waiters.pop(task.id, None)
            if fut is not None and not fut.done():
                fut.set_exception(TaskRejected("已被急停清空"))
        self._queue.clear()
        return count
