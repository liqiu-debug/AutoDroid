"""Exercise one frozen original image through export, adapter and runtime."""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from unittest.mock import patch

import pytest

from backend.autojs_export import packager
from backend.autojs_export.generator import build_script_step
from backend.tests.test_adaptive_image_contract import template_bytes


HARNESS = Path(__file__).parent / "support" / "ios_runtime_harness.mjs"
NODE = shutil.which("node")


def freeze_bundle(tmp_path, config=None):
    raw = template_bytes()
    directory = tmp_path / "static" / "images"
    directory.mkdir(parents=True)
    (directory / "element_abcd.png").write_bytes(raw)
    args = {"image_path": "static/images/element_abcd.png"}
    if config is not None:
        args["image_match"] = config
    with patch.object(packager, "PROJECT_ROOT", tmp_path):
        compiled, refs = build_script_step({"order": 1, "action": "assert_image", "description": "大云闪付 logo",
                                           "args": args}, variables={}, app_key_map={}, platform="ios")
        script = {"case_id": 1, "name": "共享模板", "steps": [compiled], "template_refs": refs}
        packager._freeze_image_descriptors(script)
        manifest = packager._build_manifest(kind="case", name=script["name"], case_entries=[{
            "id": 1, "name": script["name"], "alias": None, "file": "cases/one.js"}],
            variables={}, scenario_id=None, precheck={"ok": True, "issues": []}, platform="ios")
        bundle = packager._bundle_payload(kind="case", manifest=manifest, case_scripts=[(script, "cases/one.js")],
                                         template_refs=refs, precheck={"ok": True, "issues": []})
    return raw, compiled, bundle


def run_bundle(bundle):
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / "bundle.json"
        target.write_text(json.dumps(bundle, ensure_ascii=False))
        result = subprocess.run([NODE, str(HARNESS), str(target)], text=True, capture_output=True, timeout=30)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)


@pytest.mark.skipif(not NODE, reason="Node is needed for the device JS harness")
def test_old_step_freezes_one_original_png_and_passes_exact_descriptor_to_native(tmp_path):
    raw, compiled, bundle = freeze_bundle(tmp_path)
    config = compiled["args"]["image_match"]
    assert config["profile"] == "adaptive-v2"
    assert config["template_sha256"] == hashlib.sha256(raw).hexdigest()
    assert json.loads(config["descriptor_canonical"]) == config["descriptor"]
    assert base64.b64decode(bundle["assets"]["element_abcd.png"]) == raw
    assert list(bundle["assets"]) == ["element_abcd.png"]
    bundle["main_js"] = """
var originalCapabilities=global.__adt.strictMatcherCapabilities;
global.__adt.strictMatcherCapabilities=function(){var result=originalCapabilities();result.profiles=['strict-v1','adaptive-v2'];result.descriptor_versions=[1];return result;};
var originalMatch=global.__adt.matchImageStrict;
global.__adt.matchImageStrict=function(a,b,options){var result=originalMatch(a,b,options);result.matcher_version=options.image_match.profile;return result;};
""" + bundle["main_js"]
    outcome = run_bundle(bundle)
    assert outcome["result"]["success"], outcome["logs"]
    matches = [call for call in outcome["apiCalls"] if call["name"] == "matchImageStrict"]
    assert matches[0]["args"]["options"]["image_match"] == config
    assert matches[0]["args"]["options"]["template_sha256"] == config["template_sha256"]
    reported = outcome["reportPayload"]["cases"][0]["steps"][0]
    assert reported["description"] == "大云闪付 logo"
    assert not any(call["name"] in {"tap", "ocr"} for call in outcome["apiCalls"])


