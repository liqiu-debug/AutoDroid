import unittest
import base64
import threading

from backend.drivers.base_driver import BaseDriver
from backend.drivers.cross_platform_runner import DriverFactory, TestCaseRunner


class FakeDriver(BaseDriver):
    def __init__(self, device_id: str, **kwargs):
        super().__init__(device_id)
        self.input_calls = []
        self.input_focused_calls = []
        self.assert_text_calls = []
        self.assert_image_calls = []
        self.swipe_calls = []
        self.ocr_calls = 0
        self.ocr_failures_before_success = int(kwargs.get("ocr_failures_before_success", 0) or 0)

    def click(self, selector: str, by: str) -> None:
        if selector == "fail":
            raise RuntimeError("click failed")
        if selector == "prefer-name" and by == "label":
            raise RuntimeError("label not found")

    def input(self, selector: str, by: str, text: str) -> None:
        self.input_calls.append({"selector": selector, "by": by, "text": text})
        if text == "fail":
            raise RuntimeError("input failed")

    def input_focused(self, text: str) -> None:
        self.input_focused_calls.append(text)
        if text == "fail-focused":
            raise RuntimeError("focused input failed")

    def screenshot(self) -> bytes:
        return b"png"

    def click_by_coordinates(self, x: float, y: float) -> None:
        return None

    def wait_until_exists(self, selector: str, by: str, timeout: int = 10) -> None:
        if selector == "missing":
            raise RuntimeError("not found")

    def assert_text(
        self,
        selector: str = "",
        by: str = "",
        expected_text: str = "",
        match_mode: str = "contains",
    ) -> None:
        self.assert_text_calls.append(
            {
                "selector": selector,
                "by": by,
                "expected_text": expected_text,
                "match_mode": match_mode,
            }
        )
        if expected_text == "mismatch":
            raise AssertionError("assert failed")

    def swipe(self, direction: str) -> None:
        self.swipe_calls.append(direction)
        if direction == "invalid":
            raise ValueError("invalid direction")

    def back(self) -> None:
        return None

    def home(self) -> None:
        return None

    def start_app(self, app_id: str) -> None:
        if app_id == "":
            raise ValueError("empty app")

    def stop_app(self, app_id: str) -> None:
        if app_id == "":
            raise ValueError("empty app")

    def click_image(self, image_path: str) -> None:
        if image_path == "missing.png":
            raise RuntimeError("image not found")

    def assert_image(self, image_path: str, match_mode: str = "exists") -> None:
        self.assert_image_calls.append(
            {
                "image_path": image_path,
                "match_mode": match_mode,
            }
        )
        if image_path == "missing.png" and match_mode == "exists":
            raise AssertionError("image assert failed")

    def extract_by_ocr(self, region: str, extract_rule=None) -> str:
        self.ocr_calls += 1
        if not region:
            raise RuntimeError("invalid region")
        if self.ocr_calls <= self.ocr_failures_before_success:
            raise RuntimeError("extract_by_ocr 未识别到文本")
        return "99.00"


class FakeIOSPlanDriver(FakeDriver):
    def __init__(self, device_id: str, **kwargs):
        super().__init__(device_id, **kwargs)
        self.click_plan_calls = []
        self.screenshot_calls = 0

    def click_with_fallback_plan(self, locator_candidates, timeout: int = 10, step_context=None) -> None:
        self.click_plan_calls.append(
            {
                "locator_candidates": [dict(item) for item in locator_candidates or []],
                "timeout": timeout,
            }
        )
        first_selector = ""
        if locator_candidates:
            first_selector = str(locator_candidates[0].get("selector") or "")
        if first_selector == "reuse-shot-fail":
            if isinstance(step_context, dict):
                step_context.setdefault("artifacts", {})["screenshot_base64"] = base64.b64encode(
                    b"cached-png"
                ).decode("utf-8")
            raise RuntimeError("planned click failed")

    def screenshot(self) -> bytes:
        self.screenshot_calls += 1
        return super().screenshot()


class CrossPlatformRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.platform = "android"
        self.original_android_driver = DriverFactory._registry.get("android")
        self.original_ios_driver = DriverFactory._registry.get("ios")
        DriverFactory.register("android", FakeDriver)
        DriverFactory.register("ios", FakeDriver)
        self.runner = TestCaseRunner(platform=self.platform, device_id="device-1")

    def tearDown(self) -> None:
        try:
            self.runner.disconnect()
        finally:
            if self.original_android_driver is not None:
                DriverFactory.register("android", self.original_android_driver)
            if self.original_ios_driver is not None:
                DriverFactory.register("ios", self.original_ios_driver)

    def test_skip_when_execute_on_not_match(self):
        result = self.runner.run_step(
            {
                "action": "click",
                "platform_overrides": {
                    self.platform: {"selector": "ok", "by": "id"},
                },
                "execute_on": ["ios"],
            }
        )
        self.assertEqual(result["status"], "SKIP")
        self.assertIn("P1001_PLATFORM_NOT_ALLOWED", str(result.get("error")))

    def test_fail_when_locator_missing(self):
        result = self.runner.run_step(
            {
                "action": "click",
                "platform_overrides": {},
                "execute_on": ["android"],
            }
        )
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("P1003_SELECTOR_MISSING", str(result.get("error")))

    def test_input_without_locator_uses_focused_input(self):
        result = self.runner.run_step(
            {
                "action": "input",
                "args": {"text": "hello"},
                "platform_overrides": {},
                "execute_on": ["android"],
            }
        )
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(self.runner.driver.input_focused_calls, ["hello"])
        self.assertEqual(self.runner.driver.input_calls, [])

    def test_input_with_locator_uses_locator_input(self):
        result = self.runner.run_step(
            {
                "action": "input",
                "args": {"text": "hello"},
                "platform_overrides": {
                    "android": {"selector": "com.demo:id/input", "by": "id"},
                },
                "execute_on": ["android"],
            }
        )
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(len(self.runner.driver.input_calls), 1)
        self.assertEqual(self.runner.driver.input_calls[0]["selector"], "com.demo:id/input")
        self.assertEqual(self.runner.driver.input_focused_calls, [])

    def test_fail_when_input_text_contains_unresolved_template(self):
        result = self.runner.run_step(
            {
                "action": "input",
                "args": {"text": "{{ NAME }}"},
                "platform_overrides": {
                    "android": {"selector": "com.demo:id/input", "by": "id"},
                },
                "execute_on": ["android"],
            }
        )
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("P1006_INVALID_ARGS", str(result.get("error")))
        self.assertIn("未解析变量", str(result.get("error")))

    def test_click_image_supported_on_android(self):
        result = self.runner.run_step(
            {
                "action": "click_image",
                "platform_overrides": {
                    self.platform: {"selector": "foo.png", "by": "image"},
                },
                "execute_on": ["android"],
            }
        )
        self.assertEqual(result["status"], "PASS")

    def test_ios_click_image_supported(self):
        ios_runner = TestCaseRunner(platform="ios", device_id="ios-1")
        try:
            result = ios_runner.run_step(
                {
                    "action": "click_image",
                    "platform_overrides": {
                        "ios": {"selector": "foo.png", "by": "image"},
                    },
                    "execute_on": ["ios"],
                }
            )
        finally:
            ios_runner.disconnect()
        self.assertEqual(result["status"], "PASS")

    def test_ios_click_image_uses_android_override_when_ios_locator_missing(self):
        ios_runner = TestCaseRunner(platform="ios", device_id="ios-1")
        try:
            result = ios_runner.run_step(
                {
                    "action": "click_image",
                    "platform_overrides": {
                        "android": {"selector": "foo.png", "by": "image"},
                    },
                    "execute_on": ["ios"],
                }
            )
        finally:
            ios_runner.disconnect()
        self.assertEqual(result["status"], "PASS")

    def test_ios_extract_by_ocr_supported(self):
        ios_runner = TestCaseRunner(platform="ios", device_id="ios-1")
        try:
            result = ios_runner.run_step(
                {
                    "action": "extract_by_ocr",
                    "args": {"region": "[0.1,0.1,0.2,0.2]"},
                    "execute_on": ["ios"],
                }
            )
        finally:
            ios_runner.disconnect()
        self.assertEqual(result["status"], "PASS")

    def test_assert_image_supported_on_android(self):
        result = self.runner.run_step(
            {
                "action": "assert_image",
                "args": {"image_path": "foo.png", "match_mode": "exists"},
                "platform_overrides": {
                    self.platform: {"selector": "foo.png", "by": "image"},
                },
                "execute_on": ["android"],
            }
        )
        self.assertEqual(result["status"], "PASS")
        calls = getattr(self.runner.driver, "assert_image_calls", [])
        self.assertEqual(calls[-1]["image_path"], "foo.png")
        self.assertEqual(calls[-1]["match_mode"], "exists")

    def test_assert_image_supports_not_exists_mode(self):
        result = self.runner.run_step(
            {
                "action": "assert_image",
                "args": {"image_path": "foo.png", "match_mode": "not_exists"},
                "execute_on": ["android"],
            }
        )
        self.assertEqual(result["status"], "PASS")
        calls = getattr(self.runner.driver, "assert_image_calls", [])
        self.assertEqual(calls[-1]["match_mode"], "not_exists")

    def test_run_all_stops_when_abort_event_is_set(self):
        abort_event = threading.Event()
        abort_event.set()
        abort_runner = TestCaseRunner(
            platform="android",
            device_id="device-abort",
            abort_event=abort_event,
        )
        try:
            result = abort_runner.run_all(
                [
                    {
                        "action": "click",
                        "platform_overrides": {
                            "android": {"selector": "com.demo:id/login", "by": "id"},
                        },
                        "execute_on": ["android"],
                    }
                ]
            )
        finally:
            abort_runner.disconnect()

        self.assertFalse(result["success"])
        self.assertEqual(len(result["steps"]), 1)
        self.assertEqual(result["steps"][0]["status"], "FAIL")
        self.assertEqual(result["steps"][0]["error"], "执行已被用户中止")

    def test_ios_locator_inferred_from_android_without_ios_override(self):
        ios_runner = TestCaseRunner(platform="ios", device_id="ios-1")
        try:
            result = ios_runner.run_step(
                {
                    "action": "click",
                    "platform_overrides": {
                        "android": {"selector": "登录", "by": "text"},
                    },
                    "execute_on": ["ios"],
                }
            )
        finally:
            ios_runner.disconnect()
        self.assertEqual(result["status"], "PASS")

    def test_ios_locator_fallback_chain_label_then_name(self):
        ios_runner = TestCaseRunner(platform="ios", device_id="ios-1")
        try:
            result = ios_runner.run_step(
                {
                    "action": "click",
                    "platform_overrides": {
                        "android": {"selector": "prefer-name", "by": "text"},
                    },
                    "execute_on": ["ios"],
                }
            )
        finally:
            ios_runner.disconnect()
        self.assertEqual(result["status"], "PASS")

    def test_ios_home_is_supported(self):
        ios_runner = TestCaseRunner(platform="ios", device_id="ios-1")
        try:
            result = ios_runner.run_step(
                {
                    "action": "home",
                    "execute_on": ["ios"],
                }
            )
        finally:
            ios_runner.disconnect()
        self.assertEqual(result["status"], "PASS")

    def test_fail_when_args_not_object(self):
        result = self.runner.run_step(
            {
                "action": "sleep",
                "args": "invalid",
                "execute_on": ["android"],
            }
        )
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("P1006_INVALID_ARGS", str(result.get("error")))

    def test_fail_when_start_app_missing_app_key(self):
        result = self.runner.run_step(
            {
                "action": "start_app",
                "args": {},
                "execute_on": ["android"],
            }
        )
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("P1006_INVALID_ARGS", str(result.get("error")))

    def test_swipe_uses_args_direction(self):
        result = self.runner.run_step(
            {
                "action": "swipe",
                "args": {"direction": "down"},
                "execute_on": ["android"],
            }
        )
        self.assertEqual(result["status"], "PASS")
        calls = getattr(self.runner.driver, "swipe_calls", [])
        self.assertEqual(calls[-1], "down")

    def test_assert_text_uses_global_contains_mode(self):
        result = self.runner.run_step(
            {
                "action": "assert_text",
                "args": {"expected_text": "99.00", "match_mode": "contains"},
                "platform_overrides": {
                    "android": {"selector": "旧定位值", "by": "text"},
                },
                "execute_on": ["android"],
            }
        )
        self.assertEqual(result["status"], "PASS")
        calls = getattr(self.runner.driver, "assert_text_calls", [])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["expected_text"], "99.00")
        self.assertEqual(calls[0]["match_mode"], "contains")

    def test_assert_text_supports_not_contains_mode(self):
        result = self.runner.run_step(
            {
                "action": "assert_text",
                "args": {"expected_text": "99.00", "match_mode": "not_contains"},
                "execute_on": ["android"],
            }
        )
        self.assertEqual(result["status"], "PASS")
        calls = getattr(self.runner.driver, "assert_text_calls", [])
        self.assertEqual(calls[-1]["match_mode"], "not_contains")

    def test_swipe_falls_back_to_value_direction(self):
        result = self.runner.run_step(
            {
                "action": "swipe",
                "value": "left",
                "execute_on": ["android"],
            }
        )
        self.assertEqual(result["status"], "PASS")
        calls = getattr(self.runner.driver, "swipe_calls", [])
        self.assertEqual(calls[-1], "left")

    def test_continue_strategy_keeps_following_steps(self):
        result = self.runner.run_all(
            [
                {
                    "action": "click",
                    "platform_overrides": {
                        self.platform: {"selector": "fail", "by": "id"},
                    },
                    "execute_on": ["android"],
                    "error_strategy": "CONTINUE",
                },
                {
                    "action": "click",
                    "platform_overrides": {
                        self.platform: {"selector": "ok", "by": "id"},
                    },
                    "execute_on": ["android"],
                },
            ]
        )
        self.assertFalse(result["success"])
        self.assertEqual(len(result["steps"]), 2)
        self.assertEqual(result["steps"][0]["status"], "FAIL")
        self.assertEqual(result["steps"][1]["status"], "PASS")
        self.assertEqual(result["steps"][0].get("screenshot"), base64.b64encode(b"png").decode("utf-8"))

    def test_ignore_strategy_maps_fail_to_warning(self):
        result = self.runner.run_all(
            [
                {
                    "action": "click",
                    "platform_overrides": {
                        self.platform: {"selector": "fail", "by": "id"},
                    },
                    "execute_on": ["android"],
                    "error_strategy": "IGNORE",
                },
                {
                    "action": "click",
                    "platform_overrides": {
                        self.platform: {"selector": "ok", "by": "id"},
                    },
                    "execute_on": ["android"],
                },
            ]
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["steps"][0]["status"], "WARNING")
        self.assertEqual(result["steps"][1]["status"], "PASS")
        self.assertEqual(result["steps"][0].get("screenshot"), base64.b64encode(b"png").decode("utf-8"))

    def test_abort_strategy_stops_execution(self):
        result = self.runner.run_all(
            [
                {
                    "action": "click",
                    "platform_overrides": {
                        self.platform: {"selector": "fail", "by": "id"},
                    },
                    "execute_on": ["android"],
                    "error_strategy": "ABORT",
                },
                {
                    "action": "click",
                    "platform_overrides": {
                        self.platform: {"selector": "ok", "by": "id"},
                    },
                    "execute_on": ["android"],
                },
            ]
        )
        self.assertFalse(result["success"])
        self.assertEqual(len(result["steps"]), 1)
        self.assertEqual(result["steps"][0]["status"], "FAIL")

    def test_extract_by_ocr_exports_runtime_variable(self):
        result = self.runner.run_all(
            [
                {
                    "action": "extract_by_ocr",
                    "args": {"region": "[0.1,0.1,0.2,0.2]"},
                    "value": "PRICE",
                    "execute_on": ["android"],
                },
                {
                    "action": "input",
                    "args": {"text": "{{ PRICE }}"},
                    "platform_overrides": {
                        self.platform: {"selector": "com.demo:id/price", "by": "id"},
                    },
                    "execute_on": ["android"],
                },
            ]
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["steps"][0]["status"], "PASS")
        self.assertEqual(result["runtime_variables"].get("PRICE"), "99.00")

        calls = getattr(self.runner.driver, "input_calls", [])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["text"], "99.00")

    def test_extract_by_ocr_retries_until_success_within_timeout(self):
        retry_runner = TestCaseRunner(
            platform="android",
            device_id="device-ocr-retry",
            ocr_failures_before_success=2,
        )
        driver = retry_runner.driver
        try:
            result = retry_runner.run_step(
                {
                    "action": "extract_by_ocr",
                    "args": {"region": "[0.1,0.1,0.2,0.2]"},
                    "execute_on": ["android"],
                    "timeout": 2,
                }
            )
        finally:
            retry_runner.disconnect()

        self.assertEqual(result["status"], "PASS")
        self.assertGreaterEqual(getattr(driver, "ocr_calls", 0), 3)

    def test_ios_click_uses_planned_dispatch(self):
        DriverFactory.register("ios", FakeIOSPlanDriver)
        ios_runner = TestCaseRunner(platform="ios", device_id="ios-1")
        try:
            result = ios_runner.run_step(
                {
                    "action": "click",
                    "platform_overrides": {
                        "ios": {"selector": "允许", "by": "label"},
                    },
                    "execute_on": ["ios"],
                }
            )
            calls = getattr(ios_runner.driver, "click_plan_calls", [])
            self.assertEqual(result["status"], "PASS")
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["timeout"], 10)
            self.assertEqual(calls[0]["locator_candidates"][0]["selector"], "允许")
        finally:
            ios_runner.disconnect()

    def test_ios_click_failure_reuses_artifact_screenshot(self):
        DriverFactory.register("ios", FakeIOSPlanDriver)
        ios_runner = TestCaseRunner(platform="ios", device_id="ios-artifact")
        try:
            result = ios_runner.run_all(
                [
                    {
                        "action": "click",
                        "platform_overrides": {
                            "ios": {"selector": "reuse-shot-fail", "by": "label"},
                        },
                        "execute_on": ["ios"],
                        "error_strategy": "IGNORE",
                    }
                ]
            )
            screenshot = result["steps"][0].get("screenshot")
            self.assertEqual(result["steps"][0]["status"], "WARNING")
            self.assertEqual(
                screenshot,
                base64.b64encode(b"cached-png").decode("utf-8"),
            )
            self.assertEqual(getattr(ios_runner.driver, "screenshot_calls", 0), 0)
        finally:
            ios_runner.disconnect()


if __name__ == "__main__":
    unittest.main()
