import unittest
from unittest.mock import patch

from sqlmodel import SQLModel, Session, create_engine, select

from backend.api.devices import list_devices
from backend.models import Device


class DeviceListWdaStatusTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)

    def tearDown(self) -> None:
        self.session.close()

    def _add_device(self, serial: str, platform: str, status: str = "IDLE") -> Device:
        device = Device(serial=serial, platform=platform, model=f"{platform}-model", status=status)
        self.session.add(device)
        self.session.commit()
        self.session.refresh(device)
        return device

    async def test_list_devices_refresh_ios_wda_marks_down(self):
        self._add_device("ios-1", "ios", status="IDLE")

        with patch(
            "backend.api.devices._run_adb_command",
            return_value=b"List of devices attached\n\n",
        ), patch(
            "backend.api.devices._check_ios_wda_health",
            return_value={"healthy": False, "wda_url": "http://127.0.0.1:8201", "error": "wda down"},
        ) as health_mock:
            payload = await list_devices(refresh_ios_wda=True, session=self.session)

        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0].status, "WDA_DOWN")
        health_mock.assert_called_once_with(self.session, "ios-1")

        device = self.session.exec(select(Device).where(Device.serial == "ios-1")).first()
        self.assertIsNotNone(device)
        self.assertEqual(device.status, "WDA_DOWN")

    async def test_list_devices_refresh_ios_wda_marks_down_when_action_probe_fails(self):
        self._add_device("ios-1", "ios", status="IDLE")

        with patch(
            "backend.api.devices._run_adb_command",
            return_value=b"List of devices attached\n\n",
        ), patch(
            "backend.cross_platform_execution.resolve_ios_wda_url",
            return_value="http://127.0.0.1:8201",
        ), patch(
            "backend.cross_platform_execution.check_wda_health",
        ), patch(
            "backend.api.devices._probe_ios_wda_actionability",
            side_effect=RuntimeError("P1005_WDA_UNAVAILABLE: WDA actionable probe failed"),
        ):
            payload = await list_devices(refresh_ios_wda=True, session=self.session)

        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0].status, "WDA_DOWN")

        device = self.session.exec(select(Device).where(Device.serial == "ios-1")).first()
        self.assertIsNotNone(device)
        self.assertEqual(device.status, "WDA_DOWN")

    async def test_list_devices_without_refresh_keeps_previous_ios_status(self):
        self._add_device("ios-1", "ios", status="IDLE")

        with patch(
            "backend.api.devices._run_adb_command",
            return_value=b"List of devices attached\n\n",
        ), patch(
            "backend.api.devices._check_ios_wda_health",
        ) as health_mock:
            payload = await list_devices(refresh_ios_wda=False, session=self.session)

        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0].status, "IDLE")
        health_mock.assert_not_called()

    async def test_list_devices_orders_wda_down_after_idle(self):
        self._add_device("ios-busy", "ios", status="BUSY")
        self._add_device("ios-idle", "ios", status="IDLE")
        self._add_device("ios-wda", "ios", status="WDA_DOWN")
        self._add_device("ios-offline", "ios", status="OFFLINE")

        with patch(
            "backend.api.devices._run_adb_command",
            return_value=b"List of devices attached\n\n",
        ):
            payload = await list_devices(refresh_ios_wda=False, session=self.session)

        self.assertEqual(
            [device.serial for device in payload],
            ["ios-busy", "ios-idle", "ios-wda", "ios-offline"],
        )


if __name__ == "__main__":
    unittest.main()
