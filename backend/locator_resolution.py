"""Resolve cross-platform locator candidates from standard step payloads."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from backend.step_contract import SELECTOR_TYPE_TO_BY


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalize_override(candidate: Any) -> Optional[Dict[str, str]]:
    if not isinstance(candidate, dict):
        return None
    selector = _clean_text(candidate.get("selector"))
    by = _clean_text(candidate.get("by")).lower()
    if not selector or not by:
        return None
    return {"selector": selector, "by": by}


def _fallback_android_override(step_item: Dict[str, Any]) -> Optional[Dict[str, str]]:
    selector = _clean_text(step_item.get("selector"))
    selector_type = _clean_text(step_item.get("selector_type"))
    by = SELECTOR_TYPE_TO_BY.get(selector_type) or SELECTOR_TYPE_TO_BY.get(selector_type.lower())
    if selector and by:
        return {"selector": selector, "by": str(by).lower()}
    return None


def _infer_ios_candidates_from_android(android_override: Optional[Dict[str, str]]) -> List[Dict[str, str]]:
    if not android_override:
        return []

    selector = _clean_text(android_override.get("selector"))
    android_by = _clean_text(android_override.get("by")).lower()
    if not selector or not android_by:
        return []

    if android_by == "text":
        return [
            {"selector": selector, "by": "label"},
            {"selector": selector, "by": "name"},
        ]
    if android_by in ("description", "desc", "content-desc"):
        return [
            {"selector": selector, "by": "name"},
            {"selector": selector, "by": "label"},
        ]
    if android_by == "label":
        return [
            {"selector": selector, "by": "label"},
            {"selector": selector, "by": "name"},
        ]
    if android_by == "name":
        return [
            {"selector": selector, "by": "name"},
            {"selector": selector, "by": "label"},
        ]

    # 当前阶段不自动从 id/xpath 推导 iOS 定位，避免误匹配。
    return []


def _looks_like_verbose_text_locator(selector: str) -> bool:
    text = _clean_text(selector)
    if len(text) < 24:
        return False
    if any(ch.isspace() for ch in text):
        return True
    for token in ("，", ",", "|", "/", "¥", "￥"):
        if token in text:
            return True
    return False


def _dedupe_candidates(candidates: List[Dict[str, str]]) -> List[Dict[str, str]]:
    unique: List[Dict[str, str]] = []
    seen = set()
    for item in candidates:
        selector = _clean_text(item.get("selector"))
        by = _clean_text(item.get("by")).lower()
        if not selector or not by:
            continue
        key = (selector, by)
        if key in seen:
            continue
        seen.add(key)
        unique.append({"selector": selector, "by": by})
    return unique


def resolve_locator_candidates(step_item: Dict[str, Any], platform: str) -> List[Dict[str, str]]:
    """
    Resolve locator candidates for one step on target platform.

    Priority:
    1) explicit platform override
    2) Android override fallback from legacy selector fields
    3) iOS inferred candidates from Android text/desc mapping
    """
    platform_lower = _clean_text(platform).lower()
    overrides = step_item.get("platform_overrides") or {}
    if not isinstance(overrides, dict):
        overrides = {}

    direct = _normalize_override(overrides.get(platform_lower))
    android = _normalize_override(overrides.get("android")) or _fallback_android_override(step_item)

    if direct:
        if platform_lower == "ios":
            direct_by = _clean_text(direct.get("by")).lower()
            inferred = _infer_ios_candidates_from_android(android)
            if (
                direct_by in ("id", "accessibilityid", "accessibility_id", "description", "desc")
                and _looks_like_verbose_text_locator(direct.get("selector", ""))
                and inferred
            ):
                # iOS 某些页面会把整段可读文本塞进 id，优先尝试 Android 语义映射的短定位。
                return _dedupe_candidates(inferred + [direct])
        return [direct]

    if platform_lower == "android":
        return [android] if android else []
    if platform_lower == "ios":
        return _infer_ios_candidates_from_android(android)
    return []
