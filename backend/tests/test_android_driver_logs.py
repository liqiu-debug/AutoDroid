import unittest
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

    def test_click_image_never_substitutes_global_confirmation_text(self):
        driver = self._new_driver()
        driver._template_confirm_text_hint = Mock(return_value="确定")
        driver.click = Mock()
        driver._resolve_template_image_path = Mock(return_value="offline.png")
        evidence = {"status": "matched", "point": [10, 20]}
        with patch("backend.drivers.android_driver.locate_stable_image", return_value=evidence), patch("backend.drivers.android_driver.android_tap_once") as tap:
            AndroidDriver.click_image(driver, "offline.png")
        driver._template_confirm_text_hint.assert_not_called()
        driver.click.assert_not_called()
        driver._device.image.wait.assert_not_called()
        self.assertEqual(tap.call_args.args[1:3], (10, 20))
        driver._device.click.assert_not_called()

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

    def test_click_image_uses_verified_anchor_and_returns_evidence(self):
        driver = self._new_driver()
        driver._resolve_template_image_path = Mock(return_value="offline.png")
        evidence = {"status": "matched", "point": [10.9, 20.1]}
        options = {"template_sha256": "a" * 64, "tap_anchor": [.2, .4]}
        with patch("backend.drivers.android_driver.locate_stable_image", return_value=evidence) as locate, patch("backend.drivers.android_driver.android_tap_once") as tap:
            result = AndroidDriver.click_image(driver, "offline.png", image_options=options, timeout=3)
        locate.assert_called_once()
        self.assertEqual(locate.call_args.args[1:], ("offline.png", options, 3))
        self.assertEqual(result, {"image_match": evidence})
        self.assertEqual(tap.call_count, 1)
        self.assertEqual(tap.call_args.args[1:3], (10.9, 20.1))

    def test_click_image_does_not_repeat_tap_when_page_signature_is_unchanged(self):
        driver = self._new_driver()
        driver._resolve_template_image_path = Mock(return_value="offline.png")
        driver._capture_page_signature_quick = Mock(return_value="unchanged")
        with patch("backend.drivers.android_driver.locate_stable_image", return_value={"point": [30, 40]}), patch("backend.drivers.android_driver.android_tap_once") as tap:
            AndroidDriver.click_image(driver, "offline.png")
        self.assertEqual(tap.call_count, 1)
        driver._capture_page_signature_quick.assert_not_called()

    def test_click_image_unknown_tap_result_marks_submission_and_never_repeats(self):
        driver = self._new_driver()
        driver._resolve_template_image_path = Mock(return_value="offline.png")
        with patch("backend.drivers.android_driver.locate_stable_image", return_value={"point": [30, 40]}), patch("backend.drivers.android_driver.android_tap_once", side_effect=RuntimeError("transport failed")) as tap:
            with self.assertRaises(RuntimeError) as error:
                AndroidDriver.click_image(driver, "offline.png")
        self.assertTrue(error.exception.context["tap_submitted"])
        self.assertEqual(tap.call_count, 1)


if __name__ == "__main__":
    unittest.main()
