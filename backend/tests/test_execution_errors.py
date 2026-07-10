import threading
import unittest
from unittest.mock import Mock, patch

import uiautomator2 as u2
import wda

from backend.drivers.android_driver import AndroidDriver
from backend.drivers.base_driver import BaseDriver
from backend.drivers.cross_platform_runner import DriverFactory, TestCaseRunner
from backend.drivers.ios_driver import IOSDriver
from backend.execution_errors import (
    E2001_ELEMENT_NOT_FOUND,
    E2002_WAIT_TIMEOUT,
    E2003_ASSERT_TEXT_FAILED,
    E2004_ASSERT_IMAGE_FAILED,
    E2005_IMAGE_NOT_MATCHED,
    E2006_OCR_NO_RESULT,
    E2007_INPUT_FAILED,
    E2008_APP_CONTROL_FAILED,
    E2009_CLICK_NO_EFFECT,
    E2101_DEVICE_CONNECTION_LOST,
    E2102_WDA_SESSION_ERROR,
    E2201_INVALID_ARGS,
    E2999_EXECUTION_ERROR,
    ExecutionStepAssertionError,
    ExecutionStepError,
    ExecutionStepRuntimeError,
    classify_exception,
    suggestion_for,
)


class ExecutionStepErrorTests(unittest.TestCase):
    def test_str_keeps_plain_message_without_code_prefix(self):
        exc = ExecutionStepError(
            E2001_ELEMENT_NOT_FOUND,
            "元素未找到: selector='登录', by='text'",
        )
        self.assertEqual(str(exc), "元素未找到: selector='登录', by='text'")
        self.assertEqual(exc.code, E2001_ELEMENT_NOT_FOUND)
        self.assertEqual(exc.suggestion, suggestion_for(E2001_ELEMENT_NOT_FOUND))

    def test_runtime_variant_is_runtime_error(self):
        exc = ExecutionStepRuntimeError(E2002_WAIT_TIMEOUT, "等待超时")
        self.assertIsInstance(exc, RuntimeError)
        self.assertIsInstance(exc, ExecutionStepError)

    def test_assertion_variant_is_assertion_error(self):
        exc = ExecutionStepAssertionError(E2003_ASSERT_TEXT_FAILED, "断言失败")
        self.assertIsInstance(exc, AssertionError)
        self.assertIsInstance(exc, ExecutionStepError)

    def test_custom_suggestion_and_context_preserved(self):
        exc = ExecutionStepError(
            E2006_OCR_NO_RESULT,
            "extract_by_ocr 未识别到文本",
            context={"region": "[0,0,10,10]"},
            suggestion="自定义建议",
        )
        self.assertEqual(exc.suggestion, "自定义建议")
        self.assertEqual(exc.context, {"region": "[0,0,10,10]"})

    def test_unknown_code_falls_back_to_e2999(self):
        exc = ExecutionStepError("", "boom")
        self.assertEqual(exc.code, E2999_EXECUTION_ERROR)


