import unittest
from unittest.mock import patch

from fastapi import HTTPException

from backend.api.devices import (
    _ensure_android_device,
    _resolve_ios_device_status,
    list_wda_relays,
)
from backend.api.fastbot import _ensure_fastbot_android_device
from backend.api.packages import _ensure_android_install_device
from backend.models import Device


class DevicePlatformGuardTests(unittest.TestCase):
    def test_android_device_allowed(self):
        device = Device(serial="android-1", platform="android", model="Pixel")
        _ensure_android_device(device, action="截图")

    def test_ios_device_rejected(self):
        device = Device(serial="ios-1", platform="ios", model="iPhone")
        with self.assertRaises(HTTPException) as context:
            _ensure_android_device(device, action="截图")
        self.assertEqual(context.exception.status_code, 400)
        self.assertIn("仅支持 Android 设备", str(context.exception.detail))

    def test_ios_device_rejected_for_apk_install(self):
        device = Device(serial="ios-1", platform="ios", model="iPhone")
        with self.assertRaises(HTTPException) as context:
            _ensure_android_install_device(device)
        self.assertEqual(context.exception.status_code, 400)
        self.assertIn("APK 安装仅支持 Android", str(context.exception.detail))

    def test_ios_device_rejected_for_fastbot(self):
        device = Device(serial="ios-1", platform="ios", model="iPhone")
        with self.assertRaises(HTTPException) as context:
            _ensure_fastbot_android_device(device)
        self.assertEqual(context.exception.status_code, 400)
        self.assertIn("Fastbot 仅支持 Android", str(context.exception.detail))

    def test_ios_busy_status_is_not_overridden(self):
        self.assertEqual(_resolve_ios_device_status("BUSY", wda_healthy=True), "BUSY")
        self.assertEqual(_resolve_ios_device_status("BUSY", wda_healthy=False), "BUSY")

    def test_ios_status_maps_to_wda_down_when_unhealthy(self):
        self.assertEqual(_resolve_ios_device_status("IDLE", wda_healthy=False), "WDA_DOWN")
        self.assertEqual(_resolve_ios_device_status("OFFLINE", wda_healthy=False), "WDA_DOWN")

    def test_ios_status_maps_to_idle_when_healthy(self):
        self.assertEqual(_resolve_ios_device_status("OFFLINE", wda_healthy=True), "IDLE")
        self.assertEqual(_resolve_ios_device_status(None, wda_healthy=True), "IDLE")

    def test_list_wda_relays_wraps_manager_result(self):
        mock_items = [{"udid": "ios-1", "local_port": 8201, "remote_port": 8100, "alive": True}]
        with patch("backend.api.devices.wda_relay_manager.list_relays", return_value=mock_items):
            payload = list_wda_relays()
        self.assertEqual(payload, {"items": mock_items})


if __name__ == "__main__":
    unittest.main()
