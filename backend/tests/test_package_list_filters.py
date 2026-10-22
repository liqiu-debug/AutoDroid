# -*- coding: utf-8 -*-
"""安装包列表过滤：package_name + latest_only（客户端「检查更新」查询形态）。

GET /packages/?package_name=com.autodroid.runner&platform=android&latest_only=true&page=1&page_size=1
应只返回该包名的最新一条记录（含 version_name/version_code/file_size/upload_time）。
"""
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from jose import jwt
from sqlmodel import SQLModel, Session, create_engine

from backend.api import packages
from backend.core.security import ALGORITHM, SECRET_KEY
from backend.database import get_session
from backend.models import AppPackage, User


class PackageListFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.user = User(username="tester", hashed_password="x", full_name="Tester")
        self.session.add(self.user)
        self.session.commit()
        self.session.refresh(self.user)

        base = datetime(2026, 10, 1, 10, 0, 0)
        self.old_row = self._add_package(
            app_name="AutoDroid", package_name="com.autodroid.runner",
            version_name="6.7.0", version_code="3810", is_latest=False, upload_time=base,
        )
        self.latest_row = self._add_package(
            app_name="AutoDroid", package_name="com.autodroid.runner",
            version_name="6.8.0", version_code="3830", is_latest=True,
            upload_time=base + timedelta(days=1),
        )
        self.other_row = self._add_package(
            app_name="智家", package_name="com.haier.uhome.uplus",
            version_name="1.0.0", version_code="100", is_latest=True,
            upload_time=base + timedelta(days=2),
        )

        app = FastAPI()
        app.include_router(packages.router)
        app.dependency_overrides[get_session] = lambda: self.session
        self.client = TestClient(app)
        self.token = jwt.encode({"sub": "tester"}, SECRET_KEY, algorithm=ALGORITHM)

    def tearDown(self) -> None:
        self.session.close()
        self.temp_dir.cleanup()

    def _add_package(self, **kwargs) -> AppPackage:
        row = AppPackage(
            platform="android",
            file_path="uploads/apps/demo.apk",
            file_size=1.5,
            uploader_id=self.user.id,
            uploader_name="Tester",
            **kwargs,
        )
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        return row

    def _get(self, params: str) -> dict:
        response = self.client.get(
            f"/?{params}",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_latest_only_filter_returns_single_latest_record(self):
        data = self._get("package_name=com.autodroid.runner&platform=android&latest_only=true&page=1&page_size=1")
        self.assertEqual(data["total"], 1)
        self.assertEqual(len(data["items"]), 1)
        item = data["items"][0]
        self.assertEqual(item["id"], self.latest_row.id)
        self.assertEqual(item["version_name"], "6.8.0")
        self.assertEqual(item["version_code"], "3830")
        self.assertIn("file_size", item)
        self.assertIn("upload_time", item)

    def test_package_name_filter_excludes_other_apps(self):
        data = self._get("package_name=com.autodroid.runner&latest_only=true")
        ids = [item["id"] for item in data["items"]]
        self.assertNotIn(self.other_row.id, ids)

    def test_latest_only_omitted_returns_history(self):
        data = self._get("package_name=com.autodroid.runner")
        self.assertEqual(data["total"], 2)
        ids = [item["id"] for item in data["items"]]
        self.assertIn(self.old_row.id, ids)
        self.assertIn(self.latest_row.id, ids)

    def test_unknown_package_name_returns_empty(self):
        data = self._get("package_name=com.nothing.here&latest_only=true")
        self.assertEqual(data["total"], 0)
        self.assertEqual(data["items"], [])

    def test_filters_require_auth(self):
        response = self.client.get("/?package_name=com.autodroid.runner&latest_only=true")
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
