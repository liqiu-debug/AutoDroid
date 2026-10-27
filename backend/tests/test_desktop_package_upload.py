# -*- coding: utf-8 -*-
"""桌面包（AutoDroid 桌面客户端）上传与「检查更新」查询。

更新源 = 平台「App 包管理」，与 Android 客户端同一通道：
- .exe/.msi -> platform "windows"、.dmg -> platform "macos"（仅此三类走桌面通道）
- package_name 固定 autodroid-desktop、app_name 固定 AutoDroid
- version_name = 文件名首个 semver（AutoDroid Setup 1.2.3.exe -> 1.2.3）
- version_code = 去点数字（1.2.3 -> 123）
- 文件名解析不到版本 -> 400 明确提示，不落库
- is_latest 按 platform+package_name 独立降级（win/mac 各自最新）
- 列表 package_name + latest_only 即客户端检查更新自查询形态
- 旧 apk/ipa 行为不回归
"""
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine, select

from backend.api import packages
from backend.core.security import ALGORITHM, SECRET_KEY
from backend.database import get_session
from backend.models import AppPackage, User

APK_INFO = {
    "app_name": "Demo",
    "package_name": "com.example.demo",
    "version_name": "1.0.0",
    "version_code": "100",
}
IPA_INFO = {
    "app_name": "Demo iOS",
    "package_name": "com.example.demo.ios",
    "version_name": "2.0.0",
    "version_code": "200",
    "signing_type": "adhoc",
}


class DesktopPackageUploadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.upload_dir = Path(self.temp_dir.name) / "uploads" / "apps"
        self.chunk_dir = self.upload_dir / ".chunks"
        self.chunk_dir.mkdir(parents=True)

        self.parse_apk_mock = MagicMock(return_value=dict(APK_INFO))
        self.parse_ipa_mock = MagicMock(return_value=dict(IPA_INFO))
        self.patchers = [
            patch.object(packages, "UPLOAD_DIR", self.upload_dir),
            patch.object(packages, "CHUNK_UPLOAD_DIR", self.chunk_dir),
            patch.object(packages, "parse_apk_info", self.parse_apk_mock),
            patch.object(packages, "parse_ipa_info", self.parse_ipa_mock),
        ]
        for patcher in self.patchers:
            patcher.start()

        # StaticPool：内存库共享单一连接，TestClient 工作线程里 commit/refresh
        # 重新取连接时不会拿到另一个空库
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.user = User(username="tester", hashed_password="x", full_name="Tester")
        self.session.add(self.user)
        self.session.commit()
        self.session.refresh(self.user)

        app = FastAPI()
        app.include_router(packages.router)
        app.dependency_overrides[get_session] = lambda: self.session
        self.client = TestClient(app)
        self.token = jwt.encode({"sub": "tester"}, SECRET_KEY, algorithm=ALGORITHM)

    def tearDown(self) -> None:
        self.session.close()
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp_dir.cleanup()

    # ---- helpers ----

    def _auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def _upload(self, filename: str, data: bytes = b"pkg-bytes"):
        return self.client.post(
            "/upload",
            files={"file": (filename, BytesIO(data), "application/octet-stream")},
            headers=self._auth_headers(),
        )

    def _list(self, params: str) -> dict:
        response = self.client.get(f"/?{params}", headers=self._auth_headers())
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _rows(self):
        return self.session.exec(
            select(AppPackage).order_by(AppPackage.id)
        ).all()

    # ---- 扩展名 -> 平台 / 文件名版本解析 ----

    def test_upload_exe_maps_to_windows_with_filename_version(self):
        response = self._upload("AutoDroid Setup 1.2.3.exe")
        self.assertEqual(response.status_code, 200, response.text)
        item = response.json()
        self.assertEqual(item["platform"], "windows")
        self.assertEqual(item["package_name"], "autodroid-desktop")
        self.assertEqual(item["app_name"], "AutoDroid")
        self.assertEqual(item["version_name"], "1.2.3")
        self.assertEqual(item["version_code"], "123")
        self.assertTrue(item["is_latest"])
        self.assertIn("file_size", item)
        # 桌面包不走 apk/ipa 二进制解析
        self.parse_apk_mock.assert_not_called()
        self.parse_ipa_mock.assert_not_called()

    def test_upload_msi_and_dmg_platform_mapping(self):
        msi = self._upload("AutoDroid-1.4.0.msi")
        self.assertEqual(msi.status_code, 200, msi.text)
        self.assertEqual(msi.json()["platform"], "windows")
        self.assertEqual(msi.json()["version_name"], "1.4.0")

        dmg = self._upload("AutoDroid-1.2.3-arm64.dmg")
        self.assertEqual(dmg.status_code, 200, dmg.text)
        body = dmg.json()
        self.assertEqual(body["platform"], "macos")
        self.assertEqual(body["package_name"], "autodroid-desktop")
        self.assertEqual(body["version_name"], "1.2.3")
        self.assertEqual(body["version_code"], "123")
        self.parse_apk_mock.assert_not_called()
        self.parse_ipa_mock.assert_not_called()

    def test_version_takes_first_semver_and_strips_dots(self):
        response = self._upload("AutoDroid Setup v1.2.3.4.exe")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["version_name"], "1.2.3.4")
        self.assertEqual(body["version_code"], "1234")

    def test_filename_without_version_is_rejected(self):
        response = self._upload("AutoDroid Setup.exe")
        self.assertEqual(response.status_code, 400, response.text)
        detail = response.json()["detail"]
        self.assertIn("版本号", detail)
        self.assertIn("1.2.3", detail)
        # 不落库、不留残留文件
        self.assertEqual(self._rows(), [])
        self.assertEqual(list(self.upload_dir.glob("*.exe")), [])

    def test_unsupported_extension_rejects_with_updated_message(self):
        response = self._upload("demo.zip")
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["detail"], "仅支持 .apk、.ipa、.exe、.msi 或 .dmg 文件")

    # ---- is_latest 语义 ----

    def test_is_latest_is_independent_per_platform(self):
        """win/mac 各自最新：上传 win 新版不应影响 mac 的最新标记。"""
        self.assertEqual(self._upload("AutoDroid Setup 1.2.3.exe").status_code, 200)
        self.assertEqual(self._upload("AutoDroid-1.2.3-arm64.dmg").status_code, 200)
        self.assertEqual(self._upload("AutoDroid Setup 1.3.0.exe").status_code, 200)

        rows = self._rows()
        by_key = {(row.platform, row.version_name): row for row in rows}
        self.assertFalse(by_key[("windows", "1.2.3")].is_latest)
        self.assertTrue(by_key[("windows", "1.3.0")].is_latest)
        self.assertTrue(by_key[("macos", "1.2.3")].is_latest)

        data = self._list("package_name=autodroid-desktop&latest_only=true")
        self.assertEqual(data["total"], 2)
        latest = {(item["platform"], item["version_name"]) for item in data["items"]}
        self.assertEqual(latest, {("windows", "1.3.0"), ("macos", "1.2.3")})

    def test_upload_demotes_previous_latest_of_same_platform(self):
        self.assertEqual(self._upload("AutoDroid Setup 1.2.3.exe").status_code, 200)
        self.assertEqual(self._upload("AutoDroid Setup 1.3.0.exe").status_code, 200)

        rows = self._rows()
        self.assertEqual(len(rows), 2)
        old, new = rows
        self.assertFalse(old.is_latest)
        self.assertTrue(new.is_latest)

    # ---- 客户端「检查更新」查询形态 ----

    def test_list_latest_only_returns_update_check_fields(self):
        self._upload("AutoDroid Setup 1.2.3.exe")
        self._upload("AutoDroid Setup 1.3.0.exe")

        data = self._list("package_name=autodroid-desktop&latest_only=true")
        self.assertEqual(data["total"], 1)
        self.assertEqual(len(data["items"]), 1)
        item = data["items"][0]
        self.assertEqual(item["version_name"], "1.3.0")
        self.assertEqual(item["platform"], "windows")
        self.assertIn("id", item)
        self.assertIn("file_size", item)

        history = self._list("package_name=autodroid-desktop")
        self.assertEqual(history["total"], 2)

    def test_list_platform_filter_accepts_desktop_platforms(self):
        self._upload("AutoDroid Setup 1.2.3.exe")
        self._upload("AutoDroid-1.2.3-arm64.dmg")

        windows = self._list("package_name=autodroid-desktop&platform=windows&latest_only=true")
        self.assertEqual(windows["total"], 1)
        self.assertEqual(windows["items"][0]["platform"], "windows")

        macos = self._list("package_name=autodroid-desktop&platform=macos&latest_only=true")
        self.assertEqual(macos["total"], 1)
        self.assertEqual(macos["items"][0]["platform"], "macos")

        response = self.client.get(
            "/?package_name=autodroid-desktop&platform=linux",
            headers=self._auth_headers(),
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("windows", response.json()["detail"])

    def test_desktop_download_via_query_token(self):
        """客户端下载 URL：/api/packages/{id}/download?token=<token>。"""
        self._upload("AutoDroid Setup 1.2.3.exe", data=b"exe-bytes")
        pkg = self._rows()[0]
        local_file = self.upload_dir / Path(pkg.file_path).name
        self.assertTrue(local_file.exists())

        with patch.object(packages, "_resolve_package_file_path", return_value=local_file):
            response = self.client.get(f"/{pkg.id}/download?token={self.token}")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.content, b"exe-bytes")
        self.assertIn("AutoDroid_1.2.3.exe", response.headers.get("content-disposition", ""))
        self.assertEqual(response.headers.get("content-type"), "application/octet-stream")

    # ---- 分片上传同样按原始文件名解析版本 ----

    def test_chunked_desktop_upload_uses_original_filename_version(self):
        session_response = self.client.post(
            "/upload-sessions",
            json={
                "filename": "AutoDroid Setup 2.0.0.exe",
                "file_size": 4,
                "chunk_size": packages.PACKAGE_UPLOAD_CHUNK_SIZE,
                "total_chunks": 1,
            },
            headers=self._auth_headers(),
        )
        self.assertEqual(session_response.status_code, 200, session_response.text)
        upload_id = session_response.json()["upload_id"]

        chunk_response = self.client.post(
            f"/upload-sessions/{upload_id}/chunks/0",
            files={"file": ("chunk.part", BytesIO(b"data"), "application/octet-stream")},
            headers=self._auth_headers(),
        )
        self.assertEqual(chunk_response.status_code, 200, chunk_response.text)

        complete_response = self.client.post(
            f"/upload-sessions/{upload_id}/complete",
            headers=self._auth_headers(),
        )
        self.assertEqual(complete_response.status_code, 200, complete_response.text)
        body = complete_response.json()
        self.assertEqual(body["platform"], "windows")
        self.assertEqual(body["package_name"], "autodroid-desktop")
        self.assertEqual(body["version_name"], "2.0.0")
        self.assertEqual(body["version_code"], "200")

    def test_chunked_desktop_upload_without_version_is_rejected(self):
        session_response = self.client.post(
            "/upload-sessions",
            json={
                "filename": "AutoDroid Setup.exe",
                "file_size": 4,
                "chunk_size": packages.PACKAGE_UPLOAD_CHUNK_SIZE,
                "total_chunks": 1,
            },
            headers=self._auth_headers(),
        )
        self.assertEqual(session_response.status_code, 200, session_response.text)
        upload_id = session_response.json()["upload_id"]

        self.client.post(
            f"/upload-sessions/{upload_id}/chunks/0",
            files={"file": ("chunk.part", BytesIO(b"data"), "application/octet-stream")},
            headers=self._auth_headers(),
        )
        complete_response = self.client.post(
            f"/upload-sessions/{upload_id}/complete",
            headers=self._auth_headers(),
        )
        self.assertEqual(complete_response.status_code, 400, complete_response.text)
        self.assertIn("版本号", complete_response.json()["detail"])
        self.assertEqual(self._rows(), [])

    # ---- 旧 apk/ipa 行为不回归 ----

    def test_apk_upload_still_uses_binary_parser(self):
        self.parse_apk_mock.return_value = dict(APK_INFO)
        response = self._upload("demo.apk")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["platform"], "android")
        self.assertEqual(body["package_name"], "com.example.demo")
        self.assertEqual(body["app_name"], "Demo")
        self.assertEqual(body["version_name"], "1.0.0")
        self.assertEqual(body["version_code"], "100")
        self.parse_apk_mock.assert_called_once()
        self.parse_ipa_mock.assert_not_called()

    def test_ipa_upload_still_uses_binary_parser(self):
        response = self._upload("demo.ipa")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["platform"], "ios")
        self.assertEqual(body["package_name"], "com.example.demo.ios")
        self.assertEqual(body["version_name"], "2.0.0")
        self.parse_ipa_mock.assert_called_once()
        self.parse_apk_mock.assert_not_called()

    def test_apk_is_latest_demotion_not_affected_by_desktop_packages(self):
        """桌面降级不串台：win 新版不影响 android 同名降级逻辑，反之亦然。"""
        self._upload("demo.apk")  # android 1.0.0
        self._upload("AutoDroid Setup 1.2.3.exe")
        self.parse_apk_mock.return_value = {
            "app_name": "Demo",
            "package_name": "com.example.demo",
            "version_name": "1.1.0",
            "version_code": "110",
        }
        self._upload("demo.apk")  # android 1.1.0 降级 1.0.0

        rows = self._rows()
        android_rows = [row for row in rows if row.platform == "android"]
        self.assertEqual(len(android_rows), 2)
        by_version = {row.version_name: row for row in android_rows}
        self.assertFalse(by_version["1.0.0"].is_latest)
        self.assertTrue(by_version["1.1.0"].is_latest)

        desktop_rows = [row for row in rows if row.platform == "windows"]
        self.assertEqual(len(desktop_rows), 1)
        self.assertTrue(desktop_rows[0].is_latest)


if __name__ == "__main__":
    unittest.main()
