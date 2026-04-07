import unittest

from pydantic import ValidationError

from backend.schemas import FastbotTaskCreate


class FastbotTaskCreateSchemaTests(unittest.TestCase):
    def test_monitor_flags_are_required(self):
        with self.assertRaises(ValidationError):
            FastbotTaskCreate(
                package_name="com.example.app",
                duration=600,
                throttle=500,
                ignore_crashes=False,
                capture_log=True,
                device_serial="device-1",
            )

    def test_monitor_flags_accept_explicit_false(self):
        payload = FastbotTaskCreate(
            package_name="com.example.app",
            duration=600,
            throttle=500,
            enable_performance_monitor=False,
            enable_jank_frame_monitor=False,
            ignore_crashes=False,
            capture_log=True,
            device_serial="device-1",
        )

        self.assertFalse(payload.enable_performance_monitor)
        self.assertFalse(payload.enable_jank_frame_monitor)

    def test_local_replay_defaults_to_enabled(self):
        payload = FastbotTaskCreate(
            package_name="com.example.app",
            duration=600,
            throttle=500,
            enable_performance_monitor=True,
            enable_jank_frame_monitor=False,
            ignore_crashes=False,
            capture_log=True,
            device_serial="device-1",
        )

        self.assertTrue(payload.enable_local_replay)


if __name__ == "__main__":
    unittest.main()
