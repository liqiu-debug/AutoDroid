"""HTTP contract tests for crop metadata and the device-free match preview."""
import base64
import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import cv2
import numpy as np
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from backend.api import recording
from backend.utils.image_click import locate_stable_image


def png_bytes(image):
    ok, encoded = cv2.imencode(".png", image)
    if not ok:
        raise AssertionError("fixture PNG encoding failed")
    return encoded.tobytes()


def screenshot_fixture(duplicate=False):
    template = np.full((40, 40, 3), 230, np.uint8)
    cv2.circle(template, (17, 17), 10, (30, 30, 30), 2, cv2.LINE_AA)
    cv2.line(template, (24, 24), (34, 34), (30, 30, 30), 3, cv2.LINE_AA)
    screen = np.full((160, 220, 3), 245, np.uint8)
    screen[40:80, 30:70] = template
    if duplicate:
        screen[90:130, 150:190] = template
    return screen


class ImageMatchPreviewApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.project = Path(self.directory.name)
        self.image_root = self.project / "static" / "images"
        self.image_root.mkdir(parents=True)
        # Keep crop files and all path checks inside a disposable project root.
        self.mock_module = self.project / "backend" / "api" / "recording.py"
        self.mock_module.parent.mkdir(parents=True)
        module_path = patch.object(recording, "__file__", str(self.mock_module))
        module_path.start()
        self.addCleanup(module_path.stop)
        self.guards = []
        for target in ("_connect_recording_device", "_perform_device_operation", "CrossPlatformRunner", "IOSDriver"):
            guard = patch.object(recording, target, side_effect=AssertionError("preview must not acquire/click a device"))
            self.guards.append(guard.start())
            self.addCleanup(guard.stop)
        for pool in (recording._recording_android_device_pool, recording._recording_ios_session_pool):
            guard = patch.object(pool, "acquire", side_effect=AssertionError("preview must not acquire a device"))
            self.guards.append(guard.start())
            self.addCleanup(guard.stop)
        self.app = FastAPI()
        self.app.include_router(recording.router)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    def tearDown(self):
        for guard in self.guards:
            guard.assert_not_called()

    @staticmethod
    def encoded(screen):
        return base64.b64encode(png_bytes(screen)).decode("ascii")

    def crop(self, screen=None):
        response = self.client.post("/api/device/crop_template", json={
            "screenshot_base64": self.encoded(screen if screen is not None else screenshot_fixture()),
            "x1": 30, "y1": 40, "x2": 70, "y2": 80,
        })
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def preview(self, metadata, screen=None, config=None, **extra):
        body = {"screenshot_base64": self.encoded(screen if screen is not None else screenshot_fixture()),
                "image_path": metadata["image_path"], **extra}
        if config is not None:
            body["image_options"] = config
        return self.client.post("/api/device/match_image", json=body)

    def config(self, metadata):
        return {"template_sha256": metadata["template_sha256"], "source_size": metadata["source_size"], "tap_anchor": [0.1, 0.75]}

    def assert_diagnostic(self, payload):
        self.assertTrue({"status", "reason", "matcher_version", "candidates", "point", "quality", "timings"}.issubset(payload))
        self.assertTrue(payload["matcher_version"])
        for candidate in payload["candidates"]:
            self.assertEqual(len(candidate["rect"]), 4)
            self.assertEqual(len(candidate["point"]), 2)
            self.assertIn("similarity", candidate)
            self.assertIsInstance(candidate["metrics"], dict)
            self.assertIsInstance(candidate["accepted"], bool)

    def test_crop_metadata_matches_persisted_png_and_source_dimensions(self):
        metadata = self.crop()
        self.assertRegex(metadata["image_path"], r"^static/images/element_[a-f0-9]+\.png$")
        raw = (self.project / metadata["image_path"]).read_bytes()
        self.assertEqual(metadata["template_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(metadata["source_size"], [220, 160])
        self.assertEqual(metadata["quality"]["size"], [40, 40])
        self.assertTrue(metadata["quality"]["valid"])
        self.assertIsInstance(metadata["quality"]["warnings"], list)

    def test_preview_matches_noncentral_anchor_and_legacy_path_without_config(self):
        metadata = self.crop()
        response = self.preview(metadata, config=self.config(metadata))
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assert_diagnostic(payload)
        self.assertEqual(payload["status"], "matched")
        self.assertEqual(payload["point"], [34, 70])
        legacy = self.preview(metadata)
        self.assertEqual(legacy.json()["status"], "matched")
        self.assertEqual(legacy.json()["point"], [50, 60])

    def test_jpeg_recording_frame_uses_original_pixels_and_legacy_route_alias(self):
        ok, jpeg = cv2.imencode(".jpg", screenshot_fixture(), [cv2.IMWRITE_JPEG_QUALITY, 90])
        self.assertTrue(ok)
        screenshot = base64.b64encode(jpeg).decode("ascii")
        response = self.client.post("/device/crop_template", json={
            "screenshot_base64": screenshot, "x1": 30, "y1": 40, "x2": 70, "y2": 80,
        })
        self.assertEqual(response.status_code, 200, response.text)
        metadata = response.json()
        self.assertEqual(metadata["source_size"], [220, 160])
        response = self.client.post("/device/match_image", json={
            "screenshot_base64": screenshot, "image_path": metadata["image_path"], "image_options": self.config(metadata),
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "matched")
        self.assertEqual(response.json()["point"], [34, 70])

    def test_lossless_recording_crop_preview_and_execution_use_same_arrow_pixels(self):
        screen = np.full((160, 220, 3), 255, np.uint8)
        cv2.polylines(screen, [np.array([[54, 49], [46, 58], [54, 67]])], False,
                      (55, 55, 55), 1, cv2.LINE_AA)
        source = Image.fromarray(cv2.cvtColor(screen, cv2.COLOR_BGR2RGB))
        screenshot, image_format = recording._take_screenshot_payload(Mock(screenshot=Mock(return_value=source)))
        self.assertEqual(image_format, "png")
        recorded = Image.open(io.BytesIO(base64.b64decode(screenshot)))
        self.assertEqual(recorded.tobytes(), source.tobytes())
        response = self.client.post("/api/device/crop_template", json={
            "screenshot_base64": screenshot, "x1": 30, "y1": 40, "x2": 70, "y2": 80,
        })
        self.assertEqual(response.status_code, 200, response.text)
        metadata = response.json()
        path = self.project / metadata["image_path"]
        self.assertEqual(Image.open(path).tobytes(), source.crop((30, 40, 70, 80)).tobytes())
        # Preview and execution read independently encoded device PNG frames.
        execution_frame = png_bytes(screen)
        response = self.client.post("/api/device/match_image", json={
            "screenshot_base64": base64.b64encode(execution_frame).decode("ascii"),
            "image_path": metadata["image_path"], "image_options": self.config(metadata),
        })
        self.assertEqual(response.status_code, 200, response.text)
        preview = response.json()
        self.assertEqual(preview["status"], "matched")
        execution = locate_stable_image(lambda: execution_frame, path, self.config(metadata), timeout=2)
        self.assertEqual(execution["point"], preview["point"])
        self.assertEqual(execution["stable_frames"], 2)

    def test_preview_rejects_duplicate_targets_until_region_is_explicit(self):
        metadata = self.crop()
        image = screenshot_fixture(duplicate=True)
        response = self.preview(metadata, image, self.config(metadata))
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assert_diagnostic(payload)
        self.assertEqual(payload["status"], "ambiguous")
        self.assertIsNone(payload["point"])
        self.assertGreaterEqual(sum(c["accepted"] for c in payload["candidates"]), 2)
        config = {**self.config(metadata), "search_region": [0, 0, .5, 1]}
        restricted = self.preview(metadata, image, config)
        self.assertEqual(restricted.json()["status"], "matched")
        self.assertEqual(restricted.json()["point"], [34, 70])

    def test_template_hash_mismatch_returns_diagnostic_and_no_point(self):
        metadata = self.crop()
        response = self.preview(metadata, config={**self.config(metadata), "template_sha256": "0" * 64})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assert_diagnostic(payload)
        self.assertEqual((payload["status"], payload["reason"]), ("invalid_template", "template_hash_mismatch"))
        self.assertIsNone(payload["point"])

    def test_flat_template_quality_is_returned_and_preview_refuses_it(self):
        metadata = self.crop(np.full((160, 220, 3), 255, np.uint8))
        self.assertFalse(metadata["quality"]["valid"])
        response = self.preview(metadata, config=self.config(metadata))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "invalid_template")
        self.assertIsNone(response.json()["point"])

    def test_invalid_configuration_and_bad_screenshot_are_explicit_http_errors(self):
        metadata = self.crop()
        for config in ({"tap_anchor": [.1, .2]}, {**self.config(metadata), "tap_anchor": [-1, .5]},
                       {**self.config(metadata), "source_size": [0, 160]},
                       {**self.config(metadata), "search_region": [0, 0, 0, 1]},
                       {"android": self.config(metadata)}):
            with self.subTest(config=config):
                response = self.preview(metadata, config=config)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertIn("detail", response.json())
        for screenshot in ("not-base64", "data:image/png;base64," + self.encoded(screenshot_fixture())):
            with self.subTest(screenshot=screenshot[:24]):
                response = self.preview(metadata, screenshot_base64=screenshot)
                self.assertEqual(response.status_code, 400, response.text)
        # Frontend strips data URLs before posting. The raw base64 accepted above is its contract.

    def test_preview_limits_paths_to_template_root_including_symlinks(self):
        metadata = self.crop()
        outside = self.project / "outside.png"
        outside.write_bytes((self.project / metadata["image_path"]).read_bytes())
        (self.image_root / "escape.png").symlink_to(outside)
        for path in ("../outside.png", "static/images/../../outside.png", str(outside),
                     "https://example.invalid/template.png", "static/images/missing.png", "static/images/escape.png"):
            with self.subTest(path=path):
                response = self.preview({"image_path": path})
                self.assertEqual(response.status_code, 400, response.text)


if __name__ == "__main__":
    unittest.main()
