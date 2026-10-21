# -*- coding: utf-8 -*-
"""安装包下载认证：浏览器 <a href> 直接触发下载只能带 ?token= 查询参数。

回归场景（表现为浏览器「无法从网站上提取文件」）：
- 无 Authorization 头 + ?token=<jwt> 必须下载成功（修复前服务端返回 401 JSON，
  浏览器拿不到文件）
- 完全无凭证仍返回 401；Authorization 头路径保持不变
"""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from jose import jwt
from sqlmodel import SQLModel, Session, create_engine
from starlette.requests import Request

from backend.api import packages
from backend.api.deps import get_current_user_allow_query_token
from backend.core.security import ALGORITHM, SECRET_KEY
from backend.database import get_session
from backend.models import AppPackage, User


def _make_request(query: str = "") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/1/download",
            "query_string": query.encode(),
            "headers": [],
        }
    )


class PackageDownloadAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.package_file = Path(self.temp_dir.name) / "demo.apk"
        self.package_file.write_bytes(b"apk-bytes")

        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.user = User(username="tester", hashed_password="x", full_name="Tester")
        self.session.add(self.user)
        self.session.commit()
        self.session.refresh(self.user)
        self.pkg = AppPackage(
            platform="android",
            app_name="Demo",
            package_name="com.example.demo",
            version_name="1.0.0",
            version_code="100",
            file_path="uploads/apps/demo.apk",
            file_size=0.000009,
            uploader_id=self.user.id,
            uploader_name="Tester",
        )
        self.session.add(self.pkg)
        self.session.commit()
        self.session.refresh(self.pkg)

        app = FastAPI()
        app.include_router(packages.router)
        app.dependency_overrides[get_session] = lambda: self.session
        self.client = TestClient(app)
        self.token = jwt.encode({"sub": "tester"}, SECRET_KEY, algorithm=ALGORITHM)
        self.path = f"/{self.pkg.id}/download"

    def tearDown(self) -> None:
        self.session.close()
        self.temp_dir.cleanup()

    def _download(self, *, query_token: str = "", headers: dict = None):
        url = self.path + (f"?token={query_token}" if query_token else "")
        with patch.object(packages, "_resolve_package_file_path", return_value=self.package_file):
            return self.client.get(url, headers=headers or {})

    def test_query_token_allows_browser_download(self):
        """修复点：无 Authorization 头时接受 ?token=（浏览器直下场景）。"""
        response = self._download(query_token=self.token)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.content, b"apk-bytes")

    def test_header_token_still_works(self):
        response = self._download(headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(response.status_code, 200, response.text)

    def test_missing_credentials_returns_401(self):
        response = self._download()
        self.assertEqual(response.status_code, 401)

    def test_invalid_query_token_returns_401(self):
        response = self._download(query_token="not-a-jwt")
        self.assertEqual(response.status_code, 401)

    def test_dependency_resolves_user_from_query_token(self):
        user = asyncio.run(
            get_current_user_allow_query_token(
                request=_make_request(f"token={self.token}"),
                token=None,
                session=self.session,
            )
        )
        self.assertEqual(user.username, "tester")
