import threading
import time
import unittest
from unittest.mock import patch

from sqlmodel import SQLModel, Session, create_engine, select

from backend.api.runs import CancelRunRequest, active_runs, cancel_runs
from backend.models import TestCase, TestExecution, TestScenario
from backend.run_control import ABORTED_STATUS, registry
from backend.runner import _sleep_or_abort


class RunCancelApiTests(unittest.TestCase):
    def setUp(self) -> None:
        registry.clear()
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)

    def tearDown(self) -> None:
        registry.clear()
        self.session.close()

    def test_cancel_case_run_sets_abort_event_and_case_status(self):
        case = TestCase(name="case-cancel", steps=[], variables=[])
        self.session.add(case)
        self.session.commit()
        self.session.refresh(case)

        abort_event = threading.Event()
        record = registry.register(
            kind="case",
            target_id=case.id,
            batch_id="batch-case-1",
            device_serial="android-1",
            abort_event=abort_event,
        )

        with patch("backend.runner.trigger_device_abort") as trigger_mock:
            payload = cancel_runs(
                CancelRunRequest(
                    kind="case",
                    target_id=case.id,
                    batch_id="batch-case-1",
                    run_ids=[record.run_id],
                ),
                session=self.session,
            )

        self.session.refresh(case)
        self.assertEqual(payload["status"], ABORTED_STATUS)
        self.assertEqual(payload["cancelled_count"], 1)
        self.assertTrue(abort_event.is_set())
        self.assertEqual(case.last_run_status, ABORTED_STATUS)
        trigger_mock.assert_called_once_with("android-1")
        self.assertEqual(registry.active(kind="case", target_id=case.id), [])

    def test_cancel_scenario_batch_marks_only_matching_running_executions(self):
        scenario = TestScenario(name="scenario-cancel")
        self.session.add(scenario)
        self.session.commit()
        self.session.refresh(scenario)

        execution_1 = TestExecution(
            scenario_id=scenario.id,
            scenario_name=scenario.name,
            status="RUNNING",
            device_serial="android-1",
            batch_id="batch-1",
        )
        execution_2 = TestExecution(
            scenario_id=scenario.id,
            scenario_name=scenario.name,
            status="RUNNING",
            device_serial="android-2",
            batch_id="batch-2",
        )
        self.session.add(execution_1)
        self.session.add(execution_2)
        self.session.commit()
        self.session.refresh(execution_1)
        self.session.refresh(execution_2)

        abort_event = threading.Event()
        registry.register(
            kind="scenario",
            target_id=scenario.id,
            batch_id="batch-1",
            device_serial="android-1",
            abort_event=abort_event,
            execution_id=execution_1.id,
        )

        cancel_runs(
            CancelRunRequest(kind="scenario", target_id=scenario.id, batch_id="batch-1"),
            session=self.session,
        )

        refreshed_1 = self.session.get(TestExecution, execution_1.id)
        refreshed_2 = self.session.get(TestExecution, execution_2.id)
        refreshed_scenario = self.session.get(TestScenario, scenario.id)
        self.assertEqual(refreshed_1.status, ABORTED_STATUS)
        self.assertIsNotNone(refreshed_1.end_time)
        self.assertEqual(refreshed_2.status, "RUNNING")
        self.assertEqual(refreshed_scenario.last_run_status, ABORTED_STATUS)
        self.assertTrue(abort_event.is_set())

    def test_active_scenario_runs_include_database_running_records(self):
        scenario = TestScenario(name="scenario-active")
        self.session.add(scenario)
        self.session.commit()
        self.session.refresh(scenario)

        execution = TestExecution(
            scenario_id=scenario.id,
            scenario_name=scenario.name,
            status="RUNNING",
            device_serial="ios-1",
            batch_id="batch-db",
        )
        self.session.add(execution)
        self.session.commit()
        self.session.refresh(execution)

        payload = active_runs(kind="scenario", target_id=scenario.id, session=self.session)
        self.assertEqual(len(payload["items"]), 1)
        self.assertEqual(payload["items"][0]["execution_id"], execution.id)
        self.assertEqual(payload["items"][0]["batch_id"], "batch-db")

    def test_interruptible_sleep_returns_when_abort_is_set(self):
        abort_event = threading.Event()
        threading.Timer(0.05, abort_event.set).start()

        started_at = time.time()
        aborted = _sleep_or_abort(5.0, abort_event)
        elapsed = time.time() - started_at

        self.assertTrue(aborted)
        self.assertLess(elapsed, 1.0)


if __name__ == "__main__":
    unittest.main()
