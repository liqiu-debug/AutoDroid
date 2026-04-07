"""Helpers for bootstrapping iOS-ready standard steps from Android-centric data."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from backend.cross_platform_execution import resolve_app_id_for_platform
from backend.step_contract import normalize_action, normalize_execute_on

IOS_LOCATOR_ACTIONS = {"click", "input", "wait_until_exists", "assert_text"}
IOS_APP_ACTIONS = {"start_app", "stop_app"}
IOS_NO_LOCATOR_ACTIONS = {
    "sleep",
    "swipe",
    "back",
    "home",
    "click_image",
    "assert_image",
    "extract_by_ocr",
}
IOS_UNSUPPORTED_ACTIONS = set()

ANDROID_BY_TO_IOS_BY = {
    "text": "label",
    "label": "label",
    "name": "name",
    "xpath": "xpath",
    "id": "id",
    "resourceid": "id",
    "resource_id": "id",
    "description": "id",
    "desc": "id",
    "content-desc": "id",
    "accessibilityid": "id",
    "accessibility_id": "id",
}


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalize_locator_override(raw: Any) -> Optional[Dict[str, str]]:
    if not isinstance(raw, dict):
        return None

    selector = _clean_text(raw.get("selector"))
    by = _clean_text(raw.get("by")).lower()
    if not selector or not by:
        return None
    return {"selector": selector, "by": by}


def _extract_step_app_key(step_item: Dict[str, Any]) -> str:
    args = step_item.get("args")
    if isinstance(args, dict):
        for key in ("app_key", "app_id"):
            value = _clean_text(args.get(key))
            if value:
                return value

    for key in ("value", "selector"):
        value = _clean_text(step_item.get(key))
        if value:
            return value

    overrides = step_item.get("platform_overrides")
    if isinstance(overrides, dict):
        for platform in ("android", "ios"):
            candidate = overrides.get(platform)
            if not isinstance(candidate, dict):
                continue
            value = _clean_text(candidate.get("app_key") or candidate.get("selector"))
            if value:
                return value
    return ""


def _infer_ios_override_from_android(
    android_override: Optional[Dict[str, str]],
) -> Optional[Dict[str, str]]:
    if not android_override:
        return None

    selector = _clean_text(android_override.get("selector"))
    android_by = _clean_text(android_override.get("by")).lower()
    ios_by = ANDROID_BY_TO_IOS_BY.get(android_by)
    if not selector or not ios_by:
        return None
    return {"selector": selector, "by": ios_by}


def autofill_step_for_ios(
    step_payload: Dict[str, Any],
    app_mapping: Optional[Dict[str, Dict[str, str]]] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Return (updated_step, meta) with conservative iOS autofill behavior.

    Rules:
    - unsupported actions remove iOS from execute_on
    - no-locator actions (sleep/swipe/back) add iOS
    - locator actions try to generate iOS override from Android override
    - start/stop_app only add iOS when app mapping is resolvable
    """
    step = dict(step_payload or {})
    action = normalize_action(step.get("action"))
    execute_on = normalize_execute_on(step.get("execute_on"))
    overrides = step.get("platform_overrides")
    if not isinstance(overrides, dict):
        overrides = {}

    changes: List[str] = []
    blockers: List[str] = []

    def _add_ios() -> None:
        if "ios" not in execute_on:
            execute_on.append("ios")
            changes.append("add_execute_on_ios")

    def _remove_ios(reason: str) -> None:
        if "ios" in execute_on:
            execute_on.remove("ios")
            changes.append(reason)

    if action in IOS_UNSUPPORTED_ACTIONS:
        _remove_ios("remove_execute_on_ios_unsupported_action")
        blockers.append("ios_action_not_supported")
    elif action in IOS_NO_LOCATOR_ACTIONS:
        _add_ios()
    elif action in IOS_LOCATOR_ACTIONS:
        ios_override = _normalize_locator_override(overrides.get("ios"))
        if not ios_override:
            android_override = _normalize_locator_override(overrides.get("android"))
            ios_override = _infer_ios_override_from_android(android_override)
            if ios_override:
                overrides["ios"] = ios_override
                changes.append("generate_ios_override_from_android")
            else:
                blockers.append("missing_or_unmappable_android_override")
        if ios_override:
            _add_ios()
        else:
            blockers.append("missing_ios_override")
    elif action in IOS_APP_ACTIONS:
        app_key = _extract_step_app_key(step)
        if not app_key:
            blockers.append("missing_app_key")
        else:
            try:
                resolve_app_id_for_platform(
                    app_key=app_key,
                    platform="ios",
                    mapping=app_mapping or {},
                )
                _add_ios()
            except Exception:
                blockers.append("missing_ios_app_mapping")

    step["action"] = action
    step["execute_on"] = execute_on
    step["platform_overrides"] = overrides

    return step, {
        "action": action,
        "changed": bool(changes),
        "changes": changes,
        "blockers": blockers,
    }
