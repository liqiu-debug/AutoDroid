import unittest
from unittest.mock import patch

from fastapi import BackgroundTasks

from backend.models import User
from backend.main import run_test_case_legacy_alias


class LegacyRunAliasTests(unittest.TestCase):
    def test_legacy_run_alias_delegates_to_cases_api(self):
        background_tasks = BackgroundTasks()
        fake_session = object()
        current_user = User(id=11, username="runner", hashed_password="x")
        delegated = {"message": "Execution started", "case_id": 7, "runner": "cross_platform"}

        with patch("backend.main.cases.run_test_case", return_value=delegated) as delegate_mock:
            result = run_test_case_legacy_alias(
                case_id=7,
                background_tasks=background_tasks,
                env_id=3,
                device_serial="device-1",
                session=fake_session,
                current_user=current_user,
            )

        delegate_mock.assert_called_once_with(
            case_id=7,
            background_tasks=background_tasks,
            env_id=3,
            device_serial="device-1",
            session=fake_session,
            current_user=current_user,
        )
        self.assertEqual(result["case_id"], 7)
        self.assertTrue(result["deprecated"])
        self.assertEqual(result["deprecated_endpoint"], "/run/{case_id}")
        self.assertEqual(result["replacement_endpoint"], "/cases/{case_id}/run")
        self.assertIn("将下线", result["message"])


if __name__ == "__main__":
    unittest.main()
