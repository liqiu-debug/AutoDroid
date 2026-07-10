import os
import unittest
from unittest.mock import patch

from backend.execution_limiter import (
    DEFAULT_MAX_GLOBAL,
    DEFAULT_MAX_PER_USER,
    ExecutionLimiter,
    get_execution_limiter,
    reset_execution_limiter,
)


class ExecutionLimiterTests(unittest.TestCase):
    def test_acquire_lease_tracks_active_stats_until_release(self):
        limiter = ExecutionLimiter(max_concurrent_per_user=1, max_global=2)

        lease = limiter.acquire_lease(
            user_id=42,
            device_serial="device-1",
            task_id="run-1",
            timeout=0,
        )
        try:
            stats = limiter.get_stats()
            self.assertEqual(stats["active_tasks"], 1)
            self.assertEqual(stats["active_users"], 1)
            self.assertEqual(stats["active_devices"], ["device-1"])
            self.assertEqual(stats["global_available"], 1)

            with self.assertRaises(RuntimeError):
                limiter.acquire_lease(
                    user_id=42,
                    device_serial="device-2",
                    task_id="run-2",
                    timeout=0,
                )
        finally:
            lease.release()

        stats = limiter.get_stats()
        self.assertEqual(stats["active_tasks"], 0)
        self.assertEqual(stats["active_users"], 0)
        self.assertEqual(stats["active_devices"], [])
        self.assertEqual(stats["global_available"], 2)

    def test_device_lease_blocks_other_users_until_release(self):
        limiter = ExecutionLimiter(max_concurrent_per_user=2, max_global=2)
        lease = limiter.acquire_lease(
            user_id=1,
            device_serial="device-1",
            task_id="run-1",
            timeout=0,
        )

        try:
            with self.assertRaises(RuntimeError):
                limiter.acquire_lease(
                    user_id=2,
                    device_serial="device-1",
                    task_id="run-2",
                    timeout=0,
                )
        finally:
            lease.release()

        next_lease = limiter.acquire_lease(
            user_id=2,
            device_serial="device-1",
            task_id="run-2",
            timeout=0,
        )
        next_lease.release()


class ExecutionLimiterEnvConfigTests(unittest.TestCase):
    def setUp(self):
        reset_execution_limiter()
        self.addCleanup(reset_execution_limiter)

    def test_defaults_when_env_unset(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AUTODROID_LIMIT_PER_USER", None)
            os.environ.pop("AUTODROID_LIMIT_GLOBAL", None)
            limiter = get_execution_limiter()

        self.assertEqual(limiter.max_concurrent_per_user, DEFAULT_MAX_PER_USER)
        self.assertEqual(limiter.max_global, DEFAULT_MAX_GLOBAL)

    def test_env_overrides_limits(self):
        env = {"AUTODROID_LIMIT_PER_USER": "2", "AUTODROID_LIMIT_GLOBAL": "7"}
        with patch.dict(os.environ, env):
            limiter = get_execution_limiter()

        self.assertEqual(limiter.max_concurrent_per_user, 2)
        self.assertEqual(limiter.max_global, 7)

    def test_invalid_env_values_fall_back_to_defaults(self):
        env = {"AUTODROID_LIMIT_PER_USER": "abc", "AUTODROID_LIMIT_GLOBAL": "0"}
        with patch.dict(os.environ, env):
            limiter = get_execution_limiter()

        self.assertEqual(limiter.max_concurrent_per_user, DEFAULT_MAX_PER_USER)
        self.assertEqual(limiter.max_global, DEFAULT_MAX_GLOBAL)

    def test_singleton_reuses_first_instance(self):
        with patch.dict(os.environ, {"AUTODROID_LIMIT_GLOBAL": "7"}):
            first = get_execution_limiter()
        with patch.dict(os.environ, {"AUTODROID_LIMIT_GLOBAL": "9"}):
            second = get_execution_limiter()

        self.assertIs(first, second)
        self.assertEqual(second.max_global, 7)


if __name__ == "__main__":
    unittest.main()
