"""Startup semantics, independent of ADB/WDA hardware and production state."""
import asyncio
import copy
import threading
import time
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx
from pydantic import ValidationError

from backend.startup.android import AndroidStartupAdapter, parse_am_start
from backend.startup.contract import StartupConfig, normalize_config, startup_capabilities
from backend.startup.ios import IOSStartupAdapter
from backend.startup.metrics import compute_startup_aggregate
from backend.startup.runner import run_startup
from backend.startup.runtime import call_sync


def config(platform="android", **overrides):
    result = {"package_name": "com.example.app", "device_serials": ["device-1"], "platform": platform,
              "iterations": 1, "cooldown_sec": 0, "capture_log": False,
              "perfetto_slow_trace": {"enabled": False}}
    result.update(overrides)
    return result


class FakeAdapter:
    platform = "android"
    device_serial = "device-1"

    def __init__(self, *, ready=True, valid=True):
        self.ready = ready
        self.valid = valid
        self.foreground = False
        self.closed = False
        self.launched = asyncio.Event()

    async def preflight(self, options):
        return {"initialization_ms": 5000}

    async def prepare(self, mode, options, stop_event):
        return {"sample_valid": True, "actual_mode": mode, "state_before": {"state": "not_running"}}

    async def launch(self, mode, options, stop_event):
        self.foreground = True
        self.launched.set()
        await asyncio.sleep(0.12)
        return {"launch_success": True, "sample_valid": self.valid, "actual_mode": mode if self.valid else "warm",
                "invalid_reason": "Activity 被重建" if not self.valid else "", "total_time_ms": 100,
                "state_after": {"state": "foreground"}}

    async def is_foreground(self):
        return self.foreground

    async def ready_exists(self, options):
        await asyncio.sleep(0.01)
        return self.ready

    async def close(self):
        self.closed = True


class StartupContractTests(unittest.TestCase):
    def test_legacy_limits_apply_even_when_tracing_is_disabled(self):
        value = normalize_config(config(perfetto_slow_trace={"enabled": False, "cold_threshold_ms": 4200,
                                                               "hot_threshold_ms": 800}),
                                 platform="android", device_serial="device-1")
        self.assertEqual(value["thresholds"]["cold_ms"], 4200)
        self.assertEqual(value["thresholds"]["metric"], "total_time_ms")

    def test_ios_limits_and_locators_are_not_android_metrics(self):
        value = normalize_config(config("ios"), platform="ios", device_serial="device-1")
        self.assertEqual(value["thresholds"]["metric"], "automation_ms")
        caps = startup_capabilities("ios", trace_supported=True)
        self.assertNotIn("total_time_ms", caps["metrics"])
        self.assertFalse(caps["trace"])
        self.assertIn("accessibility_id", caps["ready_locators"])

    def test_invalid_package_and_disabled_ready_metric_are_rejected(self):
        with self.assertRaises(ValidationError):
            StartupConfig.model_validate(config(package_name="com.example; touch /tmp/test"))
        with self.assertRaises(ValidationError):
            StartupConfig.model_validate(config(thresholds={"metric": "ready_ms"}))
        with self.assertRaises(ValidationError):
            StartupConfig.model_validate(config("ios", thresholds={"metric": "total_time_ms"}))

    def test_missing_native_metric_is_not_zero_or_launch_failure(self):
        parsed = parse_am_start({"stdout": "Status: ok\nLaunchState: HOT", "returncode": 0})
        self.assertIsNone(parsed["total_time_ms"])
        self.assertEqual(parsed["error"], "")

    def test_aggregate_excludes_diagnostics_invalid_and_other_platforms(self):
        base = {"mode": "hot", "sample_valid": True, "launch_success": True,
                "success": True, "status": "PASS", "total_time_ms": 0, "ready_status": "DISABLED"}
        runs = [base, dict(base, total_time_ms=100), dict(base, total_time_ms=999, diagnostic=True),
                dict(base, total_time_ms=777, sample_valid=False, status="INVALID"),
                dict(base, platform="ios", total_time_ms=888), dict(base, total_time_ms=float("nan"))]
        stats = compute_startup_aggregate(runs, {"metric": "total_time_ms", "hot_ms": 50}, platform="android")["hot"]
        self.assertEqual(stats["metrics"]["total_time_ms"]["count"], 2)
        self.assertEqual(stats["min_ms"], 0)
        self.assertEqual(stats["median_ms"], 50)
        self.assertEqual(stats["slow_count"], 1)
        self.assertIsNone(stats["ready_p90_ms"])
        self.assertFalse(stats["metrics"]["total_time_ms"]["samples_sufficient"])


