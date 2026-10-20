"""iOS 运行链路「无真机」集成测试。

用 backend/tests/support/ios_runtime_harness.mjs（Node 扮演原生原语）把
「平台生成的脚本包 → adapter_js → runtime.js → 报告回传」整条 JS 链路跑一遍：

- 覆盖：脚本包结构、runtime.js 步骤语义、适配层 API 覆盖面、报告载荷字段；
- 不覆盖：原生层（XCTest 实际操作），需真机或模拟器用
  `python -m backend.autojs_export.selftest --url http://<设备>:8100` 验证。

node 缺失时跳过（不影响后端测试套件）。
"""
import json
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path

from backend.autojs_export import selftest
from backend.autojs_export.generator import build_script_step
from backend.autojs_export.packager import _case_module_source

HARNESS = Path(__file__).resolve().parent / "support" / "ios_runtime_harness.mjs"
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "需要 node 运行设备端 JS 链路试验台")
class IOSRuntimeHarnessTests(unittest.TestCase):

    def action_bundle(self, action, overrides=""):
        args = {
            "input": {"text": "新内容"}, "assert_text": {"expected_text": "设置"},
            "assert_image": {"image": "target.png"}, "click_image": {"image": "target.png"},
            "extract_by_ocr": {"region": [0, 0, 1, 1], "extract_rule": {"extract_rule": "regex", "custom_regex": "(HARNESS)"}, "output_var": "VALUE"},
            "sleep": {"seconds": 0.01}, "swipe": {"direction": "up"},
            "start_app": {"bundle_id": "com.apple.Preferences"},
            "stop_app": {"bundle_id": "com.apple.Preferences"},
        }.get(action, {})
        locators = [{"by": "text", "selector": "设置"}] if action in ("click", "input", "wait_until_exists") else []
        bundle = selftest.build_selftest_bundle("ios")
        bundle["assets"] = {"target.png": "aW1hZ2U="}  # Native image decode is stubbed here.
        bundle["case_files"][0]["content"] = _case_module_source({"case_id": 0, "name": action, "steps": [
            selftest._step(1, action, args=args, locators=locators, timeout=1)
        ]})
        bundle["main_js"] = (
            "global.__adt.findElements = function () { return [1]; };\n"
            "global.__adt.ocr = function () { return ['HARNESS']; };\n"
            "global.__adt.matchTemplate = function () { return {similarity: 1, x: 60, y: 90}; };\n"
            + overrides + "\n" + bundle["main_js"]
        )
        return bundle

    def test_all_thirteen_actions_execute_and_release_native_images(self):
        for action in ("click", "input", "wait_until_exists", "assert_text", "assert_image", "click_image",
                       "extract_by_ocr", "sleep", "swipe", "back", "home", "start_app", "stop_app"):
            with self.subTest(action=action):
                outcome = self._run_harness(self.action_bundle(action))
                self.assertTrue(outcome["result"]["success"], outcome["logs"])
                self.assertTrue(outcome["result"]["upload_ok"])
                created = {call["result"]["id"] for call in outcome["apiCalls"] if call["name"] == "createImage"}
                released = [call["args"]["id"] for call in outcome["apiCalls"] if call["name"] == "releaseImage"]
                self.assertEqual(created, set(released))
                self.assertEqual(len(released), len(set(released)), "double release")
                if action == "click_image":
                    taps = [call["args"] for call in outcome["apiCalls"] if call["name"] == "tap"]
                    # Native coordinates are screenshot pixels; tap the 40x40 template's center.
                    self.assertEqual(taps, [{"x": 80, "y": 110}])
                if action == "extract_by_ocr":
                    self.assertEqual(outcome["reportPayload"]["cases"][0]["steps"][0]["output"]["ocr_value"], "HARNESS")

    def test_native_failures_never_report_pass_and_failed_reports_are_uploaded(self):
        failures = {
            "click": "elementClick", "input": "elementSetText", "swipe": "drag",
            "back": "pressBack", "home": "pressHome", "start_app": "launchApp", "stop_app": "terminateApp",
            "click_image": "tap", "assert_image": "matchTemplate", "extract_by_ocr": "ocr",
            "assert_text": "findElements", "wait_until_exists": "findElements",
        }
        for action, primitive in failures.items():
            with self.subTest(action=action):
                empty = "[]" if primitive in ("ocr", "findElements") else "false"
                overrides = (
                    f"global.__adt.{primitive} = function () {{ return {empty}; }};\n"
                    "global.__adt.typeText = function () { return false; };\n"
                    # Advance timeout loops without sleeping in the native stub.
                    "var testClock = Date.now(); Date.now = function () { return testClock += 1000; };\n"
                )
                outcome = self._run_harness(self.action_bundle(action, overrides))
                self.assertFalse(outcome["result"]["success"], outcome["logs"])
                self.assertTrue(outcome["result"]["upload_ok"])
                step = outcome["reportPayload"]["cases"][0]["steps"][0]
                self.assertEqual(step["status"], "FAIL")
                self.assertTrue(step["error"])
                created = {call["result"]["id"] for call in outcome["apiCalls"] if call["name"] == "createImage"}
                released = {call["args"]["id"] for call in outcome["apiCalls"] if call["name"] == "releaseImage"}
                self.assertEqual(created, released)

    def test_start_app_rejects_wrong_foreground(self):
        outcome = self._run_harness(self.action_bundle("start_app",
            "global.__adt.activeApp = function () { return 'com.apple.springboard'; };\n"
            "var testClock = Date.now(); Date.now = function () { return testClock += 1000; };"))
        self.assertFalse(outcome["result"]["success"])
        self.assertIn("未进入前台", outcome["reportPayload"]["cases"][0]["steps"][0]["error"])

    def _run_harness(self, bundle: dict) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            bundle_path = Path(tmp) / "bundle.json"
            bundle_path.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
            completed = subprocess.run(
                [NODE, str(HARNESS), str(bundle_path)],
                capture_output=True,
                text=True,
                timeout=240,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr[-2000:])
            return json.loads(completed.stdout)

    def test_ios_selftest_bundle_runs_through_runtime_js(self):
        outcome = self._run_harness(selftest.build_selftest_bundle("ios"))
        self.assertIsNone(outcome["exception"], outcome.get("logs"))
        self.assertTrue(outcome["ok"])
        steps = outcome["reportPayload"]["cases"][0]["steps"]
        self.assertEqual(
            [step["status"] for step in steps],
            ["PASS"] * len(steps),
            [step.get("error") for step in steps],
        )
        actions = [step["action"] for step in steps]
        self.assertIn("start_app", actions)
        self.assertIn("extract_by_ocr", actions)
        self.assertIn("swipe", actions)
        self.assertTrue(outcome["result"]["upload_ok"])
        self.assertTrue(outcome["result"]["success"])

    def test_report_payload_marks_ios_platform_and_device(self):
        outcome = self._run_harness(selftest.build_selftest_bundle("ios"))
        payload = outcome["reportPayload"]
        self.assertEqual(payload["platform"], "ios")
        self.assertEqual(payload["kind"], "case")
        self.assertFalse(payload["notify"])
        self.assertTrue(payload["run_id"])
        self.assertEqual(payload["device"]["brand"], "Apple")
        # 适配层把 device.width/height 暴露为截图像素尺寸，runtime.js 直接使用
        self.assertEqual(payload["device"]["resolution"], "1170x2532")

    def test_generated_app_mapping_reaches_native_launch_and_terminate(self):
        bundle = selftest.build_selftest_bundle("ios")
        steps = []
        for order, action in enumerate(("start_app", "stop_app"), start=1):
            step, _ = build_script_step(
                {"order": order, "action": action, "args": {"app_key": "settings"}},
                variables={},
                app_key_map={"settings": {"android": "com.android.settings", "ios": "com.apple.Preferences"}},
                platform="ios",
            )
            steps.append(step)
        bundle["case_files"][0]["content"] = _case_module_source(
            {"case_id": 0, "name": "应用映射", "steps": steps}
        )
        outcome = self._run_harness(bundle)
        self.assertTrue(outcome["result"]["success"], outcome["logs"])
        calls = [call for call in outcome["apiCalls"] if call["name"] in ("launchApp", "terminateApp")]
        self.assertEqual([call["name"] for call in calls], ["launchApp", "terminateApp"])
        self.assertEqual([call["args"]["bundleId"] for call in calls], ["com.apple.Preferences"] * 2)

    def test_ios_manifest_selects_bundle_id_without_platform_in_run_config(self):
        bundle = selftest.build_selftest_bundle("ios")
        bundle["main_js"] = "delete global.__AUTODROID__.platform;\n" + bundle["main_js"]
        outcome = self._run_harness(bundle)
        self.assertTrue(outcome["result"]["success"], outcome["logs"])
        self.assertEqual(outcome["reportPayload"]["platform"], "ios")
        calls = [call for call in outcome["apiCalls"] if call["name"] == "launchApp"]
        self.assertEqual(calls[0]["args"]["bundleId"], "com.apple.Preferences")

    def test_stop_request_prevents_later_actions_and_reports_aborted(self):
        bundle = selftest.build_selftest_bundle("ios")
        bundle["main_js"] = "global.__AUTODROID_SHOULD_STOP__ = function () { return true; };\n" + bundle["main_js"]
        outcome = self._run_harness(bundle)
        self.assertIsNone(outcome["exception"])
        self.assertTrue(outcome["reportPayload"]["aborted"])
        self.assertFalse(outcome["reportPayload"]["success"])
        steps = outcome["reportPayload"]["cases"][0]["steps"]
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["error_code"], "USER_STOPPED")

    def test_click_failure_still_uploads_failed_report(self):
        bundle = selftest.build_selftest_bundle("ios")
        bundle["case_files"][0]["content"] = _case_module_source({
            "case_id": 0, "name": "点击失败回传", "steps": [
                selftest._step(1, "click", locators=[{"by": "text", "selector": "我的"}]),
                selftest._step(2, "home"),
            ],
        })
        bundle["main_js"] = "global.__adt.elementClick = function () { return false; };\n" + bundle["main_js"]
        outcome = self._run_harness(bundle)
        self.assertIsNone(outcome["exception"])
        self.assertFalse(outcome["result"]["success"])
        self.assertTrue(outcome["result"]["upload_ok"])
        steps = outcome["reportPayload"]["cases"][0]["steps"]
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["status"], "FAIL")
        self.assertIn("点击", steps[0]["error"])
        self.assertTrue(outcome["reportJson"]["upload_response"])

    def test_network_failure_keeps_report_and_diagnostic_for_retry(self):
        bundle = selftest.build_selftest_bundle("ios")
        bundle["main_js"] = (
            'global.__adt.httpRequest = function () { return {status: 0, body: "Local network prohibited"}; };\n'
            + bundle["main_js"]
        )
        outcome = self._run_harness(bundle)
        self.assertTrue(outcome["result"]["success"])
        self.assertFalse(outcome["result"]["upload_ok"])
        self.assertIn("Local network prohibited", outcome["result"]["upload_error"])
        self.assertEqual(outcome["reportJson"]["run_id"], "harness-run")
        self.assertIn("Local network prohibited", outcome["reportJson"]["upload_error"])

    def test_ios_bundle_ships_adapter_and_marks_manifest(self):
        bundle = selftest.build_selftest_bundle("ios")
        self.assertIn("adapter_js", bundle)
        self.assertIn("__adt", bundle["adapter_js"])
        self.assertEqual(bundle["manifest"]["platform"], "ios")

    def test_android_bundle_has_no_ios_adapter(self):
        bundle = selftest.build_selftest_bundle("android")
        self.assertNotIn("adapter_js", bundle)
        self.assertEqual(bundle["manifest"]["platform"], "android")

    def test_missing_adapter_is_reported_clearly(self):
        bundle = selftest.build_selftest_bundle("ios")
        bundle.pop("adapter_js")
        outcome = self._run_harness(bundle)
        self.assertFalse(outcome["ok"])
        self.assertIn("adapter_js", outcome["exception"] or "")


