import json
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

from backend.api.fastbot import _artifact_needs_trace_analysis, analyze_trace_ai_summary
from backend.models import FastbotReport, FastbotTask
from backend.schemas import JankAiSummaryRequest


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def first(self):
        return self._value


class _FakeSession:
    def __init__(self, report, task):
        self.report = report
        self.task = task
        self.committed = False

    def exec(self, query):
        return _FakeResult(self.report)

    def get(self, model, key):
        if model is FastbotTask and key == self.task.id:
            return self.task
        return None

    def add(self, obj):
        if isinstance(obj, FastbotReport):
            self.report = obj

    def commit(self):
        self.committed = True

    def refresh(self, obj):
        return obj

    def rollback(self):
        self.committed = False


class FastbotTraceAiApiTests(unittest.IsolatedAsyncioTestCase):
    def test_artifact_needs_trace_analysis_for_legacy_full_analysis(self):
        self.assertTrue(
            _artifact_needs_trace_analysis(
                {
                    "path": "reports/fastbot/37/jank_trace_001.perfetto-trace",
                    "analysis_status": "ANALYZED",
                    "analysis": {
                        "analysis_level": "full",
                        "frame_stats": {
                            "target_fps": 60.0,
                        },
                        "frame_timeline_series": [],
                    },
                }
            )
        )
        self.assertFalse(
            _artifact_needs_trace_analysis(
                {
                    "path": "reports/fastbot/37/jank_trace_001.perfetto-trace",
                    "analysis_status": "ANALYZED",
                    "analysis": {
                        "analysis_level": "full",
                        "frame_stats": {
                            "effective_fps": 42.5,
                        },
                        "frame_timeline_series": [
                            {"offset_sec": 0, "effective_fps": 42.5},
                        ],
                    },
                }
            )
        )

    async def test_analyze_trace_ai_summary_persists_summary_and_respects_force_refresh(self):
        report = FastbotReport(
            id=1,
            task_id=37,
            performance_data="[]",
            jank_data="[]",
            jank_events="[]",
            trace_artifacts=json.dumps([
                {
                    "path": "reports/fastbot/37/jank_trace_001.perfetto-trace",
                    "analysis_status": "ANALYZED",
                    "analysis": {
                        "analysis_level": "full",
                        "frame_stats": {
                            "effective_fps": 42.5,
                        },
                        "frame_timeline_series": [
                            {"offset_sec": 0, "effective_fps": 42.5},
                        ],
                    },
                }
            ]),
            crash_events="[]",
            summary="{}",
            created_at=datetime.now(),
        )
        task = FastbotTask(
            id=37,
            package_name="com.example.app",
            duration=600,
            throttle=500,
            ignore_crashes=False,
            capture_log=True,
            device_serial="device-1",
            status="COMPLETED",
        )
        session = _FakeSession(report, task)

        with patch(
            "backend.api.fastbot.summarize_jank_analysis",
            new=AsyncMock(
                return_value={
                    "success": True,
                    "analysis_result": "### 现象摘要\n测试结论",
                    "token_usage": 88,
                    "cached": False,
                }
            ),
        ) as summary_mock:
            response = await analyze_trace_ai_summary(
                task_id=37,
                req=JankAiSummaryRequest(
                    trace_path="reports/fastbot/37/jank_trace_001.perfetto-trace",
                    force_refresh=True,
                ),
                session=session,
            )

        self.assertTrue(response.success)
        self.assertEqual(response.token_usage, 88)
        self.assertTrue(session.committed)

        stored_artifacts = json.loads(session.report.trace_artifacts)
        self.assertEqual(stored_artifacts[0]["ai_summary"], "### 现象摘要\n测试结论")
        self.assertFalse(stored_artifacts[0]["ai_summary_cached"])

        await_args = summary_mock.await_args.kwargs
        self.assertTrue(await_args["force_refresh"])
        self.assertEqual(await_args["package_name"], "com.example.app")


if __name__ == "__main__":
    unittest.main()
