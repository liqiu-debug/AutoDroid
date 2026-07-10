import asyncio
import threading
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import WebSocketDisconnect
from sqlmodel import Session, SQLModel, create_engine

from backend import main
from backend.models import Device, TestCase
from backend.run_control import ABORTED_STATUS


class _DisconnectingWebSocket:
    """模拟建立后立即断开的客户端：receive_text 首次调用即抛 WebSocketDisconnect。"""

    def __init__(self) -> None:
        self.send_json = AsyncMock()

    async def receive_text(self):
        raise WebSocketDisconnect(code=1001)


class _FakeLegacyRunner:
    def __init__(self, device_serial=None):
        self.device_serial = device_serial
        self.d = object()

    def connect(self):
        return None

    def execute_step(self, step, variables):
        return {
            "success": True,
            "duration": 0.01,
        }


class CaseWebSocketDisconnectAbortTests(unittest.IsolatedAsyncioTestCase):
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

    async def _run_case_with_disconnected_client(self, *, disconnect_abort_enabled: bool):
        websocket = _DisconnectingWebSocket()
        abort_event = threading.Event()
        call_names = []

        async def fake_run_in_blocking_executor(executor, func, *args, **kwargs):
            call_names.append(getattr(func, "__name__", func.__class__.__name__))
            # 让出事件循环，确保 disconnect watcher 有机会感知断开
            await asyncio.sleep(0)
            return func(*args, **kwargs)

        def fake_flag(session, key, default=False):
            if key == main.FLAG_WS_DISCONNECT_ABORT:
                return disconnect_abort_enabled
            return False  # 其余 flag 关闭，走 legacy 执行路径

        with patch.object(main, "engine", self.engine), \
             patch.object(main, "is_flag_enabled", side_effect=fake_flag), \
             patch.object(main, "TestRunner", _FakeLegacyRunner), \
             patch.object(main, "register_device_abort", return_value=abort_event), \
             patch.object(main, "unregister_device_abort"), \
             patch.object(main, "restore_device_status_after_execution"), \
             patch.object(main, "_run_in_blocking_executor", side_effect=fake_run_in_blocking_executor), \
             patch.object(main.report_generator, "generate_report", return_value="report-1"), \
             patch.object(main.manager, "connect", new=AsyncMock()), \
             patch.object(main.manager, "broadcast_run_start", new=AsyncMock()), \
             patch.object(main.manager, "broadcast_step_update", new=AsyncMock()), \
             patch.object(main.manager, "broadcast_run_complete", new=AsyncMock()), \
             patch.object(main.manager, "disconnect"):
            await main.websocket_run_case(
                websocket,
                self.case_id,
                env_id=None,
                device_serial="android-1",
            )

        return abort_event, call_names

    async def test_disconnect_aborts_run_when_flag_enabled(self):
        abort_event, call_names = await self._run_case_with_disconnected_client(
            disconnect_abort_enabled=True,
        )

        self.assertTrue(abort_event.is_set())
        self.assertNotIn("execute_step", call_names)

        with Session(self.engine) as session:
            case = session.get(TestCase, self.case_id)
            self.assertEqual(case.last_run_status, ABORTED_STATUS)

    async def test_disconnect_keeps_running_when_flag_disabled(self):
        abort_event, call_names = await self._run_case_with_disconnected_client(
            disconnect_abort_enabled=False,
        )

        self.assertFalse(abort_event.is_set())
        self.assertIn("execute_step", call_names)

        with Session(self.engine) as session:
            case = session.get(TestCase, self.case_id)
            self.assertNotEqual(case.last_run_status, ABORTED_STATUS)


if __name__ == "__main__":
    unittest.main()
