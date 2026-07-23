"""Authenticated delivery for immutable content-addressed report assets."""
from __future__ import annotations

import hashlib
import re
from typing import Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from sqlmodel import Session

from backend.api import deps
from backend.artifact_store import (
    DEFAULT_CRITICAL_WATERMARK_PERCENT,
    DEFAULT_HIGH_WATERMARK_PERCENT,
    DEFAULT_LOW_WATERMARK_PERCENT,
    AssetGone,
    AssetNotFound,
    asset_storage_status,
    read_asset,
)
from backend.database import get_session
from backend.models import User
from backend.retention_service import (
    ASSET_CRITICAL_WATERMARK_KEY,
    ASSET_HIGH_WATERMARK_KEY,
    ASSET_LOW_WATERMARK_KEY,
    _watermark_percent,
)

router = APIRouter()

_SINGLE_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def _parse_range(value: str, total: int) -> Optional[Tuple[int, int]]:
    text = str(value or "").strip()
    if not text:
        return None
    match = _SINGLE_RANGE_RE.fullmatch(text)
    if match is None or total <= 0:
        raise ValueError("invalid range")
    start_text, end_text = match.groups()
    if not start_text and not end_text:
        raise ValueError("invalid range")
    if not start_text:
        suffix = int(end_text)
        if suffix <= 0:
            raise ValueError("invalid suffix range")
        start = max(0, total - suffix)
        return start, total - 1
    start = int(start_text)
    if start >= total:
        raise ValueError("range starts past end")
    end = int(end_text) if end_text else total - 1
    if end < start:
        raise ValueError("range end precedes start")
    return start, min(end, total - 1)


def _etag_matches(if_none_match: str, etag: str) -> bool:
    candidates = [item.strip() for item in str(if_none_match or "").split(",")]
    return "*" in candidates or etag in candidates or f"W/{etag}" in candidates


@router.get("/status")
def get_asset_storage_status(
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_active_user),
):
    del current_user
    return asset_storage_status(
        session,
        low_watermark_percent=_watermark_percent(
            session,
            ASSET_LOW_WATERMARK_KEY,
            DEFAULT_LOW_WATERMARK_PERCENT,
        ),
        high_watermark_percent=_watermark_percent(
            session,
            ASSET_HIGH_WATERMARK_KEY,
            DEFAULT_HIGH_WATERMARK_PERCENT,
        ),
        critical_watermark_percent=_watermark_percent(
            session,
            ASSET_CRITICAL_WATERMARK_KEY,
            DEFAULT_CRITICAL_WATERMARK_PERCENT,
        ),
    )


@router.get("/{asset_id}")
def get_asset_content(
    asset_id: str,
    request: Request,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_active_user),
):
    del current_user
    try:
        payload = read_asset(session, asset_id, transparent=True)
    except AssetNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AssetGone as exc:
        raise HTTPException(status_code=410, detail=str(exc)) from exc

    body = payload.body
    representation_sha = hashlib.sha256(body).hexdigest()
    etag = f'"{representation_sha}"'
    common_headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, max-age=31536000, immutable",
        "ETag": etag,
        "X-Content-Type-Options": "nosniff",
    }
    if payload.asset.content_encoding:
        common_headers["X-Asset-Stored-Encoding"] = payload.asset.content_encoding

    if _etag_matches(request.headers.get("if-none-match", ""), etag):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=common_headers)

    range_header = request.headers.get("range", "")
    try:
        selected = _parse_range(range_header, len(body))
    except (TypeError, ValueError):
        headers = dict(common_headers)
        headers["Content-Range"] = f"bytes */{len(body)}"
        raise HTTPException(status_code=416, detail="Requested range not satisfiable", headers=headers)

    response_status = status.HTTP_200_OK
    response_body = body
    headers = dict(common_headers)
    if selected is not None:
        start, end = selected
        response_body = body[start : end + 1]
        response_status = status.HTTP_206_PARTIAL_CONTENT
        headers["Content-Range"] = f"bytes {start}-{end}/{len(body)}"
    headers["Content-Length"] = str(len(response_body))
    return Response(
        content=response_body,
        status_code=response_status,
        media_type=payload.asset.media_type,
        headers=headers,
    )
