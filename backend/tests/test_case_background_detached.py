import threading
import unittest
from unittest.mock import patch

from sqlmodel import SQLModel, Session, create_engine

from backend.api import cases as cases_api
from backend.models import Device, TestCase


class _FakeLegacyRunner:
    def __init__(self, device_serial=None):  # noqa: ARG002
        self.device_serial = device_serial

    def connect(self):
        return None

    def run_case(self, case, extra_variables=None):  # noqa: ARG002
        # The background path should pass a detached-safe snapshot here.
        self._captured_case = case
        return {
            "case_id": case.id,
            "success": True,
            "steps": [{"success": True, "status": "PASS"}],
            "exported_variables": {},
        }


class CaseBackgroundDetachedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as session:
            case = TestCase(name="bg-case", steps=[], variables=[])
            device = Device(serial="device-1", platform="android", status="IDLE")
            session.add(case)
            session.add(device)
            session.commit()
            session.refresh(case)
            self.case_id = case.id

    def _session_factory(self):
        return Session(self.engine)

    def test_cross_platform_background_uses_case_snapshot_after_commit(self):
        captured = {}

        def _fake_run_case_with_standard_runner(
            *,
            session,  # noqa: ARG001
            case,
            device_serial,  # noqa: ARG001
            env_id=None,  # noqa: ARG001
            variables_map=None,  # noqa: ARG001
            abort_event=None,  # noqa: ARG001
        ):
            captured["case"] = case
            # Snapshot must already contain all execution fields without DB refresh.
            self.assertEqual(case.name, "bg-case")
            self.assertEqual(case.steps, [])
            self.assertEqual(case.variables, [])
            return {
                "case_id": case.id,
                "success": True,
                "steps": [{"success": True, "status": "PASS"}],
                "exported_variables": {},
            }

        with patch("backend.api.cases.register_device_abort", return_value=threading.Event()), patch(
            "backend.api.cases.run_case_with_standard_runner",
            side_effect=_fake_run_case_with_standard_runner,
        ), patch(
            "backend.api.cases.restore_device_status_after_execution",
            return_value="IDLE",
        ), patch("backend.api.cases.unregister_device_abort"):
            cases_api._run_case_background_cross_platform(
                case_id=self.case_id,
                session_factory=self._session_factory,
                env_id=None,
                device_serial="device-1",
            )

        passed_case = captured.get("case")
        self.assertIsNotNone(passed_case)
        self.assertEqual(passed_case.__class__.__name__, "_CaseSnapshot")

        with Session(self.engine) as session:
            db_case = session.get(TestCase, self.case_id)
            self.assertEqual(db_case.last_run_status, "PASS")
            self.assertIsNotNone(db_case.last_run_time)

    def test_legacy_background_uses_case_snapshot(self):
        captured = {}

        class _FakeRunnerWithCapture(_FakeLegacyRunner):
            def run_case(self, case, extra_variables=None):  # noqa: ARG002
                captured["case"] = case
                return super().run_case(case, extra_variables=extra_variables)

        with patch("backend.runner.TestRunner", _FakeRunnerWithCapture):
            cases_api._run_case_background(
                case_id=self.case_id,
                session_factory=self._session_factory,
                env_id=None,
                device_serial="device-1",
            )

        passed_case = captured.get("case")
        self.assertIsNotNone(passed_case)
        self.assertEqual(passed_case.__class__.__name__, "_CaseSnapshot")

        with Session(self.engine) as session:
            db_case = session.get(TestCase, self.case_id)
            self.assertEqual(db_case.last_run_status, "PASS")
            self.assertIsNotNone(db_case.last_run_time)


if __name__ == "__main__":
    unittest.main()
