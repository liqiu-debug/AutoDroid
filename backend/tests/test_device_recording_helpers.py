import base64
import hashlib
import unittest
from unittest.mock import Mock, patch

from backend.main import (
    _RecordingIOSSessionPool,
    _build_device_dump_payload,
    _get_device_hierarchy_xml,
    _get_recording_post_action_delay,
    _perform_device_operation,
    _take_screenshot_base64,
)
from backend.schemas import InteractionRequest


class DeviceRecordingHelperTests(unittest.TestCase):
    def test_take_screenshot_base64_accepts_png_bytes(self):
        raw_png = b"\x89PNG\r\n\x1a\nfake-payload"
        device = Mock()
        device.screenshot.return_value = raw_png

        encoded = _take_screenshot_base64(device)

        self.assertEqual(encoded, base64.b64encode(raw_png).decode("utf-8"))

    def test_get_device_hierarchy_xml_reads_ios_source(self):
        driver = Mock()
        session = Mock()
        driver.client.session.return_value = session
        session.source.return_value = "<AppiumAUT><XCUIElementTypeApplication /></AppiumAUT>"

        xml = _get_device_hierarchy_xml(driver, platform="ios")

        self.assertIn("XCUIElementTypeApplication", xml)

    def test_perform_device_operation_dispatches_ios_click_by_coordinates(self):
        driver = Mock()
        req = InteractionRequest(
            x=128,
            y=256,
            operation="click",
            action_data=None,
            xml_dump=None,
            device_serial="ios-1",
            record_step=True,
        )

        _perform_device_operation(driver, platform="ios", req=req)

        driver.click_by_coordinates.assert_called_once_with(128, 256)

    def test_recording_ios_session_pool_reuses_driver(self):
        pool = _RecordingIOSSessionPool()
        driver = Mock()

        with patch("backend.main.check_wda_health") as health_check, patch(
            "backend.main.IOSDriver",
            return_value=driver,
        ) as driver_cls:
            first = pool.acquire("ios-1", "http://127.0.0.1:8200")
            pool.release("ios-1", first)

            second = pool.acquire("ios-1", "http://127.0.0.1:8200")
            pool.release("ios-1", second)

        self.assertIs(first, driver)
        self.assertIs(second, driver)
        health_check.assert_called_once_with("http://127.0.0.1:8200")
        driver_cls.assert_called_once_with(device_id="ios-1", wda_url="http://127.0.0.1:8200")

        pool.close_all()
        driver.disconnect.assert_called_once()

    def test_recording_post_action_delay_prefers_faster_ios_clicks(self):
        self.assertLess(_get_recording_post_action_delay("ios", "click"), 1.0)
        self.assertEqual(_get_recording_post_action_delay("android", "click"), 1.0)

    def test_build_device_dump_payload_can_skip_optional_parts(self):
        raw_png = b"\x89PNG\r\n\x1a\nfast"
        device = Mock()
        device.screenshot.return_value = raw_png

        payload = _build_device_dump_payload(
            device,
            platform="android",
            serial="android-1",
            include_device_info=False,
            include_hierarchy=False,
            include_screenshot=True,
        )

        self.assertEqual(payload, {
            "screenshot": base64.b64encode(raw_png).decode("utf-8"),
        })
        device.dump_hierarchy.assert_not_called()

    def test_build_device_dump_payload_adds_hierarchy_hash(self):
        device = Mock()
        device.dump_hierarchy.return_value = "<hierarchy><node text='hello' /></hierarchy>"

        payload = _build_device_dump_payload(
            device,
            platform="android",
            serial="android-1",
            include_device_info=False,
            include_hierarchy=True,
            include_screenshot=False,
        )

        expected_xml = "<hierarchy><node text='hello' /></hierarchy>"
        self.assertEqual(payload["hierarchy_xml"], expected_xml)
        self.assertEqual(
            payload["hierarchy_hash"],
            hashlib.sha1(expected_xml.encode("utf-8")).hexdigest(),
        )


if __name__ == "__main__":
    unittest.main()
