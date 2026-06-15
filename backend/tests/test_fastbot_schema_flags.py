import unittest

from pydantic import ValidationError

from backend.schemas import FastbotTaskCreate, StartupRunRequest


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


class StartupRunRequestSchemaTests(unittest.TestCase):
    def test_startup_request_defaults_modes_and_thresholds(self):
        payload = StartupRunRequest(
            package_name="com.example.app",
            device_serials=["device-1"],
        )

        self.assertEqual(payload.startup_modes, ["cold", "hot"])
        self.assertEqual(payload.iterations, 3)
        self.assertTrue(payload.perfetto_slow_trace.enabled)
        self.assertEqual(payload.perfetto_slow_trace.cold_threshold_ms, 5000)
        self.assertEqual(payload.perfetto_slow_trace.hot_threshold_ms, 1500)

    def test_startup_request_deduplicates_devices(self):
        payload = StartupRunRequest(
            package_name="com.example.app",
            device_serials=["device-1", "device-1", " device-2 "],
            startup_modes=["cold", "cold", "hot"],
        )

        self.assertEqual(payload.device_serials, ["device-1", "device-2"])
        self.assertEqual(payload.startup_modes, ["cold", "hot"])

    def test_startup_request_rejects_invalid_mode(self):
        with self.assertRaises(ValidationError):
            StartupRunRequest(
                package_name="com.example.app",
                device_serials=["device-1"],
                startup_modes=["warm"],
            )


if __name__ == "__main__":
    unittest.main()