class StartupMeasurementTests(unittest.IsolatedAsyncioTestCase):
    async def test_ready_observation_runs_before_blocking_launch_command_returns(self):
        adapter = FakeAdapter()
        snapshots = []
        result = await run_startup(adapter, config(startup_modes=["cold"], ready_check={
            "enabled": True, "locator_type": "text", "locator_value": "首页", "timeout_sec": 1}),
            progress=snapshots.append)
        run = result["summary"]["startup_runs"][0]
        self.assertTrue(run["success"])
        self.assertLess(run["ready_ms"], run["automation_ms"])
        self.assertLess(run["automation_ms"], 1000)
        self.assertTrue(adapter.closed)
        self.assertEqual(result["summary"]["status"], "COMPLETED")
        self.assertEqual(snapshots[0]["summary"]["completed_count"], 0)
        self.assertEqual(snapshots[-1]["summary"]["completed_count"], 1)

    async def test_ready_timeout_is_business_failure_and_not_native_sample_loss(self):
        result = await run_startup(FakeAdapter(ready=False), config(startup_modes=["cold"], ready_check={
            "enabled": True, "locator_type": "text", "locator_value": "首页", "timeout_sec": 1}))
        summary = result["summary"]
        run = summary["startup_runs"][0]
        self.assertTrue(run["launch_success"])
        self.assertFalse(run["success"])
        self.assertEqual(run["ready_status"], "TIMEOUT")
        self.assertIsNone(run["ready_ms"])
        self.assertEqual(summary["status"], "FAILED")
        self.assertEqual(summary["startup_aggregate"]["cold"]["metrics"]["total_time_ms"]["count"], 1)
        self.assertEqual(summary["startup_aggregate"]["cold"]["metrics"]["ready_ms"]["count"], 0)

    async def test_enabled_ready_check_is_not_reported_disabled_after_preparation_failure(self):
        class FailedPreparation(FakeAdapter):
            async def prepare(self, mode, options, stop_event):
                raise RuntimeError("设备连接中断")
        result = await run_startup(FailedPreparation(), config(startup_modes=["cold"], ready_check={
            "enabled": True, "locator_type": "text", "locator_value": "首页"}))
        self.assertEqual(result["summary"]["startup_runs"][0]["ready_status"], "NOT_CHECKED")

    async def test_recreated_activity_is_invalid_and_excluded_from_hot_statistics(self):
        result = await run_startup(FakeAdapter(valid=False), config(startup_modes=["hot"]))
        summary = result["summary"]
        self.assertEqual(summary["startup_runs"][0]["status"], "INVALID")
        self.assertEqual(summary["invalid_count"], 1)
        self.assertEqual(summary["startup_aggregate"]["hot"]["metrics"]["total_time_ms"]["count"], 0)

    async def test_cancel_retains_finished_samples_and_closes_adapter(self):
        stop = threading.Event()
        adapter = FakeAdapter()
        def on_progress(report):
            if report["summary"]["completed_count"] == 1:
                stop.set()
        result = await run_startup(adapter, config(iterations=3, startup_modes=["cold"]),
                                   stop_event=stop, progress=on_progress)
        self.assertEqual(result["summary"]["status"], "ABORTED")
        self.assertEqual(result["summary"]["completed_count"], 1)
        self.assertEqual(result["summary"]["requested_count"], 3)
        self.assertTrue(adapter.closed)

    async def test_cancel_during_io_records_interruption_without_counting_it_complete(self):
        stop = threading.Event()
        adapter = FakeAdapter()
        task = asyncio.create_task(run_startup(adapter, config(startup_modes=["cold"]), stop_event=stop))
        await adapter.launched.wait()
        stop.set()
        result = await task
        self.assertEqual(result["summary"]["status"], "ABORTED")
        self.assertEqual(result["summary"]["completed_count"], 0)
        self.assertEqual(result["summary"]["startup_runs"][0]["status"], "ABORTED")

    async def test_cancelled_sync_transport_is_awaited_before_resource_release(self):
        entered, exited = threading.Event(), threading.Event()
        def request():
            entered.set()
            time.sleep(0.08)
            exited.set()
        task = asyncio.create_task(call_sync(request))
        while not entered.is_set():
            await asyncio.sleep(0.001)
        task.cancel()
        await asyncio.sleep(0.01)
        self.assertFalse(task.done())
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(exited.is_set())


