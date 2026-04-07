import unittest

from backend.step_contract import legacy_step_to_standard, standard_step_to_legacy


class StepContractClickImageTests(unittest.TestCase):
    def test_legacy_click_image_without_selector_type_maps_to_image_args(self):
        legacy_step = {
            "action": "click_image",
            "selector": "static/images/a.png",
            "selector_type": None,
            "value": "",
            "options": {},
            "timeout": 10,
            "error_strategy": "ABORT",
        }

        standard = legacy_step_to_standard(legacy_step, case_id=1, order=1)

        self.assertEqual(standard["action"], "click_image")
        self.assertEqual(standard["args"].get("image_path"), "static/images/a.png")
        self.assertEqual(
            standard["platform_overrides"].get("android"),
            {"selector": "static/images/a.png", "by": "image"},
        )

    def test_standard_click_image_can_restore_legacy_selector_from_args(self):
        standard_step = {
            "action": "click_image",
            "args": {"image_path": "static/images/a.png"},
            "platform_overrides": {},
            "execute_on": ["android", "ios"],
            "timeout": 10,
            "error_strategy": "ABORT",
        }

        legacy = standard_step_to_legacy(standard_step)

        self.assertEqual(legacy["action"], "click_image")
        self.assertEqual(legacy["selector"], "static/images/a.png")

    def test_legacy_assert_image_maps_match_mode_and_image_args(self):
        legacy_step = {
            "action": "assert_image",
            "selector": "static/images/a.png",
            "selector_type": "image",
            "value": "",
            "options": {"match_mode": "not_exists"},
            "timeout": 10,
            "error_strategy": "ABORT",
        }

        standard = legacy_step_to_standard(legacy_step, case_id=1, order=1)

        self.assertEqual(standard["action"], "assert_image")
        self.assertEqual(standard["args"].get("image_path"), "static/images/a.png")
        self.assertEqual(standard["args"].get("match_mode"), "not_exists")
        self.assertEqual(
            standard["platform_overrides"].get("android"),
            {"selector": "static/images/a.png", "by": "image"},
        )

    def test_standard_assert_image_can_restore_legacy_match_mode(self):
        standard_step = {
            "action": "assert_image",
            "args": {"image_path": "static/images/a.png", "match_mode": "not_exists"},
            "platform_overrides": {},
            "execute_on": ["android", "ios"],
            "timeout": 10,
            "error_strategy": "ABORT",
        }

        legacy = standard_step_to_legacy(standard_step)

        self.assertEqual(legacy["action"], "assert_image")
        self.assertEqual(legacy["selector"], "static/images/a.png")
        self.assertEqual(legacy["options"].get("match_mode"), "not_exists")


if __name__ == "__main__":
    unittest.main()
