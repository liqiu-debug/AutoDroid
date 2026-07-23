import io
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine, select

from backend.api.compatibility import (
    _copy_inspection_baselines,
    _get_or_create_page_diff,
    _persist_snapshot_assets,
    _record_compare_result,
    _validate_inspection_source,
    delete_run as delete_compatibility_run,
)
from backend.artifact_store import store_image_bytes, store_text
from backend.feature_flags import FLAG_CONTENT_ADDRESSED_ASSETS
from backend.models import (
    AssetReference,
    CompatibilityCell,
    CompatibilityPageResult,
    CompatibilityRun,
    InspectionBranchRun,
    InspectionObservation,
    InspectionRun,
    InspectionState,
    SystemSetting,
    User,
)


class CompatibilityAssetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.session.add(
            SystemSetting(key=FLAG_CONTENT_ADDRESSED_ASSETS, value="true")
        )
        self.session.commit()
        fake_path = lambda *parts: self.root.joinpath(*parts)
        self.patches = [
            patch("backend.artifact_store.project_path", side_effect=fake_path),
            patch("backend.api.compatibility.project_path", side_effect=fake_path),
            patch("backend.inspection.engine.project_path", side_effect=fake_path),
            patch(
                "backend.artifact_store.shutil.disk_usage",
                return_value=shutil._ntuple_diskusage(1_000_000, 100_000, 900_000),
            ),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        self.session.close()
        self.temp.cleanup()

    @staticmethod
    def _png(*, changed: bool = False) -> bytes:
        image = Image.new("RGB", (20, 20), "white")
        if changed:
            ImageDraw.Draw(image).rectangle((0, 0, 10, 10), fill="black")
        output = io.BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()

    def _legacy_snapshot(self, name: str, *, changed: bool = False):
        directory = self.root / "reports" / "compatibility" / "1" / "1" / name
        directory.mkdir(parents=True, exist_ok=True)
        screenshot = directory / "screenshot.png"
        xml = directory / "hierarchy.xml"
        screenshot.write_bytes(self._png(changed=changed))
        xml.write_text("<hierarchy><node text='A'/></hierarchy>", encoding="utf-8")
        snapshot = {
            "screenshot_path": screenshot.relative_to(self.root / "reports").as_posix(),
            "screenshot_bytes": screenshot.read_bytes(),
            "xml_path": xml.relative_to(self.root / "reports").as_posix(),
            "xml_text": xml.read_text(encoding="utf-8"),
            "activity": "com.demo/.MainActivity",
            "logcat_errors": "",
        }
        return _persist_snapshot_assets(self.session, snapshot)

    def test_inspection_baseline_uses_pinned_cas_without_copy(self):
        source = self.root / "reports" / "inspection" / "9" / "guest" / "4"
        source.mkdir(parents=True)
        (source / "screenshot.png").write_bytes(self._png())
        (source / "hierarchy.xml").write_text("<hierarchy/>", encoding="utf-8")
        run = CompatibilityRun(name="snapshot", new_package_id=1)
        self.session.add(run)
        self.session.commit()

        pages = _copy_inspection_baselines(
            session=self.session,
            compatibility_run_id=run.id,
            inspection_run_id=9,
            pages=[
                {
                    "key": "home",
                    "name": "Home",
                    "baseline_screenshot_path": "inspection/9/guest/4/screenshot.png",
                    "baseline_xml_path": "inspection/9/guest/4/hierarchy.xml",
                }
            ],
        )

        self.assertTrue(pages[0]["baseline_screenshot_asset_id"])
        self.assertTrue(pages[0]["baseline_xml_asset_id"])
        self.assertFalse(
            (self.root / "reports" / "compatibility" / str(run.id) / "inspection_baseline").exists()
        )
        references = self.session.exec(
            select(AssetReference).where(
                AssetReference.owner_type == "compatibility_run",
                AssetReference.owner_id == run.id,
                AssetReference.released_at == None,  # noqa: E711
            )
        ).all()
        self.assertEqual(len(references), 2)
        self.assertEqual({item.retention_class for item in references}, {"PINNED"})

    def test_pass_has_no_permanent_diff_and_on_demand_diff_is_hot_cached(self):
        run = CompatibilityRun(name="compare", new_package_id=1, thresholds={})
        self.session.add(run)
        self.session.flush()
        cell = CompatibilityCell(run_id=run.id, device_serial="d1")
        self.session.add(cell)
        self.session.commit()
        baseline = self._legacy_snapshot("baseline")
        candidate = self._legacy_snapshot("candidate")

        _record_compare_result(
            self.session,
            run,
            cell,
            {"key": "home", "name": "Home"},
            baseline,
            candidate,
        )
        row = self.session.exec(select(CompatibilityPageResult)).one()
        self.assertEqual(row.status, "PASS")
        self.assertIsNone(row.diff_screenshot_path)
        self.assertIsNone(row.diff_screenshot_asset_id)
        references = self.session.exec(
            select(AssetReference).where(
                AssetReference.owner_type == "compatibility_page_result",
                AssetReference.owner_id == row.id,
                AssetReference.released_at == None,  # noqa: E711
            )
        ).all()
        by_role = {item.role: item for item in references}
        self.assertEqual(
            set(by_role),
            {
                "baseline_screenshot",
                "baseline_xml",
                "candidate_screenshot",
                "candidate_xml",
            },
        )
        self.assertEqual(
            {item.retention_class for item in references},
            {"PINNED"},
        )
        self.assertNotIn("diff_screenshot", by_role)

        first = _get_or_create_page_diff(self.session, row)
        second = _get_or_create_page_diff(self.session, row)
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(first["asset_id"], second["asset_id"])
        cached = self.session.exec(
            select(AssetReference).where(
                AssetReference.owner_type == "compatibility_page_result",
                AssetReference.owner_id == row.id,
                AssetReference.role == "on_demand_diff",
            )
        ).one()
        self.assertEqual(cached.retention_class, "HOT")
        self.assertIsNotNone(cached.expires_at)

        page_result_id = int(row.id)
        run.status = "PASS"
        self.session.add(run)
        self.session.commit()
        delete_compatibility_run(
            int(run.id),
            session=self.session,
            current_user=User(username="compatibility-user", hashed_password="x"),
        )
        released = self.session.exec(
            select(AssetReference).where(
                AssetReference.owner_type == "compatibility_page_result",
                AssetReference.owner_id == page_result_id,
            )
        ).all()
        self.assertEqual(len(released), 5)
        self.assertTrue(all(item.released_at is not None for item in released))
        self.assertTrue(all(item.grace_until is not None for item in released))

    def test_warning_persists_diff_as_pinned_evidence(self):
        run = CompatibilityRun(
            name="compare",
            new_package_id=1,
            thresholds={"pixel_diff_ratio_warn": 0.001, "ssim_warn": 0.999},
        )
        self.session.add(run)
        self.session.flush()
        cell = CompatibilityCell(run_id=run.id, device_serial="d1")
        self.session.add(cell)
        self.session.commit()
        baseline = self._legacy_snapshot("baseline")
        candidate = self._legacy_snapshot("candidate", changed=True)

        _record_compare_result(
            self.session,
            run,
            cell,
            {"key": "home", "name": "Home"},
            baseline,
            candidate,
        )
        row = self.session.exec(select(CompatibilityPageResult)).one()
        self.assertEqual(row.status, "WARNING")
        self.assertTrue(row.diff_screenshot_path)
        self.assertTrue(row.diff_screenshot_asset_id)
        diff_reference = self.session.exec(
            select(AssetReference).where(
                AssetReference.owner_type == "compatibility_page_result",
                AssetReference.owner_id == row.id,
                AssetReference.role == "diff_screenshot",
                AssetReference.released_at == None,  # noqa: E711
            )
        ).one()
        self.assertEqual(diff_reference.retention_class, "PINNED")

    def test_explicit_observation_freezes_cas_only_baseline(self):
        source_run = InspectionRun(
            name="source",
            package_name="com.demo",
            device_serial="d1",
            status="PASS",
            profile_snapshot={
                "branches": {"guest": {"name": "Guest"}},
                "input_rules": [],
                "sanitizer_rules": [],
            },
        )
        self.session.add(source_run)
        self.session.flush()
        branch = InspectionBranchRun(
            run_id=source_run.id,
            branch_key="guest",
            branch_name="Guest",
            status="PASS",
        )
        self.session.add(branch)
        self.session.flush()
        state = InspectionState(
            run_id=source_run.id,
            branch_run_id=branch.id,
            branch_key="guest",
            cluster_key="home",
            state_key="home-v1",
            stable_status="STABLE",
            locator_quality="RESOURCE_ID",
            selected_for_regression=True,
        )
        self.session.add(state)
        self.session.flush()
        screenshot = store_image_bytes(self.session, self._png())
        xml = store_text(self.session, "<hierarchy/>")
        observation = InspectionObservation(
            run_id=source_run.id,
            branch_run_id=branch.id,
            state_id=state.id,
            exact_cluster_key="home",
            exact_replay_key="home-v1",
            exact_state_key="home-v1",
            screenshot_asset_id=screenshot.id,
            xml_asset_id=xml.id,
            is_representative=True,
        )
        self.session.add(observation)
        self.session.commit()

        _, states, pages = _validate_inspection_source(
            self.session,
            inspection_run_id=source_run.id,
            inspection_state_ids=[state.id],
            inspection_observation_ids=[observation.id],
            package_name="com.demo",
        )
        self.assertEqual([item.id for item in states], [state.id])
        self.assertEqual(pages[0]["inspection_observation_id"], observation.id)
        self.assertEqual(pages[0]["baseline_screenshot_asset_id"], screenshot.id)
        self.assertEqual(pages[0]["baseline_xml_asset_id"], xml.id)
        self.assertIsNone(pages[0]["baseline_screenshot_path"])


if __name__ == "__main__":
    unittest.main()
