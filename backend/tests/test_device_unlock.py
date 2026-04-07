import unittest
from unittest.mock import patch

from sqlmodel import SQLModel, Session, create_engine, select

from backend.api.devices import unlock_device
from backend.models import Device, TestExecution, TestScenario


class DeviceUnlockTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)

        self.scenario = TestScenario(name="scenario-unlock")
        self.session.add(self.scenario)
        self.session.commit()
        self.session.refresh(self.scenario)

    def tearDown(self) -> None:
        self.session.close()

    def _add_ios_device(self, status: str = "BUSY") -> Device:
        device = Device(serial="ios-1", platform="ios", model="iPhone 15", status=status)
        self.session.add(device)
        self.session.commit()
        self.session.refresh(device)
        return device

    def _add_running_execution(self) -> TestExecution:
        execution = TestExecution(
            scenario_id=self.scenario.id,
            scenario_name=self.scenario.name,
            status="RUNNING",
            device_serial="ios-1",
            platform="ios",
            device_info="iPhone 15",
        )
        self.session.add(execution)
        self.session.commit()
        self.session.refresh(execution)
        return execution

    async def test_ios_unlock_marks_running_execution_error_without_adb_cleanup(self):
        self._add_ios_device(status="BUSY")
        self._add_running_execution()

        with patch(
            "backend.api.devices._check_ios_wda_health",
            return_value={
                "healthy": False,
                "wda_url": "http://127.0.0.1:8201",
                "error": "wda down",
            },
        ), patch("backend.api.devices.asyncio.create_subprocess_shell") as adb_mock, patch(
            "backend.runner.trigger_device_abort"
        ) as abort_mock:
            payload = await unlock_device(
                serial="ios-1",
                session=self.session,
                current_user=object(),
            )

        execution = self.session.exec(
            select(TestExecution).where(TestExecution.device_serial == "ios-1")
        ).first()

        self.assertEqual(payload["platform"], "ios")
        self.assertEqual(payload["device"].status, "WDA_DOWN")
        self.assertEqual(payload["recovered_executions"], 1)
        self.assertFalse(payload["wda_healthy"])
        self.assertEqual(payload["wda_error"], "wda down")
        self.assertIsNotNone(execution)
        self.assertEqual(execution.status, "ERROR")
        self.assertIsNotNone(execution.end_time)
        abort_mock.assert_called_once_with("ios-1")
        adb_mock.assert_not_called()

    async def test_ios_unlock_restores_idle_when_wda_is_healthy(self):
        self._add_ios_device(status="BUSY")

        with patch(
            "backend.api.devices._check_ios_wda_health",
            return_value={
                "healthy": True,
                "wda_url": "http://127.0.0.1:8201",
                "error": None,
            },
        ), patch("backend.runner.trigger_device_abort"):
            payload = await unlock_device(
                serial="ios-1",
                session=self.session,
                current_user=object(),
            )

        device = self.session.exec(select(Device).where(Device.serial == "ios-1")).first()
        self.assertIsNotNone(device)
        self.assertEqual(device.status, "IDLE")
        self.assertEqual(payload["device"].status, "IDLE")
        self.assertEqual(payload["recovered_executions"], 0)
        self.assertTrue(payload["wda_healthy"])


if __name__ == "__main__":
    unittest.main()
