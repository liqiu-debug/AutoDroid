"""设备列表类接口的鉴权与归属过滤（P0 安全修复）。

覆盖此前无鉴权且返回全量设备的两个入口：
- GET /api/fastbot/devices
- GET /api/stream/devices 与 /api/stream/devices/{serial}

口径与 GET /api/devices/ 一致：admin 全量，普通用户仅 owner_id/app_owner_id
为本人的设备（未认领设备仅 admin 可见）。
"""
import unittest
from datetime import datetime
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine

from backend.api import deps
from backend.api import fastbot as fastbot_api
from backend.core.security import ALGORITHM, SECRET_KEY
from backend.database import get_session
from backend.device_stream import router as stream_router
from backend.models import Device, User


async def _adb_no_devices(*args, **kwargs):
    return set()


class _DeviceListAccessBase(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.admin = self._add_user("admin", role="admin")
        self.alice = self._add_user("alice")
        self.bob = self._add_user("bob")

        app = FastAPI()
        app.include_router(fastbot_api.router, prefix="/api/fastbot")
        app.include_router(stream_router.rest_router, prefix="/api/stream")
        app.dependency_overrides[get_session] = lambda: self.session
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.session.close()
        self.engine.dispose()

    def _add_user(self, username: str, role: str = "user") -> User:
        user = User(username=username, hashed_password="x", role=role)
        self.session.add(user)
        self.session.commit()
        self.session.refresh(user)
        return user

    def _add_device(self, serial: str, **fields) -> Device:
        device = Device(serial=serial, platform="android", **fields)
        self.session.add(device)
        self.session.commit()
        self.session.refresh(device)
        return device

    @staticmethod
    def _token(username: str) -> dict:
        return {"Authorization": f"Bearer {jwt.encode({'sub': username}, SECRET_KEY, algorithm=ALGORITHM)}"}


class FastbotDeviceListAccessTests(_DeviceListAccessBase):
    def test_list_requires_auth(self):
        with patch("backend.api.fastbot._get_online_devices", new=_adb_no_devices):
            resp = self.client.get("/api/fastbot/devices")
        self.assertEqual(resp.status_code, 401)

    def test_list_filters_by_owner_and_admin_sees_all(self):
        self._add_device("mine-1", owner_id=self.alice.id, status="IDLE")
        self._add_device("other-1", owner_id=self.bob.id, status="IDLE")
        self._add_device("legacy-1", owner_id=None, status="IDLE")

        with patch("backend.api.fastbot._get_online_devices", new=_adb_no_devices):
            alice_resp = self.client.get("/api/fastbot/devices", headers=self._token("alice"))
            admin_resp = self.client.get("/api/fastbot/devices", headers=self._token("admin"))

        self.assertEqual(alice_resp.status_code, 200, alice_resp.text)
        self.assertEqual({row["serial"] for row in alice_resp.json()}, {"mine-1"})
        self.assertEqual(
            {row["serial"] for row in admin_resp.json()},
            {"mine-1", "other-1", "legacy-1"},
        )

    def test_client_registered_device_not_marked_offline_by_adb(self):
        """客户端注册设备不参与 adb 翻转（与 GET /api/devices/ 同口径）。"""
        self._add_device(
            "desk-1",
            owner_id=self.alice.id,
            status="IDLE",
            client_instance_id="desk-inst",
            last_seen_at=datetime.now(),
        )

        with patch("backend.api.fastbot._get_online_devices", new=_adb_no_devices):
            resp = self.client.get("/api/fastbot/devices", headers=self._token("alice"))

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()[0]["status"], "IDLE")


class StreamDeviceListAccessTests(_DeviceListAccessBase):
    def setUp(self) -> None:
        super().setUp()
        self._streams = [
            {"serial": "mine-1", "state": "playing"},
            {"serial": "other-1", "state": "playing"},
            {"serial": "legacy-1", "state": "idle"},
            {"serial": "ghost-1", "state": "idle"},
        ]

    def _patch_streams(self):
        return patch.object(
            stream_router.device_manager,
            "get_devices_list",
            return_value=list(self._streams),
        )

    def test_list_requires_auth(self):
        with self._patch_streams():
            resp = self.client.get("/api/stream/devices")
        self.assertEqual(resp.status_code, 401)

    def test_list_filters_by_owner_and_admin_sees_all(self):
        self._add_device("mine-1", owner_id=self.alice.id, status="IDLE")
        self._add_device("other-1", owner_id=self.bob.id, status="IDLE")
        self._add_device("legacy-1", owner_id=None, status="IDLE")

        with self._patch_streams():
            alice_resp = self.client.get("/api/stream/devices", headers=self._token("alice"))
            admin_resp = self.client.get("/api/stream/devices", headers=self._token("admin"))

        self.assertEqual(alice_resp.status_code, 200, alice_resp.text)
        # 未认领 legacy-1 与未登记 ghost-1 对普通用户不可见
        self.assertEqual([row["serial"] for row in alice_resp.json()], ["mine-1"])
        self.assertEqual(
            [row["serial"] for row in admin_resp.json()],
            ["mine-1", "other-1", "legacy-1", "ghost-1"],
        )

    def test_get_device_enforces_ownership(self):
        self._add_device("mine-1", owner_id=self.alice.id, status="IDLE")
        self._add_device("other-1", owner_id=self.bob.id, status="IDLE")

        with patch.object(
            stream_router.device_manager,
            "get_device",
            return_value={"serial": "other-1"},
        ):
            resp = self.client.get("/api/stream/devices/other-1", headers=self._token("alice"))
            admin_resp = self.client.get("/api/stream/devices/other-1", headers=self._token("admin"))

        self.assertEqual(resp.status_code, 404)
        self.assertEqual(admin_resp.status_code, 200)
        self.assertEqual(admin_resp.json()["serial"], "other-1")

    def test_get_device_requires_auth(self):
        with patch.object(
            stream_router.device_manager,
            "get_device",
            return_value={"serial": "mine-1"},
        ):
            resp = self.client.get("/api/stream/devices/mine-1")
        self.assertEqual(resp.status_code, 401)
