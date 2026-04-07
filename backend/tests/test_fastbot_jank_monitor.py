import asyncio
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

from backend.fastbot_runner import (
    JANK_ACTIVE_FRAME_THRESHOLD,
    JANK_MAX_TRACE_EXPORTS,
    PerfettoSessionState,
    _analyze_exported_traces,
    _build_trace_artifact,
    _build_perfetto_trace_config,
    _classify_jank_sample,
    _compute_jank_summary,
    _detect_perfetto_support,
    _export_perfetto_trace,
    _find_closest_perf_sample,
    _parse_gfxinfo_output,
    _resolve_jank_monitoring_mode,
    _should_export_perfetto_trace,
)


SAMPLE_GFXINFO_OUTPUT = """
Applications Graphics Acceleration Info:
Uptime: 781124 Realtime: 781124

** Graphics info for pid 1234 [com.example.app] **

Stats since: 645122399803ns
Total frames rendered: 251
Janky frames: 31 (12.4%)
50th percentile: 9ms
90th percentile: 21ms
95th percentile: 29ms
99th percentile: 52ms
Number Missed Vsync: 3
Number High input latency: 0
Number Slow UI thread: 4
Number Slow bitmap uploads: 1
Number Slow issue draw commands: 2
Number Frame deadline missed: 6
"""


