import unittest
from unittest.mock import patch

from fastapi import HTTPException
from sqlmodel import SQLModel, Session, create_engine, select

from backend.api.devices import (
    _build_ios_wda_launch_command,
    _build_ios_wda_xcodebuild_command,
    _ensure_ios_wda_ready,
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

    def test_build_launch_command_prefers_xcodebuild_on_macos(self):
        with patch("backend.api.devices.platform.system", return_value="Darwin"), patch(
            "backend.api.devices._build_ios_wda_xcodebuild_command",
            return_value={"command": ["xcodebuild"], "command_source": "xcodebuild"},
        ) as xcodebuild_mock:
            payload = _build_ios_wda_launch_command(self.session, "ios-1")
        xcodebuild_mock.assert_called_once_with(self.session, "ios-1")
        self.assertEqual(payload["command_source"], "xcodebuild")

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
        with patch("backend.api.devices.get_setting_value") as setting_mock, patch(
            "backend.api.devices._discover_wda_xcodeproj_path",
            return_value="/tmp/WebDriverAgent.xcodeproj",
        ), patch("backend.api.devices.os.path.exists", return_value=True):
            def _side_effect(_, key):
                if key == "ios_wda_scheme.ios-1":
                    return "WebDriverAgentRunner"
                return None

            setting_mock.side_effect = _side_effect
            payload = _build_ios_wda_xcodebuild_command(self.session, "ios-1")

        self.assertEqual(payload["command_source"], "xcodebuild")
        self.assertIn("-project", payload["command"])
        self.assertIn("/tmp/WebDriverAgent.xcodeproj", payload["command"])
        self.assertIn("-destination", payload["command"])
        self.assertIn("id=ios-1", payload["command"])
        self.assertIn("test", payload["command"])

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
