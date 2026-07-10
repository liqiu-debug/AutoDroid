import threading
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

from sqlmodel import Session, SQLModel, create_engine

from backend.api import scenarios
from backend.models import Device, ScenarioStep, TestCase, TestExecution, TestScenario


class ScenarioCrossPlatformPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)

        self.device = Device(serial="ios-1", platform="ios", model="iPhone 15", status="IDLE")
        self.session.add(self.device)

        self.case = TestCase(name="case-1", steps=[], variables=[])
        self.session.add(self.case)
        self.session.commit()
        self.session.refresh(self.case)

        self.scenario = TestScenario(name="scenario-1")
        self.session.add(self.scenario)
        self.session.commit()
        self.session.refresh(self.scenario)

        self.case_id = self.case.id
        self.scenario_id = self.scenario.id
        self.device_id = self.device.id

        self.session.add(ScenarioStep(scenario_id=self.scenario_id, case_id=self.case_id, order=1, alias="case-1"))
        self.session.commit()

        self.execution = TestExecution(
            scenario_id=self.scenario_id,
            scenario_name=self.scenario.name,
            status="PENDING",
            device_serial="ios-1",
            platform="ios",
            device_info="iPhone 15",
            executor_name="tester",
        )
        self.session.add(self.execution)
        self.session.commit()
        self.session.refresh(self.execution)
        self.session.close()

    def _fake_cross_result(self):
        return {
            "success": True,
            "results": [
                {
                    "step_order": 1,
                    "scenario_step_id": 1,
                    "alias": "case-1",
                    "case_name": "case-1",
                    "result": {
                        "case_id": self.case_id,
                        "success": True,
                        "steps": [
                            {
                                "step": {
                                    "action": "click",
                                    "selector": "登录",
                                    "selector_type": "text",
                                    "description": "点击登录",
                                },
                                "success": True,
                                "duration": 0.01,
                            }
                        ],
                        "exported_variables": {},
                    },
                }
            ],
        }

    def test_execute_cross_platform_scenario_core_updates_execution_and_scenario(self):
        fake_result = self._fake_cross_result()
        start_time = datetime.now() - timedelta(seconds=1)

        with Session(self.engine) as session, \
             patch.object(scenarios, "_run_scenario_cross_platform", return_value=fake_result) as cross_run, \
             patch("backend.report_generator.report_generator.generate_scenario_report", return_value="report-1"):
            scenario = session.get(TestScenario, self.scenario_id)
            execution = session.get(TestExecution, self.execution.id)

            summary = scenarios._execute_cross_platform_scenario_core(
                session=session,
                scenario=scenario,
                execution=execution,
                scenario_id=self.scenario_id,
                device_serial="ios-1",
                start_time=start_time,
                env_id=None,
                abort_event=threading.Event(),
                commit_per_step=True,
            )

            session.refresh(scenario)
            session.refresh(execution)

        cross_run.assert_called_once()
        self.assertEqual(summary["scenario_status"], "PASS")
        self.assertEqual(summary["report_id"], "report-1")
        self.assertEqual(summary["success_count"], 1)
        self.assertEqual(summary["fail_count"], 0)
        self.assertEqual(summary["cases_results"][0]["duration"], 0.01)
        self.assertGreater(summary["total_duration"], 0)
        self.assertEqual(execution.status, "PASS")
        self.assertEqual(execution.report_id, "report-1")
        self.assertGreater(execution.duration, 0)
        self.assertEqual(scenario.last_run_status, "PASS")
        self.assertEqual(scenario.last_execution_id, self.execution.id)
        self.assertIsNone(scenario.last_failed_step)

    def test_run_single_device_sync_executes_cross_platform_path(self):
        abort_event = threading.Event()
        fake_result = self._fake_cross_result()

        with patch.object(scenarios, "engine", self.engine), \
             patch.object(scenarios, "register_device_abort", return_value=abort_event) as register_abort, \
             patch.object(scenarios, "unregister_device_abort") as unregister_abort, \
             patch.object(scenarios, "restore_device_status_after_execution") as restore_status, \
             patch.object(scenarios, "_run_scenario_cross_platform", return_value=fake_result) as cross_run, \
             patch("backend.report_generator.report_generator.generate_scenario_report", return_value="report-1"):
            scenarios._run_single_device_sync(
                execution_id=self.execution.id,
                scenario_id=self.scenario_id,
                device_serial="ios-1",
                env_id=None,
            )

        cross_run.assert_called_once()
        register_abort.assert_called_once_with("ios-1")
        unregister_abort.assert_called_once_with("ios-1")
        restore_status.assert_called_once()

        with Session(self.engine) as verify_session:
            execution = verify_session.get(TestExecution, self.execution.id)
            self.assertEqual(execution.status, "PASS")
            self.assertEqual(execution.report_id, "report-1")
            device = verify_session.get(Device, self.device_id)
            self.assertEqual(device.status, "BUSY")

    def test_run_single_device_sync_marks_execution_error_when_scenario_missing(self):
        with Session(self.engine) as setup_session:
            missing_execution = TestExecution(
                scenario_id=99999,
                scenario_name="missing",
                status="PENDING",
                device_serial="ios-1",
                platform="ios",
                device_info="iPhone 15",
                executor_name="tester",
            )
            setup_session.add(missing_execution)
            setup_session.commit()
            setup_session.refresh(missing_execution)
            missing_execution_id = missing_execution.id

        with patch.object(scenarios, "engine", self.engine), \
             self.assertLogs("backend.api.scenarios", level="ERROR") as logs:
            scenarios._run_single_device_sync(
                execution_id=missing_execution_id,
                scenario_id=99999,
                device_serial="ios-1",
                env_id=None,
            )

        self.assertIn("scenario execution aborted: scenario not found", "\n".join(logs.output))

        with Session(self.engine) as verify_session:
            execution = verify_session.get(TestExecution, missing_execution_id)
            self.assertEqual(execution.status, "ERROR")
            self.assertIsNotNone(execution.end_time)

    def test_run_single_device_sync_marks_error_when_device_prepare_fails(self):
        with patch.object(scenarios, "engine", self.engine), \
             patch.object(
                 scenarios,
                 "_prepare_cross_platform_device_execution",
                 side_effect=RuntimeError("connect failed"),
             ), \
             patch.object(scenarios, "restore_device_status_after_execution") as restore_status, \
             patch.object(scenarios, "unregister_device_abort") as unregister_abort:
            scenarios._run_single_device_sync(
                execution_id=self.execution.id,
                scenario_id=self.scenario_id,
                device_serial="ios-1",
                env_id=None,
            )

        unregister_abort.assert_called_once_with("ios-1")
        restore_status.assert_called_once()

        with Session(self.engine) as verify_session:
            execution = verify_session.get(TestExecution, self.execution.id)
            self.assertEqual(execution.status, "ERROR")
            self.assertIsNotNone(execution.end_time)


class ScenarioCrossPlatformWebSocketTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)

        with Session(self.engine) as session:
            session.add(Device(serial="ios-1", platform="ios", model="iPhone 15", status="IDLE"))

            case = TestCase(name="case-1", steps=[], variables=[])
            session.add(case)
            session.commit()
            session.refresh(case)

            scenario = TestScenario(name="scenario-1")
            session.add(scenario)
            session.commit()
            session.refresh(scenario)

            session.add(ScenarioStep(scenario_id=scenario.id, case_id=case.id, order=1, alias="case-1"))
            session.commit()
            self.scenario_id = scenario.id
            self.case_id = case.id

    async def test_websocket_cross_platform_branch_uses_shared_core(self):
        websocket = object()
        cross_summary = {
            "raw_results": [
                {
                    "alias": "case-1",
                    "case_name": "case-1",
                    "result": {"case_id": 1, "success": True, "steps": []},
                }
            ],
            "cases_results": [
                {
                    "status": "success",
                    "duration": 0.12,
                    "steps": [{"status": "success", "action": "click"}],
                }
            ],
            "report_id": "report-1",
            "report_error": None,
            "scenario_status": "PASS",
            "summary_msg": "summary",
        }

        async def fake_run_in_blocking_executor(executor, func, *args, **kwargs):
            return func(*args, **kwargs)

        with patch.object(scenarios, "engine", self.engine), \
             patch.object(scenarios, "precheck_scenario_execution", return_value={"ok": True}), \
             patch.object(scenarios, "_run_in_blocking_executor", side_effect=fake_run_in_blocking_executor), \
             patch.object(scenarios, "_prepare_cross_platform_device_execution", return_value=threading.Event()) as prepare_device, \
             patch.object(scenarios, "_execute_cross_platform_scenario_core", return_value=cross_summary) as shared_core, \
             patch.object(scenarios, "restore_device_status_after_execution") as restore_status, \
             patch.object(scenarios, "unregister_device_abort") as unregister_abort, \
             patch.object(scenarios.manager, "connect", new=AsyncMock()) as connect_mock, \
             patch.object(scenarios.manager, "broadcast_log", new=AsyncMock()) as broadcast_mock, \
             patch.object(scenarios.manager, "send_message", new=AsyncMock()) as send_mock, \
             patch.object(scenarios.manager, "disconnect") as disconnect_mock:
            await scenarios.websocket_run_scenario(
                websocket,
                self.scenario_id,
                env_id=None,
                device_serial="ios-1",
            )

        prepare_device.assert_called_once()
        shared_core.assert_called_once()
        self.assertTrue(shared_core.call_args.kwargs["commit_per_step"])
        connect_mock.assert_awaited_once()
        send_mock.assert_awaited()

        payload = send_mock.await_args_list[-1].args[1]
        self.assertEqual(payload["type"], "run_complete")
        self.assertEqual(payload["status"], "PASS")
        self.assertEqual(payload["report_id"], "report-1")

        messages = [call.args[2] for call in broadcast_mock.await_args_list]
        self.assertIn("🧠 使用跨端执行引擎", messages)
        self.assertIn("📊 报告已生成: report-1", messages)
        self.assertIn("summary", messages)

        restore_status.assert_called_once()
        unregister_abort.assert_called_once_with("ios-1")
        disconnect_mock.assert_called_once_with(websocket, f"scenario:{self.scenario_id}")

    async def test_websocket_without_device_reports_error_and_skips_execution(self):
        websocket = object()

        async def fake_run_in_blocking_executor(executor, func, *args, **kwargs):
            return func(*args, **kwargs)

        with patch.object(scenarios, "engine", self.engine), \
             patch.object(scenarios, "_run_in_blocking_executor", side_effect=fake_run_in_blocking_executor), \
             patch.object(scenarios, "_execute_cross_platform_scenario_ws") as execute_ws, \
             patch.object(scenarios.manager, "connect", new=AsyncMock()), \
             patch.object(scenarios.manager, "broadcast_log", new=AsyncMock()) as broadcast_mock, \
             patch.object(scenarios.manager, "send_message", new=AsyncMock()) as send_mock, \
             patch.object(scenarios.manager, "disconnect"):
            await scenarios.websocket_run_scenario(
                websocket,
                self.scenario_id,
                env_id=None,
                device_serial=None,
            )

        execute_ws.assert_not_called()

        payload = send_mock.await_args_list[-1].args[1]
        self.assertEqual(payload["type"], "run_complete")
        self.assertEqual(payload["status"], "ERROR")
        self.assertIn("请选择执行设备", payload["summary"])

        messages = [call.args[2] for call in broadcast_mock.await_args_list]
        self.assertTrue(any("请选择执行设备" in msg for msg in messages))

        with Session(self.engine) as session:
            from sqlmodel import select

            execution = session.exec(
                select(TestExecution).where(TestExecution.scenario_id == self.scenario_id)
            ).first()
            self.assertIsNotNone(execution)
            self.assertEqual(execution.status, "ERROR")


if __name__ == "__main__":
    unittest.main()
