"""
并发执行限流服务（带 FIFO 等待队列）

防止系统过载，控制用例和场景的并发执行数量：
- 用户级限流：每个用户最多同时执行 N 个任务
- 全局级限流：系统总并发不超过 M 个任务
- 设备级限流：每个设备同一时间只能执行一个任务

并发超限时任务可进入 FIFO 等待队列（enqueue + ExecutionTicket.wait），
先到先执行；不排队的调用方仍可用 acquire_lease(timeout=...) 快速失败。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from contextlib import contextmanager
from typing import Any, Deque, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


class QueueTimeoutError(RuntimeError):
    """排队等待执行槽位超时。"""


class QueueAbortedError(RuntimeError):
    """排队等待期间任务被取消/中止。"""


# 等待者状态机：WAITING -> GRANTED -> CLAIMED，或 WAITING/GRANTED -> CANCELLED
_WAITING = "WAITING"
_GRANTED = "GRANTED"
_CLAIMED = "CLAIMED"
_CANCELLED = "CANCELLED"


class _Waiter:
    """FIFO 队列中的一个等待项。"""

    __slots__ = (
        "user_id",
        "device_serial",
        "task_id",
        "kind",
        "target_id",
        "enqueued_at",
        "state",
    )

    def __init__(
        self,
        *,
        user_id: int,
        device_serial: Optional[str],
        task_id: Optional[str],
        kind: Optional[str] = None,
        target_id: Optional[int] = None,
    ):
        self.user_id = user_id
        self.device_serial = device_serial
        self.task_id = task_id
        self.kind = kind
        self.target_id = target_id
        self.enqueued_at = time.time()
        self.state = _WAITING


class ExecutionLease:
    """已获取的执行槽位，支持跨函数/线程持有并幂等释放。"""

    def __init__(
        self,
        *,
        limiter: "ExecutionLimiter",
        user_id: int,
        device_serial: Optional[str],
        task_id: Optional[str],
    ):
        self._limiter = limiter
        self.user_id = user_id
        self.device_serial = device_serial
        self.task_id = task_id
        self._released = False
        self._release_lock = threading.Lock()

    def release(self) -> None:
        """释放执行槽位。重复调用是安全的。"""
        with self._release_lock:
            if self._released:
                return
            self._released = True

        self._limiter._release_lease(self)

    def __enter__(self) -> "ExecutionLease":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


class ExecutionTicket:
    """排队凭据：enqueue 返回；立即获得槽位时 lease 非空，否则通过 wait() 阻塞等待。"""

    def __init__(
        self,
        *,
        limiter: "ExecutionLimiter",
        waiter: _Waiter,
        lease: Optional[ExecutionLease] = None,
        queue_position: int = 0,
    ):
        self._limiter = limiter
        self._waiter = waiter
        self.lease = lease
        # enqueue 时刻的排队位置（1 起始；0 表示立即获得槽位未排队）
        self.initial_queue_position = queue_position

    @property
    def task_id(self) -> Optional[str]:
        return self._waiter.task_id

    @property
    def queued(self) -> bool:
        """是否仍在排队等待（尚未拿到槽位）。"""
        return self.lease is None and self._waiter.state == _WAITING

    def queue_position(self) -> Optional[int]:
        """当前排队位置（1 起始）；未在排队返回 None。"""
        return self._limiter._waiter_position(self._waiter)

    def wait(
        self,
        timeout: Optional[float] = None,
        abort_event: Optional[threading.Event] = None,
        poll_interval: float = 0.2,
    ) -> ExecutionLease:
        """阻塞等待执行槽位。

        Args:
            timeout: 最长等待秒数；None/<=0 表示无限等待
            abort_event: 触发后立即退出队列并抛 QueueAbortedError
            poll_interval: abort_event 的轮询间隔（秒）

        Raises:
            QueueTimeoutError: 排队超时
            QueueAbortedError: 排队被取消
        """
        if self.lease is not None:
            return self.lease

        lease = self._limiter._wait_for_grant(
            self._waiter,
            timeout=timeout,
            abort_event=abort_event,
            poll_interval=poll_interval,
        )
        self.lease = lease
        return lease

    def cancel(self) -> None:
        """放弃排队/未使用的槽位。幂等。

        - 仍在排队：移出队列
        - 已授予但尚未通过 wait() 领取：直接释放槽位
        - 已领取（lease 已返回）：释放 lease
        """
        if self.lease is not None:
            self.lease.release()
            return
        self._limiter._cancel_waiter(self._waiter)


class ExecutionLimiter:
    """执行任务并发限流器（线程安全，FIFO 排队）"""

    def __init__(
        self,
        max_concurrent_per_user: int = 5,
        max_global: int = 20,
        queue_timeout: float = 1800.0,
    ):
        """
        初始化限流器

        Args:
            max_concurrent_per_user: 每个用户最大并发数
            max_global: 全局最大并发数
            queue_timeout: 排队等待槽位的默认超时（秒）
        """
        self.max_concurrent_per_user = max_concurrent_per_user
        self.max_global = max_global
        self.queue_timeout = queue_timeout

        # 单一 Condition 保护所有可变状态，保证授予顺序确定
        self._cond = threading.Condition()
        self._queue: Deque[_Waiter] = deque()

        # 占用统计（均由 _cond 保护）
        self._active_count = 0
        self._active_users: Dict[int, int] = {}
        self._device_owners: Dict[str, int] = {}  # device_serial -> user_id
        self._active_tasks: Set[str] = set()

    # ---------- 入队 / 授予 ----------

    def enqueue(
        self,
        user_id: int,
        device_serial: Optional[str] = None,
        task_id: Optional[str] = None,
        kind: Optional[str] = None,
        target_id: Optional[int] = None,
    ) -> ExecutionTicket:
        """请求执行槽位；有空位立即返回带 lease 的凭据，否则进入 FIFO 队列。

        本方法从不阻塞、从不抛并发相关异常。
        """
        waiter = _Waiter(
            user_id=user_id,
            device_serial=device_serial,
            task_id=task_id,
            kind=kind,
            target_id=target_id,
        )
        with self._cond:
            self._queue.append(waiter)
            self._grant_ready_locked()
            if waiter.state == _GRANTED:
                waiter.state = _CLAIMED
                lease = self._build_lease(waiter)
                logger.info(
                    "执行权限已获取: user_id=%s, device=%s, task_id=%s (未排队)",
                    user_id,
                    device_serial or "N/A",
                    task_id or "N/A",
                )
                return ExecutionTicket(limiter=self, waiter=waiter, lease=lease)

            position = self._waiter_position_locked(waiter) or len(self._queue)
            logger.info(
                "任务进入执行队列: user_id=%s, device=%s, task_id=%s, position=%s",
                user_id,
                device_serial or "N/A",
                task_id or "N/A",
                position,
            )
            return ExecutionTicket(
                limiter=self, waiter=waiter, lease=None, queue_position=position
            )

    def _can_admit_locked(self, waiter: _Waiter) -> bool:
        if self._active_count >= self.max_global:
            return False
        if self._active_users.get(waiter.user_id, 0) >= self.max_concurrent_per_user:
            return False
        if waiter.device_serial and waiter.device_serial in self._device_owners:
            return False
        return True

    def _grant_ready_locked(self) -> None:
        """按 FIFO 顺序授予当前可满足的等待者。

        逐个扫描队列：可满足者立即占用配额并出队；不可满足者保留原位。
        同一资源（全局/用户/设备）的竞争严格先到先得；仅当队首等待的
        资源与后续等待者无关（例如队首在等某台被占设备）时后续才会被放行。
        """
        granted_any = False
        remaining: Deque[_Waiter] = deque()
        while self._queue:
            waiter = self._queue.popleft()
            if waiter.state != _WAITING:
                continue
            if self._can_admit_locked(waiter):
                self._admit_locked(waiter)
                granted_any = True
            else:
                remaining.append(waiter)
        self._queue = remaining
        if granted_any:
            self._cond.notify_all()

    def _admit_locked(self, waiter: _Waiter) -> None:
        waiter.state = _GRANTED
        self._active_count += 1
        self._active_users[waiter.user_id] = self._active_users.get(waiter.user_id, 0) + 1
        if waiter.device_serial:
            self._device_owners[waiter.device_serial] = waiter.user_id
        if waiter.task_id:
            self._active_tasks.add(waiter.task_id)

    def _build_lease(self, waiter: _Waiter) -> ExecutionLease:
        return ExecutionLease(
            limiter=self,
            user_id=waiter.user_id,
            device_serial=waiter.device_serial,
            task_id=waiter.task_id,
        )

    def _wait_for_grant(
        self,
        waiter: _Waiter,
        *,
        timeout: Optional[float],
        abort_event: Optional[threading.Event],
        poll_interval: float,
    ) -> ExecutionLease:
        deadline = (
            time.monotonic() + timeout if timeout is not None and timeout > 0 else None
        )
        with self._cond:
            while True:
                if waiter.state == _GRANTED:
                    waiter.state = _CLAIMED
                    logger.info(
                        "排队任务已获得执行权限: user_id=%s, device=%s, task_id=%s, waited=%.1fs",
                        waiter.user_id,
                        waiter.device_serial or "N/A",
                        waiter.task_id or "N/A",
                        time.time() - waiter.enqueued_at,
                    )
                    return self._build_lease(waiter)
                if waiter.state == _CANCELLED:
                    raise QueueAbortedError("排队任务已被取消")
                if abort_event is not None and abort_event.is_set():
                    self._remove_waiter_locked(waiter)
                    raise QueueAbortedError("排队任务已被取消")

                now = time.monotonic()
                if deadline is not None and now >= deadline:
                    self._remove_waiter_locked(waiter)
                    waited = int(time.time() - waiter.enqueued_at)
                    raise QueueTimeoutError(
                        f"排队超时：等待 {waited} 秒仍未获得执行槽位，任务已退出队列"
                    )

                wait_for: Optional[float] = None
                if deadline is not None:
                    wait_for = max(0.0, deadline - now)
                if abort_event is not None:
                    wait_for = (
                        poll_interval if wait_for is None else min(wait_for, poll_interval)
                    )
                self._cond.wait(wait_for)

    def _remove_waiter_locked(self, waiter: _Waiter) -> None:
        """将等待者移出队列（或释放其已授予未领取的槽位）。"""
        if waiter.state == _WAITING:
            waiter.state = _CANCELLED
            try:
                self._queue.remove(waiter)
            except ValueError:
                pass
            return
        if waiter.state == _GRANTED:
            # 已授予但未领取：归还配额并让后续等待者补位
            waiter.state = _CANCELLED
            self._release_counts_locked(
                user_id=waiter.user_id,
                device_serial=waiter.device_serial,
                task_id=waiter.task_id,
            )
            self._grant_ready_locked()

    def _cancel_waiter(self, waiter: _Waiter) -> None:
        with self._cond:
            self._remove_waiter_locked(waiter)
            self._cond.notify_all()

    def _waiter_position_locked(self, waiter: _Waiter) -> Optional[int]:
        position = 0
        for item in self._queue:
            if item.state != _WAITING:
                continue
            position += 1
            if item is waiter:
                return position
        return None

    def _waiter_position(self, waiter: _Waiter) -> Optional[int]:
        with self._cond:
            return self._waiter_position_locked(waiter)

    def get_queue_position(self, task_id: Optional[str]) -> Optional[int]:
        """按 task_id 查询排队位置（1 起始）；不在队列返回 None。"""
        if not task_id:
            return None
        with self._cond:
            position = 0
            for item in self._queue:
                if item.state != _WAITING:
                    continue
                position += 1
                if item.task_id == task_id:
                    return position
        return None

    # ---------- 兼容的快速获取接口 ----------

    def acquire_lease(
        self,
        user_id: int,
        device_serial: Optional[str] = None,
        task_id: Optional[str] = None,
        timeout: float = 0.0,
    ) -> ExecutionLease:
        """
        获取执行权限并返回可持有的 lease（快速失败语义，保留给不排队的调用方）。

        Args:
            user_id: 用户 ID
            device_serial: 设备序列号（可选）
            task_id: 任务 ID（用于统计）
            timeout: 超时时间（秒），0 表示不等待

        Raises:
            RuntimeError: 超过并发限制且超时
        """
        ticket = self.enqueue(
            user_id=user_id,
            device_serial=device_serial,
            task_id=task_id,
        )
        if ticket.lease is not None:
            return ticket.lease

        if timeout and timeout > 0:
            try:
                return ticket.wait(timeout=timeout)
            except QueueTimeoutError:
                pass  # 转换为分层错误信息
        else:
            ticket.cancel()

        raise RuntimeError(
            self._fail_reason(user_id=user_id, device_serial=device_serial)
        )

    def _fail_reason(self, *, user_id: int, device_serial: Optional[str]) -> str:
        with self._cond:
            if self._active_count >= self.max_global:
                return f"系统并发已达上限（{self.max_global}），请稍后重试"
            if self._active_users.get(user_id, 0) >= self.max_concurrent_per_user:
                return (
                    f"您的并发任务已达上限（{self.max_concurrent_per_user}），"
                    "请等待其他任务完成"
                )
            if device_serial and device_serial in self._device_owners:
                return f"设备 {device_serial} 正在被其他任务使用，请稍后重试"
        return "执行槽位竞争中（前方仍有排队任务），请稍后重试"

    # ---------- 释放 ----------

    def _release_counts_locked(
        self,
        *,
        user_id: int,
        device_serial: Optional[str],
        task_id: Optional[str],
    ) -> None:
        if task_id:
            self._active_tasks.discard(task_id)
        if device_serial:
            self._device_owners.pop(device_serial, None)

        active_for_user = self._active_users.get(user_id, 0) - 1
        if active_for_user > 0:
            self._active_users[user_id] = active_for_user
        else:
            self._active_users.pop(user_id, None)

        self._active_count = max(0, self._active_count - 1)

    def _release_lease(self, lease: ExecutionLease) -> None:
        """释放 lease 占用的配额并唤醒队列中的等待者。"""
        with self._cond:
            self._release_counts_locked(
                user_id=lease.user_id,
                device_serial=lease.device_serial,
                task_id=lease.task_id,
            )
            self._grant_ready_locked()
            self._cond.notify_all()

        logger.debug(
            "执行权限已释放: user_id=%s, device=%s, task_id=%s",
            lease.user_id,
            lease.device_serial or "N/A",
            lease.task_id or "N/A",
        )

    @contextmanager
    def acquire(
        self,
        user_id: int,
        device_serial: Optional[str] = None,
        task_id: Optional[str] = None,
        timeout: float = 0.0,
    ):
        """
        获取执行权限（同步上下文版本）。

        Example:
            with limiter.acquire(user_id=1, device_serial="device1"):
                # 执行任务
                pass
        """
        lease = self.acquire_lease(
            user_id=user_id,
            device_serial=device_serial,
            task_id=task_id,
            timeout=timeout,
        )
        try:
            yield lease
        finally:
            lease.release()

    # ---------- 查询 ----------

    def get_stats(self) -> Dict[str, Any]:
        """
        获取当前统计信息

        Returns:
            {
                "active_tasks": int,
                "global_available": int,
                "active_users": int,
                "active_devices": list[str],
                "max_global": int,
                "max_per_user": int,
                "queue_length": int,
                "queued_tasks": list[dict],  # FIFO 顺序的排队项
            }
        """
        now = time.time()
        with self._cond:
            active_devices = list(self._device_owners.keys())
            active_users = len(self._active_users)
            active_count = self._active_count
            queued_tasks: List[Dict[str, Any]] = []
            position = 0
            for waiter in self._queue:
                if waiter.state != _WAITING:
                    continue
                position += 1
                queued_tasks.append(
                    {
                        "position": position,
                        "task_id": waiter.task_id,
                        "user_id": waiter.user_id,
                        "device_serial": waiter.device_serial,
                        "kind": waiter.kind,
                        "target_id": waiter.target_id,
                        "waited_seconds": round(max(0.0, now - waiter.enqueued_at), 1),
                    }
                )

        global_available = self.max_global - active_count

        return {
            "active_tasks": active_count,
            "global_available": max(0, global_available),
            "active_users": active_users,
            "active_devices": active_devices,
            "max_global": self.max_global,
            "max_per_user": self.max_concurrent_per_user,
            "queue_length": len(queued_tasks),
            "queued_tasks": queued_tasks,
        }

    def is_device_busy(self, device_serial: str) -> bool:
        """检查设备是否正在被使用"""
        with self._cond:
            return device_serial in self._device_owners

    def get_device_owner(self, device_serial: str) -> Optional[int]:
        """获取当前占用设备的用户 ID"""
        with self._cond:
            return self._device_owners.get(device_serial)


# 全局单例
_global_limiter: Optional[ExecutionLimiter] = None
_limiter_lock = threading.Lock()

# 默认限流参数（可通过环境变量覆盖）
DEFAULT_MAX_PER_USER = 5
DEFAULT_MAX_GLOBAL = 20
DEFAULT_QUEUE_TIMEOUT = 1800.0  # 排队等待上限：30 分钟


def _limit_from_env(env_name: str, default: int) -> int:
    """从环境变量读取限流参数，非法值回退默认并告警。"""
    raw = (os.environ.get(env_name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
        if value <= 0:
            raise ValueError(raw)
        return value
    except ValueError:
        logger.warning("环境变量 %s=%r 非法，回退默认值 %s", env_name, raw, default)
        return default


def _timeout_from_env(env_name: str, default: float) -> float:
    """从环境变量读取超时参数（秒），非法值回退默认并告警。"""
    raw = (os.environ.get(env_name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
        if value <= 0:
            raise ValueError(raw)
        return value
    except ValueError:
        logger.warning("环境变量 %s=%r 非法，回退默认值 %s", env_name, raw, default)
        return default


def get_execution_limiter() -> ExecutionLimiter:
    """
    获取全局限流器实例（单例模式）

    限流参数支持环境变量覆盖：
    - AUTODROID_LIMIT_PER_USER: 每用户最大并发（默认 5）
    - AUTODROID_LIMIT_GLOBAL: 全局最大并发（默认 20）
    - AUTODROID_QUEUE_TIMEOUT: 排队等待上限秒数（默认 1800）

    Returns:
        ExecutionLimiter 实例
    """
    global _global_limiter
    if _global_limiter is None:
        with _limiter_lock:
            if _global_limiter is None:
                max_per_user = _limit_from_env("AUTODROID_LIMIT_PER_USER", DEFAULT_MAX_PER_USER)
                max_global = _limit_from_env("AUTODROID_LIMIT_GLOBAL", DEFAULT_MAX_GLOBAL)
                queue_timeout = _timeout_from_env(
                    "AUTODROID_QUEUE_TIMEOUT", DEFAULT_QUEUE_TIMEOUT
                )
                _global_limiter = ExecutionLimiter(
                    max_concurrent_per_user=max_per_user,
                    max_global=max_global,
                    queue_timeout=queue_timeout,
                )
                logger.info(
                    "执行限流器已初始化: max_per_user=%s, max_global=%s, queue_timeout=%ss",
                    max_per_user,
                    max_global,
                    queue_timeout,
                )
    return _global_limiter


def reset_execution_limiter() -> None:
    """重置全局限流器（主要用于测试）"""
    global _global_limiter
    with _limiter_lock:
        _global_limiter = None
    logger.info("执行限流器已重置")
