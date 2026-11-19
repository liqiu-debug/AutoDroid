"""Image metadata survives saving/export/execution without changing its platform."""
import copy
import unittest
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

from backend.autojs_export.generator import build_script_step
from backend.autojs_export.precheck import precheck_case_script
from backend.cross_platform_execution import prepare_steps_for_platform
from backend.drivers.cross_platform_runner import DriverFactory, TestCaseRunner
from backend.execution_errors import ExecutionStepRuntimeError
from backend.schemas import Step, TestCaseStepWrite, TestCaseStepUpdate
from backend.schemas import ActionType
from backend.step_contract import legacy_step_to_standard, standard_step_to_legacy
from backend.tests.test_cross_platform_runner import FakeDriver
from backend.utils.image_match_options import preserve_image_references, select_image_options


CONFIG = {"template_sha256": "a" * 64, "tap_anchor": [0.2, 0.8], "source_size": [1080, 2400]}


class ImageDriver(FakeDriver):
    def __init__(self):
        super().__init__("offline")
        self.image_calls = []
        self.image_error = None

    def click_image(self, path, image_options=None, timeout=10):
        self.image_calls.append((path, image_options, timeout))
        if self.image_error:
            raise self.image_error
        return {"image_match": {"status": "matched", "point": [10, 20], "matcher_version": "strict-v1"}}

    def assert_image(self, path, match_mode="exists", image_options=None, timeout=10, abort_event=None):
        self.assert_image_calls.append((path, match_mode, image_options, timeout, abort_event))
        return {"image_match": {"status": "ambiguous", "candidates": [{"accepted": True}], "assertion_mode": match_mode}}


