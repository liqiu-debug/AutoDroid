"""Existence semantics over the same strict matcher used by image clicks."""
from __future__ import annotations

import hashlib
from pathlib import Path
import time

from backend.execution_errors import E2004_ASSERT_IMAGE_FAILED, E2201_INVALID_ARGS, USER_ABORT_MESSAGE, ExecutionStepAssertionError, ExecutionStepRuntimeError
from .image_click import _bounded_capture, image_failure
from .image_match import MATCHER_VERSION, match_image
from .image_match_options import normalize_image_options
from .template_match import image_to_bgr
from .adaptive_template import ensure_descriptor
from .adaptive_match import match_adaptive


def assert_image_presence(capture, image_path, match_mode="exists", image_options=None, timeout=10, abort_event=None, image_match=None):
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 3600:
        raise ValueError("图像断言 timeout 必须在 0..3600 秒内")
    deadline = time.monotonic() + timeout
    config = normalize_image_options(image_options)
    raw = Path(image_path).read_bytes()
    if config and hashlib.sha256(raw).hexdigest() != config["template_sha256"]:
        raise ExecutionStepRuntimeError(E2201_INVALID_ARGS, "模板已变化，请重新截取",
                                        context={"image_match": {"status": "invalid_template", "reason": "template_hash_mismatch", "matcher_version": MATCHER_VERSION}})
    try:
        template = image_to_bgr(raw, source="template")
    except RuntimeError as exc:
        raise image_failure({"status": "invalid_template", "reason": str(exc), "matcher_version": MATCHER_VERSION}) from exc
    try:
        match_config = ensure_descriptor(raw, image_match, config.get("tap_anchor"), deadline=deadline)
    except TimeoutError as exc:
        raise image_failure({"status": "unstable", "reason": "deadline_exceeded", "matcher_version": "adaptive-v2"}) from exc
    except (ValueError, RuntimeError) as exc:
        raise image_failure({"status": "invalid_template" if isinstance(exc, ValueError) else "unsupported",
                             "reason": str(exc), "matcher_version": "adaptive-v2"}) from exc
    adaptive = match_config["profile"] != "strict-v1"
    absent_frames = 0
    while time.monotonic() < deadline:
        if abort_event is not None and abort_event.is_set():
            raise RuntimeError(USER_ABORT_MESSAGE)
        try:
            screenshot = _bounded_capture(capture, deadline)
        except TimeoutError as exc:
            raise image_failure({"status": "unstable", "reason": "screenshot_deadline", "matcher_version": MATCHER_VERSION}) from exc
        diagnostic = (match_adaptive(screenshot, template, config, deadline, match_config)
                      if adaptive else match_image(screenshot, template, config, deadline))
        present = any(candidate["accepted"] for candidate in diagnostic["candidates"])
        # A returned verified candidate proves presence even when a second target
        # makes a click ambiguous. Unknown state never proves absence.
        if present:
            if match_mode != "not_exists":
                return {"image_match": diagnostic}
            raise ExecutionStepAssertionError(E2004_ASSERT_IMAGE_FAILED, "断言失败：页面存在通过严格验证的图像目标",
                                              context={"image_match": diagnostic})
        if diagnostic["status"] != "not_found":
            raise image_failure(diagnostic)
        if match_mode != "not_exists":
            raise ExecutionStepAssertionError(E2004_ASSERT_IMAGE_FAILED, "断言失败：页面不存在通过严格验证的图像目标",
                                              context={"image_match": diagnostic})
        absent_frames += 1
        if absent_frames == 2:
            return {"image_match": diagnostic}
        remaining = deadline - time.monotonic()
        if remaining > 0:
            if abort_event is not None:
                if abort_event.wait(min(.15, remaining)):
                    raise RuntimeError(USER_ABORT_MESSAGE)
            else:
                time.sleep(min(.15, remaining))
    raise image_failure({"status": "unstable", "reason": "deadline_exceeded", "matcher_version": MATCHER_VERSION})
