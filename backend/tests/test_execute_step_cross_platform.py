import unittest
from unittest.mock import Mock, patch

from backend.main import (
    SingleStepPayload,
    _cross_platform_result_to_legacy_payload,
    _normalize_single_step_for_runner,
    execute_single_step,
)
from backend.schemas import Step


class SingleStepCrossPlatformTests(unittest.TestCase):
    def test_step_validation_treats_blank_selector_type_as_none(self):
        step = Step.model_validate(
            {
                "action": "assert_text",
                "selector": "",
                "selector_type": "",
                "value": "登录成功",
                "options": {"match_mode": "contains"},
            }
        )

        self.assertIsNone(step.selector_type)
        self.assertEqual(step.action, "assert_text")

    def test_normalize_single_step_preserves_cross_platform_fields(self):
        raw_step = {
            "action": "click",
            "selector": "登录",
            "selector_type": "text",
            "value": "",
            "description": "Click [登录]",
            "timeout": 12,
            "error_strategy": "ABORT",
            "execute_on": ["android", "ios"],
            "platform_overrides": {
                "android": {"selector": "登录", "by": "text"},
                "ios": {"selector": "登录", "by": "label"},
            },
        }

        step = _normalize_single_step_for_runner(raw_step, case_id=1, default_platform="ios")

        self.assertEqual(step["action"], "click")
        self.assertEqual(step["execute_on"], ["android", "ios"])
        self.assertEqual(step["platform_overrides"]["ios"]["by"], "label")
        self.assertEqual(step["platform_overrides"]["android"]["by"], "text")
        self.assertEqual(step["timeout"], 12)

    def test_cross_platform_result_maps_back_to_legacy_success_shape(self):
        payload = _cross_platform_result_to_legacy_payload(
            {
                "status": "PASS",
                "platform": "ios",
                "device_id": "ios-1",
                "duration": 0.42,
                "error": None,
                "output": None,
                "step": {
                    "action": "click",
                    "args": {},
                    "value": "",
                    "execute_on": ["android", "ios"],
                    "platform_overrides": {
                        "ios": {"selector": "登录", "by": "label"}
                    },
                    "timeout": 10,
                    "error_strategy": "ABORT",
                    "description": "Click [登录]",
                },
            }
        )

        self.assertTrue(payload["success"])
        self.assertEqual(payload["status"], "PASS")
        self.assertEqual(payload["platform"], "ios")
        self.assertEqual(payload["step"]["action"], "click")

    @patch("backend.main.time.sleep", return_value=None)
    @patch("backend.main._take_screenshot_base64", return_value="abc")
    @patch("backend.main._resolve_recording_platform", return_value="android")
    @patch("backend.main.TestRunner")
    def test_execute_single_step_android_accepts_blank_selector_type_for_assert_text(
        self,
        runner_cls,
        resolve_platform_mock,
        screenshot_mock,
        sleep_mock,
    ):
        session = Mock()

        runner = Mock()
        runner.d = Mock()
        runner.d.info = {}
        runner.d.dump_hierarchy.return_value = "<xml />"
        runner.execute_step.return_value = {
            "step": {"action": "assert_text", "selector_type": None},
            "success": True,
            "error": None,
            "duration": 0.2,
        }
        runner_cls.return_value = runner

        payload = SingleStepPayload(
            step={
                "action": "assert_text",
                "selector": "",
                "selector_type": "",
                "value": "登录成功",
                "options": {"match_mode": "contains"},
                "description": "断言页面包含登录成功",
            },
            device_serial="android-1",
        )

        response = execute_single_step(payload, session=session)

        self.assertTrue(response["result"]["success"])
        self.assertEqual(response["dump"]["screenshot"], "abc")
        step_model = runner.execute_step.call_args.args[0]
        self.assertEqual(step_model.action.value, "assert_text")
        self.assertIsNone(step_model.selector_type)
        resolve_platform_mock.assert_called_once_with(session, "android-1")
        runner.connect.assert_called_once()

    @patch("backend.main.time.sleep", return_value=None)
    @patch("backend.main._build_device_dump_payload", return_value={"device_info": {}, "hierarchy_xml": "<xml />", "screenshot": "abc"})
    @patch("backend.main.check_wda_health")
    @patch("backend.main.resolve_ios_wda_url", return_value="http://127.0.0.1:8200")
    @patch("backend.main.is_flag_enabled", return_value=True)
    @patch("backend.main.resolve_device_platform", return_value="ios")
    @patch("backend.main.CrossPlatformRunner")
    def test_execute_single_step_ios_uses_cross_platform_runner(
        self,
        runner_cls,
        resolve_platform_mock,
        is_flag_enabled_mock,
        resolve_wda_mock,
        check_wda_health_mock,
        build_dump_mock,
        sleep_mock,
    ):
        session = Mock()
        session.exec.return_value.all.return_value = []

        runner = Mock()
        runner.driver = Mock()
        runner.run_step.return_value = {
            "action": "click",
            "status": "PASS",
            "platform": "ios",
            "device_id": "ios-1",
            "error_strategy": "ABORT",
            "duration": 0.3,
            "error": None,
            "output": None,
            "artifacts": None,
            "step": {
                "action": "click",
                "args": {},
                "value": "",
                "execute_on": ["android", "ios"],
                "platform_overrides": {"ios": {"selector": "登录", "by": "label"}},
                "timeout": 10,
                "error_strategy": "ABORT",
                "description": "Click [登录]",
            },
        }
        runner_cls.return_value = runner

        payload = SingleStepPayload(
            step={
                "action": "click",
                "selector": "登录",
                "selector_type": "text",
                "value": "",
                "description": "Click [登录]",
                "execute_on": ["android", "ios"],
                "platform_overrides": {
                    "ios": {"selector": "登录", "by": "label"}
                },
            },
            case_id=1,
            env_id=None,
            variables=[],
            device_serial="ios-1",
        )

        response = execute_single_step(payload, session=session)

        self.assertTrue(response["result"]["success"])
        self.assertEqual(response["result"]["platform"], "ios")
        self.assertEqual(response["dump"]["screenshot"], "abc")
        runner_cls.assert_called_once_with(
            platform="ios",
            device_id="ios-1",
            wda_url="http://127.0.0.1:8200",
        )
        normalized_step = runner.run_step.call_args.args[0]
        self.assertEqual(normalized_step["execute_on"], ["android", "ios"])
        self.assertEqual(normalized_step["platform_overrides"]["ios"]["by"], "label")
        resolve_platform_mock.assert_called_once_with(session, "ios-1")
        is_flag_enabled_mock.assert_called_once()
        resolve_wda_mock.assert_called_once_with(session, "ios-1")
        check_wda_health_mock.assert_called_once_with("http://127.0.0.1:8200")


if __name__ == "__main__":
    unittest.main()
