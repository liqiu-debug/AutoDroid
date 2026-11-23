"""Strict, device-independent image localization. Scores are not probabilities.

All geometry is in original screenshot pixels. This module never dispatches taps.
The frozen profile is also implemented by the mobile native matchers.
"""
from __future__ import annotations

import math
import time
from typing import Any, Dict, Optional

from .image_match_options import normalize_image_options
from .template_match import DEFAULT_TEMPLATE_SCALES, _load_cv_stack, image_to_bgr

MATCHER_VERSION = "strict-v1"
ACCEPTANCE_SHA256 = "534f9d1ea64c25b93692e29cdf85edf298ec69aa92f89bc7444c2a6e9882ddbd"
PROFILE = {
    "candidate_ncc": 0.82, "accept_ncc": 0.92,
    "min_std": 0.5, "min_side": 8,
    "edge_coverage": 0.88, "foreground_error": 0.12, "tile_error": 0.22,
    "color_error": 0.08, "ambiguity_margin": 0.03,
    "nms_iou": 0.35, "max_candidates": 64,
    "local_window": 16, "local_stride": 8, "local_min_foreground": 8,
}


def matcher_capabilities() -> Dict[str, Any]:
    try:
        _load_cv_stack()
    except RuntimeError:
        return {"version": MATCHER_VERSION, "validated": False, "preview": False, "metadata": True,
                "profiles": ["strict-v1"], "descriptor_versions": []}
    return {"version": MATCHER_VERSION, "validated": True, "preview": True, "metadata": True,
            "profiles": ["strict-v1", "adaptive-v2"], "descriptor_versions": [1]}


def _result(status: str, reason: str, quality=None, candidates=None, started=None) -> Dict[str, Any]:
    return {"status": status, "reason": reason, "matcher_version": MATCHER_VERSION,
            "quality": quality or {}, "candidates": candidates or [], "point": None,
            "timings": {"total_ms": round((time.monotonic() - started) * 1000, 2)} if started else {}}


def _edges(gray):
    cv2, np = _load_cv_stack()
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    magnitude = cv2.magnitude(gx, gy)
    # Measure contrast among actual gradients. On a sparse icon the full-image
    # percentile is background (often zero), so JPEG ringing would otherwise
    # become contour evidence and reject the same icon in a lossless frame.
    signal = magnitude[magnitude >= 8.0]
    threshold = max(8.0, float(np.percentile(signal, 90)) * 0.25) if signal.size else 8.0
    edge = (magnitude >= threshold).astype(np.uint8)
    # Ignore padding derivatives; there is no evidence outside the template.
    edge[[0, -1], :] = 0
    edge[:, [0, -1]] = 0
    return edge


def template_quality(template_bgr) -> Dict[str, Any]:
    cv2, np = _load_cv_stack()
    template = image_to_bgr(template_bgr)
    if template is None or not getattr(template, "size", 0):
        return {"valid": False, "reason": "invalid_image", "warnings": []}
    h, w = template.shape[:2]
    gray = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)
    std = float(gray.std())
    if min(w, h) < PROFILE["min_side"]:
        return {"valid": False, "reason": "template_too_small", "warnings": [], "size": [w, h]}
    edge = _edges(gray)
    density = float(edge.mean())
    reason = "constant_template" if std <= PROFILE["min_std"] else "no_foreground" if not edge.any() else "ok"
    warnings = []
    if std < 10:
        warnings.append("low_contrast")
    if density < 0.02:
        warnings.append("sparse_features")
    if density > 0.6:
        warnings.append("dense_texture")
    colorful = np.max(template, axis=2).astype(float) - np.min(template, axis=2)
    return {"valid": reason == "ok", "reason": reason, "warnings": warnings, "size": [w, h],
            "gray_std": round(std, 4), "edge_density": round(density, 4),
            "colorful": bool((colorful >= 32).mean() >= 0.1)}


def rect_iou(left, right) -> float:
    ax, ay, aw, ah = left
    bx, by, bw, bh = right
    intersection = max(0, min(ax + aw, bx + bw) - max(ax, bx)) * max(0, min(ay + ah, by + bh) - max(ay, by))
    return intersection / max(1, aw * ah + bw * bh - intersection)


def _same_object(left, right) -> bool:
    if rect_iou(left, right) > PROFILE["nms_iou"]:
        return True
    ax, ay, aw, ah = left
    bx, by, bw, bh = right
    return math.hypot(ax + aw / 2 - bx - bw / 2, ay + ah / 2 - by - bh / 2) <= 0.25 * min(aw, ah, bw, bh)


