"""Shared descriptor contracts across persistence, export and PNG capture."""
import base64
import hashlib
import io
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image, ImageDraw

from backend.autojs_export.packager import _freeze_image_descriptors
from backend.step_contract import legacy_step_to_standard, standard_step_to_legacy, step_image_match
from backend.utils.adaptive_template import ensure_descriptor
from backend.utils.android_png import capture_android_png


def template_bytes():
    image = Image.new("RGB", (48, 48), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((4, 4, 43, 43), outline=(220, 30, 10), width=3)
    draw.line((12, 35, 34, 12), fill=(10, 10, 10), width=4)
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


def test_old_steps_default_adaptive_and_shared_descriptor_survives_roundtrip():
    config = ensure_descriptor(template_bytes())
    step = {"action": "click_image", "args": {"image_path": "one.png", "image_match": config}}
    converted = legacy_step_to_standard(standard_step_to_legacy(step), 1, 1)
    assert converted["args"]["image_match"] == config
    assert step_image_match({"args": {}}) == {"profile": "adaptive-v2"}
    assert step_image_match({"args": {"image_match": {"profile": "strict-v1"}}})["profile"] == "strict-v1"


@pytest.mark.parametrize("config", [None, [], {"profile": "unknown"}, {"profile": "adaptive-v2", "unknown": True}])
def test_invalid_explicit_shared_config_is_not_silently_defaulted(config):
    with pytest.raises(ValueError):
        step_image_match({"args": {"image_match": config}})


def test_descriptor_canonical_hash_and_standard_legacy_conflicts_are_rejected():
    config = ensure_descriptor(template_bytes())
    with pytest.raises(ValueError, match="descriptor_hash_mismatch"):
        step_image_match({"args": {"image_match": {**config, "descriptor_sha256": "0" * 64}}})
    with pytest.raises(ValueError, match="conflicts"):
        step_image_match({"args": {"image_match": config}, "options": {"image_match": {"profile": "strict-v1"}}})


def test_export_freezes_from_the_actual_bundle_asset_root(tmp_path, monkeypatch):
    from backend.autojs_export import packager
    monkeypatch.setattr(packager, "PROJECT_ROOT", tmp_path)
    raw = template_bytes()
    (tmp_path / "one.png").write_bytes(raw)
    step = {"action": "assert_image", "args": {"image": "one.png", "image_match": {"profile": "adaptive-v2"}}}
    script = {"steps": [step], "template_refs": [("one.png", "one.png")]}
    _freeze_image_descriptors(script)
    config = step["args"]["image_match"]
    assert config["template_sha256"] == hashlib.sha256(raw).hexdigest()
    assert config["descriptor_sha256"] == hashlib.sha256(config["descriptor_canonical"].encode()).hexdigest()
    assert json.loads(config["descriptor_canonical"]) == config["descriptor"]


def test_android_recording_and_execution_read_native_png_without_jpeg_rpc():
    raw = template_bytes()
    shell = Mock(return_value=raw)
    device = SimpleNamespace(adb_device=SimpleNamespace(shell=shell), screenshot=Mock(side_effect=AssertionError("JPEG RPC forbidden")))
    assert capture_android_png(device) == raw
    shell.assert_called_once_with(["screencap", "-p"], encoding=None, timeout=15.0)
    device.screenshot.assert_not_called()


@pytest.mark.parametrize("raw", [b"", b"permission denied", b"\xff\xd8JPEG", b"\x89PNG\r\n\x1a\ninvalid"])
def test_failed_native_capture_never_returns_a_black_placeholder(raw):
    device = SimpleNamespace(adb_device=SimpleNamespace(shell=Mock(return_value=raw)))
    with pytest.raises(Exception):
        capture_android_png(device)
    with pytest.raises(TimeoutError):
        capture_android_png(device, deadline=time.monotonic() - 1)
