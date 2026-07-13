import unittest
from unittest.mock import patch

from fastapi import HTTPException
from sqlmodel import SQLModel, Session, create_engine, select

from backend.api.scenarios import run_scenario_api
from backend.execution_limiter import get_execution_limiter, reset_execution_limiter
from backend.models import Device, ScenarioStep, TestCase, TestCaseStep, TestExecution, TestScenario, User
from backend.schemas import ScenarioRunRequest


class ScenarioRunPrecheckApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        reset_execution_limiter()
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)

    def tearDown(self) -> None:
        self.session.close()
        reset_execution_limiter()

    def _create_user(self) -> User:
        user = User(username="tester", hashed_password="x", full_name="Tester")
        self.session.add(user)
        self.session.commit()
        self.session.refresh(user)
        return user

    def _create_scenario_with_android_only_case(self) -> TestScenario:
        case = TestCase(name="case-1", steps=[], variables=[])
        self.session.add(case)
        self.session.commit()
        self.session.refresh(case)

        self.session.add(
            TestCaseStep(
                case_id=case.id,
                order=1,
                action="click",
                execute_on=["android"],
                platform_overrides={"android": {"selector": "com.demo:id/login", "by": "id"}},
                args={},
                timeout=10,
                error_strategy="ABORT",
                description="click login",
            )
        )
        self.session.commit()

        scenario = TestScenario(name="scenario-1")
        self.session.add(scenario)
        self.session.commit()
        self.session.refresh(scenario)

        self.session.add(
            ScenarioStep(
                scenario_id=scenario.id,
                case_id=case.id,
                order=1,
                alias="step-1",
            )
        )
        self.session.commit()
        return scenario

    def _add_devices(self) -> None:
        self.session.add(Device(serial="android-1", platform="android", model="pixel"))
        self.session.add(Device(serial="ios-1", platform="ios", model="iphone"))
        self.session.commit()

    @staticmethod
    def _consume_task(coro):
        # create_task 被 mock 后主动关闭协程，避免 RuntimeWarning。
        coro.close()
        return None

    async def test_run_filters_blocked_devices_and_returns_blocked_prechecks(self):
        scenario = self._create_scenario_with_android_only_case()
        user = self._create_user()
        self._add_devices()

        req = ScenarioRunRequest(device_serials=["android-1", "ios-1"], env_id=None)

        with patch(
            "backend.api.scenarios.asyncio.create_task",
            side_effect=self._consume_task,
        ) as create_task_mock:
            resp = await run_scenario_api(
                scenario_id=scenario.id,
                request=req,
                session=self.session,
                current_user=user,
            )

        self.assertIn("batch_id", resp)
        self.assertEqual(len(resp["execution_ids"]), 1)
        self.assertEqual(len(resp["blocked_prechecks"]), 1)
        self.assertEqual(resp["blocked_prechecks"][0]["device_serial"], "ios-1")
        create_task_mock.assert_called_once()

        executions = self.session.exec(
            select(TestExecution).where(TestExecution.batch_id == resp["batch_id"])
        ).all()
        self.assertEqual(len(executions), 1)
        self.assertEqual(executions[0].device_serial, "android-1")
        self.assertEqual(executions[0].platform, "android")

    async def test_run_returns_400_when_all_devices_blocked_by_precheck(self):
        scenario = self._create_scenario_with_android_only_case()
        user = self._create_user()
        self._add_devices()

        req = ScenarioRunRequest(device_serials=["ios-1"], env_id=None)

        with patch(
            "backend.api.scenarios.asyncio.create_task",
            side_effect=self._consume_task,
        ) as create_task_mock:
            with self.assertRaises(HTTPException) as context:
                await run_scenario_api(
                    scenario_id=scenario.id,
                    request=req,
                    session=self.session,
                    current_user=user,
                )

        exc = context.exception
        self.assertEqual(exc.status_code, 400)
        self.assertIsInstance(exc.detail, dict)
        self.assertEqual(exc.detail.get("code"), "S1001_SCENARIO_PRECHECK_FAILED")
        self.assertEqual(len(exc.detail.get("items", [])), 1)
        self.assertEqual(exc.detail["items"][0].get("device_serial"), "ios-1")
        create_task_mock.assert_not_called()

        execution_count = len(self.session.exec(select(TestExecution)).all())
        self.assertEqual(execution_count, 0)

    async def test_run_queues_when_device_is_rate_limited(self):
        scenario = self._create_scenario_with_android_only_case()
        user = self._create_user()
        self._add_devices()
        req = ScenarioRunRequest(device_serials=["android-1"], env_id=None)

        lease = get_execution_limiter().acquire_lease(
            user_id=user.id,
            device_serial="android-1",
            task_id="busy-run",
            timeout=0,
        )
        try:
            with patch(
                "backend.api.scenarios.asyncio.create_task",
                side_effect=self._consume_task,
            ) as create_task_mock:
                resp = await run_scenario_api(
                    scenario_id=scenario.id,
                    request=req,
                    session=self.session,
                    current_user=user,
                )
        finally:
            lease.release()

        # 并发超限不再 429：任务进入 FIFO 队列并立即返回排队信息
        create_task_mock.assert_called_once()
        self.assertEqual(len(resp["execution_ids"]), 1)
        self.assertEqual(resp["queued_count"], 1)
        self.assertEqual(len(resp["runs"]), 1)
        run_item = resp["runs"][0]
        self.assertTrue(run_item["queued"])
        self.assertEqual(run_item["queue_position"], 1)
        self.assertEqual(run_item["device_serial"], "android-1")

        execution = self.session.get(TestExecution, resp["execution_ids"][0])
        self.assertIsNotNone(execution)
        self.assertEqual(execution.status, "QUEUED")

    async def test_run_starts_without_queueing_when_slots_available(self):
        scenario = self._create_scenario_with_android_only_case()
        user = self._create_user()
        self._add_devices()
        req = ScenarioRunRequest(device_serials=["android-1"], env_id=None)

        with patch(
            "backend.api.scenarios.asyncio.create_task",
            side_effect=self._consume_task,
        ):
            resp = await run_scenario_api(
                scenario_id=scenario.id,
                request=req,
                session=self.session,
                current_user=user,
            )

        self.assertEqual(resp["queued_count"], 0)
        self.assertFalse(resp["runs"][0]["queued"])
        self.assertIsNone(resp["runs"][0]["queue_position"])
        execution = self.session.get(TestExecution, resp["execution_ids"][0])
        self.assertEqual(execution.status, "PENDING")


if __name__ == "__main__":
    unittest.main()
