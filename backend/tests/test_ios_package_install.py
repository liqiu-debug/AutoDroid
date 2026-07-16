import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException
from sqlmodel import SQLModel, Session, create_engine

from backend.api import packages
from backend.models import AppPackage, Device, User
from backend.utils.ipa_parser import IpaParseError


class IosPackageInstallTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.ipa_path = Path(self.temp_dir.name) / "demo.ipa"
        self.ipa_path.write_bytes(b"fake-ipa")

        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.user = User(username="tester", hashed_password="x")
        self.package = AppPackage(
            platform="ios",
            app_name="Demo iOS",
            package_name="com.example.demo",
            version_name="2.0",
            version_code="200",
            file_path="uploads/apps/demo.ipa",
        )
        self.device = Device(
            serial="ios-udid-1",
            platform="ios",
            model="iPhone",
            os_version="26.1",
            status="IDLE",
        )
        self.session.add(self.user)
        self.session.add(self.package)
        self.session.add(self.device)
        self.session.commit()
        self.session.refresh(self.user)
        self.session.refresh(self.package)

    def tearDown(self) -> None:
        self.session.close()
        self.temp_dir.cleanup()

    async def test_devicectl_install_and_verification_success(self):
        def fake_extract(_ipa_path, destination):
            app_path = Path(destination) / "Demo.app"
            app_path.mkdir(parents=True)
            (app_path / "Info.plist").write_bytes(b"plist")
            return app_path

        process_results = [
            {"returncode": 0, "stdout": "/usr/bin/devicectl", "stderr": ""},
            {"returncode": 0, "stdout": "installed", "stderr": ""},
            {"returncode": 0, "stdout": "listed", "stderr": ""},
        ]
        json_results = [
            {"result": {"installedApplication": {"bundleIdentifier": "com.example.demo"}}},
            {"result": {"apps": [{"bundleIdentifier": "com.example.demo", "version": "2.0"}]}},
        ]

        with patch.object(packages, "_resolve_package_file_path", return_value=self.ipa_path), patch.object(
            packages,
            "validate_ipa_for_device",
            return_value={"signing_type": "adhoc"},
        ), patch.object(packages, "extract_app_bundle", side_effect=fake_extract), patch.object(
            packages,
            "_run_process",
            new=AsyncMock(side_effect=process_results),
        ), patch.object(packages, "_load_devicectl_json", side_effect=json_results):
            result = await packages.install_ios_app_package_to_device(
                session=self.session,
                package_id=self.package.id,
                serial=self.device.serial,
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["platform"], "ios")
        self.assertEqual(result["installer"], "devicectl")
        self.assertEqual(result["signing_type"], "adhoc")

    async def test_provisioning_failure_is_actionable(self):
        with patch.object(packages, "_resolve_package_file_path", return_value=self.ipa_path), patch.object(
            packages,
            "validate_ipa_for_device",
            side_effect=IpaParseError("目标设备 UDID 未包含在 IPA 的 Ad Hoc 描述文件中"),
        ):
            with self.assertRaises(HTTPException) as context:
                await packages.install_ios_app_package_to_device(
                    session=self.session,
                    package_id=self.package.id,
                    serial=self.device.serial,
                )

        self.assertEqual(context.exception.status_code, 400)
        self.assertIn("UDID", context.exception.detail)

    async def test_managed_install_restores_wda_down_status_and_releases_lease(self):
        self.device.status = "WDA_DOWN"
        self.session.add(self.device)
        self.session.commit()

        lease = Mock()
        limiter = Mock()
        limiter.acquire_lease.return_value = lease
        install_result = {
            "success": True,
            "platform": "ios",
            "msg": "安装成功",
        }
        with patch.object(packages, "get_execution_limiter", return_value=limiter), patch.object(
            packages,
            "install_ios_app_package_to_device",
            new=AsyncMock(return_value=install_result),
        ):
            result = await packages.install_managed_package_to_device(
                session=self.session,
                package_id=self.package.id,
                serial=self.device.serial,
                current_user=self.user,
            )

        self.assertEqual(result, install_result)
        self.session.refresh(self.device)
        self.assertEqual(self.device.status, "WDA_DOWN")
        lease.release.assert_called_once_with()

    async def test_package_device_platform_mismatch_is_rejected(self):
        android = Device(
            serial="android-1",
            platform="android",
            model="Pixel",
            os_version="16",
            status="IDLE",
        )
        self.session.add(android)
        self.session.commit()

        with self.assertRaises(HTTPException) as context:
            await packages.install_managed_package_to_device(
                session=self.session,
                package_id=self.package.id,
                serial=android.serial,
                current_user=self.user,
            )
        self.assertEqual(context.exception.status_code, 400)
        self.assertIn("平台", context.exception.detail)


if __name__ == "__main__":
    unittest.main()
