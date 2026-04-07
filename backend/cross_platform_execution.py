"""Shared helpers for cross-platform case execution."""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from urllib.parse import urlparse, urlunparse
from typing import Any, Dict, List, Optional, Tuple

from sqlmodel import Session, select

from backend.feature_flags import FLAG_IOS_EXECUTION, get_setting_value, is_flag_enabled
from backend.locator_resolution import resolve_locator_candidates
from backend.models import Device, GlobalVariable, TestCase, TestCaseStep
from backend.step_contract import (
    build_standard_from_legacy_steps,
    normalize_action,
    normalize_execute_on,
)
from backend.utils.variable_render import render_step_data
from backend.wda_port_manager import wda_relay_manager

logger = logging.getLogger(__name__)
_SUPPORTED_PLATFORMS = {"android", "ios"}
_ERROR_CODE_PATTERN = re.compile(r"^(P\d{4}_[A-Z0-9_]+)")
_UNRESOLVED_VAR_PATTERN = re.compile(r"{{\s*([A-Z0-9_]+)\s*}}")
_LOCATOR_REQUIRED_ACTIONS = {"click", "wait_until_exists"}
_BUNDLE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)+$")
_EXECUTE_ON_COMPAT_UPGRADE_ACTIONS = {"home", "click_image", "assert_image", "extract_by_ocr"}
_SUPPORTED_ACTIONS_BY_PLATFORM = {
    "android": {
        "click",
        "input",
        "wait_until_exists",
        "assert_text",
        "assert_image",
        "click_image",
        "extract_by_ocr",
        "sleep",
        "swipe",
        "back",
        "home",
        "start_app",
        "stop_app",
    },
    "ios": {
        "click",
        "input",
        "wait_until_exists",
        "assert_text",
        "assert_image",
        "click_image",
        "extract_by_ocr",
        "sleep",
        "swipe",
        "back",
        "home",
        "start_app",
        "stop_app",
    },
}
_VALID_DIRECTIONS = {"up", "down", "left", "right"}


def _default_execute_on_for_action(action: Any) -> List[str]:
    action_lower = _clean_text(action).lower()
    if action_lower in _SUPPORTED_ACTIONS_BY_PLATFORM.get("ios", set()):
        return ["android", "ios"]
    return ["android"]


def _normalize_step_execute_on(action: Any, execute_on_raw: Any) -> List[str]:
    action_lower = _clean_text(action).lower()
    default_execute_on = _default_execute_on_for_action(action)
    try:
        execute_on = normalize_execute_on(execute_on_raw)
    except Exception:
        return list(default_execute_on)

    # 兼容历史标准步骤：部分动作在早期版本被标记为 Android-only。
    if (
        action_lower in _EXECUTE_ON_COMPAT_UPGRADE_ACTIONS
        and execute_on == ["android"]
        and default_execute_on == ["android", "ios"]
    ):
        return ["android", "ios"]
    return execute_on


def _row_to_step_payload(row: TestCaseStep) -> Dict[str, Any]:
    return {
        "order": row.order,
        "action": row.action,
        "args": row.args or {},
        "value": row.value,
        "execute_on": _normalize_step_execute_on(row.action, row.execute_on),
        "platform_overrides": row.platform_overrides or {},
        "timeout": row.timeout,
        "error_strategy": row.error_strategy,
        "description": row.description,
    }


def list_standard_step_payloads(session: Session, case: TestCase) -> List[Dict[str, Any]]:
    """Return standard step payloads for execution (table first, legacy fallback)."""
    rows = session.exec(
        select(TestCaseStep)
        .where(TestCaseStep.case_id == case.id)
        .order_by(TestCaseStep.order, TestCaseStep.id)
    ).all()
    if rows:
        return [_row_to_step_payload(row) for row in rows]

    return build_standard_from_legacy_steps(case.steps or [], case_id=case.id)


