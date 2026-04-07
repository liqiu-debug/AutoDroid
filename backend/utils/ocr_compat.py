from __future__ import annotations

import logging
import os
import warnings
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_NOISY_OCR_LOGGERS = (
    "paddle",
    "paddleocr",
    "ppocr",
)


def suppress_ocr_runtime_noise() -> None:
    """Reduce known third-party OCR warnings/logs that do not affect runtime behavior."""
    warnings.filterwarnings(
        "ignore",
        message=r".*No ccache found.*",
        category=UserWarning,
        module=r"paddle\.utils\.cpp_extension\.extension_utils",
    )
    for logger_name in _NOISY_OCR_LOGGERS:
        logging.getLogger(logger_name).setLevel(logging.ERROR)


def create_paddle_ocr_engine(*, use_angle_cls: bool = False, lang: str = "ch") -> Any:
    suppress_ocr_runtime_noise()
    # Avoid intermittent startup failures caused by external model-source probing.
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    from paddleocr import PaddleOCR

    candidates = [
        {"use_angle_cls": use_angle_cls, "lang": lang, "show_log": False},
        {"use_angle_cls": use_angle_cls, "lang": lang},
        {"lang": lang, "show_log": False},
        {"lang": lang},
    ]
    last_exc: Optional[Exception] = None
    for kwargs in candidates:
        try:
            return PaddleOCR(**kwargs)
        except Exception as exc:
            text = str(exc or "").lower()
            if ("unknown argument" in text) or ("unexpected keyword argument" in text):
                last_exc = exc
                continue
            raise
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("PaddleOCR 初始化失败: 未命中可用参数组合")


def _is_cls_kwarg_error(exc: Exception) -> bool:
    text = str(exc or "").lower()
    return ("unexpected keyword argument" in text) and ("cls" in text)


def _normalize_box_points(box: Any) -> Optional[List[Tuple[float, float]]]:
    if box is None:
        return None
    if hasattr(box, "tolist"):
        try:
            box = box.tolist()
        except Exception:
            pass
    if not isinstance(box, (list, tuple)):
        return None

    points: List[Tuple[float, float]] = []
    for point in box:
        if hasattr(point, "tolist"):
            try:
                point = point.tolist()
            except Exception:
                pass
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            continue
        try:
            points.append((float(point[0]), float(point[1])))
        except Exception:
            continue
    return points or None


def _append_ocr_item(items: List[Dict[str, Any]], text: Any, score: Any = None, box: Any = None) -> None:
    text_value = str(text or "").strip()
    if not text_value:
        return
    parsed_score = None
    if score is not None:
        try:
            parsed_score = float(score)
        except Exception:
            parsed_score = None
    items.append(
        {
            "text": text_value,
            "score": parsed_score,
            "box": _normalize_box_points(box),
        }
    )


def run_paddle_ocr(engine: Any, image: Any, use_cls: bool = False) -> Any:
    """
    Run OCR with cross-version compatibility.

    Compatibility strategy:
    1) engine.ocr(image, cls=...)
    2) engine.ocr(image)
    3) engine.predict(image, cls=...)
    4) engine.predict(image)
    """
    if engine is None:
        raise RuntimeError("PaddleOCR engine is not initialized")

    ocr_fn = getattr(engine, "ocr", None)
    if callable(ocr_fn):
        try:
            return ocr_fn(image, cls=bool(use_cls))
        except Exception as exc:
            if _is_cls_kwarg_error(exc):
                logger.debug(
                    "PaddleOCR compatibility fallback: ocr(..., cls=%s) failed: %s",
                    bool(use_cls),
                    exc,
                )
            else:
                raise

        try:
            return ocr_fn(image)
        except Exception as exc:
            if _is_cls_kwarg_error(exc):
                logger.debug(
                    "PaddleOCR compatibility fallback: ocr(...) still failed with cls mismatch: %s",
                    exc,
                )
            else:
                raise

    predict_fn = getattr(engine, "predict", None)
    if not callable(predict_fn):
        raise RuntimeError("PaddleOCR 调用失败: 当前版本不支持兼容调用链")

    try:
        return predict_fn(image, cls=bool(use_cls))
    except Exception as exc:
        if _is_cls_kwarg_error(exc):
            logger.debug(
                "PaddleOCR compatibility fallback: predict(..., cls=%s) failed: %s",
                bool(use_cls),
                exc,
            )
        else:
            raise

    return predict_fn(image)


def iter_ocr_text_items(ocr_result: Any) -> List[Dict[str, Any]]:
    """
    Normalize OCR outputs into a list of:
    - text: str
    - score: Optional[float]
    - box: Optional[List[(x, y)]]
    """
    items: List[Dict[str, Any]] = []

    def _walk(node: Any) -> None:
        if node is None:
            return

        if not isinstance(node, (dict, list, tuple, str, bytes)) and hasattr(node, "res"):
            try:
                _walk(getattr(node, "res"))
                return
            except Exception:
                return

        if isinstance(node, dict):
            texts = node.get("rec_texts")
            if isinstance(texts, (list, tuple)):
                polys = node.get("dt_polys") or node.get("boxes") or node.get("rec_boxes")
                scores = node.get("rec_scores") or node.get("scores")
                for idx, text in enumerate(texts):
                    box = None
                    score = None
                    if isinstance(polys, (list, tuple)) and idx < len(polys):
                        box = polys[idx]
                    if isinstance(scores, (list, tuple)) and idx < len(scores):
                        score = scores[idx]
                    _append_ocr_item(items, text=text, score=score, box=box)

            if "text" in node:
                _append_ocr_item(
                    items,
                    text=node.get("text"),
                    score=node.get("score"),
                    box=node.get("box") or node.get("bbox") or node.get("points"),
                )

            for key in ("res", "result", "results", "data", "ocr_res", "ocr_result", "items"):
                if key in node:
                    _walk(node.get(key))
            return

        if isinstance(node, (list, tuple)):
            # Legacy PaddleOCR line format: [box, [text, score]]
            if len(node) >= 2 and isinstance(node[1], (list, tuple)) and len(node[1]) >= 1:
                maybe_text = node[1][0]
                maybe_score = node[1][1] if len(node[1]) >= 2 else None
                _append_ocr_item(items, text=maybe_text, score=maybe_score, box=node[0])
            for item in node:
                _walk(item)
            return

    _walk(ocr_result)

    unique: List[Dict[str, Any]] = []
    seen = set()
    for item in items:
        box = item.get("box") or []
        box_key = tuple((round(pt[0], 4), round(pt[1], 4)) for pt in box)
        key = (item.get("text"), item.get("score"), box_key)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def extract_ocr_text(ocr_result: Any, separator: str = "\n") -> str:
    texts: List[str] = []
    for item in iter_ocr_text_items(ocr_result):
        text = str(item.get("text") or "").strip()
        if text and text not in texts:
            texts.append(text)
    return separator.join(texts).strip()
