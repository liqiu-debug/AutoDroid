import unittest

from fastapi import HTTPException
from sqlmodel import SQLModel, Session, create_engine, select

from backend.api.settings import SettingItem, save_settings
from backend.feature_flags import (
    FLAG_CONTENT_ADDRESSED_ASSETS,
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


if __name__ == "__main__":
    unittest.main()