class WdaFixture:
    def __init__(self, *, locked=False, lost_background=False):
        self.state = 1
        self.locked = locked
        self.lost_background = lost_background
        self.calls = []
        self.settings = {"waitForIdleTimeout": 10, "animationCoolOffTimeout": 2}
        self.home_reads = 0

    def handle(self, request):
        import json

        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        self.calls.append((request.method, path, body))
        if path == "/status":
            value = {"ready": True, "build": {"version": "16.15.3", "productBundleIdentifier": "io.appium.wda.xctrunner"}}
            return httpx.Response(200, json={"value": value, "sessionId": "existing-session"})
        if path == "/wda/locked":
            value = self.locked
        elif path.endswith("/appium/settings"):
            if request.method == "POST":
                self.settings.update(body["settings"])
            value = copy.deepcopy(self.settings)
        elif path.endswith("/wda/apps/state"):
            if self.state == 2 and self.lost_background:
                self.home_reads += 1
                if self.home_reads >= 2:
                    self.state = 1
            value = self.state
        elif path.endswith("/wda/apps/terminate"):
            self.state, value = 1, True
        elif path.endswith(("/wda/apps/launch", "/wda/apps/activate")):
            self.state, value = 4, None
        elif path == "/wda/homescreen":
            self.state, value = 2, None
        elif path.endswith("/elements"):
            value = [{"element-6066-11e4-a52e-4f735466cecf": "ready"}] if self.state == 4 else []
        elif path == "/wda/activeAppInfo":
            value = {"bundleId": "com.example.app", "pid": 321}
        else:
            return httpx.Response(404, json={"value": {"error": "unsupported operation", "message": path}})
        return httpx.Response(200, json={"value": value})


