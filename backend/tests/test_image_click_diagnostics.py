"""Timeout reports preserve completed observations instead of inventing motion."""
from unittest.mock import patch

import pytest

from backend.execution_errors import ExecutionStepRuntimeError
from backend.utils import image_click


@pytest.mark.parametrize("interrupted_by", ["deadline_exceeded", "screenshot_deadline"])
def test_final_incomplete_frame_preserves_failed_verification(tmp_path, interrupted_by):
    template = tmp_path / "template.png"
    template.write_bytes(b"template")
    candidate = {"similarity": .9996, "accepted": False, "metrics": {"edge_coverage": .706}}
    absent = {"status": "not_found", "reason": "no_verified_candidate", "point": None,
              "candidates": [candidate], "timings": {"total_ms": 20}}
    expired = {"status": "unstable", "reason": "deadline_exceeded", "point": None,
               "candidates": [], "timings": {}}
    captures = [object(), TimeoutError("screenshot_deadline")] if interrupted_by == "screenshot_deadline" else [object(), object()]
    ticks = [0, 0, .1, .2, 1.2] if interrupted_by == "screenshot_deadline" else [0, 0, .1, .2, 1.1, 1.2]
    with patch.object(image_click, "ensure_descriptor", return_value={"profile": "strict-v1"}), patch.object(image_click, "image_to_bgr"), patch.object(image_click, "_bounded_capture", side_effect=captures), \
            patch.object(image_click, "match_image", side_effect=[absent, expired]), \
            patch.object(image_click.time, "monotonic", side_effect=ticks), patch.object(image_click.time, "sleep"), \
            pytest.raises(ExecutionStepRuntimeError) as failure:
        image_click.locate_stable_image(lambda: None, template, timeout=1, image_match={"profile": "strict-v1"})
    assert failure.value.code == "E2005_IMAGE_NOT_MATCHED"
    diagnostic = failure.value.context["image_match"]
    assert diagnostic["reason"] == "no_verified_candidate"
    assert diagnostic["candidates"] == [candidate]
    assert diagnostic["timed_out"] is True
    assert diagnostic["timeout_reason"] == interrupted_by
    assert diagnostic["frames_checked"] == 1
    assert diagnostic["point"] is None
    assert "稳定" not in str(failure.value)


@pytest.mark.parametrize("reason,label", [
    ("deadline_exceeded", "图像定位计算超时"),
    ("screenshot_deadline", "获取设备截图超时"),
    ("target_not_stable", "目标未在超时内稳定"),
])
def test_timeout_messages_explain_which_operation_failed(reason, label):
    error = image_click.image_failure({"status": "unstable", "reason": reason})
    assert error.code == "E2012_IMAGE_UNSTABLE"
    assert label in str(error)
    if reason != "target_not_stable":
        assert "稳定" not in error.suggestion


def test_budget_exhausted_before_first_frame_never_claims_verified_absence(tmp_path):
    template = tmp_path / "template.png"
    template.write_bytes(b"template")
    with patch.object(image_click, "ensure_descriptor", return_value={"profile": "strict-v1"}), patch.object(image_click, "image_to_bgr"), patch.object(image_click, "_bounded_capture") as capture, \
            patch.object(image_click.time, "monotonic", side_effect=[0, 1.1, 1.2]), \
            pytest.raises(ExecutionStepRuntimeError) as failure:
        image_click.locate_stable_image(lambda: None, template, timeout=1, image_match={"profile": "strict-v1"})
    capture.assert_not_called()
    diagnostic = failure.value.context["image_match"]
    assert diagnostic["status"] == "unstable"
    assert diagnostic["reason"] == "deadline_exceeded"
    assert diagnostic["frames_checked"] == 0
    assert "计算超时" in str(failure.value)


def test_only_verified_but_moving_target_reports_instability(tmp_path):
    template = tmp_path / "template.png"
    template.write_bytes(b"template")
    matches = [{"status": "matched", "reason": "unique_verified_target", "point": [x, 20],
                "rect": [x, 10, 40, 40], "candidates": [], "timings": {}} for x in (10, 30)]
    with patch.object(image_click, "ensure_descriptor", return_value={"profile": "strict-v1"}), patch.object(image_click, "image_to_bgr"), patch.object(image_click, "_bounded_capture"), \
            patch.object(image_click, "match_image", side_effect=matches), \
            patch.object(image_click.time, "monotonic", side_effect=[0, 0, .1, .2, 1.1, 1.2]), \
            patch.object(image_click.time, "sleep"), pytest.raises(ExecutionStepRuntimeError) as failure:
        image_click.locate_stable_image(lambda: None, template, timeout=1, image_match={"profile": "strict-v1"})
    assert failure.value.code == "E2012_IMAGE_UNSTABLE"
    diagnostic = failure.value.context["image_match"]
    assert diagnostic["reason"] == "target_not_stable"
    assert diagnostic["frames_checked"] == 2
    assert diagnostic["point"] is None
