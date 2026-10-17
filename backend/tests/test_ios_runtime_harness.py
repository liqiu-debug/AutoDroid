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
from pathlib import Path

from backend.autojs_export import selftest

HARNESS = Path(__file__).resolve().parent / "support" / "ios_runtime_harness.mjs"
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "需要 node 运行设备端 JS 链路试验台")
class IOSRuntimeHarnessTests(unittest.TestCase):

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
        for step in selftest.build_selftest_steps(platform="ios", expect_text="设置"):
            self.assertIn(step["action"], allowed)
            self.assertIn("ios", step["execute_on"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
