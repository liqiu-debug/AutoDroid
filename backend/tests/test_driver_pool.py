import threading
import unittest
from unittest.mock import patch

from backend.drivers import driver_pool as pool_module
from backend.drivers.base_driver import BaseDriver
from backend.drivers.cross_platform_runner import TestCaseRunner
from backend.drivers.driver_pool import (
    ExecutionDriverPool,
    is_driver_pool_enabled,
    reset_execution_driver_pool,
)


class _FakeDriver(BaseDriver):
    def __init__(self, device_id: str, **kwargs):
        super().__init__(device_id)
        self.kwargs = kwargs
        self.healthy = True
        self.disconnected = False

    def click(self, selector, by):
        return None

    def input(self, selector, by, text):
        return None

    def screenshot(self) -> bytes:
        return b""

    def click_by_coordinates(self, x, y):
        return None

    def health_check(self) -> bool:
        return self.healthy

    def disconnect(self) -> None:
        self.disconnected = True


class DriverPoolTests(unittest.TestCase):
    def setUp(self):
        self.pool = ExecutionDriverPool(idle_ttl_seconds=600.0, lock_timeout_seconds=0.1)
        self.created = []

        def fake_create(pool_self, platform, device_id, **kwargs):
            driver = _FakeDriver(device_id, **kwargs)
            self.created.append(driver)
            return driver

        patcher = patch.object(ExecutionDriverPool, "_create_driver", fake_create)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_acquire_release_reuses_driver(self):
        first = self.pool.acquire("android", "d1")
        self.pool.release("android", "d1", first)
        second = self.pool.acquire("android", "d1")

        self.assertIs(first, second)
        self.assertEqual(len(self.created), 1)
        self.assertFalse(first.disconnected)

    def test_unhealthy_driver_is_recreated(self):
        first = self.pool.acquire("android", "d1")
        self.pool.release("android", "d1", first)
        first.healthy = False

        second = self.pool.acquire("android", "d1")

        self.assertIsNot(first, second)
        self.assertTrue(first.disconnected)
        self.assertEqual(len(self.created), 2)

    def test_kwargs_change_recreates_driver(self):
        first = self.pool.acquire("ios", "udid-1", wda_url="http://127.0.0.1:8200")
        self.pool.release("ios", "udid-1", first)

        second = self.pool.acquire("ios", "udid-1", wda_url="http://127.0.0.1:8201")

        self.assertIsNot(first, second)
        self.assertTrue(first.disconnected)

    def test_lock_timeout_falls_back_to_standalone_driver(self):
        held = self.pool.acquire("android", "d1")  # 持锁不释放，模拟并发占用

        fallback = self.pool.acquire("android", "d1")
        self.assertIsNot(held, fallback)

        # 归还一次性驱动：直接断开，不影响池内条目
        self.pool.release("android", "d1", fallback)
        self.assertTrue(fallback.disconnected)

        self.pool.release("android", "d1", held)
        again = self.pool.acquire("android", "d1")
        self.assertIs(again, held)

    def test_idle_entries_are_evicted_on_acquire(self):
        first = self.pool.acquire("android", "d1")
        self.pool.release("android", "d1", first)
        # 手动做旧，触发 TTL 回收
        with self.pool._lock:
            self.pool._entries["android:d1"].last_used_at = 0.0

        self.pool.acquire("android", "d2")

        self.assertTrue(first.disconnected)
        with self.pool._lock:
            self.assertNotIn("android:d1", self.pool._entries)

    def test_invalidate_closes_and_removes_entry(self):
        first = self.pool.acquire("android", "d1")
        self.pool.release("android", "d1", first)

        self.pool.invalidate("android", "d1")

        self.assertTrue(first.disconnected)
        second = self.pool.acquire("android", "d1")
        self.assertIsNot(first, second)


class DriverPoolEnvFlagTests(unittest.TestCase):
    def test_disabled_by_default(self):
        with patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop(pool_module.DRIVER_POOL_ENV, None)
            self.assertFalse(is_driver_pool_enabled())

    def test_enabled_values(self):
        for raw in ("1", "true", "on", "YES"):
            with patch.dict("os.environ", {pool_module.DRIVER_POOL_ENV: raw}):
                self.assertTrue(is_driver_pool_enabled(), raw)
        with patch.dict("os.environ", {pool_module.DRIVER_POOL_ENV: "0"}):
            self.assertFalse(is_driver_pool_enabled())


class RunnerPoolIntegrationTests(unittest.TestCase):
    def setUp(self):
        reset_execution_driver_pool()
        self.addCleanup(reset_execution_driver_pool)

    def test_runner_uses_pool_when_enabled(self):
        created = []

        def fake_factory_create(platform, device_id, **kwargs):
            driver = _FakeDriver(device_id, **kwargs)
            created.append(driver)
            return driver

        with patch.dict("os.environ", {pool_module.DRIVER_POOL_ENV: "1"}), \
             patch("backend.drivers.cross_platform_runner.DriverFactory.create", side_effect=fake_factory_create):
            runner1 = TestCaseRunner(platform="android", device_id="d1", abort_event=threading.Event())
            runner1.disconnect()
            runner2 = TestCaseRunner(platform="android", device_id="d1", abort_event=threading.Event())
            runner2.disconnect()

        self.assertIs(runner1.driver, runner2.driver)
        self.assertEqual(len(created), 1)
        self.assertFalse(runner1.driver.disconnected)

    def test_runner_bypasses_pool_when_disabled(self):
        created = []

        def fake_factory_create(platform, device_id, **kwargs):
            driver = _FakeDriver(device_id, **kwargs)
            created.append(driver)
            return driver

        with patch.dict("os.environ", {pool_module.DRIVER_POOL_ENV: "0"}), \
             patch("backend.drivers.cross_platform_runner.DriverFactory.create", side_effect=fake_factory_create):
            runner1 = TestCaseRunner(platform="android", device_id="d1")
            runner1.disconnect()
            runner2 = TestCaseRunner(platform="android", device_id="d1")
            runner2.disconnect()

        self.assertEqual(len(created), 2)
        self.assertTrue(created[0].disconnected)
        self.assertTrue(created[1].disconnected)


if __name__ == "__main__":
    unittest.main()
