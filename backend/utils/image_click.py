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
    label = STATUS_LABELS.get(status, status)
    suggestion = None
    if status == "unstable":
        label = {
            "deadline_exceeded": "图像定位计算超时，未完成匹配验证",
            "screenshot_deadline": "获取设备截图超时，未完成图像定位",
        }.get(diagnostic.get("reason"), label)
        suggestion = {
            "deadline_exceeded": "请检查定位耗时，缩小模板或搜索区域，必要时调整步骤超时。",
            "screenshot_deadline": "请检查设备连接与截图服务是否响应，恢复后重试。",
        }.get(diagnostic.get("reason"))
    return ExecutionStepRuntimeError(STATUS_CODES.get(status, E2005_IMAGE_NOT_MATCHED),
                                     f"{label}: {diagnostic.get('reason', '')}",
                                     context={"image_match": diagnostic}, suggestion=suggestion)


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
    last_complete = None
    frames_checked = 0
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
        if last["status"] in {"matched", "not_found"}:
            last_complete = last
            frames_checked += 1
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
    timeout_reason = last.get("reason", "")
    if timeout_reason not in {"deadline_exceeded", "screenshot_deadline"}:
        timeout_reason = "deadline_exceeded"
    if saw_match or last["status"] == "matched":
        last = {**(last_complete or last), "status": "unstable", "reason": "target_not_stable"}
    elif last_complete is not None:
        # A final, incomplete attempt must not overwrite the verified absence
        # and candidate metrics from earlier frames with a stability error.
        last = {**last_complete}
    elif last["status"] == "not_found":
        last = {**last, "status": "unstable", "reason": "deadline_exceeded"}
    last["point"] = None
    last["timed_out"] = True
    last["timeout_reason"] = timeout_reason
    last["frames_checked"] = frames_checked
    last["timings"]["localization_ms"] = round((time.monotonic() - started) * 1000, 2)
    raise image_failure(last)
