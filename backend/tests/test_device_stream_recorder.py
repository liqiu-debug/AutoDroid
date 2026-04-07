import os
import tempfile
import unittest
from unittest.mock import patch

from backend.device_stream.recorder import RollingScrcpyRecorderSession


class RollingScrcpyRecorderSessionTests(unittest.TestCase):
    def setUp(self):
        self._mono = 1000.0

    def _fake_monotonic(self):
        return self._mono

    def _fake_sleep(self, seconds):
        self._mono += seconds

    def _advance(self, seconds):
        self._mono += seconds

    def test_segment_rotation_drops_oldest_buffer_file(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch(
            "backend.device_stream.recorder.time.monotonic",
            side_effect=self._fake_monotonic,
        ), patch(
            "backend.device_stream.recorder.time.sleep",
            side_effect=self._fake_sleep,
        ):
            recorder = RollingScrcpyRecorderSession(
                serial="device-1",
                task_id=1,
                report_dir=tmpdir,
                project_root=tmpdir,
                pre_roll_sec=2,
                post_roll_sec=0,
                segment_sec=1,
            )

            packets = [
                b"\x00\x00\x00\x01\x65\x88\x84\x01",
                b"\x00\x00\x00\x01\x41\x9a\x22\x02",
                b"\x00\x00\x00\x01\x41\x9a\x22\x03",
                b"\x00\x00\x00\x01\x41\x9a\x22\x04",
            ]
            for index, packet in enumerate(packets):
                if index > 0:
                    self._advance(1.1)
                recorder.ingest(packet)

            self.assertEqual([segment.sequence for segment in recorder._segments], [2, 3, 4])
            self.assertFalse(os.path.exists(os.path.join(recorder.buffer_dir, "segment_000001.h264")))

            recorder.stop(cleanup_buffer=True)

    def test_capture_replay_exports_recent_window_with_sps_pps(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch(
            "backend.device_stream.recorder.time.monotonic",
            side_effect=self._fake_monotonic,
        ), patch(
            "backend.device_stream.recorder.time.sleep",
            side_effect=self._fake_sleep,
        ), patch(
            "backend.device_stream.recorder.transcode_h264_to_mp4",
            side_effect=lambda source_path, output_path: os.replace(source_path, output_path),
        ):
            recorder = RollingScrcpyRecorderSession(
                serial="device-2",
                task_id=2,
                report_dir=tmpdir,
                project_root=tmpdir,
                pre_roll_sec=2,
                post_roll_sec=0,
                segment_sec=1,
            )

            sps = b"\x00\x00\x00\x01\x67\x64\x00\x1f"
            pps = b"\x00\x00\x00\x01\x68\xee\x3c\x80"
            expired_idr = b"\x00\x00\x00\x01\x65\xaa\xbb\x01"
            recent_p = b"\x00\x00\x00\x01\x41\x9a\x22\x02"
            newest_p = b"\x00\x00\x00\x01\x41\x9a\x22\x03"

            recorder.ingest(sps)
            recorder.ingest(pps)
            recorder.ingest(expired_idr)
            self._advance(1.1)
            recorder.ingest(recent_p)
            self._advance(1.1)
            recorder.ingest(newest_p)

            result = recorder.capture_replay("CRASH", "10:00:00")

            self.assertEqual(result.status, "READY")
            self.assertTrue(result.filename.endswith(".mp4"))
            self.assertEqual(result.pre_roll_sec, 2)
            self.assertEqual(result.post_roll_sec, 0)
            self.assertGreaterEqual(result.duration_sec, 1)

            replay_path = os.path.join(tmpdir, result.path)
            self.assertTrue(os.path.isfile(replay_path))

            with open(replay_path, "rb") as handle:
                content = handle.read()

            self.assertIn(sps, content)
            self.assertIn(pps, content)
            self.assertIn(expired_idr, content)
            self.assertIn(recent_p, content)
            self.assertIn(newest_p, content)

            recorder.stop(cleanup_buffer=True)

    def test_seed_init_packets_prepends_decoder_headers_for_mid_stream_recording(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch(
            "backend.device_stream.recorder.time.monotonic",
            side_effect=self._fake_monotonic,
        ), patch(
            "backend.device_stream.recorder.time.sleep",
            side_effect=self._fake_sleep,
        ), patch(
            "backend.device_stream.recorder.transcode_h264_to_mp4",
            side_effect=lambda source_path, output_path: os.replace(source_path, output_path),
        ):
            recorder = RollingScrcpyRecorderSession(
                serial="device-3",
                task_id=3,
                report_dir=tmpdir,
                project_root=tmpdir,
                pre_roll_sec=2,
                post_roll_sec=0,
                segment_sec=1,
            )

            sps = b"\x00\x00\x00\x01\x67\x64\x00\x1f"
            pps = b"\x00\x00\x00\x01\x68\xee\x3c\x80"
            cached_idr = b"\x00\x00\x00\x01\x65\x88\x84\x01"
            recent_p = b"\x00\x00\x00\x01\x41\x9a\x22\x02"

            recorder.seed_init_packets([sps, pps, cached_idr])
            recorder.ingest(recent_p)

            result = recorder.capture_replay("CRASH", "10:00:00")
            replay_path = os.path.join(tmpdir, result.path)
            with open(replay_path, "rb") as handle:
                content = handle.read()

            self.assertTrue(content.startswith(sps + pps + cached_idr))
            self.assertIn(recent_p, content)

            recorder.stop(cleanup_buffer=True)

    def test_capture_replay_rotates_active_segment_before_export(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch(
            "backend.device_stream.recorder.time.monotonic",
            side_effect=self._fake_monotonic,
        ), patch(
            "backend.device_stream.recorder.time.sleep",
            side_effect=self._fake_sleep,
        ), patch(
            "backend.device_stream.recorder.transcode_h264_to_mp4",
            side_effect=lambda source_path, output_path: os.replace(source_path, output_path),
        ):
            recorder = RollingScrcpyRecorderSession(
                serial="device-4",
                task_id=4,
                report_dir=tmpdir,
                project_root=tmpdir,
                pre_roll_sec=2,
                post_roll_sec=0,
                segment_sec=5,
            )

            recorder.ingest(b"\x00\x00\x00\x01\x67\x64\x00\x1f")
            recorder.ingest(b"\x00\x00\x00\x01\x68\xee\x3c\x80")
            recorder.ingest(b"\x00\x00\x00\x01\x65\x88\x84\x01")
            current_sequence = recorder._current_segment.sequence

            result = recorder.capture_replay("CRASH", "10:00:00")

            self.assertEqual(result.status, "READY")
            self.assertIsNotNone(recorder._current_segment)
            self.assertGreater(recorder._current_segment.sequence, current_sequence)
            self.assertFalse(recorder._current_segment.has_data)

            recorder.stop(cleanup_buffer=True)


if __name__ == "__main__":
    unittest.main()
