"""Content-addressed, bounded analysis of one shared image template.

Descriptors contain template-relative evidence, never device coordinates.
Prepared/exported descriptors are verified and reused without template OCR.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.metadata
import json
import math
import queue
import threading
import time
import unicodedata
from collections import OrderedDict

from .template_match import _load_cv_stack, image_to_bgr

PROFILE = "adaptive-v2"
ANALYZER_VERSION = "components-ocr-v1"
OCR_VERSION = "structured-ocr-v1"
_CACHE = OrderedDict()
_CACHE_LOCK = threading.RLock()
_CACHE_LIMIT = 256


def ocr_identity(ocr=None):
    explicit = getattr(ocr, "cache_version", None)
    if explicit:
        return str(explicit)
    if ocr is None:
        module = default_ocr.__module__
    else:
        owner = getattr(ocr, "__self__", None)
        module = owner.__class__.__module__ if owner is not None else getattr(ocr, "__module__", "custom")
    package = "rapidocr" if module.startswith("engine.") else "paddleocr" if module.startswith("backend.") else module.split(".")[0]
    try:
        version = importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        version = "custom"
    return f"{package}:{version}"


def canonical_descriptor(descriptor):
    return json.dumps(descriptor, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def normalize_anchor(anchor):
    anchor = list([.5, .5] if anchor is None else anchor)
    if len(anchor) != 2 or any(isinstance(x, bool) or not isinstance(x, (int, float))
                               or not math.isfinite(x) or not 0 <= x <= 1 for x in anchor):
        raise ValueError("invalid_descriptor_anchor")
    return anchor


def normalize_text(text):
    return " ".join(unicodedata.normalize("NFC", str(text)).split())


def check_deadline(deadline):
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("deadline_exceeded")


def bounded_call(fn, deadline):
    check_deadline(deadline)
    if deadline is None:
        return fn()
    result = queue.Queue(maxsize=1)

    def work():
        try:
            result.put((True, fn()))
        except BaseException as exc:
            result.put((False, exc))

    threading.Thread(target=work, daemon=True, name="adaptive-image-read").start()
    try:
        ok, value = result.get(timeout=max(.001, deadline - time.monotonic()))
    except queue.Empty as exc:
        raise TimeoutError("ocr_deadline") from exc
    if not ok:
        raise value
    check_deadline(deadline)
    return value


def default_ocr(image):
    from backend.ocr_service import get_ocr_engine
    from .ocr_compat import iter_ocr_text_items, run_paddle_ocr

    return iter_ocr_text_items(run_paddle_ocr(get_ocr_engine(use_angle_cls=False, lang="ch"), image, use_cls=False))


def ocr_items(image, ocr=None, deadline=None):
    response = bounded_call(lambda: (ocr or default_ocr)(image), deadline)
    if isinstance(response, dict) and "status" in response:
        if response["status"] != "ok":
            raise RuntimeError("ocr_unavailable")
        response = response.get("items", [])
    if not isinstance(response, (list, tuple)):
        raise RuntimeError("invalid_ocr_contract")
    items = []
    for item in response:
        if not isinstance(item, dict):
            raise RuntimeError("invalid_ocr_contract")
        text = normalize_text(item.get("text", ""))
        score = item.get("score", item.get("confidence"))
        box = item.get("box", item.get("bounds"))
        if not text:
            continue
        if score is None or not isinstance(box, (list, tuple)):
            raise RuntimeError("invalid_ocr_contract")
        try:
            if len(box) == 4 and all(isinstance(x, (int, float)) for x in box):
                x1, y1, x2, y2 = map(float, box)
            else:
                x1, x2 = min(float(p[0]) for p in box), max(float(p[0]) for p in box)
                y1, y2 = min(float(p[1]) for p in box), max(float(p[1]) for p in box)
            score = float(score)
            if not all(math.isfinite(x) for x in (score, x1, y1, x2, y2)) or not 0 <= score <= 1 or x2 <= x1 or y2 <= y1:
                raise RuntimeError("invalid_ocr_contract")
        except (ValueError, TypeError, IndexError):
            raise RuntimeError("invalid_ocr_contract")
        items.append({"text": text, "score": score, "rect": [x1, y1, x2 - x1, y2 - y1]})
    return items


def _rect(value):
    if (not isinstance(value, list) or len(value) != 4
            or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in value)):
        raise ValueError("invalid_descriptor_region")
    x1, y1, x2, y2 = value
    if not (0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1):
        raise ValueError("invalid_descriptor_region")


def validate_descriptor(descriptor):
    if not isinstance(descriptor, dict) or descriptor.get("schema_version") != 1:
        raise ValueError("unsupported_descriptor_schema")
    size = descriptor.get("template_size")
    if not isinstance(size, list) or len(size) != 2 or any(isinstance(x, bool) or not isinstance(x, int) or x <= 0 for x in size):
        raise ValueError("invalid_descriptor_size")
    regions = descriptor.get("regions")
    if not isinstance(regions, list) or not regions or len(regions) > 32:
        raise ValueError("invalid_descriptor_regions")
    ids = {}
    for region in regions:
        if not isinstance(region, dict) or not isinstance(region.get("id"), str) or not region["id"] or region["id"] in ids:
            raise ValueError("invalid_descriptor_region_id")
        if region.get("kind") not in {"visual", "text"} or region.get("required") is not True:
            raise ValueError("invalid_descriptor_region")
        _rect(region.get("rect"))
        if region["kind"] == "text" and (not isinstance(region.get("text"), str) or not normalize_text(region["text"])):
            raise ValueError("invalid_descriptor_text")
        ids[region["id"]] = region
    relations = descriptor.get("relations", [])
    if not isinstance(relations, list) or len(relations) > 64:
        raise ValueError("invalid_descriptor_relations")
    for relation in relations:
        if (not isinstance(relation, dict) or ids.get(relation.get("visual"), {}).get("kind") != "visual"
                or ids.get(relation.get("text"), {}).get("kind") != "text"
                or relation.get("direction") not in {"right", "left", "above", "below"}):
            raise ValueError("invalid_descriptor_relation")
        for key, default in (("max_gap", 2.), ("max_alignment", .5)):
            number = relation.get(key, default)
            if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or not 0 <= number <= 8:
                raise ValueError("invalid_descriptor_relation")
    for region in regions:
        if region["kind"] == "text" and not any(r["text"] == region["id"] for r in relations):
            raise ValueError("unbound_descriptor_text")
    tap = descriptor.get("tap")
    if not isinstance(tap, dict) or tap.get("region_id") not in ids:
        raise ValueError("invalid_descriptor_tap")
    anchor = tap.get("anchor")
    if not isinstance(anchor, list) or len(anchor) != 2 or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1 for v in anchor):
        raise ValueError("invalid_descriptor_anchor")
    return descriptor


def validate_coverage(image, descriptor):
    """A valid digest must not legitimize a descriptor omitting visible evidence."""
    _, np = _load_cv_stack()
    h, w = image.shape[:2]
    border = np.concatenate((image[0], image[-1], image[:, 0], image[:, -1]))
    background = np.median(border, axis=0)
    foreground = np.max(np.abs(image.astype(float) - background), axis=2) >= 48
    covered = np.zeros((h, w), dtype=bool)
    for region in descriptor["regions"]:
        x1, y1, x2, y2 = region["rect"]
        left, top = max(0, int(math.floor(x1 * w))), max(0, int(math.floor(y1 * h)))
        right, bottom = min(w, int(math.ceil(x2 * w))), min(h, int(math.ceil(y2 * h)))
        covered[top:bottom, left:right] = True
    if int((foreground & ~covered).sum()) > max(8, .01 * int(foreground.sum())):
        raise ValueError("descriptor_omits_foreground")
    return descriptor


def _config(raw, descriptor):
    canonical = canonical_descriptor(descriptor)
    return {"profile": PROFILE, "template_sha256": hashlib.sha256(raw).hexdigest(),
            "descriptor": descriptor, "descriptor_canonical": canonical,
            "descriptor_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest()}


def _with_anchor(raw, config, anchor):
    if anchor is None or config.get("profile") == "strict-v1":
        return config
    px, py = normalize_anchor(anchor)
    descriptor = copy.deepcopy(config["descriptor"])
    region = next((r for r in descriptor["regions"] if r["rect"][0] <= px <= r["rect"][2]
                   and r["rect"][1] <= py <= r["rect"][3]), None)
    if region is None:
        w, h = descriptor["template_size"]
        descriptor = {"schema_version": 1, "template_size": [w, h],
                      "regions": [{"id": "whole", "kind": "visual", "required": True, "rect": [0., 0., 1., 1.]}],
                      "relations": [], "tap": {"region_id": "whole", "anchor": list(anchor)}}
    else:
        x1, y1, x2, y2 = region["rect"]
        descriptor["tap"] = {"region_id": region["id"], "anchor": [(px - x1) / (x2 - x1), (py - y1) / (y2 - y1)]}
    validate_descriptor(descriptor)
    return _config(raw, descriptor)


def analyze_template(raw, anchor=None, ocr=None, deadline=None):
    check_deadline(deadline)
    cv2, np = _load_cv_stack()
    image = image_to_bgr(raw, source="template")
    h, w = image.shape[:2]
    anchor = normalize_anchor(anchor)
    full = {"id": "whole", "kind": "visual", "required": True, "rect": [0., 0., 1., 1.]}
    descriptor = {"schema_version": 1, "template_size": [w, h], "regions": [full], "relations": [],
                  "tap": {"region_id": "whole", "anchor": anchor}}
    # A conservative component split: all high-contrast non-background pixels
    # must belong to a visual component or a confidently recognized outer label.
    border = np.concatenate((image[0], image[-1], image[:, 0], image[:, -1]))
    background = np.median(border, axis=0)
    foreground = (np.max(np.abs(image.astype(float) - background), axis=2) >= 48).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(foreground, 8)
    components = sorted(stats[1:].tolist(), key=lambda s: s[4], reverse=True)
    if not components or components[0][4] < 64:
        return _config(raw, descriptor)
    x, y, bw, bh, area = components[0]
    # One compact graphic must dominate. Label-only templates remain whole.
    if min(bw, bh) < 12 or max(bw / bh, bh / bw) > 2.5 or area < .20 * foreground.sum():
        return _config(raw, descriptor)
    x1, y1, x2, y2 = max(0, x - 2), max(0, y - 2), min(w, x + bw + 2), min(h, y + bh + 2)
    outside = foreground.copy()
    outside[y1:y2, x1:x2] = 0
    if outside.sum() < max(16, .03 * foreground.sum()):
        return _config(raw, descriptor)
    items = ocr_items(image, ocr, deadline)
    external = []
    covered = np.zeros_like(foreground)
    covered[y1:y2, x1:x2] = 1
    for item in items:
        tx, ty, tw, th = item["rect"]
        inter = max(0, min(tx + tw, x2) - max(tx, x1)) * max(0, min(ty + th, y2) - max(ty, y1))
        if item["score"] < .90 or inter / max(1, tw * th) >= .5:
            continue
        if tx >= x2 - 2:
            direction = "right"
        elif tx + tw <= x1 + 2:
            direction = "left"
        elif ty >= y2 - 2:
            direction = "below"
        elif ty + th <= y1 + 2:
            direction = "above"
        else:
            continue
        ix1, iy1 = max(0, int(math.floor(tx)) - 2), max(0, int(math.floor(ty)) - 2)
        ix2, iy2 = min(w, int(math.ceil(tx + tw)) + 2), min(h, int(math.ceil(ty + th)) + 2)
        covered[iy1:iy2, ix1:ix2] = 1
        external.append((item, direction, [ix1 / w, iy1 / h, ix2 / w, iy2 / h]))
    if not external or (foreground & (1 - covered)).sum() > max(8, .01 * foreground.sum()):
        return _config(raw, descriptor)
    visual = {"id": "visual-1", "kind": "visual", "required": True, "rect": [x1 / w, y1 / h, x2 / w, y2 / h]}
    regions, relations = [visual], []
    for i, (item, direction, rect) in enumerate(external, 1):
        rid = f"text-{i}"
        regions.append({"id": rid, "kind": "text", "required": True, "rect": rect, "text": item["text"]})
        relations.append({"visual": "visual-1", "text": rid, "direction": direction, "max_gap": 2., "max_alignment": .5})
    px, py = anchor
    tap_region = next((r for r in regions if r["rect"][0] <= px <= r["rect"][2] and r["rect"][1] <= py <= r["rect"][3]), None)
    if tap_region is None:
        # No safe piecewise map for a click point in the gaps.
        return _config(raw, descriptor)
    rx1, ry1, rx2, ry2 = tap_region["rect"]
    descriptor.update(regions=regions, relations=relations,
                      tap={"region_id": tap_region["id"], "anchor": [(px - rx1) / (rx2 - rx1), (py - ry1) / (ry2 - ry1)]})
    validate_descriptor(descriptor)
    return _config(raw, descriptor)


def ensure_descriptor(raw, config=None, anchor=None, ocr=None, deadline=None):
    check_deadline(deadline)
    digest = hashlib.sha256(raw).hexdigest()
    config = {} if config is None else config
    if not isinstance(config, dict):
        raise ValueError("invalid_image_match_config")
    profile = config.get("profile", PROFILE)
    if profile not in {PROFILE, "strict-v1"}:
        raise ValueError("unsupported_image_match_profile")
    if config.get("template_sha256", digest) != digest:
        raise ValueError("template_hash_mismatch")
    if profile == "strict-v1":
        return {**config, "profile": "strict-v1", "template_sha256": digest}
    if "descriptor" in config or "descriptor_canonical" in config:
        canonical = config.get("descriptor_canonical")
        if canonical is not None:
            if not isinstance(canonical, str):
                raise ValueError("invalid_descriptor_canonical")
            descriptor = json.loads(canonical)
            if "descriptor" in config and descriptor != config["descriptor"]:
                raise ValueError("descriptor_mismatch")
        else:
            descriptor = config["descriptor"]
            canonical = canonical_descriptor(descriptor)
        if config.get("descriptor_sha256") != hashlib.sha256(canonical.encode("utf-8")).hexdigest():
            raise ValueError("descriptor_hash_mismatch")
        validate_descriptor(descriptor)
        image = image_to_bgr(raw, source="template")
        if descriptor["template_size"] != [image.shape[1], image.shape[0]]:
            raise ValueError("descriptor_size_mismatch")
        validate_coverage(image, descriptor)
        checked = {**config, "profile": PROFILE, "template_sha256": digest, "descriptor": descriptor, "descriptor_canonical": canonical}
        return _with_anchor(raw, checked, anchor)
    anchor = tuple(normalize_anchor(anchor))
    key = (digest, ANALYZER_VERSION, OCR_VERSION, ocr_identity(ocr), id(ocr) if ocr is not None else None, anchor)
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is not None:
            _CACHE.move_to_end(key)
            return copy.deepcopy(cached)
    result = analyze_template(raw, list(anchor), ocr, deadline)
    with _CACHE_LOCK:
        _CACHE[key] = copy.deepcopy(result)
        _CACHE.move_to_end(key)
        while len(_CACHE) > _CACHE_LIMIT:
            _CACHE.popitem(last=False)
    return result
