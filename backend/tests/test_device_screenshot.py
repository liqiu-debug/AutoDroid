import base64
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from sqlmodel import SQLModel, Session, create_engine, select

from backend.api.devices import get_screenshot
from backend.models import Device


class DeviceScreenshotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)

    def tearDown(self) -> None:
        self.session.close()

    def _add_ios_device(self, status: str = "IDLE") -> Device:
        device = Device(serial="ios-1", platform="ios", model="iPhone 15", status=status)
        self.session.add(device)
        self.session.commit()
        self.session.refresh(device)
        return device

    async def test_ios_screenshot_uses_wda_path_and_recovers_status(self):
        self._add_ios_device(status="WDA_DOWN")
        raw_png = b"\x89PNG\r\n\x1a\n" + (b"0" * 256)

        with patch(
            "backend.cross_platform_execution.resolve_ios_wda_url",
            return_value="http://127.0.0.1:8201",
        ), patch(
            "backend.cross_platform_execution.check_wda_health",
        ) as health_mock, patch(
            "backend.api.devices._probe_ios_wda_actionability",
        ) as actionability_mock, patch(
            "backend.api.devices._capture_ios_screenshot_bytes",
            return_value=raw_png,
        ) as screenshot_mock, patch(
            "backend.api.devices._run_adb_command",
        ) as adb_mock:
            payload = await get_screenshot(serial="ios-1", session=self.session)

        device = self.session.exec(select(Device).where(Device.serial == "ios-1")).first()
        self.assertIsNotNone(device)
        self.assertEqual(device.status, "IDLE")
        self.assertEqual(payload["base64_img"], base64.b64encode(raw_png).decode("utf-8"))
        health_mock.assert_called_once_with("http://127.0.0.1:8201")
        actionability_mock.assert_called_once_with("http://127.0.0.1:8201")
        screenshot_mock.assert_called_once_with("ios-1", "http://127.0.0.1:8201")
        adb_mock.assert_not_called()

    async def test_ios_screenshot_marks_wda_down_when_health_check_fails(self):
        self._add_ios_device(status="IDLE")

        with patch(
            "backend.cross_platform_execution.resolve_ios_wda_url",
            return_value="http://127.0.0.1:8201",
        ), patch(
            "backend.cross_platform_execution.check_wda_health",
            side_effect=RuntimeError("wda down"),
        ), patch(
            "backend.api.devices._probe_ios_wda_actionability",
        ) as actionability_mock, patch(
            "backend.api.devices._capture_ios_screenshot_bytes",
        ) as screenshot_mock:
            with self.assertRaises(HTTPException) as context:
                await get_screenshot(serial="ios-1", session=self.session)

        device = self.session.exec(select(Device).where(Device.serial == "ios-1")).first()
        self.assertIsNotNone(device)
        self.assertEqual(device.status, "WDA_DOWN")
        self.assertEqual(context.exception.status_code, 500)
        self.assertIn("WDA 不可用", str(context.exception.detail))
        actionability_mock.assert_not_called()
        screenshot_mock.assert_not_called()

    async def test_ios_screenshot_marks_wda_down_when_runtime_action_fails(self):
        self._add_ios_device(status="IDLE")

        with patch(
            "backend.cross_platform_execution.resolve_ios_wda_url",
            return_value="http://127.0.0.1:8201",
        ), patch(
            "backend.cross_platform_execution.check_wda_health",
        ), patch(
            "backend.api.devices._probe_ios_wda_actionability",
        ), patch(
            "backend.api.devices._capture_ios_screenshot_bytes",
            side_effect=RuntimeError(
                "iOS WDA 连接失败: "
                "WDARequestError(status=110, value={'error': 'unable to capture screen', "
                "'message': 'Error Domain=XCTDaemonErrorDomain Code=41 "
                "\"Not authorized for performing UI testing actions.\"'})"
            ),
        ):
            with self.assertRaises(HTTPException) as context:
                await get_screenshot(serial="ios-1", session=self.session)

        device = self.session.exec(select(Device).where(Device.serial == "ios-1")).first()
        self.assertIsNotNone(device)
        self.assertEqual(device.status, "WDA_DOWN")
        self.assertEqual(context.exception.status_code, 500)
        self.assertIn("unable to capture screen", str(context.exception.detail))


if __name__ == "__main__":
    unittest.main()
