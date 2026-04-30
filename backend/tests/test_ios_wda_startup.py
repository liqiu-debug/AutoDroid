import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import HTTPException
from sqlmodel import SQLModel, Session, create_engine, select

from backend.api.devices import (
    _build_ios_wda_launch_command,
    _build_ios_wda_tidevice_command,
    _build_ios_wda_xcodebuild_command,
    _ensure_ios_wda_ready,
    _expand_start_attempts_for_launch_source,
    _is_xcodebuild_launch_source,
    _is_process_alive,
    _is_tidevice_environment_error,
    _launch_ios_wda_process,
    check_ios_wda,
)
from backend.models import Device


class IOSWdaStartupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)

    def tearDown(self) -> None:
        self.session.close()

    def _add_device(self, serial: str, platform: str, status: str = "IDLE") -> Device:
        device = Device(serial=serial, platform=platform, model=f"{platform}-model", status=status)
        self.session.add(device)
        self.session.commit()
        self.session.refresh(device)
        return device

    def test_ensure_ios_wda_ready_returns_directly_when_healthy(self):
        self._add_device("ios-1", "ios")
        with patch(
            "backend.api.devices._check_ios_wda_health",
            return_value={"healthy": True, "wda_url": "http://127.0.0.1:8201", "error": None},
        ), patch("backend.api.devices._cleanup_ios_tidevice_processes") as cleanup_mock, patch(
            "backend.api.devices._launch_ios_wda_process"
        ) as launch_mock:
            result = _ensure_ios_wda_ready(self.session, "ios-1")

        self.assertTrue(result["healthy"])
        self.assertFalse(result["attempted_start"])
        self.assertFalse(result["recovered_by_cleanup"])
        self.assertEqual(result["startup_checks"], 1)
        cleanup_mock.assert_not_called()
        launch_mock.assert_not_called()

    def test_ensure_ios_wda_ready_cleanup_then_launch(self):
        self._add_device("ios-1", "ios")
        events = []

        def _cleanup(_: str):
            events.append("cleanup")
            return {"killed_pids": [101], "killed_count": 1}

        def _launch(*_args, **_kwargs):
            events.append("launch")
            return {
                "pid": 12345,
                "command_str": "tidevice -u ios-1 xctest --bundle_id com.facebook.WebDriverAgentRunner.xctrunner",
                "command_source": "default",
                "bundle_id": "com.facebook.WebDriverAgentRunner.xctrunner",
            }

        with patch(
            "backend.api.devices._check_ios_wda_health",
            side_effect=[
                {"healthy": False, "wda_url": "http://127.0.0.1:8201", "error": "down-1"},
                {"healthy": False, "wda_url": "http://127.0.0.1:8201", "error": "down-2"},
                {"healthy": True, "wda_url": "http://127.0.0.1:8201", "error": None},
            ],
        ), patch("backend.api.devices._cleanup_ios_tidevice_processes", side_effect=_cleanup), patch(
            "backend.api.devices._launch_ios_wda_process", side_effect=_launch
        ), patch("backend.api.devices.time.sleep", return_value=None):
            result = _ensure_ios_wda_ready(
                self.session,
                "ios-1",
                retry_attempts=1,
                retry_interval_seconds=0.01,
            )

        self.assertEqual(events, ["cleanup", "launch"])
        self.assertTrue(result["healthy"])
        self.assertTrue(result["attempted_start"])
        self.assertEqual(result["startup_checks"], 3)
        self.assertEqual(result["cleanup"]["killed_count"], 1)
        self.assertEqual(result["start_pid"], 12345)

    def test_ensure_ios_wda_ready_returns_failure_when_launch_fails(self):
        self._add_device("ios-1", "ios")
        with patch(
            "backend.api.devices._check_ios_wda_health",
            side_effect=[
                {"healthy": False, "wda_url": "http://127.0.0.1:8201", "error": "down-1"},
                {"healthy": False, "wda_url": "http://127.0.0.1:8201", "error": "down-2"},
            ],
        ), patch(
            "backend.api.devices._cleanup_ios_tidevice_processes",
            return_value={"killed_pids": [], "killed_count": 0},
        ), patch(
            "backend.api.devices._launch_ios_wda_process",
            side_effect=RuntimeError("launch failed"),
        ):
            result = _ensure_ios_wda_ready(
                self.session,
                "ios-1",
                retry_attempts=1,
                retry_interval_seconds=0.01,
            )

        self.assertFalse(result["healthy"])
        self.assertTrue(result["attempted_start"])
        self.assertEqual(result["startup_checks"], 2)
        self.assertIn("launch failed", result["error"])

    def test_ensure_ios_wda_ready_relaunches_xcodebuild_when_tidevice_exits_with_environment_error(self):
        self._add_device("ios-1", "ios")
        with patch(
            "backend.api.devices._check_ios_wda_health",
            side_effect=[
                {"healthy": False, "wda_url": "http://127.0.0.1:8201", "error": "down-1"},
                {"healthy": False, "wda_url": "http://127.0.0.1:8201", "error": "down-2"},
                {"healthy": False, "wda_url": "http://127.0.0.1:8201", "error": "down-3"},
                {"healthy": True, "wda_url": "http://127.0.0.1:8201", "error": None},
            ],
        ), patch(
            "backend.api.devices._cleanup_ios_tidevice_processes",
            return_value={"killed_pids": [], "killed_count": 0},
        ), patch(
            "backend.api.devices._launch_ios_wda_process",
            side_effect=[
                {
                    "pid": 12345,
                    "command_str": "tidevice -u ios-1 xctest --bundle_id com.demo.runner.xctrunner",
                    "command_source": "tidevice",
                    "bundle_id": "com.demo.runner.xctrunner",
                    "log_path": "/tmp/tidevice.log",
                },
                {
                    "pid": 22345,
                    "command_str": "xcodebuild test",
                    "command_source": "xcodebuild",
                    "bundle_id": None,
                    "log_path": "/tmp/xcodebuild.log",
                },
            ],
        ) as launch_mock, patch(
            "backend.api.devices._is_process_alive",
            return_value=False,
        ), patch(
            "backend.api.devices._read_log_tail",
            side_effect=lambda path: "DeveloperImage not found" if path == "/tmp/tidevice.log" else "",
        ), patch(
            "backend.api.devices._build_ios_wda_xcodebuild_fallback_command",
            return_value={"command": ["xcodebuild", "test"], "command_source": "xcodebuild"},
        ), patch("backend.api.devices.time.sleep", return_value=None):
            result = _ensure_ios_wda_ready(
                self.session,
                "ios-1",
                retry_attempts=2,
                retry_interval_seconds=0.01,
            )

        self.assertTrue(result["healthy"])
        self.assertEqual(result["start_pid"], 22345)
        self.assertEqual(result["start_command_source"], "xcodebuild")
        self.assertEqual(launch_mock.call_count, 2)

    def test_ensure_ios_wda_ready_extends_wait_window_for_xcodebuild(self):
        self._add_device("ios-1", "ios")
        with patch(
            "backend.api.devices._check_ios_wda_health",
            side_effect=[
                {"healthy": False, "wda_url": "http://127.0.0.1:8201", "error": "down-1"},
                {"healthy": False, "wda_url": "http://127.0.0.1:8201", "error": "down-2"},
                {"healthy": False, "wda_url": "http://127.0.0.1:8201", "error": "down-3"},
                {"healthy": False, "wda_url": "http://127.0.0.1:8201", "error": "down-4"},
                {"healthy": False, "wda_url": "http://127.0.0.1:8201", "error": "down-5"},
                {"healthy": True, "wda_url": "http://127.0.0.1:8201", "error": None},
            ],
        ), patch(
            "backend.api.devices._cleanup_ios_tidevice_processes",
            return_value={"killed_pids": [], "killed_count": 0},
        ), patch(
            "backend.api.devices._launch_ios_wda_process",
            return_value={
                "pid": 32345,
                "command_str": "xcodebuild test",
                "command_source": "xcodebuild",
                "bundle_id": None,
                "log_path": "/tmp/xcodebuild.log",
            },
        ), patch(
            "backend.api.devices.time.sleep",
            return_value=None,
        ):
            result = _ensure_ios_wda_ready(
                self.session,
                "ios-1",
                retry_attempts=2,
                retry_interval_seconds=0.01,
            )

        self.assertTrue(result["healthy"])
        self.assertEqual(result["start_command_source"], "xcodebuild")
        self.assertEqual(result["startup_checks"], 6)

    def test_build_launch_command_prefers_tidevice_on_macos(self):
        with patch("backend.api.devices.platform.system", return_value="Darwin"), patch(
            "backend.api.devices._build_ios_wda_tidevice_command",
            return_value={"command": ["tidevice"], "command_source": "tidevice"},
        ) as tidevice_mock:
            payload = _build_ios_wda_launch_command(self.session, "ios-1")
        tidevice_mock.assert_called_once_with(self.session, "ios-1")
        self.assertEqual(payload["command_source"], "tidevice")

    def test_build_launch_command_uses_custom_override_first(self):
        with patch("backend.api.devices.get_setting_value") as setting_mock, patch(
            "backend.api.devices.platform.system", return_value="Darwin"
        ):
            def _side_effect(_, key):
                if key == "ios_wda_launch_cmd.ios-1":
                    return "echo launch {udid} {bundle_id}"
                if key in {"ios_wda_bundle_id.ios-1", "ios_wda_bundle_id"}:
                    return "com.demo.runner.xctrunner"
                return None

            setting_mock.side_effect = _side_effect
            payload = _build_ios_wda_launch_command(self.session, "ios-1")

        self.assertEqual(payload["command_source"], "setting")
        self.assertEqual(
            payload["command"],
            ["echo", "launch", "ios-1", "com.demo.runner.xctrunner"],
        )

    def test_build_xcodebuild_command_with_project_path(self):
        project_path = str(Path(tempfile.gettempdir()) / "WebDriverAgent.xcodeproj")
        with patch("backend.api.devices.get_setting_value") as setting_mock, patch(
            "backend.api.devices._discover_wda_xcodeproj_path",
            return_value=project_path,
        ), patch("backend.api.devices.os.path.exists", return_value=True):
            def _side_effect(_, key):
                if key == "ios_wda_scheme.ios-1":
                    return "WebDriverAgentRunner"
                return None

            setting_mock.side_effect = _side_effect
            payload = _build_ios_wda_xcodebuild_command(self.session, "ios-1")

        self.assertEqual(payload["command_source"], "xcodebuild")
        self.assertIn("-project", payload["command"])
        self.assertIn(project_path, payload["command"])
        self.assertIn("-destination", payload["command"])
        self.assertIn("id=ios-1", payload["command"])
        self.assertIn("test", payload["command"])

    def test_build_tidevice_command_uses_resolved_bundle_id(self):
        with patch(
            "backend.api.devices._resolve_ios_wda_bundle_id",
            return_value="com.demo.runner.xctrunner",
        ) as resolve_mock:
            payload = _build_ios_wda_tidevice_command(self.session, "ios-1")

        resolve_mock.assert_called_once_with(self.session, "ios-1")
        self.assertEqual(payload["command_source"], "tidevice")
        self.assertEqual(
            payload["command"],
            ["tidevice", "-u", "ios-1", "xctest", "--bundle_id", "com.demo.runner.xctrunner"],
        )

    def test_is_tidevice_environment_error_detects_developer_image_issue(self):
        self.assertTrue(_is_tidevice_environment_error("ServiceError: DeveloperImage not found"))
        self.assertTrue(_is_tidevice_environment_error("MuxServiceError: InvalidService"))
        self.assertFalse(_is_tidevice_environment_error("No app matches bundle id"))

    def test_is_xcodebuild_launch_source_detects_prefixed_source(self):
        self.assertTrue(_is_xcodebuild_launch_source("xcodebuild"))
        self.assertTrue(_is_xcodebuild_launch_source("xcodebuild.fallback"))
        self.assertFalse(_is_xcodebuild_launch_source("tidevice"))

    def test_expand_start_attempts_for_launch_source_uses_xcodebuild_floor(self):
        attempts = _expand_start_attempts_for_launch_source(
            2,
            {"command_source": "xcodebuild"},
            {"xcodebuild_retry_attempts": 10},
        )
        self.assertEqual(attempts, 10)

    def test_is_process_alive_returns_false_for_zombie(self):
        with patch("backend.api.devices.os.kill", return_value=None), patch(
            "backend.api.devices.subprocess.run",
            return_value=Mock(stdout="Z+\n"),
        ):
            self.assertFalse(_is_process_alive(12345))

    def test_launch_ios_wda_process_falls_back_to_xcodebuild_when_runner_missing_on_macos(self):
        popen_commands = []

        class _FakeProcess:
            def __init__(self, pid: int, exit_code):
                self.pid = pid
                self._exit_code = exit_code

            def poll(self):
                return self._exit_code

        def _popen(cmd, stdout=None, stderr=None, start_new_session=None):
            popen_commands.append(list(cmd))
            if cmd and cmd[0] == "tidevice":
                return _FakeProcess(1001, 1)
            return _FakeProcess(2002, None)

        with patch("backend.api.devices.platform.system", return_value="Darwin"), patch(
            "backend.api.devices._build_ios_wda_launch_command",
            return_value={
                "command": [
                    "tidevice",
                    "-u",
                    "ios-1",
                    "xctest",
                    "--bundle_id",
                    "com.demo.missing.xctrunner",
                ],
                "command_source": "tidevice",
                "bundle_id": "com.demo.missing.xctrunner",
                "bundle_source": "resolved",
            },
        ), patch(
            "backend.api.devices._build_ios_wda_xcodebuild_command",
            return_value={"command": ["xcodebuild", "test"], "command_source": "xcodebuild"},
        ), patch(
            "backend.api.devices._discover_ios_wda_bundle_id",
            return_value=None,
        ), patch(
            "backend.api.devices._read_log_tail",
            return_value="No app matches bundle id",
        ), patch(
            "backend.api.devices.subprocess.Popen",
            side_effect=_popen,
        ), patch("backend.api.devices.time.sleep", return_value=None):
            payload = _launch_ios_wda_process(self.session, "ios-1")

        self.assertEqual(
            popen_commands,
            [
                ["tidevice", "-u", "ios-1", "xctest", "--bundle_id", "com.demo.missing.xctrunner"],
                ["xcodebuild", "test"],
            ],
        )
        self.assertEqual(payload["command_source"], "xcodebuild")
        self.assertEqual(payload["command_str"], "xcodebuild test")
        self.assertEqual(payload["pid"], 2002)

    def test_check_ios_wda_updates_device_status_to_idle_on_success(self):
        self._add_device("ios-1", "ios", status="WDA_DOWN")
        with patch(
            "backend.api.devices._ensure_ios_wda_ready",
            return_value={
                "healthy": True,
                "wda_url": "http://127.0.0.1:8201",
                "error": None,
                "attempted_start": True,
                "recovered_by_cleanup": False,
                "cleanup": {"killed_pids": [201, 202], "killed_count": 2},
                "startup_checks": 4,
                "start_command": "tidevice -u ios-1 xctest --bundle_id com.facebook.WebDriverAgentRunner.xctrunner",
                "start_pid": 5566,
                "start_command_source": "default",
                "start_bundle_id": "com.facebook.WebDriverAgentRunner.xctrunner",
            },
        ):
            payload = check_ios_wda("ios-1", session=self.session)

        self.assertTrue(payload["wda_healthy"])
        self.assertEqual(payload["status"], "IDLE")
        self.assertTrue(payload["attempted_start"])
        self.assertEqual(payload["cleanup_killed_count"], 2)
        self.assertEqual(payload["start_pid"], 5566)

        db_device = self.session.exec(select(Device).where(Device.serial == "ios-1")).first()
        self.assertIsNotNone(db_device)
        self.assertEqual(db_device.status, "IDLE")

    def test_check_ios_wda_updates_device_status_to_wda_down_on_failure(self):
        self._add_device("ios-1", "ios", status="IDLE")
        with patch(
            "backend.api.devices._ensure_ios_wda_ready",
            return_value={
                "healthy": False,
                "wda_url": "http://127.0.0.1:8201",
                "error": "startup timeout",
                "attempted_start": True,
                "recovered_by_cleanup": False,
                "cleanup": {"killed_pids": [], "killed_count": 0},
                "startup_checks": 10,
                "start_command": "tidevice -u ios-1 xctest --bundle_id com.facebook.WebDriverAgentRunner.xctrunner",
                "start_pid": 7788,
                "start_command_source": "default",
                "start_bundle_id": "com.facebook.WebDriverAgentRunner.xctrunner",
            },
        ):
            payload = check_ios_wda("ios-1", session=self.session)

        self.assertFalse(payload["wda_healthy"])
        self.assertEqual(payload["status"], "WDA_DOWN")
        self.assertEqual(payload["error"], "startup timeout")

        db_device = self.session.exec(select(Device).where(Device.serial == "ios-1")).first()
        self.assertIsNotNone(db_device)
        self.assertEqual(db_device.status, "WDA_DOWN")

    def test_check_ios_wda_rejects_non_ios_device(self):
        self._add_device("android-1", "android")
        with self.assertRaises(HTTPException) as context:
            check_ios_wda("android-1", session=self.session)
        self.assertEqual(context.exception.status_code, 400)
        self.assertIn("WDA 启动仅适用于 iOS 设备", str(context.exception.detail))


if __name__ == "__main__":
    unittest.main()