def _verify(crop, template, score: float) -> Dict[str, Any]:
    cv2, np = _load_cv_stack()
    gray = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)
    other = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    edges, other_edges = _edges(gray), _edges(other)
    if not edges.any() or not other_edges.any() or float(other.std()) <= PROFILE["min_std"]:
        return {"accepted": False, "edge_coverage": 0.0, "foreground_error": 1.0, "tile_error": 1.0,
                "color_error": 1.0, "reason": "no_foreground"}
    distance = cv2.distanceTransform(1 - edges, cv2.DIST_L2, cv2.DIST_MASK_PRECISE)
    other_distance = cv2.distanceTransform(1 - other_edges, cv2.DIST_L2, cv2.DIST_MASK_PRECISE)
    coverage = min(float((other_distance[edges > 0] <= 1.5).mean()), float((distance[other_edges > 0] <= 1.5).mean()))
    foreground = cv2.dilate(edges | other_edges, np.ones((3, 3), np.uint8)).astype(bool)
    difference = np.abs(gray.astype(float) - other.astype(float)) / 255.0
    error = float(difference[foreground].mean())
    worst = error
    h, w = gray.shape
    for row in range(4):
        for col in range(4):
            ys, xs = slice(row * h // 4, (row + 1) * h // 4), slice(col * w // 4, (col + 1) * w // 4)
            mask = foreground[ys, xs]
            if mask.sum() >= 8:
                worst = max(worst, float(difference[ys, xs][mask].mean()))
    # A shared border or long label must not dilute one changed character.
    # Overlapping pixel windows also catch differences crossing grid boundaries.
    side, stride = PROFILE["local_window"], PROFILE["local_stride"]
    sums = cv2.integral(difference * foreground)
    counts = cv2.integral(foreground.astype(np.uint8))
    ys, xs = np.arange(0, h, stride)[:, None], np.arange(0, w, stride)[None, :]
    ye, xe = np.minimum(ys + side, h), np.minimum(xs + side, w)
    totals = sums[ye, xe] - sums[ys, xe] - sums[ye, xs] + sums[ys, xs]
    samples = counts[ye, xe] - counts[ys, xe] - counts[ye, xs] + counts[ys, xs]
    active = samples >= PROFILE["local_min_foreground"]
    local_error = float((totals[active] / samples[active]).max()) if active.any() else error
    color_active = bool(((template.max(axis=2).astype(float) - template.min(axis=2)) >= 32).mean() >= 0.1
                        or ((crop.max(axis=2).astype(float) - crop.min(axis=2)) >= 32).mean() >= 0.1)
    color_error = float(np.abs(template.astype(float) - crop.astype(float))[foreground].mean() / 255.0) if color_active else None
    accepted = (score >= PROFILE["accept_ncc"] and coverage >= PROFILE["edge_coverage"]
                and error <= PROFILE["foreground_error"] and max(worst, local_error) <= PROFILE["tile_error"]
                and (color_error is None or color_error <= PROFILE["color_error"]))
    return {"accepted": bool(accepted), "edge_coverage": round(coverage, 6), "foreground_error": round(error, 6),
            "tile_error": round(worst, 6), "local_error": round(local_error, 6),
            "color_error": round(color_error, 6) if color_error is not None else None,
            "reason": "accepted" if accepted else "verification_failed"}


def match_image(screen_bgr, template_bgr, image_options: Optional[dict] = None, deadline: Optional[float] = None) -> Dict[str, Any]:
    started = time.monotonic()
    try:
        cv2, np = _load_cv_stack()
    except RuntimeError as exc:
        return _result("unsupported", str(exc), started=started)
    config = normalize_image_options(image_options)
    try:
        screen, template = image_to_bgr(screen_bgr), image_to_bgr(template_bgr)
        quality = template_quality(template)
    except (RuntimeError, ValueError, cv2.error) as exc:
        return _result("invalid_template", str(exc), started=started)
    if not quality["valid"]:
        return _result("invalid_template", quality["reason"], quality, started=started)
    sh, sw = screen.shape[:2]
    th, tw = template.shape[:2]
    base = 1.0
    if "source_size" in config:
        rw, rh = config["source_size"]
        sx, sy = sw / rw, sh / rh
        if abs(sx / sy - 1) > 0.03:
            return _result("unsupported", "aspect_ratio_changed", quality, started=started)
        base = (sx + sy) / 2
    region = config.get("search_region", [0, 0, 1, 1])
    left, top = int(region[0] * sw), int(region[1] * sh)
    right, bottom = int(math.ceil(region[2] * sw)), int(math.ceil(region[3] * sh))
    gray = cv2.cvtColor(screen[top:bottom, left:right], cv2.COLOR_BGR2GRAY)
    raw = []
    sizes = set()
    overflow = False
    search_started = time.monotonic()
    for scale in DEFAULT_TEMPLATE_SCALES:
        if deadline is not None and time.monotonic() >= deadline:
            return _result("unstable", "deadline_exceeded", quality, started=started)
        w, h = max(1, round(tw * base * scale)), max(1, round(th * base * scale))
        if min(w, h) < PROFILE["min_side"] or w > gray.shape[1] or h > gray.shape[0] or (w, h) in sizes:
            continue
        sizes.add((w, h))
        scaled = template if (w, h) == (tw, th) else cv2.resize(template, (w, h), interpolation=cv2.INTER_AREA if base * scale < 1 else cv2.INTER_LINEAR)
        scaled_gray = cv2.cvtColor(scaled, cv2.COLOR_BGR2GRAY)
        if float(scaled_gray.std()) <= PROFILE["min_std"]:
            continue
        response = cv2.matchTemplate(gray, scaled_gray, cv2.TM_CCOEFF_NORMED)
        response[~np.isfinite(response)] = -1
        # Suppress an object's neighborhood before searching the next independent peak.
        for _ in range(21):
            _, score, _, location = cv2.minMaxLoc(response)
            if score < PROFILE["candidate_ncc"]:
                break
            if _ == 20:
                overflow = True
                break
            x, y = location
            raw.append((float(score), [x + left, y + top, w, h], scaled))
            radius_x, radius_y = max(1, w // 3), max(1, h // 3)
            response[max(0, y - radius_y): y + radius_y + 1, max(0, x - radius_x): x + radius_x + 1] = -1
    search_ms = (time.monotonic() - search_started) * 1000
    independent = []
    # Verify before deduplication: a higher raw NCC must not hide a valid scale.
    verification_started = time.monotonic()
    verified = []
    for score, rect, scaled in raw:
        if deadline is not None and time.monotonic() >= deadline:
            return _result("unstable", "deadline_exceeded", quality, started=started)
        x, y, w, h = rect
        metrics = _verify(screen[y:y + h, x:x + w], scaled, score)
        verified.append({"rect": rect, "similarity": score, "metrics": metrics, "accepted": metrics["accepted"]})
    verified.sort(key=lambda c: (c["accepted"], c["similarity"]), reverse=True)
    for candidate in verified:
        if not any(_same_object(candidate["rect"], item["rect"]) for item in independent):
            independent.append(candidate)
        if len(independent) > PROFILE["max_candidates"]:
            overflow = True
            break
    independent = independent[:PROFILE["max_candidates"]]
    anchor = config.get("tap_anchor", [0.5, 0.5])
    for candidate in independent:
        x, y, w, h = candidate["rect"]
        candidate["point"] = [x + min(w - 1, anchor[0] * w), y + min(h - 1, anchor[1] * h)]
    accepted = [c for c in independent if c["accepted"]]
    status, reason = "not_found", "no_verified_candidate"
    if overflow:
        status, reason = "ambiguous", "candidate_overflow"
    elif len(accepted) > 1:
        status, reason = "ambiguous", "multiple_verified_targets"
    elif accepted:
        best = accepted[0]
        plausible = [c for c in independent if c is not best and c["similarity"] >= 0.90
                     and c["metrics"]["edge_coverage"] >= 0.80 and c["metrics"]["foreground_error"] <= 0.16
                     and best["similarity"] - c["similarity"] < PROFILE["ambiguity_margin"]]
        status, reason = ("ambiguous", "insufficient_margin") if plausible else ("matched", "unique_verified_target")
    result = _result(status, reason, quality, independent, started)
    if status == "matched":
        result["point"] = accepted[0]["point"]
        result["rect"] = accepted[0]["rect"]
    result["timings"].update(search_ms=round(search_ms, 2), verification_ms=round((time.monotonic() - verification_started) * 1000, 2))
    return result


def stable_match(first: Dict[str, Any], second: Dict[str, Any]) -> bool:
    if first.get("status") != "matched" or second.get("status") != "matched":
        return False
    rect = first["rect"]
    displacement = math.dist(first["point"], second["point"])
    return rect_iou(rect, second["rect"]) >= 0.9 and displacement <= max(2, 0.02 * min(rect[2:]))
