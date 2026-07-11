import unittest
from unittest.mock import AsyncMock, patch

from fastapi import BackgroundTasks, HTTPException
from sqlmodel import Session, SQLModel, create_engine

from backend.api import ws_run
from backend.api import cases as cases_api
from backend import scenario_execution
from backend.models import (
    ScenarioStep,
    TestCase,
    TestExecution,
    TestScenario,
    User,
)


class _FakeWebSocket:
    def __init__(self) -> None:
        self.send_json = AsyncMock()

    async def receive_text(self):
        import asyncio

        await asyncio.Event().wait()


class CaseRunDeviceRequiredTests(unittest.TestCase):
    """跨端 Runner 默认开启后，未指定设备的执行请求应返回明确错误而非静默回落 legacy。"""

    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        case = TestCase(name="case-1", steps=[], variables=[])
        self.session.add(case)
        self.session.commit()
        self.session.refresh(case)
        self.case_id = case.id
        self.user = User(id=1, username="runner", hashed_password="x")

    def tearDown(self) -> None:
        self.session.close()

    def test_run_case_without_device_returns_400(self):
        with self.assertRaises(HTTPException) as ctx:
            cases_api.run_test_case(
                case_id=self.case_id,
                background_tasks=BackgroundTasks(),
                env_id=None,
                device_serial=None,
                session=self.session,
                current_user=self.user,
            )

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("请选择执行设备", str(ctx.exception.detail))

    def test_run_case_batch_without_device_returns_400(self):
        with self.assertRaises(HTTPException) as ctx:
            cases_api.run_test_case_batch(
                case_id=self.case_id,
                request=cases_api.CaseRunBatchRequest(env_id=None, device_serials=[]),
                session=self.session,
                current_user=self.user,
            )

        self.assertEqual(ctx.exception.status_code, 400)
        items = ctx.exception.detail.get("items") or []
        self.assertTrue(any("请选择执行设备" in str(item.get("reason")) for item in items))


class CaseWebSocketDeviceRequiredTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as session:
            case = TestCase(name="case-ws", steps=[], variables=[])
            session.add(case)
            session.commit()
            session.refresh(case)
            self.case_id = case.id

    async def test_websocket_run_case_without_device_sends_error(self):
        websocket = _FakeWebSocket()

        with patch.object(ws_run, "engine", self.engine), \
             patch.object(ws_run.manager, "connect", new=AsyncMock()), \
             patch.object(ws_run.manager, "broadcast_run_start", new=AsyncMock()) as run_start_mock, \
             patch.object(ws_run.manager, "disconnect"):
            await ws_run.websocket_run_case(
                websocket,
                self.case_id,
                env_id=None,
                device_serial=None,
            )

        websocket.send_json.assert_awaited_once_with(
            {"type": "error", "message": "请选择执行设备"}
        )
        run_start_mock.assert_not_awaited()

        with Session(self.engine) as session:
            case = session.get(TestCase, self.case_id)
            # 不应把用例置为 RUNNING 后再中断。
            self.assertNotEqual(str(case.last_run_status or "").upper(), "RUNNING")


class ScenarioDeviceRequiredTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as session:
            case = TestCase(name="case-1", steps=[], variables=[])
            session.add(case)
            session.commit()
            session.refresh(case)

            scenario = TestScenario(name="scenario-1")
            session.add(scenario)
            session.commit()
            session.refresh(scenario)

            session.add(ScenarioStep(scenario_id=scenario.id, case_id=case.id, order=1))
            session.commit()

            execution = TestExecution(
                scenario_id=scenario.id,
                scenario_name=scenario.name,
                status="PENDING",
                executor_name="tester",
            )
            session.add(execution)
            session.commit()
            session.refresh(execution)

            self.scenario_id = scenario.id
            self.execution_id = execution.id

    def test_run_single_device_sync_without_device_marks_execution_error(self):
        with patch.object(scenario_execution, "engine", self.engine), \
             self.assertLogs("backend.scenario_execution", level="ERROR") as logs:
            scenario_execution._run_single_device_sync(
                execution_id=self.execution_id,
                scenario_id=self.scenario_id,
                device_serial=None,
                env_id=None,
            )

        self.assertIn("requires device_serial", "\n".join(logs.output))
        with Session(self.engine) as session:
            execution = session.get(TestExecution, self.execution_id)
            self.assertEqual(execution.status, "ERROR")
            self.assertIsNotNone(execution.end_time)


if __name__ == "__main__":
    unittest.main()
