import unittest
from unittest.mock import patch

from backend.ios_scanner import (
    IOSDeviceScanner,
    get_ios_devices,
    get_usbmux_connection_type,
)


class _FakeDeviceInfo:
    def __init__(self, udid: str, conn_type: str) -> None:
        self.udid = udid
        self.conn_type = conn_type


class _FakeUsbmux:
    def __init__(self, entries):
        self._entries = entries

    def device_list(self):
        return self._entries


class IOSScannerConnTypeTests(unittest.TestCase):
    def test_normalize_conn_type(self):
        self.assertEqual(IOSDeviceScanner._normalize_conn_type("USB"), "usb")
        self.assertEqual(IOSDeviceScanner._normalize_conn_type("network"), "network")
        self.assertIsNone(IOSDeviceScanner._normalize_conn_type("bluetooth"))
        self.assertIsNone(IOSDeviceScanner._normalize_conn_type(None))

    def test_get_online_devices_includes_connection_type(self):
        entries = [
            _FakeDeviceInfo("udid-usb", "usb"),
            _FakeDeviceInfo("udid-net", "network"),
        ]

        def _fake_fetch(_self, udid):
            return {
                "device_id": udid,
                "platform": "ios",
                "name": udid,
                "model": "iPhone",
                "os_version": "17.1",
            }

        with patch("backend.ios_scanner.Usbmux", return_value=_FakeUsbmux(entries)), patch.object(
            IOSDeviceScanner, "_fetch_device_info", _fake_fetch
        ):
            devices = get_ios_devices()

        conn_by_udid = {d["device_id"]: d["connection_type"] for d in devices}
        self.assertEqual(conn_by_udid, {"udid-usb": "usb", "udid-net": "network"})

    def test_get_usbmux_connection_type_matches_udid(self):
        entries = [
            _FakeDeviceInfo("udid-usb", "usb"),
            _FakeDeviceInfo("udid-net", "network"),
        ]
        with patch("backend.ios_scanner.Usbmux", return_value=_FakeUsbmux(entries)):
            self.assertEqual(get_usbmux_connection_type("udid-net"), "network")
            self.assertEqual(get_usbmux_connection_type("udid-usb"), "usb")
            self.assertIsNone(get_usbmux_connection_type("udid-missing"))

    def test_get_usbmux_connection_type_returns_none_on_error(self):
        with patch("backend.ios_scanner.Usbmux", side_effect=RuntimeError("no usbmuxd")):
            self.assertIsNone(get_usbmux_connection_type("udid-usb"))


if __name__ == "__main__":
    unittest.main()
