import unittest
import os
import tempfile

from backend.runner import TestRunner
from backend.schemas import ActionType, SelectorType, Step


class _FakeFocusedElement:
    def __init__(self) -> None:
        self.clear_called = 0
        self.set_text_calls = []

    def exists(self, timeout: int = 1) -> bool:
        return True

    def clear_text(self) -> None:
        self.clear_called += 1

    def set_text(self, text: str) -> None:
        self.set_text_calls.append(text)


class _FakeDevice:
    def __init__(self, focused_element=None, fail_send_keys: bool = False, page_xml: str = "", image_wait_results=None) -> None:
        self.focused_element = focused_element
        self.fail_send_keys = fail_send_keys
        self.page_xml = page_xml
        self.send_keys_calls = []
        self.shell_calls = []
        self.image = _FakeImage(image_wait_results or [])

    def __call__(self, **kwargs):
        if kwargs.get("focused"):
            return self.focused_element
        return None

    def send_keys(self, value: str, clear: bool = False) -> None:
        self.send_keys_calls.append({"value": value, "clear": clear})
        if self.fail_send_keys:
            raise RuntimeError("send_keys failed")

    def shell(self, command: str) -> None:
        self.shell_calls.append(command)

    def dump_hierarchy(self) -> str:
        return self.page_xml


class _FakeImage:
    def __init__(self, wait_results) -> None:
        self.wait_results = list(wait_results)
        self.wait_calls = []

    def wait(self, image_path: str, timeout: int = 1, threshold: float = 0.9):
        self.wait_calls.append(
            {
                "image_path": image_path,
                "timeout": timeout,
                "threshold": threshold,
            }
        )
        if self.wait_results:
            return self.wait_results.pop(0)
        return None


class LegacyRunnerInputTests(unittest.TestCase):
    def test_input_without_selector_falls_back_to_send_keys(self):
        runner = TestRunner(device_serial="android-1")
        runner.d = _FakeDevice(focused_element=None)
        step = Step(
            action=ActionType.INPUT,
            selector="",
            selector_type=SelectorType.TEXT,
            value="hello",
        )

        result = runner.execute_step(step, variables={})

        self.assertTrue(result["success"])
        self.assertGreaterEqual(len(runner.d.send_keys_calls), 1)

    def test_input_without_selector_uses_focused_element_when_available(self):
        runner = TestRunner(device_serial="android-1")
        focused = _FakeFocusedElement()
        runner.d = _FakeDevice(focused_element=focused)
        step = Step(
            action=ActionType.INPUT,
            selector="",
            selector_type=SelectorType.TEXT,
            value="hello",
        )

        result = runner.execute_step(step, variables={})

        self.assertTrue(result["success"])
        self.assertEqual(focused.set_text_calls, ["hello"])
        self.assertEqual(runner.d.send_keys_calls, [])

    def test_assert_text_checks_page_global_contains(self):
        runner = TestRunner(device_serial="android-1")
        runner.d = _FakeDevice(
            page_xml='<hierarchy><node text="订单详情"/><node content-desc="支付成功"/></hierarchy>'
        )
        step = Step(
            action=ActionType.ASSERT_TEXT,
            value="支付成功",
            options={"match_mode": "contains"},
        )

        result = runner.execute_step(step, variables={})

        self.assertTrue(result["success"])

    def test_assert_text_matches_joined_page_text_when_target_sequence_appears_late(self):
        runner = TestRunner(device_serial="android-1")
        runner.d = _FakeDevice(
            page_xml=(
                '<hierarchy>'
                '<node text="购物车"/>'
                '<node text="订单"/>'
                '<node text="购物车"/>'
                '<node text="支付成功"/>'
                '</hierarchy>'
            )
        )
        step = Step(
            action=ActionType.ASSERT_TEXT,
            value="购物车支付成功",
            options={"match_mode": "contains"},
        )

        result = runner.execute_step(step, variables={})

        self.assertTrue(result["success"])

    def test_assert_text_checks_page_global_not_contains(self):
        runner = TestRunner(device_serial="android-1")
        runner.d = _FakeDevice(
            page_xml='<hierarchy><node text="订单详情"/><node content-desc="支付成功"/></hierarchy>'
        )
        step = Step(
            action=ActionType.ASSERT_TEXT,
            value="已退款",
            options={"match_mode": "not_contains"},
        )

        result = runner.execute_step(step, variables={})

        self.assertTrue(result["success"])

    def test_assert_text_not_contains_fails_when_joined_page_text_matches_late_sequence(self):
        runner = TestRunner(device_serial="android-1")
        runner.d = _FakeDevice(
            page_xml=(
                '<hierarchy>'
                '<node text="购物车"/>'
                '<node text="订单"/>'
                '<node text="购物车"/>'
                '<node text="支付成功"/>'
                '</hierarchy>'
            )
        )
        step = Step(
            action=ActionType.ASSERT_TEXT,
            value="购物车支付成功",
            options={"match_mode": "not_contains"},
        )

        result = runner.execute_step(step, variables={})

        self.assertFalse(result["success"])
        self.assertIn("购物车订单购物车支付成功", result["error"])

    def test_assert_image_checks_exists(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            image_path = tmp.name
        try:
            runner = TestRunner(device_serial="android-1")
            runner.d = _FakeDevice(image_wait_results=[{"point": [10, 10], "similarity": 0.95}])
            step = Step(
                action=ActionType.ASSERT_IMAGE,
                selector=image_path,
                selector_type=SelectorType.IMAGE,
                options={"match_mode": "exists"},
            )

            result = runner.execute_step(step, variables={})

            self.assertTrue(result["success"])
        finally:
            if os.path.exists(image_path):
                os.remove(image_path)

    def test_assert_image_checks_not_exists(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            image_path = tmp.name
        try:
            runner = TestRunner(device_serial="android-1")
            runner.d = _FakeDevice(image_wait_results=[None])
            step = Step(
                action=ActionType.ASSERT_IMAGE,
                selector=image_path,
                selector_type=SelectorType.IMAGE,
                options={"match_mode": "not_exists"},
            )

            result = runner.execute_step(step, variables={})

            self.assertTrue(result["success"])
        finally:
            if os.path.exists(image_path):
                os.remove(image_path)

    def test_assert_image_not_exists_allows_transient_first_match(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            image_path = tmp.name
        try:
            runner = TestRunner(device_serial="android-1")
            runner.d = _FakeDevice(image_wait_results=[{"point": [10, 10], "similarity": 0.95}, None])
            step = Step(
                action=ActionType.ASSERT_IMAGE,
                selector=image_path,
                selector_type=SelectorType.IMAGE,
                options={"match_mode": "not_exists"},
            )

            result = runner.execute_step(step, variables={})

            self.assertTrue(result["success"])
        finally:
            if os.path.exists(image_path):
                os.remove(image_path)

    def test_assert_image_not_exists_fails_fast_on_strong_match(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            image_path = tmp.name
        try:
            runner = TestRunner(device_serial="android-1")
            runner.d = _FakeDevice(image_wait_results=[{"point": [10, 10], "similarity": 0.999}])
            step = Step(
                action=ActionType.ASSERT_IMAGE,
                selector=image_path,
                selector_type=SelectorType.IMAGE,
                options={"match_mode": "not_exists"},
            )

            result = runner.execute_step(step, variables={})

            self.assertFalse(result["success"])
            self.assertEqual(len(runner.d.image.wait_calls), 1)
        finally:
            if os.path.exists(image_path):
                os.remove(image_path)


if __name__ == "__main__":
    unittest.main()