class ClassifyExceptionTests(unittest.TestCase):
    def test_passthrough_execution_step_error(self):
        exc = ExecutionStepRuntimeError(
            E2005_IMAGE_NOT_MATCHED,
            "图像模板匹配失败",
            suggestion="重新截图",
        )
        code, suggestion = classify_exception(exc, platform="android", action="click_image")
        self.assertEqual(code, E2005_IMAGE_NOT_MATCHED)
        self.assertEqual(suggestion, "重新截图")

    def test_u2_element_not_found(self):
        code, suggestion = classify_exception(
            u2.UiObjectNotFoundError({"message": "not found"}),
            platform="android",
            action="click",
        )
        self.assertEqual(code, E2001_ELEMENT_NOT_FOUND)
        self.assertTrue(suggestion)

    def test_u2_xpath_element_not_found(self):
        code, _ = classify_exception(
            u2.XPathElementNotFoundError("//node"),
            platform="android",
            action="click",
        )
        self.assertEqual(code, E2001_ELEMENT_NOT_FOUND)

    def test_u2_connection_family_maps_to_e2101(self):
        for exc in (
            u2.ConnectError("connect failed"),
            u2.HTTPTimeoutError("http timeout"),
            u2.SessionBrokenError("session broken"),
        ):
            code, _ = classify_exception(exc, platform="android", action="click")
            self.assertEqual(code, E2101_DEVICE_CONNECTION_LOST, msg=str(type(exc)))

    def test_u2_app_not_found_maps_to_e2008(self):
        code, _ = classify_exception(
            u2.AppNotFoundError("com.missing.app"),
            platform="android",
            action="start_app",
        )
        self.assertEqual(code, E2008_APP_CONTROL_FAILED)

    def test_wda_element_not_found(self):
        code, _ = classify_exception(
            wda.WDAElementNotFoundError("element not found"),
            platform="ios",
            action="click",
        )
        self.assertEqual(code, E2001_ELEMENT_NOT_FOUND)

    def test_wda_session_family_maps_to_e2102(self):
        for exc in (
            wda.WDAInvalidSessionIdError(1, {"error": "invalid session id"}),
            wda.WDAEmptyResponseError("GET", "/status", None),
            wda.WDAError("GET", "/status", "bad"),
        ):
            code, _ = classify_exception(exc, platform="ios", action="click")
            self.assertEqual(code, E2102_WDA_SESSION_ERROR, msg=str(type(exc)))

    def test_wda_keyboard_not_present_maps_to_input_failed(self):
        exc = wda.exceptions.WDAKeyboardNotPresentError(
            1, {"error": "invalid element state"}
        )
        code, _ = classify_exception(exc, platform="ios", action="input")
        self.assertEqual(code, E2007_INPUT_FAILED)

    def test_builtin_connection_error_by_platform(self):
        code_android, _ = classify_exception(
            ConnectionRefusedError("connection refused"),
            platform="android",
            action="click",
        )
        code_ios, _ = classify_exception(
            ConnectionRefusedError("connection refused"),
            platform="ios",
            action="click",
        )
        self.assertEqual(code_android, E2101_DEVICE_CONNECTION_LOST)
        self.assertEqual(code_ios, E2102_WDA_SESSION_ERROR)

    def test_timeout_error_wait_action_maps_to_wait_timeout(self):
        code, _ = classify_exception(
            TimeoutError("timed out"),
            platform="android",
            action="wait_until_exists",
        )
        self.assertEqual(code, E2002_WAIT_TIMEOUT)

    def test_timeout_error_other_action_maps_to_connection(self):
        code, _ = classify_exception(
            TimeoutError("timed out"),
            platform="ios",
            action="click",
        )
        self.assertEqual(code, E2102_WDA_SESSION_ERROR)

    def test_assertion_error_by_action(self):
        code_text, _ = classify_exception(
            AssertionError("断言失败"), platform="android", action="assert_text"
        )
        code_image, _ = classify_exception(
            AssertionError("断言失败"), platform="android", action="assert_image"
        )
        self.assertEqual(code_text, E2003_ASSERT_TEXT_FAILED)
        self.assertEqual(code_image, E2004_ASSERT_IMAGE_FAILED)

    def test_value_error_maps_to_invalid_args(self):
        code, _ = classify_exception(
            ValueError("区域格式非法"), platform="android", action="extract_by_ocr"
        )
        self.assertEqual(code, E2201_INVALID_ARGS)

    def test_embedded_precheck_code_extracted(self):
        code, suggestion = classify_exception(
            ValueError("P1006_INVALID_ARGS: input 动作缺少 args.text/value"),
            platform="android",
            action="input",
        )
        self.assertEqual(code, "P1006_INVALID_ARGS")
        self.assertEqual(suggestion, suggestion_for("P1006_INVALID_ARGS"))

    def test_embedded_action_not_supported_code_extracted(self):
        code, _ = classify_exception(
            NotImplementedError("P1002_ACTION_NOT_SUPPORTED: platform=ios, action=foo"),
            platform="ios",
            action="foo",
        )
        self.assertEqual(code, "P1002_ACTION_NOT_SUPPORTED")

    def test_message_fallback_rules(self):
        cases = [
            (RuntimeError("extract_by_ocr 未识别到文本"), E2006_OCR_NO_RESULT),
            (RuntimeError("等待超时，元素未出现: selector='x'"), E2002_WAIT_TIMEOUT),
            (RuntimeError("元素未找到: selector='x', by='id'"), E2001_ELEMENT_NOT_FOUND),
            (RuntimeError("图像模板匹配失败: 未在屏幕上找到匹配的图像区域"), E2005_IMAGE_NOT_MATCHED),
            (RuntimeError("tap-no-effect"), E2009_CLICK_NO_EFFECT),
            (RuntimeError("图像模板匹配点击后页面未变化（tap-no-effect）"), E2009_CLICK_NO_EFFECT),
        ]
        for exc, expected in cases:
            code, _ = classify_exception(exc, platform="android", action="unknown")
            self.assertEqual(code, expected, msg=str(exc))

    def test_click_no_effect_structured_error_passthrough(self):
        exc = ExecutionStepRuntimeError(
            E2009_CLICK_NO_EFFECT,
            "tap-no-effect",
            context={"selector": "确定", "by": "label"},
        )
        code, suggestion = classify_exception(exc, platform="ios", action="click")
        self.assertEqual(code, E2009_CLICK_NO_EFFECT)
        self.assertEqual(suggestion, suggestion_for(E2009_CLICK_NO_EFFECT))
        self.assertTrue(suggestion)
        # str(exc) 保持原始消息格式，供既有消息匹配逻辑（如 iOS 驱动内部回退）复用
        self.assertEqual(str(exc), "tap-no-effect")

    def test_unknown_exception_falls_back_to_e2999(self):
        code, suggestion = classify_exception(
            RuntimeError("boom"), platform="android", action="click"
        )
        self.assertEqual(code, E2999_EXECUTION_ERROR)
        self.assertTrue(suggestion)

    def test_user_abort_not_classified(self):
        code, suggestion = classify_exception(
            RuntimeError("执行已被用户中止"), platform="android", action="sleep"
        )
        self.assertEqual(code, "")
        self.assertEqual(suggestion, "")