class SelftestTransportTests(unittest.TestCase):
    def test_http_unwraps_wda_status_and_errors(self):
        for payload, expected_ok in (({"running": True}, True), ({"error": "unknown error"}, False)):
            response = MagicMock()
            response.__enter__.return_value = response
            response.status = 200
            response.read.return_value = json.dumps({"value": payload, "sessionId": None}).encode()
            with patch.object(selftest.urllib.request, "urlopen", return_value=response):
                result = selftest._http("GET", "http://localhost/status")
            self.assertEqual(result["body"], payload)
            self.assertEqual(result["ok"], expected_ok)

    def test_failed_steps_do_not_count_as_successful_selftest(self):
        responses = [
            {"ok": True, "body": {"running": False}},
            {"ok": True, "body": {"accepted": True}},
            {"ok": True, "body": {"running": False, "last_result": {"success": False}}},
            {"ok": True, "body": {"lines": []}},
        ]
        with patch.object(selftest, "_http", side_effect=responses):
            result = selftest.run_selftest("http://localhost", poll_interval=0, verbose=False)
        self.assertFalse(result["ok"])


class SelftestBundleShapeTests(unittest.TestCase):
    """自检包结构与平台导出包保持一致（不依赖 node）。"""

    def test_bundle_files_match_device_expectations(self):
        bundle = selftest.build_selftest_bundle("ios")
        self.assertEqual(
            sorted(bundle.keys()),
            sorted([
                "format_version", "generator", "kind", "manifest", "runtime_js",
                "main_js", "case_files", "assets", "precheck", "adapter_js",
            ]),
        )
        self.assertEqual(bundle["case_files"][0]["path"], selftest.SELFTEST_CASE_FILE)
        self.assertIn("createContext", bundle["runtime_js"])
        self.assertIn("__loadModule__", bundle["main_js"])
        # 自检步骤不依赖界面语言；expect_text 才追加文案断言
        actions = [step["action"] for step in selftest.build_selftest_steps(platform="ios")]
        self.assertNotIn("assert_text", actions)
        with_text = [
            step["action"]
            for step in selftest.build_selftest_steps(platform="ios", expect_text="设置")
        ]
        self.assertIn("assert_text", with_text)
        self.assertIn("wait_until_exists", with_text)

    def test_selftest_steps_declare_supported_actions(self):
        allowed = {
            "click", "input", "wait_until_exists", "assert_text", "assert_image",
            "click_image", "extract_by_ocr", "sleep", "swipe", "back", "home",
            "start_app", "stop_app",
        }
        steps = selftest.build_selftest_steps(platform="ios", expect_text="设置")
        start_app = next(step for step in steps if step["action"] == "start_app")
        self.assertEqual(start_app["args"]["bundle_id"], selftest.DEFAULT_IOS_SELFTEST_APP)
        self.assertIsNone(start_app["args"]["package"])
        for step in steps:
            self.assertIn(step["action"], allowed)
            self.assertIn("ios", step["execute_on"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
