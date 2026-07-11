import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from backend.fastbot.framestats import (
    FRAMESTATS_MIN_SDK_INT,
    VSYNC_PERIOD_NS,
    FrameStatsSample,
    FramestatsMonitorState,
    _classify_jank_sample,
    _compute_framestats_sample,
    _detect_vsync_period,
    _find_closest_perf_sample,
    _parse_framestats_output,
)
from backend.fastbot.perf_monitor import (
    JANK_ACTIVE_FRAME_THRESHOLD,
    _parse_gfxinfo_output,
)
from backend.fastbot.perfetto import (
    JANK_MAX_TRACE_EXPORTS,
    PerfettoSessionState,
    _analyze_exported_traces,
    _build_trace_artifact,
    _build_perfetto_trace_config,
    _detect_perfetto_support,
    _export_perfetto_trace,
    _should_export_perfetto_trace,
)
from backend.fastbot.summary import (
    _compute_jank_summary,
    _resolve_jank_monitoring_mode,
)
from backend.paths import project_path


def _temp_path(*parts: str) -> str:
    return str(Path(tempfile.gettempdir()).joinpath(*parts))


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
            str(project_path("reports", "fastbot", "38", "continuous_trace_001.perfetto-trace")),
            PerfettoSessionState(report_dir=_temp_path("report"), capture_mode="continuous", frame_timeline_supported=True),
            trigger_time="10:00:00",
            trigger_reason="TASK_COMPLETED",
        )

        self.assertEqual(artifact["capture_mode"], "continuous")
        self.assertEqual(artifact["trigger_reason"], "TASK_COMPLETED")

    def test_should_export_perfetto_trace_honors_cooldown_and_limit(self):
        now = datetime(2026, 3, 12, 21, 0, 0)
        state = PerfettoSessionState(report_dir=tempfile.gettempdir(), available=True)

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
        state = PerfettoSessionState(report_dir=tempfile.gettempdir(), available=True)

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
            report_dir=_temp_path("fastbot-report"),
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
            "backend.fastbot.perfetto._start_perfetto_ring_buffer",
            new=AsyncMock(side_effect=start_side_effect),
        ), patch(
            "backend.fastbot.perfetto._stop_perfetto_ring_buffer",
            new=AsyncMock(side_effect=stop_side_effect),
        ), patch(
            "backend.fastbot.perfetto._check_remote_file",
            new=AsyncMock(return_value=True),
        ), patch(
            "backend.fastbot.perfetto._adb_pull",
            new=AsyncMock(),
        ), patch(
            "backend.fastbot.perfetto._cleanup_perfetto_remote_files",
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
            "backend.fastbot.perfetto._adb_shell",
            side_effect=["31", "/system/bin/perfetto"],
        ):
            state = await _detect_perfetto_support("emulator-5554", _temp_path("fastbot-report"))

        self.assertTrue(state.available)
        self.assertTrue(state.frame_timeline_supported)
        self.assertEqual(state.sdk_int, 31)

    async def test_detect_perfetto_support_disables_old_sdk(self):
        with patch(
            "backend.fastbot.perfetto._adb_shell",
            side_effect=["28", "/system/bin/perfetto"],
        ):
            state = await _detect_perfetto_support("emulator-5554", _temp_path("fastbot-report"))

        self.assertFalse(state.available)
        self.assertFalse(state.frame_timeline_supported)


SAMPLE_FRAMESTATS_OUTPUT = """Applications Graphics Acceleration Info:
Uptime: 12345678 Realtime: 12345678

** Graphics info for pid 1234 [com.example.app] **

Stats since: 645122399803ns
Total frames rendered: 300
Janky frames: 10 (3.33%)

---PROFILEDATA---
Flags,IntendedVsync,Vsync,OldestInputEvent,NewestInputEvent,HandleInputStart,AnimationStart,PerformTraversalsStart,DrawStart,SyncQueued,SyncStart,IssueDrawCommandsStart,SwapBuffers,FrameCompleted,DeadlineNs
0,1000000000,1000000000,1000000000,1000000000,1001000000,1002000000,1003000000,1005000000,1008000000,1009000000,1010000000,1012000000,1015000000,1016666667
0,1016666667,1016666667,1016666667,1016666667,1017666667,1018666667,1019666667,1021666667,1024666667,1025666667,1026666667,1028666667,1031666667,1033333334
0,1033333334,1033333334,1033333334,1033333334,1034333334,1035333334,1036333334,1038333334,1041333334,1042333334,1043333334,1045333334,1048333334,1050000001
1,1050000001,1050000001,1050000001,1050000001,1051000001,1052000001,1053000001,1055000001,1058000001,1059000001,1060000001,1062000001,1065000001,1066666668
0,1066666668,1066666668,1066666668,1066666668,1067666668,1068666668,1069666668,1071666668,1074666668,1075666668,1076666668,1078666668,1081666668,1083333335
0,1083333335,1083333335,1083333335,1083333335,1084333335,1085333335,1086333335,1088333335,1091333335,1092333335,1093333335,1095333335,1098333335,1100000002
---PROFILEDATA---
"""

