"""标准步骤 -> AutoDroid 脚本步骤编译。

导出产物是"数据驱动"的：每个用例编译为一份 JSON 步骤清单
（cases/case_<id>.js 的 module.exports），设备端 runtime.js 解释执行。
本模块只做确定性数据转换，不做任何设备交互。

与 PC 执行链路的保真约定（对应 backend/drivers/android_driver.py）：
- click/wait/input 的定位候选链按 resolve_locator_candidates 顺序下发，
  设备端逐个尝试；text 类候选先精确匹配再 textContains 降级。
- 变量占位符（{{KEY}}）在导出时用已知变量渲染，未解析项由 precheck 提示。
- start_app/stop_app 的 app_key 在导出时解析为包名（load_app_key_mapping）。
- 图像动作的 image_path 归一化为脚本包内 assets/<basename> 引用。
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from sqlmodel import Session, select

from backend.locator_resolution import resolve_locator_candidates
from backend.models import GlobalVariable, TestCase, TestCaseStep
from backend.step_contract import (
    build_standard_from_legacy_steps,
    normalize_action,
    normalize_error_strategy,
    normalize_execute_on,
)
from backend.utils.pydantic_compat import dump_model
from backend.utils.variable_render import render_step_data

logger = logging.getLogger(__name__)

SCRIPT_FORMAT_VERSION = 1
LOCATOR_ACTIONS = {"click", "wait_until_exists"}
IMAGE_TEMPLATE_ACTIONS = {"click_image", "assert_image"}
_UNRESOLVED_VAR_PATTERN = re.compile(r"{{\s*([A-Z0-9_]+)\s*}}")
_REGION_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")

# 各动作的设备端默认定位超时（秒），对齐 PC 侧 driver 行为
CLICK_TIMEOUT_SECONDS = 5
INPUT_TIMEOUT_SECONDS = 5


def collect_case_variables(
    case: TestCase,
    extra_variables: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """用例变量 + 环境变量合并（用例变量优先）。"""
    merged: Dict[str, str] = {}
    for key, value in dict(extra_variables or {}).items():
        clean_key = str(key or "").strip()
        if clean_key:
            merged[clean_key] = "" if value is None else str(value)
    for variable in case.variables or []:
        raw = dump_model(variable)
        if not isinstance(raw, dict):
            continue
        key = str(raw.get("key") or "").strip()
        if key:
            merged[key] = "" if raw.get("value") is None else str(raw.get("value"))
    return merged


def load_env_variables(session: Session, env_id: Optional[int]) -> Dict[str, str]:
    """环境变量库（GlobalVariable）读取；env_id 为空返回空表。"""
    if not env_id:
        return {}
    rows = session.exec(
        select(GlobalVariable).where(GlobalVariable.env_id == env_id)
    ).all()
    result: Dict[str, str] = {}
    for row in rows:
        if row.key:
            result[str(row.key)] = "" if row.value is None else str(row.value)
    return result


def load_case_steps(session: Session, case: TestCase) -> List[Dict[str, Any]]:
    """标准步骤优先；无标准行时从 legacy JSON 构建（与平台读取顺序一致）。"""
    rows = session.exec(
        select(TestCaseStep)
        .where(TestCaseStep.case_id == case.id)
        .order_by(TestCaseStep.order, TestCaseStep.id)
    ).all()
    if rows:
        return [dump_model(row) for row in rows]
    legacy_steps = [dump_model(step) for step in (case.steps or [])]
    if not legacy_steps:
        return []
    return build_standard_from_legacy_steps(legacy_steps, case_id=case.id)


def _render_text(value: Any, variables: Dict[str, str]) -> Any:
    if isinstance(value, str):
        return render_step_data(value, variables)
    if isinstance(value, dict):
        return {key: _render_text(item, variables) for key, item in value.items()}
    if isinstance(value, list):
        return [_render_text(item, variables) for item in value]
    return value


def _parse_region(raw: Any) -> Optional[List[float]]:
    """解析 [x1,y1,x2,y2]（绝对像素或 0-1 相对坐标），失败返回 None。"""
    if isinstance(raw, (list, tuple)) and len(raw) == 4:
        try:
            return [float(item) for item in raw]
        except Exception:
            return None
    numbers = _REGION_NUMBER_RE.findall(str(raw or ""))
    if len(numbers) != 4:
        return None
    try:
        return [float(item) for item in numbers]
    except Exception:
        return None


def _resolve_image_asset(image_path: Any) -> Optional[Tuple[str, str]]:
    """image_path -> (asset_name, 相对路径)。使用平台模板图归一化规则。"""
    from backend.api.cases import _normalize_auto_template_image_path

    normalized = _normalize_auto_template_image_path(image_path)
    if not normalized:
        return None
    return os.path.basename(normalized), normalized


def _resolve_app_package(
    app_key: str,
    app_key_map: Dict[str, Dict[str, str]],
    platform: str = "android",
) -> Optional[str]:
    """app_key -> 平台应用标识（Android 包名 / iOS bundle id）；未映射时兼容直接把 app_key 当标识。"""
    key = str(app_key or "").strip()
    if not key:
        return None
    mapped = (app_key_map or {}).get(key) or {}
    package = str(mapped.get(platform) or "").strip()
    if package:
        return package
    if "." in key and " " not in key:
        return key
    return None


def _extract_app_key(step: Dict[str, Any]) -> str:
    args = step.get("args") if isinstance(step.get("args"), dict) else {}
    value = step.get("value")
    for candidate in (args.get("app_key"), args.get("app_id"), value):
        text = str(candidate or "").strip()
        if text:
            return text
    return ""


def build_script_step(
    step: Dict[str, Any],
    *,
    variables: Dict[str, str],
    app_key_map: Dict[str, Dict[str, str]],
    platform: str = "android",
) -> Tuple[Dict[str, Any], List[Tuple[str, str]]]:
    """编译单个标准步骤。返回 (script_step, 引用的模板图列表)。

    platform 决定定位器候选与应用标识的取值（android / ios 在步骤模型里分别维护）。
    """
    action = normalize_action(step.get("action"))
    args = step.get("args") if isinstance(step.get("args"), dict) else {}
    args = _render_text(args, variables)
    template_refs: List[Tuple[str, str]] = []

    script_step: Dict[str, Any] = {
        "order": int(step.get("order") or 0),
        "action": action,
        "description": step.get("description"),
        "timeout": int(step.get("timeout") or 10),
        "error_strategy": normalize_error_strategy(step.get("error_strategy", "ABORT")),
        "retry_count": int(step.get("retry_count") or 0),
        "execute_on": normalize_execute_on(step.get("execute_on")),
        "locators": [],
        "args": {},
    }

    if action in LOCATOR_ACTIONS:
        candidates = resolve_locator_candidates(step, platform)
        script_step["locators"] = [
            {"selector": str(item.get("selector")), "by": str(item.get("by")).lower()}
            for item in candidates
        ]

    if action == "input":
        script_step["args"] = {"text": "" if args.get("text") is None else str(args.get("text"))}
        candidates = resolve_locator_candidates(step, platform)
        script_step["locators"] = [
            {"selector": str(item.get("selector")), "by": str(item.get("by")).lower()}
            for item in candidates
        ]
    elif action == "assert_text":
        match_mode = "not_contains" if str(args.get("match_mode") or "").strip().lower() == "not_contains" else "contains"
        script_step["args"] = {
            "expected_text": "" if args.get("expected_text") is None else str(args.get("expected_text")),
            "match_mode": match_mode,
        }
    elif action in IMAGE_TEMPLATE_ACTIONS:
        image_path = args.get("image_path") or args.get("path") or step.get("value")
        asset = _resolve_image_asset(image_path)
        if asset:
            asset_name, rel_path = asset
            template_refs.append((asset_name, rel_path))
        script_step["args"] = {
            "image": asset[0] if asset else None,
            "image_path": str(image_path or ""),
        }
        if action == "assert_image":
            match_mode = "not_exists" if str(args.get("match_mode") or "").strip().lower() == "not_exists" else "exists"
            script_step["args"]["match_mode"] = match_mode
    elif action == "extract_by_ocr":
        region = _parse_region(args.get("region") if args.get("region") is not None else step.get("value"))
        extract_rule = args.get("extract_rule")
        if isinstance(extract_rule, str):
            extract_rule = {"extract_rule": extract_rule}
        if not isinstance(extract_rule, dict):
            extract_rule = {}
        script_step["args"] = {
            "region": region if region is not None else args.get("region"),
            "extract_rule": extract_rule,
            "output_var": str(args.get("output_var") or "").strip() or None,
        }
    elif action == "sleep":
        seconds = args.get("seconds")
        try:
            seconds_value = float(seconds)
        except Exception:
            seconds_value = 1.0
        script_step["args"] = {"seconds": max(0.0, seconds_value)}
    elif action == "swipe":
        direction = str(args.get("direction") or "up").strip().lower()
        script_step["args"] = {"direction": direction}
    elif action in ("start_app", "stop_app"):
        app_key = _extract_app_key(step)
        script_step["args"] = {
            "app_key": app_key or None,
            "package": _resolve_app_package(app_key, app_key_map, platform) if app_key else None,
        }

    return script_step, template_refs


def build_case_script(
    session: Session,
    case: TestCase,
    *,
    env_id: Optional[int] = None,
    extra_variables: Optional[Dict[str, str]] = None,
    app_key_map: Optional[Dict[str, Dict[str, str]]] = None,
    platform: str = "android",
) -> Dict[str, Any]:
    """编译一个用例为脚本清单（含模板图引用列表）。"""
    if app_key_map is None:
        from backend.cross_platform_execution import load_app_key_mapping

        app_key_map = load_app_key_mapping(session)

    env_vars = load_env_variables(session, env_id)
    if extra_variables:
        env_vars.update({str(k): str(v) for k, v in extra_variables.items()})
    variables = collect_case_variables(case, env_vars)

    steps = load_case_steps(session, case)
    script_steps: List[Dict[str, Any]] = []
    template_refs: List[Tuple[str, str]] = []
    for step in steps:
        script_step, refs = build_script_step(
            step,
            variables=variables,
            app_key_map=app_key_map,
            platform=platform,
        )
        script_steps.append(script_step)
        for ref in refs:
            if ref not in template_refs:
                template_refs.append(ref)

    return {
        "format_version": SCRIPT_FORMAT_VERSION,
        "case_id": case.id,
        "name": case.name,
        "variables": variables,
        "steps": script_steps,
        "template_refs": template_refs,
    }


def find_unresolved_placeholders(value: Any) -> List[str]:
    """递归收集仍未解析的 {{KEY}} 占位符（用于导出预检提示）。"""
    found: List[str] = []

    def walk(item: Any) -> None:
        if isinstance(item, str):
            found.extend(_UNRESOLVED_VAR_PATTERN.findall(item))
        elif isinstance(item, dict):
            for child in item.values():
                walk(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                walk(child)

    walk(value)
    return sorted(set(found))
