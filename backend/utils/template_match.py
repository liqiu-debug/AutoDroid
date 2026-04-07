from __future__ import annotations

from typing import Any, Dict, Optional, Sequence, Tuple


DEFAULT_TEMPLATE_SCALES: Tuple[float, ...] = (1.0, 0.95, 1.05, 0.9, 1.1, 0.85, 1.15, 0.8, 1.2)


def _load_cv_stack() -> Tuple[Any, Any]:
    try:
        import cv2
        import numpy as np
    except Exception as exc:
        raise RuntimeError("图像匹配依赖缺失: 请安装 opencv-python 与 numpy") from exc
    return cv2, np


def image_to_bgr(image: Any, source: str = "image") -> Any:
    cv2, np = _load_cv_stack()

    if isinstance(image, np.ndarray):
        return image

    if isinstance(image, (bytes, bytearray)):
        arr = np.frombuffer(image, dtype=np.uint8)
        decoded = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if decoded is None:
            raise RuntimeError(f"{source} 图片解码失败")
        return decoded

    try:
        from PIL import Image
    except Exception:
        Image = None

    if Image is not None and isinstance(image, Image.Image):
        return cv2.cvtColor(np.array(image.convert("RGB")), cv2.COLOR_RGB2BGR)

    raise RuntimeError(f"{source} 格式不支持: {type(image)}")


def load_image_bgr(image_path: str) -> Any:
    cv2, _ = _load_cv_stack()
    image = cv2.imread(image_path, cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError(f"图片解码失败: {image_path}")
    return image


def find_template_match(
    screen_bgr: Any,
    template_bgr: Any,
    threshold: float = 0.95,
    scales: Optional[Sequence[float]] = None,
    ssim_threshold: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    cv2, _ = _load_cv_stack()
    from skimage.metrics import structural_similarity

    screen_gray = cv2.cvtColor(screen_bgr, cv2.COLOR_BGR2GRAY)
    template_gray_raw = cv2.cvtColor(template_bgr, cv2.COLOR_BGR2GRAY)

    sh, sw = screen_gray.shape[:2]
    th_raw, tw_raw = template_gray_raw.shape[:2]
    if th_raw <= 0 or tw_raw <= 0:
        raise RuntimeError("模板图尺寸无效")

    best_score = -1.0
    best_loc = (0, 0)
    best_size = (tw_raw, th_raw)
    best_template_gray = template_gray_raw
    for scale in scales or DEFAULT_TEMPLATE_SCALES:
        tw = max(1, int(round(tw_raw * scale)))
        th = max(1, int(round(th_raw * scale)))
        if tw > sw or th > sh:
            continue

        if scale == 1.0:
            template_gray = template_gray_raw
        else:
            template_gray = cv2.resize(
                template_gray_raw,
                (tw, th),
                interpolation=cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR,
            )

        result = cv2.matchTemplate(screen_gray, template_gray, cv2.TM_CCOEFF_NORMED)
        _, score, _, loc = cv2.minMaxLoc(result)
        if score > best_score:
            best_score = float(score)
            best_loc = (int(loc[0]), int(loc[1]))
            best_size = (tw, th)
            best_template_gray = template_gray

    if best_score < float(threshold):
        return None

    bx, by = best_loc
    bw, bh = best_size
    center_x = float(bx + bw / 2)
    center_y = float(by + bh / 2)

    ssim_score = None
    if ssim_threshold is not None:
        crop = screen_gray[by:by + bh, bx:bx + bw]
        if crop.shape[:2] != best_template_gray.shape[:2]:
            return None
        ssim_score = float(structural_similarity(crop, best_template_gray))
        if ssim_score < float(ssim_threshold):
            return None

    return {
        "similarity": best_score,
        "ssim": ssim_score,
        "point": [center_x, center_y],
        "size": [bw, bh],
    }
