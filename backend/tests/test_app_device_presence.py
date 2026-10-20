import sqlite3
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from sqlmodel import SQLModel, Session, create_engine, select

from backend.api.autojs import DeviceRegisterPayload, register_device, logout_device
from backend.api.devices import bind_app_device, list_devices, sync_devices, get_screenshot, unlock_device, check_ios_wda
from backend.database import _migrate_app_device_presence
from backend.device_presence import app_presence, refresh_app_presence
from backend.models import Device
from backend.schemas import DeviceAppBindingRequest


class AppPresenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.user = SimpleNamespace(id=1)

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def register(self, platform="ios", identity="app-1", state="READY"):
        payload = DeviceRegisterPayload(platform=platform, udid=identity, executor_status=state)
        response = register_device(payload, self.session, self.user)
        return self.session.exec(select(Device).where(Device.serial == response["device_serial"])).one(), payload

    async def listing(self):
        with patch("backend.api.devices._run_adb_command", return_value=b"List of devices attached\n"), patch(
            "backend.api.devices._check_ios_wda_health") as probe:
            result = await list_devices(refresh_ios_wda=True, session=self.session)
        return result, probe

    async def test_both_app_platforms_survive_host_scans_then_expire(self):
        for platform in ("android", "ios"):
            self.register(platform, platform)
        rows, probe = await self.listing()
        self.assertEqual([r.status for r in rows], ["IDLE", "IDLE"])
        self.assertTrue(all(r.is_app_device and not r.host_control_available for r in rows))
        probe.assert_not_called()
        for device in self.session.exec(select(Device)).all():
            device.app_last_seen_at = datetime.now() - timedelta(seconds=181)
            device.updated_at = datetime.now()  # Renaming/other writes must not extend the heartbeat.
            self.session.add(device)
        self.session.commit()
        rows, _ = await self.listing()
        self.assertTrue(all(r.status == "OFFLINE" and r.app_presence["status"] == "TIMEOUT" for r in rows))

    async def test_logout_and_relogin_preserve_physical_status(self):
        app, payload = self.register()
        physical = Device(serial="usb-1", platform="ios", status="BUSY", connection_type="usb")
        self.session.add(physical); self.session.commit()
        logout_device(payload, self.session, self.user)
        self.assertEqual(app_presence(app)["status"], "LOGGED_OUT")
        self.assertEqual(physical.status, "BUSY")
        register_device(payload, self.session, self.user)
        self.assertEqual(app_presence(app)["status"], "ONLINE")

    async def test_running_state_and_expired_state_are_independent(self):
        app, _ = self.register(state="RUNNING")
        self.assertEqual(app.status, "BUSY")
        app.app_last_seen_at = datetime.now() - timedelta(minutes=4)
        self.session.add(app); self.session.commit()
        refresh_app_presence(self.session)
        self.assertEqual(app.status, "OFFLINE")
        self.assertEqual(app_presence(app)["executor_status"], "UNKNOWN")
        self.assertEqual(app.app_executor_status, "RUNNING")  # Not a stop signal.

    async def test_sync_counts_bound_phone_once_even_without_host_transport(self):
        app, payload = self.register()
        self.session.add(Device(serial="usb-1", platform="ios", status="OFFLINE"))
        self.session.commit()
        bind_app_device(app.serial, DeviceAppBindingRequest(physical_serial="usb-1"), self.session, self.user)
        with patch("backend.api.devices._run_adb_command", return_value=b"List of devices attached\n"), patch(
            "backend.ios_scanner.get_ios_devices", return_value=[]):
            result = await sync_devices(self.session, self.user)
            self.assertEqual((result.online, result.offline, len(result.devices)), (1, 0, 1))
            self.assertFalse(result.devices[0].host_control_available)
            logout_device(payload, self.session, self.user)
            result = await sync_devices(self.session, self.user)
            self.assertEqual((result.online, result.offline, len(result.devices)), (0, 1, 1))

    async def test_binding_merges_only_presentation_and_unbind_restores_two_rows(self):
        app, payload = self.register()
        physical = Device(serial="usb-1", platform="ios", status="OFFLINE", connection_type="usb")
        self.session.add(physical); self.session.commit()
        bind_app_device(app.serial, DeviceAppBindingRequest(physical_serial=physical.serial), self.session, self.user)
        rows, probe = await self.listing()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].serial, physical.serial)
        self.assertEqual(rows[0].status, "OFFLINE")
        self.assertEqual(rows[0].app_presence["status"], "ONLINE")
        self.assertFalse(rows[0].host_control_available)
        self.assertEqual(len(self.session.exec(select(Device)).all()), 2)
        logout_device(payload, self.session, self.user)
        rows, _ = await self.listing()
        self.assertEqual(rows[0].app_presence["status"], "LOGGED_OUT")
        bind_app_device(app.serial, DeviceAppBindingRequest(), self.session, self.user)
        self.assertEqual(len((await self.listing())[0]), 2)

    async def test_binding_requires_owner_same_platform_and_unique_target(self):
        app, payload = self.register()
        self.session.add_all([Device(serial="android-usb", platform="android"), Device(serial="ios-usb", platform="ios")])
        self.session.commit()
        for target, owner, code in [("android-usb", self.user, 400), ("ios-usb", SimpleNamespace(id=2), 403), (app.serial, self.user, 400)]:
            with self.assertRaises(HTTPException) as error:
                bind_app_device(app.serial, DeviceAppBindingRequest(physical_serial=target), self.session, owner)
            self.assertEqual(error.exception.status_code, code)
        bind_app_device(app.serial, DeviceAppBindingRequest(physical_serial="ios-usb"), self.session, self.user)
        other, _ = self.register(identity="app-2")
        with self.assertRaises(HTTPException) as error:
            bind_app_device(other.serial, DeviceAppBindingRequest(physical_serial="ios-usb"), self.session, self.user)
        self.assertEqual(error.exception.status_code, 409)
        with self.assertRaises(HTTPException):
            logout_device(payload, self.session, SimpleNamespace(id=2))

    async def test_host_operations_reject_app_ids_before_touching_device(self):
        for platform in ("android", "ios"):
            app, _ = self.register(platform, platform)
            with self.assertRaises(HTTPException) as error:
                await get_screenshot(app.serial, self.session)
            self.assertEqual(error.exception.status_code, 409)
            with self.assertRaises(HTTPException):
                await unlock_device(app.serial, self.session, self.user)
            with self.assertRaises(HTTPException):
                check_ios_wda(app.serial, self.session)

    async def test_legacy_registration_recovers_and_unknown_readiness_is_not_ready(self):
        app, _ = self.register(state=None)
        self.assertEqual(app_presence(app)["executor_status"], "UNKNOWN")
        app.app_last_seen_at = None
        app.updated_at = datetime.now() - timedelta(minutes=5)
        self.session.add(app); self.session.commit()
        self.assertEqual((await self.listing())[0][0].status, "OFFLINE")

    def test_presence_migration_is_additive_and_repeatable(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE device (id INTEGER PRIMARY KEY, serial VARCHAR)")
        conn.execute("INSERT INTO device VALUES (1, 'existing')")
        _migrate_app_device_presence(conn.cursor())
        _migrate_app_device_presence(conn.cursor())
        self.assertEqual(conn.execute("SELECT serial, app_executor_status FROM device").fetchone(), ("existing", "UNKNOWN"))
        conn.close()
