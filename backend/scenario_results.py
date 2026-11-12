"""
场景结果转换 / 持久化 / 聚合

从 backend.api.scenarios 拆出的结果处理链路（无路由、无调度）：
- 跨端结果 -> legacy 用例结果转换（_convert_cross_result_to_legacy_case_result）
- 步骤截图与 TestResult 持久化（_persist_case_result_and_build_case_report 及辅助）
- 用例/场景状态聚合与汇总消息（_summarize_cases_results 等）

执行编排见 backend.scenario_execution，路由端点见 backend.api.scenarios。
"""
import base64
import io
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from sqlmodel import Session

from backend.models import TestCase, TestResult
from backend.report_display import (
    build_report_display,
    is_ignored_skip,
    normalize_step_payload_for_report,
    step_skip_reason,
    storage_report_display,
)
from backend.step_contract import normalize_error_strategy, standard_step_to_legacy

logger = logging.getLogger(__name__)


def _step_error_strategy(step_result: Any) -> str:
    """步骤容错策略（步骤结果顶层或 step 定义内，非法值归 ABORT）。"""
    if not isinstance(step_result, dict):
        return "ABORT"
    raw = step_result.get("error_strategy")
    if not raw and isinstance(step_result.get("step"), dict):
        raw = step_result["step"].get("error_strategy")
    try:
        return normalize_error_strategy(raw)
    except Exception:
        return "ABORT"


def _step_is_success(step_result: Dict[str, Any]) -> bool:
    if step_result.get("success") is True:
        return True
    return str(step_result.get("status") or "").strip().upper() in ("PASS", "SUCCESS")


def _step_is_skip_record(step_result: Dict[str, Any]) -> bool:
    if step_result.get("is_skipped"):
        return True
    return str(step_result.get("status") or "").strip().upper() in ("SKIP", "SKIPPED")


def _step_has_failure(step_result: Dict[str, Any]) -> bool:
    """步骤是否带失败信号：真实失败，或 SKIP 记录里仍留有 error（如 IGNORE 失败降级）。"""
    if is_ignored_skip(step_result):
        return True
    if _step_is_success(step_result) and not step_result.get("error"):
        return False
    if _step_is_skip_record(step_result):
        return bool(step_result.get("error"))
    return True


def is_tolerated_failure(step_result: Any) -> bool:
    """该步骤的失败是否已被 error_strategy=IGNORE 容忍。

    被容忍的失败步骤结果为 SKIP，结论不计入 has_warning / has_fail，
    但 error / error_code / error_context / suggestion / 截图仍作为详情留痕保留。
    """
    if not isinstance(step_result, dict):
        return False
    if is_ignored_skip(step_result):
        return True
    if not _step_has_failure(step_result):
        return False
    return _step_error_strategy(step_result) == "IGNORE"


def _case_failures_all_ignored(case_result: Dict[str, Any]) -> bool:
    """用例的失败是否全部被 IGNORE 容忍（另有成功步骤不影响判定）。"""
    steps = [step for step in (case_result.get("steps") or []) if isinstance(step, dict)]
    failures = [step for step in steps if _step_has_failure(step)]
    return bool(failures) and all(is_tolerated_failure(step) for step in failures)


def _step_ui_status(step_result: Dict[str, Any]) -> str:
    """Normalize step result to UI status: success/warning/skipped/failed."""
    if is_tolerated_failure(step_result):
        return "skipped"
    if step_result.get("is_warning"):
        return "warning"
    if step_result.get("is_skipped"):
        return "skipped"
    if step_result.get("success"):
        return "success"
    return "failed"


def _safe_attempts(value: Any) -> int:
    """解析步骤总尝试次数（见 cross_platform_runner.run_step，非法值归 0）。"""
    try:
        return int(value or 0)
    except Exception:
        return 0