def collect_case_variables_map(
    session: Session,
    case: TestCase,
    env_id: Optional[int],
) -> Dict[str, str]:
    """Merge env globals + case variables into one map."""
    variables_map: Dict[str, str] = {}

    if env_id:
        global_vars = session.exec(
            select(GlobalVariable).where(GlobalVariable.env_id == env_id)
        ).all()
        for gv in global_vars:
            if gv.key:
                variables_map[gv.key] = gv.value

    for item in case.variables or []:
        if isinstance(item, dict):
            key = str(item.get("key") or "").strip()
            value = item.get("value")
        else:
            key = str(getattr(item, "key", "") or "").strip()
            value = getattr(item, "value", None)

        if key:
            variables_map[key] = "" if value is None else str(value)

    return variables_map


def render_with_variables(raw: Any, variables: Dict[str, str]) -> Any:
    """Recursively render {{ VAR }} placeholders in strings."""
    if isinstance(raw, str):
        return render_step_data(raw, variables)
    if isinstance(raw, list):
        return [render_with_variables(item, variables) for item in raw]
    if isinstance(raw, dict):
        return {k: render_with_variables(v, variables) for k, v in raw.items()}
    return raw


def _collect_unresolved_templates(value: Any) -> List[str]:
    found: List[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, str):
            match = _UNRESOLVED_VAR_PATTERN.search(node)
            if match:
                found.append(match.group(0))
            return
        if isinstance(node, list):
            for item in node:
                _walk(item)
            return
        if isinstance(node, dict):
            for item in node.values():
                _walk(item)

    _walk(value)
    return found


def _extract_template_var_name(template: str) -> str:
    match = _UNRESOLVED_VAR_PATTERN.search(str(template or ""))
    if not match:
        return ""
    return str(match.group(1) or "").strip()


def _collect_step_runtime_export_vars(action: str, args: Dict[str, Any], value: Any) -> List[str]:
    action_lower = _clean_text(action).lower()
    if action_lower == "extract_by_ocr":
        candidate = _clean_text(args.get("output_var", value))
        if candidate:
            return [candidate]
    return []


def resolve_device_platform(session: Session, device_serial: str) -> str:
    device = session.exec(select(Device).where(Device.serial == device_serial)).first()
    if not device:
        raise RuntimeError(f"device not found: {device_serial}")

    platform = str(device.platform or "android").strip().lower()
    if platform not in ("android", "ios"):
        raise RuntimeError(f"unsupported device platform: {platform}")
    return platform


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _extract_error_code(message: Any) -> Optional[str]:
    text = _clean_text(message)
    if not text:
        return None
    match = _ERROR_CODE_PATTERN.match(text)
    if not match:
        return None
    return match.group(1)


def _merge_app_mapping(
    target: Dict[str, Dict[str, str]],
    app_key: Any,
    platform: str,
    app_id: Any,
) -> None:
    key = _clean_text(app_key)
    app_identifier = _clean_text(app_id)
    platform_lower = _clean_text(platform).lower()
    if not key or not app_identifier or platform_lower not in _SUPPORTED_PLATFORMS:
        return
    bucket = target.setdefault(key, {})
    bucket[platform_lower] = app_identifier


def _parse_json_object(raw_value: Optional[str], setting_key: str) -> Dict[str, Any]:
    if not raw_value:
        return {}
    try:
        parsed = json.loads(raw_value)
    except Exception as exc:
        logger.warning(
            "invalid JSON in system setting: key=%s error=%s",
            setting_key,
            exc,
        )
        return {}
    if not isinstance(parsed, dict):
        logger.warning(
            "system setting expects JSON object: key=%s type=%s",
            setting_key,
            type(parsed).__name__,
        )
        return {}
    return parsed


