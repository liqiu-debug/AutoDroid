import unittest
from datetime import datetime, timedelta

from sqlmodel import Session, SQLModel, create_engine

from backend.api.reports import get_reports
from backend.models import TestExecution, TestScenario


class ReportFiltersTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self._seed_data()

    def tearDown(self) -> None:
        self.session.close()

    def _seed_data(self) -> None:
        scenario = TestScenario(name="scenario-report-filter")
        self.session.add(scenario)
        self.session.commit()
        self.session.refresh(scenario)

        base_time = datetime(2026, 3, 5, 10, 0, 0)
        executions = [
            TestExecution(
                scenario_id=scenario.id,
                scenario_name=scenario.name,
                status="PASS",
                start_time=base_time,
                device_serial="android-001",
                platform="android",
                device_info="Pixel 8 (android-001)",
            ),
            TestExecution(
                scenario_id=scenario.id,
                scenario_name=scenario.name,
                status="WARNING",
                start_time=base_time + timedelta(minutes=1),
                device_serial="ios-001",
                platform="ios",
                device_info="iPhone 15 (ios-001)",
            ),
            TestExecution(
                scenario_id=scenario.id,
                scenario_name=scenario.name,
                status="PASS",
                start_time=base_time + timedelta(minutes=2),
                device_serial=None,
                platform=None,
                device_info="Legacy Device (legacy-001)",
            ),
        ]
        self.session.add_all(executions)
        self.session.commit()

    def test_filter_by_platform_is_case_insensitive(self):
        result = get_reports(platform="IOS", session=self.session)
        self.assertEqual(result.total, 1)
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0].platform, "ios")
        self.assertEqual(result.items[0].device_serial, "ios-001")

    def test_filter_by_platform_and_device_serial(self):
        result = get_reports(platform="android", device_serial="android-001", session=self.session)
        self.assertEqual(result.total, 1)
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0].platform, "android")
        self.assertEqual(result.items[0].device_serial, "android-001")

    def test_legacy_execution_is_kept_without_structured_filters(self):
        result = get_reports(session=self.session)
        self.assertEqual(result.total, 3)
        self.assertEqual(len(result.items), 3)
        legacy_items = [item for item in result.items if item.platform is None and item.device_serial is None]
        self.assertEqual(len(legacy_items), 1)
        self.assertEqual(legacy_items[0].device_info, "Legacy Device (legacy-001)")


if __name__ == "__main__":
    unittest.main()
