import json
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from backend import jank_ai_service
from backend.jank_ai_service import build_jank_ai_payload_text, summarize_jank_analysis


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or json.dumps(self._payload, ensure_ascii=False)

    def json(self):
        return self._payload


class _FakeAsyncClient:
    call_count = 0

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, json=None, headers=None):
        type(self).call_count += 1
        return _FakeResponse(
            payload={
                "choices": [
                    {
                        "message": {
                            "content": "### 现象摘要\n测试结果",
                        }
                    }
                ],
                "usage": {"total_tokens": 321},
            }
        )


class JankAiServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        jank_ai_service._jank_ai_cache.clear()
        _FakeAsyncClient.call_count = 0
        self.artifact = {
            "trigger_time": "10:00:00",
            "trigger_reason": "窗口卡顿率超阈值",
            "path": "reports/fastbot/37/jank_trace_001.perfetto-trace",
            "analysis": {
                "analysis_level": "full",
                "analysis_window_sec": 30,
                "frame_timeline_available": True,
                "frame_stats": {"jank_rate": 0.32},
                "jank_type_breakdown": [{"jank_type": f"type-{i}", "count": i} for i in range(8)],
                "suspected_causes": [{"title": f"cause-{i}"} for i in range(8)],
                "top_busy_threads": [{"thread_name": f"thread-{i}", "running_ms": i * 10} for i in range(8)],
                "thread_summaries": {"main_thread": {"running_ms": 120.5}},
                "top_jank_frames": [{"layer_name": f"layer-{i}"} for i in range(10)],
                "hot_slices": [{"slice_name": f"slice-{i}"} for i in range(10)],
            },
        }

    def test_build_jank_ai_payload_text_limits_collections(self):
        payload = json.loads(build_jank_ai_payload_text(self.artifact))

        self.assertEqual(payload["capture_mode"], None)
        self.assertEqual(payload["analysis_scope"], None)
        self.assertEqual(payload["experience_severity_hint"]["label"], "局部可感知波动")
        self.assertEqual(payload["experience_severity_hint"]["confidence"], "medium")
        self.assertEqual(len(payload["jank_type_breakdown"]), 5)
        self.assertEqual(len(payload["suspected_causes"]), 5)
        self.assertEqual(len(payload["top_busy_threads"]), 5)
        self.assertEqual(len(payload["top_jank_frames"]), 8)
        self.assertEqual(len(payload["hot_slices"]), 8)

    def test_build_jank_ai_payload_marks_high_fps_low_delay_trace_as_mild(self):
        artifact = {
            "capture_mode": "continuous",
            "analysis": {
                "analysis_level": "frame_timeline_only",
                "analysis_scope": "full_trace",
                "frame_stats": {
                    "jank_rate": 0.3121,
                    "effective_fps": 62.9,
                    "present_delay_p95_ms": 0.0,
                    "max_frame_ms": 67.85,
                },
                "top_busy_threads": [],
                "hot_slices": [],
            },
        }

        payload = json.loads(build_jank_ai_payload_text(artifact))

        self.assertEqual(payload["experience_severity_hint"]["label"], "轻微波动")
        self.assertIn(
            "FrameTimeline jank_rate 偏高，但 effective_fps 仍高且 P95 呈现延迟很低，更像节奏抖动或晚到统计，不宜直接表述为严重卡顿。",
            payload["experience_severity_hint"]["evidence_notes"],
        )

    async def test_summarize_jank_analysis_uses_cache_and_force_refresh(self):
        with patch(
            "backend.jank_ai_service._get_setting",
            side_effect=["test-key", "https://api.openai.com/v1", "gpt-4o-mini"] * 3,
        ), patch(
            "backend.jank_ai_service.httpx.AsyncClient",
            _FakeAsyncClient,
        ):
            first = await summarize_jank_analysis(
                artifact=self.artifact,
                package_name="com.example.app",
                device_info="device-1",
                session=object(),
            )
            second = await summarize_jank_analysis(
                artifact=self.artifact,
                package_name="com.example.app",
                device_info="device-1",
                session=object(),
            )
            third = await summarize_jank_analysis(
                artifact=self.artifact,
                package_name="com.example.app",
                device_info="device-1",
                session=object(),
                force_refresh=True,
            )

        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertFalse(third["cached"])
        self.assertEqual(_FakeAsyncClient.call_count, 2)
        self.assertEqual(first["analysis_result"], "### 现象摘要\n测试结果")
        self.assertEqual(third["token_usage"], 321)

    def test_ttl_cache_expires_and_evicts_old_entries(self):
        cache = jank_ai_service._TtlCache(maxsize=2, ttl=10)

        with patch("backend.jank_ai_service.time.time", return_value=100.0):
            cache.set("a", "A")
            cache.set("b", "B")
            self.assertEqual(cache.get("a"), "A")

        with patch("backend.jank_ai_service.time.time", return_value=105.0):
            cache.set("c", "C")
            self.assertIsNone(cache.get("b"))
            self.assertEqual(cache.get("a"), "A")
            self.assertEqual(cache.get("c"), "C")

        with patch("backend.jank_ai_service.time.time", return_value=111.0):
            self.assertIsNone(cache.get("a"))
            self.assertEqual(len(cache), 1)
            self.assertEqual(cache.get("c"), "C")

    async def test_summarize_jank_analysis_requires_structured_analysis(self):
        with self.assertRaises(HTTPException) as ctx:
            await summarize_jank_analysis(
                artifact={"path": "reports/fastbot/37/empty.perfetto-trace"},
                package_name="com.example.app",
                device_info="device-1",
                session=object(),
            )

        self.assertEqual(ctx.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