class FastbotJankMonitorTests(unittest.TestCase):
    def test_parse_gfxinfo_output_extracts_window_metrics(self):
        sample = _parse_gfxinfo_output(
            SAMPLE_GFXINFO_OUTPUT,
            interval_sec=5,
            timestamp="10:05:10",
        )

        self.assertIsNotNone(sample)
        self.assertEqual(sample["time"], "10:05:10")
        self.assertEqual(sample["window_sec"], 5)
        self.assertEqual(sample["total_frames"], 251)
        self.assertEqual(sample["jank_frames"], 31)
        self.assertAlmostEqual(sample["jank_rate"], 0.124, places=3)
        self.assertEqual(sample["slow_frames"], 7)
        self.assertEqual(sample["frame_deadline_missed"], 6)
        self.assertEqual(sample["source"], "gfxinfo")
        self.assertEqual(sample["fps"], 50.2)
        self.assertEqual(sample["render_throughput"], 50.2)
        self.assertFalse(sample["is_idle"])

    def test_parse_gfxinfo_output_returns_none_for_missing_process(self):
        sample = _parse_gfxinfo_output("No process found for: com.example.app")
        self.assertIsNone(sample)

    def test_idle_window_does_not_trigger_low_fps_jank(self):
        sample = {
            "total_frames": JANK_ACTIVE_FRAME_THRESHOLD - 1,
            "fps": 3.2,
            "render_throughput": 3.2,
            "jank_rate": 0.0,
            "jank_frames": 0,
            "slow_frames": 0,
            "frozen_frames": 0,
            "missed_vsync": 0,
            "frame_deadline_missed": 0,
            "is_idle": True,
        }

        verdict = _classify_jank_sample(sample)

        self.assertIsNone(verdict["severity"])
        self.assertIsNone(verdict["reason"])

    def test_compute_jank_summary_aggregates_metrics(self):
        jank_data = [
            {"fps": 50.0, "render_throughput": 50.0, "jank_rate": 0.08, "is_idle": False, "time": "10:00:00", "total_frames": 250},
            {"fps": 22.0, "render_throughput": 22.0, "jank_rate": 0.24, "is_idle": False, "time": "10:00:05", "total_frames": 110},
            {"fps": 4.0, "render_throughput": 4.0, "jank_rate": 0.0, "is_idle": True, "time": "10:00:10", "total_frames": 8},
        ]
        jank_events = [
            {"severity": "WARNING"},
            {"severity": "CRITICAL"},
        ]

        summary = _compute_jank_summary(
            jank_data,
            jank_events,
            trace_artifacts=[{"path": "reports/fastbot/32/jank_trace_001.perfetto-trace"}],
            enable_jank_frame_monitor=True,
            frame_timeline_supported=True,
            jank_monitoring_mode="gfxinfo+perfetto",
        )

        self.assertEqual(summary["avg_fps"], 25.3)
        self.assertEqual(summary["min_fps"], 4.0)
        self.assertEqual(summary["avg_render_throughput"], 36.0)
        self.assertEqual(summary["min_render_throughput"], 22.0)
        self.assertAlmostEqual(summary["avg_jank_rate"], 0.1067, places=3)
        self.assertAlmostEqual(summary["active_avg_jank_rate"], 0.16, places=3)
        self.assertAlmostEqual(summary["max_jank_rate"], 0.24, places=3)
        self.assertEqual(summary["peak_jank_rate_window"]["time"], "10:00:05")
        self.assertEqual(summary["total_jank_events"], 2)
        self.assertEqual(summary["severe_jank_events"], 1)
        self.assertEqual(summary["trace_artifact_count"], 1)
        self.assertEqual(summary["analyzed_trace_count"], 0)
        self.assertTrue(summary["frame_timeline_supported"])
        self.assertEqual(summary["jank_monitoring_mode"], "gfxinfo+perfetto")
        self.assertEqual(summary["active_sample_count"], 2)

    def test_find_closest_perf_sample_matches_nearest_timestamp(self):
        perf_data = [
            {"time": "10:00:00", "cpu": 20.1, "mem": 120.0},
            {"time": "10:00:10", "cpu": 30.2, "mem": 140.0},
            {"time": "10:00:20", "cpu": 40.3, "mem": 180.0},
        ]

        nearest = _find_closest_perf_sample(perf_data, "10:00:12")

        self.assertEqual(nearest["cpu"], 30.2)
        self.assertEqual(nearest["mem"], 140.0)

    def test_build_perfetto_trace_config_includes_frametimeline_when_supported(self):
        config = _build_perfetto_trace_config(
            "com.example.app",
            frame_timeline_supported=True,
        )

        self.assertIn('name: "android.surfaceflinger.frametimeline"', config)
        self.assertIn('atrace_apps: "com.example.app"', config)

    def test_build_perfetto_trace_config_for_continuous_mode_is_lightweight(self):
        config = _build_perfetto_trace_config(
            "com.example.app",
            frame_timeline_supported=True,
            capture_mode="continuous",
        )

        self.assertIn("write_into_file: true", config)
        self.assertIn("file_write_period_ms:", config)
        self.assertIn("max_file_size_bytes:", config)
        self.assertIn('name: "android.surfaceflinger.frametimeline"', config)
        self.assertNotIn('atrace_categories: "gfx"', config)

    def test_build_trace_artifact_preserves_capture_mode(self):
        artifact = _build_trace_artifact(
            "/Users/liuzhenyu/Desktop/x/AutoDroid/reports/fastbot/38/continuous_trace_001.perfetto-trace",
            PerfettoSessionState(report_dir="/tmp/report", capture_mode="continuous", frame_timeline_supported=True),
            trigger_time="10:00:00",
            trigger_reason="TASK_COMPLETED",
        )

        self.assertEqual(artifact["capture_mode"], "continuous")
        self.assertEqual(artifact["trigger_reason"], "TASK_COMPLETED")

    def test_should_export_perfetto_trace_honors_cooldown_and_limit(self):
        now = datetime(2026, 3, 12, 21, 0, 0)
        state = PerfettoSessionState(report_dir="/tmp", available=True)

        self.assertTrue(_should_export_perfetto_trace(state, now))

        state.last_export_time = now - timedelta(seconds=10)
        self.assertFalse(_should_export_perfetto_trace(state, now))

        state.last_export_time = now - timedelta(seconds=120)
        state.export_attempts = JANK_MAX_TRACE_EXPORTS
        self.assertFalse(_should_export_perfetto_trace(state, now))

        state.export_attempts = 0
        state.capture_in_progress = True
        self.assertFalse(_should_export_perfetto_trace(state, now))

    def test_resolve_jank_monitoring_mode_reflects_perfetto_state(self):
        state = PerfettoSessionState(report_dir="/tmp", available=True)

        self.assertEqual(
            _resolve_jank_monitoring_mode(True, perfetto_state=state),
            "gfxinfo+perfetto",
        )
        self.assertEqual(_resolve_jank_monitoring_mode(True, perfetto_state=None), "gfxinfo")
        self.assertEqual(_resolve_jank_monitoring_mode(False, perfetto_state=state), "disabled")

    def test_analyze_exported_traces_updates_event_status(self):
        trace_artifacts = [
            {"path": "reports/fastbot/42/jank_trace_001.perfetto-trace", "analyzed": False},
        ]
        jank_events = [
            {
                "trace_path": "reports/fastbot/42/jank_trace_001.perfetto-trace",
                "diagnosis_status": "PENDING",
            },
        ]

        with patch(
            "backend.jank_analyzer.analyze_perfetto_trace",
            return_value={
                "status": "ANALYZED",
                "error": "",
                "analysis": {
                    "suspected_causes": [
                        {"title": "React Native 视图挂载开销偏高"},
                    ],
                },
            },
        ):
            _analyze_exported_traces("com.example.app", trace_artifacts, jank_events)

        self.assertTrue(trace_artifacts[0]["analyzed"])
        self.assertEqual(trace_artifacts[0]["analysis_status"], "ANALYZED")
        self.assertEqual(jank_events[0]["diagnosis_status"], "ANALYZED")
        self.assertEqual(jank_events[0]["diagnosis_summary"], "React Native 视图挂载开销偏高")


