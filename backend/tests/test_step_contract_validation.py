import unittest

from pydantic import ValidationError

from backend.schemas import PlatformOverrides, Step, TestCaseStepWrite
from backend.step_contract import (
    legacy_step_to_standard,
    normalize_action,
    normalize_execute_on,
    normalize_platform_overrides,
    normalize_retry_count,
    standard_step_to_legacy,
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


class RetryCountContractTests(unittest.TestCase):
    def test_normalize_retry_count_accepts_valid_values(self):
        self.assertEqual(normalize_retry_count(None), 0)
        self.assertEqual(normalize_retry_count(""), 0)
        self.assertEqual(normalize_retry_count(0), 0)
        self.assertEqual(normalize_retry_count(3), 3)
        self.assertEqual(normalize_retry_count("2"), 2)

    def test_normalize_retry_count_rejects_negative(self):
        with self.assertRaises(ValueError) as context:
            normalize_retry_count(-1)

        self.assertIn("non-negative integer", str(context.exception))

    def test_normalize_retry_count_rejects_over_limit(self):
        with self.assertRaises(ValueError) as context:
            normalize_retry_count(4)

        self.assertIn("cannot exceed 3", str(context.exception))

    def test_normalize_retry_count_rejects_non_integer(self):
        for value in ("abc", 1.5, True, {}):
            with self.assertRaises(ValueError):
                normalize_retry_count(value)

    def test_legacy_step_to_standard_carries_retry_count(self):
        standard = legacy_step_to_standard(
            {
                "action": "click",
                "selector": "com.demo:id/login",
                "selector_type": "resourceId",
                "retry_count": 2,
            },
            case_id=1,
            order=1,
        )
        self.assertEqual(standard["retry_count"], 2)

    def test_legacy_step_to_standard_defaults_and_clamps_retry_count(self):
        standard_default = legacy_step_to_standard(
            {"action": "back"},
            case_id=1,
            order=1,
        )
        self.assertEqual(standard_default["retry_count"], 0)

        standard_clamped = legacy_step_to_standard(
            {"action": "back", "retry_count": 99},
            case_id=1,
            order=1,
        )
        self.assertEqual(standard_clamped["retry_count"], 3)

        standard_invalid = legacy_step_to_standard(
            {"action": "back", "retry_count": "oops"},
            case_id=1,
            order=1,
        )
        self.assertEqual(standard_invalid["retry_count"], 0)

    def test_standard_step_to_legacy_carries_retry_count(self):
        legacy = standard_step_to_legacy(
            {
                "action": "click",
                "platform_overrides": {"android": {"selector": "登录", "by": "text"}},
                "retry_count": 3,
            }
        )
        self.assertEqual(legacy["retry_count"], 3)

        legacy_default = standard_step_to_legacy({"action": "back"})
        self.assertEqual(legacy_default["retry_count"], 0)

    def test_legacy_step_schema_parses_retry_count_leniently(self):
        step = Step(action="click", retry_count=2)
        self.assertEqual(step.retry_count, 2)

        clamped = Step(action="click", retry_count=9)
        self.assertEqual(clamped.retry_count, 3)

        fallback = Step(action="click", retry_count="oops")
        self.assertEqual(fallback.retry_count, 0)

    def test_step_write_schema_validates_retry_count_range(self):
        step = TestCaseStepWrite(action="click", retry_count=3)
        self.assertEqual(step.retry_count, 3)

        with self.assertRaises(ValidationError):
            TestCaseStepWrite(action="click", retry_count=4)

        with self.assertRaises(ValidationError):
            TestCaseStepWrite(action="click", retry_count=-1)


if __name__ == "__main__":
    unittest.main()
