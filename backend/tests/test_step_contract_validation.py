import unittest

from pydantic import ValidationError

from backend.schemas import PlatformOverrides, TestCaseStepWrite
from backend.step_contract import (
    normalize_action,
    normalize_execute_on,
    normalize_platform_overrides,
)


class StepContractValidationTests(unittest.TestCase):
    def test_normalize_action_maps_alias_to_lowercase(self):
        self.assertEqual(normalize_action("CLICK_IMAGE"), "click_image")
        self.assertEqual(normalize_action("ASSERT_IMAGE"), "assert_image")

    def test_normalize_execute_on_normalizes_and_dedupes(self):
        self.assertEqual(
            normalize_execute_on(["Android", "ios", "ANDROID"]),
            ["android", "ios"],
        )

    def test_normalize_execute_on_rejects_unknown_platform(self):
        with self.assertRaises(ValueError) as context:
            normalize_execute_on(["android", "web"])

        self.assertIn("unsupported platform", str(context.exception))

    def test_normalize_platform_overrides_rejects_partial_selector_shape(self):
        with self.assertRaises(ValueError) as context:
            normalize_platform_overrides({"ios": {"selector": "登录"}})

        self.assertIn("requires both selector and by", str(context.exception))

    def test_normalize_platform_overrides_rejects_unknown_platform_key(self):
        with self.assertRaises(ValueError) as context:
            normalize_platform_overrides({"web": {"selector": "登录", "by": "text"}})

        self.assertIn("unsupported platform", str(context.exception))

    def test_platform_overrides_schema_forbids_extra_fields(self):
        with self.assertRaises(ValidationError):
            PlatformOverrides.model_validate(
                {"android": {"selector": "登录", "by": "text", "foo": "bar"}}
            )

    def test_step_write_accepts_valid_platform_overrides(self):
        step = TestCaseStepWrite(
            action="click",
            platform_overrides={"android": {"selector": "登录", "by": "text"}},
        )

        self.assertEqual(step.platform_overrides.android.selector, "登录")
        self.assertEqual(step.platform_overrides.android.by, "text")


if __name__ == "__main__":
    unittest.main()