SAMPLE_FRAMESTATS_FROZEN = """---PROFILEDATA---
Flags,IntendedVsync,Vsync,OldestInputEvent,NewestInputEvent,HandleInputStart,AnimationStart,PerformTraversalsStart,DrawStart,SyncQueued,SyncStart,IssueDrawCommandsStart,SwapBuffers,FrameCompleted,DeadlineNs
0,2000000000,2000000000,2000000000,2000000000,2001000000,2002000000,2003000000,2005000000,2008000000,2009000000,2010000000,2012000000,2750000000,2016666667
---PROFILEDATA---
"""

SAMPLE_FRAMESTATS_SDK31 = """---PROFILEDATA---
Flags,FrameTimelineVsyncId,IntendedVsync,Vsync,InputEventId,HandleInputStart,AnimationStart,PerformTraversalsStart,DrawStart,FrameDeadline,FrameStartTime,FrameInterval,WorkloadTarget,SyncQueued,SyncStart,IssueDrawCommandsStart,SwapBuffers,FrameCompleted,DequeueBufferDuration,QueueBufferDuration,GpuCompleted,SwapBuffersCompleted,DisplayPresentTime,CommandSubmissionCompleted,
0,92298737,28505515984415,28505515984415,0,28505517424727,28505517424727,28505517424727,28505518055831,28505527095526,28505515984415,11111111,5555555,28505519123456,28505519200000,28505519500000,28505520000000,28505521000000,100000,50000,28505521500000,28505520100000,28505522000000,28505520200000,
0,92298738,28505527095526,28505527095526,0,28505528000000,28505528000000,28505528000000,28505529000000,28505538206637,28505527095526,11111111,5555555,28505530000000,28505530100000,28505530500000,28505531000000,28505532000000,100000,50000,28505532500000,28505531100000,28505533000000,28505531200000,
0,92298739,28505538206637,28505538206637,0,28505539000000,28505539000000,28505539000000,28505540000000,28505549317748,28505538206637,11111111,5555555,28505541000000,28505541100000,28505541500000,28505542000000,28505543000000,100000,50000,28505543500000,28505542100000,28505544000000,28505542200000,
1,92298740,28505549317748,28505549317748,0,28505550000000,28505550000000,28505550000000,28505551000000,28505560428859,28505549317748,11111111,5555555,28505552000000,28505552100000,28505552500000,28505553000000,28505554000000,100000,50000,28505554500000,28505553100000,28505555000000,28505553200000,
0,92298741,28505560428859,28505560428859,0,28505561000000,28505561000000,28505561000000,28505562000000,28505571539970,28505560428859,11111111,5555555,28505563000000,28505563100000,28505563500000,28505564000000,28505565000000,100000,50000,28505565500000,28505564100000,28505566000000,28505564200000,
---PROFILEDATA---
"""


