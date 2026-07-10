import json
import unittest
from unittest.mock import MagicMock, patch

from sqlmodel import Session, SQLModel, create_engine

from backend.api.tasks import list_tasks
from backend.models import ScheduledTask, TestScenario


class TasksListPaginationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)

        scheduler = MagicMock()
        scheduler.get_next_run_time.return_value = None
        self._scheduler_patch = patch("backend.api.tasks.get_scheduler", return_value=scheduler)
        self._scheduler_patch.start()

        self._seed_data()

    def tearDown(self) -> None:
        self._scheduler_patch.stop()
        self.session.close()

    def _seed_data(self) -> None:
        scenario = TestScenario(name="登录冒烟场景")
        self.session.add(scenario)
        self.session.commit()
        self.session.refresh(scenario)
        self.scenario = scenario

        tasks = [
            ScheduledTask(
                name="每日回归",
                scenario_id=scenario.id,
                strategy="DAILY",
                strategy_config=json.dumps({"_task_type": "ui"}),
            ),
            ScheduledTask(
                name="每周巡检",
                scenario_id=scenario.id,
                strategy="WEEKLY",
                strategy_config=json.dumps({"_task_type": "ui"}),
            ),
            ScheduledTask(
                name="夜间探索",
                scenario_id=None,
                strategy="DAILY",
                strategy_config=json.dumps({"_task_type": "fastbot"}),
            ),
        ]
        self.session.add_all(tasks)
        self.session.commit()

    def test_list_returns_paginated_payload(self):
        result = list_tasks(session=self.session)
        self.assertEqual(result.total, 3)
        self.assertEqual(len(result.items), 3)
        # id 倒序
        self.assertEqual([item.name for item in result.items], ["夜间探索", "每周巡检", "每日回归"])

    def test_skip_limit_slices_without_losing_total(self):
        result = list_tasks(skip=1, limit=1, session=self.session)
        self.assertEqual(result.total, 3)
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0].name, "每周巡检")

    def test_keyword_matches_task_name(self):
        result = list_tasks(keyword="回归", session=self.session)
        self.assertEqual(result.total, 1)
        self.assertEqual(result.items[0].name, "每日回归")

    def test_keyword_matches_scenario_name(self):
        result = list_tasks(keyword="登录冒烟", session=self.session)
        self.assertEqual(result.total, 2)
        self.assertEqual({item.scenario_name for item in result.items}, {"登录冒烟场景"})

    def test_fastbot_task_reports_scenario_placeholder(self):
        result = list_tasks(keyword="夜间", session=self.session)
        self.assertEqual(result.total, 1)
        self.assertEqual(result.items[0].scenario_name, "智能探索")


if __name__ == "__main__":
    unittest.main()