class FastbotPerfettoSupportTests(unittest.IsolatedAsyncioTestCase):
    async def test_export_perfetto_trace_records_diagnostic_capture(self):
        state = PerfettoSessionState(
            report_dir="/tmp/fastbot-report",
            available=True,
            capture_mode="diagnostic",
            frame_timeline_supported=True,
        )
        event = {"diagnosis_status": "EXPORT_IN_PROGRESS", "trace_exported": False, "trace_path": ""}
        trace_artifacts = []
        stop_event = asyncio.Event()
        stop_event.set()

        async def start_side_effect(device_serial, package_name, perfetto_state):
            perfetto_state.remote_config_path = "/remote/config.pbtxt"
            perfetto_state.remote_trace_path = "/remote/trace.perfetto-trace"
            perfetto_state.enabled = True
            perfetto_state.started_successfully = True
            return True

        async def stop_side_effect(device_serial, perfetto_state, preserve_trace=True):
            perfetto_state.enabled = False
            perfetto_state.session_pid = None

        with patch(
            "backend.fastbot_runner._start_perfetto_ring_buffer",
            new=AsyncMock(side_effect=start_side_effect),
        ), patch(
            "backend.fastbot_runner._stop_perfetto_ring_buffer",
            new=AsyncMock(side_effect=stop_side_effect),
        ), patch(
            "backend.fastbot_runner._check_remote_file",
            new=AsyncMock(return_value=True),
        ), patch(
            "backend.fastbot_runner._adb_pull",
            new=AsyncMock(),
        ), patch(
            "backend.fastbot_runner._cleanup_perfetto_remote_files",
            new=AsyncMock(),
        ):
            artifact = await _export_perfetto_trace(
                "emulator-5554",
                "com.example.app",
                stop_event=stop_event,
                perfetto_state=state,
                trace_artifacts=trace_artifacts,
                trigger_time="10:00:00",
                trigger_reason="HIGH_JANK_RATE",
                event=event,
                duration_sec=0,
            )

        self.assertIsNotNone(artifact)
        self.assertEqual(event["diagnosis_status"], "PENDING")
        self.assertTrue(event["trace_exported"])
        self.assertEqual(event["trace_path"], artifact["path"])
        self.assertEqual(artifact["capture_mode"], "diagnostic")
        self.assertEqual(artifact["capture_window_sec"], 0)
        self.assertEqual(len(trace_artifacts), 1)
        self.assertFalse(state.capture_in_progress)
        self.assertIsNotNone(state.last_export_time)

    async def test_detect_perfetto_support_marks_android_12_as_supported(self):
        with patch(
            "backend.fastbot_runner._adb_shell",
            side_effect=["31", "/system/bin/perfetto"],
        ):
            state = await _detect_perfetto_support("emulator-5554", "/tmp/fastbot-report")

        self.assertTrue(state.available)
        self.assertTrue(state.frame_timeline_supported)
        self.assertEqual(state.sdk_int, 31)

    async def test_detect_perfetto_support_disables_old_sdk(self):
        with patch(
            "backend.fastbot_runner._adb_shell",
            side_effect=["28", "/system/bin/perfetto"],
        ):
            state = await _detect_perfetto_support("emulator-5554", "/tmp/fastbot-report")

        self.assertFalse(state.available)
        self.assertFalse(state.frame_timeline_supported)


if __name__ == "__main__":
    unittest.main()
