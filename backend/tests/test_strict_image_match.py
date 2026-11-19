import base64
import hashlib
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import cv2
import numpy as np

from backend.drivers.android_driver import AndroidDriver
from backend.drivers.ios_driver import IOSDriver
from backend.execution_errors import ExecutionStepRuntimeError
from backend.utils.image_click import locate_stable_image
from backend.utils.image_match import match_image, stable_match
from backend.utils.image_match_options import normalize_image_options, select_image_options
from backend.utils.image_match_preview import preview_image_match
from backend.utils.image_tap import android_tap_once, ios_tap_once
from backend.utils.image_assert import assert_image_presence


def icon():
    image = np.full((40, 40, 3), 230, np.uint8)
    cv2.circle(image, (17, 17), 10, (30, 30, 30), 2, cv2.LINE_AA)
    cv2.line(image, (24, 24), (34, 34), (30, 30, 30), 3, cv2.LINE_AA)
    return image


def frame(template, x=30, y=40):
    image = np.full((160, 220, 3), 245, np.uint8)
    h, w = template.shape[:2]
    image[y:y + h, x:x + w] = template
    return image


class StrictImageMatchTests(unittest.TestCase):
    def test_actual_search_false_positives_and_check_in_regression(self):
        fixtures = Path(__file__).parent / "fixtures" / "image_click"
        for template, screen, status in [("search-black", "shield-region", "not_found"),
                                          ("search-white", "card-region", "not_found"),
                                          ("check-in", "check-in-region", "matched")]:
            with self.subTest(template=template):
                result = match_image(cv2.imread(str(fixtures / (screen + ".png"))), cv2.imread(str(fixtures / (template + ".png"))))
                self.assertEqual(result["status"], status)

    def test_absent_outline_never_matches_blank(self):
        result = match_image(np.full((160, 220, 3), 255, np.uint8), icon())
        self.assertEqual(result["status"], "not_found")
        self.assertIsNone(result["point"])

    def test_flat_and_tiny_templates_are_rejected(self):
        for template in (np.zeros((30, 30, 3), np.uint8), np.full((30, 30, 3), 255, np.uint8), icon()[:4, :4]):
            with self.subTest(shape=template.shape):
                self.assertEqual(match_image(frame(icon()), template)["status"], "invalid_template")

    def test_unique_target_and_noncentral_anchor(self):
        template = icon()
        config = {"template_sha256": "a" * 64, "tap_anchor": [0.1, 1.0], "source_size": [220, 160]}
        result = match_image(frame(template), template, config)
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["point"], [34, 79])

    def test_duplicate_target_requires_explicit_region(self):
        template = icon()
        image = frame(template)
        image[90:130, 150:190] = template
        self.assertEqual(match_image(image, template)["status"], "ambiguous")
        selected = match_image(image, template, {"template_sha256": "a" * 64, "search_region": [0, 0, 0.5, 1]})
        self.assertEqual(selected["status"], "matched")
        self.assertEqual(selected["point"], [50, 60])

    def test_similar_wrong_button_text_is_not_accepted(self):
        def button(text):
            result = np.full((60, 160, 3), 255, np.uint8)
            cv2.rectangle(result, (2, 2), (157, 57), (100, 100, 100), 2)
            cv2.putText(result, text, (30, 39), cv2.FONT_HERSHEY_SIMPLEX, .7, (10, 10, 10), 2, cv2.LINE_AA)
            return result
        for wanted, wrong in [("BUY", "PAY"), ("YES", "NO"), ("Delete", "Cancel"), ("Order 1", "Order 2")]:
            with self.subTest(wanted=wanted):
                self.assertNotEqual(match_image(frame(button(wrong)), button(wanted))["status"], "matched")

    def test_color_shape_mismatch_is_not_accepted(self):
        template = icon()
        template[template[:, :, 0] < 100] = (15, 15, 230)
        other = template.copy()
        other[template[:, :, 0] < 100] = (230, 15, 15)
        self.assertEqual(match_image(frame(other), template)["status"], "not_found")

    def test_changed_digit_is_not_diluted_by_shared_label_and_border(self):
        fixtures = Path(__file__).parent / "fixtures" / "image_click"
        one = cv2.imread(str(fixtures / "order-1-thin.png"))
        two = cv2.imread(str(fixtures / "order-2-thin.png"))
        for template, wrong in ((one, two), (two, one)):
            with self.subTest(template=hashlib.sha256(template.tobytes()).hexdigest()):
                self.assertEqual(match_image(frame(wrong), template)["status"], "not_found")
                self.assertEqual(match_image(frame(template), template)["status"], "matched")
                _, encoded = cv2.imencode(".jpg", template, [cv2.IMWRITE_JPEG_QUALITY, 95])
                compressed = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
                self.assertEqual(match_image(frame(compressed), template)["status"], "matched")

    def test_sparse_jpeg_recording_matches_lossless_frames(self):
        # Recording used full-frame JPEG q90 before cropping the template;
        # execution reads lossless PNG. Background pixels must not set the
        # contour threshold or compression ringing becomes foreground.
        for side in (60, 100, 160):
            for thickness in (1, 2, 3):
                with self.subTest(side=side, thickness=thickness):
                    screen = np.full((side + 80, side + 100, 3), 255, np.uint8)
                    center = np.array([30 + side // 2, 40 + side // 2])
                    points = center + np.array([[6, -12], [-6, 0], [6, 12]])
                    cv2.polylines(screen, [points], False, (20, 20, 20), thickness, cv2.LINE_AA)
                    _, encoded = cv2.imencode(".jpg", screen, [cv2.IMWRITE_JPEG_QUALITY, 90])
                    recorded = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
                    template = recorded[40:40 + side, 30:30 + side]
                    result = match_image(screen, template)
                    self.assertEqual(result["status"], "matched", result)
                    self.assertEqual(result["rect"], [30, 40, side, side])
                    self.assertGreaterEqual(result["candidates"][0]["metrics"]["edge_coverage"], .88)

    def test_sparse_jpeg_template_still_rejects_blank_changed_shape_and_duplicates(self):
        side = 100
        template = np.full((side, side, 3), 255, np.uint8)
        cv2.polylines(template, [np.array([[56, 38], [44, 50], [56, 62]])], False, (20, 20, 20), 1, cv2.LINE_AA)
        _, encoded = cv2.imencode(".jpg", template, [cv2.IMWRITE_JPEG_QUALITY, 90])
        compressed = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
        blank = np.full((240, 320, 3), 255, np.uint8)
        self.assertEqual(match_image(blank, compressed)["status"], "not_found")
        changed = np.flip(template, axis=1).copy()
        wrong = blank.copy()
        wrong[40:140, 30:130] = changed
        self.assertEqual(match_image(wrong, compressed)["status"], "not_found")
        duplicate = blank.copy()
        duplicate[20:120, 20:120] = template
        duplicate[130:230, 190:290] = template
        self.assertEqual(match_image(duplicate, compressed)["status"], "ambiguous")

    def test_size_change_uses_recorded_resolution(self):
        template = icon()
        scaled = cv2.resize(template, (60, 60))
        image = np.full((240, 330, 3), 245, np.uint8)
        image[60:120, 45:105] = scaled
        result = match_image(image, template, {"template_sha256": "a" * 64, "source_size": [220, 160]})
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["point"], [75, 90])

    def test_aspect_change_and_expired_deadline_do_not_match(self):
        template = icon()
        config = {"template_sha256": "a" * 64, "source_size": [220, 300]}
        self.assertEqual(match_image(frame(template), template, config)["status"], "unsupported")
        self.assertEqual(match_image(frame(template), template, deadline=time.monotonic() - 1)["status"], "unstable")

    def test_invalid_config_cannot_turn_into_center_click(self):
        for config in ({"tap_anchor": [0.1, 0.2]}, {"template_sha256": "a" * 64, "tap_anchor": [float("nan"), 0.5]},
                       {"template_sha256": "a" * 64, "search_region": None}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                normalize_image_options(config)
        for config in ({"ios": None}, {"ios": {}}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                select_image_options(config, "android")

    def test_preview_does_not_read_devices_and_rejects_hash_mismatch(self):
        _, raw = cv2.imencode(".png", icon())
        _, screenshot = cv2.imencode(".png", frame(icon()))
        result = preview_image_match(base64.b64encode(screenshot).decode(), raw.tobytes(), {"template_sha256": "0" * 64})
        self.assertEqual(result["reason"], "template_hash_mismatch")

    def test_stability_checks_local_target_and_resets_after_absence(self):
        matched = match_image(frame(icon()), icon())
        moved = match_image(frame(icon(), x=34), icon())
        self.assertTrue(stable_match(matched, matched))
        self.assertFalse(stable_match(matched, moved))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "icon.png"
            cv2.imwrite(str(path), icon())
            config = {"template_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            read = Mock(side_effect=[frame(icon()), np.full((160, 220, 3), 255, np.uint8), frame(icon()), frame(icon())])
            result = locate_stable_image(read, path, config, timeout=3)
            self.assertEqual(read.call_count, 4)
            self.assertEqual(result["stable_frames"], 2)

    def test_unstable_target_fails_without_tap(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "icon.png"
            cv2.imwrite(str(path), icon())
            def capture():
                return frame(icon(), x=30 if capture.count % 2 == 0 else 70)
            capture.count = 0
            def moving():
                capture.count += 1
                return capture()
            with self.assertRaises(ExecutionStepRuntimeError) as failure:
                locate_stable_image(moving, path, timeout=.4)
            self.assertEqual(failure.exception.code, "E2012_IMAGE_UNSTABLE")

    def test_both_drivers_refuse_ambiguous_evidence_without_any_tap(self):
        for cls, module in ((AndroidDriver, "backend.drivers.android_driver"), (IOSDriver, "backend.drivers.ios_driver")):
            driver = cls.__new__(cls)
            driver.device_id = "offline"
            driver._device = Mock()
            driver.client = Mock()
            driver._resolve_template_image_path = Mock(return_value="offline.png")
            failure = ExecutionStepRuntimeError("E2010_IMAGE_AMBIGUOUS", "ambiguous", context={"image_match": {"status": "ambiguous"}})
            with patch(module + ".locate_stable_image", side_effect=failure), self.assertRaises(ExecutionStepRuntimeError):
                driver.click_image("offline.png")
            driver._device.click.assert_not_called()
            driver.client.session.return_value.tap.assert_not_called()

    def test_ios_single_tap_preserves_retina_scale_and_anchor(self):
        driver = IOSDriver.__new__(IOSDriver)
        driver.device_id, driver.scale, driver.wda_url = "offline", 3, "http://127.0.0.1:8100"
        driver.client = Mock()
        driver.client.session.return_value.session_id = "session-id"
        driver._resolve_template_image_path = Mock(return_value="offline.png")
        diagnostic = {"point": [39, 78], "status": "matched"}
        with patch("backend.drivers.ios_driver.locate_stable_image", return_value=diagnostic), patch("backend.drivers.ios_driver.ios_tap_once") as tap:
            self.assertEqual(driver.click_image("offline.png"), {"image_match": diagnostic})
        self.assertEqual(tap.call_count, 1)
        self.assertEqual(tap.call_args.args[:4], (driver.wda_url, "session-id", 13, 26))
        driver.client.session.return_value.tap.assert_not_called()

    def test_cancel_during_localization_never_reaches_tap(self):
        import threading
        event = threading.Event()
        driver = AndroidDriver.__new__(AndroidDriver)
        driver.device_id, driver._device = "offline", Mock()
        driver._resolve_template_image_path = Mock(return_value="offline.png")
        def localized(*args, **kwargs):
            event.set()
            return {"point": [10, 20]}
        with patch("backend.drivers.android_driver.locate_stable_image", side_effect=localized), patch("backend.drivers.android_driver.android_tap_once") as tap:
            with self.assertRaisesRegex(RuntimeError, "执行已被用户中止"):
                driver.click_image("offline.png", abort_event=event)
        tap.assert_not_called()

    def test_android_low_level_submission_has_no_sdk_recovery(self):
        device = Mock()
        device._device_server_port = 9008
        transport = Mock()
        connection = Mock()
        connection.request.side_effect = RuntimeError("lost response")
        with patch("backend.utils.image_tap._adb_tunnel", return_value=transport), patch("http.client.HTTPConnection", return_value=connection):
            with self.assertRaises(RuntimeError):
                android_tap_once(device, 10, 20, time.monotonic() + 1)
        self.assertEqual(connection.request.call_count, 1)
        self.assertIn('"params": [10, 20]', connection.request.call_args.args[2])
        device.click.assert_not_called()
        device.jsonrpc_call.assert_not_called()

    def test_android_connection_that_exhausts_budget_never_sends_tap(self):
        device = Mock()
        device._device_server_port = 9008
        now = time.monotonic()
        def slow_connection(*args):
            time.sleep(.025)
            return Mock()
        with patch("backend.utils.image_tap._adb_tunnel", side_effect=slow_connection), patch("http.client.HTTPConnection") as http:
            with self.assertRaises(TimeoutError):
                android_tap_once(device, 10, 20, now + .01)
        http.assert_not_called()

    def test_android_adb_tunnel_submits_one_jsonrpc_over_real_local_socket(self):
        import json
        import socket
        import threading
        from types import SimpleNamespace
        observed = []
        errors = []
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        def handle():
            try:
                with server.accept()[0] as client:
                    def read_exact(size):
                        result = b""
                        while len(result) < size:
                            result += client.recv(size - len(result))
                        return result
                    for expected in (b"host:transport:offline-serial", b"tcp:9008"):
                        length = int(read_exact(4), 16)
                        self.assertEqual(read_exact(length), expected)
                        client.sendall(b"OKAY")
                    stream = client.makefile("rb")
                    self.assertEqual(stream.readline().split()[:2], [b"POST", b"/jsonrpc/0"])
                    headers = {}
                    while True:
                        line = stream.readline()
                        if line == b"\r\n":
                            break
                        key, value = line.decode().split(":", 1)
                        headers[key.lower()] = value.strip()
                    observed.append(json.loads(stream.read(int(headers["content-length"]))))
                    body = b'{"jsonrpc":"2.0","id":1,"result":true}'
                    client.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
            except Exception as exc:
                errors.append(exc)
            finally:
                server.close()
        worker = threading.Thread(target=handle, daemon=True)
        worker.start()
        adb = SimpleNamespace(serial="offline-serial", _client=SimpleNamespace(host="127.0.0.1", port=server.getsockname()[1]))
        android_tap_once(SimpleNamespace(adb_device=adb, _device_server_port=9008), 12.5, 18.8, time.monotonic() + 2)
        worker.join(timeout=2)
        self.assertFalse(errors)
        self.assertEqual(observed, [{"jsonrpc": "2.0", "id": 1, "method": "click", "params": [12, 18]}])

    def test_ios_low_level_submission_has_no_endpoint_retry(self):
        transport = Mock()
        transport.post.side_effect = RuntimeError("lost response")
        with patch("requests.Session") as session:
            session.return_value.__enter__.return_value = transport
            with self.assertRaises(RuntimeError):
                ios_tap_once("http://localhost:8100", "session-id", 10, 20, time.monotonic() + 1)
        self.assertEqual(transport.post.call_count, 1)
        self.assertTrue(transport.post.call_args.args[0].endswith("/session/session-id/wda/tap"))
        self.assertFalse(transport.post.call_args.kwargs["allow_redirects"])

    def test_assertion_duplicates_prove_presence_and_not_exists_checks_two_frames(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "icon.png"
            cv2.imwrite(str(path), icon())
            duplicate = frame(icon())
            duplicate[90:130, 150:190] = icon()
            read = Mock(return_value=duplicate)
            result = assert_image_presence(read, path)
            self.assertEqual(result["image_match"]["status"], "ambiguous")
            with self.assertRaisesRegex(AssertionError, "存在"):
                assert_image_presence(read, path, "not_exists")
            absent = Mock(return_value=np.full_like(duplicate, 255))
            self.assertEqual(assert_image_presence(absent, path, "not_exists")["image_match"]["status"], "not_found")
            self.assertEqual(absent.call_count, 2)

    def test_not_exists_never_treats_invalid_template_as_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "blank.png"
            cv2.imwrite(str(path), np.full((30, 30, 3), 255, np.uint8))
            with self.assertRaises(ExecutionStepRuntimeError) as failure:
                assert_image_presence(lambda: frame(icon()), path, "not_exists")
            self.assertEqual(failure.exception.code, "E2011_IMAGE_TEMPLATE_INVALID")


if __name__ == "__main__":
    unittest.main()
