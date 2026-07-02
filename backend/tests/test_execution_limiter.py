import unittest

from backend.execution_limiter import ExecutionLimiter


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


if __name__ == "__main__":
    unittest.main()
