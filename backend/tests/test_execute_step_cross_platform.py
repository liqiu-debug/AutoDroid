import json
import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException

from backend.api.recording import (
    SingleStepPayload,
    _cross_platform_result_to_legacy_payload,
    _normalize_single_step_for_runner,
    execute_single_step,
)
from backend.schemas import Step


def _fake_request(accept_encoding: str = ""):
    request = Mock()
    request.headers = {"accept-encoding": accept_encoding}
    return request


def _response_json(response):
    return json.loads(response.body)


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

    @patch("backend.api.recording._wait_ui_stable", return_value=None)
    @patch("backend.api.recording._build_device_dump_payload", return_value={"device_info": {}, "hierarchy_xml": "<xml />", "screenshot": "abc"})
    @patch("backend.api.recording._resolve_recording_platform", return_value="android")
    @patch("backend.api.recording.CrossPlatformRunner")
    def test_execute_single_step_android_uses_cross_platform_runner(
        self,
        runner_cls,
        resolve_platform_mock,
        build_dump_mock,
        wait_ui_mock,
    ):
        session = Mock()

        runner = Mock()
        runner.driver = Mock()
        runner.run_step.return_value = {
            "action": "assert_text",
            "status": "PASS",
            "platform": "android",
            "device_id": "android-1",
            "error_strategy": "ABORT",
            "duration": 0.2,
            "error": None,
            "output": None,
            "artifacts": None,
            "step": {
                "action": "assert_text",
                "args": {"expected_text": "登录成功", "match_mode": "contains"},
                "value": "登录成功",
                "execute_on": ["android"],
                "platform_overrides": {},
                "timeout": 10,
                "error_strategy": "ABORT",
                "description": "断言页面包含登录成功",
            },
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

        response = _response_json(execute_single_step(_fake_request(), payload, session=session))

        self.assertTrue(response["result"]["success"])
        self.assertEqual(response["result"]["status"], "PASS")
        self.assertEqual(response["dump"]["screenshot"], "abc")
        self.assertTrue(build_dump_mock.call_args.kwargs["include_screenshot"])
        runner_cls.assert_called_once_with(platform="android", device_id="android-1")
        normalized_step = runner.run_step.call_args.args[0]
        self.assertEqual(normalized_step["action"], "assert_text")
        self.assertEqual(normalized_step["args"].get("expected_text"), "登录成功")
        resolve_platform_mock.assert_called_once_with(session, "android-1")
        runner.disconnect.assert_called_once()

    @patch("backend.api.recording._wait_ui_stable", return_value=None)
    @patch("backend.api.recording._build_device_dump_payload", return_value={"hierarchy_xml": "<xml />"})
    @patch("backend.api.recording._resolve_recording_platform", return_value="android")
    @patch("backend.api.recording.CrossPlatformRunner")
    def test_execute_single_step_can_skip_screenshot(
        self,
        runner_cls,
        resolve_platform_mock,
        build_dump_mock,
        wait_ui_mock,
    ):
        runner = Mock()
        runner.driver = Mock()
        runner.run_step.return_value = {
            "action": "click",
            "status": "PASS",
            "platform": "android",
            "device_id": "android-1",
            "error_strategy": "ABORT",
            "duration": 0.1,
            "error": None,
            "output": None,
            "artifacts": None,
            "step": {"action": "click", "execute_on": ["android"], "platform_overrides": {}},
        }
        runner_cls.return_value = runner

        payload = SingleStepPayload(
            step={"action": "click", "selector": "登录", "selector_type": "text"},
            device_serial="android-1",
            include_screenshot=False,
        )

        execute_single_step(_fake_request(), payload, session=Mock())

        # 投屏模式下响应 dump 跳过整图截图（远程弱链路每步省一张整图）
        self.assertFalse(build_dump_mock.call_args.kwargs["include_screenshot"])

    def test_execute_single_step_without_device_returns_400(self):
        payload = SingleStepPayload(
            step={"action": "click", "selector": "登录", "selector_type": "text"},
            device_serial=None,
        )

        with self.assertRaises(HTTPException) as ctx:
            execute_single_step(_fake_request(), payload, session=Mock())

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("请选择执行设备", str(ctx.exception.detail))

    @patch("backend.api.recording.time.sleep", return_value=None)
    @patch("backend.api.recording._build_device_dump_payload", return_value={"device_info": {}, "hierarchy_xml": "<xml />", "screenshot": "abc"})
    @patch("backend.api.recording.check_wda_health")
    @patch("backend.api.recording.resolve_ios_wda_url", return_value="http://127.0.0.1:8200")
    @patch("backend.api.recording.is_flag_enabled", return_value=True)
    @patch("backend.api.recording.resolve_device_platform", return_value="ios")
    @patch("backend.api.recording.CrossPlatformRunner")
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

        response = _response_json(execute_single_step(_fake_request(), payload, session=session))

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