@pytest.mark.skipif(not NODE, reason="Node is needed for the device JS harness")
def test_old_native_adaptive_upgrade_failure_precedes_image_load_and_match(tmp_path):
    _, _, bundle = freeze_bundle(tmp_path)
    outcome = run_bundle(bundle)
    step = outcome["reportPayload"]["cases"][0]["steps"][0]
    assert step["error_code"] == "E2013_IMAGE_MATCH_UNSUPPORTED"
    assert step["output"]["image_match"]["reason"] == "adaptive_matcher_upgrade_required"
    assert not any(call["name"] in {"imageFromFile", "matchImageStrict", "tap"} for call in outcome["apiCalls"])


@pytest.mark.skipif(not NODE, reason="Node is needed for the device JS harness")
def test_strict_shared_hash_is_checked_before_native_matching(tmp_path):
    _, _, bundle = freeze_bundle(tmp_path, {"profile": "strict-v1"})
    # Corrupt the frozen file after preparation. Shared hashes are mandatory
    # evidence for either profile, even when platform options are absent.
    bundle["assets"]["element_abcd.png"] = base64.b64encode(b"changed bytes").decode()
    outcome = run_bundle(bundle)
    step = outcome["reportPayload"]["cases"][0]["steps"][0]
    assert step["status"] == "FAIL"
    assert step["output"]["image_match"]["reason"] == "template_hash_mismatch"
    assert not any(call["name"] == "matchImageStrict" for call in outcome["apiCalls"])


@pytest.mark.skipif(not NODE, reason="Node is needed for the device JS harness")
@pytest.mark.parametrize("invalid", [None, False, [], ""])
def test_runtime_invalid_explicit_shared_metadata_is_not_defaulted(tmp_path, invalid):
    _, compiled, bundle = freeze_bundle(tmp_path)
    compiled["args"]["image_match"] = invalid
    bundle["case_files"][0]["content"] = packager._case_module_source({"case_id": 1, "name": "invalid", "steps": [compiled]})
    outcome = run_bundle(bundle)
    step = outcome["reportPayload"]["cases"][0]["steps"][0]
    assert step["status"] == "FAIL"
    assert step["output"]["image_match"]["reason"] == "image_match_must_be_object"
    assert not any(call["name"] in {"imageFromFile", "matchImageStrict", "tap"} for call in outcome["apiCalls"])


@pytest.mark.skipif(not NODE, reason="Node is needed for the device JS harness")
@pytest.mark.parametrize("profile", [None, False, ""])
def test_runtime_explicit_invalid_profile_does_not_default_to_adaptive(tmp_path, profile):
    _, compiled, bundle = freeze_bundle(tmp_path)
    compiled["args"]["image_match"]["profile"] = profile
    bundle["case_files"][0]["content"] = packager._case_module_source({"case_id": 1, "name": "invalid", "steps": [compiled]})
    outcome = run_bundle(bundle)
    step = outcome["reportPayload"]["cases"][0]["steps"][0]
    assert step["output"]["image_match"]["reason"] == "unknown_matcher_profile"
    assert not any(call["name"] in {"imageFromFile", "matchImageStrict", "tap"} for call in outcome["apiCalls"])


def test_preparation_freezes_legacy_shared_image_after_platform_selection(tmp_path):
    from backend.cross_platform_execution import prepare_steps_for_platform
    raw = template_bytes()
    original = tmp_path / "shared.png"
    original.write_bytes(raw)
    step = {"action": "assert_image", "execute_on": ["android", "ios"], "args": {"image_path": str(original)},
            "platform_overrides": {"ios": {"selector": str(original), "by": "image"}}}
    for platform in ("android", "ios"):
        with patch("backend.cross_platform_execution.load_app_key_mapping", return_value={}):
            prepared = prepare_steps_for_platform(None, [step], platform)[0]
        config = prepared["args"]["image_match"]
        assert config["profile"] == "adaptive-v2"
        assert config["template_sha256"] == hashlib.sha256(raw).hexdigest()
        assert config["descriptor_canonical"]
    assert "image_match" not in step["args"], "Preparing a run must not mutate the stored old case"
    assert original.read_bytes() == raw
