import unittest

from backend.fastbot_runner import _compute_summary


class FastbotRunnerSummaryTests(unittest.TestCase):
    def test_summary_keeps_crash_counts_without_performance_samples(self):
        crash_events = [
            {"type": "CRASH", "time": "10:00:00"},
            {"type": "ANR", "time": "10:01:00"},
        ]

        summary = _compute_summary(
            [],
            crash_events,
            enable_performance_monitor=False,
            enable_jank_frame_monitor=True,
        )

        self.assertEqual(summary["avg_cpu"], 0)
        self.assertEqual(summary["avg_mem"], 0)
        self.assertEqual(summary["total_crashes"], 1)
        self.assertEqual(summary["total_anrs"], 1)
        self.assertFalse(summary["performance_monitor_enabled"])
        self.assertTrue(summary["jank_frame_monitor_enabled"])

    def test_summary_calculates_performance_metrics_when_samples_exist(self):
        perf_data = [
            {"cpu": 10.0, "mem": 100.0},
            {"cpu": 30.0, "mem": 140.0},
        ]
        crash_events = [{"type": "CRASH", "time": "10:00:00"}]

        summary = _compute_summary(perf_data, crash_events)

        self.assertEqual(summary["avg_cpu"], 20.0)
        self.assertEqual(summary["max_cpu"], 30.0)
        self.assertEqual(summary["avg_mem"], 120.0)
        self.assertEqual(summary["max_mem"], 140.0)
        self.assertEqual(summary["total_crashes"], 1)
        self.assertEqual(summary["total_anrs"], 0)
        self.assertTrue(summary["performance_monitor_enabled"])
        self.assertFalse(summary["jank_frame_monitor_enabled"])


if __name__ == "__main__":
    unittest.main()
