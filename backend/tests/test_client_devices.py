"""桌面客户端设备归属 API（client-devices）与归属过滤/回填测试。

覆盖：register upsert、409 归属冲突、heartbeat、logout、
GET /api/devices/ 归属过滤（普通用户 / admin 全量）、存量 owner_id 回填、
客户端注册设备的在线状态判定（不被服务端 adb 刷新误杀）、路由鉴权与裸别名。
"""
import sqlite3
import tempfile
import os
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine, select

from backend.api import client_devices, devices as devices_api, deps
from backend.api.client_devices import (
    ClientDeviceHeartbeatRequest,
    ClientDeviceLogoutRequest,
    ClientDeviceRegisterRequest,
    heartbeat_client_devices,
    logout_client_devices,
    register_client_device,
)
from backend.api.devices import filter_devices_for_user, list_devices
from backend.core.security import ALGORITHM, SECRET_KEY
from backend.database import _run_migrations_with_conn, backfill_legacy_device_owners, get_session
from backend.models import Device, User


def _register_payload(serial: str = "emulator-5554", **overrides) -> ClientDeviceRegisterRequest:
    data = dict(
        platform="android",
        serial=serial,
        model="Pixel 7",
        brand="google",
        android_version="14",
        os_version="14",
        resolution="1080x2400",
    )
    data.update(overrides)
    return ClientDeviceRegisterRequest(**data)


class ClientDeviceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.admin = self._add_user("admin", role="admin")
        self.alice = self._add_user("alice")
        self.bob = self._add_user("bob")

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
        device = Device(serial=serial, **fields)
        self.session.add(device)
        self.session.commit()
        self.session.refresh(device)
        return device

    # ---------- register ----------

    def test_register_creates_device_and_claims_ownership(self):
        before = datetime.now()
        payload = _register_payload(
            serial="emulator-5554",
            custom_name="客厅机",
            connection_type="usb",
            client_instance_id="desk-1",
        )
        read = register_client_device(payload, self.session, self.alice)

        self.assertEqual(read.serial, "emulator-5554")
        self.assertEqual(read.owner_id, self.alice.id)
        self.assertEqual(read.model, "Pixel 7")
        self.assertEqual(read.custom_name, "客厅机")
        self.assertEqual(read.client_instance_id, "desk-1")
        self.assertIsNotNone(read.last_seen_at)

        device = self.session.exec(select(Device).where(Device.serial == "emulator-5554")).one()
        self.assertEqual(device.owner_id, self.alice.id)
        self.assertEqual(device.client_instance_id, "desk-1")
        self.assertIsNotNone(device.last_seen_at)
        self.assertGreaterEqual(device.last_seen_at, before)
        self.assertEqual(device.status, "IDLE")

    def test_register_upserts_existing_unclaimed_device(self):
        self._add_device("emulator-5554", model="Old", owner_id=None)

        read = register_client_device(_register_payload(model="New"), self.session, self.bob)

        rows = self.session.exec(select(Device).where(Device.serial == "emulator-5554")).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].model, "New")
        self.assertEqual(rows[0].owner_id, self.bob.id)
        self.assertEqual(read.owner_id, self.bob.id)

    def test_register_refreshes_metadata_for_own_device(self):
        self._add_device("emulator-5554", owner_id=self.alice.id, model="Old")
        first = self.session.exec(select(Device).where(Device.serial == "emulator-5554")).one()
        first.last_seen_at = datetime(2020, 1, 1)
        self.session.add(first)
        self.session.commit()

        read = register_client_device(_register_payload(model="New"), self.session, self.alice)

        self.assertEqual(read.model, "New")
        self.assertEqual(read.owner_id, self.alice.id)
        device = self.session.exec(select(Device).where(Device.serial == "emulator-5554")).one()
        self.assertGreater(device.last_seen_at, datetime(2020, 1, 1))

    def test_register_conflicts_when_owned_by_other_member(self):
        # 客户端管理形态（client_instance_id 非空）：他人已注册 → 409，不得接管
        self._add_device(
            "emulator-5554", owner_id=self.alice.id, model="Alice", client_instance_id="desk-alice"
        )

        with self.assertRaises(HTTPException) as context:
            register_client_device(_register_payload(), self.session, self.bob)

        self.assertEqual(context.exception.status_code, 409)
        self.assertEqual(context.exception.detail, "设备已被其他成员注册")
        device = self.session.exec(select(Device).where(Device.serial == "emulator-5554")).one()
        self.assertEqual(device.owner_id, self.alice.id)
        self.assertEqual(device.model, "Alice")

    def test_register_conflicts_when_other_member_has_heartbeat_only(self):
        # 仅 last_seen_at 非空也属客户端管理形态 → 409
        self._add_device("emulator-5554", owner_id=self.alice.id, model="Alice")
        device = self.session.exec(select(Device).where(Device.serial == "emulator-5554")).one()
        device.last_seen_at = datetime.now()
        self.session.add(device)
        self.session.commit()

        with self.assertRaises(HTTPException) as context:
            register_client_device(_register_payload(), self.session, self.bob)

        self.assertEqual(context.exception.status_code, 409)
        self.assertEqual(context.exception.detail, "设备已被其他成员注册")
        self.session.refresh(device)
        self.assertEqual(device.owner_id, self.alice.id)

    def test_register_rejects_blank_serial(self):
        with self.assertRaises(HTTPException) as context:
            register_client_device(_register_payload(serial="  "), self.session, self.alice)
        self.assertEqual(context.exception.status_code, 400)

    # ---------- heartbeat ----------

    def test_heartbeat_with_empty_serials_updates_nothing(self):
        self._add_device("mine-1", owner_id=self.alice.id, last_seen_at=None)
        result = heartbeat_client_devices(ClientDeviceHeartbeatRequest(serials=[]), self.session, self.alice)
        self.assertEqual(result.get("updated"), 0)

    # ---------- logout ----------

    def test_logout_marks_all_own_devices_offline(self):
        self._add_device("mine-1", owner_id=self.alice.id, status="IDLE")
        self._add_device("mine-2", owner_id=self.alice.id, status="BUSY")
        other = self._add_device("other-1", owner_id=self.bob.id, status="IDLE")

        result = logout_client_devices(ClientDeviceLogoutRequest(), self.session, self.alice)

        self.assertEqual(result, {"updated": 2})
        rows = self.session.exec(select(Device)).all()
        statuses = {row.serial: row.status for row in rows}
        self.assertEqual(statuses, {"mine-1": "OFFLINE", "mine-2": "OFFLINE", "other-1": "IDLE"})
        self.session.refresh(other)
        self.assertEqual(other.status, "IDLE")

    def test_logout_subset_only_touches_given_serials(self):
        self._add_device("mine-1", owner_id=self.alice.id, status="IDLE")
        self._add_device("mine-2", owner_id=self.alice.id, status="IDLE")
        self._add_device("other-1", owner_id=self.bob.id, status="IDLE")

        result = logout_client_devices(
            ClientDeviceLogoutRequest(serials=["mine-1", "other-1"]),
            self.session,
            self.alice,
        )

        self.assertEqual(result, {"updated": 1})
        rows = self.session.exec(select(Device)).all()
        statuses = {row.serial: row.status for row in rows}
        self.assertEqual(statuses, {"mine-1": "OFFLINE", "mine-2": "IDLE", "other-1": "IDLE"})

    def test_logout_with_empty_subset_updates_nothing(self):
        self._add_device("mine-1", owner_id=self.alice.id, status="IDLE")
        result = logout_client_devices(ClientDeviceLogoutRequest(serials=[]), self.session, self.alice)
        self.assertEqual(result, {"updated": 0})

    # ---------- 列表过滤 ----------

    def test_filter_devices_for_user_visibility(self):
        mine = self._add_device("mine-1", owner_id=self.alice.id)
        app_owned = self._add_device("app-1", app_owner_id=self.alice.id)
        unclaimed = self._add_device("legacy-1", owner_id=None)
        self._add_device("other-1", owner_id=self.bob.id)
        devices = [mine, app_owned, unclaimed]

        alice_view = filter_devices_for_user(devices, SimpleNamespace(id=self.alice.id, role="user"))
        self.assertEqual({d.serial for d in alice_view}, {"mine-1", "app-1"})

        admin_view = filter_devices_for_user(devices, SimpleNamespace(id=self.admin.id, role="admin"))
        self.assertEqual({d.serial for d in admin_view}, {"mine-1", "app-1", "legacy-1"})

    async def test_list_devices_filters_by_owner_and_admin_sees_all(self):
        self._add_device("mine-1", owner_id=self.alice.id, status="IDLE")
        self._add_device("other-1", owner_id=self.bob.id, status="IDLE")
        self._add_device("legacy-1", owner_id=None, status="IDLE")

        async def _no_devices(*args, **kwargs):
            return b"List of devices attached\n"

        with patch("backend.api.devices._run_adb_command", new=_no_devices):
            alice_rows = await list_devices(session=self.session, current_user=SimpleNamespace(id=self.alice.id, role="user"))
            admin_rows = await list_devices(session=self.session, current_user=SimpleNamespace(id=self.admin.id, role="admin"))

        # adb 报告全部离线 → 状态写库后重查，归属过滤必须仍然生效（不泄露他人设备）
        self.assertEqual({d.serial for d in alice_rows}, {"mine-1"})
        self.assertEqual({d.serial for d in admin_rows}, {"mine-1", "other-1", "legacy-1"})

    # ---------- backfill ----------

    def test_backfill_legacy_device_owners_prefers_admin_and_is_idempotent(self):
        legacy_a = self._add_device("legacy-a", owner_id=None)
        legacy_b = self._add_device("legacy-b", owner_id=None)
        owned = self._add_device("owned-1", owner_id=self.alice.id)

        counts = backfill_legacy_device_owners(self.session, self.admin.id)
        self.assertEqual(counts, {"devices": 2})
        self.session.refresh(legacy_a)
        self.session.refresh(legacy_b)
        self.session.refresh(owned)
        self.assertEqual(legacy_a.owner_id, self.admin.id)
        self.assertEqual(legacy_b.owner_id, self.admin.id)
        self.assertEqual(owned.owner_id, self.alice.id)

        again = backfill_legacy_device_owners(self.session, self.admin.id)
        self.assertEqual(again, {"devices": 0})

    def test_backfill_without_admin_falls_back_to_earliest_user(self):
        # 清空用户后仅剩两个普通用户：无 admin 时回填给最早用户
        for user in self.session.exec(select(User)).all():
            self.session.delete(user)
        self.session.commit()
        first = self._add_user("first-user")
        self._add_user("second-user")
        legacy = self._add_device("legacy-a", owner_id=None)

        counts = backfill_legacy_device_owners(self.session)

        self.assertEqual(counts, {"devices": 1})
        self.session.refresh(legacy)
        self.assertEqual(legacy.owner_id, first.id)

    def test_backfill_prefers_admin_role_when_no_explicit_id(self):
        self._add_user("plain-first")
        legacy = self._add_device("legacy-a", owner_id=None)

        backfill_legacy_device_owners(self.session)

        self.session.refresh(legacy)
        self.assertEqual(legacy.owner_id, self.admin.id)


