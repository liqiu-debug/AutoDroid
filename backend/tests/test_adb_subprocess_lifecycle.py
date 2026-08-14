"""ADB 截图生命周期：超时不遗留子进程，并合并同设备并发采集。"""
import asyncio
import signal
import unittest
from unittest.mock import AsyncMock, patch

from backend.api import devices


class _BlockingProcess:
    def __init__(self):
        self.pid = 4321
        self.returncode = None
        self._stopped = asyncio.Event()

    async def communicate(self):
        await self._stopped.wait()
        return b"", b""

    async def wait(self):
        await self._stopped.wait()
        return self.returncode

    def stop(self, returncode=-signal.SIGTERM):
        self.returncode = returncode
        self._stopped.set()


class AdbSubprocessLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        devices._android_screenshot_task_lock = None
        devices._android_screenshot_tasks.clear()

    async def test_timeout_terminates_adb_process_group(self):
        proc = _BlockingProcess()

        def terminate_process_group(pid, sig):
            self.assertEqual(pid, proc.pid)
            self.assertEqual(sig, signal.SIGTERM)
            proc.stop()

        with patch(
            "backend.api.devices.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ), patch("backend.api.devices.os.killpg", side_effect=terminate_process_group) as killpg:
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                await devices._run_adb_command("devices", timeout=0.01)

        killpg.assert_called_once_with(proc.pid, signal.SIGTERM)
        self.assertEqual(proc.returncode, -signal.SIGTERM)

    async def test_cancellation_terminates_adb_process_group(self):
        proc = _BlockingProcess()

        def terminate_process_group(pid, sig):
            self.assertEqual(sig, signal.SIGTERM)
            proc.stop()

        with patch(
            "backend.api.devices.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ), patch("backend.api.devices.os.killpg", side_effect=terminate_process_group) as killpg:
            task = asyncio.create_task(devices._run_adb_command("devices", timeout=30))
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        killpg.assert_called_once_with(proc.pid, signal.SIGTERM)
        self.assertEqual(proc.returncode, -signal.SIGTERM)

    async def test_concurrent_screenshot_requests_share_one_capture(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def capture(serial):
            self.assertEqual(serial, "127.0.0.1:28101")
            started.set()
            await release.wait()
            return "encoded", "jpeg"

        with patch(
            "backend.api.devices._capture_android_screenshot_preview",
            side_effect=capture,
        ) as capture_mock:
            first = asyncio.create_task(
                devices._get_coalesced_android_screenshot_preview("127.0.0.1:28101")
            )
            await started.wait()
            second = asyncio.create_task(
                devices._get_coalesced_android_screenshot_preview("127.0.0.1:28101")
            )
            await asyncio.sleep(0)
            release.set()
            self.assertEqual(await first, ("encoded", "jpeg"))
            self.assertEqual(await second, ("encoded", "jpeg"))

        capture_mock.assert_awaited_once_with("127.0.0.1:28101")


if __name__ == "__main__":
    unittest.main()
