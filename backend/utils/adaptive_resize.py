"""Deterministic half-pixel bilinear resize for adaptive matching.

The scalar specification is shared with Kotlin and Objective-C: clamp source
coordinates, interpolate unsigned channel values, and round half up once.
"""
from __future__ import annotations


def resize_bilinear(image, width: int, height: int):
    import numpy as np

    width, height = int(width), int(height)
    if width <= 0 or height <= 0:
        raise ValueError("invalid_resize_size")
    sh, sw = image.shape[:2]
    if (sw, sh) == (width, height):
        return image.copy()
    xs = np.clip((np.arange(width, dtype=np.float64) + .5) * sw / width - .5, 0, sw - 1)
    ys = np.clip((np.arange(height, dtype=np.float64) + .5) * sh / height - .5, 0, sh - 1)
    x0, y0 = np.floor(xs).astype(np.intp), np.floor(ys).astype(np.intp)
    x1, y1 = np.minimum(x0 + 1, sw - 1), np.minimum(y0 + 1, sh - 1)
    wx, wy = xs - x0, ys - y0
    if image.ndim == 3:
        wx, wy = wx[None, :, None], wy[:, None, None]
    else:
        wx, wy = wx[None, :], wy[:, None]
    top = image[y0[:, None], x0[None, :]].astype(np.float64) * (1 - wx) + image[y0[:, None], x1[None, :]] * wx
    bottom = image[y1[:, None], x0[None, :]].astype(np.float64) * (1 - wx) + image[y1[:, None], x1[None, :]] * wx
    return np.clip(np.floor(top * (1 - wy) + bottom * wy + .5), 0, 255).astype(np.uint8)
