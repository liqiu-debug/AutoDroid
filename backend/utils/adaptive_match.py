"""One-template adaptive localization with unchanged strict visual verification."""
from __future__ import annotations

import math
import hashlib
import json
import threading
import time
from collections import OrderedDict

from .adaptive_resize import resize_bilinear
from .adaptive_template import (PROFILE as ADAPTIVE_VERSION, check_deadline, normalize_text,
                                ocr_items, ocr_identity, validate_descriptor, validate_coverage)
from .image_match import PROFILE, _same_object, _verify, match_image, template_quality
from .image_match_options import normalize_image_options
from .template_match import DEFAULT_TEMPLATE_SCALES, _load_cv_stack, image_to_bgr

COARSE_RATIO = 1.12
PROPOSAL_SIDE = 960
MAX_OBJECTS = 64
_TEMPLATE_RASTERS = OrderedDict()
_RASTER_LOCK = threading.RLock()
_RASTER_BYTES = 0
_RASTER_LIMIT = 256
_RASTER_BYTE_LIMIT = 64 * 1024 * 1024


def _raster(template, width, height, digest):
    global _RASTER_BYTES
    cv2, _ = _load_cv_stack()
    key = (digest, int(width), int(height))
    with _RASTER_LOCK:
        found = _TEMPLATE_RASTERS.get(key)
        if found is not None:
            _TEMPLATE_RASTERS.move_to_end(key)
            return found
    scaled = resize_bilinear(template, width, height)
    gray = cv2.cvtColor(scaled, cv2.COLOR_BGR2GRAY)
    entry = (scaled, gray)
    cost = scaled.nbytes + gray.nbytes
    if cost <= _RASTER_BYTE_LIMIT:
        # Read-only rasters are safe to reuse across devices and stable frames.
        scaled.flags.writeable = False
        gray.flags.writeable = False
        with _RASTER_LOCK:
            if key not in _TEMPLATE_RASTERS:
                _TEMPLATE_RASTERS[key] = entry
                _RASTER_BYTES += cost
            while len(_TEMPLATE_RASTERS) > _RASTER_LIMIT or _RASTER_BYTES > _RASTER_BYTE_LIMIT:
                _, old = _TEMPLATE_RASTERS.popitem(last=False)
                _RASTER_BYTES -= old[0].nbytes + old[1].nbytes
    return entry


def _round(value):
    return int(math.floor(value + .5))


def _scales(tw, th, sw, sh, prior=1.):
    lo, hi = 8 / min(tw, th), min(sw / tw, sh / th)
    if lo > hi:
        return []
    values = {lo, hi, min(hi, max(lo, 1.)), min(hi, max(lo, prior))}
    # Preserve the frozen profile's proposal seeds in addition to the expanded
    # logarithmic pyramid; quantization of tiny glyphs is not monotonic in NCC.
    values.update(s for s in DEFAULT_TEMPLATE_SCALES if lo <= s <= hi)
    scale = lo
    while scale < hi:
        values.add(scale)
        scale *= COARSE_RATIO
    return sorted(values, key=lambda s: (abs(math.log(s / max(prior, .001))), s))


def refined_sizes(tw, th, scale, lo, hi):
    """All integer sizes attained between adjacent coarse half intervals."""
    low, high = max(lo, scale / math.sqrt(COARSE_RATIO)), min(hi, scale * math.sqrt(COARSE_RATIO))
    breaks = {low, high, min(high, max(low, scale))}
    for side in (tw, th):
        for integer in range(max(0, int(math.floor(side * low - .5))), int(math.ceil(side * high + .5)) + 1):
            point = (integer + .5) / side
            if low <= point <= high:
                breaks.add(point)
    points = sorted(breaks)
    probes = points + [(a + b) / 2 for a, b in zip(points, points[1:])]
    sizes = {(_round(tw * s), _round(th * s)) for s in probes}
    return sorted(sizes, key=lambda size: (abs(size[0] / tw - scale), size))


