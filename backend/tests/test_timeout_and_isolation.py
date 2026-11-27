"""超时控制与异步事件循环隔离回归测试。"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from backend.api import compatibility, devices
from backend.fastbot import adb as fastbot_adb


class _HangingProcess:
    def __init__(self) -> None:
        self.returncode = None
        self.terminated = False
        self.killed = False

    async def communicate(self):
        await asyncio.sleep(60)
        return b"", b""

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        return self.returncode


class FastbotAdbTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_adb_push_times_out_and_terminates_process(self):
        fake_proc = _HangingProcess()
        with patch(
            "backend.fastbot.adb.asyncio.create_subprocess_shell",
            return_value=fake_proc,
        ):
            with self.assertRaises(TimeoutError):
                await fastbot_adb._adb_push("serial-1", "/tmp/a", "/sdcard/a", timeout=0.05)
        self.assertTrue(fake_proc.terminated)

    async def test_adb_pull_times_out_and_terminates_process(self):
        fake_proc = _HangingProcess()
        with patch(
            "backend.fastbot.adb.asyncio.create_subprocess_shell",
            return_value=fake_proc,
        ):
            with self.assertRaises(TimeoutError):
                await fastbot_adb._adb_pull("serial-1", "/sdcard/a", "/tmp/a", timeout=0.05)
        self.assertTrue(fake_proc.terminated)

    async def test_adb_push_and_pull_terminate_real_hung_subprocess(self):
        hang_cmd = f"{sys.executable} -c 'import time; time.sleep(30)'"
        with patch.object(
            fastbot_adb.asyncio,
            "create_subprocess_shell",
            wraps=fastbot_adb.asyncio.create_subprocess_shell,
        ):
            proc = await asyncio.create_subprocess_shell(
                hang_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            with patch(
                "backend.fastbot.adb.asyncio.create_subprocess_shell",
                return_value=proc,
            ):
                with self.assertRaises(TimeoutError):
                    await fastbot_adb._adb_pull("serial-1", "/sdcard/x", "/tmp/x", timeout=0.1)
            self.assertIsNotNone(proc.returncode)


class DevicesSubprocessTimeoutTests(unittest.TestCase):
    def test_discover_ios_wda_bundle_id_passes_timeout_and_handles_expiry(self):
        with patch(
            "backend.api.devices.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["tidevice"], timeout=15),
        ) as run_mock:
            result = devices._discover_ios_wda_bundle_id("ios-udid-1")
        self.assertIsNone(result)
        self.assertEqual(run_mock.call_args.kwargs.get("timeout"), 15)

    def test_find_tidevice_pids_and_is_process_alive_pass_timeout(self):
        with patch(
            "backend.api.devices.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["ps"], timeout=5),
        ) as run_mock:
            self.assertEqual(devices._find_tidevice_pids_for_udid("ios-udid-1"), [])
            self.assertEqual(run_mock.call_args.kwargs.get("timeout"), 5)

        with patch("backend.api.devices.os.kill", return_value=None), patch(
            "backend.api.devices.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["ps"], timeout=5),
        ) as run_mock:
            self.assertTrue(devices._is_process_alive(12345))
            self.assertEqual(run_mock.call_args.kwargs.get("timeout"), 5)


class CompatibilityCaptureIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_capture_snapshot_runs_blocking_u2_off_event_loop(self):
        class _SlowU2Device:
            def screenshot(self, format="pillow"):
                time.sleep(0.25)
                return Image.new("RGB", (8, 8), color=(10, 20, 30))

            def dump_hierarchy(self):
                return "<hierarchy/>"

        fake_u2 = types.SimpleNamespace(connect=lambda serial: _SlowU2Device())
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(sys.modules, {"uiautomator2": fake_u2}), patch.object(
                compatibility,
                "project_path",
                side_effect=lambda *parts: Path(tmpdir).joinpath(*parts),
            ), patch.object(
                compatibility, "_capture_activity", return_value="MainActivity"
            ), patch.object(
                compatibility, "_capture_logcat_errors", return_value=""
            ):
                ticks = 0

                async def _ticker():
                    nonlocal ticks
                    for _ in range(5):
                        await asyncio.sleep(0.03)
                        ticks += 1

                ticker_task = asyncio.create_task(_ticker())
                snap = await compatibility._capture_snapshot(
                    serial="android-1",
                    package_name="com.demo",
                    run_id=1,
                    cell_id=2,
                    phase="before",
                    page={"key": "home"},
                )
                await ticker_task
                self.assertGreaterEqual(ticks, 3)
                self.assertEqual(snap["xml_text"], "<hierarchy/>")

    async def test_capture_snapshot_respects_timeout(self):
        class _HungU2Device:
            def screenshot(self, format="pillow"):
                time.sleep(1.0)
                return Image.new("RGB", (8, 8))

            def dump_hierarchy(self):
                return "<hierarchy/>"

        fake_u2 = types.SimpleNamespace(connect=lambda serial: _HungU2Device())
        with patch.dict(sys.modules, {"uiautomator2": fake_u2}), patch.object(
            compatibility, "COMPAT_CAPTURE_TIMEOUT_SECONDS", 0.08
        ):
            with self.assertRaises((asyncio.TimeoutError, TimeoutError)):
                await compatibility._capture_snapshot(
                    serial="android-1",
                    package_name="com.demo",
                    run_id=1,
                    cell_id=2,
                    phase="before",
                    page={"key": "home"},
                )


if __name__ == "__main__":
    unittest.main()
