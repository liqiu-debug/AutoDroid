import unittest

from backend.ios_step_autofill import autofill_step_for_ios


class IOSStepAutofillTests(unittest.TestCase):
    def test_locator_text_generates_ios_label_override(self):
        step = {
            "action": "click",
            "execute_on": ["android"],
            "platform_overrides": {
                "android": {"selector": "登录", "by": "text"},
            },
        }

        updated, meta = autofill_step_for_ios(step, app_mapping={})

        self.assertTrue(meta["changed"])
        self.assertIn("generate_ios_override_from_android", meta["changes"])
        self.assertIn("add_execute_on_ios", meta["changes"])
        self.assertEqual(
            updated["platform_overrides"]["ios"],
            {"selector": "登录", "by": "label"},
        )
        self.assertEqual(updated["execute_on"], ["android", "ios"])

    def test_locator_description_generates_ios_id_override(self):
        step = {
            "action": "assert_text",
            "execute_on": ["android"],
            "platform_overrides": {
                "android": {"selector": "搜索", "by": "description"},
            },
        }

        updated, meta = autofill_step_for_ios(step, app_mapping={})

        self.assertTrue(meta["changed"])
        self.assertEqual(
            updated["platform_overrides"]["ios"],
            {"selector": "搜索", "by": "id"},
        )
        self.assertEqual(updated["execute_on"], ["android", "ios"])

    def test_no_locator_action_adds_ios_execute_on(self):
        step = {
            "action": "home",
            "execute_on": ["android"],
            "platform_overrides": {},
        }

        updated, meta = autofill_step_for_ios(step, app_mapping={})

        self.assertTrue(meta["changed"])
        self.assertIn("add_execute_on_ios", meta["changes"])
        self.assertEqual(meta["blockers"], [])
        self.assertEqual(updated["execute_on"], ["android", "ios"])

    def test_click_image_adds_ios_execute_on(self):
        step = {
            "action": "click_image",
            "execute_on": ["android"],
            "args": {"image_path": "static/images/a.png"},
            "platform_overrides": {},
        }

        updated, meta = autofill_step_for_ios(step, app_mapping={})

        self.assertTrue(meta["changed"])
        self.assertIn("add_execute_on_ios", meta["changes"])
        self.assertEqual(meta["blockers"], [])
        self.assertEqual(updated["execute_on"], ["android", "ios"])

    def test_start_app_requires_ios_mapping(self):
        step = {
            "action": "start_app",
            "execute_on": ["android"],
            "args": {"app_key": "mall_app"},
            "platform_overrides": {},
        }

        updated, meta = autofill_step_for_ios(step, app_mapping={})

        self.assertFalse(meta["changed"])
        self.assertIn("missing_ios_app_mapping", meta["blockers"])
        self.assertEqual(updated["execute_on"], ["android"])

    def test_start_app_adds_ios_when_mapping_exists(self):
        step = {
            "action": "start_app",
            "execute_on": ["android"],
            "args": {"app_key": "mall_app"},
            "platform_overrides": {},
        }

        updated, meta = autofill_step_for_ios(
            step,
            app_mapping={"mall_app": {"ios": "com.demo.mall.ios"}},
        )

        self.assertTrue(meta["changed"])
        self.assertIn("add_execute_on_ios", meta["changes"])
        self.assertEqual(updated["execute_on"], ["android", "ios"])

    def test_locator_without_android_override_keeps_android_only(self):
        step = {
            "action": "input",
            "execute_on": ["android"],
            "args": {"text": "abc"},
            "platform_overrides": {},
        }

        updated, meta = autofill_step_for_ios(step, app_mapping={})

        self.assertFalse(meta["changed"])
        self.assertIn("missing_or_unmappable_android_override", meta["blockers"])
        self.assertIn("missing_ios_override", meta["blockers"])
        self.assertEqual(updated["execute_on"], ["android"])


if __name__ == "__main__":
    unittest.main()
