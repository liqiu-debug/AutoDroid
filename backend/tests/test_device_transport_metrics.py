import unittest

from backend.device_transport_metrics import DeviceTransportMetrics


class DeviceTransportMetricsTests(unittest.TestCase):
    def test_snapshot_aggregates_latency_failures_and_media_counters(self):
        metrics = DeviceTransportMetrics()
        metrics.record("device-1", "screencap", 0.010, byte_count=100)
        metrics.record("device-1", "screencap", 0.030, success=False, timed_out=True)
        metrics.record("device-1", "video", byte_count=2048, dropped=2, queue_depth=5)

        snapshot = metrics.snapshot("device-1")

        self.assertEqual(snapshot["serial"], "device-1")
        self.assertEqual(snapshot["operations"]["screencap"]["count"], 2)
        self.assertEqual(snapshot["operations"]["screencap"]["failures"], 1)
        self.assertEqual(snapshot["operations"]["screencap"]["timeouts"], 1)
        self.assertEqual(snapshot["operations"]["screencap"]["bytes"], 100)
        self.assertEqual(snapshot["operations"]["video"]["dropped"], 2)
        self.assertEqual(snapshot["operations"]["video"]["bytes"], 2048)
        self.assertEqual(snapshot["operations"]["video"]["max_queue_depth"], 5)
        self.assertEqual(snapshot["operations"]["screencap"]["p50_ms"], 10.0)


if __name__ == "__main__":
    unittest.main()
