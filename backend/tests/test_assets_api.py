import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine

from backend.api import assets, deps
from backend.artifact_store import resolve_storage_key, store_text
from backend.database import get_session
from backend.models import User


class AssetsApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.user = User(username="asset-reader", hashed_password="x", is_active=True)
        self.session.add(self.user)
        self.session.commit()
        self.path_patch = patch(
            "backend.artifact_store.project_path",
            side_effect=lambda *parts: self.root.joinpath(*parts),
        )
        self.path_patch.start()
        self.disk_patch = patch(
            "backend.artifact_store.shutil.disk_usage",
            return_value=shutil._ntuple_diskusage(1_000_000, 100_000, 900_000),
        )
        self.disk_patch.start()

        self.app = FastAPI()
        self.app.include_router(assets.router, prefix="/api/assets")
        self.app.dependency_overrides[get_session] = lambda: self.session
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.client.close()
        self.disk_patch.stop()
        self.path_patch.stop()
        self.session.close()
        self.temp.cleanup()

    def _authenticate(self) -> None:
        self.app.dependency_overrides[deps.get_current_active_user] = lambda: self.user

    def test_download_requires_authentication(self):
        asset = store_text(self.session, "secret")
        response = self.client.get(f"/api/assets/{asset.id}")
        self.assertIn(response.status_code, {401, 403})

    def test_transparent_download_etag_and_ranges(self):
        self._authenticate()
        body = b"0123456789"
        asset = store_text(self.session, body.decode("ascii"), media_type="text/plain", suffix="txt")

        full = self.client.get(f"/api/assets/{asset.id}")
        self.assertEqual(full.status_code, 200)
        self.assertEqual(full.content, body)
        self.assertEqual(full.headers["accept-ranges"], "bytes")
        self.assertEqual(full.headers["x-asset-stored-encoding"], "gzip")

        cached = self.client.get(
            f"/api/assets/{asset.id}",
            headers={"If-None-Match": full.headers["etag"]},
        )
        self.assertEqual(cached.status_code, 304)

        selected = self.client.get(
            f"/api/assets/{asset.id}",
            headers={"Range": "bytes=2-5"},
        )
        self.assertEqual(selected.status_code, 206)
        self.assertEqual(selected.content, b"2345")
        self.assertEqual(selected.headers["content-range"], "bytes 2-5/10")

        suffix = self.client.get(
            f"/api/assets/{asset.id}",
            headers={"Range": "bytes=-3"},
        )
        self.assertEqual(suffix.content, b"789")
        opened = self.client.get(
            f"/api/assets/{asset.id}",
            headers={"Range": "bytes=7-"},
        )
        self.assertEqual(opened.content, b"789")

        invalid = self.client.get(
            f"/api/assets/{asset.id}",
            headers={"Range": "bytes=99-100"},
        )
        self.assertEqual(invalid.status_code, 416)
        self.assertEqual(invalid.headers["content-range"], "bytes */10")

    def test_not_found_and_gone_are_distinct(self):
        self._authenticate()
        missing = self.client.get("/api/assets/not-a-real-id")
        self.assertEqual(missing.status_code, 404)

        asset = store_text(self.session, "gone")
        resolve_storage_key(asset.storage_key).unlink()
        gone = self.client.get(f"/api/assets/{asset.id}")
        self.assertEqual(gone.status_code, 410)

    def test_status_route_is_authenticated_and_not_captured_as_asset_id(self):
        denied = self.client.get("/api/assets/status")
        self.assertIn(denied.status_code, {401, 403})

        self._authenticate()
        status = self.client.get("/api/assets/status")
        self.assertEqual(status.status_code, 200)
        self.assertIn("used_percent", status.json())
        self.assertIn("pinned_reference_count", status.json())
        self.assertIn("can_start", status.json())


if __name__ == "__main__":
    unittest.main()
