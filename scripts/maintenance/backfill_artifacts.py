"""Backfill legacy report files into the content-addressed asset store.

The migration is deliberately non-destructive and resumable: each database
owner is committed independently, existing active asset IDs are reused, and
legacy report paths remain untouched for rollback reads.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PIL import Image
from sqlmodel import Session, col, select

from backend.artifact_store import (
    ASSET_STATUS_ACTIVE,
    RETENTION_HOT,
    RETENTION_PINNED,
    read_asset,
    release_owner_references,
    retention_expiry,
    store_file,
    upsert_reference,
)
from backend.database import engine
from backend.models import (
    AssetReference,
    CompatibilityPageResult,
    CompatibilityRun,
    InspectionFault,
    InspectionObservation,
    InspectionRun,
    InspectionState,
    StoredAsset,
)
from backend.paths import project_path


def _report_target(value: Optional[str]) -> Optional[Path]:
    text = str(value or "").strip()
    if not text:
        return None
    root = project_path("reports").resolve()
    candidate = (root / text).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    current = root
    for part in candidate.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            return None
    return candidate


def _report_file(value: Optional[str]) -> Optional[Path]:
    candidate = _report_target(value)
    return candidate if candidate is not None and candidate.is_file() else None


def _active_asset(session: Session, asset_id: Optional[str]) -> Optional[StoredAsset]:
    if not asset_id:
        return None
    row = session.get(StoredAsset, asset_id)
    if row is None or row.status != ASSET_STATUS_ACTIVE:
        return None
    return row


def _asset_for_path(
    session: Session,
    *,
    asset_id: Optional[str],
    legacy_path: Optional[str],
) -> Optional[StoredAsset]:
    existing = _active_asset(session, asset_id)
    if existing is not None:
        return existing
    source = _report_file(legacy_path)
    if source is None:
        return None
    return store_file(session, source, commit=False)


def _upsert_owner_assets(
    session: Session,
    *,
    owner_type: str,
    owner_id: int,
    assets: Iterable[tuple[str, Optional[str], str]],
    pinned_reason: Optional[str] = None,
) -> None:
    release_owner_references(
        session,
        owner_type=owner_type,
        owner_id=owner_id,
        commit=False,
    )
    for role, asset_id, retention_class in assets:
        if not asset_id:
            continue
        upsert_reference(
            session,
            asset_id=asset_id,
            owner_type=owner_type,
            owner_id=owner_id,
            role=role,
            retention_class=retention_class,
            pinned_reason=(pinned_reason if retention_class == RETENTION_PINNED else None),
            commit=False,
        )


def _backfill_state(session: Session, state: InspectionState) -> bool:
    observation = None
    if state.representative_observation_id:
        observation = session.get(
            InspectionObservation,
            state.representative_observation_id,
        )
    if observation is None:
        observation = session.exec(
            select(InspectionObservation)
            .where(InspectionObservation.state_id == state.id)
            .order_by(
                col(InspectionObservation.is_representative).desc(),
                col(InspectionObservation.id).asc(),
            )
        ).first()

    run = session.get(InspectionRun, state.run_id)
    if observation is None:
        observation = InspectionObservation(
            run_id=state.run_id,
            branch_run_id=state.branch_run_id,
            state_id=state.id,
            template_id=state.template_id,
            sequence=int(state.id or 0),
            capture_kind="LEGACY",
            package_name=(run.package_name if run else state.foreground_package),
            activity=state.activity,
            exact_cluster_key=state.cluster_key or "",
            exact_replay_key=state.state_key or "",
            exact_state_key=state.state_key or "",
            screenshot_sha=state.screenshot_sha,
            screenshot_phash=state.perceptual_hash,
            perceptual_hash=state.perceptual_hash,
            is_representative=True,
            captured_at=state.created_at,
            created_at=datetime.now(),
        )
        session.add(observation)
        session.flush()

    action_path = None
    if state.screenshot_path:
        action_path = str(Path(state.screenshot_path).with_name("actions.json"))
    screenshot = _asset_for_path(
        session,
        asset_id=observation.screenshot_asset_id,
        legacy_path=state.screenshot_path,
    )
    xml = _asset_for_path(
        session,
        asset_id=observation.xml_asset_id,
        legacy_path=state.xml_path,
    )
    thumbnail = _asset_for_path(
        session,
        asset_id=observation.thumbnail_asset_id,
        legacy_path=state.thumbnail_path,
    )
    action_map = _asset_for_path(
        session,
        asset_id=observation.action_map_asset_id,
        legacy_path=action_path,
    )

    observation.screenshot_asset_id = screenshot.id if screenshot else None
    observation.xml_asset_id = xml.id if xml else None
    observation.thumbnail_asset_id = thumbnail.id if thumbnail else None
    observation.action_map_asset_id = action_map.id if action_map else None
    observation.original_width = screenshot.original_width if screenshot else None
    observation.original_height = screenshot.original_height if screenshot else None
    observation.is_representative = True
    observation.retention_class = (
        RETENTION_PINNED if state.selected_for_regression else RETENTION_HOT
    )
    observation.retained_until = retention_expiry(observation.retention_class)
    present = sum(
        bool(item)
        for item in (
            observation.screenshot_asset_id,
            observation.xml_asset_id,
            observation.thumbnail_asset_id,
            observation.action_map_asset_id,
        )
    )
    observation.asset_status = "AVAILABLE" if present == 4 else ("PARTIAL" if present else "UNAVAILABLE")
    observation.metadata_only = present == 0
    session.add(observation)
    session.flush()

    retention = observation.retention_class
    _upsert_owner_assets(
        session,
        owner_type="inspection_observation",
        owner_id=int(observation.id),
        assets=(
            ("screenshot", observation.screenshot_asset_id, retention),
            ("xml", observation.xml_asset_id, retention),
            ("thumbnail", observation.thumbnail_asset_id, retention),
            ("action_map", observation.action_map_asset_id, retention),
        ),
        pinned_reason=f"selected inspection regression state {state.id}",
    )
    state.representative_observation_id = observation.id
    state.observation_count = max(1, int(state.observation_count or 0))
    state.last_observed_at = state.last_observed_at or observation.captured_at
    session.add(state)
    return present > 0


def _backfill_page_result(session: Session, row: CompatibilityPageResult) -> bool:
    baseline_screenshot = _asset_for_path(
        session,
        asset_id=row.baseline_screenshot_asset_id,
        legacy_path=row.baseline_screenshot_path,
    )
    candidate_screenshot = _asset_for_path(
        session,
        asset_id=row.candidate_screenshot_asset_id,
        legacy_path=row.candidate_screenshot_path,
    )
    baseline_xml = _asset_for_path(
        session,
        asset_id=row.baseline_xml_asset_id,
        legacy_path=row.baseline_xml_path,
    )
    candidate_xml = _asset_for_path(
        session,
        asset_id=row.candidate_xml_asset_id,
        legacy_path=row.candidate_xml_path,
    )
    diff = None
    if str(row.status or "").upper() != "PASS":
        diff = _asset_for_path(
            session,
            asset_id=row.diff_screenshot_asset_id,
            legacy_path=row.diff_screenshot_path,
        )

    row.baseline_screenshot_asset_id = baseline_screenshot.id if baseline_screenshot else None
    row.candidate_screenshot_asset_id = candidate_screenshot.id if candidate_screenshot else None
    row.baseline_xml_asset_id = baseline_xml.id if baseline_xml else None
    row.candidate_xml_asset_id = candidate_xml.id if candidate_xml else None
    row.diff_screenshot_asset_id = diff.id if diff else None
    session.add(row)
    session.flush()

    _upsert_owner_assets(
        session,
        owner_type="compatibility_page_result",
        owner_id=int(row.id),
        assets=(
            ("baseline_screenshot", row.baseline_screenshot_asset_id, RETENTION_PINNED),
            ("baseline_xml", row.baseline_xml_asset_id, RETENTION_PINNED),
            ("candidate_screenshot", row.candidate_screenshot_asset_id, RETENTION_PINNED),
            ("candidate_xml", row.candidate_xml_asset_id, RETENTION_PINNED),
            ("diff_screenshot", row.diff_screenshot_asset_id, RETENTION_PINNED),
        ),
        pinned_reason=f"compatibility report evidence {row.id}",
    )
    return any(
        (
            row.baseline_screenshot_asset_id,
            row.candidate_screenshot_asset_id,
            row.baseline_xml_asset_id,
            row.candidate_xml_asset_id,
            row.diff_screenshot_asset_id,
        )
    )


def _snapshot_key(index: int, page: Dict[str, Any]) -> str:
    raw = str(page.get("key") or page.get("name") or f"page_{index}")
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", raw).strip("_")[:80] or f"page_{index}"


def _backfill_run_snapshot(session: Session, run: CompatibilityRun) -> bool:
    pages = [dict(item) for item in run.page_set_snapshot or [] if isinstance(item, dict)]
    changed = False
    for index, page in enumerate(pages, start=1):
        key = _snapshot_key(index, page)
        for kind, path_field, asset_field in (
            ("screenshot", "baseline_screenshot_path", "baseline_screenshot_asset_id"),
            ("xml", "baseline_xml_path", "baseline_xml_asset_id"),
        ):
            asset = _asset_for_path(
                session,
                asset_id=page.get(asset_field),
                legacy_path=page.get(path_field),
            )
            if asset is None:
                continue
            if page.get(asset_field) != asset.id:
                page[asset_field] = asset.id
                changed = True
            upsert_reference(
                session,
                asset_id=asset.id,
                owner_type="compatibility_run",
                owner_id=int(run.id),
                role=f"baseline:{key}:{kind}",
                retention_class=RETENTION_PINNED,
                pinned_reason=f"inspection baseline for compatibility run {run.id}",
                commit=False,
            )
    if changed:
        run.page_set_snapshot = pages
        session.add(run)
    return changed


def _backfill_fault(session: Session, fault: InspectionFault) -> bool:
    assets = []
    for role, path_value in (
        ("full_log", fault.full_log_path),
        ("screenshot", fault.screenshot_path),
        ("xml", fault.xml_path),
        ("replay", fault.replay_path),
        ("trace", fault.trace_path),
    ):
        source = _report_file(path_value)
        if source is None:
            continue
        asset = store_file(session, source, commit=False)
        assets.append((role, asset.id, RETENTION_PINNED))
    _upsert_owner_assets(
        session,
        owner_type="inspection_fault",
        owner_id=int(fault.id),
        assets=assets,
        pinned_reason=f"inspection fault evidence {fault.id}",
    )
    return bool(assets)


def _run_rows(
    session: Session,
    model: Any,
    handler: Any,
    summary_key: str,
    summary: Dict[str, int],
    *,
    after_id: int,
    limit: Optional[int],
) -> None:
    query = select(model).where(col(model.id) > after_id).order_by(col(model.id).asc())
    if limit is not None:
        query = query.limit(limit)
    for row in session.exec(query).all():
        try:
            populated = bool(handler(session, row))
            session.commit()
            summary[summary_key] += 1
            if populated:
                summary["owners_with_assets"] += 1
        except Exception as exc:
            session.rollback()
            summary["failed"] += 1
            print(f"backfill failed: {model.__name__} id={row.id}: {exc}", file=sys.stderr)


def backfill(
    *,
    after_id: int = 0,
    limit: Optional[int] = None,
    include_faults: bool = True,
) -> Dict[str, int]:
    summary = {
        "inspection_states": 0,
        "compatibility_page_results": 0,
        "compatibility_runs": 0,
        "inspection_faults": 0,
        "owners_with_assets": 0,
        "failed": 0,
    }
    with Session(engine) as session:
        _run_rows(
            session,
            InspectionState,
            _backfill_state,
            "inspection_states",
            summary,
            after_id=after_id,
            limit=limit,
        )
        _run_rows(
            session,
            CompatibilityPageResult,
            _backfill_page_result,
            "compatibility_page_results",
            summary,
            after_id=after_id,
            limit=limit,
        )
        _run_rows(
            session,
            CompatibilityRun,
            _backfill_run_snapshot,
            "compatibility_runs",
            summary,
            after_id=after_id,
            limit=limit,
        )
        if include_faults:
            _run_rows(
                session,
                InspectionFault,
                _backfill_fault,
                "inspection_faults",
                summary,
                after_id=after_id,
                limit=limit,
            )
    return summary


def _legacy_encoded_bytes(asset: StoredAsset, body: bytes, target: Path) -> bytes:
    if not str(asset.media_type or "").startswith("image/"):
        return body
    with Image.open(io.BytesIO(body)) as source:
        image = source.convert("RGB")
        output = io.BytesIO()
        if target.suffix.lower() in {".jpg", ".jpeg"}:
            image.save(output, format="JPEG", quality=95, optimize=True)
        elif target.suffix.lower() == ".webp":
            image.save(
                output,
                format="WEBP",
                lossless=True,
                quality=100,
                method=4,
                exact=True,
            )
        else:
            image.save(output, format="PNG", optimize=True)
        return output.getvalue()


def _write_materialized_asset(
    session: Session,
    *,
    asset_id: Optional[str],
    legacy_path: Optional[str],
    force: bool,
) -> str:
    if not asset_id:
        return "missing"
    target = _report_target(legacy_path)
    if target is None:
        return "missing"
    if target.is_file() and not force:
        return "existing"
    payload = read_asset(session, asset_id, transparent=True)
    body = _legacy_encoded_bytes(payload.asset, payload.body, target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return "written"


def materialize_legacy_paths(*, force: bool = False) -> Dict[str, int]:
    """Recreate legacy report paths before rolling back a CAS-only release."""
    summary = {"written": 0, "existing": 0, "missing": 0, "failed": 0}
    seen: set[Path] = set()
    with Session(engine) as session:
        def materialize(asset_id: Optional[str], legacy_path: Optional[str]) -> None:
            target = _report_target(legacy_path)
            if target is not None and target in seen:
                return
            try:
                result = _write_materialized_asset(
                    session,
                    asset_id=asset_id,
                    legacy_path=legacy_path,
                    force=force,
                )
                summary[result] += 1
                if target is not None and result in {"written", "existing"}:
                    seen.add(target)
            except Exception as exc:
                summary["failed"] += 1
                print(
                    f"legacy materialization failed: asset={asset_id} path={legacy_path}: {exc}",
                    file=sys.stderr,
                )

        for state in session.exec(select(InspectionState)).all():
            observation = (
                session.get(InspectionObservation, state.representative_observation_id)
                if state.representative_observation_id is not None
                else None
            )
            if observation is None:
                continue
            materialize(observation.screenshot_asset_id, state.screenshot_path)
            materialize(observation.xml_asset_id, state.xml_path)
            materialize(observation.thumbnail_asset_id, state.thumbnail_path)
            action_path = (
                str(Path(state.screenshot_path).with_name("actions.json"))
                if state.screenshot_path
                else None
            )
            materialize(observation.action_map_asset_id, action_path)

        for row in session.exec(select(CompatibilityPageResult)).all():
            for asset_id, path_value in (
                (row.baseline_screenshot_asset_id, row.baseline_screenshot_path),
                (row.candidate_screenshot_asset_id, row.candidate_screenshot_path),
                (row.diff_screenshot_asset_id, row.diff_screenshot_path),
                (row.baseline_xml_asset_id, row.baseline_xml_path),
                (row.candidate_xml_asset_id, row.candidate_xml_path),
            ):
                materialize(asset_id, path_value)

        for run in session.exec(select(CompatibilityRun)).all():
            for page in run.page_set_snapshot or []:
                if not isinstance(page, dict):
                    continue
                materialize(
                    page.get("baseline_screenshot_asset_id"),
                    page.get("baseline_screenshot_path"),
                )
                materialize(
                    page.get("baseline_xml_asset_id"),
                    page.get("baseline_xml_path"),
                )

        for fault in session.exec(select(InspectionFault)).all():
            for role, path_value in (
                ("full_log", fault.full_log_path),
                ("screenshot", fault.screenshot_path),
                ("xml", fault.xml_path),
                ("replay", fault.replay_path),
                ("trace", fault.trace_path),
            ):
                reference = session.exec(
                    select(AssetReference).where(
                        AssetReference.owner_type == "inspection_fault",
                        AssetReference.owner_id == fault.id,
                        AssetReference.role == role,
                        AssetReference.released_at == None,  # noqa: E711
                    )
                ).first()
                materialize(reference.asset_id if reference else None, path_value)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Idempotently backfill legacy report artifacts into CAS",
    )
    parser.add_argument("--after-id", type=int, default=0)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--skip-faults", action="store_true")
    parser.add_argument(
        "--materialize-legacy",
        action="store_true",
        help="recreate legacy report paths from CAS before an application rollback",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite existing legacy files while materializing",
    )
    args = parser.parse_args()
    if args.materialize_legacy:
        result = materialize_legacy_paths(force=args.force)
    else:
        result = backfill(
            after_id=max(0, args.after_id),
            limit=(max(1, args.limit) if args.limit is not None else None),
            include_faults=not args.skip_faults,
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
