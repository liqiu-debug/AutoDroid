import asyncio
import threading
import unittest
from unittest.mock import AsyncMock, patch

from sqlmodel import Session, SQLModel, create_engine

from backend import main
from backend.models import Device, TestCase


class _FakeWebSocket:
    def __init__(self) -> None:
        self.send_json = AsyncMock()

    async def receive_text(self):
        # 模拟保持连接的客户端：挂起直至被 disconnect watcher 取消
        await asyncio.Event().wait()


class _FakeCrossPlatformRunner:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def run_step(self, step):
        return {
            "status": "PASS",
            "error_strategy": "ABORT",
            "duration": 0.01,
            "artifacts": {},
        }

    def disconnect(self):
        return None


class _FakeDriver:
    def screenshot(self):
        return b"fake-png"


class _FakeFailingCrossPlatformRunner:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.driver = _FakeDriver()

    def run_step(self, step):
        return {
            "status": "FAIL",
            "error": "element not found",
            "error_code": "E2001_ELEMENT_NOT_FOUND",
            "error_context": {"action": "click", "selector": "login"},
            "suggestion": "请检查定位器是否正确",
            "error_strategy": "ABORT",
            "duration": 0.01,
            "artifacts": {},
        }

    def disconnect(self):
        return None


class CaseWebSocketOffloadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)

        with Session(self.engine) as session:
            case = TestCase(
                name="case-1",
                steps=[
                    {
                        "action": "click",
                        "selector": "login",
                        "selector_type": "text",
                        "value": "",
                        "description": "点击登录",
                        "error_strategy": "ABORT",
                    }
                ],
                variables=[],
            )
            device = Device(serial="android-1", platform="android", model="Pixel 8", status="IDLE")
            session.add(case)
            session.add(device)
            session.commit()
            session.refresh(case)
            self.case_id = case.id

    async def test_websocket_run_case_cross_platform_steps_use_blocking_executor(self):
        websocket = _FakeWebSocket()
        call_names = []

        async def fake_run_in_blocking_executor(executor, func, *args, **kwargs):
            call_names.append(getattr(func, "__name__", func.__class__.__name__))
            return func(*args, **kwargs)

        with patch.object(main, "engine", self.engine), \
             patch.object(main, "resolve_device_platform", return_value="android"), \
             patch.object(main, "prepare_case_steps_for_platform", return_value=([{"action": "click", "description": "点击登录", "error_strategy": "ABORT", "timeout": 10}], {})), \
             patch.object(main, "register_device_abort", return_value=threading.Event()), \
             patch.object(main, "restore_device_status_after_execution") as restore_status, \
             patch.object(main, "unregister_device_abort") as unregister_abort, \
             patch.object(main, "CrossPlatformRunner", _FakeCrossPlatformRunner), \
             patch.object(main, "_run_in_blocking_executor", side_effect=fake_run_in_blocking_executor), \
             patch.object(main.report_generator, "generate_report", return_value="report-cross-1") as report_mock, \
             patch.object(main.manager, "connect", new=AsyncMock()) as connect_mock, \
             patch.object(main.manager, "broadcast_run_start", new=AsyncMock()) as run_start_mock, \
             patch.object(main.manager, "broadcast_step_update", new=AsyncMock()) as step_update_mock, \
             patch.object(main.manager, "broadcast_run_complete", new=AsyncMock()) as run_complete_mock, \
             patch.object(main.manager, "disconnect") as disconnect_mock:
            await main.websocket_run_case(
                websocket,
                self.case_id,
                env_id=None,
                device_serial="android-1",
            )

        self.assertIn("_FakeCrossPlatformRunner", call_names)
        self.assertIn("run_step", call_names)
        connect_mock.assert_awaited_once()
        run_start_mock.assert_awaited_once()
        step_update_mock.assert_awaited()
        run_complete_mock.assert_awaited_once()
        report_mock.assert_called_once()
        restore_status.assert_called_once()
        unregister_abort.assert_called_once_with("android-1")
        disconnect_mock.assert_called_once_with(websocket, self.case_id)
        websocket.send_json.assert_not_awaited()

    async def test_websocket_run_case_failed_step_offloads_screenshot_capture(self):
        websocket = _FakeWebSocket()
        call_names = []

        async def fake_run_in_blocking_executor(executor, func, *args, **kwargs):
            call_names.append(getattr(func, "__name__", func.__class__.__name__))
            return func(*args, **kwargs)

        with patch.object(main, "engine", self.engine), \
             patch.object(main, "resolve_device_platform", return_value="android"), \
             patch.object(main, "prepare_case_steps_for_platform", return_value=([{"action": "click", "description": "点击登录", "error_strategy": "ABORT", "timeout": 10}], {})), \
             patch.object(main, "register_device_abort", return_value=threading.Event()), \
             patch.object(main, "restore_device_status_after_execution"), \
             patch.object(main, "unregister_device_abort"), \
             patch.object(main, "CrossPlatformRunner", _FakeFailingCrossPlatformRunner), \
             patch.object(main, "_run_in_blocking_executor", side_effect=fake_run_in_blocking_executor), \
             patch.object(main.report_generator, "generate_report", return_value="report-cross-2") as report_mock, \
             patch.object(main.manager, "connect", new=AsyncMock()), \
             patch.object(main.manager, "broadcast_run_start", new=AsyncMock()), \
             patch.object(main.manager, "broadcast_step_update", new=AsyncMock()) as step_update_mock, \
             patch.object(main.manager, "broadcast_run_complete", new=AsyncMock()) as run_complete_mock, \
             patch.object(main.manager, "disconnect"):
            await main.websocket_run_case(
                websocket,
                self.case_id,
                env_id=None,
                device_serial="android-1",
            )

        # 失败截图采集也应经由 blocking executor 执行
        self.assertIn("run_step", call_names)
        self.assertIn("_capture_cross_platform_runner_screenshot", call_names)
        step_update_mock.assert_awaited()
        run_complete_mock.assert_awaited_once()
        complete_kwargs = run_complete_mock.await_args.kwargs
        self.assertFalse(complete_kwargs.get("success"))
        self.assertEqual(complete_kwargs.get("failed"), 1)

        # 失败步骤的 WS 广播携带结构化错误码/建议（纯增量字段）
        failed_call = next(
            call for call in step_update_mock.await_args_list if "failed" in call.args
        )
        self.assertEqual(failed_call.kwargs.get("error_code"), "E2001_ELEMENT_NOT_FOUND")
        self.assertEqual(failed_call.kwargs.get("suggestion"), "请检查定位器是否正确")

        # HTML 报告的步骤结果同样携带（经 report_display 渲染）
        report_steps = report_mock.call_args.kwargs["steps_results"]
        self.assertEqual(report_steps[0]["error_code"], "E2001_ELEMENT_NOT_FOUND")
        self.assertEqual(report_steps[0]["suggestion"], "请检查定位器是否正确")

        with Session(self.engine) as session:
            case = session.get(TestCase, self.case_id)
            self.assertEqual(case.last_run_status, "FAIL")


if __name__ == "__main__":
    unittest.main()
