import json
import unittest

from sqlmodel import SQLModel, Session, create_engine

from backend.cross_platform_execution import prepare_steps_for_platform
from backend.models import SystemSetting


class AppKeyMappingPrecheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)

    def tearDown(self) -> None:
        self.session.close()

    def _save_setting(self, key: str, value: str) -> None:
        self.session.add(SystemSetting(key=key, value=value))
        self.session.commit()

    def test_ios_uses_unified_app_key_map(self):
        self._save_setting(
            "app_key_map",
            json.dumps(
                {
                    "mall_app": {
                        "android": "com.demo.mall.android",
                        "ios": "com.demo.mall.ios",
                    }
                }
            ),
        )
        steps = [
            {
                "action": "start_app",
                "args": {"app_key": "mall_app"},
                "execute_on": ["android", "ios"],
            }
        ]

        prepared = prepare_steps_for_platform(self.session, steps, platform="ios")
        self.assertEqual(prepared[0]["args"]["app_key"], "com.demo.mall.ios")

    def test_ios_uses_platform_split_app_key_map(self):
        self._save_setting(
            "app_key_map",
            json.dumps(
                {
                    "android": {"mall_app": "com.demo.mall.android"},
                    "ios": {"mall_app": "com.demo.mall.ios"},
                }
            ),
        )
        steps = [{"action": "stop_app", "args": {"app_key": "mall_app"}}]

        prepared = prepare_steps_for_platform(self.session, steps, platform="ios")
        self.assertEqual(prepared[0]["args"]["app_key"], "com.demo.mall.ios")

    def test_ios_uses_legacy_split_settings(self):
        self._save_setting("android_app_map", json.dumps({"mall_app": "com.demo.mall.android"}))
        self._save_setting("ios_app_map", json.dumps({"mall_app": "com.demo.mall.ios"}))
        steps = [{"action": "start_app", "args": {"app_key": "mall_app"}}]

        prepared = prepare_steps_for_platform(self.session, steps, platform="ios")
        self.assertEqual(prepared[0]["args"]["app_key"], "com.demo.mall.ios")

    def test_ios_missing_mapping_raises_p1004(self):
        steps = [{"action": "start_app", "args": {"app_key": "mall_app"}}]

        with self.assertRaises(RuntimeError) as context:
            prepare_steps_for_platform(self.session, steps, platform="ios")
        self.assertIn("P1004_APP_MAPPING_MISSING", str(context.exception))

    def test_ios_fallback_keeps_raw_bundle_id_when_mapping_missing(self):
        steps = [{"action": "start_app", "args": {"app_key": "com.demo.mall.ios"}}]

        prepared = prepare_steps_for_platform(self.session, steps, platform="ios")
        self.assertEqual(prepared[0]["args"]["app_key"], "com.demo.mall.ios")

    def test_android_fallback_keeps_raw_app_key_when_mapping_missing(self):
        steps = [{"action": "start_app", "args": {"app_key": "com.demo.mall.android"}}]

        prepared = prepare_steps_for_platform(self.session, steps, platform="android")
        self.assertEqual(prepared[0]["args"]["app_key"], "com.demo.mall.android")

    def test_ios_skip_step_does_not_require_mapping(self):
        steps = [
            {
                "action": "start_app",
                "args": {"app_key": "mall_app"},
                "execute_on": ["android"],
            }
        ]

        prepared = prepare_steps_for_platform(self.session, steps, platform="ios")
        self.assertEqual(prepared[0]["args"]["app_key"], "mall_app")

if __name__ == "__main__":
    unittest.main()
