"""输入步骤的设备 RPC 有界护栏回归测试。

背景：uiautomator2 的 el.info 等 jsonrpc 调用在部分控件（WebView 输入框、
动效页面）上会长阻塞；输入「定位与输入成功」后校验阶段的 el.info 无保护挂死，
表现为步骤永远「执行中」。修复：所有可能长阻塞的 RPC 走 _call_bounded 护栏；
读取通道全部失败时按「不可验证」放行（与设备端 runtime 不校验的语义一致），
而不是误判为校验失败。
"""

import threading
import time
import unittest

from backend.drivers.android_driver import AndroidDriver, _call_bounded
from backend.execution_errors import ExecutionStepRuntimeError


class _BlockingInfoElement:
    """info 长阻塞的元素桩：输入动作正常，读取类 RPC 挂死。"""

    def __init__(self, typed_sink=None):
        self.typed = None
        self._typed_sink = typed_sink

    def exists(self, timeout=None):
        return True

    @property
    def info(self):
        time.sleep(60)

    def child(self, **kwargs):
        return None

    def click(self):
        return None

    def clear_text(self):
        return None

    def set_text(self, text):
        self.typed = text
        if self._typed_sink is not None:
            self._typed_sink.append(text)

    def get_text(self):
        return self.typed


class _ReadableElement:
    """info 正常可读、文本为空的元素桩（真正的输入失败场景）。"""

    def exists(self, timeout=None):
        return True

    @property
    def info(self):
        return {"className": "android.widget.EditText", "text": "", "password": False}

    def child(self, **kwargs):
        return None

    def click(self):
        return None

    def clear_text(self):
        return None

    def set_text(self, text):
        raise RuntimeError("set_text rejected")

    def get_text(self):
        return ""


class _StubDevice:
    def __init__(self, focused_element=None):
        self._focused_element = focused_element

    def __call__(self, **kwargs):
        return self._focused_element or _BlockingInfoElement()

    def shell(self, cmd):
        return ""


def _make_driver(element, focused_element=None):
    driver = object.__new__(AndroidDriver)
    driver.device_id = "stub-device"
    driver._device = _StubDevice(focused_element=focused_element)
    driver._find_element = lambda selector, by: element
    return driver


class CallBoundedTests(unittest.TestCase):
    def test_timeout_raises_and_does_not_hang(self):
        started = time.time()
        with self.assertRaises(TimeoutError):
            _call_bounded(lambda: time.sleep(60), 0.2, label="slow")
        self.assertLess(time.time() - started, 3.0)

    def test_value_and_error_passthrough(self):
        self.assertEqual(_call_bounded(lambda: 42, 1.0), 42)
        with self.assertRaises(ValueError):
            _call_bounded(lambda: (_ for _ in ()).throw(ValueError("x")), 1.0)


class InputBoundsTests(unittest.TestCase):
    def setUp(self):
        for name, value in (
            ("_RPC_TIMEOUT_INFO", 0.3),
            ("_RPC_TIMEOUT_EXISTS", 0.3),
            ("_RPC_TIMEOUT_CLICK", 0.3),
            ("_RPC_TIMEOUT_CLEAR", 0.3),
            ("_RPC_TIMEOUT_STRATEGY", 0.5),
            ("_RPC_TIMEOUT_FOCUSED", 0.3),
        ):
            self._prev = getattr(self, "_prev", {})
            self._prev[name] = getattr(AndroidDriver, name)
            setattr(AndroidDriver, name, value)

    def tearDown(self):
        for name, value in self._prev.items():
            setattr(AndroidDriver, name, value)

    def test_blocking_info_does_not_hang_and_input_succeeds(self):
        """输入成功但读取通道全部挂死：有界结束，按「不可验证」放行，不误杀不卡死。"""
        typed = []
        driver = _make_driver(_BlockingInfoElement(typed_sink=typed))

        started = time.time()
        driver.input(selector="x", by="text", text="hello")  # 不应抛出、不应挂死
        elapsed = time.time() - started

        self.assertLess(elapsed, 8.0, "input 必须在 RPC 护栏内有界结束")
        self.assertEqual(typed, ["hello"], "输入动作本身应已执行")

    def test_readable_empty_still_fails_bounded(self):
        """info 可读但输入真的失败：仍应按 E2007 失败，且在护栏内结束。"""
        driver = _make_driver(_ReadableElement(), focused_element=_ReadableElement())

        started = time.time()
        with self.assertRaises(ExecutionStepRuntimeError):
            driver.input(selector="x", by="text", text="hello")
        self.assertLess(time.time() - started, 10.0)


if __name__ == "__main__":
    unittest.main()
