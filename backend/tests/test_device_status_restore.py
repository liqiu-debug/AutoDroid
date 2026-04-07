import unittest
from unittest.mock import patch

from sqlmodel import SQLModel, Session, create_engine, select

from backend.cross_platform_execution import restore_device_status_after_execution
from backend.models import Device


class DeviceStatusRestoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)

    def tearDown(self) -> None:
        self.session.close()

    def _add_device(self, serial: str, platform: str, status: str = "BUSY") -> Device:
        device = Device(serial=serial, platform=platform, model=f"{platform}-model", status=status)
        self.session.add(device)
        self.session.commit()
        self.session.refresh(device)
        return device

    def test_restore_android_device_status_to_idle(self):
        self._add_device("android-1", "android", status="BUSY")

        with patch("backend.cross_platform_execution.check_wda_health") as check_mock:
            status = restore_device_status_after_execution(self.session, "android-1")

        device = self.session.exec(select(Device).where(Device.serial == "android-1")).first()
        self.assertEqual(status, "IDLE")
        self.assertIsNotNone(device)
        self.assertEqual(device.status, "IDLE")
        check_mock.assert_not_called()

    def test_restore_ios_device_status_to_idle_when_wda_healthy(self):
        self._add_device("ios-1", "ios", status="BUSY")

        with patch(
            "backend.cross_platform_execution.resolve_ios_wda_url",
            return_value="http://127.0.0.1:8201",
        ), patch("backend.cross_platform_execution.check_wda_health") as check_mock:
            status = restore_device_status_after_execution(self.session, "ios-1")

        device = self.session.exec(select(Device).where(Device.serial == "ios-1")).first()
        self.assertEqual(status, "IDLE")
        self.assertIsNotNone(device)
        self.assertEqual(device.status, "IDLE")
        check_mock.assert_called_once_with("http://127.0.0.1:8201")

    def test_restore_ios_device_status_to_wda_down_when_health_check_fails(self):
        self._add_device("ios-1", "ios", status="BUSY")

        with patch(
            "backend.cross_platform_execution.resolve_ios_wda_url",
            return_value="http://127.0.0.1:8201",
        ), patch(
            "backend.cross_platform_execution.check_wda_health",
            side_effect=RuntimeError("P1005_WDA_UNAVAILABLE: health check failed"),
        ):
            status = restore_device_status_after_execution(self.session, "ios-1")

        device = self.session.exec(select(Device).where(Device.serial == "ios-1")).first()
        self.assertEqual(status, "WDA_DOWN")
        self.assertIsNotNone(device)
        self.assertEqual(device.status, "WDA_DOWN")

    def test_restore_skips_non_busy_device_by_default(self):
        self._add_device("ios-1", "ios", status="IDLE")

        with patch("backend.cross_platform_execution.check_wda_health") as check_mock:
            status = restore_device_status_after_execution(self.session, "ios-1")

        device = self.session.exec(select(Device).where(Device.serial == "ios-1")).first()
        self.assertEqual(status, "IDLE")
        self.assertIsNotNone(device)
        self.assertEqual(device.status, "IDLE")
        check_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
