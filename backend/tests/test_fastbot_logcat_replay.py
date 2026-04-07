import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from backend.fastbot_runner import _monitor_logcat, run_fastbot_task


class _FakeStream:
    def __init__(self, lines):
        self._lines = [line.encode("utf-8") for line in lines]

    async def readline(self):
        if self._lines:
            return self._lines.pop(0)
        await asyncio.sleep(0)
        return b""


class _FakeProc:
    def __init__(self, lines):
        self.stdout = _FakeStream(lines)
        self.stderr = _FakeStream([])
        self.terminated = False

    def terminate(self):
        self.terminated = True

    async def wait(self):
        return 0


class _FakeMonkeyProc:
    def __init__(self):
        self.terminated = False
        self.killed = False
        self.returncode = None

    async def communicate(self):
        await asyncio.Event().wait()

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        return self.returncode or 0


class FastbotLogcatReplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_monitor_logcat_attaches_replay_to_anr_event(self):
        proc = _FakeProc([
            "04-02 10:00:00.000 E ActivityManager: ANR in com.example.app\n",
        ])
        crash_events = []
        replay_callback = AsyncMock(return_value={
            "status": "READY",
            "filename": "anr_100000.h264",
            "duration_sec": 35,
        })

        with patch(
            "backend.fastbot_runner.asyncio.create_subprocess_shell",
            new=AsyncMock(return_value=proc),
        ), patch(
            "backend.fastbot_runner._capture_logcat_snapshot",
            new=AsyncMock(return_value="log snapshot"),
        ):
            await _monitor_logcat(
                device_serial="device-1",
                package_name="com.example.app",
                stop_event=asyncio.Event(),
                crash_events=crash_events,
                capture_log=True,
                replay_callback=replay_callback,
            )

        self.assertEqual(len(crash_events), 1)
        self.assertEqual(crash_events[0]["type"], "ANR")
        self.assertEqual(crash_events[0]["full_log"], "log snapshot")
        self.assertEqual(crash_events[0]["replay"]["status"], "READY")
        self.assertEqual(crash_events[0]["replay"]["filename"], "anr_100000.h264")
        replay_callback.assert_awaited_once()
        self.assertTrue(proc.terminated)

    async def test_monitor_logcat_marks_replay_failure_without_dropping_crash(self):
        proc = _FakeProc([
            "04-02 10:00:00.000 E/AndroidRuntime(1234): FATAL EXCEPTION: main\n",
            "04-02 10:00:00.001 E/AndroidRuntime(1234): Process: com.example.app, PID: 1234\n",
        ])
        crash_events = []
        replay_callback = AsyncMock(side_effect=RuntimeError("replay export failed"))

        with patch(
            "backend.fastbot_runner.asyncio.create_subprocess_shell",
            new=AsyncMock(return_value=proc),
        ):
            await _monitor_logcat(
                device_serial="device-2",
                package_name="com.example.app",
                stop_event=asyncio.Event(),
                crash_events=crash_events,
                capture_log=False,
                replay_callback=replay_callback,
            )

        self.assertEqual(len(crash_events), 1)
        self.assertEqual(crash_events[0]["type"], "CRASH")
        self.assertEqual(crash_events[0]["replay"]["status"], "FAILED")
        self.assertIn("replay export failed", crash_events[0]["replay"]["error"])
        replay_callback.assert_awaited_once()
        self.assertTrue(proc.terminated)

    async def test_run_fastbot_task_does_not_hang_when_monitor_shutdown_times_out(self):
        monkey_proc = _FakeMonkeyProc()

        async def fake_logcat(
            device_serial,
            package_name,
            stop_event,
            crash_events,
            capture_log,
            abort_on_crash=False,
            abort_event=None,
            replay_callback=None,
        ):
            crash_events.append({"type": "CRASH", "time": "10:00:00", "full_log": ""})
            if abort_event:
                abort_event.set()

        async def stuck_monitor(*args, **kwargs):
            await asyncio.Event().wait()

        with patch(
            "backend.fastbot_runner.push_fastbot_assets",
            new=AsyncMock(),
        ), patch(
            "backend.fastbot_runner._adb_shell",
            new=AsyncMock(return_value=""),
        ), patch(
            "backend.fastbot_runner._monitor_logcat",
            new=AsyncMock(side_effect=fake_logcat),
        ), patch(
            "backend.fastbot_runner._monitor_performance",
            new=stuck_monitor,
        ), patch(
            "backend.fastbot_runner.asyncio.create_subprocess_shell",
            new=AsyncMock(return_value=monkey_proc),
        ), patch(
            "backend.fastbot_runner.MONITOR_TASK_SHUTDOWN_TIMEOUT_SECONDS",
            0.05,
        ):
            result = await asyncio.wait_for(
                run_fastbot_task(
                    device_serial="device-3",
                    package_name="com.example.app",
                    duration=60,
                    throttle=500,
                    ignore_crashes=False,
                    capture_log=False,
                    enable_performance_monitor=True,
                    enable_jank_frame_monitor=False,
                    enable_local_replay=False,
                ),
                timeout=1,
            )

        self.assertEqual(result["summary"]["total_crashes"], 1)
        self.assertTrue(monkey_proc.terminated)


if __name__ == "__main__":
    unittest.main()
