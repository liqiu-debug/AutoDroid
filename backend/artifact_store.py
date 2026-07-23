"""Immutable content-addressed storage for report evidence.

The legacy report tree remains the compatibility surface during rollout.  When
``content_addressed_assets`` is enabled, callers dual-write immutable blobs here
and attach owner-scoped references.  Blob deletion is therefore independent of
legacy report-directory deletion and can be governed by reference retention.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import logging
import mimetypes
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

from PIL import Image
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from backend.database import engine
from backend.feature_flags import is_flag_enabled
from backend.models import (
    AssetReference,
    CompatibilityCell,
    CompatibilityPageResult,
    CompatibilityRun,
    InspectionFault,
    InspectionObservation,
    InspectionRun,
    InspectionState,
    InspectionTransition,
    StoredAsset,
)
from backend.paths import project_path

logger = logging.getLogger(__name__)

CONTENT_ADDRESSED_ASSETS_FLAG = "content_addressed_assets"
TIERED_ASSET_RETENTION_FLAG = "tiered_asset_retention"

ASSET_STATUS_ACTIVE = "ACTIVE"
ASSET_STATUS_MISSING = "MISSING"
ASSET_STATUS_DELETED = "DELETED"

RETENTION_HOT = "HOT"
RETENTION_WARM = "WARM"
RETENTION_PINNED = "PINNED"
RETENTION_COLD = "COLD"

# Transitional aliases for callers written during the first CAS rollout.
RETENTION_EVIDENCE = RETENTION_WARM
RETENTION_PIN = RETENTION_PINNED

HOT_RETENTION_DAYS = 7
EVIDENCE_RETENTION_DAYS = 90
LEGACY_ROLLBACK_DAYS = 14
UNREFERENCED_GRACE_HOURS = 24
DEFAULT_LOW_WATERMARK_PERCENT = 80.0
DEFAULT_HIGH_WATERMARK_PERCENT = 90.0
DEFAULT_CRITICAL_WATERMARK_PERCENT = 95.0


class AssetStoreError(RuntimeError):
    pass


class AssetNotFound(AssetStoreError):
    pass


class AssetGone(AssetStoreError):
    pass


class AssetCapacityExceeded(AssetStoreError):
    def __init__(self, status: Dict[str, Any]) -> None:
        self.status = status
        super().__init__(
            "asset storage is at the critical watermark "
            f"({status.get('used_percent', 0)}%)"
        )


@dataclass(frozen=True)
class AssetPayload:
    asset: StoredAsset
    body: bytes


def assets_root() -> Path:
    # ``reports/`` is mounted as a public static tree by the legacy app.  CAS
    # blobs must only be reachable through the authenticated asset endpoint.
    return project_path("asset_store").resolve()


def content_addressed_assets_enabled(session: Session) -> bool:
    return is_flag_enabled(session, CONTENT_ADDRESSED_ASSETS_FLAG, default=False)


def tiered_asset_retention_enabled(session: Session) -> bool:
    return is_flag_enabled(session, TIERED_ASSET_RETENTION_FLAG, default=False)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_suffix(value: str) -> str:
    cleaned = "".join(ch for ch in str(value or "bin").lower() if ch.isalnum())
    return cleaned or "bin"


def _storage_key(blob_sha256: str, suffix: str) -> str:
    return (
        Path("asset_store")
        / blob_sha256[:2]
        / blob_sha256[2:4]
        / f"{blob_sha256}.{_safe_suffix(suffix)}"
    ).as_posix()


def resolve_storage_key(storage_key: str) -> Path:
    raw = str(storage_key or "").strip().replace("\\", "/").lstrip("/")
    if not raw:
        raise AssetGone("asset storage key is empty")
    target = project_path(raw).resolve(strict=False)
    root = assets_root()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise AssetGone("asset storage key escapes the asset root") from exc
    current = root
    for part in target.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            raise AssetGone("asset storage key contains a symlink")
    return target


def _write_blob(storage_key: str, body: bytes, blob_sha256: str) -> Path:
    target = resolve_storage_key(storage_key)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file():
        if target.stat().st_size == len(body) and _sha256(target.read_bytes()) == blob_sha256:
            return target
        raise AssetStoreError(f"immutable asset collision: {storage_key}")

    temporary = target.with_name(f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        if _sha256(temporary.read_bytes()) != blob_sha256:
            raise AssetStoreError("asset blob hash mismatch before publish")
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return target


def _active_asset_by_logical(
    session: Session,
    *,
    logical_sha256: str,
    media_type: str,
    content_encoding: Optional[str],
) -> Optional[StoredAsset]:
    return session.exec(
        select(StoredAsset).where(
            StoredAsset.logical_sha256 == logical_sha256,
            StoredAsset.media_type == media_type,
            StoredAsset.content_encoding == content_encoding,
            StoredAsset.status == ASSET_STATUS_ACTIVE,
        )
    ).first()


def _store_encoded(
    session: Session,
    *,
    logical_sha256: str,
    body: bytes,
    media_type: str,
    encoding: str,
    content_encoding: Optional[str],
    suffix: str,
    width: Optional[int] = None,
    height: Optional[int] = None,
    original_width: Optional[int] = None,
    original_height: Optional[int] = None,
    scale: float = 1.0,
    commit: bool = True,
) -> StoredAsset:
    existing = _active_asset_by_logical(
        session,
        logical_sha256=logical_sha256,
        media_type=media_type,
        content_encoding=content_encoding,
    )
    if existing is not None:
        try:
            target = resolve_storage_key(existing.storage_key)
        except AssetGone:
            target = None
        if target is not None and target.is_file():
            existing_body = target.read_bytes()
            if _sha256(existing_body) == existing.blob_sha256:
                existing.encoding = encoding
                existing.scale = float(scale)
                existing.integrity_status = "VERIFIED"
                existing.last_verified_at = datetime.now()
                session.add(existing)
                if commit:
                    session.commit()
                    session.refresh(existing)
                return existing
            existing.status = ASSET_STATUS_MISSING
            existing.integrity_status = "FAILED"
            existing.updated_at = datetime.now()
            session.add(existing)
            session.flush()

    blob_sha256 = _sha256(body)
    storage_key = _storage_key(blob_sha256, suffix)
    _write_blob(storage_key, body, blob_sha256)

    by_blob = session.exec(
        select(StoredAsset).where(StoredAsset.blob_sha256 == blob_sha256)
    ).first()
    if by_blob is not None:
        verified_at = datetime.now()
        by_blob.status = ASSET_STATUS_ACTIVE
        by_blob.encoding = encoding
        by_blob.content_encoding = content_encoding
        by_blob.byte_size = len(body)
        by_blob.width = width
        by_blob.height = height
        by_blob.original_width = original_width
        by_blob.original_height = original_height
        by_blob.scale = float(scale)
        by_blob.integrity_status = "VERIFIED"
        by_blob.last_verified_at = verified_at
        by_blob.orphaned_at = verified_at
        by_blob.updated_at = verified_at
        session.add(by_blob)
        if commit:
            session.commit()
            session.refresh(by_blob)
        return by_blob

    created_at = datetime.now()
    row = StoredAsset(
        id=uuid.uuid4().hex,
        logical_sha256=logical_sha256,
        blob_sha256=blob_sha256,
        media_type=media_type,
        encoding=encoding,
        content_encoding=content_encoding,
        storage_key=storage_key,
        byte_size=len(body),
        width=width,
        height=height,
        original_width=original_width,
        original_height=original_height,
        scale=float(scale),
        status=ASSET_STATUS_ACTIVE,
        integrity_status="VERIFIED",
        last_verified_at=created_at,
        orphaned_at=created_at,
        created_at=created_at,
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        row = session.exec(
            select(StoredAsset).where(StoredAsset.blob_sha256 == blob_sha256)
        ).first()
        if row is None:
            raise
    if commit:
        session.commit()
        session.refresh(row)
    return row


def store_image_bytes(
    session: Session,
    image_bytes: bytes,
    *,
    original_size: Optional[Tuple[int, int]] = None,
    scale: float = 1.0,
    commit: bool = True,
) -> StoredAsset:
    """Store sanitized pixels at original resolution as lossless WebP."""
    with Image.open(io.BytesIO(image_bytes)) as source:
        image = source.convert("RGB")
        width, height = image.size
        logical = hashlib.sha256()
        logical.update(f"RGB:{width}x{height}\0".encode("ascii"))
        logical.update(image.tobytes())
        output = io.BytesIO()
        image.save(
            output,
            format="WEBP",
            lossless=True,
            quality=100,
            method=4,
            exact=True,
        )
        encoded = output.getvalue()
        with Image.open(io.BytesIO(encoded)) as decoded:
            restored = decoded.convert("RGB")
            if restored.size != image.size or restored.tobytes() != image.tobytes():
                raise AssetStoreError("lossless WebP pixel verification failed")
    original_width, original_height = original_size or (width, height)
    return _store_encoded(
        session,
        logical_sha256=logical.hexdigest(),
        body=encoded,
        media_type="image/webp",
        encoding="WEBP_LOSSLESS",
        content_encoding=None,
        suffix="webp",
        width=width,
        height=height,
        original_width=int(original_width),
        original_height=int(original_height),
        scale=float(scale),
        commit=commit,
    )


def derive_warm_image_bytes(image_bytes: bytes, *, scale: float = 0.75) -> bytes:
    if not 0.0 < float(scale) < 1.0:
        raise ValueError("warm image scale must be between 0 and 1")
    with Image.open(io.BytesIO(image_bytes)) as source:
        image = source.convert("RGB")
        target_size = (
            max(1, int(round(image.width * float(scale)))),
            max(1, int(round(image.height * float(scale)))),
        )
        resized = image.resize(target_size, Image.Resampling.LANCZOS)
        output = io.BytesIO()
        resized.save(
            output,
            format="WEBP",
            lossless=True,
            quality=100,
            method=4,
            exact=True,
        )
        encoded = output.getvalue()
        with Image.open(io.BytesIO(encoded)) as decoded:
            restored = decoded.convert("RGB")
            if restored.size != target_size or restored.tobytes() != resized.tobytes():
                raise AssetStoreError("warm derivative pixel verification failed")
        return encoded


def store_warm_image_derivative(
    session: Session,
    source_asset_id: str,
    *,
    scale: float = 0.75,
    commit: bool = True,
) -> StoredAsset:
    source = read_asset(session, source_asset_id, transparent=True)
    if not str(source.asset.media_type or "").startswith("image/"):
        raise ValueError("warm derivative source must be an image")
    derived = derive_warm_image_bytes(source.body, scale=scale)
    original_size = (
        int(source.asset.original_width or source.asset.width or 0),
        int(source.asset.original_height or source.asset.height or 0),
    )
    return store_image_bytes(
        session,
        derived,
        original_size=original_size,
        scale=scale,
        commit=commit,
    )


def store_text_bytes(
    session: Session,
    content: bytes,
    *,
    media_type: str,
    suffix: str,
    commit: bool = True,
) -> StoredAsset:
    logical_bytes = bytes(content or b"")
    compressed = gzip.compress(logical_bytes, compresslevel=9, mtime=0)
    try:
        restored = gzip.decompress(compressed)
    except OSError as exc:
        raise AssetStoreError("gzip verification failed") from exc
    if restored != logical_bytes:
        raise AssetStoreError("gzip round-trip verification failed")
    return _store_encoded(
        session,
        logical_sha256=_sha256(logical_bytes),
        body=compressed,
        media_type=media_type,
        encoding="UTF-8",
        content_encoding="gzip",
        suffix=f"{suffix}gz",
        commit=commit,
    )


def store_text(
    session: Session,
    content: str,
    *,
    media_type: str = "application/xml",
    suffix: str = "xml",
    commit: bool = True,
) -> StoredAsset:
    return store_text_bytes(
        session,
        str(content or "").encode("utf-8"),
        media_type=media_type,
        suffix=suffix,
        commit=commit,
    )


def store_json(
    session: Session,
    payload: Any,
    *,
    commit: bool = True,
) -> StoredAsset:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return store_text_bytes(
        session,
        canonical,
        media_type="application/json",
        suffix="json",
        commit=commit,
    )


def store_binary_bytes(
    session: Session,
    content: bytes,
    *,
    media_type: str = "application/octet-stream",
    suffix: str = "bin",
    commit: bool = True,
) -> StoredAsset:
    body = bytes(content or b"")
    return _store_encoded(
        session,
        logical_sha256=_sha256(body),
        body=body,
        media_type=media_type,
        encoding="BINARY",
        content_encoding=None,
        suffix=suffix,
        commit=commit,
    )


def store_file(session: Session, path: Path, *, commit: bool = True) -> StoredAsset:
    source = Path(path)
    suffix = source.suffix.lower()
    body = source.read_bytes()
    if suffix in {".png", ".jpg", ".jpeg", ".webp"}:
        return store_image_bytes(session, body, commit=commit)
    if suffix == ".xml":
        return store_text_bytes(
            session,
            body,
            media_type="application/xml",
            suffix="xml",
            commit=commit,
        )
    if suffix == ".json":
        try:
            return store_json(session, json.loads(body.decode("utf-8")), commit=commit)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return store_text_bytes(
                session,
                body,
                media_type="application/json",
                suffix="json",
                commit=commit,
            )
    media_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
    return store_binary_bytes(
        session,
        body,
        media_type=media_type,
        suffix=suffix.lstrip(".") or "bin",
        commit=commit,
    )


def get_asset(session: Session, asset_id: str) -> StoredAsset:
    row = session.get(StoredAsset, str(asset_id or ""))
    if row is None:
        raise AssetNotFound("asset not found")
    if row.status != ASSET_STATUS_ACTIVE:
        raise AssetGone(f"asset is {str(row.status or 'unavailable').lower()}")
    return row


def read_asset(session: Session, asset_id: str, *, transparent: bool = True) -> AssetPayload:
    row = get_asset(session, asset_id)
    try:
        target = resolve_storage_key(row.storage_key)
    except AssetGone:
        target = None
    if target is None or not target.is_file():
        row.status = ASSET_STATUS_MISSING
        row.integrity_status = "MISSING"
        row.updated_at = datetime.now()
        session.add(row)
        session.commit()
        raise AssetGone("asset blob is missing")
    body = target.read_bytes()
    if _sha256(body) != row.blob_sha256:
        row.status = ASSET_STATUS_MISSING
        row.integrity_status = "FAILED"
        row.updated_at = datetime.now()
        session.add(row)
        session.commit()
        raise AssetGone("asset blob failed integrity validation")
    if transparent and row.content_encoding == "gzip":
        try:
            body = gzip.decompress(body)
        except OSError as exc:
            row.status = ASSET_STATUS_MISSING
            row.integrity_status = "FAILED"
            row.updated_at = datetime.now()
            session.add(row)
            session.commit()
            raise AssetGone("asset compression payload is invalid") from exc
        if _sha256(body) != row.logical_sha256:
            row.status = ASSET_STATUS_MISSING
            row.integrity_status = "FAILED"
            row.updated_at = datetime.now()
            session.add(row)
            session.commit()
            raise AssetGone("asset logical content failed integrity validation")
    verified_at = datetime.now()
    if row.last_verified_at is None or verified_at - row.last_verified_at >= timedelta(days=1):
        row.integrity_status = "VERIFIED"
        row.last_verified_at = verified_at
        session.add(row)
        session.commit()
    return AssetPayload(asset=row, body=body)


def _normalize_retention_class(retention_class: str) -> str:
    normalized = str(retention_class or RETENTION_HOT).upper()
    return {
        "EVIDENCE": RETENTION_WARM,
        "PIN": RETENTION_PINNED,
    }.get(normalized, normalized)


def retention_expiry(retention_class: str, *, now: Optional[datetime] = None) -> Optional[datetime]:
    current = now or datetime.now()
    normalized = _normalize_retention_class(retention_class)
    if normalized in {RETENTION_PINNED, RETENTION_COLD}:
        return None
    if normalized == RETENTION_WARM:
        return current + timedelta(days=EVIDENCE_RETENTION_DAYS)
    return current + timedelta(days=HOT_RETENTION_DAYS)


def upsert_reference(
    session: Session,
    *,
    asset_id: str,
    owner_type: str,
    owner_id: int,
    role: str,
    retention_class: str = RETENTION_HOT,
    expires_at: Optional[datetime] = None,
    pinned_reason: Optional[str] = None,
    commit: bool = True,
) -> AssetReference:
    retention = _normalize_retention_class(retention_class)
    expiry = None if retention == RETENTION_PINNED else (
        expires_at or retention_expiry(retention)
    )
    row = session.exec(
        select(AssetReference).where(
            AssetReference.owner_type == str(owner_type),
            AssetReference.owner_id == int(owner_id),
            AssetReference.role == str(role),
        )
    ).first()
    previous_asset_id = row.asset_id if row is not None else None
    if row is None:
        row = AssetReference(
            asset_id=str(asset_id),
            owner_type=str(owner_type),
            owner_id=int(owner_id),
            role=str(role),
            retention_class=retention,
            expires_at=expiry,
            pinned_reason=pinned_reason,
            created_at=datetime.now(),
        )
    else:
        row.asset_id = str(asset_id)
        row.retention_class = retention
        row.expires_at = expiry
        row.pinned_reason = pinned_reason
        row.released_at = None
        row.grace_until = None
    session.add(row)
    asset = session.get(StoredAsset, str(asset_id))
    if asset is not None and asset.status == ASSET_STATUS_ACTIVE:
        asset.orphaned_at = None
        session.add(asset)
    session.flush()
    if previous_asset_id and previous_asset_id != str(asset_id):
        _mark_assets_orphaned_if_unreferenced(
            session,
            {previous_asset_id},
            now=datetime.now(),
        )
    if commit:
        session.commit()
        session.refresh(row)
    else:
        session.flush()
    return row


def release_owner_references(
    session: Session,
    *,
    owner_type: str,
    owner_id: int,
    commit: bool = True,
) -> int:
    rows = session.exec(
        select(AssetReference).where(
            AssetReference.owner_type == str(owner_type),
            AssetReference.owner_id == int(owner_id),
            AssetReference.released_at == None,  # noqa: E711
        )
    ).all()
    _release_references(
        session,
        rows,
        now=datetime.now(),
    )
    if commit:
        session.commit()
    return len(rows)


def _inferred_owner(path: Path) -> Optional[Tuple[str, int, str]]:
    try:
        relative = path.resolve().relative_to(project_path("reports").resolve())
    except ValueError:
        return None
    parts = relative.parts
    if len(parts) >= 5 and parts[0] == "inspection":
        try:
            state_id = int(parts[3])
        except (TypeError, ValueError):
            return None
        role_by_name = {
            "screenshot.png": "screenshot",
            "hierarchy.xml": "xml",
            "thumbnail.jpg": "thumbnail",
            "actions.json": "action_map",
        }
        role = role_by_name.get(parts[-1])
        if role:
            return "inspection_state", state_id, role
    if len(parts) >= 6 and parts[0] == "compatibility":
        try:
            cell_id = int(parts[2])
        except (TypeError, ValueError):
            return None
        role = ":".join(parts[3:])
        return "compatibility_cell", cell_id, role
    return None


def _mirror_with_internal_session(path: Path, kind: str, payload: Any) -> Optional[str]:
    try:
        with Session(engine) as session:
            if not content_addressed_assets_enabled(session):
                return None
            if kind == "image":
                asset = store_image_bytes(session, bytes(payload), commit=False)
            elif kind == "xml":
                asset = store_text(
                    session,
                    str(payload or ""),
                    media_type="application/xml",
                    suffix="xml",
                    commit=False,
                )
            elif kind == "json":
                asset = store_json(session, payload, commit=False)
            else:
                raise ValueError(f"unsupported mirrored asset kind: {kind}")
            owner = _inferred_owner(Path(path))
            if owner is not None:
                owner_type, owner_id, role = owner
                upsert_reference(
                    session,
                    asset_id=asset.id,
                    owner_type=owner_type,
                    owner_id=owner_id,
                    role=role,
                    retention_class=RETENTION_HOT,
                    commit=False,
                )
            session.commit()
            return asset.id
    except Exception:
        logger.exception("legacy asset CAS mirror failed: path=%s kind=%s", path, kind)
        return None


def mirror_image(path: Path, image_bytes: bytes) -> Optional[str]:
    return _mirror_with_internal_session(path, "image", image_bytes)


def mirror_xml(path: Path, xml: str) -> Optional[str]:
    return _mirror_with_internal_session(path, "xml", xml)


def mirror_final_json(path: Path, payload: Dict[str, Any]) -> Optional[str]:
    actions = list(payload.get("actions") or []) if isinstance(payload, dict) else []
    if any(str(item.get("status") or "") in {"PENDING", "ACTIVE", "INVOKED"} for item in actions):
        return None
    return _mirror_with_internal_session(path, "json", payload)


_OWNER_MODELS = {
    "inspection_run": InspectionRun,
    "inspection_state": InspectionState,
    "inspection_regression": InspectionState,
    "inspection_observation": InspectionObservation,
    "inspection_fault": InspectionFault,
    "compatibility_run": CompatibilityRun,
    "compatibility_cell": CompatibilityCell,
    "compatibility_page_result": CompatibilityPageResult,
}


def remove_stale_references(
    session: Session,
    *,
    now: Optional[datetime] = None,
) -> int:
    removed = 0
    stale_rows = []
    for row in session.exec(select(AssetReference)).all():
        if row.released_at is not None:
            continue
        model = _OWNER_MODELS.get(str(row.owner_type))
        if model is None:
            continue
        if session.get(model, row.owner_id) is None:
            stale_rows.append(row)
            removed += 1
    _release_references(
        session,
        stale_rows,
        now=now or datetime.now(),
    )
    return removed


def _release_references(
    session: Session,
    rows: Iterable[AssetReference],
    *,
    now: datetime,
    grace_hours: int = UNREFERENCED_GRACE_HOURS,
) -> None:
    released_asset_ids = set()
    grace_until = now + timedelta(hours=max(0, int(grace_hours)))
    for row in rows:
        if row.released_at is not None:
            continue
        row.retention_class = RETENTION_COLD
        row.released_at = now
        row.grace_until = grace_until
        session.add(row)
        released_asset_ids.add(row.asset_id)
    session.flush()
    _mark_assets_orphaned_if_unreferenced(
        session,
        released_asset_ids,
        now=now,
    )


def _mark_assets_orphaned_if_unreferenced(
    session: Session,
    asset_ids: Iterable[str],
    *,
    now: datetime,
) -> None:
    for asset_id in set(asset_ids):
        if _has_references(session, asset_id):
            continue
        asset = session.get(StoredAsset, asset_id)
        if asset is None or asset.status != ASSET_STATUS_ACTIVE:
            continue
        asset.orphaned_at = now
        session.add(asset)


def _delete_asset_blob(session: Session, row: StoredAsset) -> int:
    deleted_bytes = 0
    try:
        target = resolve_storage_key(row.storage_key)
        if target.is_file():
            deleted_bytes = target.stat().st_size
            target.unlink()
    except (AssetGone, OSError):
        logger.warning("unable to delete asset blob: %s", row.storage_key, exc_info=True)
    row.status = ASSET_STATUS_DELETED
    row.updated_at = datetime.now()
    session.add(row)
    return deleted_bytes


def _has_references(session: Session, asset_id: str) -> bool:
    return session.exec(
        select(AssetReference.id)
        .where(
            AssetReference.asset_id == asset_id,
            AssetReference.released_at == None,  # noqa: E711
        )
        .limit(1)
    ).first() is not None


def _grace_until_for_asset(
    session: Session,
    asset_id: str,
) -> Optional[datetime]:
    rows = session.exec(
        select(AssetReference).where(
            AssetReference.asset_id == asset_id,
            AssetReference.released_at != None,  # noqa: E711
        )
    ).all()
    values = [row.grace_until for row in rows if row.grace_until is not None]
    return max(values) if values else None


def _legacy_report_file(path_value: Optional[str], *, prefix: str) -> Optional[Path]:
    text = str(path_value or "").strip().replace("\\", "/").lstrip("/")
    if not text or not text.startswith(f"{prefix}/"):
        return None
    root = project_path("reports").resolve()
    target = (root / text).resolve(strict=False)
    try:
        relative = target.relative_to(root)
    except ValueError:
        return None
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return None
    return target


def _has_verified_legacy_reference(
    session: Session,
    *,
    owner_candidates: Iterable[Tuple[str, int]],
    role: str,
    cutoff: datetime,
    expected_asset_id: Optional[str] = None,
) -> bool:
    for owner_type, owner_id in owner_candidates:
        rows = session.exec(
            select(AssetReference).where(
                AssetReference.owner_type == owner_type,
                AssetReference.owner_id == owner_id,
                AssetReference.role == role,
                AssetReference.released_at == None,  # noqa: E711
                AssetReference.created_at <= cutoff,
            )
        ).all()
        for reference in rows:
            if expected_asset_id and reference.asset_id != expected_asset_id:
                continue
            asset = session.get(StoredAsset, reference.asset_id)
            if (
                asset is None
                or asset.status != ASSET_STATUS_ACTIVE
                or asset.integrity_status != "VERIFIED"
                or asset.last_verified_at is None
            ):
                continue
            try:
                blob = resolve_storage_key(asset.storage_key)
            except AssetGone:
                continue
            if blob.is_file() and blob.stat().st_size == int(asset.byte_size or 0):
                return True
    return False


def cleanup_verified_legacy_files(
    session: Session,
    *,
    now: Optional[datetime] = None,
    rollback_days: int = LEGACY_ROLLBACK_DAYS,
) -> Dict[str, int]:
    """Remove dual-written files only after a verified CAS rollback window."""
    current = now or datetime.now()
    cutoff = current - timedelta(days=max(1, int(rollback_days)))
    candidates: list[
        Tuple[Optional[Path], Tuple[Tuple[str, int], ...], str, Optional[str]]
    ] = []

    for state in session.exec(select(InspectionState)).all():
        representative = (
            session.get(InspectionObservation, state.representative_observation_id)
            if state.representative_observation_id is not None
            else None
        )
        owners = (("inspection_state", int(state.id)),)
        if representative is not None:
            owners = (("inspection_observation", int(representative.id)),) + owners
        candidates.append(
            (
                _legacy_report_file(state.screenshot_path, prefix="inspection"),
                owners,
                "screenshot",
                representative.screenshot_asset_id if representative else None,
            )
        )
        candidates.extend(
            [
                (
                    _legacy_report_file(state.xml_path, prefix="inspection"),
                    owners,
                    "xml",
                    representative.xml_asset_id if representative else None,
                ),
                (
                    _legacy_report_file(state.thumbnail_path, prefix="inspection"),
                    owners,
                    "thumbnail",
                    representative.thumbnail_asset_id if representative else None,
                ),
            ]
        )
        if state.screenshot_path:
            action_path = str(Path(state.screenshot_path).with_name("actions.json"))
            candidates.append(
                (
                    _legacy_report_file(action_path, prefix="inspection"),
                    owners,
                    "action_map",
                    representative.action_map_asset_id if representative else None,
                )
            )

    for row in session.exec(select(CompatibilityPageResult)).all():
        owner = (("compatibility_page_result", int(row.id)),)
        for role, path_value, asset_id in (
            ("baseline_screenshot", row.baseline_screenshot_path, row.baseline_screenshot_asset_id),
            ("candidate_screenshot", row.candidate_screenshot_path, row.candidate_screenshot_asset_id),
            ("diff_screenshot", row.diff_screenshot_path, row.diff_screenshot_asset_id),
            ("baseline_xml", row.baseline_xml_path, row.baseline_xml_asset_id),
            ("candidate_xml", row.candidate_xml_path, row.candidate_xml_asset_id),
        ):
            candidates.append(
                (
                    _legacy_report_file(path_value, prefix="compatibility"),
                    owner,
                    role,
                    asset_id,
                )
            )

    for fault in session.exec(select(InspectionFault)).all():
        owner = (("inspection_fault", int(fault.id)),)
        for role, path_value in (
            ("full_log", fault.full_log_path),
            ("screenshot", fault.screenshot_path),
            ("xml", fault.xml_path),
            ("replay", fault.replay_path),
            ("trace", fault.trace_path),
        ):
            candidates.append(
                (
                    _legacy_report_file(path_value, prefix="inspection"),
                    owner,
                    role,
                    None,
                )
            )

    summary = {"eligible": 0, "deleted": 0, "missing": 0, "failed": 0}
    seen: set[Path] = set()
    reports_root = project_path("reports").resolve()
    for target, owners, role, expected_asset_id in candidates:
        if target is None or target in seen:
            continue
        if not _has_verified_legacy_reference(
            session,
            owner_candidates=owners,
            role=role,
            cutoff=cutoff,
            expected_asset_id=expected_asset_id,
        ):
            continue
        seen.add(target)
        summary["eligible"] += 1
        if not target.is_file():
            summary["missing"] += 1
            continue
        try:
            target.unlink()
            summary["deleted"] += 1
            parent = target.parent
            while parent != reports_root:
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent
        except OSError:
            logger.exception("failed to remove verified legacy asset: %s", target)
            summary["failed"] += 1
    return summary


def materialize_warm_derivatives(
    session: Session,
    *,
    now: Optional[datetime] = None,
    scale: float = 0.75,
) -> Dict[str, int]:
    """Downsample ordinary 7-day-old inspection captures into the WARM tier."""
    current = now or datetime.now()
    cutoff = current - timedelta(days=HOT_RETENTION_DAYS)
    candidates = session.exec(
        select(InspectionObservation).where(
            InspectionObservation.retention_class == RETENTION_HOT,
            InspectionObservation.captured_at <= cutoff,
        )
    ).all()
    state_ids = {item.state_id for item in candidates}
    fault_state_ids = {
        item.state_id
        for item in session.exec(
            select(InspectionFault).where(col(InspectionFault.state_id).in_(state_ids))
        ).all()
        if item.state_id is not None
    } if state_ids else set()
    cycle_state_ids = set()
    if state_ids:
        transitions = session.exec(
            select(InspectionTransition).where(
                or_(
                    col(InspectionTransition.from_state_id).in_(state_ids),
                    col(InspectionTransition.to_state_id).in_(state_ids),
                )
            )
        ).all()
        for transition in transitions:
            topology = str(transition.topology_type or transition.relation_type or "").upper()
            if transition.from_state_id == transition.to_state_id or topology in {
                "CYCLE",
                "CYCLE_BACK",
                "LOOP",
                "SELF_LOOP",
            }:
                cycle_state_ids.add(transition.from_state_id)
                if transition.to_state_id is not None:
                    cycle_state_ids.add(transition.to_state_id)

    pinned_references = session.exec(
        select(AssetReference).where(
            AssetReference.retention_class == RETENTION_PINNED,
            AssetReference.released_at == None,  # noqa: E711
        )
    ).all()
    pinned_assets = {item.asset_id for item in pinned_references}
    pinned_observations = {
        item.owner_id
        for item in pinned_references
        if item.owner_type == "inspection_observation"
    }
    summary = {
        "eligible": len(candidates),
        "derived": 0,
        "full_resolution": 0,
        "protected": 0,
        "non_representative": 0,
        "missing": 0,
        "failed": 0,
    }
    for observation in candidates:
        state = session.get(InspectionState, observation.state_id)
        evidence_text = json.dumps(
            observation.match_evidence or {},
            ensure_ascii=True,
            sort_keys=True,
            default=str,
        ).lower()
        protected = bool(
            observation.id in pinned_observations
            or observation.screenshot_asset_id in pinned_assets
            or state is None
            or state.selected_for_regression
        )
        if protected:
            summary["protected"] += 1
            continue
        if not observation.is_representative:
            summary["non_representative"] += 1
            continue
        preserve_full_resolution = bool(
            state.is_opaque
            or str(state.stable_status or "").upper() != "STABLE"
            or observation.state_id in fault_state_ids
            or observation.state_id in cycle_state_ids
            or any(token in evidence_text for token in ("anomaly", "fault", "loop", "cycle"))
        )
        screenshot = (
            session.get(StoredAsset, observation.screenshot_asset_id)
            if observation.screenshot_asset_id
            else None
        )
        if screenshot is None or screenshot.status != ASSET_STATUS_ACTIVE:
            summary["missing"] += 1
            continue
        try:
            if preserve_full_resolution or float(screenshot.scale or 1.0) <= float(scale):
                derived = screenshot
            else:
                derived = store_warm_image_derivative(
                    session,
                    screenshot.id,
                    scale=scale,
                    commit=False,
                )
            observation.screenshot_asset_id = derived.id
            observation.original_width = derived.original_width
            observation.original_height = derived.original_height
            observation.retention_class = RETENTION_WARM
            observation.retained_until = observation.captured_at + timedelta(
                days=EVIDENCE_RETENTION_DAYS
            )
            session.add(observation)
            session.flush()
            for role, asset_id, retention_class in (
                ("screenshot", observation.screenshot_asset_id, RETENTION_WARM),
                ("xml", observation.xml_asset_id, RETENTION_WARM),
                ("thumbnail", observation.thumbnail_asset_id, RETENTION_COLD),
                ("action_map", observation.action_map_asset_id, RETENTION_WARM),
            ):
                if not asset_id:
                    continue
                upsert_reference(
                    session,
                    asset_id=asset_id,
                    owner_type="inspection_observation",
                    owner_id=int(observation.id),
                    role=role,
                    retention_class=retention_class,
                    expires_at=(
                        observation.retained_until
                        if retention_class == RETENTION_WARM
                        else None
                    ),
                    commit=False,
                )
            session.commit()
            summary["derived"] += 1
            if preserve_full_resolution:
                summary["full_resolution"] += 1
        except Exception:
            session.rollback()
            logger.exception(
                "failed to materialize WARM derivative: observation=%s",
                observation.id,
            )
            summary["failed"] += 1
    return summary


def transition_warm_observations_to_cold(
    session: Session,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, int]:
    """Release 90-day WARM evidence while retaining thumbnail metadata."""
    current = now or datetime.now()
    candidates = session.exec(
        select(InspectionObservation).where(
            InspectionObservation.retention_class == RETENTION_WARM,
            InspectionObservation.retained_until != None,  # noqa: E711
            InspectionObservation.retained_until <= current,
        )
    ).all()
    transitioned = 0
    failed = 0
    for observation in candidates:
        try:
            warm_rows = session.exec(
                select(AssetReference).where(
                    AssetReference.owner_type == "inspection_observation",
                    AssetReference.owner_id == observation.id,
                    col(AssetReference.role).in_(["screenshot", "xml", "action_map"]),
                    AssetReference.released_at == None,  # noqa: E711
                )
            ).all()
            _release_references(session, warm_rows, now=current)
            if observation.thumbnail_asset_id:
                upsert_reference(
                    session,
                    asset_id=observation.thumbnail_asset_id,
                    owner_type="inspection_observation",
                    owner_id=int(observation.id),
                    role="thumbnail",
                    retention_class=RETENTION_COLD,
                    commit=False,
                )
            observation.screenshot_asset_id = None
            observation.xml_asset_id = None
            observation.action_map_asset_id = None
            observation.retention_class = RETENTION_COLD
            observation.retained_until = None
            observation.asset_status = (
                "THUMBNAIL_ONLY" if observation.thumbnail_asset_id else "METADATA_ONLY"
            )
            observation.metadata_only = True
            session.add(observation)
            session.commit()
            transitioned += 1
        except Exception:
            session.rollback()
            logger.exception(
                "failed to transition WARM observation to COLD: observation=%s",
                observation.id,
            )
            failed += 1
    return {"eligible": len(candidates), "transitioned": transitioned, "failed": failed}


def gc_assets(
    session: Session,
    *,
    now: Optional[datetime] = None,
    unreferenced_grace_hours: int = UNREFERENCED_GRACE_HOURS,
    high_watermark_percent: float = DEFAULT_HIGH_WATERMARK_PERCENT,
    low_watermark_percent: float = DEFAULT_LOW_WATERMARK_PERCENT,
    critical_watermark_percent: float = DEFAULT_CRITICAL_WATERMARK_PERCENT,
) -> Dict[str, Any]:
    """Expire references and reclaim unreferenced blobs.

    The 24-hour orphan grace is always measured from the final reference
    release.  Watermark pressure changes scheduling/telemetry, but never breaks
    a live reference or bypasses that recovery window.
    """
    current = now or datetime.now()
    removed_stale = remove_stale_references(session, now=current)
    expired = session.exec(
        select(AssetReference).where(
            col(AssetReference.retention_class).notin_([RETENTION_PINNED, "PIN"]),
            AssetReference.released_at == None,  # noqa: E711
            AssetReference.expires_at != None,  # noqa: E711
            AssetReference.expires_at <= current,
        )
    ).all()
    _release_references(
        session,
        expired,
        now=current,
        grace_hours=unreferenced_grace_hours,
    )

    root = assets_root()
    root.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(root)
    used_percent = (usage.used / usage.total * 100.0) if usage.total else 0.0
    pressure = used_percent >= float(high_watermark_percent)
    critical = used_percent >= float(critical_watermark_percent)
    cutoff = current - timedelta(hours=max(0, int(unreferenced_grace_hours)))
    candidates = session.exec(
        select(StoredAsset).where(StoredAsset.status == ASSET_STATUS_ACTIVE)
        .order_by(col(StoredAsset.created_at).asc())
    ).all()
    deleted_assets = 0
    deleted_bytes = 0
    for asset in candidates:
        if _has_references(session, asset.id):
            continue
        grace_until = _grace_until_for_asset(session, asset.id)
        if grace_until is not None and current < grace_until:
            continue
        orphaned_since = asset.orphaned_at or asset.created_at
        if orphaned_since > cutoff:
            continue
        deleted_bytes += _delete_asset_blob(session, asset)
        deleted_assets += 1
        if pressure:
            usage = shutil.disk_usage(root)
            current_percent = (usage.used / usage.total * 100.0) if usage.total else 0.0
            if current_percent <= float(low_watermark_percent):
                pressure = False
    session.commit()
    return {
        "stale_references": removed_stale,
        "expired_references": len(expired),
        "deleted_assets": deleted_assets,
        "deleted_bytes": deleted_bytes,
        "disk_used_percent": round(used_percent, 3),
        "watermark_low_percent": float(low_watermark_percent),
        "watermark_high_percent": float(high_watermark_percent),
        "watermark_critical_percent": float(critical_watermark_percent),
        "critical_pressure": critical,
        "pressure_remaining": pressure,
    }


def store_usage_bytes(session: Session) -> int:
    rows = session.exec(
        select(StoredAsset).where(StoredAsset.status == ASSET_STATUS_ACTIVE)
    ).all()
    return sum(max(0, int(item.byte_size or 0)) for item in rows)


def asset_storage_status(
    session: Session,
    *,
    now: Optional[datetime] = None,
    low_watermark_percent: float = DEFAULT_LOW_WATERMARK_PERCENT,
    high_watermark_percent: float = DEFAULT_HIGH_WATERMARK_PERCENT,
    critical_watermark_percent: float = DEFAULT_CRITICAL_WATERMARK_PERCENT,
) -> Dict[str, Any]:
    current = now or datetime.now()
    root = assets_root()
    root.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(root)
    used_percent = (usage.used / usage.total * 100.0) if usage.total else 0.0

    assets = session.exec(
        select(StoredAsset).where(StoredAsset.status == ASSET_STATUS_ACTIVE)
    ).all()
    active_references = session.exec(
        select(AssetReference).where(
            AssetReference.released_at == None,  # noqa: E711
        )
    ).all()
    references_by_asset: Dict[str, list[AssetReference]] = {}
    for reference in active_references:
        references_by_asset.setdefault(reference.asset_id, []).append(reference)
    released_references = session.exec(
        select(AssetReference).where(
            AssetReference.released_at != None,  # noqa: E711
            AssetReference.grace_until != None,  # noqa: E711
        )
    ).all()
    grace_by_asset: Dict[str, datetime] = {}
    for reference in released_references:
        if reference.grace_until is None:
            continue
        previous = grace_by_asset.get(reference.asset_id)
        if previous is None or reference.grace_until > previous:
            grace_by_asset[reference.asset_id] = reference.grace_until

    tier_bytes = {
        RETENTION_HOT: 0,
        RETENTION_WARM: 0,
        RETENTION_PINNED: 0,
        RETENTION_COLD: 0,
    }
    reclaimable_bytes = 0
    for asset in assets:
        byte_size = max(0, int(asset.byte_size or 0))
        references = references_by_asset.get(asset.id, [])
        classes = {
            _normalize_retention_class(item.retention_class)
            for item in references
        }
        if RETENTION_PINNED in classes:
            tier = RETENTION_PINNED
        elif RETENTION_WARM in classes:
            tier = RETENTION_WARM
        elif RETENTION_HOT in classes:
            tier = RETENTION_HOT
        else:
            tier = RETENTION_COLD
        tier_bytes[tier] += byte_size

        if references:
            continue
        grace_until = grace_by_asset.get(asset.id)
        if grace_until is None:
            orphaned_since = asset.orphaned_at or asset.created_at
            grace_until = orphaned_since + timedelta(hours=UNREFERENCED_GRACE_HOURS)
        if current >= grace_until:
            reclaimable_bytes += byte_size

    if used_percent >= float(critical_watermark_percent):
        pressure_level = "CRITICAL"
    elif used_percent >= float(high_watermark_percent):
        pressure_level = "HIGH"
    elif used_percent >= float(low_watermark_percent):
        pressure_level = "WATCH"
    else:
        pressure_level = "NORMAL"

    return {
        "used_percent": round(used_percent, 3),
        "used_bytes": int(usage.used),
        "free_bytes": int(usage.free),
        "total_bytes": int(usage.total),
        "stored_bytes": sum(max(0, int(item.byte_size or 0)) for item in assets),
        "pinned_bytes": tier_bytes[RETENTION_PINNED],
        "pinned_reference_count": sum(
            1
            for item in active_references
            if _normalize_retention_class(item.retention_class) == RETENTION_PINNED
        ),
        "reclaimable_bytes": reclaimable_bytes,
        "hot_7d_bytes": tier_bytes[RETENTION_HOT],
        "warm_90d_bytes": tier_bytes[RETENTION_WARM],
        "cold_bytes": tier_bytes[RETENTION_COLD],
        "active_asset_count": len(assets),
        "low_watermark_percent": float(low_watermark_percent),
        "high_watermark_percent": float(high_watermark_percent),
        "critical_watermark_percent": float(critical_watermark_percent),
        "pressure_level": pressure_level,
        "can_start": used_percent < float(critical_watermark_percent),
    }


def ensure_asset_capacity_for_new_run(
    session: Session,
    *,
    critical_watermark_percent: float = DEFAULT_CRITICAL_WATERMARK_PERCENT,
) -> Dict[str, Any]:
    status = asset_storage_status(
        session,
        critical_watermark_percent=critical_watermark_percent,
    )
    if not status["can_start"]:
        raise AssetCapacityExceeded(status)
    return status