def load_app_key_mapping(session: Session) -> Dict[str, Dict[str, str]]:
    """
    Load app_key mapping from system settings.

    Supported schemas:
    1) Unified map (recommended):
       {
         "mall_app": {"android": "com.demo.mall", "ios": "com.demo.mall.ios"}
       }
    2) Platform-split map:
       {
         "android": {"mall_app": "com.demo.mall"},
         "ios": {"mall_app": "com.demo.mall.ios"}
       }
    3) Legacy split settings:
       - android_app_map: {"mall_app":"com.demo.mall"}
       - ios_app_map: {"mall_app":"com.demo.mall.ios"}
    """
    merged: Dict[str, Dict[str, str]] = {}

    for setting_key in ("app_key_map", "app_key_mapping"):
        payload = _parse_json_object(get_setting_value(session, setting_key), setting_key)
        if not payload:
            continue

        for platform in ("android", "ios"):
            section = payload.get(platform)
            if isinstance(section, dict):
                for app_key, app_id in section.items():
                    _merge_app_mapping(merged, app_key, platform, app_id)

        for app_key, platform_value in payload.items():
            if app_key in _SUPPORTED_PLATFORMS:
                continue
            if isinstance(platform_value, dict):
                _merge_app_mapping(
                    merged,
                    app_key,
                    "android",
                    (
                        platform_value.get("android")
                        or platform_value.get("package")
                        or platform_value.get("package_name")
                    ),
                )
                _merge_app_mapping(
                    merged,
                    app_key,
                    "ios",
                    (
                        platform_value.get("ios")
                        or platform_value.get("bundleId")
                        or platform_value.get("bundle_id")
                    ),
                )
            elif isinstance(platform_value, str):
                # String shorthand keeps backward compatibility for Android.
                _merge_app_mapping(merged, app_key, "android", platform_value)

    for platform, setting_key in (("android", "android_app_map"), ("ios", "ios_app_map")):
        payload = _parse_json_object(get_setting_value(session, setting_key), setting_key)
        for app_key, app_id in payload.items():
            _merge_app_mapping(merged, app_key, platform, app_id)

    return merged


def resolve_app_id_for_platform(
    app_key: Any,
    platform: str,
    mapping: Dict[str, Dict[str, str]],
) -> str:
    platform_lower = _clean_text(platform).lower()
    if platform_lower not in _SUPPORTED_PLATFORMS:
        raise RuntimeError(f"unsupported device platform: {platform}")

    key = _clean_text(app_key)
    if not key:
        raise RuntimeError("P1004_APP_MAPPING_MISSING: app_key is empty")

    app_id = _clean_text((mapping.get(key) or {}).get(platform_lower))
    if app_id:
        return app_id

    # Android 保持兼容：未配置映射时沿用 app_key 原值（历史步骤常直接存 package）。
    if platform_lower == "android":
        return key

    # iOS 兼容：若 app_key 本身像 bundleId，允许直接透传。
    if _BUNDLE_ID_PATTERN.match(key):
        return key

    raise RuntimeError(
        f"P1004_APP_MAPPING_MISSING: app_key={key!r} 未映射到平台 {platform_lower}"
    )


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


def _extract_override_selector(step_item: Dict[str, Any], platform: str) -> str:
    overrides = step_item.get("platform_overrides")
    if not isinstance(overrides, dict):
        return ""
    candidate = overrides.get(platform)
    if not isinstance(candidate, dict):
        return ""
    return _clean_text(candidate.get("selector"))


def _first_locator_selector(locator_candidates: List[Dict[str, Any]]) -> str:
    first = locator_candidates[0] if locator_candidates else {}
    return _clean_text(first.get("selector"))


def _resolve_image_path(
    step_item: Dict[str, Any],
    args: Dict[str, Any],
    platform: str,
    first_selector: str,
    value: Any,
) -> str:
    options = step_item.get("options")
    option_image_path = ""
    if isinstance(options, dict):
        option_image_path = _clean_text(options.get("image_path") or options.get("path"))

    return _clean_text(
        args.get("image_path")
        or args.get("path")
        or first_selector
        or step_item.get("selector")
        or _extract_override_selector(step_item, platform)
        or _extract_override_selector(step_item, "android")
        or option_image_path
        or value
    )


def _resolve_extract_region(
    step_item: Dict[str, Any],
    args: Dict[str, Any],
    platform: str,
    first_selector: str,
) -> str:
    return _clean_text(
        args.get("region")
        or first_selector
        or step_item.get("selector")
        or _extract_override_selector(step_item, platform)
        or _extract_override_selector(step_item, "android")
    )