def _summarize_scenario_raw_results(raw_results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Aggregate scenario status from raw case results.

    Returns:
      {
        "status": "PASS" | "WARNING" | "FAIL",
        "all_skipped": bool,
        "has_fail": bool,
        "has_warning": bool,
        "has_pass": bool,
        "has_skip": bool,
        "total_steps": int,
      }
    """
    has_fail = False
    has_warning = False
    has_pass = False
    has_skip = False
    total_steps = 0

    for item in raw_results or []:
        case_res = item.get("result", {}) if isinstance(item, dict) else {}
        case_steps = case_res.get("steps", []) if isinstance(case_res, dict) else []
        if case_res.get("is_warning") and not _case_failures_all_ignored(case_res):
            has_warning = True

        if not case_steps and case_res.get("success") is False:
            has_fail = True

        for step in case_steps or []:
            total_steps += 1
            ui_status = _step_ui_status(step)
            if ui_status == "failed":
                has_fail = True
            elif ui_status == "warning":
                has_warning = True
            elif ui_status == "skipped":
                has_skip = True
            elif ui_status == "success":
                has_pass = True

    all_skipped = total_steps > 0 and has_skip and not has_pass and not has_warning and not has_fail

    if has_fail:
        status = "FAIL"
    elif has_warning or all_skipped:
        status = "WARNING"
    else:
        status = "PASS"

    return {
        "status": status,
        "all_skipped": all_skipped,
        "has_fail": has_fail,
        "has_warning": has_warning,
        "has_pass": has_pass,
        "has_skip": has_skip,
        "total_steps": total_steps,
    }


def _get_reports_root_dir() -> str:
    current_file = os.path.abspath(__file__)
    project_root = os.path.dirname(os.path.dirname(current_file))
    return os.path.join(project_root, "reports")


def _persist_step_screenshot(
    execution_id: int,
    step_order: int,
    screenshot_b64: Optional[str],
) -> Optional[str]:
    payload = str(screenshot_b64 or "").strip()
    if not payload:
        return None
    if payload.lower().startswith("data:") and "," in payload:
        payload = payload.split(",", 1)[1].strip()

    try:
        raw_png = base64.b64decode(payload)
        if not raw_png:
            return None
    except Exception:
        return None

    try:
        reports_dir = os.path.join(_get_reports_root_dir(), "screenshots")
        os.makedirs(reports_dir, exist_ok=True)
        filename = f"exec_{execution_id}_step_{step_order}.png"
        full_path = os.path.join(reports_dir, filename)
        with open(full_path, "wb") as fp:
            fp.write(raw_png)
        return f"screenshots/{filename}"
    except Exception:
        return None


def _determine_case_status(
    formatted_steps: List[Dict[str, Any]],
    *,
    case_success: bool,
    case_is_warning: bool = False,
) -> str:
    steps = list(formatted_steps or [])
    # 结论只由"未被容忍的失败"决定：IGNORE 容忍的失败不计入失败/警告。
    counted = [step for step in steps if not is_tolerated_failure(step)]
    tolerated_failure_only = bool(steps) and len(counted) < len(steps) and not any(
        step.get("status") in ("failed", "warning") for step in counted
    )

    case_has_failed = any(step.get("status") == "failed" for step in counted)
    case_has_warning = any(step.get("status") == "warning" for step in counted)
    # 全 SKIP 例外：含被 IGNORE 全部忽略的情形，仍归 skipped（没真正验证到东西）。
    case_all_skipped = bool(steps) and all(step.get("status") == "skipped" for step in steps)

    if case_has_failed:
        return "failed"
    if case_all_skipped:
        return "skipped"
    if case_has_warning or (case_is_warning and not tolerated_failure_only):
        return "warning"
    if case_success or tolerated_failure_only:
        return "success"
    return "failed"


def _find_last_failed_step_name(
    cases_results: List[Dict[str, Any]],
    *,
    all_skipped: bool = False,
) -> Optional[str]:
    if all_skipped:
        return "全部步骤均跳过（平台不匹配或未配置）"

    for item in cases_results or []:
        if item.get("status") != "failed":
            continue
        for step in item.get("steps", []) or []:
            if step.get("status") != "failed":
                continue
            display = step.get("report_display") if isinstance(step.get("report_display"), dict) else {}
            step_desc = (
                display.get("display_text")
                or step.get("description")
                or step.get("selector")
                or step.get("action")
                or "未知操作"
            )
            case_name = item.get("alias") or item.get("case_name", "未知用例")
            return f"[{case_name}] {step_desc}"
    return None


def _build_scenario_summary_message(
    *,
    total_duration: float,
    success_count: int,
    warning_count: int,
    skipped_count: int,
    fail_count: int,
) -> str:
    return (
        f"🏁 执行结束: 总耗时 {total_duration:.2f}s | 通过 {success_count} | 警告 {warning_count} | "
        f"跳过 {skipped_count} | 失败 {fail_count}"
    )


def _ui_status_to_db_status(ui_status: str) -> str:
    if ui_status == "warning":
        return "WARNING"
    if ui_status == "skipped":
        return "SKIP"
    if ui_status == "success":
        return "PASS"
    return "FAIL"


def _encode_case_error_screenshot_base64(
    error_screenshot: Any,
    *,
    execution_id: int,
    step_order: int,
) -> Optional[str]:
    if error_screenshot is None:
        return None

    try:
        buffered = io.BytesIO()
        error_screenshot.save(buffered, format="PNG")
        return base64.b64encode(buffered.getvalue()).decode("utf-8")
    except Exception as exc:
        logger.warning(
            "scenario case-level screenshot encode failed: execution_id=%s step_order=%s error=%s",
            execution_id,
            step_order,
            exc,
        )
        return None


def _persist_case_result_and_build_case_report(
    *,
    session: Session,
    execution_id: int,
    item: Dict[str, Any],
    case_result: Dict[str, Any],
    global_step_order: int,
    step_name_prefix: Optional[str] = None,
    include_case_duration: bool = False,
    case_level_error_screenshot: Any = None,
    commit_per_step: bool = False,
) -> Tuple[Dict[str, Any], int, float]:
    formatted_steps: List[Dict[str, Any]] = []
    case_duration = 0.0
    error_screenshot = case_level_error_screenshot
    prefix = step_name_prefix or item.get("alias") or item.get("case_name") or "Unknown"

    for step_result in case_result.get("steps", []) or []:
        step_payload = normalize_step_payload_for_report(step_result.get("step", {}) or {})
        step_output = step_result.get("output") if isinstance(step_result.get("output"), dict) else None
        display_payload = dict(step_payload)
        if step_output:
            display_payload["output"] = step_output
        # 结构化错误信息（纯增量）：随 report_display 持久化，避免 TestResult 加列迁移。
        error_code = str(step_result.get("error_code") or "").strip()
        error_suggestion = str(step_result.get("suggestion") or "").strip()
        if error_code:
            display_payload["error_code"] = error_code
        if error_suggestion:
            display_payload["suggestion"] = error_suggestion
        error_context = step_result.get("error_context")
        if isinstance(error_context, dict) and error_context:
            display_payload["error_context"] = error_context
        # 跳过原因（纯增量）：IGNORE 失败 / 平台跳过 / 中止补记的说明，随 report_display 持久化。
        skip_reason = step_skip_reason(step_result)
        if skip_reason:
            display_payload["skip_reason"] = skip_reason
        # 失败重试次数（纯增量）：attempts>1 时随 report_display 持久化，供报告/Flaky 统计。
        attempts = _safe_attempts(step_result.get("attempts"))
        if attempts > 1:
            display_payload["attempts"] = attempts
        step_duration = float(step_result.get("duration", 0) or 0)
        case_duration += step_duration

        ui_status = _step_ui_status(step_result)
        db_status = _ui_status_to_db_status(ui_status)

        screenshot_b64 = str(step_result.get("screenshot") or "").strip() or None
        if not step_result.get("success") and not screenshot_b64 and error_screenshot is not None:
            screenshot_b64 = _encode_case_error_screenshot_base64(
                error_screenshot,
                execution_id=execution_id,
                step_order=global_step_order,
            )
            if screenshot_b64:
                error_screenshot = None

        screenshot_path = None
        if screenshot_b64:
            screenshot_path = _persist_step_screenshot(
                execution_id=execution_id,
                step_order=global_step_order,
                screenshot_b64=screenshot_b64,
            )

        report_display = build_report_display(
            display_payload,
            screenshot_base64=screenshot_b64,
            screenshot_path=screenshot_path,
            include_preview_base64=True,
        )
        step_desc = report_display.get("display_text") or str(step_payload.get("action") or "未知操作")

        test_result = TestResult(
            execution_id=execution_id,
            step_name=f"[{prefix}] {step_desc}",
            step_order=global_step_order,
            status=db_status,
            duration=step_duration * 1000,
            error_message=step_result.get("error"),
            screenshot_path=screenshot_path,
            report_display=storage_report_display(report_display),
        )
        session.add(test_result)
        if commit_per_step:
            session.commit()

        step_entry = {
            **step_payload,
            "status": ui_status,
            "duration": round(step_duration, 2),
            "error": step_result.get("error"),
            "report_display": report_display,
        }
        if attempts > 1:
            step_entry["attempts"] = attempts
        if step_output:
            step_entry["output"] = step_output
        if skip_reason:
            step_entry["skip_reason"] = skip_reason
        if screenshot_b64:
            step_entry["screenshot"] = screenshot_b64
        formatted_steps.append(step_entry)
        global_step_order += 1

    case_status = _determine_case_status(
        formatted_steps,
        case_success=bool(case_result.get("success")),
        case_is_warning=bool(case_result.get("is_warning"))
        and not _case_failures_all_ignored(case_result),
    )
    case_entry = {
        "case_id": case_result.get("case_id"),
        "case_name": item.get("case_name"),
        "alias": item.get("alias"),
        "status": case_status,
        "steps": formatted_steps,
    }
    if include_case_duration:
        case_entry["duration"] = round(case_duration, 2)

    return case_entry, global_step_order, case_duration


def _count_case_statuses(cases_results: List[Dict[str, Any]]) -> Dict[str, int]:
    counts = {
        "success_count": 0,
        "warning_count": 0,
        "skipped_count": 0,
        "fail_count": 0,
    }

    for case_entry in cases_results or []:
        status = case_entry.get("status")
        if status == "success":
            counts["success_count"] += 1
        elif status == "warning":
            counts["warning_count"] += 1
        elif status == "skipped":
            counts["skipped_count"] += 1
        else:
            counts["fail_count"] += 1

    return counts


def _build_synthetic_case_result(
    *,
    case_id: Optional[int],
    error_message: str,
    description: str,
    exported_variables: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "case_id": case_id,
        "success": False,
        "steps": [
            {
                "step": {
                    "action": "system",
                    "selector": None,
                    "selector_type": None,
                    "value": None,
                    "options": {},
                    "description": description,
                    "error_strategy": "ABORT",
                    "timeout": 1,
                },
                "success": False,
                "error": error_message,
                "duration": 0,
            }
        ],
        "exported_variables": dict(exported_variables or {}),
    }


def _normalize_raw_case_result_item(item: Dict[str, Any]) -> Dict[str, Any]:
    normalized = dict(item or {})
    case_result = normalized.get("result")
    if isinstance(case_result, dict):
        return normalized

    error_message = str(normalized.get("error") or "legacy scenario execution failed")
    normalized["case_name"] = normalized.get("case_name") or "Unknown"
    normalized["result"] = _build_synthetic_case_result(
        case_id=normalized.get("case_id"),
        error_message=error_message,
        description=error_message,
    )
    return normalized


def _build_cases_results_from_raw_results(
    *,
    session: Session,
    execution_id: int,
    raw_results: List[Dict[str, Any]],
    include_case_duration: bool = True,
    commit_per_step: bool = False,
) -> List[Dict[str, Any]]:
    cases_results: List[Dict[str, Any]] = []
    global_step_order = 1

    for raw_item in raw_results or []:
        item = _normalize_raw_case_result_item(raw_item)
        case_res = item.get("result", {}) or {}
        case_entry, global_step_order, _ = _persist_case_result_and_build_case_report(
            session=session,
            execution_id=execution_id,
            item=item,
            case_result=case_res,
            global_step_order=global_step_order,
            step_name_prefix=item.get("alias") or item.get("case_name"),
            include_case_duration=include_case_duration,
            case_level_error_screenshot=case_res.get("last_error_screenshot"),
            commit_per_step=commit_per_step,
        )
        cases_results.append(case_entry)

    return cases_results


def _summarize_cases_results(cases_results: List[Dict[str, Any]]) -> Dict[str, Any]:
    counts = _count_case_statuses(cases_results)
    total_cases = len(cases_results or [])
    all_skipped = total_cases > 0 and counts["skipped_count"] == total_cases

    if counts["fail_count"] > 0:
        scenario_status = "FAIL"
    elif counts["warning_count"] > 0 or all_skipped:
        scenario_status = "WARNING"
    else:
        scenario_status = "PASS"

    return {
        "scenario_status": scenario_status,
        "all_skipped": all_skipped,
        "last_failed_step_name": _find_last_failed_step_name(
            cases_results,
            all_skipped=all_skipped,
        ),
        **counts,
    }


def _to_legacy_step_dict(step_data: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return standard_step_to_legacy(step_data)
    except Exception:
        # 回退到最小字段，避免报告链路崩溃
        return {
            "action": step_data.get("action"),
            "selector": None,
            "selector_type": None,
            "value": step_data.get("value"),
            "options": {},
            "description": step_data.get("description"),
            "timeout": step_data.get("timeout", 10),
            "error_strategy": step_data.get("error_strategy", "ABORT"),
        }


def _convert_cross_result_to_legacy_case_result(
    case: TestCase,
    cross_result: Dict[str, Any],
    variables_map: Dict[str, str],
) -> Dict[str, Any]:
    converted_steps: List[Dict[str, Any]] = []
    has_warning = False
    exported_variables: Dict[str, str] = {
        str(key): "" if value is None else str(value)
        for key, value in dict(variables_map or {}).items()
        if str(key).strip()
    }
    runtime_exports = cross_result.get("exported_variables")
    if isinstance(runtime_exports, dict):
        for key, value in runtime_exports.items():
            clean_key = str(key).strip()
            if clean_key:
                exported_variables[clean_key] = "" if value is None else str(value)

    for step_item in cross_result.get("steps", []):
        status = str(step_item.get("status") or "").upper()
        step_data = step_item.get("step") or {}
        legacy_step = _to_legacy_step_dict(step_data)

        success = status in ("PASS", "SKIP")
        converted = {
            "step": legacy_step,
            "success": success,
            "duration": step_item.get("duration", 0),
        }
        if step_item.get("error"):
            converted["error"] = step_item.get("error")
        if isinstance(step_item.get("output"), dict):
            converted["output"] = step_item.get("output")
        if step_item.get("screenshot"):
            converted["screenshot"] = step_item.get("screenshot")
        # 结构化错误信息与跳过原因（纯增量字段）：失败被容忍后仍需作为详情留痕保留。
        for field in ("error_code", "error_context", "suggestion"):
            value = step_item.get(field)
            if value not in (None, "", {}):
                converted[field] = value
        skip_reason = step_skip_reason(step_item)
        if skip_reason:
            converted["skip_reason"] = skip_reason
        # 失败重试次数（纯增量字段，见 cross_platform_runner.run_step）。
        attempts = _safe_attempts(step_item.get("attempts"))
        if attempts > 1:
            converted["attempts"] = attempts
        if is_tolerated_failure(converted):
            # IGNORE 失败：行状态为 SKIP（详情留痕保留），不再计入警告/失败。
            converted["is_skipped"] = True
        elif status == "WARNING":
            converted["success"] = False
            converted["is_warning"] = True
            has_warning = True
        if status == "SKIP":
            converted["is_skipped"] = True

        converted_steps.append(converted)

    result = {
        "case_id": case.id,
        "success": bool(cross_result.get("success")),
        "steps": converted_steps,
        "exported_variables": exported_variables,
    }
    if has_warning and result["success"]:
        result["is_warning"] = True
    return result
