import base64
import gzip
import hashlib
import io
import json
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from backend.api.recording import (
    _RecordingAndroidDevicePool,
    _RecordingIOSSessionPool,
    _build_device_dump_payload,
    _get_device_hierarchy_xml,
    _get_recording_post_action_delay,
    _json_response_with_optional_gzip,
    _perform_device_operation,
    _take_screenshot_base64,
    _take_screenshot_payload,
    _wait_ui_stable,
)
from backend.schemas import InteractionRequest


class JsonGzipResponseTests(unittest.TestCase):
    @staticmethod
    def _request(accept_encoding=""):
        request = Mock()
        request.headers = {"accept-encoding": accept_encoding}
        return request

    def test_large_payload_gzipped_when_accepted(self):
        payload = {"hierarchy_xml": "<node text='x' />" * 600}
        response = _json_response_with_optional_gzip(self._request("gzip, deflate"), payload)

        self.assertEqual(response.headers.get("content-encoding"), "gzip")
        self.assertEqual(json.loads(gzip.decompress(response.body)), payload)
        self.assertLess(len(response.body), len(json.dumps(payload)))

    def test_plain_when_client_does_not_accept_gzip(self):
        payload = {"hierarchy_xml": "<node text='x' />" * 600}
        response = _json_response_with_optional_gzip(self._request(""), payload)

        self.assertIsNone(response.headers.get("content-encoding"))
        self.assertEqual(json.loads(response.body), payload)

    def test_small_payload_not_gzipped(self):
        response = _json_response_with_optional_gzip(self._request("gzip"), {"ok": True})

        self.assertIsNone(response.headers.get("content-encoding"))
        self.assertEqual(json.loads(response.body), {"ok": True})


class DeviceRecordingHelperTests(unittest.TestCase):
    def test_take_screenshot_base64_accepts_png_bytes(self):
        raw_png = b"\x89PNG\r\n\x1a\nfake-payload"
        device = Mock()
        device.screenshot.return_value = raw_png

        encoded = _take_screenshot_base64(device)

        self.assertEqual(encoded, base64.b64encode(raw_png).decode("utf-8"))

    def test_take_screenshot_payload_marks_jpeg_bytes(self):
        raw_jpeg = b"\xff\xd8\xff\xe0fake-jpeg"
        device = Mock()
        device.screenshot.return_value = raw_jpeg

        encoded, image_format = _take_screenshot_payload(device)

        self.assertEqual(encoded, base64.b64encode(raw_jpeg).decode("utf-8"))
        self.assertEqual(image_format, "jpeg")

    def test_take_screenshot_payload_encodes_pil_image_as_jpeg(self):
        device = Mock()
        device.screenshot.return_value = Image.new("RGBA", (32, 64), (255, 0, 0, 255))

        encoded, image_format = _take_screenshot_payload(device)

        self.assertEqual(image_format, "jpeg")
        decoded = Image.open(io.BytesIO(base64.b64decode(encoded)))
        self.assertEqual(decoded.format, "JPEG")
        # 原分辨率不缩放：截图是图像模板裁剪素材源
        self.assertEqual(decoded.size, (32, 64))

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

        with patch("backend.api.recording.check_wda_health") as health_check, patch(
            "backend.api.recording.IOSDriver",
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
        # d78f30d 起固定等待改为“短等待 + 截图轮询”：iOS 点击最快，Android 通用 0.2s
        self.assertLess(
            _get_recording_post_action_delay("ios", "click"),
            _get_recording_post_action_delay("android", "click"),
        )
        self.assertEqual(_get_recording_post_action_delay("android", "click"), 0.2)

    def test_network_device_settle_skips_extra_screenshot_hashes(self):
        device = Mock()

        with patch("backend.api.recording.time.sleep") as sleep:
            _wait_ui_stable(
                device,
                platform="android",
                operation="click",
                serial="192.168.1.8:5555",
            )

        sleep.assert_called_once_with(0.2)
        device.screenshot.assert_not_called()

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
            "screenshot_format": "png",
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

    def test_build_device_dump_payload_parallel_path_produces_full_payload(self):
        raw_png = b"\x89PNG\r\n\x1a\nfast"
        device = Mock()
        device.info = {"serial": "android-1"}
        device.dump_hierarchy.return_value = "<hierarchy />"
        device.screenshot.return_value = raw_png

        payload = _build_device_dump_payload(
            device,
            platform="android",
            serial="android-1",
            include_device_info=True,
            include_hierarchy=True,
            include_screenshot=True,
        )

        self.assertEqual(payload["device_info"], {"serial": "android-1"})
        self.assertEqual(payload["hierarchy_xml"], "<hierarchy />")
        self.assertEqual(payload["screenshot"], base64.b64encode(raw_png).decode("utf-8"))
        self.assertEqual(payload["screenshot_format"], "png")

    def test_build_device_dump_payload_parallel_propagates_hierarchy_error(self):
        device = Mock()
        device.dump_hierarchy.side_effect = RuntimeError("uiautomator down")
        device.screenshot.return_value = b"\x89PNG\r\n\x1a\nfast"

        with self.assertRaises(RuntimeError):
            _build_device_dump_payload(
                device,
                platform="android",
                serial="android-1",
                include_device_info=False,
                include_hierarchy=True,
                include_screenshot=True,
            )


class RecordingAndroidDevicePoolTests(unittest.TestCase):
    def test_acquire_reuses_cached_device_per_serial(self):
        pool = _RecordingAndroidDevicePool()
        device = Mock()
        with patch("backend.api.recording.u2.connect", return_value=device) as connect:
            first = pool.acquire("android-1")
            second = pool.acquire("android-1")

        self.assertIs(first, device)
        self.assertIs(second, device)
        connect.assert_called_once_with("android-1")

    def test_invalidate_forces_reconnect(self):
        pool = _RecordingAndroidDevicePool()
        with patch("backend.api.recording.u2.connect", side_effect=[Mock(), Mock()]) as connect:
            first = pool.acquire("android-1")
            pool.invalidate("android-1", first)
            second = pool.acquire("android-1")

        self.assertIsNot(first, second)
        self.assertEqual(connect.call_count, 2)

    def test_invalidate_ignores_stale_device_reference(self):
        pool = _RecordingAndroidDevicePool()
        with patch("backend.api.recording.u2.connect", return_value=Mock()) as connect:
            current = pool.acquire("android-1")
            pool.invalidate("android-1", Mock())  # 非缓存中的对象，不应误伤
            again = pool.acquire("android-1")

        self.assertIs(current, again)
        connect.assert_called_once()

    def test_stale_entries_evicted_after_ttl(self):
        pool = _RecordingAndroidDevicePool()
        with patch("backend.api.recording.u2.connect", side_effect=[Mock(), Mock()]) as connect:
            with patch("backend.api.recording.time.time", return_value=1000.0):
                pool.acquire("android-1")
            # TTL(300s) 之后再取：旧条目被回收，重新连接
            with patch("backend.api.recording.time.time", return_value=1000.0 + 301.0):
                pool.acquire("android-1")

        self.assertEqual(connect.call_count, 2)


if __name__ == "__main__":
    unittest.main()