class ImageClickContractTests(unittest.TestCase):
    def step(self):
        return {"action": "click_image", "timeout": 7, "args": {
            "image_path": "static/images/global.png", "image_options": {"android": copy.deepcopy(CONFIG)}},
            "platform_overrides": {"android": {"by": "image", "selector": "static/images/android.png"}}}

    def runner(self, platform="android", driver=None, abort_event=None):
        driver = driver or ImageDriver()
        with patch("backend.drivers.cross_platform_runner.is_driver_pool_enabled", return_value=False), patch.object(DriverFactory, "create", return_value=driver):
            return TestCaseRunner(platform=platform, device_id="offline", abort_event=abort_event)

    def test_round_trip_preserves_platform_metadata(self):
        step = self.step()
        step["args"]["image_options"]["ios"] = {**CONFIG, "tap_anchor": [0.8, 0.2]}
        legacy = standard_step_to_legacy(step)
        standard = legacy_step_to_standard(legacy, 1, 1)
        self.assertEqual(standard["args"]["image_options"], step["args"]["image_options"])

    def test_writer_rejects_invalid_metadata_even_on_another_platform(self):
        malformed = [None, [], {}, {"tap_anchor": [0.5, 0.5]}, {**CONFIG, "tap_anchor": [float("nan"), 0.5]},
                     {**CONFIG, "source_size": [100, 0]}, {**CONFIG, "search_region": None},
                     {**CONFIG, "tap_anchor": [True, 0.5]}, {**CONFIG, "unknown": 1}]
        for config in malformed:
            with self.subTest(config=config):
                options = {"android": CONFIG, "ios": config}
                for model in (TestCaseStepWrite, TestCaseStepUpdate):
                    with self.assertRaises(ValueError):
                        model(action="click_image", args={"image_options": options})
                with self.assertRaises(ValueError):
                    Step(action="click_image", options={"image_options": options})

    def test_runner_uses_current_platform_path_metadata_timeout_and_output(self):
        runner = self.runner()
        result = runner.run_step(self.step())
        self.assertEqual(result["status"], "PASS", result)
        path, config, timeout = runner.driver.image_calls[0]
        self.assertEqual((path, config), ("static/images/android.png", CONFIG))
        self.assertAlmostEqual(timeout, 7, places=2)
        self.assertEqual(result["output"]["image_match"]["status"], "matched")

    def test_independent_legacy_template_does_not_borrow_another_fingerprint(self):
        runner = self.runner("ios")
        result = runner.run_step(self.step())
        self.assertEqual(result["status"], "PASS", result)
        self.assertEqual(runner.driver.image_calls[0][:2], ("static/images/global.png", {}))

    def test_single_platform_recording_borrows_only_template_relative_settings(self):
        for platform, other in (("android", "ios"), ("ios", "android")):
            for action in ("click_image", "assert_image"):
                with self.subTest(platform=platform, action=action):
                    original = {**CONFIG, "search_region": [0, 0, 0.5, 0.5]}
                    step = {"action": action, "args": {"image_path": "recorded.png", "image_options": {other: original}},
                            "platform_overrides": {other: {"by": "image", "selector": "recorded.png"}}}
                    runner = self.runner(platform)
                    result = runner.run_step(step)
                    self.assertEqual(result["status"], "PASS", result)
                    config = (runner.driver.image_calls[0][1] if action == "click_image"
                              else runner.driver.assert_image_calls[0][2])
                    self.assertEqual(config, {"template_sha256": CONFIG["template_sha256"], "tap_anchor": CONFIG["tap_anchor"]})
                    self.assertEqual(step["args"]["image_options"][other], original)
                    self.assertEqual(select_image_options({other: original}, other), original)

    def test_recropped_ios_does_not_invalidate_an_unbound_legacy_android_image(self):
        step = {"action": "click_image", "args": {"image_path": "android-old.png", "image_options": {"ios": CONFIG}},
                "platform_overrides": {"android": {"by": "image", "selector": "android-old.png"},
                                       "ios": {"by": "image", "selector": "ios-new.png"}}}
        runner = self.runner()
        result = runner.run_step(step)
        self.assertEqual(result["status"], "PASS", result)
        self.assertEqual(runner.driver.image_calls[0][:2], ("android-old.png", {}))

    def test_current_platform_and_default_keep_all_geometry(self):
        other = {**CONFIG, "template_sha256": "b" * 64, "search_region": [0, 0, 0.5, 0.5]}
        self.assertEqual(select_image_options({"android": other, "ios": CONFIG, "default": CONFIG}, "android"), other)
        self.assertEqual(select_image_options({"android": CONFIG, "default": other}, "ios"), other)

    def test_preparation_preserves_a_donor_referenced_only_by_the_default_path(self):
        for platform, donor in (("android", "ios"), ("ios", "android")):
            for action in ("click_image", "assert_image"):
                for key in ("image_path", "path"):
                    with self.subTest(platform=platform, action=action, key=key):
                        step = {"action": action, "args": {key: "donor-old.png", "image_options": {donor: CONFIG}},
                                "platform_overrides": {platform: {"by": "image", "selector": "independent.png"}}}
                        original = copy.deepcopy(step)
                        with patch("backend.cross_platform_execution.load_app_key_mapping", return_value={}):
                            prepared = prepare_steps_for_platform(None, [step], platform)[0]
                        self.assertEqual(prepared["platform_overrides"][donor], {"by": "image", "selector": "donor-old.png"})
                        runner = self.runner(platform)
                        self.assertEqual(runner.run_step(prepared)["status"], "PASS")
                        call = runner.driver.image_calls[0] if action == "click_image" else runner.driver.assert_image_calls[0]
                        self.assertEqual(call[0], "independent.png")
                        self.assertEqual(call[1] if action == "click_image" else call[2], {})
                        self.assertEqual(step, original)

    def test_reference_preservation_leaves_existing_inactive_overrides_unchanged(self):
        step = {"action": "click_image", "args": {"path": "legacy.png", "image_options": {"ios": CONFIG}},
                "platform_overrides": {"ios": {"by": "text", "selector": "existing-label"}}}
        original = copy.deepcopy(step)
        result = preserve_image_references(step, step["args"])
        self.assertEqual(result["platform_overrides"]["ios"], original["platform_overrides"]["ios"])
        self.assertEqual(step, original)

    def test_runtime_variables_keep_the_borrowed_template_fingerprint_and_anchor(self):
        for platform, donor in (("android", "ios"), ("ios", "android")):
            for action in ("click_image", "assert_image"):
                for reference in ("path", "selector"):
                    with self.subTest(platform=platform, action=action, reference=reference):
                        step = {"action": action, "args": {"image_options": {donor: CONFIG}}}
                        if reference == "path":
                            step["args"]["path"] = "{{IMAGE}}"
                            step["platform_overrides"] = {donor: {"by": "image", "selector": "{{IMAGE}}"}}
                        else:
                            step["selector"] = "{{IMAGE}}"
                            step["selector_type"] = "image"
                        runner = self.runner(platform)
                        runner.runtime_variables = {"IMAGE": "static/images/same.png"}
                        self.assertEqual(runner.run_step(step)["status"], "PASS")
                        call = runner.driver.image_calls[0] if action == "click_image" else runner.driver.assert_image_calls[0]
                        self.assertEqual(call[0], "static/images/same.png")
                        self.assertEqual(call[1] if action == "click_image" else call[2],
                                         {"template_sha256": CONFIG["template_sha256"], "tap_anchor": CONFIG["tap_anchor"]})

    def test_legacy_single_argument_driver_remains_supported(self):
        runner = self.runner(driver=FakeDriver("offline"))
        self.assertEqual(runner.run_step({"action": "click_image", "args": {"image_path": "old.png"}})["status"], "PASS")

    def test_diagnostics_survive_failure_and_submitted_tap_is_never_retried(self):
        runner = self.runner()
        diagnostic = {"status": "matched", "reason": "tap_failed", "candidates": [{"rect": [1, 2, 3, 4]}]}
        runner.driver.image_error = ExecutionStepRuntimeError("E2009_CLICK_NO_EFFECT", "tap failed", context={"image_match": diagnostic, "tap_submitted": True})
        step = self.step()
        step["retry_count"] = 3
        result = runner.run_step(step)
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(len(runner.driver.image_calls), 1)
        self.assertEqual(result["output"]["image_match"], diagnostic)
        self.assertTrue(result["error_context"]["tap_submitted"])

    def test_image_attempts_share_one_timeout_budget(self):
        clock = [0.0]
        runner = self.runner()
        def fail_image(path, image_options=None, timeout=10):
            runner.driver.image_calls.append(timeout)
            clock[0] += min(0.8, timeout)
            raise ExecutionStepRuntimeError("E2005_IMAGE_NOT_MATCHED", "not found")
        def advance(seconds):
            clock[0] += seconds
            return False
        runner.driver.click_image = fail_image
        runner._sleep_or_abort = advance
        step = self.step()
        step.update(timeout=2, retry_count=3)
        with patch("backend.drivers.cross_platform_runner.time.monotonic", side_effect=lambda: clock[0]):
            result = runner.run_step(step)
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(len(runner.driver.image_calls), 2)
        self.assertAlmostEqual(runner.driver.image_calls[0], 2)
        self.assertAlmostEqual(runner.driver.image_calls[1], 0.2)

    def test_image_driver_receives_user_cancellation_event(self):
        abort = threading.Event()
        runner = self.runner(abort_event=abort)
        received = []
        def image(path, timeout=10, abort_event=None, **kwargs):
            received.append(abort_event)
            abort_event.set()
            raise RuntimeError("执行已被用户中止")
        runner.driver.click_image = image
        result = runner.run_step(self.step())
        self.assertEqual(received, [abort])
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["error"], "执行已被用户中止")

    def test_device_guard_cannot_reset_expired_image_budget(self):
        clock = [0.0]
        runner = self.runner()
        runner.before_device_step = lambda action: clock.__setitem__(0, 3.0)
        step = self.step()
        step.update(timeout=2, retry_count=3)
        with patch("backend.drivers.cross_platform_runner.time.monotonic", side_effect=lambda: clock[0]):
            result = runner.run_step(step)
        self.assertEqual(result["error_code"], "E2012_IMAGE_UNSTABLE")
        self.assertEqual(result["output"]["image_match"]["reason"], "deadline_exceeded")
        self.assertEqual(runner.driver.image_calls, [])

    def test_enum_action_shares_budget_and_guard_consumes_remaining_time(self):
        clock = [0.0]
        runner = self.runner()
        runner.before_device_step = lambda action: clock.__setitem__(0, clock[0] + 0.75)
        step = self.step()
        step.update(action=ActionType.CLICK_IMAGE, timeout=2)
        with patch("backend.drivers.cross_platform_runner.time.monotonic", side_effect=lambda: clock[0]):
            result = runner.run_step(step)
        self.assertEqual(result["status"], "PASS", result)
        self.assertEqual(runner.driver.image_calls[0][2], 1.25)

    def test_export_uses_selected_platform_template_and_flat_options(self):
        step = self.step()
        step["args"]["image_options"] = {"default": CONFIG, "ios": {**CONFIG, "tap_anchor": [0.7, 0.3]}}
        step["platform_overrides"]["ios"] = {"by": "image", "selector": "static/images/element_12345678.png"}
        exported, refs = build_script_step(step, variables={}, app_key_map={}, platform="ios")
        self.assertEqual(exported["args"]["image"], "element_12345678.png")
        self.assertEqual(exported["args"]["image_options"]["tap_anchor"], [0.7, 0.3])
        self.assertEqual(exported["timeout"], 7)
        self.assertEqual(refs, [("element_12345678.png", "static/images/element_12345678.png")])

    def test_export_uses_legacy_binding_for_a_different_platform_template(self):
        exported, _ = build_script_step(self.step(), variables={}, app_key_map={}, platform="ios")
        self.assertEqual(exported["args"]["image_path"], "static/images/global.png")
        self.assertEqual(exported["args"]["image_options"], {})

    def test_export_borrows_the_same_single_platform_template(self):
        step = {"action": "click_image", "args": {"image_options": {"ios": CONFIG}},
                "platform_overrides": {"ios": {"by": "image", "selector": "static/images/ios.png"}}}
        exported, _ = build_script_step(step, variables={}, app_key_map={}, platform="android")
        self.assertEqual(exported["args"]["image_path"], "static/images/ios.png")
        self.assertEqual(exported["args"]["image_options"], {"template_sha256": CONFIG["template_sha256"], "tap_anchor": CONFIG["tap_anchor"]})

    def test_export_precheck_detects_changed_template_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target.png"
            target.write_bytes(b"changed-template")
            result = precheck_case_script({"steps": [{"order": 1, "action": "click_image", "args": {"image": "target.png", "image_options": CONFIG}}],
                                          "template_refs": [("target.png", "target.png")]}, project_root=Path(directory))
        self.assertFalse(result["ok"])
        self.assertEqual(result["issues"][0]["code"], "P1006_INVALID_ARGS")

    def test_assertion_metadata_survives_contract_export_and_runner(self):
        step = self.step()
        step["action"] = "assert_image"
        step["args"]["match_mode"] = "not_exists"
        legacy = standard_step_to_legacy(step)
        restored = legacy_step_to_standard(legacy, 1, 1)
        self.assertEqual(restored["args"]["image_options"]["android"], CONFIG)
        self.assertEqual(restored["args"]["match_mode"], "not_exists")
        exported, _ = build_script_step(step, variables={}, app_key_map={}, platform="android")
        self.assertEqual(exported["args"]["image_options"], CONFIG)
        self.assertEqual(exported["args"]["match_mode"], "not_exists")
        abort = threading.Event()
        runner = self.runner(abort_event=abort)
        result = runner.run_step(step)
        self.assertEqual(result["status"], "PASS", result)
        path, mode, config, timeout, event = runner.driver.assert_image_calls[0]
        self.assertEqual((path, mode, config, event), ("static/images/android.png", "not_exists", CONFIG, abort))
        self.assertAlmostEqual(timeout, 7, places=2)
        self.assertEqual(result["output"]["image_match"]["assertion_mode"], "not_exists")


if __name__ == "__main__":
    unittest.main()
