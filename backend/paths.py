"""Project-local path helpers."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable, Optional, Union

PathLike = Union[str, os.PathLike]

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def project_path(*parts: PathLike) -> Path:
    return PROJECT_ROOT.joinpath(*(Path(part) for part in parts))


def _normalized_text(path: PathLike) -> str:
    return os.fspath(path).strip().replace("\\", "/")


def _is_absolute_text(path_text: str) -> bool:
    return path_text.startswith("/") or bool(re.match(r"^[A-Za-z]:/", path_text))


def _anchor_relative_path(path_text: str, anchors: Iterable[str]) -> Optional[Path]:
    normalized = path_text.lstrip("/")
    for anchor in anchors:
        clean_anchor = str(anchor).strip("/").replace("\\", "/")
        if not clean_anchor:
            continue
        if normalized == clean_anchor or normalized.startswith(f"{clean_anchor}/"):
            return Path(normalized)

        marker = f"/{clean_anchor}/"
        idx = path_text.find(marker)
        if idx >= 0:
            return Path(path_text[idx + 1 :])
    return None


def project_relative_path(path: PathLike, *, anchors: Iterable[str] = ()) -> str:
    """Return a portable path relative to PROJECT_ROOT when possible."""
    path_text = _normalized_text(path)
    anchored = _anchor_relative_path(path_text, anchors)
    if anchored is not None:
        return anchored.as_posix()

    current = Path(path_text).expanduser()
    try:
        return current.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    except Exception:
        return path_text


def resolve_project_path(path: PathLike, *, anchors: Iterable[str] = ()) -> Path:
    """Resolve stored relative paths and legacy absolute paths under PROJECT_ROOT."""
    path_text = _normalized_text(path)
    anchored = _anchor_relative_path(path_text, anchors)
    if anchored is not None:
        return PROJECT_ROOT / anchored

    expanded = Path(path_text).expanduser()
    if _is_absolute_text(path_text):
        return expanded
    return PROJECT_ROOT / expanded
