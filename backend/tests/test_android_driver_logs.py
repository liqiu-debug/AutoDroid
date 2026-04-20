import unittest
import itertools
import os
import tempfile
from unittest.mock import Mock, patch

from backend.drivers.android_driver import AndroidDriver


class _MissingElement:
    def exists(self, timeout: int = 0):  # noqa: ARG002
        return False


class AndroidDriverLogTests(unittest.TestCase):
    def _new_driver(self) -> AndroidDriver:
        driver = AndroidDriver.__new__(AndroidDriver)
        driver.device_id = "android-1"
        driver._device = Mock()
        return driver

    def test_click_logs_failure_when_element_missing(self):
        driver = self._new_driver()
        driver._find_element = Mock(return_value=_MissingElement())

        with self.assertLogs("backend.drivers.android_driver", level="WARNING") as logs:
            with self.assertRaises(RuntimeError):
                AndroidDriver.click(driver, selector="登录", by="text")

        output = "\n".join(logs.output)
        self.assertIn("Android.click failed", output)
        self.assertIn("category=ELEMENT_NOT_FOUND", output)

    def test_click_by_coordinates_logs_success_and_uses_int_pixels(self):
        driver = self._new_driver()

        with self.assertLogs("backend.drivers.android_driver", level="INFO") as logs:
            AndroidDriver.click_by_coordinates(driver, 10.9, 20.1)

        driver._device.click.assert_called_once_with(10, 20)
        self.assertIn("Android.click_by_coordinates success", "\n".join(logs.output))

    def test_input_focused_failure_logs_text_length_instead_of_plain_text(self):
        driver = self._new_driver()

        def _device_call(**kwargs):
            if kwargs.get("focused"):
                return _MissingElement()
            return Mock()

        driver._device.side_effect = _device_call
        driver._device.send_keys.side_effect = [
            RuntimeError("clear send_keys failed"),
            RuntimeError("plain send_keys failed"),
        ]

        with self.assertLogs("backend.drivers.android_driver", level="WARNING") as logs:
            with self.assertRaises(RuntimeError):
                AndroidDriver.input_focused(driver, text="secret123")

        output = "\n".join(logs.output)
        self.assertIn("Android.input_focused failed", output)
        self.assertIn("text_len='9'", output)
        self.assertNotIn("secret123", output)

    def test_click_image_prefers_text_hint_for_confirm_template(self):
        driver = self._new_driver()
        driver._device.image = Mock()
        driver._template_confirm_text_hint = Mock(return_value="确定")
        driver.click = Mock(return_value=None)

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            AndroidDriver.click_image(driver, tmp_path)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

        driver.click.assert_called_once_with(selector="确定", by="text")
        driver._device.image.wait.assert_not_called()

    def test_assert_text_matches_joined_page_text_when_target_sequence_appears_late(self):
        driver = self._new_driver()
        driver._collect_page_text_candidates = Mock(
            return_value=["购物车", "订单", "购物车", "支付成功"]
        )

        with self.assertLogs("backend.drivers.android_driver", level="INFO") as logs:
            AndroidDriver.assert_text(
                driver,
                expected_text="购物车支付成功",
                match_mode="contains",
            )

        output = "\n".join(logs.output)
        self.assertIn("Android.assert_text success", output)
        self.assertIn("match_source='page_joined'", output)

    def test_assert_text_not_contains_fails_when_joined_page_text_matches_late_sequence(self):
        driver = self._new_driver()
        driver._collect_page_text_candidates = Mock(
            return_value=["购物车", "订单", "购物车", "支付成功"]
        )

        with self.assertLogs("backend.drivers.android_driver", level="WARNING") as logs:
            with self.assertRaises(AssertionError) as context:
                AndroidDriver.assert_text(
                    driver,
                    expected_text="购物车支付成功",
                    match_mode="not_contains",
                )

        self.assertIn("购物车订单购物车支付成功", str(context.exception))
        self.assertIn("Android.assert_text failed", "\n".join(logs.output))

    def test_click_image_uses_template_wait_and_click_point(self):
        driver = self._new_driver()
        driver._device.image = Mock()
        driver._device.image.wait.return_value = {"point": [10.9, 20.1], "similarity": 0.96}
        driver._template_confirm_text_hint = Mock(return_value="")
        driver._capture_page_signature_quick = Mock(return_value="sig")

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            AndroidDriver.click_image(driver, tmp_path)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

        driver._device.image.wait.assert_called_once_with(tmp_path, timeout=5, threshold=0.9)
        driver._device.click.assert_called_once_with(10, 20)

    def test_click_image_retries_once_when_match_slow_and_page_unchanged(self):
        driver = self._new_driver()
        driver._device.image = Mock()
        driver._device.image.wait.return_value = {"point": [30.0, 40.0], "similarity": 0.95}
        driver._template_confirm_text_hint = Mock(return_value="")
        driver._capture_page_signature_quick = Mock(side_effect=["sig-a", "sig-a", "sig-b"])

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            mock_times = itertools.chain([1000.0, 1001.0, 1004.5, 1005.0], itertools.repeat(1005.0))
            with patch(
                "backend.drivers.android_driver.time.time",
                side_effect=lambda: next(mock_times),
            ):
                AndroidDriver.click_image(driver, tmp_path)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

        self.assertEqual(driver._device.click.call_count, 2)

    def test_click_image_raises_when_retry_still_has_no_effect(self):
        driver = self._new_driver()
        driver._device.image = Mock()
        driver._device.image.wait.return_value = {"point": [30.0, 40.0], "similarity": 0.95}
        driver._template_confirm_text_hint = Mock(return_value="")
        driver._capture_page_signature_quick = Mock(side_effect=["sig-a", "sig-a", "sig-a"])

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            mock_times = itertools.chain([1000.0, 1001.0, 1004.5, 1005.0], itertools.repeat(1005.0))
            with patch(
                "backend.drivers.android_driver.time.time",
                side_effect=lambda: next(mock_times),
            ):
                with self.assertRaises(RuntimeError) as context:
                    AndroidDriver.click_image(driver, tmp_path)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

        self.assertIn("tap-no-effect", str(context.exception))
        self.assertEqual(driver._device.click.call_count, 2)


if __name__ == "__main__":
    unittest.main()
