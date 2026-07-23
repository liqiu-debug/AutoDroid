import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine, func, select

from backend.models import (
    AssetReference,
    CompatibilityCell,
    CompatibilityPageResult,
    CompatibilityRun,
    InspectionBranchRun,
    InspectionObservation,
    InspectionRun,
    InspectionState,
    StoredAsset,
)
from scripts.maintenance import backfill_artifacts


class ArtifactBackfillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        fake_path = lambda *parts: self.root.joinpath(*parts)
        self.patches = [
            patch.object(backfill_artifacts, "engine", self.engine),
            patch("backend.artifact_store.project_path", side_effect=fake_path),
            patch.object(backfill_artifacts, "project_path", side_effect=fake_path),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    @staticmethod
    def _png() -> bytes:
        output = io.BytesIO()
        Image.new("RGB", (10, 12), (20, 30, 40)).save(output, format="PNG")
        return output.getvalue()

    def _write_report_assets(self, relative_dir: str) -> dict:
        directory = self.root / "reports" / relative_dir
        directory.mkdir(parents=True, exist_ok=True)
        screenshot = directory / "screenshot.png"
        xml = directory / "hierarchy.xml"
        thumbnail = directory / "thumbnail.jpg"
        actions = directory / "actions.json"
        diff = directory / "diff.png"
        screenshot.write_bytes(self._png())
        xml.write_text("<hierarchy/>", encoding="utf-8")
        Image.new("RGB", (4, 4), (20, 30, 40)).save(thumbnail, format="JPEG")
        actions.write_text('{"actions":[]}', encoding="utf-8")
        diff.write_bytes(self._png())
        root = self.root / "reports"
        return {
            "screenshot": screenshot.relative_to(root).as_posix(),
            "xml": xml.relative_to(root).as_posix(),
            "thumbnail": thumbnail.relative_to(root).as_posix(),
            "diff": diff.relative_to(root).as_posix(),
        }

    def test_backfill_is_idempotent_and_skips_pass_diff(self):
        inspection_files = self._write_report_assets("inspection/1/guest/1")
        compatibility_files = self._write_report_assets("compatibility/1/1/candidate/home")
        with Session(self.engine) as session:
            inspection_run = InspectionRun(
                id=1,
                name="legacy inspection",
                package_name="com.demo",
                device_serial="d1",
                status="PASS",
            )
            session.add(inspection_run)
            session.add(
                InspectionBranchRun(
                    id=1,
                    run_id=1,
                    branch_key="guest",
                    branch_name="Guest",
                    status="PASS",
                )
            )
            session.add(
                InspectionState(
                    id=1,
                    run_id=1,
                    branch_run_id=1,
                    branch_key="guest",
                    cluster_key="home",
                    state_key="home-v1",
                    stable_status="STABLE",
                    screenshot_path=inspection_files["screenshot"],
                    xml_path=inspection_files["xml"],
                    thumbnail_path=inspection_files["thumbnail"],
                )
            )
            run = CompatibilityRun(
                id=1,
                name="legacy compatibility",
                new_package_id=1,
                status="PASS",
                page_set_snapshot=[
                    {
                        "key": "home",
                        "name": "Home",
                        "baseline_screenshot_path": compatibility_files["screenshot"],
                        "baseline_xml_path": compatibility_files["xml"],
                    }
                ],
            )
            session.add(run)
            session.add(CompatibilityCell(id=1, run_id=1, device_serial="d1"))
            session.add(
                CompatibilityPageResult(
                    id=1,
                    run_id=1,
                    cell_id=1,
                    page_key="home",
                    status="PASS",
                    baseline_screenshot_path=compatibility_files["screenshot"],
                    candidate_screenshot_path=compatibility_files["screenshot"],
                    diff_screenshot_path=compatibility_files["diff"],
                    baseline_xml_path=compatibility_files["xml"],
                    candidate_xml_path=compatibility_files["xml"],
                )
            )
            session.commit()

        first = backfill_artifacts.backfill(include_faults=False)
        self.assertEqual(first["failed"], 0)
        with Session(self.engine) as session:
            first_asset_count = session.exec(select(func.count(StoredAsset.id))).one()
            first_reference_count = session.exec(
                select(func.count(AssetReference.id))
            ).one()
            observation = session.exec(select(InspectionObservation)).one()
            self.assertEqual(observation.capture_kind, "LEGACY")
            self.assertTrue(observation.is_representative)
            result = session.get(CompatibilityPageResult, 1)
            self.assertTrue(result.baseline_screenshot_asset_id)
            self.assertTrue(result.candidate_screenshot_asset_id)
            self.assertTrue(result.baseline_xml_asset_id)
            self.assertTrue(result.candidate_xml_asset_id)
            self.assertIsNone(result.diff_screenshot_asset_id)
            page_references = session.exec(
                select(AssetReference).where(
                    AssetReference.owner_type == "compatibility_page_result",
                    AssetReference.owner_id == result.id,
                    AssetReference.released_at == None,  # noqa: E711
                )
            ).all()
            self.assertEqual(
                {item.role for item in page_references},
                {
                    "baseline_screenshot",
                    "baseline_xml",
                    "candidate_screenshot",
                    "candidate_xml",
                },
            )
            self.assertEqual(
                {item.retention_class for item in page_references},
                {"PINNED"},
            )
            run = session.get(CompatibilityRun, 1)
            self.assertTrue(run.page_set_snapshot[0]["baseline_screenshot_asset_id"])

        second = backfill_artifacts.backfill(include_faults=False)
        self.assertEqual(second["failed"], 0)
        with Session(self.engine) as session:
            self.assertEqual(
                session.exec(select(func.count(StoredAsset.id))).one(),
                first_asset_count,
            )
            self.assertEqual(
                session.exec(select(func.count(AssetReference.id))).one(),
                first_reference_count,
            )
            self.assertEqual(
                session.exec(select(func.count(InspectionObservation.id))).one(),
                1,
            )

        inspection_screenshot = self.root / "reports" / inspection_files["screenshot"]
        inspection_xml = self.root / "reports" / inspection_files["xml"]
        compatibility_screenshot = (
            self.root / "reports" / compatibility_files["screenshot"]
        )
        compatibility_xml = self.root / "reports" / compatibility_files["xml"]
        for path in {
            inspection_screenshot,
            inspection_xml,
            compatibility_screenshot,
            compatibility_xml,
        }:
            path.unlink()

        restored = backfill_artifacts.materialize_legacy_paths()
        self.assertEqual(restored["failed"], 0)
        self.assertGreaterEqual(restored["written"], 4)
        with Image.open(inspection_screenshot) as image:
            self.assertEqual(image.convert("RGB").size, (10, 12))
        self.assertEqual(inspection_xml.read_text(encoding="utf-8"), "<hierarchy/>")
        with Image.open(compatibility_screenshot) as image:
            self.assertEqual(image.convert("RGB").size, (10, 12))
        self.assertEqual(
            compatibility_xml.read_text(encoding="utf-8"),
            "<hierarchy/>",
        )


if __name__ == "__main__":
    unittest.main()
