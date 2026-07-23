import unittest
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from backend.api.tasks import (
    _run_scheduled_inspection,
    _validate_scheduled_target,
)
from backend.feature_flags import FLAG_MODEL_INSPECTION
from backend.inspection.runtime import discard_abort_event
from backend.models import (
    Device,
    InspectionBranchRun,
    InspectionProfile,
    InspectionRun,
    SystemSetting,
    User,
)


class ScheduledInspectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.user = User(username="scheduler-user", hashed_password="x")
        self.device = Device(
            serial="scheduled-android",
            platform="android",
            status="IDLE",
        )
        branches = {
            "guest": {
                "name": "未登录",
                "prepare_case_id": 1,
                "entry_case_id": 1,
                "ready_assertion": {
                    "selector": "首页",
                    "by": "description",
                },
            },
            "authenticated": {
                "name": "已登录",
                "prepare_case_id": 2,
                "entry_case_id": 2,
                "ready_assertion": {
                    "selector": "首页",
                    "by": "description",
                },
            },
        }
        self.profile = InspectionProfile(
            name="scheduled profile",
            package_name="com.example.scheduled",
            branches=branches,
            budgets={"duration_seconds": 300},
        )
        self.session.add(self.user)
        self.session.add(self.device)
        self.session.add(self.profile)
        self.session.commit()
        self.session.refresh(self.user)
        self.session.refresh(self.profile)

    def tearDown(self) -> None:
        for row in self.session.exec(select(InspectionRun)).all():
            if row.id is not None:
                discard_abort_event(row.id)
        self.session.close()
        self.engine.dispose()

    def _enable(self) -> None:
        self.session.add(
            SystemSetting(key=FLAG_MODEL_INSPECTION, value="true")
        )
        self.session.commit()

    def test_schedule_validation_is_feature_gated(self):
        with self.assertRaises(HTTPException) as context:
            _validate_scheduled_target(
                session=self.session,
                scenario_id=None,
                device_serials=[self.device.serial],
                config={
                    "_task_type": "inspection",
                    "inspection_profile_id": self.profile.id,
                },
            )
        self.assertEqual(context.exception.status_code, 404)

    def test_schedule_requires_exactly_one_explicit_android_device(self):
        self._enable()
        with self.assertRaises(HTTPException) as context:
            _validate_scheduled_target(
                session=self.session,
                scenario_id=None,
                device_serials=[],
                config={
                    "_task_type": "inspection",
                    "inspection_profile_id": self.profile.id,
                },
            )
        self.assertEqual(context.exception.status_code, 400)

    def test_scheduled_execution_creates_snapshot_and_branch_rows(self):
        self._enable()
        config = {
            "_task_type": "inspection",
            "inspection_profile_id": self.profile.id,
            "inspection_branches": ["authenticated"],
        }
        _validate_scheduled_target(
            session=self.session,
            scenario_id=None,
            device_serials=[self.device.serial],
            config=config,
        )

        with patch("backend.database.engine", self.engine), patch(
            "backend.inspection.engine.execute_inspection_run"
        ) as execute:
            run_id = _run_scheduled_inspection(
                config=config,
                task_name="nightly inspection",
                device_serial=self.device.serial,
                executor_id=self.user.id,
            )

        self.assertIsNotNone(run_id)
        execute.assert_called_once()
        self.session.expire_all()
        run = self.session.get(InspectionRun, run_id)
        self.assertEqual(run.status, "PENDING")
        self.assertEqual(run.selected_branches, ["authenticated"])
        self.assertEqual(
            run.profile_snapshot["selected_branches"],
            ["authenticated"],
        )
        self.assertEqual(run.profile_snapshot["graph_hierarchy_version"], 2)
        branches = self.session.exec(
            select(InspectionBranchRun).where(
                InspectionBranchRun.run_id == run_id
            )
        ).all()
        self.assertEqual([item.branch_key for item in branches], ["authenticated"])


if __name__ == "__main__":
    unittest.main()
