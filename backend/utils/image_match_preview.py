"""Pure screenshot/template preview helpers; deliberately no driver imports."""
from __future__ import annotations

import base64
import hashlib
import time

from .image_match import MATCHER_VERSION, match_image, template_quality
from .image_match_options import normalize_image_options
from .template_match import image_to_bgr


def crop_metadata(image_bytes: bytes, screenshot_base64: str) -> dict:
    from .adaptive_template import ensure_descriptor
    screenshot = image_to_bgr(base64.b64decode(screenshot_base64), source="screenshot")
    return {"template_sha256": hashlib.sha256(image_bytes).hexdigest(),
            "source_size": [screenshot.shape[1], screenshot.shape[0]],
            "quality": template_quality(image_to_bgr(image_bytes, source="template")),
            "image_match": ensure_descriptor(image_bytes, deadline=time.monotonic() + 10)}


def preview_image_match(screenshot_base64: str, image_bytes: bytes, image_options=None, image_match=None) -> dict:
    from .adaptive_match import match_adaptive
    from .adaptive_template import ensure_descriptor
    config = normalize_image_options(image_options)
    if config and hashlib.sha256(image_bytes).hexdigest() != config["template_sha256"]:
        return {"status": "invalid_template", "reason": "template_hash_mismatch", "matcher_version": MATCHER_VERSION,
                "point": None, "candidates": [], "quality": {}, "timings": {}}
    try:
        screenshot = image_to_bgr(base64.b64decode(screenshot_base64, validate=True), source="screenshot")
        template = image_to_bgr(image_bytes, source="template")
    except Exception as exc:
        raise ValueError(f"无法解析匹配图片: {exc}") from exc
    deadline = time.monotonic() + 10
    shared = ensure_descriptor(image_bytes, image_match, anchor=config.get("tap_anchor"), deadline=deadline)
    if shared["profile"] == "strict-v1":
        return match_image(screenshot, template, config, deadline=deadline)
    return match_adaptive(screenshot, template, config, deadline=deadline, image_match=shared)
