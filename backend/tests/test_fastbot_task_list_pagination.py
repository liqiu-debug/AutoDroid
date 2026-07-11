import json
import unittest

from sqlmodel import Session, SQLModel, create_engine

from backend.api import fastbot as fastbot_api
from backend.models import FastbotReport, FastbotTask


class FastbotTaskListPaginationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self._original_startup_ids = set(fastbot_api._startup_task_ids)
        fastbot_api._startup_task_ids.clear()
        self._seed_data()

    def tearDown(self) -> None:
        fastbot_api._startup_task_ids.clear()
        fastbot_api._startup_task_ids.update(self._original_startup_ids)
        self.session.close()

    def _task(self, package_name: str, device_serial: str = "android-001") -> FastbotTask:
        task = FastbotTask(
            package_name=package_name,
            device_serial=device_serial,
            status="COMPLETED",
        )
        self.session.add(task)
        self.session.commit()
        self.session.refresh(task)
        return task

    def _report(self, task_id: int, summary: dict) -> FastbotReport:
        report = FastbotReport(task_id=task_id, summary=json.dumps(summary))
        self.session.add(report)
        self.session.commit()
        self.session.refresh(report)
        return report

    def _seed_data(self) -> None:
        # 三个普通探索任务（其中一个有普通报告）+ 两个冷热启动任务
        self.explore_1 = self._task("com.demo.alpha", "android-001")
        self.explore_2 = self._task("com.demo.beta", "android-002")
        self.explore_3 = self._task("com.demo.gamma", "android-003")
        self._report(self.explore_3.id, {"avg_cpu": 20})

        self.startup_1 = self._task("com.demo.alpha", "android-009")
        self._report(self.startup_1.id, {"session_type": "startup", "slow_count": 1})
        self.startup_2 = self._task("com.demo.delta", "android-010")
        self._report(self.startup_2.id, {"session_type": "startup", "slow_count": 0})

    def test_list_tasks_excludes_startup_and_reports_total(self):
        result = fastbot_api.list_tasks(session=self.session)
        self.assertEqual(result.total, 3)
        self.assertEqual(
            [item.id for item in result.items],
            [self.explore_3.id, self.explore_2.id, self.explore_1.id],
        )

    def test_list_tasks_pagination_keeps_total(self):
        result = fastbot_api.list_tasks(skip=1, limit=1, session=self.session)
        self.assertEqual(result.total, 3)
        self.assertEqual([item.id for item in result.items], [self.explore_2.id])

    def test_list_tasks_keyword_matches_package_or_serial(self):
        by_package = fastbot_api.list_tasks(keyword="beta", session=self.session)
        self.assertEqual(by_package.total, 1)
        self.assertEqual(by_package.items[0].id, self.explore_2.id)

        by_serial = fastbot_api.list_tasks(keyword="android-003", session=self.session)
        self.assertEqual(by_serial.total, 1)
        self.assertEqual(by_serial.items[0].id, self.explore_3.id)

    def test_startup_tasks_only_include_startup_reports(self):
        result = fastbot_api.list_startup_tasks(session=self.session)
        self.assertEqual(result.total, 2)
        self.assertEqual(
            [item.id for item in result.items],
            [self.startup_2.id, self.startup_1.id],
        )
        self.assertTrue(all(item.report_ready for item in result.items))
        self.assertEqual(result.items[1].summary["slow_count"], 1)

    def test_startup_tasks_pagination_and_keyword(self):
        paged = fastbot_api.list_startup_tasks(skip=1, limit=1, session=self.session)
        self.assertEqual(paged.total, 2)
        self.assertEqual([item.id for item in paged.items], [self.startup_1.id])

        keyword = fastbot_api.list_startup_tasks(keyword="delta", session=self.session)
        self.assertEqual(keyword.total, 1)
        self.assertEqual(keyword.items[0].id, self.startup_2.id)

    def test_freshly_launched_startup_task_without_report_is_listed(self):
        pending = self._task("com.demo.pending", "android-011")
        fastbot_api._startup_task_ids.add(pending.id)

        startup = fastbot_api.list_startup_tasks(session=self.session)
        self.assertEqual(startup.total, 3)
        self.assertEqual(startup.items[0].id, pending.id)
        self.assertFalse(startup.items[0].report_ready)

        explore = fastbot_api.list_tasks(session=self.session)
        self.assertEqual(explore.total, 3)
        self.assertNotIn(pending.id, [item.id for item in explore.items])


if __name__ == "__main__":
    unittest.main()
