import unittest
import time
from unittest.mock import Mock

from backend.drivers.android_driver import AndroidDriver


class _MissingElement:
    def exists(self, timeout: int = 0):  # noqa: ARG002
        return False


class _FakeElement:
    def __init__(
        self,
        *,
        text: str = "",
        class_name: str = "android.widget.EditText",
        password: bool = False,
        exists: bool = True,
    ):
        self._text = text
        self.class_name = class_name
        self.password = password
        self._exists = exists
        self.set_text_mode = "ok"  # ok|noop|raise
        self.send_keys_mode = "ok"  # ok|noop|raise
        self.raise_get_text = False
        self.child_edittext = None
        self.set_text_calls = []

    @property
    def info(self):
        return {
            "className": self.class_name,
            "password": self.password,
            "text": self._text,
            "contentDescription": "",
        }

    def exists(self, timeout: int = 0):  # noqa: ARG002
        return self._exists

    def click(self):
        return None

    def clear_text(self):
        self._text = ""

    def set_text(self, value: str):
        self.set_text_calls.append(value)
        if self.set_text_mode == "raise":
            raise RuntimeError("set_text failed")
        if self.set_text_mode == "ok":
            self._text = str(value)

    def send_keys(self, value: str):
        if self.send_keys_mode == "raise":
            raise RuntimeError("send_keys failed")
        if self.send_keys_mode == "ok":
            self._text = str(value)

    def get_text(self):
        if self.raise_get_text:
            raise RuntimeError("get_text not supported")
        return self._text

    def child(self, **kwargs):
        if kwargs.get("className") == "android.widget.EditText" and self.child_edittext is not None:
            return self.child_edittext
        return _MissingElement()


class AndroidDriverInputTests(unittest.TestCase):
    def _new_driver(self) -> AndroidDriver:
        driver = AndroidDriver.__new__(AndroidDriver)
        driver.device_id = "android-1"
        driver._device = Mock()
        return driver

    def test_input_falls_back_to_focused_edittext_when_target_not_editable(self):
        driver = self._new_driver()
        target = _FakeElement(text="请输入手机号码")
        target.set_text_mode = "noop"
        target.send_keys_mode = "raise"
        focused = _FakeElement(text="", class_name="android.widget.EditText")

        def _device_call(**kwargs):
            if kwargs.get("focused"):
                return focused
            return _MissingElement()

        driver._device.side_effect = _device_call
        driver._find_element = Mock(return_value=target)
        driver._device.shell.return_value = None

        AndroidDriver.input(driver, selector="请输入手机号码", by="text", text="13051811799")

        self.assertIn("13051811799", target.set_text_calls)
        self.assertEqual(focused._text, "13051811799")

    def test_input_raises_when_value_not_applied_on_non_password_field(self):
        driver = self._new_driver()
        target = _FakeElement(text="请输入手机号码")
        target.set_text_mode = "noop"
        target.send_keys_mode = "noop"
        focused = _FakeElement(text="", class_name="android.widget.EditText")
        focused.set_text_mode = "noop"

        def _device_call(**kwargs):
            if kwargs.get("focused"):
                return focused
            return _MissingElement()

        driver._device.side_effect = _device_call
        driver._find_element = Mock(return_value=target)
        driver._device.shell.side_effect = lambda *args, **kwargs: None

        with self.assertRaises(RuntimeError) as context:
            AndroidDriver.input(driver, selector="请输入手机号码", by="text", text="{{ NAME }}")

        self.assertIn("Android.input 执行失败", str(context.exception))
        self.assertIn("verify-failed", str(context.exception))

    def test_input_accepts_unverifiable_password_field(self):
        driver = self._new_driver()
        target = _FakeElement(password=True)
        target.raise_get_text = True
        target.set_text_mode = "noop"
        focused = _FakeElement(password=True, class_name="android.widget.EditText", text="")
        focused.set_text_mode = "ok"
        focused.raise_get_text = True

        def _masked_set_text(value: str):
            focused._text = "•" * len(str(value))

        focused.set_text = _masked_set_text

        def _device_call(**kwargs):
            if kwargs.get("focused"):
                return focused
            return _MissingElement()

        driver._device.side_effect = _device_call
        driver._find_element = Mock(return_value=target)
        driver._device.shell = Mock()

        AndroidDriver.input(driver, selector="请输入密码", by="text", text="haier123")
        driver._device.shell.assert_not_called()

    def test_input_raises_when_password_mask_does_not_change(self):
        driver = self._new_driver()
        target = _FakeElement(text="请输入密码", password=False, class_name="android.widget.EditText")
        target.set_text_mode = "noop"
        focused = _FakeElement(text="••••••••", password=False, class_name="android.widget.EditText")
        focused.set_text_mode = "noop"

        def _device_call(**kwargs):
            if kwargs.get("focused"):
                return focused
            return _MissingElement()

        driver._device.side_effect = _device_call
        driver._find_element = Mock(return_value=target)
        driver._device.shell = Mock()

        with self.assertRaises(RuntimeError) as context:
            AndroidDriver.input(driver, selector="请输入密码", by="text", text="haier123")

        self.assertIn("unverifiable-no-state-change", str(context.exception))
        driver._device.shell.assert_not_called()

    def test_input_uses_child_edittext_when_selector_hits_container(self):
        driver = self._new_driver()
        container = _FakeElement(
            class_name="android.widget.LinearLayout",
            text="请输入手机号码",
        )
        container.set_text_mode = "noop"
        child = _FakeElement(text="请输入手机号码", class_name="android.widget.EditText")
        container.child_edittext = child
        focused = _FakeElement(text="", class_name="android.widget.EditText")

        def _device_call(**kwargs):
            if kwargs.get("focused"):
                return focused
            return _MissingElement()

        driver._device.side_effect = _device_call
        driver._find_element = Mock(return_value=container)
        driver._device.shell.side_effect = lambda *args, **kwargs: None

        AndroidDriver.input(driver, selector="请输入手机号码", by="text", text="13051811799")

        self.assertEqual(child._text, "13051811799")

    def test_safe_get_text_returns_none_when_timeout(self):
        driver = self._new_driver()
        target = _FakeElement(text="hello", class_name="android.widget.EditText")

        def _slow_get_text():
            time.sleep(0.5)
            return "hello"

        target.get_text = _slow_get_text

        started = time.time()
        value = driver._safe_get_text(target, timeout_seconds=0.05)
        elapsed = time.time() - started

        self.assertIsNone(value)
        self.assertLess(elapsed, 0.3)


if __name__ == "__main__":
    unittest.main()
