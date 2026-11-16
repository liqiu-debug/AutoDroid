"""Generate disjoint deterministic image families and evaluate strict localization.

Generated samples are a reproducible engineering benchmark, not a substitute for
independent production pages. Optional real audit fixtures remain separate cases.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.utils.image_match import MATCHER_VERSION, PROFILE, match_image, rect_iou


def glyph(seed):
    rng = np.random.default_rng(seed)
    image = np.full((32, 32, 3), int(rng.integers(200, 246)), np.uint8)
    ink = tuple(int(v) for v in rng.integers(5, 100, 3))
    if seed % 3:
        ink = (30, 30, 30)
    for _ in range(4):
        x, y = rng.integers(5, 25, 2)
        if rng.random() < .5:
            cv2.circle(image, (int(x), int(y)), int(rng.integers(2, 7)), ink, 1, cv2.LINE_AA)
        else:
            end = tuple(int(v) for v in rng.integers(5, 27, 2))
            cv2.line(image, (int(x), int(y)), end, ink, int(rng.integers(1, 3)), cv2.LINE_AA)
    return image


def generate(directory: Path, split: str, real_fixtures: Path | None = None):
    directory.mkdir(parents=True, exist_ok=True)
    cases = []
    base = 17000 if split == "calibration" else 43000
    families = []
    for family in range(40):
        seed = base + family * 17
        families.append(seed)
        rng = np.random.default_rng(seed + 1)
        template = glyph(seed)
        template_name = f"template-{seed}.png"
        cv2.imwrite(str(directory / template_name), template)
        digest = hashlib.sha256((directory / template_name).read_bytes()).hexdigest()
        for index in range(30):
            screen = np.full((120, 160, 3), int(rng.integers(210, 256)), np.uint8)
            x, y = int(rng.integers(5, 65)), int(rng.integers(5, 65))
            expected = None
            if index < 5:
                scale = [1, .95, 1.05, 1, 1][index]
                side = round(32 * scale)
                target = cv2.resize(template, (side, side), interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
                if index == 3:
                    _, encoded = cv2.imencode(".jpg", target, [cv2.IMWRITE_JPEG_QUALITY, 95])
                    target = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
                if index == 4:
                    target = np.clip(target.astype(int) + 3, 0, 255).astype(np.uint8)
                screen[y:y + side, x:x + side] = target
                expected = [x, y, side, side]
                category = ["exact", "scale_down", "scale_up", "jpeg", "brightness"][index]
            elif index < 10:
                category = "absent_constant"
            elif index < 25:
                # Distinct out-of-family glyphs must not be confused with target.
                other = glyph(seed + 100000 + index * 97)
                screen[y:y + 32, x:x + 32] = other
                category = "different_shape"
            else:
                screen[10:42, 10:42] = template
                screen[65:97, 105:137] = template
                category = "duplicate"
            name = f"screen-{seed}-{index}.png"
            cv2.imwrite(str(directory / name), screen)
            case = {"id": f"{split}-{seed}-{index}", "family": str(seed), "category": category,
                    "screen": name, "template": template_name, "options": {"template_sha256": digest},
                    "label": "positive" if expected else "negative"}
            if expected:
                case["expected_rect"] = expected
            cases.append(case)
    if real_fixtures and real_fixtures.exists() and split == "acceptance":
        import shutil
        for source in sorted((real_fixtures / "templates").glob("*.png")):
            target = directory / source.name
            shutil.copy2(source, target)
            template = cv2.imread(str(target))
            blank_name = "white.png"
            if not (directory / blank_name).exists():
                cv2.imwrite(str(directory / blank_name), np.full((400, 800, 3), 255, np.uint8))
            cases.append({"id": "real-absent-" + source.stem, "family": "real-" + source.stem,
                          "category": "real_template_absent", "screen": blank_name, "template": source.name,
                          "options": {}, "label": "negative"})
        shutil.copy2(real_fixtures / "current-android-screen.png", directory / "real-screen.png")
        for suffix, expected in [("6bff073fdaf3fe6c", None), ("f47328d074bd5432", None),
                                 ("04ec7231d001f7c5", [1015, 188, 65, 69])]:
            case = {"id": "real-screen-" + suffix, "family": "real-" + suffix,
                    "category": "real_screenshot", "screen": "real-screen.png",
                    "template": f"element_{suffix}.png", "options": {},
                    "label": "positive" if expected else "negative"}
            if expected:
                case["expected_rect"] = expected
            cases.append(case)
    manifest = {"version": 1, "split": split, "families": families, "cases": cases,
                "provenance": "Deterministic generated icon families; acceptance may also include separately collected real audit fixtures.",
                "limits": "Generated data is not 200 independent production pages; real rollout still requires representative live-page review."}
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def evaluate(directory: Path, output: Path, engine="platform"):
    matcher = match_image
    if engine == "desktop":
        sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "AutoDroid-Desktop"))
        from engine.core.utils.image_match import match_image as desktop_match
        matcher = desktop_match
    manifest = json.loads((directory / "manifest.json").read_text())
    results = []
    for case in manifest["cases"]:
        screen, template = cv2.imread(str(directory / case["screen"])), cv2.imread(str(directory / case["template"]))
        started = time.monotonic()
        result = matcher(screen, template, case.get("options"))
        elapsed = (time.monotonic() - started) * 1000
        positive = case["label"] == "positive"
        geometry = False
        if positive and result["status"] == "matched":
            x, y, w, h = case["expected_rect"]
            px, py = result["point"]
            geometry = rect_iou(result["rect"], case["expected_rect"]) >= .8 and x <= px < x + w and y <= py < y + h
        results.append({"id": case["id"], "label": case["label"], "category": case["category"],
                        "status": result["status"], "correct": bool(geometry if positive else result["status"] != "matched"),
                        "elapsed_ms": round(elapsed, 3), "diagnostic": result})
    positives = [r for r in results if r["label"] == "positive"]
    negatives = [r for r in results if r["label"] == "negative"]
    wrong_positive = sum(r["status"] == "matched" and not r["correct"] for r in positives)
    fp = sum(not r["correct"] for r in negatives) + wrong_positive
    recall = sum(r["correct"] for r in positives) / len(positives)
    times = [r["elapsed_ms"] for r in results]
    summary = {"positive_pairs": len(positives), "negative_pairs": len(negatives), "false_accepts": fp,
               "positive_recall": recall, "positive_rejection_rate": 1 - recall,
               "negative_rejection_rate": sum(r["correct"] for r in negatives) / len(negatives),
               "p50_ms": float(np.percentile(times, 50)), "p95_ms": float(np.percentile(times, 95)),
               "gate_passed": fp == 0 and recall >= .95}
    output.write_text(json.dumps({"engine": engine, "matcher_version": MATCHER_VERSION, "profile": PROFILE, "split": manifest["split"],
                                  "summary": summary, "limits": manifest["limits"], "cases": results}, indent=2) + "\n")
    print(json.dumps(summary))
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["generate", "evaluate"])
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--split", choices=["calibration", "acceptance"], default="acceptance")
    parser.add_argument("--real-fixtures", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--engine", choices=["platform", "desktop"], default="platform")
    args = parser.parse_args()
    if args.mode == "generate":
        generated = generate(args.directory, args.split, args.real_fixtures)
        print(json.dumps({"split": args.split, "cases": len(generated["cases"]), "manifest": str(args.directory / "manifest.json")}))
    else:
        evaluate(args.directory, args.output or args.directory / "python-results.json", args.engine)
