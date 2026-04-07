import unittest
from datetime import datetime, timedelta

from sqlmodel import Session, SQLModel, create_engine

from backend.api.reports import get_dashboard_overview
from backend.models import Device, ScheduledTask, TestExecution, TestScenario


class DashboardOverviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self._seed_data()

    def tearDown(self) -> None:
        self.session.close()

    def _seed_data(self) -> None:
        scenario_main = TestScenario(name="主链路冒烟")
        scenario_ios = TestScenario(name="iOS 回归")
        self.session.add_all([scenario_main, scenario_ios])
        self.session.commit()
        self.session.refresh(scenario_main)
        self.session.refresh(scenario_ios)

        now = datetime.now()
        executions = [
            # Android - in range
            TestExecution(
                scenario_id=scenario_main.id,
                scenario_name=scenario_main.name,
                status="PASS",
                platform="android",
                device_serial="android-01",
                start_time=now - timedelta(days=1, minutes=30),
                end_time=now - timedelta(days=1, minutes=25),
            ),
            TestExecution(
                scenario_id=scenario_main.id,
                scenario_name=scenario_main.name,
                status="FAIL",
                platform="android",
                device_serial="android-01",
                start_time=now - timedelta(days=2, minutes=10),
                end_time=now - timedelta(days=2, minutes=6),
            ),
            TestExecution(
                scenario_id=scenario_main.id,
                scenario_name=scenario_main.name,
                status="WARNING",
                platform="android",
                device_serial="android-02",
                start_time=now - timedelta(days=3, minutes=40),
                end_time=now - timedelta(days=3, minutes=33),
            ),
            TestExecution(
                scenario_id=scenario_main.id,
                scenario_name=scenario_main.name,
                status="RUNNING",
                platform="android",
                device_serial="android-02",
                start_time=now - timedelta(minutes=5),
            ),
            # Android - out of range for 7d
            TestExecution(
                scenario_id=scenario_main.id,
                scenario_name=scenario_main.name,
                status="PASS",
                platform="android",
                device_serial="android-01",
                start_time=now - timedelta(days=8),
                end_time=now - timedelta(days=8, minutes=-2),
            ),
            # iOS - in range
            TestExecution(
                scenario_id=scenario_ios.id,
                scenario_name=scenario_ios.name,
                status="PASS",
                platform="ios",
                device_serial="ios-01",
                start_time=now - timedelta(hours=8),
                end_time=now - timedelta(hours=7, minutes=56),
            ),
        ]
        self.session.add_all(executions)

        devices = [
            Device(serial="android-01", platform="android", model="Pixel 8", status="IDLE"),
            Device(serial="android-02", platform="android", model="Xiaomi 14", status="OFFLINE"),
            Device(serial="ios-01", platform="ios", model="iPhone 15", status="IDLE"),
            Device(serial="ios-02", platform="ios", model="iPhone 14", status="WDA_DOWN"),
        ]
        self.session.add_all(devices)

        tasks = [
            ScheduledTask(
                name="缺少调度时间任务",
                scenario_id=scenario_main.id,
                strategy="DAILY",
                strategy_config='{"hour": 10, "minute": 0}',
                is_active=True,
                next_run_time=None,
            ),
            ScheduledTask(
                name="超时未触发任务",
                scenario_id=scenario_main.id,
                strategy="INTERVAL",
                strategy_config='{"interval_value": 30, "interval_unit": "minutes"}',
                is_active=True,
                next_run_time=now - timedelta(minutes=12),
            ),
            ScheduledTask(
                name="即将执行任务",
                scenario_id=scenario_main.id,
                strategy="WEEKLY",
                strategy_config='{"days": [0,2,4], "hour": 9, "minute": 0}',
                is_active=True,
                next_run_time=now + timedelta(minutes=40),
            ),
        ]
        self.session.add_all(tasks)
        self.session.commit()

    def test_dashboard_overview_android_7d(self):
        overview = get_dashboard_overview(
            range_key="7d",
            platform="android",
            limit_recent=10,
            limit_tasks=10,
            session=self.session,
        )

        self.assertEqual(overview.kpis.total_executions, 4)
        self.assertAlmostEqual(overview.kpis.pass_rate, 33.3, places=1)
        self.assertEqual(overview.kpis.failed_scenarios, 1)
        self.assertEqual(overview.kpis.running_executions, 1)
        self.assertEqual(overview.kpis.idle_devices, 1)
        self.assertEqual(overview.kpis.active_tasks, 3)

        status_map = {item.status: item.count for item in overview.status_distribution}
        self.assertEqual(status_map["PASS"], 1)
        self.assertEqual(status_map["WARNING"], 1)
        self.assertEqual(status_map["FAIL"], 1)
        self.assertEqual(status_map["RUNNING"], 1)

        self.assertGreaterEqual(len(overview.top_failed_scenarios), 1)
        self.assertEqual(overview.top_failed_scenarios[0].name, "主链路冒烟")
        self.assertEqual(overview.top_failed_scenarios[0].fail_count, 2)

        alert_types = {item.type for item in overview.alerts}
        self.assertIn("device_offline", alert_types)
        self.assertIn("task_missing_next_run", alert_types)
        self.assertIn("task_overdue", alert_types)

        self.assertGreaterEqual(len(overview.recent_executions), 1)
        self.assertGreaterEqual(len(overview.trend), 7)
        self.assertEqual(len(overview.upcoming_tasks), 1)
        self.assertEqual(overview.upcoming_tasks[0].name, "即将执行任务")

    def test_dashboard_overview_ios_filter(self):
        overview = get_dashboard_overview(
            range_key="7d",
            platform="ios",
            limit_recent=10,
            limit_tasks=10,
            session=self.session,
        )

        self.assertEqual(overview.kpis.total_executions, 1)
        self.assertEqual(overview.kpis.pass_rate, 100.0)
        self.assertEqual(overview.kpis.idle_devices, 1)

        alert_types = {item.type for item in overview.alerts}
        self.assertIn("device_wda_down", alert_types)


if __name__ == "__main__":
    unittest.main()
