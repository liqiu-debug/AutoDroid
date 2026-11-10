import io
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine, select

from backend.artifact_store import (
    ASSET_STATUS_ACTIVE,
    ASSET_STATUS_DELETED,
    RETENTION_HOT,
    RETENTION_PINNED,
    RETENTION_WARM,
    AssetGone,
    asset_storage_status,
    assets_root,
    cleanup_verified_legacy_files,
    derive_warm_image_bytes,
    gc_assets,
    read_asset,
    release_owner_references,
    remove_stale_references,
    resolve_storage_key,
    store_image_bytes,
    store_json,
    store_text,
    store_warm_image_derivative,
    upsert_reference,
)
from backend.models import (
    AssetReference,
    CompatibilityCell,
    CompatibilityPageResult,
    CompatibilityRun,
    StoredAsset,
)


class ArtifactStoreTests(unittest.TestCase):
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
        self.path_patch = patch(
            "backend.artifact_store.project_path",
            side_effect=lambda *parts: self.root.joinpath(*parts),
        )
        self.path_patch.start()
        self.disk_patch = patch(
            "backend.artifact_store.shutil.disk_usage",
            return_value=shutil._ntuple_diskusage(1_000_000, 100_000, 900_000),
        )
        self.disk_patch.start()

    def tearDown(self) -> None:
        self.disk_patch.stop()
        self.path_patch.stop()
        self.session.close()
        self.temp.cleanup()

    @staticmethod
    def _png(*, compress_level: int) -> bytes:
        image = Image.new("RGB", (7, 5), (20, 80, 160))
        output = io.BytesIO()
        image.save(output, format="PNG", compress_level=compress_level)
        return output.getvalue()

    def test_images_deduplicate_by_sanitized_pixels_and_round_trip_losslessly(self):
        first = store_image_bytes(self.session, self._png(compress_level=0))
        second = store_image_bytes(self.session, self._png(compress_level=9))

        self.assertEqual(first.id, second.id)
        self.assertEqual(first.media_type, "image/webp")
        self.assertEqual(first.encoding, "WEBP_LOSSLESS")
        self.assertEqual(first.integrity_status, "VERIFIED")
        self.assertIsNotNone(first.last_verified_at)
        self.assertEqual((first.width, first.height), (7, 5))
        self.assertEqual((first.original_width, first.original_height), (7, 5))
        with Image.open(io.BytesIO(read_asset(self.session, first.id).body)) as restored:
            self.assertEqual(restored.convert("RGB").getpixel((2, 3)), (20, 80, 160))

    def test_xml_and_json_are_deterministically_gzipped_and_transparently_read(self):
        xml = store_text(self.session, "<hierarchy><node text='A'/></hierarchy>")
        json_a = store_json(self.session, {"b": 2, "a": 1})
        json_b = store_json(self.session, {"a": 1, "b": 2})

        self.assertEqual(resolve_storage_key(xml.storage_key).read_bytes()[:2], b"\x1f\x8b")
        self.assertEqual(
            read_asset(self.session, xml.id).body,
            b"<hierarchy><node text='A'/></hierarchy>",
        )
        self.assertEqual(json_a.id, json_b.id)
        self.assertEqual(read_asset(self.session, json_a.id).body, b'{"a":1,"b":2}')

    def test_release_records_grace_and_gc_waits_24_hours(self):
        asset = store_text(self.session, "evidence")
        upsert_reference(
            self.session,
            asset_id=asset.id,
            owner_type="compatibility_run",
            owner_id=41,
            role="baseline",
            retention_class=RETENTION_HOT,
        )

        release_owner_references(
            self.session,
            owner_type="compatibility_run",
            owner_id=41,
        )
        reference = self.session.exec(select(AssetReference)).one()
        self.assertIsNotNone(reference.released_at)
        self.assertEqual(reference.grace_until - reference.released_at, timedelta(hours=24))

        before = gc_assets(self.session, now=reference.grace_until - timedelta(seconds=1))
        self.assertEqual(before["deleted_assets"], 0)
        after = gc_assets(self.session, now=reference.grace_until + timedelta(seconds=1))
        self.assertEqual(after["deleted_assets"], 1)
        self.assertEqual(self.session.get(StoredAsset, asset.id).status, ASSET_STATUS_DELETED)

    def test_expired_warm_reference_gets_grace_while_pinned_asset_survives(self):
        warm = store_text(self.session, "warm")
        pinned = store_text(self.session, "pinned")
        now = warm.created_at + timedelta(days=100)
        upsert_reference(
            self.session,
            asset_id=warm.id,
            owner_type="test_owner_warm",
            owner_id=1,
            role="candidate",
            retention_class=RETENTION_WARM,
            expires_at=now - timedelta(seconds=1),
        )
        upsert_reference(
            self.session,
            asset_id=pinned.id,
            owner_type="test_owner",
            owner_id=1,
            role="baseline",
            retention_class=RETENTION_PINNED,
        )

        summary = gc_assets(self.session, now=now)
        self.assertEqual(summary["expired_references"], 1)
        self.assertEqual(self.session.get(StoredAsset, warm.id).status, ASSET_STATUS_ACTIVE)
        warm_ref = self.session.exec(
            select(AssetReference).where(AssetReference.asset_id == warm.id)
        ).one()
        self.assertIsNotNone(warm_ref.grace_until)
        gc_assets(self.session, now=warm_ref.grace_until + timedelta(seconds=1))
        self.assertEqual(self.session.get(StoredAsset, warm.id).status, ASSET_STATUS_DELETED)
        self.assertEqual(self.session.get(StoredAsset, pinned.id).status, ASSET_STATUS_ACTIVE)

    def test_missing_blob_returns_gone_and_store_is_not_under_public_reports(self):
        asset = store_text(self.session, "gone")
        self.assertEqual(assets_root(), (self.root / "asset_store").resolve())
        self.assertNotIn("reports", resolve_storage_key(asset.storage_key).parts)
        resolve_storage_key(asset.storage_key).unlink()

        with self.assertRaises(AssetGone):
            read_asset(self.session, asset.id)

    def test_capacity_status_counts_pinned_references(self):
        asset = store_text(self.session, "pin")
        upsert_reference(
            self.session,
            asset_id=asset.id,
            owner_type="compatibility_run",
            owner_id=7,
            role="baseline",
            retention_class=RETENTION_PINNED,
        )
        status = asset_storage_status(self.session)
        self.assertEqual(status["stored_bytes"], asset.byte_size)
        self.assertEqual(status["pinned_bytes"], asset.byte_size)
        self.assertEqual(status["pinned_reference_count"], 1)
        self.assertIn("used_percent", status)
        self.assertTrue(status["can_start"])

    def test_stale_owner_reference_is_soft_released_with_grace(self):
        asset = store_text(self.session, "stale")
        upsert_reference(
            self.session,
            asset_id=asset.id,
            owner_type="compatibility_run",
            owner_id=999,
            role="baseline",
            retention_class=RETENTION_PINNED,
        )
        now = asset.created_at + timedelta(days=1)
        self.assertEqual(remove_stale_references(self.session, now=now), 1)
        reference = self.session.exec(select(AssetReference)).one()
        self.assertEqual(reference.released_at, now)
        self.assertEqual(reference.grace_until, now + timedelta(hours=24))

    def test_warm_derivative_uses_lanczos_scale(self):
        image = Image.new("RGB", (8, 8), (100, 20, 50))
        source_bytes = io.BytesIO()
        image.save(source_bytes, format="PNG")
        source = store_image_bytes(self.session, source_bytes.getvalue())
        derived_bytes = derive_warm_image_bytes(source_bytes.getvalue())
        with Image.open(io.BytesIO(derived_bytes)) as derived_image:
            self.assertEqual(derived_image.size, (6, 6))
        direct = store_warm_image_derivative(self.session, source.id)
        self.assertEqual(direct.scale, 0.75)
        self.assertEqual((direct.width, direct.height), (6, 6))
        self.assertEqual((direct.original_width, direct.original_height), (8, 8))

    def test_verified_legacy_file_is_kept_for_14_days_then_removed(self):
        run = CompatibilityRun(
            id=1,
            name="rollback-window",
            new_package_id=1,
            status="PASS",
        )
        self.session.add(run)
        self.session.flush()
        cell = CompatibilityCell(id=1, run_id=run.id, device_serial="d1")
        self.session.add(cell)
        self.session.flush()
        relative_path = f"compatibility/{run.id}/{cell.id}/baseline_screenshot.png"
        legacy_path = self.root / "reports" / relative_path
        legacy_path.parent.mkdir(parents=True)
        image_bytes = self._png(compress_level=6)
        legacy_path.write_bytes(image_bytes)
        result = CompatibilityPageResult(
            id=1,
            run_id=run.id,
            cell_id=cell.id,
            page_key="home",
            baseline_screenshot_path=relative_path,
        )
        self.session.add(result)
        asset = store_image_bytes(self.session, image_bytes)
        reference = upsert_reference(
            self.session,
            asset_id=asset.id,
            owner_type="compatibility_page_result",
            owner_id=result.id,
            role="baseline_screenshot",
            retention_class=RETENTION_HOT,
        )
        now = datetime.now()
        reference.created_at = now - timedelta(days=13)
        self.session.add(reference)
        self.session.commit()

        before = cleanup_verified_legacy_files(self.session, now=now)
        self.assertEqual(before["deleted"], 0)
        self.assertTrue(legacy_path.is_file())

        reference.created_at = now - timedelta(days=15)
        self.session.add(reference)
        self.session.commit()
        after = cleanup_verified_legacy_files(self.session, now=now)
        self.assertEqual(after["deleted"], 1)
        self.assertFalse(legacy_path.exists())


if __name__ == "__main__":
    unittest.main()