class _StructuredFakeDriver(BaseDriver):
    """按 selector 触发不同异常的假驱动，用于验证结构化字段透传。"""

    def __init__(self, device_id: str, **kwargs):  # noqa: ARG002
        super().__init__(device_id)

    def click(self, selector: str, by: str) -> None:
        if selector == "structured-missing":
            raise ExecutionStepRuntimeError(
                E2001_ELEMENT_NOT_FOUND,
                f"元素未找到: selector={selector!r}, by={by!r}",
                context={"selector": selector, "by": by},
            )
        if selector == "raw-boom":
            raise RuntimeError("boom")

    def input(self, selector: str, by: str, text: str) -> None:
        return None

    def input_focused(self, text: str) -> None:
        return None

    def screenshot(self) -> bytes:
        return b"png"

    def click_by_coordinates(self, x: float, y: float) -> None:
        return None

    def wait_until_exists(self, selector: str, by: str, timeout: int = 10) -> None:
        if selector == "missing":
            raise RuntimeError(
                f"等待超时，元素未出现: selector={selector!r}, by={by!r}, timeout={timeout}"
            )

    def assert_text(
        self,
        selector: str = "",
        by: str = "",
        expected_text: str = "",
        match_mode: str = "contains",
    ) -> None:
        if expected_text == "mismatch":
            raise ExecutionStepAssertionError(
                E2003_ASSERT_TEXT_FAILED,
                "断言失败: 期望页面包含 'mismatch'",
                context={"expected_text": expected_text, "match_mode": match_mode},
            )

    def swipe(self, direction: str) -> None:
        return None

    def back(self) -> None:
        return None

    def home(self) -> None:
        return None

    def start_app(self, app_id: str) -> None:
        return None

    def stop_app(self, app_id: str) -> None:
        return None

    def click_image(self, image_path: str) -> None:
        return None

    def assert_image(self, image_path: str, match_mode: str = "exists") -> None:
        return None

    def extract_by_ocr(self, region: str, extract_rule=None) -> str:
        raise RuntimeError("extract_by_ocr 未识别到文本")


class RunnerStructuredErrorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_android_driver = DriverFactory._registry.get("android")
        DriverFactory.register("android", _StructuredFakeDriver)
        self.runner = TestCaseRunner(platform="android", device_id="device-err")

    def tearDown(self) -> None:
        try:
            self.runner.disconnect()
        finally:
            if self.original_android_driver is not None:
                DriverFactory.register("android", self.original_android_driver)

    def test_structured_error_passthrough_to_step_result(self):
        result = self.runner.run_step(
            {
                "action": "click",
                "platform_overrides": {
                    "android": {"selector": "structured-missing", "by": "id"},
                },
                "execute_on": ["android"],
            }
        )
        self.assertEqual(result["status"], "FAIL")
        # 原 error 字符串格式（定位器聚合格式）不回归。
        self.assertIn("selector='structured-missing'", str(result.get("error")))
        self.assertIn("error=元素未找到", str(result.get("error")))
        self.assertNotIn("E2001", str(result.get("error")))
        self.assertEqual(result.get("error_code"), E2001_ELEMENT_NOT_FOUND)
        self.assertEqual(result.get("suggestion"), suggestion_for(E2001_ELEMENT_NOT_FOUND))
        context = result.get("error_context") or {}
        self.assertEqual(context.get("action"), "click")
        self.assertEqual(context.get("platform"), "android")
        self.assertEqual(context.get("device_id"), "device-err")
        self.assertEqual(context.get("selector"), "structured-missing")
        self.assertEqual(context.get("by"), "id")

    def test_unknown_exception_falls_back_to_e2999(self):
        result = self.runner.run_step(
            {
                "action": "click",
                "platform_overrides": {
                    "android": {"selector": "raw-boom", "by": "id"},
                },
                "execute_on": ["android"],
            }
        )
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result.get("error_code"), E2999_EXECUTION_ERROR)
        self.assertTrue(result.get("suggestion"))
        self.assertIn("boom", str(result.get("error")))

    def test_wait_timeout_message_classified_via_fallback(self):
        result = self.runner.run_step(
            {
                "action": "wait_until_exists",
                "platform_overrides": {
                    "android": {"selector": "missing", "by": "id"},
                },
                "execute_on": ["android"],
                "timeout": 1,
            }
        )
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result.get("error_code"), E2002_WAIT_TIMEOUT)

    def test_assert_text_structured_error_code(self):
        result = self.runner.run_step(
            {
                "action": "assert_text",
                "args": {"expected_text": "mismatch"},
                "execute_on": ["android"],
            }
        )
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result.get("error_code"), E2003_ASSERT_TEXT_FAILED)
        context = result.get("error_context") or {}
        self.assertEqual(context.get("expected_text"), "mismatch")

    def test_extract_by_ocr_retry_exhaustion_maps_to_e2006(self):
        result = self.runner.run_step(
            {
                "action": "extract_by_ocr",
                "args": {"region": "[0.1,0.1,0.2,0.2]"},
                "execute_on": ["android"],
                "timeout": 1,
            }
        )
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result.get("error_code"), E2006_OCR_NO_RESULT)
        self.assertIn("未识别到文本", str(result.get("error")))
        context = result.get("error_context") or {}
        self.assertEqual(context.get("region"), "[0.1,0.1,0.2,0.2]")

    def test_invalid_args_keeps_precheck_code(self):
        result = self.runner.run_step(
            {
                "action": "input",
                "args": {},
                "execute_on": ["android"],
            }
        )
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("P1006_INVALID_ARGS", str(result.get("error")))
        self.assertEqual(result.get("error_code"), "P1006_INVALID_ARGS")
        self.assertTrue(result.get("suggestion"))

    def test_pass_result_has_null_structured_fields(self):
        result = self.runner.run_step(
            {
                "action": "click",
                "platform_overrides": {
                    "android": {"selector": "ok", "by": "id"},
                },
                "execute_on": ["android"],
            }
        )
        self.assertEqual(result["status"], "PASS")
        self.assertIn("error_code", result)
        self.assertIn("error_context", result)
        self.assertIn("suggestion", result)
        self.assertIsNone(result.get("error_code"))
        self.assertIsNone(result.get("error_context"))
        self.assertIsNone(result.get("suggestion"))

    def test_skip_result_carries_platform_code(self):
        result = self.runner.run_step(
            {
                "action": "click",
                "platform_overrides": {
                    "android": {"selector": "ok", "by": "id"},
                },
                "execute_on": ["ios"],
            }
        )
        self.assertEqual(result["status"], "SKIP")
        self.assertEqual(result.get("error_code"), "P1001_PLATFORM_NOT_ALLOWED")
        self.assertTrue(result.get("suggestion"))

    def test_abort_result_has_no_error_code(self):
        abort_event = threading.Event()
        abort_event.set()
        abort_runner = TestCaseRunner(
            platform="android",
            device_id="device-abort",
            abort_event=abort_event,
        )
        try:
            result = abort_runner.run_step(
                {
                    "action": "click",
                    "platform_overrides": {
                        "android": {"selector": "ok", "by": "id"},
                    },
                    "execute_on": ["android"],
                }
            )
        finally:
            abort_runner.disconnect()
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result.get("error"), "执行已被用户中止")
        self.assertIsNone(result.get("error_code"))
        self.assertIsNone(result.get("suggestion"))


class _MissingElement:
    def exists(self, timeout: int = 0):  # noqa: ARG002
        return False


