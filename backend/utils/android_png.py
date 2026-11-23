"""One native PNG capture path for recording, previews and image execution.

uiautomator2.screenshot uses JPEG80 internally, even when its result is later
saved as PNG. Read screencap directly and never substitute a black image on an
ADB failure: an unavailable screenshot cannot establish image absence.
"""
from __future__ import annotations

import io
import time

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def capture_android_png(device, deadline=None) -> bytes:
    timeout = 15.0 if deadline is None else min(15.0, deadline - time.monotonic())
    if timeout <= 0:
        raise TimeoutError("screenshot_deadline")
    adb = device.adb_device
    raw = adb.shell(["screencap", "-p"], encoding=None, timeout=timeout)
    if not isinstance(raw, (bytes, bytearray)) or not bytes(raw).startswith(PNG_SIGNATURE):
        raise RuntimeError("Android 原生 PNG 截图失败，无法判定图像是否存在")
    raw = bytes(raw)
    from PIL import Image
    with Image.open(io.BytesIO(raw)) as image:
        image.verify()
    return raw
