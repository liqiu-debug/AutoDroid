"""Helpers for consistent step display in reports."""
from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from backend.execution_errors import suggestion_for
from backend.paths import project_path
from backend.step_contract import ACTION_DISPLAY_NAMES
from backend.utils.pydantic_compat import dump_model


TEXT_ASSERT_MATCH_LABELS = {
    "contains": "包含",
    "not_contains": "不包含",
}
IMAGE_ASSERT_MATCH_LABELS = {
    "exists": "存在",
    "not_exists": "不存在",
}
DIRECTION_LABELS = {
    "up": "上滑",
    "down": "下滑",
    "left": "左滑",
    "right": "右滑",
}


def normalize_step_payload_for_report(step: Any) -> Dict[str, Any]:
    data = dump_model(step)
    if isinstance(data, dict):
        return dict(data)
    return {}


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "value"):
        value = value.value
    return str(value).strip()


def _first_text(*values: Any) -> str:
    for value in values:
        text = _clean_text(value)
        if text:
            return text
    return ""


def _object(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _platform_selectors(step: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    overrides = _object(step.get("platform_overrides"))
    for platform in ("android", "ios"):
        selector = overrides.get(platform)
        if isinstance(selector, dict):
            yield selector
    for platform, selector in overrides.items():
        if platform in {"android", "ios"}:
            continue
        if isinstance(selector, dict):
            yield selector


def _locator_value(step: Dict[str, Any]) -> str:
    direct = _clean_text(step.get("selector"))
    if direct:
        return direct
    for selector in _platform_selectors(step):
        value = _clean_text(selector.get("selector"))
        if value:
            return value
    args = _object(step.get("args"))
    return _first_text(args.get("selector"), args.get("locator"))


def _image_path(step: Dict[str, Any]) -> str:
    args = _object(step.get("args"))
    options = _object(step.get("options"))
    candidates: List[Any] = [
        args.get("image_path"),
        args.get("path"),
        options.get("image_path"),
        options.get("path"),
        step.get("selector"),
        step.get("value"),
    ]
    for selector in _platform_selectors(step):
        if _clean_text(selector.get("by")).lower() == "image":
            candidates.append(selector.get("selector"))
    return _first_text(*candidates)


def _input_value(step: Dict[str, Any]) -> str:
    args = _object(step.get("args"))
    return _first_text(step.get("value"), args.get("text"))


def _assert_text_value(step: Dict[str, Any]) -> str:
    args = _object(step.get("args"))
    return _first_text(args.get("expected_text"), step.get("value"))


def _assert_text_mode(step: Dict[str, Any]) -> str:
    args = _object(step.get("args"))
    options = _object(step.get("options"))
    mode = _clean_text(args.get("match_mode") or options.get("match_mode")).lower()
    if mode in {"not_contains", "not contain", "not_contains_text", "不包含"}:
        return "not_contains"
    return "contains"


def _assert_image_mode(step: Dict[str, Any]) -> str:
    args = _object(step.get("args"))
    options = _object(step.get("options"))
    mode = _clean_text(args.get("match_mode") or options.get("match_mode")).lower()
    if mode in {"not_exists", "not exist", "not_found", "不存在"}:
        return "not_exists"
    return "exists"


def _swipe_direction(step: Dict[str, Any]) -> str:
    args = _object(step.get("args"))
    direction = _first_text(args.get("direction"), step.get("selector")).lower()
    return DIRECTION_LABELS.get(direction, direction or "-")


def _sleep_seconds(step: Dict[str, Any]) -> str:
    args = _object(step.get("args"))
    return _first_text(args.get("seconds"), step.get("value"), step.get("selector"))


def _app_param(step: Dict[str, Any]) -> str:
    args = _object(step.get("args"))
    return _first_text(args.get("app_key"), args.get("app_id"), step.get("selector"), step.get("value"))


def _ocr_variable(step: Dict[str, Any]) -> str:
    args = _object(step.get("args"))
    return _first_text(args.get("output_var"), args.get("export_var"), step.get("value"))


def _ocr_result(step: Dict[str, Any]) -> str:
    output = _object(step.get("output"))
    return _first_text(
        output.get("export_value"),
        output.get("ocr_value"),
        output.get("value"),
        output.get("text"),
        step.get("extracted_text"),
        step.get("ocr_value"),
    )


def _normalize_project_asset_path(raw_path: Any) -> str:
    text = _clean_text(raw_path).replace("\\", "/")
    if not text:
        return ""
    if text.startswith("./"):
        text = text[2:]
    if text.startswith("/static/"):
        text = text[1:]
    if text.startswith("static/"):
        return text
    return text


def _image_base64_payload(value: Any) -> str:
    text = _clean_text(value)
    if text.lower().startswith("data:") and "," in text:
        return text.split(",", 1)[1].strip()
    return text


def _read_image_base64(raw_path: Any) -> Optional[str]:
    normalized = _normalize_project_asset_path(raw_path)
    if not normalized:
        return None

    candidate = Path(normalized)
    if candidate.is_absolute():
        image_path = candidate.resolve()
    else:
        image_path = project_path(normalized).resolve()

    try:
        image_path.relative_to(project_path().resolve())
    except ValueError:
        return None

    if not image_path.exists() or not image_path.is_file():
        return None
    try:
        return base64.b64encode(image_path.read_bytes()).decode("utf-8")
    except OSError:
        return None


def _build_preview(
    *,
    display: Dict[str, Any],
    preview_type: str,
    preview_path: Optional[str] = None,
    preview_base64: Optional[str] = None,
    preview_label: str,
) -> None:
    display["preview_type"] = preview_type
    display["preview_label"] = preview_label
    if preview_path:
        display["preview_path"] = preview_path
    if preview_base64:
        display["preview_base64"] = preview_base64


def _merge_error_fields(display: Dict[str, Any], step: Dict[str, Any]) -> None:
    """并入结构化错误信息（error_code/suggestion，纯增量字段）。

    步骤载荷可携带执行期错误码与修复建议（见 cross_platform_runner._result）；
    仅缺 suggestion 时按错误码回填默认建议，便于报告/前端统一渲染。
    """
    error_code = _clean_text(step.get("error_code"))
    suggestion = _clean_text(step.get("suggestion"))
    if error_code:
        display["error_code"] = error_code
        if not suggestion:
            suggestion = suggestion_for(error_code)
    if suggestion:
        display["suggestion"] = suggestion


def build_report_display(
    step: Any,
    *,
    screenshot_base64: Optional[str] = None,
    screenshot_path: Optional[str] = None,
    include_preview_base64: bool = False,
) -> Dict[str, Any]:
    step_data = normalize_step_payload_for_report(step)
    action = _clean_text(step_data.get("action")).lower()
    action_label = ACTION_DISPLAY_NAMES.get(action, action or "未知操作")
    description = _clean_text(step_data.get("description"))

    display: Dict[str, Any] = {
        "display_text": description or action_label,
        "action": action,
        "action_label": action_label,
        "has_custom_description": bool(description),
    }
    _merge_error_fields(display, step_data)

    if description:
        return display

    if action == "assert_text":
        mode = _assert_text_mode(step_data)
        text = _assert_text_value(step_data)
        display["display_text"] = " ".join(
            item for item in [action_label, TEXT_ASSERT_MATCH_LABELS.get(mode, mode), text] if item
        )
    elif action in {"click", "wait_until_exists"}:
        target = _locator_value(step_data)
        display["display_text"] = " ".join(item for item in [action_label, target] if item)
    elif action == "click_image":
        image_path = _image_path(step_data)
        display["display_text"] = action_label
        if image_path:
            normalized_path = _normalize_project_asset_path(image_path)
            preview_base64 = _read_image_base64(image_path) if include_preview_base64 else None
            _build_preview(
                display=display,
                preview_type="template_image",
                preview_path=normalized_path,
                preview_base64=preview_base64,
                preview_label="图像预览",
            )
    elif action == "input":
        value = _input_value(step_data)
        display["display_text"] = " ".join(item for item in [action_label, value] if item)
    elif action == "assert_image":
        mode = _assert_image_mode(step_data)
        image_path = _image_path(step_data)
        display["display_text"] = " ".join(
            item for item in [action_label, IMAGE_ASSERT_MATCH_LABELS.get(mode, mode)] if item
        )
        if image_path:
            normalized_path = _normalize_project_asset_path(image_path)
            preview_base64 = _read_image_base64(image_path) if include_preview_base64 else None
            _build_preview(
                display=display,
                preview_type="template_image",
                preview_path=normalized_path,
                preview_base64=preview_base64,
                preview_label="图像预览",
            )
    elif action == "swipe":
        display["display_text"] = " ".join(item for item in [action_label, _swipe_direction(step_data)] if item)
    elif action == "sleep":
        seconds = _sleep_seconds(step_data)
        suffix = f"{seconds}s" if seconds else ""
        display["display_text"] = " ".join(item for item in [action_label, suffix] if item)
    elif action == "extract_by_ocr":
        variable = _ocr_variable(step_data)
        result = _ocr_result(step_data)
        display["display_text"] = " ".join(item for item in [action_label, variable, result] if item)
    elif action in {"start_app", "stop_app"}:
        display["display_text"] = " ".join(item for item in [action_label, _app_param(step_data)] if item)
    elif action in {"back", "home"}:
        display["display_text"] = action_label
    else:
        fallback = _first_text(_locator_value(step_data), _input_value(step_data))
        display["display_text"] = " ".join(item for item in [action_label, fallback] if item)

    return display


def storage_report_display(display: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(display, dict):
        return None
    cleaned = {
        key: value
        for key, value in display.items()
        if value not in (None, "") and key != "preview_base64"
    }
    return cleaned or None


def _merge_existing_preview_base64(step: Dict[str, Any], display: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(display)
    if result.get("preview_base64"):
        return result

    preview_type = _clean_text(result.get("preview_type"))
    preview_path = result.get("preview_path")
    if preview_type == "template_image" and preview_path:
        preview_base64 = _read_image_base64(preview_path)
        if preview_base64:
            result["preview_base64"] = preview_base64
    elif preview_type == "screenshot" and step.get("screenshot"):
        result["preview_base64"] = _image_base64_payload(step.get("screenshot"))
    return result


def with_report_display(step: Any, *, include_preview_base64: bool = False) -> Dict[str, Any]:
    step_data = normalize_step_payload_for_report(step)
    if include_preview_base64 and step_data.get("screenshot"):
        step_data["screenshot"] = _image_base64_payload(step_data.get("screenshot"))
    existing = step_data.get("report_display")
    if isinstance(existing, dict) and existing.get("display_text"):
        display = dict(existing)
        if include_preview_base64:
            display = _merge_existing_preview_base64(step_data, display)
    else:
        display = build_report_display(
            step_data,
            screenshot_base64=step_data.get("screenshot") if include_preview_base64 else None,
            screenshot_path=step_data.get("screenshot_path"),
            include_preview_base64=include_preview_base64,
        )
    step_data["report_display"] = display
    return step_data


def enrich_steps_for_html(steps: Iterable[Any]) -> List[Dict[str, Any]]:
    return [with_report_display(step, include_preview_base64=True) for step in (steps or [])]


def enrich_cases_for_html(cases_results: Iterable[Any]) -> List[Dict[str, Any]]:
    enriched_cases: List[Dict[str, Any]] = []
    for case in cases_results or []:
        case_data = normalize_step_payload_for_report(case)
        case_data["steps"] = enrich_steps_for_html(case_data.get("steps") or [])
        enriched_cases.append(case_data)
    return enriched_cases
