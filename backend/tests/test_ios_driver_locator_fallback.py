import unittest
import base64
from unittest.mock import Mock, patch

from backend.drivers.ios_driver import IOSDriver


class _FakeSelector:
    def __init__(self, exists: bool, element=None, get_error: Exception = None):
        self._exists = exists
        self._element = element if element is not None else Mock()
        self._get_error = get_error

    def wait(self, timeout: int = 5, raise_error: bool = False):  # noqa: ARG002
        return self._exists

    @property
    def exists(self):
        return self._exists

    def get(self, timeout: int = 5, raise_error: bool = True):  # noqa: ARG002
        if self._get_error is not None:
            raise self._get_error
        if not self._exists:
            raise RuntimeError("not found")
        return self._element


class _FakeSession:
    def __init__(self, mapping):
        self._mapping = mapping
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if not kwargs:
            return _FakeSelector(False)
        key, value = next(iter(kwargs.items()))
        return self._mapping.get((key, value), _FakeSelector(False))


class IOSDriverLocatorFallbackTests(unittest.TestCase):
    def _new_driver(self) -> IOSDriver:
        driver = IOSDriver.__new__(IOSDriver)
        driver.device_id = "ios-1"
        driver.client = Mock()
        driver.scale = 3.0
        return driver

    def test_get_element_text_falls_back_to_predicate_contains(self):
        driver = self._new_driver()
        element = Mock()
        predicate = IOSDriver._build_contains_predicate(driver, "请输入手机号")
        session = _FakeSession(
            {
                ("label", "请输入手机号"): _FakeSelector(False),
                ("name", "请输入手机号"): _FakeSelector(False),
                ("predicate", predicate): _FakeSelector(True, element=element),
            }
        )
        driver.client.session.return_value = session

        found = IOSDriver._get_element(driver, "请输入手机号", "text", timeout=1)

        self.assertIs(found, element)
        self.assertIn({"label": "请输入手机号"}, session.calls)
        self.assertIn({"name": "请输入手机号"}, session.calls)
        self.assertIn({"predicate": predicate}, session.calls)

    def test_wait_until_exists_uses_same_fallback_chain(self):
        driver = self._new_driver()
        predicate = IOSDriver._build_contains_predicate(driver, "账号")
        session = _FakeSession(
            {
                ("label", "账号"): _FakeSelector(False),
                ("name", "账号"): _FakeSelector(False),
                ("predicate", predicate): _FakeSelector(True),
            }
        )
        driver.client.session.return_value = session

        IOSDriver.wait_until_exists(driver, "账号", "text", timeout=1)

        self.assertIn({"predicate": predicate}, session.calls)

    def test_get_element_id_falls_back_to_label_name_predicate(self):
        driver = self._new_driver()
        element = Mock()
        predicate = IOSDriver._build_contains_predicate(driver, "商品标题")
        session = _FakeSession(
            {
                ("id", "商品标题"): _FakeSelector(False),
                ("label", "商品标题"): _FakeSelector(True, element=element),
                ("name", "商品标题"): _FakeSelector(False),
                ("predicate", predicate): _FakeSelector(False),
            }
        )
        driver.client.session.return_value = session

        found = IOSDriver._get_element(driver, "商品标题", "id", timeout=1)

        self.assertIs(found, element)
        self.assertIn({"id": "商品标题"}, session.calls)
        self.assertIn({"label": "商品标题"}, session.calls)

    def test_get_element_reports_attempt_chain_when_all_failed(self):
        driver = self._new_driver()
        session = _FakeSession({})
        driver.client.session.return_value = session

        with self.assertRaises(RuntimeError) as context:
            IOSDriver._get_element(driver, "不存在", "text", timeout=1)

        self.assertIn("attempts=", str(context.exception))
        self.assertIn("predicate:not-found", str(context.exception))

    def test_click_fallbacks_to_alert_button_when_primary_locator_fails(self):
        driver = self._new_driver()
        driver._has_alert_or_sheet = Mock(return_value=False)
        driver._get_element = Mock(side_effect=RuntimeError("not-found"))
        driver._tap_alert_button = Mock(return_value=True)
        driver._tap_by_ocr_text = Mock(return_value=False)

        IOSDriver.click(driver, "允许", "text")

        driver._tap_alert_button.assert_called_once()
        driver._tap_by_ocr_text.assert_not_called()

    def test_click_fallbacks_to_ocr_when_alert_not_found(self):
        driver = self._new_driver()
        driver._has_alert_or_sheet = Mock(return_value=False)
        driver._get_element = Mock(side_effect=RuntimeError("not-found"))
        driver._tap_alert_button = Mock(return_value=False)
        driver._tap_by_ocr_text = Mock(return_value=True)

        IOSDriver.click(driver, "允许", "text")

        driver._tap_alert_button.assert_called_once()
        driver._tap_by_ocr_text.assert_called_once()

    def test_click_fallbacks_to_ocr_for_id_locator_when_primary_fails(self):
        driver = self._new_driver()
        driver._has_alert_or_sheet = Mock(return_value=False)
        driver._get_element = Mock(side_effect=RuntimeError("not-found"))
        driver._tap_alert_button = Mock(return_value=False)
        driver._tap_by_ocr_text = Mock(return_value=True)

        IOSDriver.click(driver, "确定", "id")

        driver._tap_alert_button.assert_called_once()
        driver._tap_by_ocr_text.assert_called_once()

    def test_click_raises_when_all_fallbacks_failed(self):
        driver = self._new_driver()
        driver._has_alert_or_sheet = Mock(return_value=False)
        driver._get_element = Mock(side_effect=RuntimeError("not-found"))
        driver._tap_alert_button = Mock(return_value=False)
        driver._tap_by_ocr_text = Mock(return_value=False)

        with self.assertRaises(RuntimeError) as context:
            IOSDriver.click(driver, "允许", "text")

        self.assertIn("fallback=", str(context.exception))

    def test_click_prioritizes_alert_button_when_alert_exists(self):
        driver = self._new_driver()
        driver._has_alert_or_sheet = Mock(return_value=True)
        driver._tap_alert_button = Mock(return_value=True)
        driver._get_element = Mock()
        driver._tap_by_ocr_text = Mock(return_value=False)

        IOSDriver.click(driver, "允许", "text")

        driver._tap_alert_button.assert_called_once()
        driver._get_element.assert_not_called()

    def test_click_requires_effect_for_confirm_text_then_fallback(self):
        driver = self._new_driver()
        element = Mock()
        element.tap = Mock(return_value=None)
        driver._has_alert_or_sheet = Mock(return_value=False)
        driver._get_element = Mock(return_value=element)
        driver._capture_page_signature = Mock(return_value="sig-a")
        driver._wait_page_changed = Mock(return_value=False)
        driver._tap_alert_button = Mock(return_value=False)
        driver._tap_by_ocr_text = Mock(return_value=True)

        IOSDriver.click(driver, "确定", "text")

        driver._tap_by_ocr_text.assert_called_once()

    def test_click_locator_once_skips_effect_signature_when_element_missing(self):
        driver = self._new_driver()
        driver._build_selector = Mock(return_value=_FakeSelector(False))
        driver._wait_selector = Mock(return_value=False)
        driver._capture_page_signature = Mock(return_value="sig")

        with self.assertRaises(RuntimeError):
            IOSDriver._click_locator_once(driver, "确定", "id", timeout=0.5)

        driver._capture_page_signature.assert_not_called()

    def test_click_locator_once_rejects_false_positive_signature_change(self):
        driver = self._new_driver()
        element = Mock()
        element.tap = Mock(return_value=None)
        driver._build_selector = Mock(return_value=_FakeSelector(True, element=element))
        driver._wait_selector = Mock(return_value=True)
        driver._has_alert_or_sheet = Mock(side_effect=[True, True])
        driver._capture_page_signature = Mock(return_value="sig-a")
        driver._wait_page_changed = Mock(return_value=True)
        driver._is_alert_button_present = Mock(return_value=True)
        driver._is_selector_present = Mock(return_value=True)

        with self.assertRaises(RuntimeError) as context:
            IOSDriver._click_locator_once(driver, "确定", "id", timeout=0.5)

        self.assertIn("tap-no-effect", str(context.exception))

    def test_wait_confirm_click_effect_blocks_signature_noise_when_alert_still_visible(self):
        driver = self._new_driver()
        driver._wait_page_changed = Mock(return_value=True)
        driver._has_alert_or_sheet = Mock(return_value=True)
        driver._is_alert_button_present = Mock(return_value=True)
        driver._is_selector_present = Mock(return_value=True)

        changed = IOSDriver._wait_confirm_click_effect(
            driver,
            selector="确定",
            before_signature="sig-a",
            timeout=0.5,
            selector_by="label",
            had_alert_before=True,
        )

        self.assertFalse(changed)

    def test_wait_confirm_click_effect_accepts_alert_dismissal_without_signature_change(self):
        driver = self._new_driver()
        driver._wait_page_changed = Mock(return_value=False)
        driver._has_alert_or_sheet = Mock(return_value=False)
        driver._is_alert_button_present = Mock(return_value=False)
        driver._is_selector_present = Mock(return_value=False)

        changed = IOSDriver._wait_confirm_click_effect(
            driver,
            selector="确定",
            before_signature="sig-a",
            timeout=0.5,
            selector_by="label",
            had_alert_before=True,
        )

        self.assertTrue(changed)

    def test_popup_probe_cache_reused_for_bounds_query(self):
        driver = self._new_driver()
        popup = Mock()
        popup.bounds = (10, 20, 50, 80)
        session = _FakeSession(
            {
                ("className", "XCUIElementTypeAlert"): _FakeSelector(True, element=popup),
                ("className", "XCUIElementTypeSheet"): _FakeSelector(False),
            }
        )
        driver.client.session.return_value = session

        has_alert = IOSDriver._has_alert_or_sheet(driver, timeout=0.1)
        bounds = IOSDriver._get_active_popup_bounds(driver, timeout=0.1)

        self.assertTrue(has_alert)
        self.assertEqual(bounds, (10, 20, 50, 80))
        alert_calls = [call for call in session.calls if call.get("className") == "XCUIElementTypeAlert"]
        self.assertEqual(len(alert_calls), 1)

    def test_get_step_ocr_result_reuses_screenshot_and_ocr_within_step(self):
        driver = self._new_driver()
        step_context = {}
        driver._capture_screenshot_cached = Mock(return_value=b"\x89PNG\r\n\x1a\nfake")
        class _FakeImage:
            shape = (100, 200, 3)

            def __getitem__(self, item):
                return ("crop", item)

        driver._decode_png_to_bgr = Mock(return_value=_FakeImage())
        driver._get_ocr_engine = Mock(return_value="ocr-engine")

        with patch("backend.drivers.ios_driver.run_paddle_ocr", return_value=[("ok", 0.99)]) as mock_ocr:
            first = IOSDriver._get_step_ocr_result(driver, step_context=step_context, timeout=1.0)
            second = IOSDriver._get_step_ocr_result(driver, step_context=step_context, timeout=1.0)

        self.assertEqual(first.get("result"), second.get("result"))
        self.assertEqual(first.get("offset"), (0.0, 0.0))
        driver._capture_screenshot_cached.assert_called_once_with(step_context=step_context, timeout=1.0)
        mock_ocr.assert_called_once()
        self.assertEqual(mock_ocr.call_args[0][0], "ocr-engine")

    def test_get_step_ocr_result_crops_to_popup_bounds(self):
        class _FakeImage:
            shape = (100, 200, 3)

            def __getitem__(self, item):
                return ("crop", item)

        driver = self._new_driver()
        step_context = {}
        driver._capture_screenshot_cached = Mock(return_value=b"\x89PNG\r\n\x1a\nfake")
        driver._decode_png_to_bgr = Mock(return_value=_FakeImage())
        driver._get_ocr_engine = Mock(return_value="ocr-engine")

        with patch("backend.drivers.ios_driver.run_paddle_ocr", return_value=[("ok", 0.99)]) as mock_ocr:
            payload = IOSDriver._get_step_ocr_result(
                driver,
                step_context=step_context,
                timeout=1.0,
                crop_bounds=(50, 20, 150, 80),
            )

        self.assertTrue(isinstance(payload, dict))
        self.assertGreater(payload.get("offset")[0], 0)
        self.assertGreater(payload.get("offset")[1], 0)
        called_image = mock_ocr.call_args[0][1]
        self.assertEqual(called_image[0], "crop")
        self.assertIn("ocr_crop_bounds", step_context.get("artifacts", {}))

    def test_get_step_ocr_result_uses_popup_hint_crop_for_confirm(self):
        class _FakeImage:
            shape = (200, 100, 3)

            def __getitem__(self, item):
                return ("crop", item)

        driver = self._new_driver()
        step_context = {}
        driver._capture_screenshot_cached = Mock(return_value=b"\x89PNG\r\n\x1a\nfake")
        driver._decode_png_to_bgr = Mock(return_value=_FakeImage())
        driver._get_ocr_engine = Mock(return_value="ocr-engine")

        with patch("backend.drivers.ios_driver.run_paddle_ocr", return_value=[("ok", 0.99)]) as mock_ocr:
            payload = IOSDriver._get_step_ocr_result(
                driver,
                step_context=step_context,
                timeout=1.0,
                prefer_popup_crop=True,
            )

        self.assertEqual(payload.get("metrics", {}).get("ocr_scope"), "popup-hint")
        called_image = mock_ocr.call_args[0][1]
        self.assertEqual(called_image[0], "crop")
        self.assertEqual(step_context.get("artifacts", {}).get("ocr_crop_source"), "popup_hint")

    def test_click_with_fallback_plan_skips_ocr_without_popup_signal(self):
        driver = self._new_driver()
        driver._build_click_locator_attempts = Mock(
            return_value=[{"selector": "商品标题", "by": "label", "source_by": "label"}]
        )
        driver._click_locator_once = Mock(side_effect=RuntimeError("not-found"))
        driver._has_alert_or_sheet = Mock(return_value=False)
        driver._tap_alert_button = Mock(return_value=False)
        driver._tap_by_ocr_text = Mock(return_value=True)

        with self.assertRaises(RuntimeError):
            IOSDriver.click_with_fallback_plan(
                driver,
                [{"selector": "商品标题", "by": "label"}],
                timeout=5,
                step_context={},
            )

        driver._tap_alert_button.assert_not_called()
        driver._tap_by_ocr_text.assert_not_called()

    def test_click_with_fallback_plan_dedupes_popup_rescue_targets(self):
        driver = self._new_driver()
        driver._build_click_locator_attempts = Mock(
            return_value=[
                {"selector": "允许", "by": "label", "source_by": "label"},
                {"selector": "允许", "by": "name", "source_by": "name"},
            ]
        )
        driver._click_locator_once = Mock(side_effect=RuntimeError("not-found"))
        driver._has_alert_or_sheet = Mock(return_value=True)
        driver._tap_alert_button = Mock(return_value=False)
        driver._tap_by_ocr_text = Mock(return_value=True)

        IOSDriver.click_with_fallback_plan(
            driver,
            [
                {"selector": "允许", "by": "label"},
                {"selector": "允许", "by": "name"},
            ],
            timeout=10,
            step_context={},
        )

        driver._tap_alert_button.assert_called_once()
        driver._tap_by_ocr_text.assert_called_once()

    def test_click_with_fallback_plan_reports_ocr_attempt_for_confirm_text(self):
        driver = self._new_driver()
        driver._build_click_locator_attempts = Mock(
            return_value=[{"selector": "确定", "by": "id", "source_by": "id"}]
        )
        driver._click_locator_once = Mock(side_effect=RuntimeError("not-found"))
        driver._has_alert_or_sheet = Mock(return_value=False)
        driver._tap_alert_button = Mock(return_value=False)
        driver._tap_by_ocr_text = Mock(return_value=False)

        with self.assertRaises(RuntimeError) as context:
            IOSDriver.click_with_fallback_plan(
                driver,
                [{"selector": "确定", "by": "id"}],
                timeout=5,
                step_context={},
            )

        self.assertIn("ocr:确定:not-found", str(context.exception))
        driver._tap_by_ocr_text.assert_called_once()

    def test_click_with_fallback_plan_reserves_budget_for_confirm_rescue(self):
        class _FakeClock:
            def __init__(self):
                self.now = 1000.0

            def time(self):
                return self.now

            def advance(self, seconds: float):
                self.now += seconds

        clock = _FakeClock()
        driver = self._new_driver()
        driver._log_action_success = Mock()
        driver._log_action_failure = Mock()
        driver._build_click_locator_attempts = Mock(
            return_value=[
                {"selector": "确定", "by": "id", "source_by": "id"},
                {"selector": "确定", "by": "label", "source_by": "id"},
                {"selector": "确定", "by": "name", "source_by": "id"},
            ]
        )

        def _consume_time(**kwargs):
            clock.advance(0.48)
            raise RuntimeError("not-found")

        driver._click_locator_once = Mock(side_effect=_consume_time)
        driver._has_alert_or_sheet = Mock(return_value=False)
        driver._tap_alert_button = Mock(return_value=False)
        driver._tap_by_ocr_text = Mock(return_value=True)

        with patch("backend.drivers.ios_driver.time.time", side_effect=clock.time):
            IOSDriver.click_with_fallback_plan(
                driver,
                [{"selector": "确定", "by": "id"}],
                timeout=1,
                step_context={},
            )

        driver._tap_by_ocr_text.assert_called_once()

    def test_screenshot_uses_explicit_timeout(self):
        driver = self._new_driver()
        png = b"\x89PNG\r\n\x1a\nfake"
        driver.client.http.get.return_value = Mock(value=base64.b64encode(png).decode("utf-8"))

        raw = IOSDriver.screenshot(driver)

        self.assertEqual(raw, png)
        driver.client.http.get.assert_called_once_with(
            "screenshot", timeout=driver._SCREENSHOT_TIMEOUT_SECONDS
        )

    def test_screenshot_raises_on_timeout(self):
        driver = self._new_driver()
        driver.client.http.get.side_effect = RuntimeError("request timeout")

        with self.assertRaises(RuntimeError) as context:
            IOSDriver.screenshot(driver)

        self.assertIn("请求失败或超时", str(context.exception))

    def test_truncate_log_value_keeps_zero_metrics(self):
        self.assertEqual(IOSDriver._truncate_log_value(0.0), "0.0")
        self.assertEqual(IOSDriver._truncate_log_value(0), "0")


if __name__ == "__main__":
    unittest.main()
