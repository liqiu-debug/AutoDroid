import os
import tempfile
import unittest
from unittest.mock import patch

from backend.api.fastbot import _upgrade_replay_payload_to_mp4


class FastbotReplayUpgradeTests(unittest.TestCase):
    def test_upgrade_updates_h264_metadata_to_mp4(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch(
            "backend.api.fastbot.FASTBOT_REPORTS_DIR",
            tmpdir,
        ), patch(
            "backend.api.fastbot.transcode_h264_to_mp4",
            side_effect=lambda source_path, output_path: open(output_path, "wb").write(b"mp4"),
        ):
            replay_dir = os.path.join(tmpdir, "1", "replays")
            os.makedirs(replay_dir, exist_ok=True)
            source_path = os.path.join(replay_dir, "crash_100000_01.h264")
            with open(source_path, "wb") as handle:
                handle.write(b"h264")

            replay = {
                "status": "READY",
                "filename": "crash_100000_01.h264",
                "path": "reports/fastbot/1/replays/crash_100000_01.h264",
            }

            changed = _upgrade_replay_payload_to_mp4(1, replay)

            self.assertTrue(changed)
            self.assertEqual(replay["status"], "READY")
            self.assertEqual(replay["filename"], "crash_100000_01.mp4")
            self.assertEqual(replay["path"], "reports/fastbot/1/replays/crash_100000_01.mp4")
            self.assertEqual(replay["error"], "")
            self.assertTrue(os.path.isfile(os.path.join(replay_dir, "crash_100000_01.mp4")))

    def test_upgrade_marks_replay_failed_when_source_file_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch(
            "backend.api.fastbot.FASTBOT_REPORTS_DIR",
            tmpdir,
        ):
            replay = {
                "status": "READY",
                "filename": "missing_01.h264",
                "path": "reports/fastbot/1/replays/missing_01.h264",
            }

            changed = _upgrade_replay_payload_to_mp4(1, replay)

            self.assertTrue(changed)
            self.assertEqual(replay["status"], "FAILED")
            self.assertEqual(replay["filename"], "")
            self.assertEqual(replay["path"], "")
            self.assertIn("MP4", replay["error"])


if __name__ == "__main__":
    unittest.main()
