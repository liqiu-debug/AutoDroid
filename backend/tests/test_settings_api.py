import unittest

from fastapi import HTTPException
from sqlmodel import SQLModel, Session, create_engine, select

from backend.api.settings import SettingItem, get_feature_flags, save_settings
from backend.feature_flags import (
    FLAG_CONTENT_ADDRESSED_ASSETS,
    FLAG_DRIVER_POOL,
    FLAG_TIERED_ASSET_RETENTION,
)
from backend.models import SystemSetting, User


class SettingsDependencyTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
        )
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.user = User(username="settings-user", hashed_password="x")
        self.session.add(self.user)
        self.session.commit()

    def tearDown(self):
        self.session.close()

    def _value(self, key):
        row = self.session.exec(
            select(SystemSetting).where(SystemSetting.key == key)
        ).first()
        return row.value if row else None

    def test_disabling_cas_cascades_retention_off(self):
        self.session.add(
            SystemSetting(key=FLAG_CONTENT_ADDRESSED_ASSETS, value="true")
        )
        self.session.add(
            SystemSetting(key=FLAG_TIERED_ASSET_RETENTION, value="true")
        )
        self.session.commit()

        response = save_settings(
            [SettingItem(key=FLAG_CONTENT_ADDRESSED_ASSETS, value="false")],
            session=self.session,
            current_user=self.user,
        )

        self.assertEqual(response["count"], 2)
        self.assertEqual(self._value(FLAG_CONTENT_ADDRESSED_ASSETS), "false")
        self.assertEqual(self._value(FLAG_TIERED_ASSET_RETENTION), "false")

    def test_retention_cannot_be_enabled_without_cas(self):
        with self.assertRaises(HTTPException) as context:
            save_settings(
                [
                    SettingItem(
                        key=FLAG_TIERED_ASSET_RETENTION,
                        value="true",
                    )
                ],
                session=self.session,
                current_user=self.user,
            )

        self.assertEqual(context.exception.status_code, 422)

    def test_driver_pool_flag_roundtrips_through_settings_and_feature_flags(self):
        initial = get_feature_flags(session=self.session, current_user=self.user)
        self.assertIn(FLAG_DRIVER_POOL, initial)
        self.assertFalse(initial[FLAG_DRIVER_POOL])

        save_settings(
            [SettingItem(key=FLAG_DRIVER_POOL, value="true")],
            session=self.session,
            current_user=self.user,
        )
        updated = get_feature_flags(session=self.session, current_user=self.user)
        self.assertTrue(updated[FLAG_DRIVER_POOL])

        save_settings(
            [SettingItem(key=FLAG_DRIVER_POOL, value="false")],
            session=self.session,
            current_user=self.user,
        )
        disabled = get_feature_flags(session=self.session, current_user=self.user)
        self.assertFalse(disabled[FLAG_DRIVER_POOL])

    def test_recording_locator_settings_round_trip_and_priority_validation(self):
        from fastapi import HTTPException
        from backend.api.settings import get_recording_locator_settings
        from backend.feature_flags import (
            FLAG_RECORDING_ENABLE_CLASS_NAME,
            FLAG_RECORDING_ENABLE_RESOURCE_ID,
            RECORDING_PRIORITY_ID_FIRST,
            RECORDING_PRIORITY_SEMANTIC_FIRST,
            SETTING_RECORDING_LOCATOR_PRIORITY,
        )

        initial_flags = get_feature_flags(session=self.session, current_user=self.user)
        self.assertFalse(initial_flags[FLAG_RECORDING_ENABLE_RESOURCE_ID])
        self.assertFalse(initial_flags[FLAG_RECORDING_ENABLE_CLASS_NAME])

        initial_cfg = get_recording_locator_settings(session=self.session, current_user=self.user)
        self.assertEqual(
            initial_cfg,
            {
                "enable_resource_id": False,
                "enable_class_name": False,
                "priority": RECORDING_PRIORITY_SEMANTIC_FIRST,
            },
        )

        save_settings(
            [
                SettingItem(key=FLAG_RECORDING_ENABLE_RESOURCE_ID, value="true"),
                SettingItem(key=FLAG_RECORDING_ENABLE_CLASS_NAME, value="true"),
                SettingItem(key=SETTING_RECORDING_LOCATOR_PRIORITY, value=RECORDING_PRIORITY_ID_FIRST),
            ],
            session=self.session,
            current_user=self.user,
        )
        updated_cfg = get_recording_locator_settings(session=self.session, current_user=self.user)
        self.assertEqual(
            updated_cfg,
            {
                "enable_resource_id": True,
                "enable_class_name": True,
                "priority": RECORDING_PRIORITY_ID_FIRST,
            },
        )

        with self.assertRaises(HTTPException) as ctx:
            save_settings(
                [SettingItem(key=SETTING_RECORDING_LOCATOR_PRIORITY, value="invalid_mode")],
                session=self.session,
                current_user=self.user,
            )
        self.assertEqual(ctx.exception.status_code, 422)


if __name__ == "__main__":
    unittest.main()