class AndroidDriverStructuredErrorTests(unittest.TestCase):
    def _new_driver(self) -> AndroidDriver:
        driver = AndroidDriver.__new__(AndroidDriver)
        driver.device_id = "android-1"
        driver._device = Mock()
        return driver

    def test_click_missing_element_raises_e2001(self):
        driver = self._new_driver()
        driver._find_element = Mock(return_value=_MissingElement())

        with self.assertRaises(RuntimeError) as context:
            AndroidDriver.click(driver, selector="登录", by="text")

        exc = context.exception
        self.assertIsInstance(exc, ExecutionStepError)
        self.assertEqual(exc.code, E2001_ELEMENT_NOT_FOUND)
        self.assertEqual(str(exc), "元素未找到: selector='登录', by='text'")
        self.assertEqual(exc.context.get("selector"), "登录")

    def test_wait_until_exists_timeout_raises_e2002(self):
        driver = self._new_driver()
        driver._find_element = Mock(return_value=_MissingElement())

        with self.assertRaises(RuntimeError) as context:
            AndroidDriver.wait_until_exists(driver, selector="首页", by="text", timeout=1)

        exc = context.exception
        self.assertIsInstance(exc, ExecutionStepError)
        self.assertEqual(exc.code, E2002_WAIT_TIMEOUT)
        self.assertIn("等待超时，元素未出现", str(exc))

    def test_assert_text_failure_raises_e2003_assertion(self):
        driver = self._new_driver()
        driver._collect_page_text_candidates = Mock(return_value=["首页", "我的"])

        with self.assertRaises(AssertionError) as context:
            AndroidDriver.assert_text(driver, expected_text="支付成功", match_mode="contains")

        exc = context.exception
        self.assertIsInstance(exc, ExecutionStepError)
        self.assertEqual(exc.code, E2003_ASSERT_TEXT_FAILED)
        self.assertIn("断言失败", str(exc))


class _FakeIOSSelector:
    def __init__(self, exists: bool):
        self._exists = exists

    def wait(self, timeout: int = 5, raise_error: bool = False):  # noqa: ARG002
        return self._exists

    @property
    def exists(self):
        return self._exists


class _FakeIOSSession:
    def __call__(self, **kwargs):  # noqa: ARG002
        return _FakeIOSSelector(False)


class IOSDriverStructuredErrorTests(unittest.TestCase):
    def _new_driver(self) -> IOSDriver:
        driver = IOSDriver.__new__(IOSDriver)
        driver.device_id = "ios-1"
        driver.client = Mock()
        driver.scale = 3.0
        return driver

    def test_resolve_selector_all_failed_raises_e2001(self):
        driver = self._new_driver()
        driver.client.session.return_value = _FakeIOSSession()

        with self.assertRaises(RuntimeError) as context:
            IOSDriver._get_element(driver, "不存在", "text", timeout=1)

        exc = context.exception
        self.assertIsInstance(exc, ExecutionStepError)
        self.assertEqual(exc.code, E2001_ELEMENT_NOT_FOUND)
        self.assertIn("attempts=", str(exc))

    def test_wait_until_exists_retags_to_e2002(self):
        driver = self._new_driver()
        driver.client.session.return_value = _FakeIOSSession()

        with self.assertRaises(RuntimeError) as context:
            IOSDriver.wait_until_exists(driver, "不存在", "text", timeout=1)

        exc = context.exception
        self.assertIsInstance(exc, ExecutionStepError)
        self.assertEqual(exc.code, E2002_WAIT_TIMEOUT)
        self.assertIn("元素未找到", str(exc))
        self.assertEqual(exc.context.get("timeout"), 1)

    @patch("backend.drivers.ios.app_control.time.sleep", return_value=None)
    def test_stop_app_failure_raises_e2008(self, _):
        driver = self._new_driver()
        session = Mock()
        session.app_terminate.side_effect = RuntimeError("terminate failed")
        driver.client.session.return_value = session
        driver.client.app_terminate.side_effect = RuntimeError("terminate failed")
        driver.client.app_state.return_value = {"value": 3}

        with self.assertRaises(RuntimeError) as context:
            IOSDriver.stop_app(driver, "com.demo.mall.ios")

        exc = context.exception
        self.assertIsInstance(exc, ExecutionStepError)
        self.assertEqual(exc.code, E2008_APP_CONTROL_FAILED)
        self.assertIn("iOS.stop_app 执行失败", str(exc))
        self.assertEqual(exc.context.get("app_id"), "com.demo.mall.ios")


if __name__ == "__main__":
    unittest.main()