class ClientDeviceMigrationTests(unittest.TestCase):
    """device 归属三列的迁移补列 + 索引（历史库升级路径）。"""

    def setUp(self) -> None:
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.conn = sqlite3.connect(self.tmp.name)
        cursor = self.conn.cursor()
        cursor.executescript(
            """
            CREATE TABLE device (
                id INTEGER PRIMARY KEY,
                serial VARCHAR
            );
            INSERT INTO device(id, serial) VALUES (1, 'legacy-1');
            """
        )
        self.conn.commit()

    def tearDown(self) -> None:
        try:
            self.conn.close()
        finally:
            if os.path.exists(self.tmp.name):
                os.remove(self.tmp.name)

    def test_migration_adds_client_presence_columns_and_indexes(self):
        _run_migrations_with_conn(self.conn)

        cursor = self.conn.cursor()
        columns = {row[1] for row in cursor.execute("PRAGMA table_info(device)").fetchall()}
        self.assertTrue({"owner_id", "client_instance_id", "last_seen_at"} <= columns)

        index_names = {row[1] for row in cursor.execute("PRAGMA index_list(device)").fetchall()}
        self.assertIn("ix_device_owner_id", index_names)
        self.assertIn("ix_device_client_instance_id", index_names)


async def _adb_no_devices(*args, **kwargs):
    """模拟服务端 adb 看不到任何设备（桌面客户端设备插在别的机器上）。"""
    return b"List of devices attached\n"


