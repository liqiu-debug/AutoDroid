import io
import unittest

from PIL import Image

from backend.api.devices import (
    DEVICE_PREVIEW_JPEG_QUALITY,
    DEVICE_PREVIEW_MAX_SIDE,
    _encode_preview_image,
)


def _png_bytes(width: int, height: int) -> bytes:
    buffered = io.BytesIO()
    Image.new("RGB", (width, height), (30, 120, 200)).save(buffered, format="PNG")
    return buffered.getvalue()


class EncodePreviewImageTests(unittest.TestCase):
    def test_large_screenshot_downscaled_to_preview_jpeg(self):
        raw = _png_bytes(1080, 2340)

        preview, image_format = _encode_preview_image(raw)

        self.assertEqual(image_format, "jpeg")
        decoded = Image.open(io.BytesIO(preview))
        self.assertEqual(decoded.format, "JPEG")
        self.assertEqual(max(decoded.size), DEVICE_PREVIEW_MAX_SIDE)
        # 宽高比保持（1080x2340 → 长边 1280）
        self.assertEqual(decoded.size, (590, 1280))
        self.assertLess(len(preview), len(raw))

    def test_small_screenshot_not_upscaled(self):
        raw = _png_bytes(600, 800)

        preview, image_format = _encode_preview_image(raw)

        self.assertEqual(image_format, "jpeg")
        decoded = Image.open(io.BytesIO(preview))
        self.assertEqual(decoded.size, (600, 800))

    def test_undecodable_bytes_pass_through_with_sniffed_format(self):
        raw_png_like = b"\x89PNG\r\n\x1a\nnot-really-a-png"
        preview, image_format = _encode_preview_image(raw_png_like)
        self.assertEqual(preview, raw_png_like)
        self.assertEqual(image_format, "png")

        raw_jpeg_like = b"\xff\xd8\xff\xe0not-really-a-jpeg"
        preview, image_format = _encode_preview_image(raw_jpeg_like)
        self.assertEqual(preview, raw_jpeg_like)
        self.assertEqual(image_format, "jpeg")

    def test_quality_constant_is_sane(self):
        self.assertTrue(1 <= DEVICE_PREVIEW_JPEG_QUALITY <= 95)


if __name__ == "__main__":
    unittest.main()