class IOSStartupTests(unittest.IsolatedAsyncioTestCase):
    def adapter(self, fixture):
        http = httpx.Client(base_url="http://wda.invalid", transport=httpx.MockTransport(fixture.handle))
        self.addCleanup(http.close)
        return IOSStartupAdapter(SimpleNamespace(wda_url="http://wda.invalid", device_id="device-1"), http_client=http)

    async def test_cold_and_hot_use_distinct_commands_and_restore_wait_settings(self):
        fixture = WdaFixture()
        result = await run_startup(self.adapter(fixture), config("ios"))
        runs = result["summary"]["startup_runs"]
        self.assertEqual(result["summary"]["status"], "COMPLETED")
        self.assertEqual(runs[0]["state_before"]["value"], 1)
        self.assertEqual(runs[1]["state_before"]["value"], 2)
        self.assertTrue(any(path.endswith("/wda/apps/activate") for _, path, _ in fixture.calls))
        self.assertEqual(fixture.settings, {"waitForIdleTimeout": 10, "animationCoolOffTimeout": 2})
        self.assertFalse(any(path == "/session" for _, path, _ in fixture.calls))
        self.assertTrue(all(run["total_time_ms"] is None for run in runs))

    async def test_background_process_loss_never_falls_back_to_a_cold_launch(self):
        fixture = WdaFixture(lost_background=True)
        result = await run_startup(self.adapter(fixture), config("ios", startup_modes=["hot"]))
        self.assertEqual(result["summary"]["startup_runs"][0]["status"], "INVALID")
        self.assertFalse(any(path.endswith("/wda/apps/activate") for _, path, _ in fixture.calls))

    async def test_locked_device_and_wda_runner_are_rejected_without_launch(self):
        for options, fixture in ((config("ios"), WdaFixture(locked=True)),
                                 (config("ios", package_name="io.appium.wda.xctrunner"), WdaFixture())):
            result = await run_startup(self.adapter(fixture), options)
            self.assertEqual(result["summary"]["status"], "FAILED")
            self.assertFalse(any(path.endswith("/wda/apps/launch") for _, path, _ in fixture.calls))

    async def test_state_loss_at_measurement_boundary_remains_an_invalid_sample(self):
        class LostAtBoundary(WdaFixture):
            def handle(self, request):
                if request.url.path.endswith("/wda/apps/state") and self.state == 2:
                    self.home_reads += 1
                    if self.home_reads == 3:
                        self.state = 1
                return super().handle(request)
        fixture = LostAtBoundary()
        result = await run_startup(self.adapter(fixture), config("ios", startup_modes=["hot"]))
        summary = result["summary"]
        self.assertEqual(summary["startup_runs"][0]["status"], "INVALID")
        self.assertFalse(summary["startup_runs"][0]["sample_valid"])
        self.assertEqual(summary["valid_count"], 0)
        self.assertEqual(summary["invalid_count"], 1)


class AndroidFixture(AndroidStartupAdapter):
    def __init__(self, launch_state="COLD"):
        super().__init__("device-1", driver=SimpleNamespace(info={"screenOn": True}))
        self.launch_state = launch_state
        self.running = False
        self.foreground = False
        self.calls = []

    async def _run(self, args, *, timeout=8, stop_event=None, check=True):
        self.calls.append(args)
        value = ""
        if args == ["get-state"]:
            value = "device"
        elif args[:3] == ["shell", "pm", "path"]:
            value = "package:/data/app/base.apk"
        elif args[:4] == ["shell", "cmd", "package", "resolve-activity"]:
            value = "com.example.app/.MainActivity"
        elif args[:3] == ["shell", "getprop", "ro.build.version.sdk"]:
            value = "33"
        elif args[:2] == ["shell", "pidof"]:
            value = "321" if self.running else ""
        elif args[:4] == ["shell", "dumpsys", "activity", "activities"]:
            value = "mResumedActivity: ActivityRecord com.example.app/.MainActivity" if self.foreground else "mResumedActivity: ActivityRecord com.launcher/.Home"
        elif args[:3] == ["shell", "am", "force-stop"]:
            self.running, self.foreground = False, False
        elif args[:3] == ["shell", "am", "start"]:
            self.running, self.foreground = True, True
            value = f"Status: ok\nLaunchState: {self.launch_state}\nTotalTime: 42\nWaitTime: 60"
        elif args[:3] == ["shell", "input", "keyevent"]:
            self.foreground = False
        return {"stdout": value, "stderr": "", "returncode": 0}


class AndroidTraceFixture(AndroidFixture):
    log = ""

    async def _run(self, args, **kwargs):
        if args[:3] == ["shell", "which", "perfetto"]:
            self.calls.append(args)
            return {"stdout": "/system/bin/perfetto", "stderr": "", "returncode": 0}
        if args[:2] == ["shell", "perfetto"]:
            self.calls.append(args)
            return {"stdout": "1234", "stderr": "", "returncode": 0}
        if args[:3] == ["shell", "kill", "-0"]:
            self.calls.append(args)
            return {"stdout": "", "stderr": "", "returncode": 1}
        if args[:1] == ["pull"]:
            self.calls.append(args)
            Path(args[2]).write_bytes(b"test-original-trace")
            return {"stdout": "pulled", "stderr": "", "returncode": 0}
        if args[:2] == ["shell", "logcat"]:
            self.calls.append(args)
            return {"stdout": self.log, "stderr": "", "returncode": 0}
        if args[:2] == ["exec-out", "screencap"]:
            self.calls.append(args)
            return {"stdout": "", "stdout_bytes": b"\x89PNG\r\n\x1a\nfixture", "stderr": "", "returncode": 0}
        return await super()._run(args, **kwargs)


class AndroidStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_cold_force_stops_without_clearing_data_and_preserves_native_metrics(self):
        adapter = AndroidFixture()
        result = await run_startup(adapter, config(startup_modes=["cold"]))
        self.assertEqual(result["summary"]["status"], "COMPLETED")
        run = result["summary"]["startup_runs"][0]
        self.assertEqual(run["total_time_ms"], 42)
        self.assertEqual(run["state_before"]["state"], "not_running")
        self.assertTrue(any(args[:3] == ["shell", "am", "force-stop"] for args in adapter.calls))
        self.assertFalse(any("clear" in args for args in adapter.calls))

    async def test_warm_activity_recreation_does_not_pollute_hot_aggregate(self):
        result = await run_startup(AndroidFixture("WARM"), config(startup_modes=["hot"]))
        run = result["summary"]["startup_runs"][0]
        self.assertEqual(run["actual_mode"], "warm")
        self.assertFalse(run["sample_valid"])
        self.assertEqual(result["summary"]["startup_aggregate"]["hot"]["metrics"]["total_time_ms"]["count"], 0)

    async def test_perfetto_captures_the_original_slow_iteration_without_relaunch(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = AndroidTraceFixture()
            adapter.artifact_dir = Path(directory)
            result = await run_startup(adapter, config(startup_modes=["cold"],
                perfetto_slow_trace={"enabled": True}, thresholds={"metric": "total_time_ms", "cold_ms": 1}))
            self.assertEqual(len(result["trace_artifacts"]), 1)
            artifact = result["trace_artifacts"][0]
            self.assertEqual(artifact["capture_mode"], "original_sample")
            self.assertFalse(artifact["diagnostic"])
            self.assertTrue((Path(directory) / artifact["path"]).is_file())
            starts = [index for index, args in enumerate(adapter.calls) if args[:3] == ["shell", "am", "start"]]
            captures = [index for index, args in enumerate(adapter.calls) if args[:2] == ["shell", "perfetto"]]
            self.assertEqual(len(starts), 1)
            self.assertLess(captures[0], starts[0])

    async def test_target_crash_retains_original_trace_and_failure_screenshot(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = AndroidTraceFixture()
            adapter.artifact_dir = Path(directory)
            adapter.log = "AndroidRuntime: FATAL EXCEPTION: main\nAndroidRuntime: Process: com.example.app, PID: 321"
            result = await run_startup(adapter, config(startup_modes=["cold"], capture_log=True,
                                                       perfetto_slow_trace={"enabled": True}))
            run = result["summary"]["startup_runs"][0]
            self.assertEqual(run["status"], "FAIL")
            self.assertEqual(len(result["trace_artifacts"]), 1)
            self.assertTrue((Path(directory) / run["screenshot_path"]).is_file())
            self.assertEqual(result["summary"]["total_crashes"], 1)

    async def test_other_process_crash_is_not_a_target_app_failure(self):
        adapter = AndroidTraceFixture()
        adapter.log = "AndroidRuntime: FATAL EXCEPTION: main\nProcess: com.other.app, PID: 77\nI com.example.app: normal"
        result = await run_startup(adapter, config(startup_modes=["cold"], capture_log=True))
        self.assertEqual(result["summary"]["status"], "COMPLETED")
        self.assertEqual(result["summary"]["total_crashes"], 0)


if __name__ == "__main__":
    unittest.main()
