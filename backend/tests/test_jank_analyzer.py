import os
import tempfile
import unittest
from unittest.mock import patch

from backend.jank_analyzer import (
    _build_suspected_causes,
    _summarize_frame_metrics,
    analyze_loaded_trace,
    analyze_perfetto_trace,
)


class JankAnalyzerTests(unittest.TestCase):
    def test_build_suspected_causes_flags_react_native_and_render_backpressure(self):
        causes = _build_suspected_causes(
            frame_stats={"jank_rate": 0.23},
            jank_types=[{"jank_type": "App Deadline Missed", "count": 8}],
            top_jank_frames=[{"layer_name": "TX - PopupWindow#123"}],
            top_busy_threads=[{"thread_name": "mqt_v_js", "running_ms": 512.4}],
            hot_slices=[
                {"slice_name": "MountItemDispatcher::mountViews mountItems to execute"},
                {"slice_name": "queueBuffer"},
            ],
        )

        tags = {item["tag"] for item in causes}
        self.assertIn("app_deadline_missed", tags)
        self.assertIn("react_native_js_busy", tags)
        self.assertIn("react_native_mounting", tags)
        self.assertIn("render_pipeline_backpressure", tags)
        self.assertIn("popup_window", tags)

    def test_build_suspected_causes_flags_gc_binder_webview_and_layout(self):
        causes = _build_suspected_causes(
            frame_stats={"jank_rate": 0.31},
            jank_types=[{"jank_type": "Buffer Stuffing", "count": 5}],
            top_jank_frames=[],
            top_busy_threads=[
                {"thread_name": "HeapTaskDaemon", "running_ms": 182.5},
                {"thread_name": "Binder:1234_1", "running_ms": 146.2},
                {"thread_name": "CrRendererMain", "running_ms": 221.6},
            ],
            hot_slices=[
                {"thread_name": "HeapTaskDaemon", "slice_name": "art::gc::collector::ConcurrentCopying", "total_ms": 96.3},
                {"thread_name": "Binder:1234_1", "slice_name": "binder transaction", "total_ms": 74.1},
                {"thread_name": "main", "slice_name": "sqliteExecuteForCursorWindow", "total_ms": 58.2},
                {"thread_name": "CrRendererMain", "slice_name": "AwContents::DrawGL", "total_ms": 88.4},
                {"thread_name": "main", "slice_name": "performTraversals", "total_ms": 133.7},
            ],
        )

        tags = {item["tag"] for item in causes}
        self.assertIn("gc_pressure", tags)
        self.assertIn("binder_blocking", tags)
        self.assertIn("main_thread_io", tags)
        self.assertIn("webview_rendering", tags)
        self.assertIn("layout_measure_heavy", tags)

    def test_analyze_loaded_trace_returns_structured_analysis(self):
        with patch(
            "backend.jank_analyzer._trace_time_bounds",
            return_value={
                "trace_start_ns": 1_000,
                "trace_end_ns": 31_000_000_000,
                "latest_frame_ts_ns": 30_000_000_000,
            },
        ), patch(
            "backend.jank_analyzer._query_first",
            return_value={"c": 1},
        ), patch(
            "backend.jank_analyzer._summarize_frame_metrics",
            return_value={
                "total_frames": 20,
                "jank_frames": 5,
                "jank_rate": 0.25,
                "avg_frame_ms": 18.3,
                "max_frame_ms": 42.6,
            },
        ), patch(
            "backend.jank_analyzer._build_frame_timeline_series",
            return_value=[
                {"offset_sec": 0, "effective_fps": 54.2, "jank_rate": 0.12},
            ],
        ), patch(
            "backend.jank_analyzer._query_jank_breakdown",
            return_value=[{"jank_type": "App Deadline Missed", "count": 4}],
        ), patch(
            "backend.jank_analyzer._query_top_jank_frames",
            return_value=[{"layer_name": "TX - MainActivity", "jank_type": "App Deadline Missed"}],
        ), patch(
            "backend.jank_analyzer._query_top_busy_threads",
            return_value=[{"thread_name": "RenderThread", "running_ms": 480.5}],
        ), patch(
            "backend.jank_analyzer._query_thread_summary",
            side_effect=[
                {"label": "main_thread", "thread_name": "com.example.app", "running_ms": 620.0, "top_slices": []},
                {"label": "render_thread", "thread_name": "RenderThread", "running_ms": 480.5, "top_slices": []},
            ],
        ), patch(
            "backend.jank_analyzer._query_hot_slices",
            return_value=[{"thread_name": "RenderThread", "slice_name": "queueBuffer", "total_ms": 220.0}],
        ):
            result = analyze_loaded_trace(object(), "com.example.app", window_sec=30)

        self.assertEqual(result["status"], "ANALYZED")
        self.assertEqual(result["analysis"]["analysis_level"], "full")
        self.assertEqual(result["analysis"]["analysis_scope"], "last_window_before_trace_end")
        self.assertEqual(result["analysis"]["analysis_window_sec"], 30.0)
        self.assertTrue(result["analysis"]["frame_timeline_available"])
        self.assertEqual(result["analysis"]["frame_stats"]["jank_rate"], 0.25)
        self.assertEqual(result["analysis"]["frame_timeline_series"][0]["effective_fps"], 54.2)
        self.assertEqual(result["analysis"]["top_busy_threads"][0]["thread_name"], "RenderThread")
        self.assertGreater(len(result["analysis"]["suspected_causes"]), 0)

    def test_analyze_loaded_trace_marks_frame_timeline_only_when_thread_data_missing(self):
        with patch(
            "backend.jank_analyzer._trace_time_bounds",
            return_value={
                "trace_start_ns": 1_000,
                "trace_end_ns": 31_000_000_000,
                "latest_frame_ts_ns": 30_000_000_000,
            },
        ), patch(
            "backend.jank_analyzer._table_exists",
            side_effect=lambda _tp, table: table == "actual_frame_timeline_slice",
        ), patch(
            "backend.jank_analyzer._summarize_frame_metrics",
            return_value={"total_frames": 10, "jank_rate": 0.1},
        ), patch(
            "backend.jank_analyzer._build_frame_timeline_series",
            return_value=[],
        ), patch(
            "backend.jank_analyzer._query_jank_breakdown",
            return_value=[],
        ), patch(
            "backend.jank_analyzer._query_top_jank_frames",
            return_value=[],
        ):
            result = analyze_loaded_trace(object(), "com.example.app", window_sec=30, capture_mode="continuous")

        self.assertEqual(result["analysis"]["analysis_level"], "frame_timeline_only")
        self.assertEqual(result["analysis"]["analysis_scope"], "full_trace")
        self.assertEqual(result["analysis"]["analysis_window_sec"], 31.0)

    def test_summarize_frame_metrics_uses_distinct_display_frames(self):
        actual_frames = [
            {
                "frame_key": "101",
                "frame_start_ns": 0,
                "frame_end_ns": 16_666_667,
                "max_frame_ms": 16.6,
                "is_jank": 0,
                "is_late_present": 0,
                "is_dropped": 0,
                "on_time_finish": 1,
                "present_delay_ms": -0.3,
            },
            {
                "frame_key": "102",
                "frame_start_ns": 16_666_667,
                "frame_end_ns": 33_333_334,
                "max_frame_ms": 24.0,
                "is_jank": 1,
                "is_late_present": 1,
                "is_dropped": 0,
                "on_time_finish": 0,
                "present_delay_ms": 8.0,
            },
            {
                "frame_key": "103",
                "frame_start_ns": 33_333_334,
                "frame_end_ns": 50_000_001,
                "max_frame_ms": 18.0,
                "is_jank": 1,
                "is_late_present": 0,
                "is_dropped": 1,
                "on_time_finish": 0,
                "present_delay_ms": 14.0,
            },
        ]
        expected_frames = [
            {"frame_key": "101", "expected_frame_ms": 16.667},
            {"frame_key": "102", "expected_frame_ms": 16.667},
            {"frame_key": "103", "expected_frame_ms": 16.667},
        ]

        with patch(
            "backend.jank_analyzer._query_distinct_actual_frames",
            return_value=actual_frames,
        ), patch(
            "backend.jank_analyzer._query_distinct_expected_frames",
            return_value=expected_frames,
        ), patch(
            "backend.jank_analyzer._table_exists",
            return_value=True,
        ):
            metrics = _summarize_frame_metrics(object(), "com.example.app", 0, 60_000_000)

        self.assertEqual(metrics["total_frames"], 3)
        self.assertEqual(metrics["jank_frames"], 2)
        self.assertAlmostEqual(metrics["jank_rate"], 0.6667, places=4)
        self.assertAlmostEqual(metrics["target_fps"], 60.0, places=1)
        self.assertAlmostEqual(metrics["cadence_fps"], 60.0, places=1)
        self.assertAlmostEqual(metrics["effective_fps"], 20.0, places=1)
        self.assertAlmostEqual(metrics["presented_fps"], 40.0, places=1)
        self.assertAlmostEqual(metrics["on_time_fps"], 20.0, places=1)
        self.assertAlmostEqual(metrics["late_present_ratio"], 0.3333, places=4)
        self.assertAlmostEqual(metrics["dropped_frame_ratio"], 0.3333, places=4)
        self.assertAlmostEqual(metrics["present_delay_p95_ms"], 13.4, places=1)
        self.assertAlmostEqual(metrics["actual_frame_interval_p50_ms"], 16.667, places=3)

    def test_analyze_perfetto_trace_returns_missing_for_absent_file(self):
        result = analyze_perfetto_trace("/tmp/does-not-exist.perfetto-trace", "com.example.app")

        self.assertEqual(result["status"], "TRACE_MISSING")
        self.assertIsNone(result["analysis"])

    def test_analyze_perfetto_trace_wraps_loader_errors(self):
        with tempfile.NamedTemporaryFile(suffix=".perfetto-trace", delete=False) as handle:
            path = handle.name

        try:
            with patch("backend.jank_analyzer._get_trace_processor", side_effect=RuntimeError("boom")):
                result = analyze_perfetto_trace(path, "com.example.app")
        finally:
            os.unlink(path)

        self.assertEqual(result["status"], "FAILED")
        self.assertIn("boom", result["error"])


if __name__ == "__main__":
    unittest.main()
