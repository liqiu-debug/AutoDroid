"""Bounded two-frame localization shared by Android and iOS click drivers."""
from __future__ import annotations

import hashlib
from pathlib import Path
import queue
import threading
import time

from backend.execution_errors import E2005_IMAGE_NOT_MATCHED, E2201_INVALID_ARGS, USER_ABORT_MESSAGE, ExecutionStepRuntimeError
from .image_match import MATCHER_VERSION, match_image, stable_match
from .image_match_options import normalize_image_options
from .template_match import image_to_bgr

STATUS_CODES = {
    "not_found": E2005_IMAGE_NOT_MATCHED,
    "ambiguous": "E2010_IMAGE_AMBIGUOUS",
    "invalid_template": "E2011_IMAGE_TEMPLATE_INVALID",
    "unstable": "E2012_IMAGE_UNSTABLE",
    "unsupported": "E2013_IMAGE_MATCH_UNSUPPORTED",
}
STATUS_LABELS = {
    "not_found": "未找到通过严格验证的图像目标",
    "ambiguous": "存在多个相似目标，请指定搜索区域或重新圈选",
    "invalid_template": "图像模板无效或缺少可识别特征，请重新圈选",
    "unstable": "目标未在超时内稳定，未执行点击",
    "unsupported": "当前图像配置或执行环境不支持严格匹配",
}


def image_failure(diagnostic):
    status = diagnostic["status"]
    return ExecutionStepRuntimeError(STATUS_CODES.get(status, E2005_IMAGE_NOT_MATCHED),
                                     f"{STATUS_LABELS.get(status, status)}: {diagnostic.get('reason', '')}",
                                     context={"image_match": diagnostic})


def bounded_image_read(capture, deadline):
    """A timed-out screenshot thread can only read; it never owns a click callback."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("screenshot_deadline")
    result = queue.Queue(maxsize=1)

    def read():
        try:
            result.put((True, capture()))
        except Exception as exc:
            result.put((False, exc))

    threading.Thread(target=read, daemon=True, name="image-match-screenshot").start()
    try:
        ok, payload = result.get(timeout=remaining)
    except queue.Empty as exc:
        raise TimeoutError("screenshot_deadline") from exc
    if not ok:
        raise payload
    return payload


def _bounded_capture(capture, deadline):
    return image_to_bgr(bounded_image_read(capture, deadline), source="screenshot")


def locate_stable_image(capture, image_path, image_options=None, timeout=10, abort_event=None):
    """Returns evidence immediately before one caller-owned tap, or raises.

    Only not-found and unstable observations are retried; ambiguity/invalid input
    fail immediately. No page-signature based second tap is ever scheduled.
    """
    started = time.monotonic()
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 3600:
        raise ValueError("图像点击 timeout 必须在 0..3600 秒内")
    deadline = started + timeout
    if abort_event is not None and abort_event.is_set():
        raise RuntimeError(USER_ABORT_MESSAGE)
    config = normalize_image_options(image_options)
    raw = Path(image_path).read_bytes()
    if config and hashlib.sha256(raw).hexdigest() != config["template_sha256"]:
        diagnostic = {"status": "invalid_template", "reason": "template_hash_mismatch",
                      "matcher_version": MATCHER_VERSION, "candidates": [], "point": None, "timings": {}}
        raise ExecutionStepRuntimeError(E2201_INVALID_ARGS, "模板已变化，请重新确认点击位置及搜索区域",
                                        context={"image_match": diagnostic})
    try:
        template = image_to_bgr(raw, source="template")
    except RuntimeError as exc:
        raise image_failure({"status": "invalid_template", "reason": str(exc), "matcher_version": MATCHER_VERSION,
                             "point": None, "candidates": [], "timings": {}}) from exc
    previous = None
    saw_match = False
    last = {"status": "not_found", "reason": "no_verified_candidate", "matcher_version": MATCHER_VERSION,
            "point": None, "candidates": [], "timings": {}}
    while time.monotonic() < deadline:
        if abort_event is not None and abort_event.is_set():
            raise RuntimeError(USER_ABORT_MESSAGE)
        try:
            screen = _bounded_capture(capture, deadline)
        except TimeoutError:
            last = {**last, "status": "unstable", "reason": "screenshot_deadline"}
            break
        last = match_image(screen, template, config, deadline=deadline)
        if last["status"] in {"ambiguous", "invalid_template", "unsupported"}:
            raise image_failure(last)
        if last["status"] == "matched":
            saw_match = True
            if previous is not None and stable_match(previous, last) and time.monotonic() < deadline:
                if abort_event is not None and abort_event.is_set():
                    raise RuntimeError(USER_ABORT_MESSAGE)
                last["timings"]["localization_ms"] = round((time.monotonic() - started) * 1000, 2)
                last["stable_frames"] = 2
                last["_deadline"] = deadline
                return last
            previous = last
        else:
            previous = None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if abort_event is not None:
            if abort_event.wait(min(0.15, remaining)):
                raise RuntimeError(USER_ABORT_MESSAGE)
        else:
            time.sleep(min(0.15, remaining))
    if saw_match or last["status"] == "matched":
        last = {**last, "status": "unstable", "reason": "target_not_stable"}
    last["point"] = None
    last["timings"]["localization_ms"] = round((time.monotonic() - started) * 1000, 2)
    raise image_failure(last)
