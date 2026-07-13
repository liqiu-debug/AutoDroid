import unittest
from unittest.mock import patch

from fastapi import HTTPException
from sqlmodel import SQLModel, Session, create_engine, select

from backend.api.devices import (
    disable_ios_wireless,
    enable_ios_wireless,
    sync_devices,
    _get_wireless_url_map,
)
from backend.feature_flags import get_setting_value, set_setting_value
from backend.models import Device, SystemSetting
from backend.schemas import DeviceWirelessEnableRequest


def _make_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return engine, Session(engine)


def _add_device(session: Session, serial: str, platform: str, status: str = "IDLE") -> Device:
    device = Device(serial=serial, platform=platform, model=f"{platform}-model", status=status)
    session.add(device)
    session.commit()
    session.refresh(device)
    return device


_HEALTHY = {"healthy": True, "wda_url": "http://127.0.0.1:8201", "error": None}
_UNHEALTHY = {"healthy": False, "wda_url": "http://127.0.0.1:8201", "error": "wda down"}


class WirelessEnableDisableTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine, self.session = _make_session()

    def tearDown(self) -> None:
        self.session.close()

    def _scoped_setting(self, serial: str):
        return get_setting_value(self.session, f"ios_wda_url.{serial}")

    def test_enable_wireless_happy_path(self):
        _add_device(self.session, "ios-1", "ios", status="WDA_DOWN")
        with patch(
            "backend.api.devices._check_ios_wda_health", return_value=dict(_HEALTHY)
        ), patch(
            "backend.api.devices._fetch_ios_device_ip_from_wda", return_value="192.168.1.23"
        ) as fetch_ip_mock, patch(
            "backend.cross_platform_execution.check_wda_health"
        ) as direct_check_mock, patch(
            "backend.api.devices._probe_ios_wda_actionability"
        ) as probe_mock, patch(
            "backend.api.devices.wda_relay_manager"
        ) as relay_mock:
            payload = enable_ios_wireless("ios-1", session=self.session, current_user=None)

        fetch_ip_mock.assert_called_once_with("http://127.0.0.1:8201")
        direct_check_mock.assert_called_once_with("http://192.168.1.23:8100")
        probe_mock.assert_called_once_with("http://192.168.1.23:8100")
        relay_mock.stop_relay.assert_called_once_with("ios-1")

        self.assertTrue(payload["wireless_enabled"])
        self.assertEqual(payload["device_ip"], "192.168.1.23")
        self.assertEqual(payload["wda_url"], "http://192.168.1.23:8100")
        self.assertEqual(payload["status"], "IDLE")
        self.assertEqual(self._scoped_setting("ios-1"), "http://192.168.1.23:8100")

        device = self.session.exec(select(Device).where(Device.serial == "ios-1")).first()
        self.assertEqual(device.status, "IDLE")

    def test_enable_wireless_uses_manual_ip_and_port(self):
        _add_device(self.session, "ios-1", "ios")
        req = DeviceWirelessEnableRequest(ip="192.168.5.9", port=8123)
        with patch(
            "backend.api.devices._check_ios_wda_health", return_value=dict(_HEALTHY)
        ), patch(
            "backend.api.devices._fetch_ios_device_ip_from_wda"
        ) as fetch_ip_mock, patch(
            "backend.cross_platform_execution.check_wda_health"
        ) as direct_check_mock, patch(
            "backend.api.devices._probe_ios_wda_actionability"
        ), patch("backend.api.devices.wda_relay_manager"):
            payload = enable_ios_wireless("ios-1", req=req, session=self.session, current_user=None)

        fetch_ip_mock.assert_not_called()
        direct_check_mock.assert_called_once_with("http://192.168.5.9:8123")
        self.assertEqual(payload["wda_url"], "http://192.168.5.9:8123")
        self.assertEqual(self._scoped_setting("ios-1"), "http://192.168.5.9:8123")

    def test_enable_wireless_rejects_when_wda_unhealthy(self):
        _add_device(self.session, "ios-1", "ios", status="WDA_DOWN")
        with patch(
            "backend.api.devices._check_ios_wda_health", return_value=dict(_UNHEALTHY)
        ):
            with self.assertRaises(HTTPException) as context:
                enable_ios_wireless("ios-1", session=self.session, current_user=None)

        self.assertEqual(context.exception.status_code, 400)
        self.assertIn("P3004_WIRELESS_WDA_NOT_READY", str(context.exception.detail))
        self.assertIsNone(self._scoped_setting("ios-1"))

    def test_enable_wireless_rejects_when_ip_unavailable(self):
        _add_device(self.session, "ios-1", "ios")
        with patch(
            "backend.api.devices._check_ios_wda_health", return_value=dict(_HEALTHY)
        ), patch(
            "backend.api.devices._fetch_ios_device_ip_from_wda", return_value=None
        ):
            with self.assertRaises(HTTPException) as context:
                enable_ios_wireless("ios-1", session=self.session, current_user=None)

        self.assertEqual(context.exception.status_code, 400)
        self.assertIn("P3005_WIRELESS_IP_UNAVAILABLE", str(context.exception.detail))
        self.assertIsNone(self._scoped_setting("ios-1"))

    def test_enable_wireless_rejects_when_direct_unreachable(self):
        _add_device(self.session, "ios-1", "ios")
        with patch(
            "backend.api.devices._check_ios_wda_health", return_value=dict(_HEALTHY)
        ), patch(
            "backend.api.devices._fetch_ios_device_ip_from_wda", return_value="192.168.1.23"
        ), patch(
            "backend.cross_platform_execution.check_wda_health",
            side_effect=RuntimeError("P1005_WDA_UNAVAILABLE: connect timeout"),
        ):
            with self.assertRaises(HTTPException) as context:
                enable_ios_wireless("ios-1", session=self.session, current_user=None)

        self.assertEqual(context.exception.status_code, 400)
        self.assertIn("P3006_WIRELESS_DIRECT_UNREACHABLE", str(context.exception.detail))
        self.assertIsNone(self._scoped_setting("ios-1"))

    def test_enable_wireless_rejects_non_ios_device(self):
        _add_device(self.session, "android-1", "android")
        with self.assertRaises(HTTPException) as context:
            enable_ios_wireless("android-1", session=self.session, current_user=None)
        self.assertEqual(context.exception.status_code, 400)
        self.assertIn("P3003_WIRELESS_IOS_ONLY", str(context.exception.detail))

    def test_enable_wireless_rejects_invalid_params(self):
        _add_device(self.session, "ios-1", "ios")
        bad_ip = DeviceWirelessEnableRequest(ip="192.168.1.5/evil")
        with self.assertRaises(HTTPException) as context:
            enable_ios_wireless("ios-1", req=bad_ip, session=self.session, current_user=None)
        self.assertEqual(context.exception.status_code, 400)
        self.assertIn("P3007_WIRELESS_INVALID_PARAM", str(context.exception.detail))

        bad_port = DeviceWirelessEnableRequest(port=0)
        with self.assertRaises(HTTPException) as context:
            enable_ios_wireless("ios-1", req=bad_port, session=self.session, current_user=None)
        self.assertEqual(context.exception.status_code, 400)
        self.assertIn("P3007_WIRELESS_INVALID_PARAM", str(context.exception.detail))
        self.assertIsNone(self._scoped_setting("ios-1"))

    def test_disable_wireless_removes_setting_and_is_idempotent(self):
        _add_device(self.session, "ios-1", "ios", status="IDLE")
        set_setting_value(self.session, "ios_wda_url.ios-1", "http://192.168.1.23:8100")

        with patch(
            "backend.api.devices._check_ios_wda_health", return_value=dict(_UNHEALTHY)
        ):
            payload = disable_ios_wireless("ios-1", session=self.session, current_user=None)

        self.assertFalse(payload["wireless_enabled"])
        self.assertTrue(payload["removed"])
        self.assertEqual(payload["status"], "WDA_DOWN")
        self.assertIsNone(self._scoped_setting("ios-1"))

        with patch(
            "backend.api.devices._check_ios_wda_health", return_value=dict(_HEALTHY)
        ):
            payload = disable_ios_wireless("ios-1", session=self.session, current_user=None)

        self.assertFalse(payload["removed"])
        self.assertEqual(payload["status"], "IDLE")

    def test_get_wireless_url_map_ignores_local_urls(self):
        set_setting_value(self.session, "ios_wda_url.ios-remote", "http://192.168.1.23:8100")
        set_setting_value(self.session, "ios_wda_url.ios-local", "http://127.0.0.1:8201")
        set_setting_value(self.session, "ios_wda_url", "http://192.168.9.9:8100")  # 全局键不带 serial

        mapping = _get_wireless_url_map(self.session)
        self.assertEqual(mapping, {"ios-remote": "http://192.168.1.23:8100"})


class WirelessSyncSemanticsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.engine, self.session = _make_session()

    def tearDown(self) -> None:
        self.session.close()

    async def _run_sync(self):
        with patch(
            "backend.api.devices._run_adb_command",
            return_value=b"List of devices attached\n\n",
        ), patch("backend.ios_scanner.get_ios_devices", return_value=[]):
            return await sync_devices(session=self.session, current_user=None)

    async def test_sync_keeps_wireless_device_online_when_absent_from_usbmux(self):
        _add_device(self.session, "ios-1", "ios", status="IDLE")
        set_setting_value(self.session, "ios_wda_url.ios-1", "http://192.168.1.23:8100")

        with patch(
            "backend.api.devices._is_ios_wda_healthy", return_value=True
        ) as health_mock:
            response = await self._run_sync()

        health_mock.assert_called_once_with(self.session, "ios-1")
        device = self.session.exec(select(Device).where(Device.serial == "ios-1")).first()
        self.assertEqual(device.status, "IDLE")
        self.assertEqual(response.online, 1)
        payload = next(d for d in response.devices if d.serial == "ios-1")
        self.assertTrue(payload.wireless_enabled)

    async def test_sync_marks_wireless_device_offline_when_unreachable(self):
        _add_device(self.session, "ios-1", "ios", status="IDLE")
        set_setting_value(self.session, "ios_wda_url.ios-1", "http://192.168.1.23:8100")

        with patch("backend.api.devices._is_ios_wda_healthy", return_value=False):
            response = await self._run_sync()

        device = self.session.exec(select(Device).where(Device.serial == "ios-1")).first()
        self.assertEqual(device.status, "OFFLINE")
        self.assertEqual(response.online, 0)

    async def test_sync_marks_plain_ios_device_offline_without_probe(self):
        _add_device(self.session, "ios-1", "ios", status="IDLE")

        with patch("backend.api.devices._is_ios_wda_healthy") as health_mock:
            await self._run_sync()

        health_mock.assert_not_called()
        device = self.session.exec(select(Device).where(Device.serial == "ios-1")).first()
        self.assertEqual(device.status, "OFFLINE")


if __name__ == "__main__":
    unittest.main()
