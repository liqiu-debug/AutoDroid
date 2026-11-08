"""设备展示名（型号）口径：iOS 机型标识翻译、手机名不占型号位、客户端执行记录的型号快照。

真实数据（2026-11-08 云端）：USB iPhone model=iPhone18,2 / market_name=手机名（服务端同步写入），
桌面客户端执行记录 device_info=「桌面客户端 · <UDID>」，报告里看到的是一串编号。
"""
import os
import sqlite3
import tempfile
import unittest

from sqlmodel import Session, SQLModel, create_engine, select

from backend.api.autojs import DeviceRegisterPayload, register_device
from backend.api.client_devices import ClientDeviceRegisterRequest, register_client_device
from backend.api.client_runs import _client_device_display
from backend.database import _migrate_device_display_names
from backend.ios_models import ios_marketing_name, is_product_type
from backend.models import Device, User

UDID = "00008150-000C103434C0401C"


class IosModelTableTests(unittest.TestCase):
    def test_translation(self):
        self.assertEqual(ios_marketing_name("iPhone15,2"), "iPhone 14 Pro")
        self.assertEqual(ios_marketing_name("iPhone18,2"), "iPhone 17 Pro Max")
        self.assertEqual(ios_marketing_name("iPhone 14 Pro"), "iPhone 14 Pro")
        self.assertEqual(ios_marketing_name("iPhone99,9"), "iPhone99,9")
        self.assertTrue(is_product_type("iPad14,5"))
        self.assertFalse(is_product_type("iPhone"))

    def test_scanner_reexports_table(self):
        try:
            from backend.ios_scanner import IPHONE_MODEL_MAP
        except ImportError:  # pragma: no cover - 无 tidevice 的环境
            self.skipTest("tidevice 未安装")
        self.assertEqual(IPHONE_MODEL_MAP["iPhone18,1"], "iPhone 17 Pro")


class RegistrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.user = User(username="alice", hashed_password="x", role="user")
        self.session.add(self.user)
        self.session.commit()
        self.session.refresh(self.user)

    def tearDown(self) -> None:
        self.session.close()
        self.engine.dispose()

    def _client_payload(self, **overrides):
        data = dict(platform="android", serial="3B6F68E6SA7TA0Q5", model="PKZ110", brand="OPPO",
                    android_version="15", os_version="15", resolution="1264x2780")
        data.update(overrides)
        return ClientDeviceRegisterRequest(**data)

    def test_client_register_stores_android_market_name(self):
        read = register_client_device(self._client_payload(market_name="OPPO Reno14 Pro"), self.session, self.user)
        self.assertEqual((read.model, read.market_name), ("PKZ110", "OPPO Reno14 Pro"))
        # 市场名与型号相同 / 未上报：不覆盖已有值
        read = register_client_device(self._client_payload(market_name="PKZ110"), self.session, self.user)
        self.assertEqual(read.market_name, "OPPO Reno14 Pro")

    def test_client_register_ios_translates_code_and_drops_phone_name(self):
        self.session.add(Device(serial=UDID, platform="ios", model="iPhone18,2", market_name="华为17 Pro Max",
                                owner_id=self.user.id))
        self.session.commit()
        read = register_client_device(
            self._client_payload(platform="ios", serial=UDID, model="iPhone18,2", brand="APPLE"), self.session, self.user
        )
        self.assertEqual((read.model, read.market_name), ("iPhone 17 Pro Max", None))

    def test_ios_app_register_translates_model_and_clears_legacy_default_name(self):
        payload = DeviceRegisterPayload(platform="ios", udid="IDFV-1", model="iPhone15,2", name="AutoDroid · iPhone")
        register_device(payload, self.session, self.user)  # 旧版 App：带默认名
        device = self.session.exec(select(Device)).one()
        self.assertEqual((device.model, device.custom_name), ("iPhone 14 Pro", "AutoDroid · iPhone"))
        register_device(DeviceRegisterPayload(platform="ios", udid="IDFV-1", model="iPhone15,2"), self.session, self.user)
        self.session.refresh(device)
        self.assertIsNone(device.custom_name, "新版 App 不再上报 name：清掉旧默认名，型号显示出来")

    def test_ios_app_register_keeps_user_chosen_name(self):
        self.session.add(Device(serial="ios:IDFV-2", platform="ios", custom_name="测试机 A"))
        self.session.commit()
        register_device(DeviceRegisterPayload(platform="ios", udid="IDFV-2", model="iPhone15,2"), self.session, self.user)
        self.assertEqual(self.session.exec(select(Device)).one().custom_name, "测试机 A")

    def test_android_app_register_unchanged(self):
        register_device(DeviceRegisterPayload(platform="android", android_id="a1", model="PKZ110", name="我的手机"),
                        self.session, self.user)
        device = self.session.exec(select(Device)).one()
        self.assertEqual((device.model, device.custom_name), ("PKZ110", "我的手机"))

    def test_client_run_device_snapshot(self):
        self.assertEqual(_client_device_display(self.session, None), "桌面客户端")
        self.assertEqual(_client_device_display(self.session, UDID), f"桌面客户端 · {UDID}")
        self.session.add(Device(serial=UDID, platform="ios", model="iPhone 17 Pro Max"))
        self.session.add(Device(serial="A1", platform="android", model="PKZ110", market_name="OPPO Reno14 Pro"))
        self.session.add(Device(serial="A2", platform="android", model="PKZ110", custom_name="客厅机"))
        self.session.commit()
        self.assertEqual(_client_device_display(self.session, UDID), "iPhone 17 Pro Max")
        self.assertEqual(_client_device_display(self.session, "A1"), "OPPO Reno14 Pro")
        self.assertEqual(_client_device_display(self.session, "A2"), "客厅机")


