"""Cross-platform step contract helpers.

This module normalizes actions and converts between:
- legacy case.steps JSON schema
- standard TestCaseStep rows
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Dict, Iterable, List, Optional

from backend.utils.pydantic_compat import dump_model

VALID_ACTIONS = {
    "click",
    "input",
    "wait_until_exists",
    "assert_text",
    "assert_image",
    "swipe",
    "start_app",
    "stop_app",
    "back",
    "home",
    "sleep",
    "click_image",
    "extract_by_ocr",
}

ACTION_ALIASES = {
    "CLICK": "click",
    "INPUT": "input",
    "WAIT_UNTIL_EXISTS": "wait_until_exists",
    "ASSERT_TEXT": "assert_text",
    "ASSERT_IMAGE": "assert_image",
    "SWIPE": "swipe",
    "START_APP": "start_app",
    "STOP_APP": "stop_app",
    "BACK": "back",
    "HOME": "home",
    "SLEEP": "sleep",
    "CLICK_IMAGE": "click_image",
    "EXTRACT_BY_OCR": "extract_by_ocr",
}

VALID_PLATFORMS = {"android", "ios"}
VALID_ERROR_STRATEGIES = {"ABORT", "CONTINUE", "IGNORE"}

SELECTOR_TYPE_TO_BY = {
    "resourceId": "id",
    "text": "text",
    "xpath": "xpath",
    "description": "description",
    "image": "image",
}
SELECTOR_TYPE_TO_BY_LOWER = {k.lower(): v for k, v in SELECTOR_TYPE_TO_BY.items()}

BY_TO_SELECTOR_TYPE = {
    "id": "resourceId",
    "resourceid": "resourceId",
    "resource_id": "resourceId",
    "text": "text",
    "xpath": "xpath",
    "description": "description",
    "desc": "description",
    "image": "image",
    "label": "text",  # UI fallback for iOS selectors in Android-only recorder
    "name": "text",   # UI fallback for iOS selectors in Android-only recorder
}


def _unwrap_enum(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    return value


def normalize_action(action: Any) -> str:
    action = _unwrap_enum(action)
    if action is None:
        raise ValueError("action is required")

    raw = str(action).strip()
    if not raw:
        raise ValueError("action is required")

    action_lower = raw.lower()
    if action_lower in VALID_ACTIONS:
        return action_lower

    alias = ACTION_ALIASES.get(raw.upper())
    if alias:
        return alias

    raise ValueError(f"unsupported action: {raw}")


def normalize_error_strategy(strategy: Any) -> str:
    strategy = _unwrap_enum(strategy)
    raw = str(strategy or "ABORT").strip().upper()
    if raw not in VALID_ERROR_STRATEGIES:
        raise ValueError(f"unsupported error_strategy: {raw}")
    return raw


def normalize_execute_on(execute_on: Optional[Iterable[Any]]) -> List[str]:
    values = list(execute_on or ["android", "ios"])
    normalized: List[str] = []

    for item in values:
        item = _unwrap_enum(item)
        platform = str(item).strip().lower()
        if platform not in VALID_PLATFORMS:
            raise ValueError(f"unsupported platform in execute_on: {item}")
        if platform not in normalized:
            normalized.append(platform)

    if not normalized:
        raise ValueError("execute_on cannot be empty")

    return normalized


def _normalize_platform_selector(platform: str, selector: Any) -> Optional[Dict[str, str]]:
    if selector is None:
        return None

    selector = dump_model(selector, exclude_none=True)

    if not isinstance(selector, dict):
        raise ValueError(f"platform_overrides.{platform} must be an object")

    unknown_fields = sorted(set(selector.keys()) - {"selector", "by"})
    if unknown_fields:
        raise ValueError(
            f"platform_overrides.{platform} has unsupported fields: {', '.join(unknown_fields)}"
        )

    value = _unwrap_enum(selector.get("selector"))
    by = _unwrap_enum(selector.get("by"))

    selector_text = "" if value is None else str(value).strip()
    by_text = "" if by is None else str(by).strip().lower()

    if not selector_text and not by_text:
        return None
    if not selector_text or not by_text:
        raise ValueError(f"platform_overrides.{platform} requires both selector and by")

    return {"selector": selector_text, "by": by_text}


def normalize_platform_overrides(raw: Any) -> Dict[str, Dict[str, str]]:
    if raw is None:
        return {}

    raw = dump_model(raw, exclude_none=True)

    if not isinstance(raw, dict):
        raise ValueError("platform_overrides must be an object")

    unknown_platforms = sorted(set(raw.keys()) - VALID_PLATFORMS)
    if unknown_platforms:
        raise ValueError(
            f"unsupported platform in platform_overrides: {', '.join(unknown_platforms)}"
        )

    normalized: Dict[str, Dict[str, str]] = {}
    for platform in VALID_PLATFORMS:
        if platform not in raw:
            continue
        candidate = _normalize_platform_selector(platform, raw.get(platform))
        if candidate:
            normalized[platform] = candidate

    return normalized


def _to_dict(step: Any) -> Dict[str, Any]:
    data = dump_model(step)
    if isinstance(data, dict):
        return data
    raise ValueError(f"unsupported step object: {type(step)}")


def _safe_timeout(value: Any, default: int = 10) -> int:
    try:
        timeout = int(value)
        return timeout if timeout > 0 else default
    except Exception:
        return default


def _safe_seconds(value: Any, default: float = 1.0) -> float:
    try:
        seconds = float(value)
        return seconds if seconds >= 0 else default
    except Exception:
        return default


def legacy_step_to_standard(step: Any, case_id: int, order: int) -> Dict[str, Any]:
    raw = _to_dict(step)

    action = normalize_action(raw.get("action"))
    selector = raw.get("selector")
    selector_type = _unwrap_enum(raw.get("selector_type"))
    value = raw.get("value")
    options = raw.get("options") if isinstance(raw.get("options"), dict) else {}

    if action in {"click_image", "assert_image"}:
        image_path_candidate = selector
        if image_path_candidate in (None, ""):
            image_path_candidate = options.get("image_path") or options.get("path")
        if image_path_candidate in (None, ""):
            image_path_candidate = value
        if selector in (None, "") and image_path_candidate not in (None, ""):
            selector = image_path_candidate

    by = None
    if selector_type is not None:
        selector_type_text = str(selector_type).strip()
        by = SELECTOR_TYPE_TO_BY.get(selector_type_text)
        if by is None:
            by = SELECTOR_TYPE_TO_BY_LOWER.get(selector_type_text.lower())
    if action in {"click_image", "assert_image"} and selector not in (None, "") and not by:
        by = "image"

    overrides: Dict[str, Dict[str, str]] = {}
    if action != "assert_text" and selector not in (None, "") and by:
        overrides["android"] = {
            "selector": str(selector),
            "by": by,
        }

    args: Dict[str, Any] = {}
    if action == "input":
        args = {"text": "" if value is None else str(value)}
    elif action == "assert_text":
        match_mode = "contains"
        if isinstance(options, dict) and options.get("match_mode") == "not_contains":
            match_mode = "not_contains"
        args = {
            "expected_text": "" if value is None else str(value),
            "match_mode": match_mode,
        }
    elif action == "assert_image":
        match_mode = "exists"
        if isinstance(options, dict) and options.get("match_mode") == "not_exists":
            match_mode = "not_exists"
        if selector not in (None, ""):
            args = {
                "image_path": str(selector),
                "match_mode": match_mode,
            }
    elif action == "swipe":
        args = {"direction": str(selector or "up").lower()}
    elif action == "sleep":
        args = {"seconds": _safe_seconds(value, default=1.0)}
    elif action in ("start_app", "stop_app"):
        args = {"app_key": "" if selector is None else str(selector)}
    elif action == "click_image":
        if selector not in (None, ""):
            args = {"image_path": str(selector)}
    elif action == "extract_by_ocr":
        args = {
            "region": selector,
            "extract_rule": raw.get("options") or {},
        }

    return {
        "case_id": case_id,
        "order": order,
        "action": action,
        "args": args,
        "value": None if value is None else str(value),
        "execute_on": ["android"],
        "platform_overrides": overrides,
        "timeout": _safe_timeout(raw.get("timeout"), default=10),
        "error_strategy": normalize_error_strategy(raw.get("error_strategy", "ABORT")),
        "description": raw.get("description"),
    }


def standard_step_to_legacy(step: Any) -> Dict[str, Any]:
    raw = _to_dict(step)

    action = normalize_action(raw.get("action"))
    args = raw.get("args") or {}
    overrides = raw.get("platform_overrides") or {}
    android = overrides.get("android") or {}

    selector = android.get("selector")
    by = android.get("by")
    selector_type = BY_TO_SELECTOR_TYPE.get(str(by).lower(), None) if by else None

    value = raw.get("value")
    if value in (None, ""):
        if action == "input":
            value = args.get("text")
        elif action == "assert_text":
            value = args.get("expected_text")
        elif action == "sleep":
            if "seconds" in args:
                value = str(args.get("seconds"))

    if action == "swipe" and not selector:
        selector = args.get("direction", "up")
    if action in ("start_app", "stop_app") and not selector:
        selector = args.get("app_key", "")
    if action in {"click_image", "assert_image"} and not selector:
        selector = args.get("image_path", "")
    if action == "extract_by_ocr" and not selector and "region" in args:
        selector = str(args.get("region"))

    options: Dict[str, Any] = {}
    if action == "assert_text":
        selector = ""
        selector_type = None
        options = {
            "match_mode": "not_contains"
            if str(args.get("match_mode") or "").strip().lower() == "not_contains"
            else "contains"
        }
    elif action == "assert_image":
        options = {
            "match_mode": "not_exists"
            if str(args.get("match_mode") or "").strip().lower() == "not_exists"
            else "exists"
        }
    elif action == "extract_by_ocr":
        extract_rule = args.get("extract_rule")
        if isinstance(extract_rule, dict):
            options = extract_rule
        elif isinstance(extract_rule, str):
            options = {"extract_rule": extract_rule}

    return {
        "action": action,
        "selector": selector,
        "selector_type": selector_type,
        "value": value,
        "options": options,
        "description": raw.get("description"),
        "timeout": _safe_timeout(raw.get("timeout"), default=10),
        "error_strategy": normalize_error_strategy(raw.get("error_strategy", "ABORT")),
    }


def build_standard_from_legacy_steps(steps: Iterable[Any], case_id: int) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for idx, step in enumerate(steps or [], start=1):
        result.append(legacy_step_to_standard(step, case_id=case_id, order=idx))
    return result


def build_legacy_from_standard_steps(steps: Iterable[Any]) -> List[Dict[str, Any]]:
    sorted_steps = sorted(
        list(steps or []),
        key=lambda s: ((_to_dict(s).get("order") or 0), (_to_dict(s).get("id") or 0)),
    )
    return [standard_step_to_legacy(step) for step in sorted_steps]