def _region_rect(region, tw, th):
    x1, y1, x2, y2 = region["rect"]
    left, top = max(0, _round(x1 * tw)), max(0, _round(y1 * th))
    right, bottom = min(tw, _round(x2 * tw)), min(th, _round(y2 * th))
    return [left, top, right - left, bottom - top]


def _search(screen, template, deadline, prior=1., offset=(0, 0)):
    cv2, np = _load_cv_stack()
    sh, sw = screen.shape[:2]
    th, tw = template.shape[:2]
    digest = hashlib.sha256(template.tobytes()).hexdigest()
    scales = _scales(tw, th, sw, sh, prior)
    if not scales:
        return [], False
    gray = cv2.cvtColor(screen, cv2.COLOR_BGR2GRAY)
    pyramids = {}
    raw, overflow = [], False
    sizes = set()
    for scale in scales:
        check_deadline(deadline)
        w, h = _round(tw * scale), _round(th * scale)
        if (w, h) in sizes or min(w, h) < 8 or w > sw or h > sh:
            continue
        sizes.add((w, h))
        # Small objects retain thirty-two proposal pixels, instead of becoming
        # meaningless two-pixel high-correlation speckles in a fixed pyramid.
        factor = min(max(1., max(sw, sh) / PROPOSAL_SIDE), max(1., min(w, h) / 32))
        # Quantize pyramid factors to cache the screenshot across scales.
        factor = max(1., math.floor(factor * 4) / 4)
        if factor not in pyramids:
            pw, ph = _round(sw / factor), _round(sh / factor)
            pyramids[factor] = resize_bilinear(gray, pw, ph)
        search = pyramids[factor]
        fw, fh = max(8, _round(w / factor)), max(8, _round(h / factor))
        if fw > search.shape[1] or fh > search.shape[0]:
            continue
        scaled, tplgray = _raster(template, fw, fh, digest)
        if float(tplgray.std()) <= .5:
            continue
        response = cv2.matchTemplate(search, tplgray, cv2.TM_CCOEFF_NORMED)
        response[~np.isfinite(response)] = -1
        threshold = .82 if factor == 1 else .65
        for peak in range(21):
            _, score, _, location = cv2.minMaxLoc(response)
            if score < threshold:
                break
            if peak == 20:
                overflow = True
                break
            px, py = location
            x, y = _round(px * factor), _round(py * factor)
            raw.append({"rect": [x, y, w, h], "similarity": float(score), "scale": scale})
            rx, ry = max(1, fw // 3), max(1, fh // 3)
            response[max(0, py - ry):py + ry + 1, max(0, px - rx):px + rx + 1] = -1
    groups = []
    for candidate in sorted(raw, key=lambda c: c["similarity"], reverse=True):
        if not any(_same_object(candidate["rect"], old["rect"]) for old in groups):
            groups.append(candidate)
    if len(groups) > MAX_OBJECTS:
        overflow = True
    verified = []
    lo, hi = 8 / min(tw, th), min(sw / tw, sh / th)
    for proposal in groups[:MAX_OBJECTS]:
        check_deadline(deadline)
        x, y, pw, ph = proposal["rect"]
        # The strongest NCC basin owns this object. Refining the same logo at
        # every weak neighboring octave wastes the two-frame step budget.
        target_sizes = refined_sizes(tw, th, proposal["scale"], lo, hi)
        proposals = []
        best = None
        seen_sizes = set()
        # If quantization moves the strongest coarse peak into a neighboring
        # basin, refine the other proposals for the same object before absence.
        nearby = [p for p in raw if _same_object(p["rect"], proposal["rect"])]
        for pass_index in range(2):
            if pass_index:
                target_sizes = set()
                for p in nearby:
                    target_sizes.update(refined_sizes(tw, th, p["scale"], lo, hi))
            for w, h in sorted(target_sizes, key=lambda s: abs(s[0] - pw) + abs(s[1] - ph)):
                check_deadline(deadline)
                if (w, h) in seen_sizes or min(w, h) < 8 or w > sw or h > sh:
                    continue
                seen_sizes.add((w, h))
                radius = max(abs(w - pw), abs(h - ph)) + 3
                left, top = max(0, x - radius), max(0, y - radius)
                right, bottom = min(sw, x + max(pw, w) + radius), min(sh, y + max(ph, h) + radius)
                if right - left < w or bottom - top < h:
                    continue
                scaled, sg = _raster(template, w, h, digest)
                response = cv2.matchTemplate(gray[top:bottom, left:right], sg, cv2.TM_CCOEFF_NORMED)
                _, score, _, loc = cv2.minMaxLoc(response)
                if not math.isfinite(score) or score < PROFILE["candidate_ncc"]:
                    continue
                cx, cy = left + loc[0], top + loc[1]
                proposals.append((float(score), cx, cy, w, h))
            # Highest NCC first; lower scores cannot improve an accepted scale.
            for score, cx, cy, w, h in sorted(proposals, reverse=True):
                check_deadline(deadline)
                if best is not None and score < PROFILE["accept_ncc"]:
                    break
                scaled, _ = _raster(template, w, h, digest)
                metrics = _verify(screen[cy:cy + h, cx:cx + w], scaled, score)
                candidate = {"rect": [cx + offset[0], cy + offset[1], w, h], "similarity": score,
                             "metrics": metrics, "accepted": metrics["accepted"], "scale": w / tw}
                if best is None or score > best["similarity"]:
                    best = candidate
                if candidate["accepted"]:
                    best = candidate
                    break
            if best is not None and best["accepted"]:
                break
        if best is not None:
            verified.append(best)
    return verified, overflow


def relation_matches(visual, text, relation):
    vx, vy, vw, vh = visual
    tx, ty, tw, th = text
    if min(vw, vh, tw, th) <= 0:
        return False
    overlap = max(0, min(vx + vw, tx + tw) - max(vx, tx)) * max(0, min(vy + vh, ty + th) - max(vy, ty))
    if overlap / (tw * th) >= .5:
        return False
    direction = relation["direction"]
    if direction in {"right", "left"}:
        gap = ((tx - vx - vw) if direction == "right" else (vx - tx - tw)) / min(vw, vh)
        alignment = abs(ty + th / 2 - vy - vh / 2) / max(vh, th)
    else:
        gap = ((ty - vy - vh) if direction == "below" else (vy - ty - th)) / min(vw, vh)
        alignment = abs(tx + tw / 2 - vx - vw / 2) / max(vw, tw)
    return -.05 <= gap <= relation.get("max_gap", 2.) and alignment <= relation.get("max_alignment", .5)


def _union(rects):
    left, top = min(r[0] for r in rects), min(r[1] for r in rects)
    right, bottom = max(r[0] + r[2] for r in rects), max(r[1] + r[3] for r in rects)
    return [left, top, right - left, bottom - top]


def _compose(screen, visual_results, descriptor, ocr, deadline, bounds=None):
    sh, sw = screen.shape[:2]
    bx1, by1, bx2, by2 = bounds or (0, 0, sw, sh)
    text_regions = [r for r in descriptor["regions"] if r["kind"] == "text"]
    visual_regions = [r for r in descriptor["regions"] if r["kind"] == "visual"]
    # The analyzer currently creates one required visual anchor. Explicit
    # multi-anchor descriptors are rejected, never partially checked.
    if len(visual_regions) != 1:
        raise ValueError("unsupported_multiple_visual_regions")
    visual_id = visual_regions[0]["id"]
    combined = []
    for candidate in visual_results:
        if not candidate["accepted"]:
            continue
        check_deadline(deadline)
        vx, vy, vw, vh = candidate["rect"]
        padding = _round(max(vw, vh) * 2.5)
        left, top = max(bx1, vx - padding), max(by1, vy - padding)
        right, bottom = min(bx2, vx + vw + padding), min(by2, vy + vh + padding)
        detected = ocr_items(screen[top:bottom, left:right], ocr, deadline)
        for item in detected:
            item["rect"][0] += left
            item["rect"][1] += top
        detected = [item for item in detected if bx1 <= item["rect"][0] and by1 <= item["rect"][1]
                    and item["rect"][0] + item["rect"][2] <= bx2 and item["rect"][1] + item["rect"][3] <= by2]
        assemblies = [({visual_id: {"rect": candidate["rect"], "metrics": candidate["metrics"], "accepted": True}}, set())]
        for region in text_regions:
            relations = [r for r in descriptor["relations"] if r["text"] == region["id"]]
            adjacent = [(i, item) for i, item in enumerate(detected) if
                        all(r["visual"] == visual_id and relation_matches(candidate["rect"], item["rect"], r) for r in relations)]
            if not adjacent:
                raise RuntimeError("ocr_incomplete")
            if any(item["score"] < .90 for _, item in adjacent):
                raise RuntimeError("ocr_low_confidence")
            possible = [(i, item) for i, item in adjacent if item["text"] == normalize_text(region["text"])]
            updated = []
            for matches, used in assemblies:
                for index, item in possible:
                    if index not in used:
                        updated.append(({**matches, region["id"]: {**item, "accepted": True}}, used | {index}))
            if len(updated) > MAX_OBJECTS:
                raise RuntimeError("candidate_overflow")
            assemblies = updated
            if not assemblies:
                break
        for matches, _ in assemblies:
            if len(matches) != len(descriptor["regions"]):
                continue
            rect = _union([m["rect"] for m in matches.values()])
            tap = descriptor["tap"]
            target = matches[tap["region_id"]]["rect"]
            point = [target[0] + min(target[2] - 1, tap["anchor"][0] * target[2]),
                     target[1] + min(target[3] - 1, tap["anchor"][1] * target[3])]
            combined.append({**candidate, "rect": rect, "point": point, "branch": "composed",
                             "region_matches": matches, "accepted": True})
    return combined


def _same_candidate(left, right):
    if not _same_object(left["rect"], right["rect"]):
        return False
    if left.get("branch") == right.get("branch") == "composed":
        a, b = left["region_matches"], right["region_matches"]
        return all(key in b and _same_object(value["rect"], b[key]["rect"]) for key, value in a.items())
    return True


def match_adaptive(screen, template, image_options=None, deadline=None, image_match=None, ocr=None):
    started = time.monotonic()
    candidates, quality = [], {}

    def result(status, reason, **extra):
        return {"status": status, "reason": reason, "matcher_version": ADAPTIVE_VERSION,
                "quality": quality, "candidates": candidates, "point": None,
                "timings": {"total_ms": round((time.monotonic() - started) * 1000, 2)}, **extra}

    try:
        check_deadline(deadline)
        screen, template = image_to_bgr(screen), image_to_bgr(template)
        quality = template_quality(template)
        if not quality["valid"]:
            return result("invalid_template", quality["reason"])
        if image_match and image_match.get("profile") == "strict-v1":
            return match_image(screen, template, image_options, deadline)
        config_shared = {} if image_match is None else image_match
        if not isinstance(config_shared, dict) or config_shared.get("profile", ADAPTIVE_VERSION) != ADAPTIVE_VERSION:
            return result("unsupported", "invalid_image_match_profile", search_complete=False)
        descriptor = config_shared.get("descriptor")
        canonical = config_shared.get("descriptor_canonical")
        if canonical is not None:
            if not isinstance(canonical, str) or hashlib.sha256(canonical.encode("utf-8")).hexdigest() != config_shared.get("descriptor_sha256"):
                return result("invalid_template", "descriptor_hash_mismatch", search_complete=False)
            parsed = json.loads(canonical)
            if descriptor is not None and descriptor != parsed:
                return result("invalid_template", "descriptor_mismatch", search_complete=False)
            descriptor = parsed
        elif descriptor is not None:
            return result("invalid_template", "descriptor_canonical_required", search_complete=False)
        if descriptor is None:
            # Direct numerical callers without preparation use the whole image;
            # execution wrappers always supply a verified cached descriptor.
            descriptor = {"schema_version": 1, "template_size": [template.shape[1], template.shape[0]],
                          "regions": [{"id": "whole", "kind": "visual", "required": True, "rect": [0, 0, 1, 1]}],
                          "relations": [], "tap": {"region_id": "whole", "anchor": [.5, .5]}}
        validate_descriptor(descriptor)
        validate_coverage(template, descriptor)
        if descriptor["template_size"] != [template.shape[1], template.shape[0]]:
            return result("invalid_template", "descriptor_size_mismatch")
        config = normalize_image_options(image_options)
        sh, sw = screen.shape[:2]
        th, tw = template.shape[:2]
        prior = 1.
        if "source_size" in config:
            rw, rh = config["source_size"]
            prior = math.sqrt((sw / rw) * (sh / rh))
        region = config.get("search_region", [0, 0, 1, 1])
        left, top = int(region[0] * sw), int(region[1] * sh)
        right, bottom = int(math.ceil(region[2] * sw)), int(math.ceil(region[3] * sh))
        roi = screen[top:bottom, left:right]
        full, overflow = _search(roi, template, deadline, prior, (left, top))
        tap = descriptor["tap"]
        tap_region = next(r for r in descriptor["regions"] if r["id"] == tap["region_id"])
        rx1, ry1, rx2, ry2 = tap_region["rect"]
        descriptor_anchor = [rx1 + tap["anchor"][0] * (rx2 - rx1), ry1 + tap["anchor"][1] * (ry2 - ry1)]
        anchor = config.get("tap_anchor", descriptor_anchor)
        for candidate in full:
            x, y, w, h = candidate["rect"]
            candidate.update(branch="whole", point=[x + min(w - 1, anchor[0] * w), y + min(h - 1, anchor[1] * h)])
        candidates.extend(full)
        if any(r["kind"] == "text" for r in descriptor["regions"]):
            visual = [r for r in descriptor["regions"] if r["kind"] == "visual"]
            if len(visual) != 1:
                return result("unsupported", "unsupported_multiple_visual_regions")
            x, y, w, h = _region_rect(visual[0], tw, th)
            visual_matches, more = _search(roi, template[y:y + h, x:x + w], deadline, prior, (left, top))
            overflow = overflow or more
            # Complete pixel identity proves labels on this same object; other
            # independent visual candidates still need composition evidence.
            proven_visuals = []
            for candidate in full:
                if candidate["accepted"]:
                    cx, cy, cw, ch = candidate["rect"]
                    proven_visuals.append([cx + x * cw / tw, cy + y * ch / th, w * cw / tw, h * ch / th])
            unproven = [candidate for candidate in visual_matches
                        if not any(_same_object(candidate["rect"], rect) for rect in proven_visuals)]
            candidates.extend(_compose(screen, unproven, descriptor, ocr, deadline, (left, top, right, bottom)))
        candidates.sort(key=lambda c: (c["accepted"], c["similarity"]), reverse=True)
        independent = []
        for candidate in candidates:
            if not any(_same_candidate(candidate, old) for old in independent):
                independent.append(candidate)
        candidates[:] = independent[:MAX_OBJECTS]
        accepted = [c for c in candidates if c["accepted"]]
        if overflow or len(independent) > MAX_OBJECTS:
            return result("unstable", "candidate_overflow", search_complete=False)
        if len(accepted) > 1:
            return result("ambiguous", "multiple_verified_targets", search_complete=True)
        if not accepted:
            return result("not_found", "no_verified_candidate", search_complete=True)
        best = accepted[0]
        rivals = [c for c in candidates if c is not best and c["similarity"] >= .90
                  and c["metrics"]["edge_coverage"] >= .80 and c["metrics"]["foreground_error"] <= .16
                  and best["similarity"] - c["similarity"] < .03]
        if rivals:
            return result("ambiguous", "insufficient_margin", search_complete=True)
        return result("matched", "unique_verified_target", point=best["point"], rect=best["rect"],
                      branch=best["branch"], search_complete=True,
                      ocr_engine=ocr_identity(ocr), ocr_threshold=.90)
    except TimeoutError:
        return result("unstable", "deadline_exceeded", search_complete=False)
    except ValueError as exc:
        return result("invalid_template", str(exc), search_complete=False)
    except RuntimeError as exc:
        return result("unsupported", str(exc), search_complete=False)
