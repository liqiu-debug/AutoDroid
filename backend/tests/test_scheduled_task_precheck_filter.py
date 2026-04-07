import json
import unittest
from unittest.mock import patch

from sqlmodel import SQLModel, Session, create_engine

from backend.api.tasks import _run_scheduled_scenario
from backend.models import ScheduledTask


class ScheduledTaskPrecheckFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)

    def tearDown(self) -> None:
        self.session.close()

    def _create_ui_task(self, name: str, scenario_id: int, device_serial: str, env_id: int = 0) -> ScheduledTask:
        task = ScheduledTask(
            name=name,
            scenario_id=scenario_id,
            device_serial=device_serial,
            strategy="DAILY",
            strategy_config=json.dumps({"_task_type": "ui", "env_id": env_id}),
            enable_notification=False,
            is_active=True,
        )
        self.session.add(task)
        self.session.commit()
        self.session.refresh(task)
        return task

    def test_scheduler_runs_only_runnable_devices_after_precheck(self):
        task = self._create_ui_task(
            name="nightly",
            scenario_id=101,
            device_serial="android-1,ios-1",
            env_id=88,
        )

        def _fake_precheck(*, session, scenario_id, device_serial, env_id):
            _ = (session, scenario_id, env_id)
            return {"ok": device_serial == "android-1"}

        with patch("backend.database.engine", self.engine), \
             patch("backend.api.scenarios.precheck_scenario_execution", side_effect=_fake_precheck), \
             patch("backend.api.scenarios._summarize_precheck_failure", return_value="blocked"), \
             patch("backend.api.scenarios.execute_scenario_batch_background", return_value="batch-1") as execute_mock:
            _run_scheduled_scenario(task.id)

        execute_mock.assert_called_once_with(
            101,
            "定时任务: nightly",
            88,
            ["android-1"],
        )

    def test_scheduler_cancels_when_all_devices_blocked_by_precheck(self):
        task = self._create_ui_task(
            name="nightly-blocked",
            scenario_id=202,
            device_serial="ios-1",
            env_id=66,
        )

        with patch("backend.database.engine", self.engine), \
             patch("backend.api.scenarios.precheck_scenario_execution", return_value={"ok": False}), \
             patch("backend.api.scenarios._summarize_precheck_failure", return_value="blocked"), \
             patch("backend.api.scenarios.execute_scenario_batch_background", return_value="batch-2") as execute_mock:
            _run_scheduled_scenario(task.id)

        execute_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
