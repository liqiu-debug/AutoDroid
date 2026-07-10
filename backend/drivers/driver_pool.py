"""执行驱动连接池：按 (platform, device_id) 复用驱动实例，降低连接建立开销。

- Android：省去每次执行的 u2.connect + info RPC
- iOS：省去 WDA Client/会话重建（成本最高，也最易受益）

默认关闭，设置环境变量 ``AUTODROID_DRIVER_POOL=1`` 启用（团队服务器推荐开启）。
复用前执行 driver.health_check() 快速探测，失效即销毁重建；空闲超过 TTL 的
条目在下次 acquire 时惰性回收。同一设备的并发获取通过条目锁串行化，锁等待
超时则降级为创建一次性驱动（release 时直接断开），不会阻塞执行。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

from .base_driver import BaseDriver

logger = logging.getLogger(__name__)

DRIVER_POOL_ENV = "AUTODROID_DRIVER_POOL"
# 空闲回收阈值（秒）与同设备锁等待上限（秒）
DRIVER_POOL_IDLE_TTL_SECONDS = 600.0
DRIVER_POOL_LOCK_TIMEOUT_SECONDS = 5.0

_TRUE_VALUES = {"1", "true", "yes", "on", "enabled"}


def is_driver_pool_enabled() -> bool:
    raw = (os.environ.get(DRIVER_POOL_ENV) or "").strip().lower()
    return raw in _TRUE_VALUES


class _PoolEntry:
    def __init__(self, key: str, driver: BaseDriver, kwargs_sig: str) -> None:
        self.key = key
        self.driver = driver
        self.kwargs_sig = kwargs_sig
        self.lock = threading.Lock()
        self.last_used_at = time.time()

    def touch(self) -> None:
        self.last_used_at = time.time()


class ExecutionDriverPool:
    """线程安全的执行驱动池（进程内单例使用）。"""

    def __init__(
        self,
        idle_ttl_seconds: float = DRIVER_POOL_IDLE_TTL_SECONDS,
        lock_timeout_seconds: float = DRIVER_POOL_LOCK_TIMEOUT_SECONDS,
    ) -> None:
        self._entries: Dict[str, _PoolEntry] = {}
        self._lock = threading.Lock()
        self._idle_ttl_seconds = idle_ttl_seconds
        self._lock_timeout_seconds = lock_timeout_seconds

    @staticmethod
    def _key(platform: str, device_id: str) -> str:
        return f"{str(platform or '').strip().lower()}:{str(device_id or '').strip()}"

    @staticmethod
    def _kwargs_sig(driver_kwargs: Dict[str, Any]) -> str:
        return repr(sorted(driver_kwargs.items()))

    def _create_driver(self, platform: str, device_id: str, **driver_kwargs: Any) -> BaseDriver:
        # 延迟导入避免与 cross_platform_runner 的模块级互相依赖
        from .cross_platform_runner import DriverFactory

        return DriverFactory.create(platform, device_id, **driver_kwargs)

    def acquire(self, platform: str, device_id: str, **driver_kwargs: Any) -> BaseDriver:
        key = self._key(platform, device_id)
        sig = self._kwargs_sig(driver_kwargs)

        stale: List[_PoolEntry] = []
        with self._lock:
            stale = self._collect_stale_locked(exclude_key=key)
            entry = self._entries.get(key)
            if entry and entry.kwargs_sig != sig:
                # 连接参数变化（如 WDA URL），旧驱动作废
                self._entries.pop(key, None)
                if not entry.lock.locked():
                    stale.append(entry)
                entry = None
        for item in stale:
            self._close_driver(item.driver)

        if entry is not None:
            if not entry.lock.acquire(timeout=self._lock_timeout_seconds):
                # 设备驱动被其他执行占用：降级为一次性驱动，避免阻塞
                logger.warning("驱动池条目占用超时，创建一次性驱动: %s", key)
                return self._create_driver(platform, device_id, **driver_kwargs)
            if entry.driver.health_check():
                entry.touch()
                logger.info("复用池化驱动: %s", key)
                return entry.driver
            # 健康检查失败：销毁并走新建路径
            logger.info("池化驱动健康检查未通过，重建: %s", key)
            with self._lock:
                if self._entries.get(key) is entry:
                    self._entries.pop(key, None)
            entry.lock.release()
            self._close_driver(entry.driver)

        driver = self._create_driver(platform, device_id, **driver_kwargs)
        new_entry = _PoolEntry(key, driver, sig)
        new_entry.lock.acquire()
        with self._lock:
            current = self._entries.get(key)
            if current is None:
                self._entries[key] = new_entry
            else:
                # 竞争下已有他人放入条目：本驱动作为一次性驱动使用
                logger.info("驱动池条目竞争，返回一次性驱动: %s", key)
        return driver

    def release(self, platform: str, device_id: str, driver: Optional[BaseDriver]) -> None:
        if driver is None:
            return
        key = self._key(platform, device_id)
        with self._lock:
            entry = self._entries.get(key)
        if entry is not None and entry.driver is driver:
            entry.touch()
            if entry.lock.locked():
                try:
                    entry.lock.release()
                except RuntimeError:
                    pass
            return
        # 一次性驱动（锁超时/竞争产生）：直接断开
        self._close_driver(driver)

    def invalidate(self, platform: str, device_id: str, driver: Optional[BaseDriver] = None) -> None:
        key = self._key(platform, device_id)
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return
            if driver is not None and entry.driver is not driver:
                return
            self._entries.pop(key, None)
        if entry.lock.locked():
            try:
                entry.lock.release()
            except RuntimeError:
                pass
        self._close_driver(entry.driver)

    def close_all(self) -> None:
        with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
        for entry in entries:
            if entry.lock.locked():
                try:
                    entry.lock.release()
                except RuntimeError:
                    pass
            self._close_driver(entry.driver)

    def _collect_stale_locked(self, exclude_key: str = "") -> List[_PoolEntry]:
        now = time.time()
        stale: List[_PoolEntry] = []
        for key, entry in list(self._entries.items()):
            if key == exclude_key:
                continue
            if entry.lock.locked():
                continue
            if (now - float(entry.last_used_at or 0.0)) < self._idle_ttl_seconds:
                continue
            stale.append(self._entries.pop(key))
        return stale

    @staticmethod
    def _close_driver(driver: BaseDriver) -> None:
        try:
            driver.disconnect()
        except Exception:
            logger.exception("关闭池化驱动失败: %s", driver)


_global_pool: Optional[ExecutionDriverPool] = None
_pool_lock = threading.Lock()


def get_execution_driver_pool() -> ExecutionDriverPool:
    global _global_pool
    if _global_pool is None:
        with _pool_lock:
            if _global_pool is None:
                _global_pool = ExecutionDriverPool()
                logger.info("执行驱动连接池已初始化")
    return _global_pool


def reset_execution_driver_pool() -> None:
    """重置全局驱动池（主要用于测试）。"""
    global _global_pool
    with _pool_lock:
        pool = _global_pool
        _global_pool = None
    if pool is not None:
        pool.close_all()