class ClientDeviceStatusRefreshTests(unittest.TestCase):
    """P0 blocker 复现：GET /api/devices/ 与 /sync 的 adb 实时刷新不误杀客户端注册设备。

    修复前：客户端 register 的设备在服务端 adb devices 里看不到，列表刷新
    直接把它打成 OFFLINE；应改为 last_seen_at 心跳超时判定（超时才 OFFLINE）。
    """

    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.alice = self._add_user("alice")

        # hermetic：sync 内部会跑真机 iOS 扫描（tidevice），插着 iPhone 时会往
        # 测试库塞额外设备导致断言串扰；统一桩成无 iOS 设备。
        try:
            ios_patcher = patch("backend.ios_scanner.get_ios_devices", return_value=[])
            ios_patcher.start()
            self.addCleanup(ios_patcher.stop)
        except ImportError:
            pass  # tidevice 未安装时 sync 内部本来就跳过 iOS 扫描

        app = FastAPI()
        app.include_router(client_devices.router, prefix="/api/client-devices")
        app.include_router(devices_api.router, prefix="/api/devices")
        app.dependency_overrides[get_session] = lambda: self.session
        app.dependency_overrides[deps.get_current_user] = lambda: self.alice
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
        device = Device(serial=serial, **fields)
        self.session.add(device)
        self.session.commit()
        self.session.refresh(device)
        return device

    def _register(self, serial: str = "emulator-5554", **overrides) -> dict:
        payload = dict(
            platform="android",
            serial=serial,
            model="Pixel 7",
            brand="google",
            android_version="14",
            os_version="14",
            resolution="1080x2400",
            client_instance_id="desk-1",
        )
        payload.update(overrides)
        resp = self.client.post("/api/client-devices/register", json=payload)
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def _set_last_seen(self, serial: str, seen_at: datetime) -> None:
        device = self.session.exec(select(Device).where(Device.serial == serial)).one()
        device.last_seen_at = seen_at
        self.session.add(device)
        self.session.commit()

    def test_register_then_list_keeps_client_device_online(self):
        """复现修复：register 后 GET /api/devices/ 不得把设备打成 OFFLINE。"""
        self._register()

        with patch("backend.api.devices._run_adb_command", new=_adb_no_devices):
            resp = self.client.get("/api/devices/")

        self.assertEqual(resp.status_code, 200, resp.text)
        rows = resp.json()
        self.assertEqual([row["serial"] for row in rows], ["emulator-5554"])
        self.assertEqual(rows[0]["status"], "IDLE")

    def test_client_device_marks_offline_after_last_seen_timeout(self):
        self._register()
        self._set_last_seen(
            "emulator-5554",
            datetime.now() - timedelta(seconds=devices_api.CLIENT_HEARTBEAT_TIMEOUT_SECONDS + 5),
        )

        with patch("backend.api.devices._run_adb_command", new=_adb_no_devices):
            resp = self.client.get("/api/devices/")

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()[0]["status"], "OFFLINE")

    def test_list_keeps_client_device_online_within_timeout_window(self):
        self._register()
        self._set_last_seen(
            "emulator-5554",
            datetime.now() - timedelta(seconds=devices_api.CLIENT_HEARTBEAT_TIMEOUT_SECONDS - 30),
        )

        with patch("backend.api.devices._run_adb_command", new=_adb_no_devices):
            resp = self.client.get("/api/devices/")

        self.assertEqual(resp.json()[0]["status"], "IDLE")

    def test_client_managed_ios_device_skips_server_wda_probe(self):
        """客户端注册的 iOS 设备不走服务端 WDA 探测（WDA 在客户端机器上）。"""
        self._register(serial="ios-udid-1", platform="ios")

        with patch(
            "backend.api.devices._check_ios_wda_health",
            return_value={"healthy": False, "error": "unreachable"},
        ), patch("backend.api.devices._run_adb_command", new=_adb_no_devices):
            resp = self.client.get("/api/devices/", params={"refresh_ios_wda": "true"})

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()[0]["status"], "IDLE")

    def test_server_side_device_still_follows_adb(self):
        """无客户端标记（client_instance_id/last_seen_at 均空）的服务端设备仍按 adb 翻转。"""
        self._add_device("usb-1", owner_id=self.alice.id, status="IDLE")

        with patch("backend.api.devices._run_adb_command", new=_adb_no_devices):
            resp = self.client.get("/api/devices/")

        self.assertEqual(resp.json()[0]["status"], "OFFLINE")

    def test_sync_keeps_fresh_client_device_and_times_out_stale(self):
        self._register(serial="emulator-5554")
        self._register(serial="emulator-5556", client_instance_id="desk-2")
        self._set_last_seen(
            "emulator-5556",
            datetime.now() - timedelta(seconds=devices_api.CLIENT_HEARTBEAT_TIMEOUT_SECONDS + 5),
        )

        with patch("backend.api.devices._run_adb_command", new=_adb_no_devices):
            resp = self.client.post("/api/devices/sync")

        self.assertEqual(resp.status_code, 200, resp.text)
        statuses = {row["serial"]: row["status"] for row in resp.json()["devices"]}
        self.assertEqual(statuses, {"emulator-5554": "IDLE", "emulator-5556": "OFFLINE"})

    def test_heartbeat_revives_offline_client_device(self):
        """心跳到过即可复活：插拔回来后 heartbeat 把 OFFLINE 拉回 IDLE。"""
        self._register()
        device = self.session.exec(select(Device).where(Device.serial == "emulator-5554")).one()
        device.status = "OFFLINE"
        self.session.add(device)
        self.session.commit()

        resp = self.client.post(
            "/api/client-devices/heartbeat", json={"serials": ["emulator-5554"]}
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.session.refresh(device)
        self.assertEqual(device.status, "IDLE")

    def test_register_revives_offline_client_device(self):
        self._register()
        device = self.session.exec(select(Device).where(Device.serial == "emulator-5554")).one()
        device.status = "OFFLINE"
        self.session.add(device)
        self.session.commit()

        self._register()
        self.session.refresh(device)
        self.assertEqual(device.status, "IDLE")

    # ---------- R3：心跳超时判定与 adb 成败解耦 ----------

    def test_list_marks_expired_client_offline_when_adb_fails(self):
        """R3：adb 缺失/失败时超时判定不得被吞（修复前过期设备永远 IDLE）。"""
        self._register()
        self._add_device("usb-legacy", owner_id=self.alice.id, status="IDLE")
        self._set_last_seen(
            "emulator-5554",
            datetime.now() - timedelta(seconds=devices_api.CLIENT_HEARTBEAT_TIMEOUT_SECONDS + 5),
        )

        async def _adb_boom(*args, **kwargs):
            raise RuntimeError("adb not found")

        with patch("backend.api.devices._run_adb_command", new=_adb_boom):
            resp = self.client.get("/api/devices/")

        self.assertEqual(resp.status_code, 200, resp.text)
        statuses = {row["serial"]: row["status"] for row in resp.json()}
        self.assertEqual(statuses["emulator-5554"], "OFFLINE")
        # 非客户端设备在 adb 失败时不得被误判
        self.assertEqual(statuses["usb-legacy"], "IDLE")

    def test_sync_marks_expired_client_offline_when_adb_fails(self):
        """R3：sync 的 adb 失败仍先跑心跳判定（独立提交）再决定响应。"""
        self._register()
        self._set_last_seen(
            "emulator-5554",
            datetime.now() - timedelta(seconds=devices_api.CLIENT_HEARTBEAT_TIMEOUT_SECONDS + 5),
        )

        async def _adb_boom(*args, **kwargs):
            raise RuntimeError("adb not found")

        with patch("backend.api.devices._run_adb_command", new=_adb_boom):
            resp = self.client.post("/api/devices/sync")

        self.assertEqual(resp.status_code, 500)  # adb 失败如实上报
        device = self.session.exec(select(Device).where(Device.serial == "emulator-5554")).one()
        self.assertEqual(device.status, "OFFLINE")

    def test_sync_keeps_fresh_client_device_online_when_adb_fails(self):
        """R3：adb 失败只影响超时判定，不得误杀心跳新鲜的客户端设备。"""
        self._register()

        async def _adb_boom(*args, **kwargs):
            raise RuntimeError("adb not found")

        with patch("backend.api.devices._run_adb_command", new=_adb_boom):
            self.client.post("/api/devices/sync")

        device = self.session.exec(select(Device).where(Device.serial == "emulator-5554")).one()
        self.assertEqual(device.status, "IDLE")


class ClientDeviceRouteTests(unittest.TestCase):
    """P0 路由级：/api/client-devices 鉴权 + 裸别名可达（与 main.py 双挂载一致）。"""

    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.user = User(username="tester", hashed_password="x", role="user")
        self.session.add(self.user)
        self.session.commit()
        self.session.refresh(self.user)

        app = FastAPI()
        app.include_router(client_devices.router, prefix="/api/client-devices")
        app.include_router(client_devices.router, prefix="/client-devices")
        app.dependency_overrides[get_session] = lambda: self.session
        self.client = TestClient(app)
        self.token = jwt.encode({"sub": "tester"}, SECRET_KEY, algorithm=ALGORITHM)

    def tearDown(self) -> None:
        self.session.close()
        self.engine.dispose()

    @staticmethod
    def _payload(serial: str = "emulator-5554") -> dict:
        return {
            "platform": "android",
            "serial": serial,
            "model": "Pixel 7",
            "brand": "google",
            "android_version": "14",
            "os_version": "14",
            "resolution": "1080x2400",
            "client_instance_id": "desk-1",
        }

    def test_register_without_token_returns_401(self):
        resp = self.client.post("/api/client-devices/register", json=self._payload())
        self.assertEqual(resp.status_code, 401)

    def test_heartbeat_and_logout_without_token_return_401(self):
        self.assertEqual(
            self.client.post("/api/client-devices/heartbeat", json={"serials": ["a"]}).status_code,
            401,
        )
        self.assertEqual(
            self.client.post("/api/client-devices/logout", json={}).status_code,
            401,
        )

    def test_register_with_token_returns_device_read_shape(self):
        resp = self.client.post(
            "/api/client-devices/register",
            json=self._payload(),
            headers={"Authorization": f"Bearer {self.token}"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        for key in (
            "id",
            "serial",
            "platform",
            "model",
            "status",
            "owner_id",
            "client_instance_id",
            "last_seen_at",
            "created_at",
        ):
            self.assertIn(key, body)
        self.assertEqual(body["serial"], "emulator-5554")
        self.assertEqual(body["owner_id"], self.user.id)
        self.assertEqual(body["client_instance_id"], "desk-1")
        self.assertIsNotNone(body["last_seen_at"])

    def test_bare_alias_register_reachable(self):
        resp = self.client.post(
            "/client-devices/register",
            json=self._payload(serial="emulator-2"),
            headers={"Authorization": f"Bearer {self.token}"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["serial"], "emulator-2")