class FramestatsParsingTests(unittest.TestCase):
    def test_parse_framestats_output_extracts_per_frame_data(self):
        frames = _parse_framestats_output(SAMPLE_FRAMESTATS_OUTPUT)
        self.assertEqual(len(frames), 5)
        self.assertEqual(frames[0].intended_vsync_ns, 1000000000)
        self.assertEqual(frames[0].frame_completed_ns, 1015000000)
        self.assertEqual(frames[0].deadline_ns, 1016666667)

    def test_parse_framestats_skips_flagged_frames(self):
        frames = _parse_framestats_output(SAMPLE_FRAMESTATS_OUTPUT)
        intended_vsyncs = [f.intended_vsync_ns for f in frames]
        self.assertNotIn(1050000001, intended_vsyncs)

    def test_parse_framestats_empty_input(self):
        self.assertEqual(_parse_framestats_output(""), [])
        self.assertEqual(_parse_framestats_output("No process found"), [])

    def test_parse_framestats_sdk31_format(self):
        frames = _parse_framestats_output(SAMPLE_FRAMESTATS_SDK31)
        self.assertEqual(len(frames), 4)
        self.assertEqual(frames[0].intended_vsync_ns, 28505515984415)
        self.assertEqual(frames[0].frame_completed_ns, 28505521000000)
        self.assertEqual(frames[0].deadline_ns, 28505527095526)
        self.assertAlmostEqual(frames[0].total_duration_ms, 5.016, delta=0.1)
        self.assertFalse(frames[0].is_jank)

    def test_parse_framestats_sdk31_skips_flagged(self):
        frames = _parse_framestats_output(SAMPLE_FRAMESTATS_SDK31)
        intended_vsyncs = [f.intended_vsync_ns for f in frames]
        self.assertNotIn(28505549317748, intended_vsyncs)

    def test_parse_framestats_sdk31_trailing_comma(self):
        """SDK 31+ 格式的 header 和数据行末尾有逗号，解析器应正确处理。"""
        frames = _parse_framestats_output(SAMPLE_FRAMESTATS_SDK31)
        self.assertTrue(len(frames) > 0)
        for frame in frames:
            self.assertGreater(frame.intended_vsync_ns, 0)
            self.assertGreater(frame.frame_completed_ns, frame.intended_vsync_ns)

    def test_frame_stats_sample_properties(self):
        frame = FrameStatsSample(
            intended_vsync_ns=1000000000,
            vsync_ns=1000000000,
            draw_start_ns=1005000000,
            sync_start_ns=1009000000,
            issue_draw_commands_start_ns=1010000000,
            swap_buffers_ns=1012000000,
            frame_completed_ns=1015000000,
            deadline_ns=1016666667,
        )
        self.assertAlmostEqual(frame.total_duration_ms, 15.0, places=1)
        self.assertFalse(frame.missed_deadline)
        self.assertFalse(frame.is_jank)
        self.assertFalse(frame.is_frozen)

    def test_frame_stats_sample_jank_detection(self):
        frame = FrameStatsSample(
            intended_vsync_ns=1000000000,
            vsync_ns=1000000000,
            draw_start_ns=1005000000,
            sync_start_ns=1009000000,
            issue_draw_commands_start_ns=1010000000,
            swap_buffers_ns=1012000000,
            frame_completed_ns=1040000000,
            deadline_ns=1016666667,
        )
        self.assertAlmostEqual(frame.total_duration_ms, 40.0, places=1)
        self.assertTrue(frame.missed_deadline)
        self.assertTrue(frame.is_jank)
        self.assertFalse(frame.is_frozen)

    def test_frame_stats_sample_frozen_detection(self):
        frames = _parse_framestats_output(SAMPLE_FRAMESTATS_FROZEN)
        self.assertEqual(len(frames), 1)
        self.assertTrue(frames[0].is_frozen)
        self.assertAlmostEqual(frames[0].total_duration_ms, 750.0, places=0)


class FramestatsDeduplicationTests(unittest.TestCase):
    def test_deduplication_filters_already_seen(self):
        frames = _parse_framestats_output(SAMPLE_FRAMESTATS_OUTPUT)
        state = FramestatsMonitorState()
        state.last_seen_vsync_ns = frames[2].intended_vsync_ns

        new_frames = [f for f in frames if f.intended_vsync_ns > state.last_seen_vsync_ns]
        self.assertEqual(len(new_frames), 2)
        self.assertEqual(new_frames[0].intended_vsync_ns, 1066666668)

    def test_deduplication_all_seen(self):
        frames = _parse_framestats_output(SAMPLE_FRAMESTATS_OUTPUT)
        state = FramestatsMonitorState()
        state.last_seen_vsync_ns = frames[-1].intended_vsync_ns

        new_frames = [f for f in frames if f.intended_vsync_ns > state.last_seen_vsync_ns]
        self.assertEqual(len(new_frames), 0)


