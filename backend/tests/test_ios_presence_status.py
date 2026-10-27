"""iOS 设备「幽灵在线」修复回归测试。

背景：设备未连接任何电脑时，iOS 行的历史状态（IDLE/WDA_DOWN）会被冻结保留，
而 WDA_DOWN 在「在线」口径（status != OFFLINE）里算在线，形成
「显示在线 + WDA 不在线」的矛盾显示。修复：refresh_ios_wda 时先做 USB 在场
判定——非无线且不在 usbmux 在场列表的设备直接判 OFFLINE，不再探测 WDA。
"""
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine, select

from backend.api import devices as devices_api
from backend.api.deps import get_current_user
from backend.database import get_session
from backend.models import Device, User


class IosPresenceStatusTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.user = User(username="ios-user", hashed_password="x", role="admin")
        self.session.add(self.user)
        self.session.commit()
        self.session.refresh(self.user)

        app = FastAPI()
        app.include_router(devices_api.router, prefix="/api/devices")
        app.dependency_overrides[get_session] = lambda: self.session
        app.dependency_overrides[get_current_user] = lambda: self.user
        self.client = TestClient(app)

        self.present = []
        self.wda_healthy = True
        self.wireless = {}

        async def fake_adb(*args, **kwargs):
            return b"List of devices attached\n"

        async def fake_props(serial):
            return {"ro.product.model": "M", "ro.product.brand": "B",
                    "ro.build.version.release": "15", "resolution": "1x1"}

        async def noop(*args, **kwargs):
            return None

        self._patches = [
            patch.object(devices_api, "_run_adb_command", fake_adb),
            patch.object(devices_api, "_get_device_props_batch", fake_props),
            patch.object(devices_api, "refresh_app_presence", noop),
            patch.object(devices_api, "_sweep_client_presence", noop),
            patch.object(devices_api, "_discover_new_adb_devices", noop),
            patch.object(devices_api, "_get_wireless_url_map", lambda session: dict(self.wireless)),
            patch.object(devices_api, "_check_ios_wda_health",
                         lambda session, udid: {"healthy": self.wda_healthy, "wda_url": "u", "error": None}),
            patch("backend.ios_scanner.get_ios_devices", lambda: list(self.present)),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.session.close()
        self.engine.dispose()

    def add_ios(self, serial, status="IDLE", client_instance_id=None, last_seen_at=None):
        row = Device(
            serial=serial, platform="ios", model="iPhone", brand="APPLE",
            status=status, client_instance_id=client_instance_id,
            last_seen_at=last_seen_at, owner_id=self.user.id,
        )
        self.session.add(row)
        self.session.commit()
        return row

    def fetch(self):
        resp = self.client.get("/api/devices/", params={"refresh_ios_wda": "true"})
        self.assertEqual(resp.status_code, 200, resp.text)
        return {d["serial"]: d for d in resp.json()}

    def status_of(self, serial):
        row = self.session.exec(select(Device).where(Device.serial == serial)).first()
        return row.status

    def test_ghost_online_device_goes_offline(self):
        """主场景：设备未连接任何电脑，历史 IDLE 状态应被判 OFFLINE。"""
        self.add_ios("UDID-GHOST", status="IDLE")
        self.present = []
        rows = self.fetch()
        self.assertEqual(rows["UDID-GHOST"]["status"], "OFFLINE")
        self.assertEqual(self.status_of("UDID-GHOST"), "OFFLINE")

    def test_wda_down_stale_also_goes_offline(self):
        self.add_ios("UDID-STALE", status="WDA_DOWN")
        self.present = []
        rows = self.fetch()
        self.assertEqual(rows["UDID-STALE"]["status"], "OFFLINE")

    def test_present_device_with_healthy_wda_is_idle(self):
        self.add_ios("UDID-LIVE", status="WDA_DOWN")
        self.present = [{"device_id": "UDID-LIVE"}]
        self.wda_healthy = True
        rows = self.fetch()
        self.assertEqual(rows["UDID-LIVE"]["status"], "IDLE")

    def test_present_device_with_dead_wda_is_wda_down(self):
        self.add_ios("UDID-NOWDA", status="IDLE")
        self.present = [{"device_id": "UDID-NOWDA"}]
        self.wda_healthy = False
        rows = self.fetch()
        self.assertEqual(rows["UDID-NOWDA"]["status"], "WDA_DOWN")

    def test_present_busy_device_keeps_busy(self):
        self.add_ios("UDID-BUSY", status="BUSY")
        self.present = [{"device_id": "UDID-BUSY"}]
        self.wda_healthy = True
        rows = self.fetch()
        self.assertEqual(rows["UDID-BUSY"]["status"], "BUSY")

    def test_wireless_device_not_demoted_by_usb_absence(self):
        """无线 WDA 设备不插 USB 也可能是在线的：保持既有无线语义。"""
        self.add_ios("UDID-WIFI", status="IDLE")
        self.wireless = {"UDID-WIFI": "http://1.2.3.4:8100"}
        self.present = []
        self.wda_healthy = False
        rows = self.fetch()
        self.assertEqual(rows["UDID-WIFI"]["status"], "WDA_DOWN",
                         "无线设备走 WDA 判定而非 USB 在场判定")

    def test_client_managed_ios_not_touched_by_presence(self):
        """客户端注册的 iOS 设备由心跳 TTL 管，不参与服务端 USB 在场判定。"""
        from datetime import datetime
        self.add_ios("UDID-CLIENT", status="IDLE", client_instance_id="desk-1", last_seen_at=datetime.now())
        self.present = []
        rows = self.fetch()
        self.assertEqual(rows["UDID-CLIENT"]["status"], "IDLE")


if __name__ == "__main__":
    unittest.main()