def _build_step_precheck_result(
    order: int,
    action: str,
    status: str,
    code: Optional[str],
    message: Optional[str],
) -> Dict[str, Any]:
    return {
        "order": order,
        "action": action,
        "status": status,
        "code": code,
        "message": message,
    }


def precheck_steps_for_platform(
    session: Session,
    steps: List[Dict[str, Any]],
    platform: str,
    known_variable_keys: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """
    Precheck standard steps for target platform without executing device actions.

    Returns step-level statuses:
    - PASS: executable
    - SKIP: execute_on mismatch
    - FAIL: blocking issue for this step
    """
    platform_lower = _clean_text(platform).lower()
    if platform_lower not in _SUPPORTED_PLATFORMS:
        raise RuntimeError(f"unsupported device platform: {platform}")

    mapping = load_app_key_mapping(session)
    results: List[Dict[str, Any]] = []
    available_runtime_vars = {
        _clean_text(item)
        for item in (known_variable_keys or [])
        if _clean_text(item)
    }

    for index, raw_step in enumerate(steps or [], start=1):
        step_item = dict(raw_step or {})
        raw_action = step_item.get("action")
        action = _clean_text(raw_action).lower() or "unknown"

        try:
            action = normalize_action(raw_action)
        except Exception as exc:
            msg = f"P1002_ACTION_NOT_SUPPORTED: {exc}"
            results.append(
                _build_step_precheck_result(
                    order=index,
                    action=action,
                    status="FAIL",
                    code=_extract_error_code(msg),
                    message=msg,
                )
            )
            continue

        try:
            execute_on = _normalize_step_execute_on(action, step_item.get("execute_on"))
        except Exception as exc:
            msg = f"P1006_INVALID_ARGS: {exc}"
            results.append(
                _build_step_precheck_result(
                    order=index,
                    action=action,
                    status="FAIL",
                    code=_extract_error_code(msg),
                    message=msg,
                )
            )
            continue

        if platform_lower not in execute_on:
            msg = (
                "P1001_PLATFORM_NOT_ALLOWED: "
                f"execute_on={execute_on}, current={platform_lower}"
            )
            results.append(
                _build_step_precheck_result(
                    order=index,
                    action=action,
                    status="SKIP",
                    code=_extract_error_code(msg),
                    message=msg,
                )
            )
            continue

        if action not in _SUPPORTED_ACTIONS_BY_PLATFORM.get(platform_lower, set()):
            msg = (
                "P1002_ACTION_NOT_SUPPORTED: "
                f"platform={platform_lower}, action={action}"
            )
            results.append(
                _build_step_precheck_result(
                    order=index,
                    action=action,
                    status="FAIL",
                    code=_extract_error_code(msg),
                    message=msg,
                )
            )
            continue

        args = step_item.get("args") or {}
        if not isinstance(args, dict):
            msg = "P1006_INVALID_ARGS: args must be an object"
            results.append(
                _build_step_precheck_result(
                    order=index,
                    action=action,
                    status="FAIL",
                    code=_extract_error_code(msg),
                    message=msg,
                )
            )
            continue

        value = step_item.get("value")
        locator_candidates = resolve_locator_candidates(step_item, platform=platform_lower)
        selector = _first_locator_selector(locator_candidates)
        export_vars = _collect_step_runtime_export_vars(action, args, value)
        unresolved_templates = _collect_unresolved_templates(
            {
                "args": args,
                "value": value,
                "locators": locator_candidates,
            }
        )
        unresolved_blocking = []
        for token in unresolved_templates:
            key = _extract_template_var_name(token)
            if key and key in available_runtime_vars:
                continue
            unresolved_blocking.append(token)

        if unresolved_blocking:
            preview = ", ".join(unresolved_blocking[:3])
            msg = f"P1006_INVALID_ARGS: 存在未解析变量占位符: {preview}"
            results.append(
                _build_step_precheck_result(
                    order=index,
                    action=action,
                    status="FAIL",
                    code=_extract_error_code(msg),
                    message=msg,
                )
            )
            continue

        if action in _LOCATOR_REQUIRED_ACTIONS and not locator_candidates:
            msg = f"P1003_SELECTOR_MISSING: {action} 动作缺少 selector/by"
            results.append(
                _build_step_precheck_result(
                    order=index,
                    action=action,
                    status="FAIL",
                    code=_extract_error_code(msg),
                    message=msg,
                )
            )
            continue

        if action in {"click_image", "assert_image"}:
            image_path = _resolve_image_path(
                step_item=step_item,
                args=args,
                platform=platform_lower,
                first_selector=selector,
                value=value,
            )
            if not image_path:
                msg = f"P1006_INVALID_ARGS: {action} 动作缺少 image_path/selector"
                results.append(
                    _build_step_precheck_result(
                        order=index,
                        action=action,
                        status="FAIL",
                        code=_extract_error_code(msg),
                        message=msg,
                    )
                )
                continue
            if action == "assert_image":
                match_mode = _clean_text(args.get("match_mode") or "exists").lower()
                if match_mode not in {"exists", "not_exists"}:
                    msg = f"P1006_INVALID_ARGS: assert_image 不支持的 match_mode: {match_mode}"
                    results.append(
                        _build_step_precheck_result(
                            order=index,
                            action=action,
                            status="FAIL",
                            code=_extract_error_code(msg),
                            message=msg,
                        )
                    )
                    continue

        if action == "extract_by_ocr":
            region = _resolve_extract_region(
                step_item=step_item,
                args=args,
                platform=platform_lower,
                first_selector=selector,
            )
            if not region:
                msg = "P1006_INVALID_ARGS: extract_by_ocr 动作缺少 args.region/selector"
                results.append(
                    _build_step_precheck_result(
                        order=index,
                        action=action,
                        status="FAIL",
                        code=_extract_error_code(msg),
                        message=msg,
                    )
                )
                continue

        if action == "input":
            text = args.get("text", value)
            if text is None:
                msg = "P1006_INVALID_ARGS: input 动作缺少 args.text/value"
                results.append(
                    _build_step_precheck_result(
                        order=index,
                        action=action,
                        status="FAIL",
                        code=_extract_error_code(msg),
                        message=msg,
                    )
                )
                continue

        if action == "assert_text":
            expected_text = args.get("expected_text", value)
            if expected_text is None:
                msg = "P1006_INVALID_ARGS: assert_text 动作缺少 args.expected_text/value"
                results.append(
                    _build_step_precheck_result(
                        order=index,
                        action=action,
                        status="FAIL",
                        code=_extract_error_code(msg),
                        message=msg,
                    )
                )
                continue

        if action == "swipe":
            direction = _clean_text(args.get("direction", selector or "up")).lower()
            if direction not in _VALID_DIRECTIONS:
                msg = f"P1006_INVALID_ARGS: 不支持的滑动方向: {direction}"
                results.append(
                    _build_step_precheck_result(
                        order=index,
                        action=action,
                        status="FAIL",
                        code=_extract_error_code(msg),
                        message=msg,
                    )
                )
                continue

        if action == "sleep":
            try:
                seconds = float(args.get("seconds", value if value is not None else 1))
                if seconds < 0:
                    raise ValueError("sleep seconds cannot be negative")
            except Exception as exc:
                msg = f"P1006_INVALID_ARGS: {exc}"
                results.append(
                    _build_step_precheck_result(
                        order=index,
                        action=action,
                        status="FAIL",
                        code=_extract_error_code(msg),
                        message=msg,
                    )
                )
                continue

        if action in ("start_app", "stop_app"):
            app_key = _extract_step_app_key(step_item)
            if not app_key:
                msg = (
                    "P1004_APP_MAPPING_MISSING: "
                    f"step#{index} action={action} 缺少 app_key"
                )
                results.append(
                    _build_step_precheck_result(
                        order=index,
                        action=action,
                        status="FAIL",
                        code=_extract_error_code(msg),
                        message=msg,
                    )
                )
                continue
            try:
                resolve_app_id_for_platform(app_key=app_key, platform=platform_lower, mapping=mapping)
            except Exception as exc:
                msg = str(exc)
                results.append(
                    _build_step_precheck_result(
                        order=index,
                        action=action,
                        status="FAIL",
                        code=_extract_error_code(msg),
                        message=msg,
                    )
                )
                continue

        results.append(
            _build_step_precheck_result(
                order=index,
                action=action,
                status="PASS",
                code=None,
                message=None,
            )
        )
        available_runtime_vars.update(export_vars)

    return results


def prepare_steps_for_platform(
    session: Session,
    steps: List[Dict[str, Any]],
    platform: str,
) -> List[Dict[str, Any]]:
    """
    Execute prechecks and normalize platform-specific payloads before dispatch.

    Current precheck coverage:
    - app_key mapping for start_app/stop_app (P1004)
    """
    platform_lower = _clean_text(platform).lower()
    if platform_lower not in _SUPPORTED_PLATFORMS:
        raise RuntimeError(f"unsupported device platform: {platform}")

    mapping = load_app_key_mapping(session)
    prepared: List[Dict[str, Any]] = []

    for index, raw_step in enumerate(steps or [], start=1):
        step_item = dict(raw_step or {})
        action = normalize_action(step_item.get("action"))
        execute_on = _normalize_step_execute_on(action, step_item.get("execute_on"))
        step_item["execute_on"] = list(execute_on)

        args = step_item.get("args")
        args = dict(args) if isinstance(args, dict) else {}
        value = step_item.get("value")
        locator_candidates = resolve_locator_candidates(step_item, platform=platform_lower)
        first_selector = _first_locator_selector(locator_candidates)

        if platform_lower in execute_on and action in {"click_image", "assert_image"}:
            image_path = _resolve_image_path(
                step_item=step_item,
                args=args,
                platform=platform_lower,
                first_selector=first_selector,
                value=value,
            )
            if image_path:
                args["image_path"] = image_path

        if platform_lower in execute_on and action == "extract_by_ocr":
            region = _resolve_extract_region(
                step_item=step_item,
                args=args,
                platform=platform_lower,
                first_selector=first_selector,
            )
            if region:
                args["region"] = region

        if platform_lower in execute_on and action in ("start_app", "stop_app"):
            app_key = _extract_step_app_key(step_item)
            if not app_key:
                raise RuntimeError(
                    f"P1004_APP_MAPPING_MISSING: step#{index} action={action} 缺少 app_key"
                )
            args["app_key"] = resolve_app_id_for_platform(
                app_key=app_key,
                platform=platform_lower,
                mapping=mapping,
            )

        step_item["args"] = args

        prepared.append(step_item)

    return prepared


def prepare_case_steps_for_platform(
    session: Session,
    case: TestCase,
    platform: str,
    env_id: Optional[int] = None,
    variables_map: Optional[Dict[str, str]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    if variables_map is None:
        variables_map = collect_case_variables_map(session, case, env_id=env_id)

    step_payloads = list_standard_step_payloads(session, case)
    rendered_steps = [
        render_with_variables(dict(step_item), variables_map)
        for step_item in step_payloads
    ]
    prepared_steps = prepare_steps_for_platform(session, rendered_steps, platform=platform)
    return prepared_steps, dict(variables_map)


def precheck_case_execution(
    session: Session,
    case: TestCase,
    device_serial: str,
    env_id: Optional[int] = None,
    variables_map: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    platform = resolve_device_platform(session, device_serial)
    global_checks: List[Dict[str, Any]] = []

    if platform == "ios":
        if not is_flag_enabled(session, FLAG_IOS_EXECUTION, default=False):
            global_checks.append(
                {
                    "status": "FAIL",
                    "code": "IOS_EXECUTION_DISABLED",
                    "message": "iOS execution is disabled by feature flag",
                }
            )
        else:
            try:
                wda_url = resolve_ios_wda_url(session, device_serial)
                check_wda_health(wda_url)
            except Exception as exc:
                msg = str(exc)
                global_checks.append(
                    {
                        "status": "FAIL",
                        "code": _extract_error_code(msg) or "WDA_UNAVAILABLE",
                        "message": msg,
                    }
                )

    if variables_map is None:
        variables_map = collect_case_variables_map(session, case, env_id=env_id)

    normalized_variables_map: Dict[str, str] = {
        str(key): "" if value is None else str(value)
        for key, value in dict(variables_map).items()
        if str(key).strip()
    }

    step_payloads = list_standard_step_payloads(session, case)
    rendered_steps = [
        render_with_variables(dict(step_item), normalized_variables_map)
        for step_item in step_payloads
    ]
    step_checks = precheck_steps_for_platform(
        session,
        rendered_steps,
        platform=platform,
        known_variable_keys=list(normalized_variables_map.keys()),
    )

    predicted_exported_variables = dict(normalized_variables_map)
    for index, step_check in enumerate(step_checks):
        if step_check.get("status") != "PASS":
            continue
        if index >= len(rendered_steps):
            continue
        step_item = rendered_steps[index] or {}
        step_args = step_item.get("args")
        step_args = dict(step_args) if isinstance(step_args, dict) else {}
        export_vars = _collect_step_runtime_export_vars(
            action=step_item.get("action", ""),
            args=step_args,
            value=step_item.get("value"),
        )
        for key in export_vars:
            clean_key = _clean_text(key)
            if clean_key and clean_key not in predicted_exported_variables:
                # 预检阶段仅预测变量名，运行时再填充值。
                predicted_exported_variables[clean_key] = ""

    pass_count = sum(1 for item in step_checks if item.get("status") == "PASS")
    skip_count = sum(1 for item in step_checks if item.get("status") == "SKIP")
    fail_count = sum(1 for item in step_checks if item.get("status") == "FAIL")
    has_global_fail = any(item.get("status") == "FAIL" for item in global_checks)
    has_runnable_steps = pass_count > 0

    return {
        "case_id": case.id,
        "device_serial": device_serial,
        "platform": platform,
        "ok": (not has_global_fail) and fail_count == 0 and has_runnable_steps,
        "has_runnable_steps": has_runnable_steps,
        "summary": {
            "total": len(step_checks),
            "pass": pass_count,
            "skip": skip_count,
            "fail": fail_count,
            "global_fail": 1 if has_global_fail else 0,
        },
        "global_checks": global_checks,
        "steps": step_checks,
        "exported_variables": predicted_exported_variables,
    }


def resolve_ios_wda_url(session: Session, device_serial: str) -> str:
    """Resolve WDA URL with precedence: scoped -> map -> global -> auto-relay."""
    def _maybe_ensure_local_relay(url: str) -> str:
        candidate = str(url or "").strip()
        if not candidate:
            return candidate
        try:
            parsed = urlparse(candidate)
            host = (parsed.hostname or "").strip().lower()
            if host in ("127.0.0.1", "localhost"):
                preferred_port = int(parsed.port) if parsed.port else None
                actual_port = wda_relay_manager.ensure_relay(
                    device_serial,
                    preferred_port=preferred_port,
                )
                if preferred_port != actual_port or parsed.port is None:
                    updated = parsed._replace(netloc=f"{host}:{actual_port}")
                    return urlunparse(updated)
        except Exception as exc:
            logger.warning(
                "failed to ensure local WDA relay for device=%s url=%s error=%s",
                device_serial,
                candidate,
                exc,
            )
        return candidate

    scoped_key = f"ios_wda_url.{device_serial}"
    scoped = get_setting_value(session, scoped_key)
    if scoped:
        return _maybe_ensure_local_relay(scoped)

    mapping_raw = get_setting_value(session, "ios_wda_url_map")
    if mapping_raw:
        try:
            mapping = json.loads(mapping_raw)
            if isinstance(mapping, dict):
                mapped = mapping.get(device_serial)
                if mapped:
                    return _maybe_ensure_local_relay(mapped)
        except Exception:
            pass

    global_url = get_setting_value(session, "ios_wda_url")
    if global_url:
        return _maybe_ensure_local_relay(global_url)

    # 自动分配本地 relay 端口，默认映射到 http://127.0.0.1:{port}
    local_port = wda_relay_manager.ensure_relay(device_serial)
    return f"http://127.0.0.1:{local_port}"


def check_wda_health(wda_url: str) -> None:
    import requests

    status_url = f"{wda_url.rstrip('/')}/status"
    started_at = time.time()
    try:
        resp = requests.get(status_url, timeout=5)
        resp.raise_for_status()
        logger.info(
            "WDA health check passed: url=%s status=%s duration=%.3fs",
            status_url,
            resp.status_code,
            time.time() - started_at,
        )
    except Exception as exc:
        logger.warning(
            "WDA health check failed: url=%s duration=%.3fs error=%s",
            status_url,
            time.time() - started_at,
            exc,
        )
        raise RuntimeError(
            f"P1005_WDA_UNAVAILABLE: WDA health check failed: {status_url}, error={exc}"
        ) from exc


def restore_device_status_after_execution(
    session: Session,
    device_serial: str,
    *,
    only_if_busy: bool = True,
) -> Optional[str]:
    """Restore one device's status after case/scenario execution finishes."""
    device = session.exec(select(Device).where(Device.serial == device_serial)).first()
    if not device:
        logger.warning("restore device status skipped: device not found serial=%s", device_serial)
        return None

    if only_if_busy and str(device.status or "").strip().upper() != "BUSY":
        logger.info(
            "restore device status skipped: serial=%s current_status=%s only_if_busy=%s",
            device_serial,
            device.status,
            only_if_busy,
        )
        return str(device.status or "").strip().upper() or None

    platform = str(device.platform or "android").strip().lower() or "android"
    target_status = "IDLE"
    wda_url = None

    if platform == "ios":
        try:
            wda_url = resolve_ios_wda_url(session, device_serial)
            check_wda_health(wda_url)
        except Exception as exc:
            logger.warning(
                "restore iOS device status fallback to WDA_DOWN: serial=%s wda_url=%s error=%s",
                device_serial,
                wda_url,
                exc,
            )
            target_status = "WDA_DOWN"

    device.status = target_status
    device.updated_at = datetime.now()
    session.add(device)
    session.commit()

    logger.info(
        "device status restored after execution: serial=%s platform=%s status=%s wda_url=%s",
        device_serial,
        platform,
        target_status,
        wda_url,
    )
    return target_status


def run_case_with_standard_runner(
    session: Session,
    case: TestCase,
    device_serial: str,
    env_id: Optional[int] = None,
    variables_map: Optional[Dict[str, str]] = None,
    abort_event=None,
) -> Dict[str, Any]:
    """
    Execute one case on target device with cross-platform runner.

    Returns a result with fields:
    - success
    - platform
    - device_id
    - steps
    - exported_variables
    """
    platform = resolve_device_platform(session, device_serial)
    driver_kwargs: Dict[str, Any] = {}

    if platform == "ios":
        if not is_flag_enabled(session, FLAG_IOS_EXECUTION, default=False):
            raise RuntimeError("iOS execution is disabled by feature flag")
        wda_url = resolve_ios_wda_url(session, device_serial)
        check_wda_health(wda_url)
        driver_kwargs["wda_url"] = wda_url

    prepared_steps, exported_variables = prepare_case_steps_for_platform(
        session=session,
        case=case,
        platform=platform,
        env_id=env_id,
        variables_map=variables_map,
    )

    from backend.drivers.cross_platform_runner import TestCaseRunner as CrossPlatformRunner

    runner = CrossPlatformRunner(
        platform=platform,
        device_id=device_serial,
        abort_event=abort_event,
        **driver_kwargs,
    )
    try:
        result = runner.run_all(prepared_steps)
    finally:
        runner.disconnect()

    runtime_variables = result.get("runtime_variables")
    if isinstance(runtime_variables, dict):
        for key, value in runtime_variables.items():
            if key:
                exported_variables[str(key)] = "" if value is None else str(value)
    result["exported_variables"] = exported_variables
    return result
