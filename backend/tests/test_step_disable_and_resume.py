"""步骤禁用与从此步继续执行（WS start_step_index / 导出 / 预检 / Runner）单元测试。"""
from __future__ import annotations

import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sqlmodel import Session, SQLModel, create_engine

from backend.api import ws_run
from backend.autojs_export.generator import build_script_step
from backend.cross_platform_execution import precheck_steps_for_platform, prepare_steps_for_platform
from backend.drivers.cross_platform_runner import TestCaseRunner
from backend.models import Device, TestCase
from backend.step_contract import is_step_disabled, legacy_step_to_standard, standard_step_to_legacy


class _DummyDriver:
    def __init__(self) -> None:
        self.calls = []

    def click(self, *args, **kwargs):
        self.calls.append(("click", args, kwargs))

    def back(self):
        self.calls.append(("back",))

    def disconnect(self):
        return None


class _MockWebSocket:
    def __init__(self) -> None:
        self.send_json = AsyncMock()

    async def receive_text(self):
        import asyncio

        await asyncio.sleep(10)
        return ""


class StepDisabledContractAndExecutionTests(unittest.TestCase):
    def test_step_contract_preserves_disabled_only_when_true(self):
        enabled = legacy_step_to_standard({"action": "back"}, case_id=1, order=1)
        self.assertFalse(is_step_disabled(enabled))
        self.assertNotIn("disabled", enabled["args"])
        enabled_legacy = standard_step_to_legacy(enabled)
        self.assertNotIn("disabled", enabled_legacy)

        disabled = legacy_step_to_standard(
            {"action": "click", "selector": "登录", "selector_type": "text", "disabled": True},
            case_id=1,
            order=2,
        )
        self.assertTrue(is_step_disabled(disabled))
        self.assertTrue(disabled["args"].get("disabled"))
        disabled_legacy = standard_step_to_legacy(disabled)
        self.assertTrue(disabled_legacy.get("disabled"))

    def test_precheck_and_prepare_skip_disabled_steps_without_blocking(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            steps = [
                # 缺 selector 与未映射 app_key，但已禁用，不应阻断预检或准备
                {"action": "click", "args": {"disabled": True}, "execute_on": ["android"]},
                {"action": "start_app", "args": {"disabled": True}, "execute_on": ["android"]},
                {"action": "back", "args": {}, "execute_on": ["android"]},
            ]
            checks = precheck_steps_for_platform(session, steps, platform="android")
            self.assertEqual([item["status"] for item in checks], ["SKIP", "SKIP", "PASS"])
            self.assertEqual(checks[0]["code"], "STEP_DISABLED")
            prepared = prepare_steps_for_platform(session, steps, platform="android")
            self.assertEqual(len(prepared), 3)

    def test_cross_platform_runner_skips_disabled_step_without_driver_call(self):
        driver = _DummyDriver()
        with patch("backend.drivers.cross_platform_runner.DriverFactory.create", return_value=driver):
            runner = TestCaseRunner(platform="android", device_id="serial-1")
        result = runner.run_step(
            {
                "action": "click",
                "args": {"disabled": True},
                "platform_overrides": {"android": {"selector": "登录", "by": "text"}},
                "retry_count": 2,
            }
        )
        self.assertEqual(result["status"], "SKIP")
        self.assertEqual(result["error_code"], "STEP_DISABLED")
        self.assertEqual(result["skip_reason"], "步骤已禁用")
        self.assertEqual(driver.calls, [])

    def test_autojs_generator_marks_disabled_step(self):
        script_step, _ = build_script_step(
            {
                "order": 1,
                "action": "start_app",
                "args": {"disabled": True},
                "execute_on": ["android"],
            },
            variables={},
            app_key_map={},
            platform="android",
        )
        self.assertTrue(script_step.get("disabled"))
        self.assertTrue(script_step["args"].get("disabled"))


class _RecordingRunner:
    executed_steps = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def run_step(self, step):
        type(self).executed_steps.append(step.get("action"))
        return {"status": "PASS", "error_strategy": "ABORT", "duration": 0.01, "artifacts": {}}

    def disconnect(self):
        return None


class WebSocketStartStepIndexTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        _RecordingRunner.executed_steps = []
        with Session(self.engine) as session:
            case = TestCase(
                name="multi-step-case",
                steps=[
                    {"action": "back"},
                    {"action": "home"},
                    {"action": "sleep", "value": "1"},
                ],
                variables=[],
            )
            device = Device(serial="android-1", platform="android", model="Pixel", status="IDLE")
            session.add(case)
            session.add(device)
            session.commit()
            session.refresh(case)
            self.case_id = case.id

    async def test_ws_run_respects_start_step_index(self):
        websocket = _MockWebSocket()
        step_updates = []
        run_completes = []

        async def fake_step_update(case_id, step_index, status, log, *args, **kwargs):
            step_updates.append((step_index, status, log))

        async def fake_run_complete(case_id, **kwargs):
            run_completes.append(kwargs)

        prepared = [
            {"action": "back", "args": {}, "timeout": 5, "error_strategy": "ABORT"},
            {"action": "home", "args": {}, "timeout": 5, "error_strategy": "ABORT"},
            {"action": "sleep", "args": {"seconds": 1}, "value": "1", "timeout": 5, "error_strategy": "ABORT"},
        ]

        async def fake_blocking(executor, func, *args, **kwargs):
            return func(*args, **kwargs)

        with patch.object(ws_run, "engine", self.engine), \
             patch.object(ws_run, "is_flag_enabled", return_value=False), \
             patch.object(ws_run, "resolve_device_platform", return_value="android"), \
             patch.object(ws_run, "prepare_case_steps_for_platform", return_value=(prepared, {})), \
             patch.object(ws_run, "CrossPlatformRunner", _RecordingRunner), \
             patch.object(ws_run, "register_device_abort", return_value=threading.Event()), \
             patch.object(ws_run, "unregister_device_abort"), \
             patch.object(ws_run, "restore_device_status_after_execution"), \
             patch.object(ws_run, "_run_in_blocking_executor", side_effect=fake_blocking), \
             patch.object(ws_run.report_generator, "generate_report", return_value="r-1"), \
             patch.object(ws_run.manager, "connect", new=AsyncMock()), \
             patch.object(ws_run.manager, "broadcast_run_start", new=AsyncMock()), \
             patch.object(ws_run.manager, "broadcast_step_update", side_effect=fake_step_update), \
             patch.object(ws_run.manager, "broadcast_run_complete", side_effect=fake_run_complete), \
             patch.object(ws_run.manager, "disconnect"):
            await ws_run.websocket_run_case(
                websocket,
                self.case_id,
                device_serial="android-1",
                start_step_index=2,
            )

        self.assertEqual(_RecordingRunner.executed_steps, ["sleep"])
        skipped_events = [(idx, st) for idx, st, _ in step_updates if st == "skipped"]
        self.assertEqual(skipped_events, [(0, "skipped"), (1, "skipped")])
        self.assertEqual(len(run_completes), 1)
        self.assertEqual(run_completes[0]["passed"], 1)
        self.assertEqual(run_completes[0]["failed"], 0)

    async def test_ws_run_rejects_out_of_range_start_step_index(self):
        websocket = _MockWebSocket()
        prepared = [{"action": "back", "args": {}, "timeout": 5, "error_strategy": "ABORT"}]
        with patch.object(ws_run, "engine", self.engine), \
             patch.object(ws_run, "is_flag_enabled", return_value=False), \
             patch.object(ws_run, "resolve_device_platform", return_value="android"), \
             patch.object(ws_run, "prepare_case_steps_for_platform", return_value=(prepared, {})), \
             patch.object(ws_run, "register_device_abort", return_value=threading.Event()), \
             patch.object(ws_run, "unregister_device_abort"), \
             patch.object(ws_run, "restore_device_status_after_execution"), \
             patch.object(ws_run.manager, "connect", new=AsyncMock()), \
             patch.object(ws_run.manager, "disconnect"):
            await ws_run.websocket_run_case(
                websocket,
                self.case_id,
                device_serial="android-1",
                start_step_index=3,
            )

        websocket.send_json.assert_awaited()
        self.assertIn("超出范围", websocket.send_json.call_args[0][0]["message"])


if __name__ == "__main__":
    unittest.main()
