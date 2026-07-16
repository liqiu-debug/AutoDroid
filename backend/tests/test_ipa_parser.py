import os
import plistlib
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from backend.utils.ipa_parser import (
    IpaParseError,
    extract_app_bundle,
    parse_ipa_info,
    validate_ipa_for_device,
)


class IpaParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.ipa_path = Path(self.temp_dir.name) / "Demo.ipa"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def _profile(**overrides):
        profile = {
            "ExpirationDate": datetime.now(timezone.utc) + timedelta(days=30),
            "ProvisionedDevices": ["ios-udid-1"],
            "Entitlements": {
                "application-identifier": "TEAM123.com.example.demo",
                "get-task-allow": False,
            },
        }
        profile.update(overrides)
        return profile

    def _write_ipa(self, *, bundle_id="com.example.demo", minimum_os="17.0", extra_entries=None):
        info = {
            "CFBundleIdentifier": bundle_id,
            "CFBundleDisplayName": "Demo iOS",
            "CFBundleShortVersionString": "2.3.0",
            "CFBundleVersion": "230",
            "MinimumOSVersion": minimum_os,
        }
        with zipfile.ZipFile(self.ipa_path, "w") as zf:
            zf.writestr("Payload/Demo.app/Info.plist", plistlib.dumps(info))
            zf.writestr("Payload/Demo.app/embedded.mobileprovision", b"signed-profile")
            executable = zipfile.ZipInfo("Payload/Demo.app/Demo")
            executable.create_system = 3
            executable.external_attr = (0o100755 << 16)
            zf.writestr(executable, b"binary")
            for name, data in extra_entries or []:
                zf.writestr(name, data)

    def test_parse_valid_adhoc_ipa(self):
        self._write_ipa()
        with patch("backend.utils.ipa_parser._decode_mobileprovision", return_value=self._profile()):
            result = parse_ipa_info(str(self.ipa_path))

        self.assertEqual(result["app_name"], "Demo iOS")
        self.assertEqual(result["package_name"], "com.example.demo")
        self.assertEqual(result["version_name"], "2.3.0")
        self.assertEqual(result["version_code"], "230")
        self.assertEqual(result["signing_type"], "adhoc")

    def test_validate_target_udid_and_minimum_os(self):
        self._write_ipa(minimum_os="18.0")
        with patch("backend.utils.ipa_parser._decode_mobileprovision", return_value=self._profile()):
            with self.assertRaisesRegex(IpaParseError, "UDID"):
                validate_ipa_for_device(str(self.ipa_path), "other-device", "18.0")
            with self.assertRaisesRegex(IpaParseError, "最低要求"):
                validate_ipa_for_device(str(self.ipa_path), "ios-udid-1", "17.6")

    def test_rejects_expired_and_non_adhoc_profiles(self):
        self._write_ipa()
        expired = self._profile(ExpirationDate=datetime.now(timezone.utc) - timedelta(days=1))
        with patch("backend.utils.ipa_parser._decode_mobileprovision", return_value=expired):
            with self.assertRaisesRegex(IpaParseError, "已过期"):
                parse_ipa_info(str(self.ipa_path))

        development = self._profile(
            Entitlements={
                "application-identifier": "TEAM123.com.example.demo",
                "get-task-allow": True,
            }
        )
        with patch("backend.utils.ipa_parser._decode_mobileprovision", return_value=development):
            with self.assertRaisesRegex(IpaParseError, "Development"):
                parse_ipa_info(str(self.ipa_path))

    def test_rejects_multiple_apps_and_path_traversal(self):
        second_info = plistlib.dumps({"CFBundleIdentifier": "com.example.other"})
        self._write_ipa(extra_entries=[("Payload/Other.app/Info.plist", second_info)])
        with self.assertRaisesRegex(IpaParseError, "只能包含一个"):
            parse_ipa_info(str(self.ipa_path))

        self._write_ipa(extra_entries=[("../escape", b"bad")])
        with self.assertRaisesRegex(IpaParseError, "越界"):
            extract_app_bundle(str(self.ipa_path), Path(self.temp_dir.name) / "extract")

    def test_extract_preserves_executable_mode(self):
        self._write_ipa()
        destination = Path(self.temp_dir.name) / "extract"
        app_path = extract_app_bundle(str(self.ipa_path), destination)

        executable = app_path / "Demo"
        self.assertEqual(app_path.name, "Demo.app")
        self.assertTrue(executable.exists())
        self.assertTrue(os.stat(executable).st_mode & 0o100)


if __name__ == "__main__":
    unittest.main()