class MigrationTests(unittest.TestCase):
    def test_041_fixes_existing_ios_rows_and_client_run_placeholders(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            conn = sqlite3.connect(tmp.name)
            conn.executescript(
                """
                CREATE TABLE device (id INTEGER PRIMARY KEY, serial TEXT, platform TEXT, model TEXT, market_name TEXT,
                                     custom_name TEXT);
                INSERT INTO device VALUES (1, 'U1', 'ios', 'iPhone18,2', '华为17 Pro Max', NULL);
                INSERT INTO device VALUES (2, 'U2', 'ios', 'iPhone 14 Pro', NULL, '测试机');
                INSERT INTO device VALUES (3, 'A1', 'android', 'PKZ110', 'OPPO Reno14 Pro', NULL);
                INSERT INTO device VALUES (4, 'ios:X', 'ios', 'iPhone', NULL, 'AutoDroid · iPhone');
                CREATE TABLE testexecution (id INTEGER PRIMARY KEY, device_serial TEXT, device_info TEXT);
                INSERT INTO testexecution VALUES (10, 'U1', '桌面客户端 · U1');
                INSERT INTO testexecution VALUES (11, 'A1', '桌面客户端 · A1');
                INSERT INTO testexecution VALUES (12, 'GONE', '桌面客户端 · GONE');
                INSERT INTO testexecution VALUES (13, 'U2', 'iPhone 14 Pro');
                """
            )
            _migrate_device_display_names(conn.cursor())
            rows = conn.execute("SELECT id, model, market_name, custom_name FROM device ORDER BY id").fetchall()
            self.assertEqual(rows, [
                (1, "iPhone 17 Pro Max", None, None),
                (2, "iPhone 14 Pro", None, "测试机"),
                (3, "PKZ110", "OPPO Reno14 Pro", None),
                (4, "iPhone", None, "AutoDroid · iPhone"),
            ])
            execs = conn.execute("SELECT id, device_info FROM testexecution ORDER BY id").fetchall()
            self.assertEqual(execs, [
                (10, "iPhone 17 Pro Max"),
                (11, "OPPO Reno14 Pro"),
                (12, "桌面客户端 · GONE"),  # 设备已删除：保留原值
                (13, "iPhone 14 Pro"),
            ])
            conn.close()
        finally:
            os.remove(tmp.name)
