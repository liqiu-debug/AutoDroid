"""GET /api/devices/ 自愈发现回归测试。

背景：设备行历史上只由「一键同步」写入，用例编辑器直接进入会显示无设备，
必须先去设备中心同步。修复：列表接口的实时 adb 检查顺带登记库中缺失的设备
（字段口径与 sync 一致，无主新设备由发现者认领）。
"""
import unittest
from unittest.mock import patch

from sqlmodel import select

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine

from backend.api import deps
from backend.api import devices as devices_api
from backend.database import get_session
from backend.models import Device, User


class DeviceAutoDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.alice = self._add_user("alice")
        self.bob = self._add_user("bob")

        app = FastAPI()
        app.include_router(devices_api.router, prefix="/api/devices")
        app.dependency_overrides[get_session] = lambda: self.session
        app.dependency_overrides[deps.get_current_user] = lambda: self.alice
        self.client = TestClient(app)

        self._props_calls = []
        self._adb_calls = []

        async def fake_adb(*args, **kwargs):
            self._adb_calls.append(args)
            return b"List of devices attached\nFAKE123\tdevice\n"

        async def fake_props(serial: str):
            self._props_calls.append(serial)
            return {
                "ro.product.model": "TestPhone",
                "ro.product.brand": "acme",
                "ro.build.version.release": "15",
                "resolution": "1080x2400",
            }

        async def noop(*args, **kwargs):
            return None

        self._patches = [
            patch.object(devices_api, "_run_adb_command", fake_adb),
            patch.object(devices_api, "_get_device_props_batch", fake_props),
            patch.object(devices_api, "refresh_app_presence", noop),
            patch.object(devices_api, "_sweep_client_presence", noop),
            patch.object(
                devices_api,
                "_remote_usb_fields",
                lambda serial: {"connection_type": None, "agent_name": None, "source_serial": None},
            ),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self.session.close()
        self.engine.dispose()

    def _add_user(self, username: str, role: str = "user") -> User:
        user = User(username=username, hashed_password="x", role=role)
        self.session.add(user)
        self.session.commit()
        self.session.refresh(user)
        return user

    def test_list_devices_registers_unknown_adb_device(self):
        resp = self.client.get("/api/devices/")
        self.assertEqual(resp.status_code, 200)
        serials = [d["serial"] for d in resp.json()]
        self.assertIn("FAKE123", serials, "新插入的 adb 设备应被自愈登记并出现在列表")

        row = self.session.exec(
            select(Device).where(Device.serial == "FAKE123")
        ).first()
        self.assertIsNotNone(row)
        self.assertEqual(row.owner_id, self.alice.id, "无主新设备由发现者认领")
        self.assertEqual(row.status, "IDLE")
        self.assertEqual(row.model, "TestPhone")

    def test_discovery_is_idempotent_across_polls(self):
        self.client.get("/api/devices/")
        self.client.get("/api/devices/")
        rows = self.session.exec(
            select(Device).where(Device.serial == "FAKE123")
        ).all()
        self.assertEqual(len(rows), 1, "重复轮询不得重复建行")
        self.assertEqual(self._props_calls.count("FAKE123"), 1, "已知设备不再重复拉属性")

    def test_discovered_device_hidden_from_other_users(self):
        self.client.get("/api/devices/")
        app = FastAPI()
        app.include_router(devices_api.router, prefix="/api/devices")
        app.dependency_overrides[get_session] = lambda: self.session
        app.dependency_overrides[deps.get_current_user] = lambda: self.bob
        bob_client = TestClient(app)
        serials = [d["serial"] for d in bob_client.get("/api/devices/").json()]
        self.assertNotIn("FAKE123", serials, "他人认领的设备不应可见")


if __name__ == "__main__":
    unittest.main()
