import unittest

from backend.locator_resolution import resolve_locator_candidates


class LocatorResolutionTests(unittest.TestCase):
    def test_android_args_candidates_are_ordered_before_legacy_locator(self):
        step = {
            "args": {
                "locator_candidates": [
                    {"selector": "购物车", "by": "description"},
                    {"selector": "购物车", "by": "text"},
                    {"selector": "//*[@text='购物车'][1]", "by": "xpath"},
                ]
            },
            "selector": "legacy-id",
            "selector_type": "ID",
            "platform_overrides": {},
        }
        self.assertEqual(
            resolve_locator_candidates(step, platform="android"),
            [
                {"selector": "购物车", "by": "description"},
                {"selector": "购物车", "by": "text"},
                {"selector": "//*[@text='购物车'][1]", "by": "xpath"},
            ],
        )

    def test_explicit_android_override_precedes_args_candidates(self):
        step = {
            "args": {
                "locator_candidates": [
                    {"selector": "购物车", "by": "description"},
                ]
            },
            "platform_overrides": {
                "android": {"selector": "显式定位", "by": "text"},
            },
        }
        self.assertEqual(
            resolve_locator_candidates(step, platform="android"),
            [{"selector": "显式定位", "by": "text"}],
        )

    def test_ios_direct_override_kept_for_short_id(self):
        step = {
            "platform_overrides": {
                "ios": {"selector": "login_button", "by": "id"},
                "android": {"selector": "登录", "by": "text"},
            }
        }
        result = resolve_locator_candidates(step, platform="ios")
        self.assertEqual(result, [{"selector": "login_button", "by": "id"}])

    def test_ios_verbose_id_prefers_android_text_mapping(self):
        step = {
            "platform_overrides": {
                "ios": {
                    "selector": "Haier/海尔 505升风冷变频多门冰箱 BCD-505WGCFDM4WKU1, 自营, ¥8000, 14人加购",
                    "by": "id",
                },
                "android": {"selector": "505升风冷变频多门冰箱", "by": "text"},
            }
        }
        result = resolve_locator_candidates(step, platform="ios")
        self.assertGreaterEqual(len(result), 2)
        self.assertEqual(result[0], {"selector": "505升风冷变频多门冰箱", "by": "label"})
        self.assertEqual(result[1], {"selector": "505升风冷变频多门冰箱", "by": "name"})
        self.assertEqual(result[-1]["by"], "id")

    def test_ios_verbose_id_without_android_fallback_keeps_direct(self):
        step = {
            "platform_overrides": {
                "ios": {
                    "selector": "Haier/海尔 505升风冷变频多门冰箱 BCD-505WGCFDM4WKU1, 自营, ¥8000, 14人加购",
                    "by": "id",
                }
            }
        }
        result = resolve_locator_candidates(step, platform="ios")
        self.assertEqual(
            result,
            [
                {
                    "selector": "Haier/海尔 505升风冷变频多门冰箱 BCD-505WGCFDM4WKU1, 自营, ¥8000, 14人加购",
                    "by": "id",
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
