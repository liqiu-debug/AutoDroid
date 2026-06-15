import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from backend.fastbot_runner import (
    _compute_startup_aggregate,
    _parse_am_start_output,
    _parse_duration_token_to_ms,
    run_startup_task,
)


class StartupParsingTests(unittest.TestCase):
    def test_parse_duration_token_to_ms(self):
        self.assertEqual(_parse_duration_token_to_ms("123"), 123)
        self.assertEqual(_parse_duration_token_to_ms("+456ms"), 456)
        self.assertEqual(_parse_duration_token_to_ms("+1s234ms"), 1234)
        self.assertEqual(_parse_duration_token_to_ms("+1.5s"), 1500)

    def test_parse_am_start_output_extracts_metrics(self):
        parsed = _parse_am_start_output(
            "\n".join([
                "Status: ok",
                "LaunchState: COLD",
                "Activity: com.example/.MainActivity",
                "ThisTime: 120",
                "TotalTime: 340",
                "WaitTime: 360",
                "Complete",
            ]),
            "",
            0,
        )

        self.assertEqual(parsed["status"], "ok")
        self.assertEqual(parsed["launch_state"], "COLD")
        self.assertEqual(parsed["activity"], "com.example/.MainActivity")
        self.assertEqual(parsed["this_time_ms"], 120)
        self.assertEqual(parsed["total_time_ms"], 340)
        self.assertEqual(parsed["wait_time_ms"], 360)
        self.assertEqual(parsed["error"], "")

    def test_parse_am_start_output_marks_missing_total_time_failed(self):
        parsed = _parse_am_start_output("Status: ok", "", 0)
        self.assertEqual(parsed["error"], "am start -W 未返回 TotalTime")


class StartupAggregateTests(unittest.TestCase):
    def test_compute_startup_aggregate_by_mode(self):
        runs = [
            {"mode": "cold", "success": True, "total_time_ms": 1000, "ready_ms": 1200},
            {"mode": "cold", "success": True, "total_time_ms": 2000, "ready_ms": 2300},
            {"mode": "cold", "success": False, "total_time_ms": None},
            {"mode": "hot", "success": True, "total_time_ms": 300},
            {"mode": "hot", "success": True, "total_time_ms": 500},
        ]

        aggregate = _compute_startup_aggregate(runs, {"cold": 1500, "hot": 400})

        self.assertEqual(aggregate["cold"]["count"], 3)
        self.assertEqual(aggregate["cold"]["success_count"], 2)
        self.assertEqual(aggregate["cold"]["fail_count"], 1)
        self.assertEqual(aggregate["cold"]["median_ms"], 1500)
        self.assertEqual(aggregate["cold"]["slow_count"], 1)
        self.assertEqual(aggregate["cold"]["ready_median_ms"], 1750)
        self.assertEqual(aggregate["hot"]["median_ms"], 400)
        self.assertEqual(aggregate["hot"]["slow_count"], 1)


class StartupRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_cold_start_uses_force_stop_and_records_metrics(self):
        shell_calls = []

        async def fake_shell(device_serial, cmd, timeout=None):
            shell_calls.append(cmd)
            return ""

        async def fake_shell_result(device_serial, cmd, timeout=None):
            shell_calls.append(cmd)
            return {
                "stdout": "\n".join([
                    "Status: ok",
                    "LaunchState: COLD",
                    "Activity: com.example/.MainActivity",
                    "ThisTime: 100",
                    "TotalTime: 220",
                    "WaitTime: 240",
                ]),
                "stderr": "",
                "returncode": 0,
            }

        with patch("backend.fastbot_runner._build_fastbot_report_dir", return_value=tempfile.gettempdir()), \
            patch("backend.fastbot_runner._adb_shell", new=AsyncMock(side_effect=fake_shell)), \
            patch("backend.fastbot_runner._adb_shell_result", new=AsyncMock(side_effect=fake_shell_result)), \
            patch("backend.fastbot_runner._capture_logcat_snapshot", new=AsyncMock(return_value="")), \
            patch("backend.fastbot_runner._wait_for_startup_ready", new=AsyncMock(return_value={"status": "DISABLED", "error": ""})):
            result = await run_startup_task(
                "device-1",
                "com.example",
                activity_name=".MainActivity",
                startup_modes=["cold"],
                iterations=1,
                cooldown_sec=0,
                perfetto_slow_trace={"enabled": False, "cold_threshold_ms": 5000, "hot_threshold_ms": 1500},
            )

        self.assertTrue(any("am force-stop com.example" in cmd for cmd in shell_calls))
        self.assertTrue(any("am start -W" in cmd for cmd in shell_calls))
        self.assertEqual(result["summary"]["startup_runs"][0]["total_time_ms"], 220)
        self.assertEqual(result["summary"]["startup_aggregate"]["cold"]["median_ms"], 220)

    async def test_hot_start_prewarms_and_goes_home(self):
        shell_calls = []

        async def fake_shell(device_serial, cmd, timeout=None):
            shell_calls.append(cmd)
            return ""

        async def fake_shell_result(device_serial, cmd, timeout=None):
            shell_calls.append(cmd)
            return {
                "stdout": "Status: ok\nTotalTime: 180\nWaitTime: 190",
                "stderr": "",
                "returncode": 0,
            }

        with patch("backend.fastbot_runner._build_fastbot_report_dir", return_value=tempfile.gettempdir()), \
            patch("backend.fastbot_runner._adb_shell", new=AsyncMock(side_effect=fake_shell)), \
            patch("backend.fastbot_runner._adb_shell_result", new=AsyncMock(side_effect=fake_shell_result)), \
            patch("backend.fastbot_runner._capture_logcat_snapshot", new=AsyncMock(return_value="")), \
            patch("backend.fastbot_runner._wait_for_startup_ready", new=AsyncMock(return_value={"status": "DISABLED", "error": ""})):
            await run_startup_task(
                "device-1",
                "com.example",
                activity_name=".MainActivity",
                startup_modes=["hot"],
                iterations=1,
                cooldown_sec=0,
                perfetto_slow_trace={"enabled": False, "cold_threshold_ms": 5000, "hot_threshold_ms": 1500},
            )

        self.assertTrue(any(cmd.startswith("am start ") and ">/dev/null" in cmd for cmd in shell_calls))
        self.assertTrue(any(cmd == "input keyevent HOME" for cmd in shell_calls))

    async def test_slow_trace_diagnostic_run_does_not_pollute_main_runs(self):
        async def fake_shell_result(device_serial, cmd, timeout=None):
            return {
                "stdout": "Status: ok\nTotalTime: 6000\nWaitTime: 6100",
                "stderr": "",
                "returncode": 0,
            }

        with patch("backend.fastbot_runner._build_fastbot_report_dir", return_value=tempfile.gettempdir()), \
            patch("backend.fastbot_runner._adb_shell", new=AsyncMock(return_value="")), \
            patch("backend.fastbot_runner._adb_shell_result", new=AsyncMock(side_effect=fake_shell_result)), \
            patch("backend.fastbot_runner._capture_logcat_snapshot", new=AsyncMock(return_value="")), \
            patch("backend.fastbot_runner._wait_for_startup_ready", new=AsyncMock(return_value={"status": "DISABLED", "error": ""})), \
            patch(
                "backend.fastbot_runner._capture_startup_perfetto_trace",
                new=AsyncMock(return_value={
                    "mode": "cold",
                    "trace_exported": True,
                    "trace_path": "reports/fastbot/1/startup_trace_cold_001.perfetto-trace",
                    "diagnosis_status": "PENDING",
                }),
            ):
            result = await run_startup_task(
                "device-1",
                "com.example",
                activity_name=".MainActivity",
                startup_modes=["cold"],
                iterations=1,
                cooldown_sec=0,
                perfetto_slow_trace={"enabled": True, "cold_threshold_ms": 5000, "hot_threshold_ms": 1500},
            )

        self.assertEqual(len(result["summary"]["startup_runs"]), 1)
        self.assertEqual(result["summary"]["slow_count"], 1)
        self.assertTrue(result["summary"]["slow_events"][0]["trace_exported"])


if __name__ == "__main__":
    unittest.main()