class FramestatsComputeTests(unittest.TestCase):
    def test_compute_framestats_real_fps(self):
        frames = _parse_framestats_output(SAMPLE_FRAMESTATS_OUTPUT)
        sample = _compute_framestats_sample(frames, timestamp="12:00:00")

        self.assertIsNotNone(sample)
        self.assertEqual(sample["source"], "framestats")
        self.assertEqual(sample["total_frames"], 5)
        self.assertGreater(sample["fps"], 0)
        self.assertFalse(sample["is_idle"])

    def test_compute_framestats_detects_jank(self):
        frames = _parse_framestats_output(SAMPLE_FRAMESTATS_FROZEN)
        sample = _compute_framestats_sample(frames, timestamp="12:00:00")

        self.assertIsNotNone(sample)
        self.assertEqual(sample["frozen_frames"], 1)
        self.assertGreater(sample["frame_time_max_ms"], 700)

    def test_compute_framestats_percentiles(self):
        frames = _parse_framestats_output(SAMPLE_FRAMESTATS_OUTPUT)
        sample = _compute_framestats_sample(frames, timestamp="12:00:00")

        self.assertIn("frame_time_p50_ms", sample)
        self.assertIn("frame_time_p90_ms", sample)
        self.assertIn("frame_time_p95_ms", sample)
        self.assertIn("frame_time_p99_ms", sample)
        self.assertIn("frame_time_max_ms", sample)
        self.assertIn("frame_time_avg_ms", sample)
        self.assertGreater(sample["frame_time_p50_ms"], 0)
        self.assertGreaterEqual(sample["frame_time_p99_ms"], sample["frame_time_p50_ms"])

    def test_compute_framestats_empty_returns_none(self):
        self.assertIsNone(_compute_framestats_sample([]))

    def test_adaptive_vsync_period_60hz(self):
        frames = _parse_framestats_output(SAMPLE_FRAMESTATS_OUTPUT)
        period = _detect_vsync_period(frames)
        self.assertAlmostEqual(period / 1_000_000, 16.67, delta=1.0)

    def test_adaptive_vsync_period_too_few_frames(self):
        frames = _parse_framestats_output(SAMPLE_FRAMESTATS_OUTPUT)[:2]
        period = _detect_vsync_period(frames)
        self.assertEqual(period, VSYNC_PERIOD_NS)


class FramestatsClassifyTests(unittest.TestCase):
    def test_framestats_frozen_frame_via_max_ms(self):
        sample = {
            "source": "framestats",
            "total_frames": 10,
            "render_throughput": 50,
            "fps": 50,
            "jank_rate": 0.1,
            "frozen_frames": 0,
            "jank_frames": 1,
            "slow_frames": 0,
            "frame_deadline_missed": 0,
            "missed_vsync": 0,
            "is_idle": False,
            "frame_time_max_ms": 800,
            "frame_time_p99_ms": 800,
        }
        verdict = _classify_jank_sample(sample)
        self.assertEqual(verdict["severity"], "CRITICAL")
        self.assertEqual(verdict["reason"], "FROZEN_FRAME")
        self.assertTrue(verdict["immediate"])

    def test_framestats_high_p99_critical(self):
        sample = {
            "source": "framestats",
            "total_frames": 50,
            "render_throughput": 40,
            "fps": 40,
            "jank_rate": 0.20,
            "frozen_frames": 0,
            "jank_frames": 10,
            "slow_frames": 5,
            "frame_deadline_missed": 8,
            "missed_vsync": 0,
            "is_idle": False,
            "frame_time_max_ms": 200,
            "frame_time_p99_ms": 150,
        }
        verdict = _classify_jank_sample(sample)
        self.assertEqual(verdict["severity"], "CRITICAL")
        self.assertEqual(verdict["reason"], "HIGH_FRAME_TIME_P99")

    def test_framestats_backward_compatible_with_classify(self):
        frames = _parse_framestats_output(SAMPLE_FRAMESTATS_OUTPUT)
        sample = _compute_framestats_sample(frames, timestamp="12:00:00")
        verdict = _classify_jank_sample(sample)
        self.assertIn(verdict["severity"], [None, "WARNING", "CRITICAL"])


class FramestatsMonitoringModeTests(unittest.TestCase):
    def test_resolve_mode_framestats(self):
        mode = _resolve_jank_monitoring_mode(True, use_framestats=True)
        self.assertEqual(mode, "framestats")

    def test_resolve_mode_framestats_with_perfetto(self):
        state = PerfettoSessionState(report_dir="/tmp")
        state.available = True
        mode = _resolve_jank_monitoring_mode(True, perfetto_state=state, use_framestats=True)
        self.assertEqual(mode, "framestats+perfetto")

    def test_resolve_mode_gfxinfo_fallback(self):
        mode = _resolve_jank_monitoring_mode(True, use_framestats=False)
        self.assertEqual(mode, "gfxinfo")


if __name__ == "__main__":
    unittest.main()
