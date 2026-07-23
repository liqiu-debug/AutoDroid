import unittest

from sqlmodel import Session, SQLModel, create_engine

from backend.feature_flags import (
    FLAG_COMPATIBILITY_INSTALLED_REPLAY,
    FLAG_COMPATIBILITY_LEGACY_COMPARE_CREATION,
    FLAG_CONTENT_ADDRESSED_ASSETS,
    FLAG_INSPECTION_EXPLORATION_FAMILY_CONVERGENCE,
    FLAG_INSPECTION_COVERAGE_SCHEDULER_V2,
    FLAG_INSPECTION_IDENTITY_V2,
    FLAG_INSPECTION_SIMILARITY_CONVERGENCE,
    FLAG_INSPECTION_VISUAL_HOME_ACTIONS,
    FLAG_IOS_EXECUTION,
    FLAG_MODEL_INSPECTION,
    FLAG_NEW_STEP_MODEL,
    FLAG_TIERED_ASSET_RETENTION,
    FLAG_WS_DISCONNECT_ABORT,
    is_flag_enabled,
)
from backend.models import SystemSetting


class FeatureFlagDefaultsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)

    def tearDown(self) -> None:
        self.session.close()

    def test_new_step_model_defaults_enabled(self):
        self.assertTrue(is_flag_enabled(self.session, FLAG_NEW_STEP_MODEL))

    def test_ios_and_ws_disconnect_flags_default_disabled(self):
        self.assertFalse(is_flag_enabled(self.session, FLAG_IOS_EXECUTION))
        self.assertFalse(is_flag_enabled(self.session, FLAG_WS_DISCONNECT_ABORT))
        self.assertFalse(is_flag_enabled(self.session, FLAG_MODEL_INSPECTION))

    def test_unknown_flag_defaults_to_false(self):
        self.assertFalse(is_flag_enabled(self.session, "unknown_flag"))

    def test_inspection_identity_and_family_coverage_default_enabled(self):
        self.assertTrue(
            is_flag_enabled(self.session, FLAG_INSPECTION_IDENTITY_V2)
        )
        self.assertTrue(
            is_flag_enabled(
                self.session,
                FLAG_INSPECTION_EXPLORATION_FAMILY_CONVERGENCE,
            )
        )
        self.assertTrue(
            is_flag_enabled(self.session, FLAG_COMPATIBILITY_INSTALLED_REPLAY)
        )
        self.assertFalse(
            is_flag_enabled(
                self.session,
                FLAG_COMPATIBILITY_LEGACY_COMPARE_CREATION,
            )
        )

    def test_destructive_inspection_rollout_flags_default_disabled(self):
        for flag in (
            FLAG_INSPECTION_SIMILARITY_CONVERGENCE,
            FLAG_CONTENT_ADDRESSED_ASSETS,
            FLAG_TIERED_ASSET_RETENTION,
            FLAG_INSPECTION_COVERAGE_SCHEDULER_V2,
            FLAG_INSPECTION_VISUAL_HOME_ACTIONS,
        ):
            self.assertFalse(is_flag_enabled(self.session, flag))

    def test_explicit_default_argument_still_wins_for_missing_setting(self):
        self.assertTrue(is_flag_enabled(self.session, "unknown_flag", default=True))
        self.assertFalse(is_flag_enabled(self.session, FLAG_NEW_STEP_MODEL, default=False))

    def test_db_false_value_overrides_enabled_default(self):
        self.session.add(SystemSetting(key=FLAG_NEW_STEP_MODEL, value="0"))
        self.session.commit()

        self.assertFalse(is_flag_enabled(self.session, FLAG_NEW_STEP_MODEL))

    def test_db_true_value_overrides_disabled_default(self):
        self.session.add(SystemSetting(key=FLAG_IOS_EXECUTION, value="true"))
        self.session.commit()

        self.assertTrue(is_flag_enabled(self.session, FLAG_IOS_EXECUTION))

    def test_invalid_db_value_falls_back_to_default(self):
        self.session.add(SystemSetting(key=FLAG_NEW_STEP_MODEL, value="whatever"))
        self.session.add(SystemSetting(key=FLAG_IOS_EXECUTION, value="whatever"))
        self.session.commit()

        self.assertTrue(is_flag_enabled(self.session, FLAG_NEW_STEP_MODEL))
        self.assertFalse(is_flag_enabled(self.session, FLAG_IOS_EXECUTION))


if __name__ == "__main__":
    unittest.main()
