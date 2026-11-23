from __future__ import annotations

import hashlib
import time
import unittest
from unittest.mock import Mock

import cv2
import numpy as np

from backend.utils.adaptive_match import match_adaptive, refined_sizes, relation_matches
from backend.utils.adaptive_resize import resize_bilinear
from backend.utils.adaptive_template import (canonical_descriptor, ensure_descriptor,
                                             analyze_template, ocr_items)


def icon(side=48):
    image = np.full((side, side, 3), 255, np.uint8)
    cv2.rectangle(image, (4, 4), (side - 5, side - 5), (20, 40, 220), 3)
    cv2.line(image, (10, side // 2), (side - 10, 12), (10, 10, 10), 3)
    return image


def scene(template, scale=1.1675, duplicate=False):
    target = resize_bilinear(template, round(template.shape[1] * scale), round(template.shape[0] * scale))
    result = np.full((230, 320, 3), 255, np.uint8)
    h, w = target.shape[:2]
    result[35:35 + h, 40:40 + w] = target
    if duplicate:
        result[145:145 + h, 220:220 + w] = target
    return result


def encoded(image):
    return cv2.imencode(".png", image)[1].tobytes()


class AdaptiveImageTests(unittest.TestCase):
    def test_half_pixel_resize_golden(self):
        source = np.array([[0, 100], [200, 255]], np.uint8)
        self.assertEqual(resize_bilinear(source, 3, 3).tolist(), [[0, 50, 100], [100, 139, 178], [200, 228, 255]])

    def test_non_grid_size_is_enumerated(self):
        self.assertIn((354, 343), refined_sizes(303, 294, 1.15, .1, 4))

    def test_arbitrary_scale_and_duplicate_safety(self):
        template = icon()
        for duplicate, expected in ((False, "matched"), (True, "ambiguous")):
            result = match_adaptive(scene(template, duplicate=duplicate), template, deadline=time.monotonic() + 5)
            self.assertEqual(result["status"], expected, result)
        wrong = np.full((230, 320, 3), 255, np.uint8)
        self.assertEqual(match_adaptive(wrong, template, deadline=time.monotonic() + 5)["status"], "not_found")

    def test_descriptor_hash_and_prepared_template_never_reocr(self):
        raw = encoded(icon())
        config = analyze_template(raw, ocr=Mock(side_effect=AssertionError("not needed")))
        self.assertEqual(config["descriptor_sha256"], hashlib.sha256(config["descriptor_canonical"].encode()).hexdigest())
        self.assertEqual(ensure_descriptor(raw, config, ocr=Mock(side_effect=AssertionError("no template OCR"))), config)
        changed = {**config, "descriptor_sha256": "0" * 64}
        with self.assertRaisesRegex(ValueError, "descriptor_hash_mismatch"):
            ensure_descriptor(raw, changed)
        with self.assertRaisesRegex(ValueError, "template_hash_mismatch"):
            ensure_descriptor(encoded(icon(52)), config)

    def test_explicit_strict_is_preserved_and_default_adaptive(self):
        raw = encoded(icon())
        self.assertEqual(ensure_descriptor(raw)["profile"], "adaptive-v2")
        self.assertEqual(ensure_descriptor(raw, {"profile": "strict-v1"})["profile"], "strict-v1")
        config = ensure_descriptor(raw, anchor=[.1, .9])
        result = match_adaptive(scene(icon()), icon(), image_match=config, deadline=time.monotonic() + 5)
        x, y, w, h = result["rect"]
        self.assertEqual(result["point"], [x + .1 * w, y + .9 * h])

    def test_template_decomposes_all_foreground_and_binds_tap(self):
        template = np.full((110, 250, 3), 255, np.uint8)
        template[25:85, 10:70] = resize_bilinear(icon(), 60, 60)
        cv2.putText(template, "PAY", (105, 70), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (10, 10, 10), 2)
        ocr = Mock(return_value=[{"text": "PAY", "score": .999, "box": [100, 35, 185, 78]}])
        config = analyze_template(encoded(template), [.5, .5], ocr)
        self.assertEqual([r["kind"] for r in config["descriptor"]["regions"]], ["visual", "text"])
        self.assertEqual(config["descriptor"]["tap"]["region_id"], "text-1")
        rebound = ensure_descriptor(encoded(template), config, [40 / 250, 55 / 110], ocr=Mock(side_effect=AssertionError("reuse")))
        self.assertEqual(rebound["descriptor"]["tap"]["region_id"], "visual-1")
        self.assertNotEqual(rebound["descriptor_sha256"], config["descriptor_sha256"])

    def test_text_relation_excludes_internal_and_neighbor_rows(self):
        relation = {"direction": "right", "max_gap": 2., "max_alignment": .5}
        self.assertTrue(relation_matches([10, 10, 50, 50], [80, 20, 90, 30], relation))
        self.assertFalse(relation_matches([10, 10, 50, 50], [15, 20, 30, 20], relation))
        self.assertFalse(relation_matches([10, 10, 50, 50], [80, 100, 90, 30], relation))

    def test_composed_text_and_visual_are_both_required(self):
        template = np.full((100, 220, 3), 255, np.uint8)
        template[20:68, 10:58] = icon()
        cv2.putText(template, "PAY", (88, 58), cv2.FONT_HERSHEY_SIMPLEX, .8, (10, 10, 10), 2)
        analysis_ocr = lambda _: [{"text": "PAY", "score": .99, "box": [83, 35, 143, 65]}]
        config = analyze_template(encoded(template), [.15, .4], analysis_ocr)
        self.assertTrue(any(r["kind"] == "text" for r in config["descriptor"]["regions"]))
        screen = np.full((180, 320, 3), 255, np.uint8)
        screen[60:108, 35:83] = icon()
        # The expected text pixels deliberately differ: semantic verification
        # accepts another font, but never a different label or another row.
        for text, box, expected in [("PAY", [110, 70, 175, 100], "matched"),
                                    ("BUY", [110, 70, 175, 100], "not_found"),
                                    ("PAY", [110, 140, 175, 170], "unsupported")]:
            def read(crop, text=text, box=box):
                # Anchor at [35,60] with padding120 gives crop origin [0,0].
                return [{"text": text, "score": .99, "box": box}]
            result = match_adaptive(screen, template, image_match=config, ocr=read, deadline=time.monotonic() + 5)
            self.assertEqual(result["status"], expected, result)
        low_confidence = match_adaptive(screen, template, image_match=config,
                                        ocr=lambda _: [{"text": "PAY", "score": .6, "box": [110, 70, 175, 100]}],
                                        deadline=time.monotonic() + 5)
        self.assertEqual(low_confidence["status"], "unsupported")
        self.assertEqual(low_confidence["reason"], "ocr_low_confidence")

    def test_ambiguous_or_unexplained_foreground_keeps_whole_template(self):
        template = np.full((110, 250, 3), 255, np.uint8)
        template[25:85, 10:70] = resize_bilinear(icon(), 60, 60)
        cv2.putText(template, "PAY", (105, 70), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (10, 10, 10), 2)
        cv2.circle(template, (220, 35), 12, (0, 0, 0), -1)
        result = analyze_template(encoded(template), [.5, .5], lambda _: [{"text": "PAY", "score": .99, "box": [100, 35, 185, 78]}])
        self.assertEqual(result["descriptor"]["regions"][0]["id"], "whole")
        self.assertEqual(len(result["descriptor"]["regions"]), 1)
        edited = result["descriptor"]
        edited["regions"][0]["rect"] = [.04, .20, .30, .80]
        canonical = canonical_descriptor(edited)
        config = {**result, "descriptor_canonical": canonical, "descriptor_sha256": hashlib.sha256(canonical.encode()).hexdigest()}
        with self.assertRaisesRegex(ValueError, "descriptor_omits_foreground"):
            ensure_descriptor(encoded(template), config)

    def test_unknown_ocr_and_expired_search_never_prove_absence(self):
        with self.assertRaisesRegex(RuntimeError, "ocr_unavailable"):
            ocr_items(icon(), lambda _: {"status": "unknown", "items": []})
        with self.assertRaises(TimeoutError):
            ocr_items(icon(), lambda _: [], deadline=time.monotonic() - 1)
        result = match_adaptive(scene(icon()), icon(), deadline=time.monotonic() - 1)
        self.assertEqual(result["status"], "unstable")
        self.assertFalse(result["search_complete"])

    def test_multiple_correct_labels_prove_presence_but_refuse_a_click(self):
        template = np.full((100, 220, 3), 255, np.uint8)
        template[20:68, 10:58] = icon()
        cv2.putText(template, "PAY", (88, 58), cv2.FONT_HERSHEY_SIMPLEX, .8, (10, 10, 10), 2)
        config = analyze_template(encoded(template), [.15, .4],
            lambda _: [{"text": "PAY", "score": .99, "box": [83, 35, 143, 65]}])
        screen = np.full((180, 320, 3), 255, np.uint8)
        screen[60:108, 35:83] = icon()
        read = lambda _: [{"text": "PAY", "score": .99, "box": [105, 70, 145, 100]},
                          {"text": "PAY", "score": .99, "box": [155, 70, 195, 100]}]
        result = match_adaptive(screen, template, image_match=config, ocr=read, deadline=time.monotonic() + 5)
        self.assertEqual(result["status"], "ambiguous", result)
        self.assertEqual(len([c for c in result["candidates"] if c["accepted"]]), 2)
        self.assertIsNone(result["point"])

    def test_component_label_and_tap_cannot_escape_the_search_region(self):
        template = np.full((100, 220, 3), 255, np.uint8)
        template[20:68, 10:58] = icon()
        cv2.putText(template, "PAY", (88, 58), cv2.FONT_HERSHEY_SIMPLEX, .8, (10, 10, 10), 2)
        config = analyze_template(encoded(template), [.5, .5],
            lambda _: [{"text": "PAY", "score": .99, "box": [83, 35, 143, 65]}])
        screen = np.full((180, 320, 3), 255, np.uint8)
        screen[60:108, 35:83] = icon()
        read = lambda _: [{"text": "PAY", "score": .99, "box": [110, 70, 175, 100]}]
        options = {"template_sha256": config["template_sha256"], "search_region": [0, 0, 100 / 320, 1]}
        result = match_adaptive(screen, template, options, image_match=config, ocr=read, deadline=time.monotonic() + 5)
        self.assertEqual(result["status"], "unsupported", result)
        self.assertIsNone(result["point"])
        self.assertFalse(any(c["accepted"] for c in result["candidates"]))

    def test_numerical_matcher_rejects_a_descriptor_that_disagrees_with_canonical(self):
        import copy
        raw = encoded(icon())
        config = ensure_descriptor(raw)
        altered = copy.deepcopy(config)
        altered["descriptor"]["tap"]["anchor"] = [0., 0.]
        result = match_adaptive(scene(icon()), icon(), image_match=altered, deadline=time.monotonic() + 5)
        self.assertEqual(result["reason"], "descriptor_mismatch")
        self.assertEqual(result["status"], "invalid_template")


if __name__ == "__main__":
    unittest.main()
