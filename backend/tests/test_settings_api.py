import unittest

from fastapi import HTTPException
from sqlmodel import SQLModel, Session, create_engine, select

from backend.api.settings import SettingItem, get_feature_flags, save_settings
from backend.feature_flags import (
    FLAG_CONTENT_ADDRESSED_ASSETS,
    FLAG_INSPECTION_COVERAGE_SCHEDULER_V2,
    FLAG_INSPECTION_IDENTITY_V2,
    FLAG_INSPECTION_VISUAL_HOME_ACTIONS,
    FLAG_MODEL_INSPECTION,
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

    def test_effective_flags_close_inspection_children_with_master(self):
        flags = get_feature_flags(session=self.session, current_user=self.user)

        self.assertFalse(flags[FLAG_MODEL_INSPECTION])
        self.assertFalse(flags[FLAG_INSPECTION_IDENTITY_V2])
        self.assertFalse(flags[FLAG_INSPECTION_COVERAGE_SCHEDULER_V2])
        self.assertFalse(flags[FLAG_INSPECTION_VISUAL_HOME_ACTIONS])

    def test_explicit_parent_disable_overrides_child_true_in_same_request(self):
        response = save_settings(
            [
                SettingItem(key=FLAG_MODEL_INSPECTION, value="false"),
                SettingItem(key=FLAG_INSPECTION_IDENTITY_V2, value="true"),
                SettingItem(
                    key=FLAG_INSPECTION_COVERAGE_SCHEDULER_V2,
                    value="true",
                ),
                SettingItem(
                    key=FLAG_INSPECTION_VISUAL_HOME_ACTIONS,
                    value="true",
                ),
            ],
            session=self.session,
            current_user=self.user,
        )

        self.assertGreaterEqual(response["count"], 4)
        self.assertEqual(self._value(FLAG_MODEL_INSPECTION), "false")
        self.assertEqual(self._value(FLAG_INSPECTION_IDENTITY_V2), "false")
        self.assertEqual(
            self._value(FLAG_INSPECTION_COVERAGE_SCHEDULER_V2),
            "false",
        )
        self.assertEqual(
            self._value(FLAG_INSPECTION_VISUAL_HOME_ACTIONS),
            "false",
        )


if __name__ == "__main__":
    unittest.main()
