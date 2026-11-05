# -*- coding: utf-8 -*-
"""安装包「更新说明」（changelog）字段。

桌面客户端「检查更新」对话框展示的更新内容来源：
- 直传 /upload 用 multipart 表单字段 changelog（可选）
- 分片上传在创建会话时带 changelog，随 meta.json 落盘，complete 时写入记录
- PATCH /{package_id} 可单独改说明（改文案不用重传安装包）
- 列表（客户端检查更新的查询形态）返回 changelog 字段
"""
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine, select

from backend.api import packages
from backend.core.security import ALGORITHM, SECRET_KEY
from backend.database import get_session
from backend.models import AppPackage, User


class PackageChangelogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.upload_dir = Path(self.temp_dir.name) / "uploads" / "apps"
        self.chunk_dir = self.upload_dir / ".chunks"
        self.chunk_dir.mkdir(parents=True)

        self.patchers = [
            patch.object(packages, "UPLOAD_DIR", self.upload_dir),
            patch.object(packages, "CHUNK_UPLOAD_DIR", self.chunk_dir),
        ]
        for patcher in self.patchers:
            patcher.start()

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

    def _auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def _upload(self, filename: str, changelog: str = None, data: bytes = b"pkg-bytes"):
        form = {}
        if changelog is not None:
            form["changelog"] = changelog
        return self.client.post(
            "/upload",
            files={"file": (filename, BytesIO(data), "application/octet-stream")},
            data=form,
            headers=self._auth_headers(),
        )

    def _rows(self):
        return self.session.exec(select(AppPackage).order_by(AppPackage.id)).all()

    # ---- 直传 ----

    def test_direct_upload_stores_changelog(self):
        response = self._upload("AutoDroid Setup 1.3.0.exe", changelog="- 新增更新提示\n- 修复假断连")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["changelog"], "- 新增更新提示\n- 修复假断连")
        self.assertEqual(self._rows()[0].changelog, "- 新增更新提示\n- 修复假断连")

    def test_direct_upload_without_changelog_defaults_empty(self):
        response = self._upload("AutoDroid Setup 1.3.0.exe")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["changelog"], "")
        self.assertEqual(self._rows()[0].changelog, "")

    def test_direct_upload_changelog_too_long_is_rejected(self):
        response = self._upload(
            "AutoDroid Setup 1.3.0.exe",
            changelog="x" * (packages.MAX_CHANGELOG_LENGTH + 1),
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("更新说明过长", response.json()["detail"])
        self.assertEqual(self._rows(), [])

    # ---- 分片上传 ----

    def _chunked_upload(self, filename: str, payload_extra: dict = None, data: bytes = b"data"):
        payload = {
            "filename": filename,
            "file_size": len(data),
            "chunk_size": packages.PACKAGE_UPLOAD_CHUNK_SIZE,
            "total_chunks": 1,
        }
        if payload_extra:
            payload.update(payload_extra)
        session_response = self.client.post(
            "/upload-sessions", json=payload, headers=self._auth_headers()
        )
        self.assertEqual(session_response.status_code, 200, session_response.text)
        upload_id = session_response.json()["upload_id"]
        chunk_response = self.client.post(
            f"/upload-sessions/{upload_id}/chunks/0",
            files={"file": ("chunk.part", BytesIO(data), "application/octet-stream")},
            headers=self._auth_headers(),
        )
        self.assertEqual(chunk_response.status_code, 200, chunk_response.text)
        return self.client.post(
            f"/upload-sessions/{upload_id}/complete", headers=self._auth_headers()
        )

    def test_chunked_upload_carries_changelog_from_session_meta(self):
        complete = self._chunked_upload(
            "AutoDroid Setup 1.4.0.exe", payload_extra={"changelog": "修复 iOS 安装"}
        )
        self.assertEqual(complete.status_code, 200, complete.text)
        self.assertEqual(complete.json()["changelog"], "修复 iOS 安装")
        self.assertEqual(self._rows()[0].changelog, "修复 iOS 安装")

    def test_chunked_upload_without_changelog_defaults_empty(self):
        complete = self._chunked_upload("AutoDroid Setup 1.4.0.exe")
        self.assertEqual(complete.status_code, 200, complete.text)
        self.assertEqual(complete.json()["changelog"], "")

    def test_chunked_session_rejects_overlong_changelog(self):
        response = self.client.post(
            "/upload-sessions",
            json={
                "filename": "AutoDroid Setup 1.4.0.exe",
                "file_size": 4,
                "chunk_size": packages.PACKAGE_UPLOAD_CHUNK_SIZE,
                "total_chunks": 1,
                "changelog": "x" * (packages.MAX_CHANGELOG_LENGTH + 1),
            },
            headers=self._auth_headers(),
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("更新说明过长", response.json()["detail"])

    # ---- PATCH 单独改说明 ----

    def test_patch_updates_changelog_without_touching_package(self):
        created = self._upload(
            "AutoDroid Setup 1.3.0.exe", changelog="初稿", data=b"exe-bytes"
        ).json()
        row_before = self._rows()[0]
        file_path_before = row_before.file_path

        response = self.client.patch(
            f"/{created['id']}",
            json={"changelog": "终稿：- 新增更新提示"},
            headers=self._auth_headers(),
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["changelog"], "终稿：- 新增更新提示")
        self.assertEqual(body["version_name"], "1.3.0")
        self.assertEqual(body["file_path"], file_path_before)

        row_after = self._rows()[0]
        self.assertEqual(row_after.changelog, "终稿：- 新增更新提示")
        self.assertEqual(row_after.version_name, "1.3.0")
        self.assertEqual(row_after.file_path, file_path_before)

    def test_patch_can_clear_changelog(self):
        created = self._upload("AutoDroid Setup 1.3.0.exe", changelog="有说明").json()
        response = self.client.patch(
            f"/{created['id']}", json={}, headers=self._auth_headers()
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["changelog"], "")

    def test_patch_missing_package_is_404(self):
        response = self.client.patch(
            "/9999", json={"changelog": "x"}, headers=self._auth_headers()
        )
        self.assertEqual(response.status_code, 404, response.text)

    def test_patch_overlong_changelog_is_rejected(self):
        created = self._upload("AutoDroid Setup 1.3.0.exe", changelog="短说明").json()
        response = self.client.patch(
            f"/{created['id']}",
            json={"changelog": "x" * (packages.MAX_CHANGELOG_LENGTH + 1)},
            headers=self._auth_headers(),
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self._rows()[0].changelog, "短说明")

    # ---- 客户端检查更新查询形态 ----

    def test_latest_only_list_exposes_changelog_for_update_check(self):
        self._upload("AutoDroid Setup 1.3.0.exe", changelog="旧版说明")
        self._upload("AutoDroid-1.4.0-arm64.dmg", changelog="- 新增启动检查更新提示")

        data = self.client.get(
            "/?package_name=autodroid-desktop&latest_only=true",
            headers=self._auth_headers(),
        ).json()
        macos = [item for item in data["items"] if item["platform"] == "macos"][0]
        self.assertEqual(macos["changelog"], "- 新增启动检查更新提示")


if __name__ == "__main__":
    unittest.main()
