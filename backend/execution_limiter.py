"""
并发执行限流服务

防止系统过载，控制用例和场景的并发执行数量：
- 用户级限流：每个用户最多同时执行 N 个任务
- 全局级限流：系统总并发不超过 M 个任务
- 设备级限流：每个设备同一时间只能执行一个任务
"""
from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, Optional, Set

logger = logging.getLogger(__name__)


class ExecutionLease:
    """已获取的执行槽位，支持跨函数/线程持有并幂等释放。"""

    def __init__(
        self,
        *,
        limiter: "ExecutionLimiter",
        user_id: int,
        device_serial: Optional[str],
        task_id: Optional[str],
        user_sem: threading.Semaphore,
        device_lock: Optional[threading.Lock],
    ):
        self._limiter = limiter
        self.user_id = user_id
        self.device_serial = device_serial
        self.task_id = task_id
        self._user_sem = user_sem
        self._device_lock = device_lock
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


class ExecutionLimiter:
    """执行任务并发限流器（线程安全）"""

    def __init__(
        self,
        max_concurrent_per_user: int = 5,
        max_global: int = 20,
    ):
        """
        初始化限流器

        Args:
            max_concurrent_per_user: 每个用户最大并发数
            max_global: 全局最大并发数
        """
        self.max_concurrent_per_user = max_concurrent_per_user
        self.max_global = max_global

        # 用户级限流
        self._user_semaphores: Dict[int, threading.Semaphore] = {}
        self._user_semaphores_lock = threading.Lock()

        # 全局限流
        self._global_semaphore = threading.Semaphore(max_global)

        # 设备级限流（每个设备同时只能有一个任务）
        self._device_locks: Dict[str, threading.Lock] = {}
        self._device_locks_lock = threading.Lock()
        self._device_owners: Dict[str, int] = {}  # device_serial -> user_id

        # 统计信息
        self._active_tasks: Set[str] = set()  # task_id 集合
        self._active_tasks_lock = threading.Lock()
        self._active_users: Dict[int, int] = {}
        self._active_users_lock = threading.Lock()
        self._active_count = 0
        self._active_count_lock = threading.Lock()

    def _get_user_semaphore(self, user_id: int) -> threading.Semaphore:
        """获取用户级信号量（懒加载）"""
        if user_id not in self._user_semaphores:
            with self._user_semaphores_lock:
                if user_id not in self._user_semaphores:
                    self._user_semaphores[user_id] = threading.Semaphore(
                        self.max_concurrent_per_user
                    )
        return self._user_semaphores[user_id]

    def _get_device_lock(self, device_serial: str) -> threading.Lock:
        """获取设备级锁（懒加载）"""
        if device_serial not in self._device_locks:
            with self._device_locks_lock:
                if device_serial not in self._device_locks:
                    self._device_locks[device_serial] = threading.Lock()
        return self._device_locks[device_serial]

    def acquire_lease(
        self,
        user_id: int,
        device_serial: Optional[str] = None,
        task_id: Optional[str] = None,
        timeout: float = 0.0,
    ) -> ExecutionLease:
        """
        获取执行权限并返回可持有的 lease。

        Args:
            user_id: 用户 ID
            device_serial: 设备序列号（可选）
            task_id: 任务 ID（用于统计）
            timeout: 超时时间（秒），0 表示不等待

        Raises:
            RuntimeError: 超过并发限制且超时
        """
        acquired_global = False
        acquired_user = False
        acquired_device = False
        user_sem: Optional[threading.Semaphore] = None
        device_lock = None

        start_time = time.time()

        try:
            # 1. 全局限流
            if not self._global_semaphore.acquire(timeout=timeout):
                raise RuntimeError(
                    f"系统并发已达上限（{self.max_global}），请稍后重试"
                )
            acquired_global = True

            # 2. 用户级限流
            user_sem = self._get_user_semaphore(user_id)
            remaining_timeout = max(0, timeout - (time.time() - start_time)) if timeout > 0 else 0
            if not user_sem.acquire(timeout=remaining_timeout):
                raise RuntimeError(
                    f"您的并发任务已达上限（{self.max_concurrent_per_user}），请等待其他任务完成"
                )
            acquired_user = True

            # 3. 设备级限流
            if device_serial:
                device_lock = self._get_device_lock(device_serial)
                remaining_timeout = max(0, timeout - (time.time() - start_time)) if timeout > 0 else 0
                if not device_lock.acquire(timeout=remaining_timeout):
                    raise RuntimeError(
                        f"设备 {device_serial} 正在被其他任务使用，请稍后重试"
                    )
                acquired_device = True

            lease = ExecutionLease(
                limiter=self,
                user_id=user_id,
                device_serial=device_serial,
                task_id=task_id,
                user_sem=user_sem,
                device_lock=device_lock,
            )

            # 4. 记录活跃任务
            with self._active_count_lock:
                self._active_count += 1
            with self._active_users_lock:
                self._active_users[user_id] = self._active_users.get(user_id, 0) + 1
            if acquired_device and device_serial:
                with self._device_locks_lock:
                    self._device_owners[device_serial] = user_id
            if task_id:
                with self._active_tasks_lock:
                    self._active_tasks.add(task_id)

            elapsed = time.time() - start_time
            logger.info(
                "执行权限已获取: user_id=%s, device=%s, task_id=%s, elapsed=%.2fs",
                user_id,
                device_serial or "N/A",
                task_id or "N/A",
                elapsed,
            )

            return lease
        except Exception:
            if acquired_device and device_lock:
                device_lock.release()
            if acquired_user and user_sem:
                user_sem.release()
            if acquired_global:
                self._global_semaphore.release()
            raise

    def _release_lease(self, lease: ExecutionLease) -> None:
        """释放 lease 占用的状态与 semaphore/lock。"""
        if lease.task_id:
            with self._active_tasks_lock:
                self._active_tasks.discard(lease.task_id)

        if lease.device_serial and lease._device_lock:
            with self._device_locks_lock:
                self._device_owners.pop(lease.device_serial, None)
            lease._device_lock.release()

        with self._active_users_lock:
            active_for_user = self._active_users.get(lease.user_id, 0) - 1
            if active_for_user > 0:
                self._active_users[lease.user_id] = active_for_user
            else:
                self._active_users.pop(lease.user_id, None)

        with self._active_count_lock:
            self._active_count = max(0, self._active_count - 1)

        lease._user_sem.release()
        self._global_semaphore.release()

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

    def get_stats(self) -> Dict[str, Any]:
        """
        获取当前统计信息

        Returns:
            {
                "active_tasks": int,
                "global_available": int,
                "active_users": int,
                "active_devices": list[str],
            }
        """
        with self._device_locks_lock:
            active_devices = list(self._device_owners.keys())

        with self._active_users_lock:
            active_users = len(self._active_users)

        with self._active_count_lock:
            active_count = self._active_count

        global_available = self.max_global - active_count

        return {
            "active_tasks": active_count,
            "global_available": max(0, global_available),
            "active_users": active_users,
            "active_devices": active_devices,
            "max_global": self.max_global,
            "max_per_user": self.max_concurrent_per_user,
        }

    def is_device_busy(self, device_serial: str) -> bool:
        """检查设备是否正在被使用"""
        with self._device_locks_lock:
            return device_serial in self._device_owners

    def get_device_owner(self, device_serial: str) -> Optional[int]:
        """获取当前占用设备的用户 ID"""
        with self._device_locks_lock:
            return self._device_owners.get(device_serial)


# 全局单例
_global_limiter: Optional[ExecutionLimiter] = None
_limiter_lock = threading.Lock()


def get_execution_limiter() -> ExecutionLimiter:
    """
    获取全局限流器实例（单例模式）

    Returns:
        ExecutionLimiter 实例
    """
    global _global_limiter
    if _global_limiter is None:
        with _limiter_lock:
            if _global_limiter is None:
                _global_limiter = ExecutionLimiter(
                    max_concurrent_per_user=5,
                    max_global=20,
                )
                logger.info("执行限流器已初始化: max_per_user=5, max_global=20")
    return _global_limiter


def reset_execution_limiter() -> None:
    """重置全局限流器（主要用于测试）"""
    global _global_limiter
    with _limiter_lock:
        _global_limiter = None
    logger.info("执行限流器已重置")
