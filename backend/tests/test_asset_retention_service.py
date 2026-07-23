import unittest
from unittest.mock import patch

from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine

from backend import retention_service
from backend.feature_flags import FLAG_TIERED_ASSET_RETENTION
from backend.models import SystemSetting


class AssetRetentionServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)

    def test_tiered_gc_runs_when_whole_report_retention_is_disabled(self):
        with Session(self.engine) as session:
            session.add(
                SystemSetting(key=FLAG_TIERED_ASSET_RETENTION, value="true")
            )
            session.commit()

        with (
            patch.object(retention_service, "engine", self.engine),
            patch(
                "backend.artifact_store.materialize_warm_derivatives",
                return_value={"derived": 1},
            ) as warm,
            patch(
                "backend.artifact_store.transition_warm_observations_to_cold",
                return_value={"transitioned": 1},
            ) as cold,
            patch(
                "backend.artifact_store.gc_assets",
                return_value={"deleted_assets": 2},
            ) as gc,
        ):
            summary = retention_service.run_retention_cleanup()

        self.assertTrue(summary["enabled"])
        self.assertFalse(summary["report_retention_enabled"])
        self.assertTrue(summary["asset_retention_enabled"])
        self.assertEqual(summary["assets"]["deleted_assets"], 2)
        warm.assert_called_once()
        cold.assert_called_once()
        gc.assert_called_once()
        self.assertEqual(gc.call_args.kwargs["low_watermark_percent"], 80.0)
        self.assertEqual(gc.call_args.kwargs["high_watermark_percent"], 90.0)
        self.assertEqual(gc.call_args.kwargs["critical_watermark_percent"], 95.0)


if __name__ == "__main__":
    unittest.main()
