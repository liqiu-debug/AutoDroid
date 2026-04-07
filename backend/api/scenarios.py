from typing import Any, Dict, List, Optional, Tuple
import logging
import os
import base64
import io
from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks
from sqlmodel import Session, select, func
from datetime import datetime

from backend.database import get_session, engine
from backend.cross_platform_execution import (
    precheck_case_execution,
    restore_device_status_after_execution,
    run_case_with_standard_runner,
)
from backend.feature_flags import FLAG_CROSS_PLATFORM_RUNNER, is_flag_enabled
from backend.models import TestScenario, ScenarioStep, User, TestCase, Step, TestExecution, TestResult, Device
from backend.schemas import (
    PaginatedTestScenarioRead,
    ScenarioRunRequest,
    ScenarioStepCreate,
    ScenarioStepRead,
    TestScenarioCreate,
    TestScenarioRead,
)
from backend.api import deps
from backend.step_contract import normalize_error_strategy, standard_step_to_legacy
import uuid
import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from backend.runner import ScenarioRunner, register_device_abort, trigger_device_abort, unregister_device_abort

router = APIRouter()
logger = logging.getLogger(__name__)


def _resolve_device_meta(
    session: Session,
    serial: Optional[str],
    fallback_display: Optional[str] = None,
) -> Dict[str, Optional[str]]:
    """Resolve structured device metadata for execution records."""
    if not serial:
        return {
            "device_serial": None,
            "platform": None,
            "device_info": fallback_display or None,
        }

    dev = session.exec(select(Device).where(Device.serial == serial)).first()
    if dev:
        display = dev.custom_name or dev.market_name or dev.model or serial
        platform = str(dev.platform or "").strip().lower() or None
        return {
            "device_serial": serial,
            "platform": platform,
            "device_info": display,
        }

    return {
        "device_serial": serial,
        "platform": None,
        "device_info": fallback_display or serial,
    }


def _step_ui_status(step_result: Dict[str, Any]) -> str:
    """Normalize step result to UI status: success/warning/skipped/failed."""
    if step_result.get("is_warning"):
        return "warning"
    if step_result.get("is_skipped"):
        return "skipped"
    if step_result.get("success"):
        return "success"
    return "failed"


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
        if case_res.get("is_warning"):
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


def _summarize_precheck_failure(payload: Dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        return "precheck failed"
    for item in payload.get("global_checks", []) or []:
        if isinstance(item, dict) and item.get("status") == "FAIL":
            return str(item.get("message") or item.get("code") or "global precheck failed")
    for item in payload.get("steps", []) or []:
        if isinstance(item, dict) and item.get("status") == "FAIL":
            return str(item.get("message") or item.get("code") or "step precheck failed")
    if payload.get("has_runnable_steps") is False:
        return "all steps would be skipped on this device"
    return "precheck failed"


def _get_reports_root_dir() -> str:
    current_file = os.path.abspath(__file__)
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(current_file)))
    return os.path.join(project_root, "reports")


def _persist_step_screenshot(
    execution_id: int,
    step_order: int,
    screenshot_b64: Optional[str],
) -> Optional[str]:
    payload = str(screenshot_b64 or "").strip()
    if not payload:
        return None

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
    case_has_failed = any(step.get("status") == "failed" for step in formatted_steps)
    case_has_warning = any(step.get("status") == "warning" for step in formatted_steps)
    case_all_skipped = bool(formatted_steps) and all(
        step.get("status") == "skipped" for step in formatted_steps
    )

    if case_has_failed:
        return "failed"
    if case_all_skipped:
        return "skipped"
    if case_has_warning or case_is_warning:
        return "warning"
    if case_success:
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
            step_desc = (
                step.get("description")
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
        step_payload = step_result.get("step", {}) or {}
        step_duration = float(step_result.get("duration", 0) or 0)
        case_duration += step_duration

        ui_status = _step_ui_status(step_result)
        db_status = _ui_status_to_db_status(ui_status)
        step_desc = step_payload.get("description") or f"{step_payload.get('action')} {step_payload.get('selector') or ''}"

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

        test_result = TestResult(
            execution_id=execution_id,
            step_name=f"[{prefix}] {step_desc}",
            step_order=global_step_order,
            status=db_status,
            duration=step_duration * 1000,
            error_message=step_result.get("error"),
            screenshot_path=screenshot_path,
        )
        session.add(test_result)
        if commit_per_step:
            session.commit()

        step_entry = {
            "status": ui_status,
            "action": step_payload.get("action"),
            "description": step_payload.get("description"),
            "selector": step_payload.get("selector"),
            "selector_type": step_payload.get("selector_type"),
            "duration": round(step_duration, 2),
            "error": step_result.get("error"),
        }
        if screenshot_b64:
            step_entry["screenshot"] = screenshot_b64
        formatted_steps.append(step_entry)
        global_step_order += 1

    case_status = _determine_case_status(
        formatted_steps,
        case_success=bool(case_result.get("success")),
        case_is_warning=bool(case_result.get("is_warning")),
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


def _finalize_scenario_execution(
    *,
    session: Session,
    scenario: TestScenario,
    execution: TestExecution,
    cases_results: List[Dict[str, Any]],
    start_time: datetime,
) -> Dict[str, Any]:
    from backend.report_generator import report_generator

    end_time = datetime.now()
    total_duration = (end_time - start_time).total_seconds()

    report_id = None
    report_error = None
    try:
        report_id = report_generator.generate_scenario_report(
            scenario_id=scenario.id,
            scenario_name=scenario.name,
            cases_results=cases_results,
            start_time=start_time,
            end_time=end_time,
        )
    except Exception as exc:
        report_error = str(exc)
        logger.warning(
            "scenario report generation failed: execution_id=%s scenario_id=%s error=%s",
            execution.id,
            scenario.id,
            exc,
        )

    summary = _summarize_cases_results(cases_results)
    summary_msg = _build_scenario_summary_message(
        total_duration=total_duration,
        success_count=summary["success_count"],
        warning_count=summary["warning_count"],
        skipped_count=summary["skipped_count"],
        fail_count=summary["fail_count"],
    )

    scenario.last_run_status = summary["scenario_status"]
    scenario.last_run_time = end_time
    scenario.last_run_duration = int(total_duration)
    scenario.last_execution_id = execution.id
    scenario.last_executor = execution.executor_name
    scenario.last_failed_step = summary["last_failed_step_name"]
    if report_id:
        scenario.last_report_id = report_id
    session.add(scenario)

    execution.status = summary["scenario_status"]
    execution.end_time = end_time
    execution.duration = total_duration
    if report_id:
        execution.report_id = report_id
    session.add(execution)
    session.commit()

    return {
        "report_id": report_id,
        "report_error": report_error,
        "summary_msg": summary_msg,
        "total_duration": total_duration,
        "end_time": end_time,
        **summary,
    }


def _prepare_cross_platform_device_execution(
    *,
    session: Session,
    execution: TestExecution,
    device_serial: str,
):
    abort_event = register_device_abort(device_serial)
    resolved_meta = _resolve_device_meta(
        session,
        device_serial,
        fallback_display=execution.device_info,
    )

    execution.device_serial = resolved_meta.get("device_serial")
    if resolved_meta.get("platform"):
        execution.platform = resolved_meta.get("platform")
    if resolved_meta.get("device_info"):
        execution.device_info = resolved_meta.get("device_info")

    dev = session.exec(select(Device).where(Device.serial == device_serial)).first()
    if dev:
        dev.status = "BUSY"
        dev.updated_at = datetime.now()
        session.add(dev)

    session.add(execution)
    session.commit()
    return abort_event


def _prepare_legacy_scenario_device_execution(
    *,
    session: Session,
    execution: TestExecution,
    runner: ScenarioRunner,
) -> Tuple[Optional[str], Any]:
    runner.runner.connect()
    driver = runner.runner.d
    device_serial = getattr(driver, "serial", None)
    if not device_serial:
        return None, None

    abort_event = register_device_abort(device_serial)
    runner.abort_event = abort_event
    runner.runner.abort_event = abort_event

    resolved_meta = _resolve_device_meta(
        session,
        device_serial,
        fallback_display=execution.device_info,
    )
    execution.device_serial = resolved_meta.get("device_serial")
    execution.platform = "android"

    dev = session.exec(select(Device).where(Device.serial == device_serial)).first()
    if dev:
        dev.status = "BUSY"
        dev.updated_at = datetime.now()
        execution.platform = str(dev.platform or "").strip().lower() or execution.platform
        name_part = dev.custom_name or dev.market_name or dev.model
        if name_part:
            execution.device_info = name_part
        session.add(dev)
    else:
        runner_info = getattr(driver, "info", None) or {}
        if runner_info:
            execution.device_info = (
                f"{runner_info.get('manufacturer')} {runner_info.get('model')} "
                f"(Android {runner_info.get('version')})"
            )
        elif resolved_meta.get("device_info"):
            execution.device_info = resolved_meta.get("device_info")

    if resolved_meta.get("platform"):
        execution.platform = resolved_meta.get("platform")
    if resolved_meta.get("device_info") and not execution.device_info:
        execution.device_info = resolved_meta.get("device_info")

    session.add(execution)
    session.commit()
    return device_serial, abort_event


def _execute_cross_platform_scenario_core(
    *,
    session: Session,
    scenario: TestScenario,
    execution: TestExecution,
    scenario_id: int,
    device_serial: str,
    start_time: datetime,
    env_id: Optional[int] = None,
    abort_event=None,
    commit_per_step: bool = False,
) -> Dict[str, Any]:
    result = _run_scenario_cross_platform(
        scenario_id=scenario_id,
        session=session,
        device_serial=device_serial,
        env_id=env_id,
        abort_event=abort_event,
    )

    raw_results = result.get("results", [])
    cases_results = _build_cases_results_from_raw_results(
        session=session,
        execution_id=execution.id,
        raw_results=raw_results,
        include_case_duration=True,
        commit_per_step=commit_per_step,
    )
    if not commit_per_step:
        session.commit()

    final_summary = _finalize_scenario_execution(
        session=session,
        scenario=scenario,
        execution=execution,
        cases_results=cases_results,
        start_time=start_time,
    )

    return {
        "result": result,
        "raw_results": raw_results,
        "cases_results": cases_results,
        **final_summary,
    }


@router.post("/", response_model=TestScenarioRead)
def create_scenario(
    scenario: TestScenarioCreate, 
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user)
):
    """Create a new scenario"""
    db_scenario = TestScenario.from_orm(scenario)
    db_scenario.user_id = current_user.id
    db_scenario.updater_id = current_user.id
    db_scenario.created_at = datetime.now()
    session.add(db_scenario)
    session.commit()
    session.refresh(db_scenario)
    return db_scenario

@router.get("/", response_model=PaginatedTestScenarioRead)
def list_scenarios(
    skip: int = 0,
    limit: int = 100,
    keyword: Optional[str] = None,
    session: Session = Depends(get_session)
):
    """List scenarios with pagination and filtering"""
    from sqlalchemy.orm import aliased
    Creator = aliased(User)
    Updater = aliased(User)
    
    query = session.query(TestScenario, Creator.full_name, Creator.username, Updater.full_name, Updater.username)\
        .outerjoin(Creator, TestScenario.user_id == Creator.id)\
        .outerjoin(Updater, TestScenario.updater_id == Updater.id)
    
    if keyword:
        query = query.filter(TestScenario.name.contains(keyword))
        
    count_query = session.query(func.count(TestScenario.id))
    if keyword:
        count_query = count_query.filter(TestScenario.name.contains(keyword))
    total = count_query.scalar()
        
    query = query.order_by(TestScenario.created_at.desc())
    query = query.offset(skip).limit(limit)
    
    results = query.all()
    
    scenario_list = []
    for scenario, c_full, c_user, u_full, u_user in results:
        read_obj = TestScenarioRead.from_orm(scenario)
        read_obj.creator_name = c_full or c_user or "Unknown"
        read_obj.updater_name = u_full or u_user or "Unknown"
        scenario_list.append(read_obj)
        
    return PaginatedTestScenarioRead(total=total, items=scenario_list)

@router.get("/{scenario_id}", response_model=TestScenarioRead)
def get_scenario(scenario_id: int, session: Session = Depends(get_session)):
    """Get a single scenario"""
    scenario = session.get(TestScenario, scenario_id)
    if not scenario:
        raise HTTPException(status_code=404, detail="Scenario not found")
    return scenario

@router.put("/{scenario_id}", response_model=TestScenarioRead)
def update_scenario(
    scenario_id: int, 
    scenario: TestScenarioCreate, 
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user)
):
    """Update scenario details"""
    db_scenario = session.get(TestScenario, scenario_id)
    if not db_scenario:
        raise HTTPException(status_code=404, detail="Scenario not found")
    
    db_scenario.name = scenario.name
    if scenario.description is not None:
        db_scenario.description = scenario.description
        
    db_scenario.updater_id = current_user.id
    db_scenario.updated_at = datetime.now()
        
    session.add(db_scenario)
    session.commit()
    session.refresh(db_scenario)
    return db_scenario

@router.delete("/{scenario_id}")
def delete_scenario(scenario_id: int, session: Session = Depends(get_session)):
    """Delete a scenario"""
    scenario = session.get(TestScenario, scenario_id)
    if not scenario:
        raise HTTPException(status_code=404, detail="Scenario not found")
    
    # Cascade delete steps
    steps = session.exec(select(ScenarioStep).where(ScenarioStep.scenario_id == scenario_id)).all()
    for s in steps:
        session.delete(s)
        
    session.delete(scenario)
    session.commit()
    return {"message": "Scenario deleted", "id": scenario_id}

# ---- Steps Management ----

@router.get("/{scenario_id}/steps", response_model=List[ScenarioStepRead])
def get_scenario_steps(scenario_id: int, session: Session = Depends(get_session)):
    """Get steps for a scenario"""
    steps = session.exec(select(ScenarioStep).where(ScenarioStep.scenario_id == scenario_id).order_by(ScenarioStep.order)).all()
    return steps

@router.post("/{scenario_id}/steps")
def update_scenario_steps(
    scenario_id: int, 
    steps: List[ScenarioStepCreate], 
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user)
):
    """Replace all steps in a scenario"""
    scenario = session.get(TestScenario, scenario_id)
    if not scenario:
        raise HTTPException(status_code=404, detail="Scenario not found")
    
    # 1. Delete old steps
    old_steps = session.exec(select(ScenarioStep).where(ScenarioStep.scenario_id == scenario_id)).all()
    for s in old_steps:
        session.delete(s)
    
    # 2. Add new steps
    for s_in in steps:
        new_step = ScenarioStep(
            scenario_id=scenario_id,
            case_id=s_in.case_id,
            order=s_in.order,
            alias=s_in.alias
        )
        session.add(new_step)
        
    # 3. Update scenario stats
    scenario.step_count = len(steps)
    scenario.updated_at = datetime.now()
    scenario.updater_id = current_user.id
    session.add(scenario)
    
    session.commit()
    return {"success": True, "count": len(steps)}

# ---- Execution ----

def _merge_case_variables_with_context(case: TestCase, scenario_context: Dict[str, str]) -> Dict[str, str]:
    merged = dict(scenario_context)
    for item in case.variables or []:
        if isinstance(item, dict):
            key = str(item.get("key") or "").strip()
            value = item.get("value")
        else:
            key = str(getattr(item, "key", "") or "").strip()
            value = getattr(item, "value", None)

        if key:
            merged[key] = "" if value is None else str(value)
    return merged


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
        if step_item.get("screenshot"):
            converted["screenshot"] = step_item.get("screenshot")
        if status == "WARNING":
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


def precheck_scenario_execution(
    session: Session,
    scenario_id: int,
    device_serial: str,
    env_id: Optional[int] = None,
) -> Dict[str, Any]:
    scenario = session.get(TestScenario, scenario_id)
    if not scenario:
        raise ValueError(f"Scenario not found: {scenario_id}")

    steps = session.exec(
        select(ScenarioStep)
        .where(ScenarioStep.scenario_id == scenario_id)
        .order_by(ScenarioStep.order)
    ).all()

    scenario_context: Dict[str, str] = {}
    if env_id:
        from backend.models import GlobalVariable

        global_vars = session.exec(
            select(GlobalVariable).where(GlobalVariable.env_id == env_id)
        ).all()
        for gv in global_vars:
            if gv.key:
                scenario_context[gv.key] = gv.value

    case_checks: List[Dict[str, Any]] = []
    has_runnable_cases = False
    fail_cases = 0
    warning_cases = 0
    skipped_cases = 0
    pass_cases = 0

    for scenario_step in steps:
        case = session.get(TestCase, scenario_step.case_id)
        if not case:
            fail_cases += 1
            case_checks.append(
                {
                    "step_order": scenario_step.order,
                    "scenario_step_id": scenario_step.id,
                    "alias": scenario_step.alias,
                    "case_id": scenario_step.case_id,
                    "case_name": "Unknown",
                    "status": "FAIL",
                    "ok": False,
                    "reason": f"Case not found: {scenario_step.case_id}",
                    "summary": {"pass": 0, "skip": 0, "fail": 1, "global_fail": 0, "total": 1},
                    "global_checks": [],
                    "steps": [
                        {
                            "order": 0,
                            "action": "system",
                            "status": "FAIL",
                            "code": "CASE_NOT_FOUND",
                            "message": f"Case not found: {scenario_step.case_id}",
                        }
                    ],
                }
            )
            continue

        variables_map = _merge_case_variables_with_context(case, scenario_context)
        check = precheck_case_execution(
            session=session,
            case=case,
            device_serial=device_serial,
            env_id=None,
            variables_map=variables_map,
        )

        if check.get("has_runnable_steps"):
            has_runnable_cases = True

        summary = check.get("summary") or {}
        global_fail_count = int(summary.get("global_fail") or 0)
        fail_count = int(summary.get("fail") or 0)
        pass_count = int(summary.get("pass") or 0)
        skip_count = int(summary.get("skip") or 0)
        all_skipped = skip_count > 0 and pass_count == 0 and fail_count == 0 and global_fail_count == 0

        if global_fail_count > 0 or fail_count > 0:
            case_status = "FAIL"
            fail_cases += 1
        elif all_skipped:
            case_status = "SKIP"
            skipped_cases += 1
        elif check.get("ok"):
            case_status = "PASS"
            pass_cases += 1
        else:
            # e.g. no runnable steps but not all skipped due to empty case
            case_status = "WARNING"
            warning_cases += 1

        case_checks.append(
            {
                "step_order": scenario_step.order,
                "scenario_step_id": scenario_step.id,
                "alias": scenario_step.alias,
                "case_id": case.id,
                "case_name": case.name,
                "status": case_status,
                "ok": bool(check.get("ok")),
                "has_runnable_steps": bool(check.get("has_runnable_steps")),
                "summary": summary,
                "global_checks": check.get("global_checks") or [],
                "steps": check.get("steps") or [],
                "reason": _summarize_precheck_failure(check) if not check.get("ok") else None,
            }
        )

        exported = check.get("exported_variables")
        if isinstance(exported, dict):
            scenario_context.update(exported)
        else:
            scenario_context.update(variables_map)

    total_cases = len(case_checks)
    all_cases_skipped = total_cases > 0 and skipped_cases == total_cases
    ok = fail_cases == 0 and has_runnable_cases

    return {
        "scenario_id": scenario_id,
        "scenario_name": scenario.name,
        "device_serial": device_serial,
        "ok": ok,
        "has_runnable_cases": has_runnable_cases,
        "summary": {
            "total_cases": total_cases,
            "pass_cases": pass_cases,
            "warning_cases": warning_cases,
            "skip_cases": skipped_cases,
            "fail_cases": fail_cases,
            "all_cases_skipped": all_cases_skipped,
        },
        "cases": case_checks,
    }


def _run_scenario_cross_platform(
    scenario_id: int,
    session: Session,
    device_serial: str,
    env_id: Optional[int] = None,
    abort_event=None,
) -> Dict[str, Any]:
    scenario = session.get(TestScenario, scenario_id)
    if not scenario:
        raise ValueError(f"Scenario not found: {scenario_id}")

    steps = session.exec(
        select(ScenarioStep)
        .where(ScenarioStep.scenario_id == scenario_id)
        .order_by(ScenarioStep.order)
    ).all()

    scenario_context: Dict[str, str] = {}
    if env_id:
        from backend.models import GlobalVariable

        global_vars = session.exec(
            select(GlobalVariable).where(GlobalVariable.env_id == env_id)
        ).all()
        for gv in global_vars:
            if gv.key:
                scenario_context[gv.key] = gv.value

    success = True
    results: List[Dict[str, Any]] = []

    for scenario_step in steps:
        if abort_event and abort_event.is_set():
            success = False
            break

        case = session.get(TestCase, scenario_step.case_id)
        if not case:
            results.append(
                {
                    "step_order": scenario_step.order,
                    "scenario_step_id": scenario_step.id,
                    "alias": scenario_step.alias,
                    "case_name": "Unknown",
                    "result": {
                        "case_id": scenario_step.case_id,
                        "success": False,
                        "steps": [
                            {
                                "step": {
                                    "action": "system",
                                    "selector": None,
                                    "selector_type": None,
                                    "value": None,
                                    "options": {},
                                    "description": "case not found",
                                    "error_strategy": "ABORT",
                                    "timeout": 1,
                                },
                                "success": False,
                                "error": f"Case not found: {scenario_step.case_id}",
                                "duration": 0,
                            }
                        ],
                        "exported_variables": dict(scenario_context),
                    },
                }
            )
            success = False
            continue

        variables_map = _merge_case_variables_with_context(case, scenario_context)

        try:
            cross_result = run_case_with_standard_runner(
                session=session,
                case=case,
                device_serial=device_serial,
                env_id=None,
                variables_map=variables_map,
                abort_event=abort_event,
            )
            case_result = _convert_cross_result_to_legacy_case_result(
                case=case,
                cross_result=cross_result,
                variables_map=variables_map,
            )
        except Exception as exc:
            case_result = {
                "case_id": case.id,
                "success": False,
                "steps": [
                    {
                        "step": {
                            "action": "system",
                            "selector": None,
                            "selector_type": None,
                            "value": None,
                            "options": {},
                            "description": "cross-platform execution failed",
                            "error_strategy": "ABORT",
                            "timeout": 1,
                        },
                        "success": False,
                        "error": str(exc),
                        "duration": 0,
                    }
                ],
                "exported_variables": dict(variables_map),
            }

        results.append(
            {
                "step_order": scenario_step.order,
                "scenario_step_id": scenario_step.id,
                "alias": scenario_step.alias,
                "case_name": case.name,
                "result": case_result,
            }
        )

        exported = case_result.get("exported_variables", {})
        if isinstance(exported, dict):
            scenario_context.update(exported)

        if not case_result.get("success"):
            strategy = "ABORT"
            for step_result in reversed(case_result.get("steps", [])):
                if not step_result.get("success") and not step_result.get("is_warning"):
                    strategy = normalize_error_strategy(
                        (step_result.get("step") or {}).get("error_strategy", "ABORT")
                    )
                    break

            if strategy == "CONTINUE":
                success = False
                continue

            success = False
            break

    return {
        "success": success,
        "scenario_id": scenario_id,
        "results": results,
    }


def _run_single_device_sync(execution_id: int, scenario_id: int, device_serial: Optional[str] = None, env_id: Optional[int] = None):
    """核心：每个子线程内独立的执行逻辑。必须使用独立的数据库 Session 防止并发冲突"""
    from sqlmodel import Session as SQLSession
    
    abort_event = None
    with SQLSession(engine) as session:
        execution = session.get(TestExecution, execution_id)
        if not execution:
            return

        resolved_meta = _resolve_device_meta(
            session,
            device_serial,
            fallback_display=execution.device_info,
        )
        execution.status = "RUNNING"
        execution.start_time = datetime.now()
        execution.device_serial = resolved_meta.get("device_serial")
        if resolved_meta.get("platform"):
            execution.platform = resolved_meta.get("platform")
        if resolved_meta.get("device_info"):
            execution.device_info = resolved_meta.get("device_info")
        session.add(execution)
        session.commit()
        
        scenario = session.get(TestScenario, scenario_id)
        if not scenario:
            execution.status = "ERROR"
            execution.end_time = datetime.now()
            execution.duration = 0
            session.add(execution)
            session.commit()
            logger.error("scenario execution aborted: scenario not found scenario_id=%s execution_id=%s", scenario_id, execution_id)
            return
            
        try:
            start_time = execution.start_time

            use_cross_platform_runner = (
                is_flag_enabled(session, FLAG_CROSS_PLATFORM_RUNNER, default=False)
                and bool(device_serial)
            )

            if use_cross_platform_runner:
                abort_event = _prepare_cross_platform_device_execution(
                    session=session,
                    execution=execution,
                    device_serial=device_serial,
                )
                _execute_cross_platform_scenario_core(
                    session=session,
                    scenario=scenario,
                    execution=execution,
                    scenario_id=scenario_id,
                    device_serial=device_serial,
                    start_time=start_time,
                    env_id=env_id,
                    abort_event=abort_event,
                )
            else:
                # Legacy Android path
                runner = ScenarioRunner(device_serial=device_serial)
                try:
                    prepared_serial, abort_event = _prepare_legacy_scenario_device_execution(
                        session=session,
                        execution=execution,
                        runner=runner,
                    )
                    if prepared_serial:
                        device_serial = prepared_serial
                except Exception as e:
                    logger.warning("legacy scenario device connect failed: execution_id=%s scenario_id=%s error=%s", execution_id, scenario_id, e)
                    execution.status = "ERROR"
                    execution.end_time = datetime.now()
                    session.add(execution)
                    session.commit()
                    return

                event_iter = _iter_legacy_scenario_event_infos(
                    session=session,
                    execution_id=execution.id,
                    runner=runner,
                    scenario_id=scenario_id,
                    env_id=env_id,
                    commit_per_step=False,
                )
                while True:
                    try:
                        next(event_iter)
                    except StopIteration as stop:
                        legacy_state = stop.value or _build_legacy_scenario_state()
                        break

                session.commit()
                _finalize_scenario_execution(
                    session=session,
                    scenario=scenario,
                    execution=execution,
                    cases_results=legacy_state["cases_results"],
                    start_time=start_time,
                )
        except Exception as e:
            logger.exception("background scenario execution failed: scenario_id=%s execution_id=%s", scenario_id, execution.id if execution else None)
            scenario.last_run_status = "FAIL"
            scenario.last_execution_id = execution.id if 'execution' in locals() else None
            if 'start_time' in locals():
                scenario.last_run_duration = int((datetime.now() - start_time).total_seconds())
            session.add(scenario)
            
            # Fail the execution record if exists
            if 'execution' in locals():
                execution.status = "ERROR"
                execution.end_time = datetime.now()
                session.add(execution)
                
            session.commit()
        finally:
            # ★ 恢复设备状态
            if device_serial:
                try:
                    restore_device_status_after_execution(session, device_serial)
                except Exception as e:
                    logger.warning("恢复设备状态失败（后台场景执行）: device=%s error=%s", device_serial, e)
                # ★ 清除中止事件注册
                unregister_device_abort(device_serial)

async def _schedule_concurrent_runs(execution_ids: List[int], scenario_id: int, device_serials: List[str], env_id: Optional[int] = None):
    """使用 ThreadPoolExecutor 并发执行每个设备的测试"""
    loop = asyncio.get_running_loop()
    
    # 创建线程池，最大 worker 数量与设备数量一致，保证并发
    max_workers = len(device_serials) if device_serials else 1
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        tasks = []
        for exec_id, serial in zip(execution_ids, device_serials):
            # run_in_executor 将同步阻塞的 Runner 任务放入线程池调度
            task = loop.run_in_executor(
                executor, 
                _run_single_device_sync,  # 传入同步目标函数
                exec_id, 
                scenario_id, 
                serial, 
                env_id
            )
            tasks.append(task)
            
        # 等待所有设备上的执行任务全部返回
        if tasks:
            await asyncio.gather(*tasks)

def execute_scenario_batch_background(scenario_id: int, executor_name: str, env_id: Optional[int], device_serials: List[str]):
    """Background task used by tasks.py to execute tests concurrently on multiple devices."""
    from sqlmodel import Session as SQLSession
    from backend.database import engine
    import uuid
    import asyncio
    
    with SQLSession(engine) as session:
        scenario = session.get(TestScenario, scenario_id)
        if not scenario: return None
        
        batch_id = str(uuid.uuid4())
        execution_ids = []
        
        if not device_serials:
            device_serials = [None]
            
        for serial in device_serials:
            meta = _resolve_device_meta(
                session,
                serial,
                fallback_display=(serial or "Scheduled Runner"),
            )

            execution = TestExecution(
                scenario_id=scenario_id,
                scenario_name=scenario.name,
                status="PENDING",
                executor_id=None,
                executor_name=executor_name,
                device_serial=meta.get("device_serial"),
                platform=meta.get("platform"),
                device_info=meta.get("device_info"),
                batch_id=batch_id,
                batch_name=f"{scenario.name} 定时执行"
            )
            session.add(execution)
            session.commit()
            session.refresh(execution)
            execution_ids.append(execution.id)
            
    # Run concurrently and block the APScheduler thread until all finish
    asyncio.run(_schedule_concurrent_runs(execution_ids, scenario_id, device_serials, env_id))
    return batch_id

@router.post("/{scenario_id}/run")
async def run_scenario_api(
    scenario_id: int,
    request: ScenarioRunRequest,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user)
):
    """触发场景在多个设备上的并发执行"""
    scenario = session.get(TestScenario, scenario_id)
    if not scenario:
        raise HTTPException(status_code=404, detail="Scenario not found")
        
    executor_name = current_user.full_name or current_user.username
    
    batch_id = str(uuid.uuid4())
    execution_ids = []
    
    requested_serials = request.device_serials or [None]
    runnable_serials: List[Optional[str]] = []
    blocked_prechecks: List[Dict[str, Any]] = []

    for serial in requested_serials:
        if not serial:
            runnable_serials.append(serial)
            continue
        try:
            precheck = precheck_scenario_execution(
                session=session,
                scenario_id=scenario_id,
                device_serial=serial,
                env_id=request.env_id,
            )
        except Exception as exc:
            blocked_prechecks.append(
                {
                    "device_serial": serial,
                    "reason": str(exc),
                }
            )
            continue

        if precheck.get("ok"):
            runnable_serials.append(serial)
        else:
            blocked_prechecks.append(
                {
                    "device_serial": serial,
                    "reason": _summarize_precheck_failure(precheck),
                    "precheck": precheck,
                }
            )

    if not runnable_serials:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "S1001_SCENARIO_PRECHECK_FAILED",
                "message": "scenario precheck failed for all selected devices",
                "items": blocked_prechecks,
            },
        )

    for serial in runnable_serials:
        meta = _resolve_device_meta(
            session,
            serial,
            fallback_display=(serial or "Default Runner"),
        )

        execution = TestExecution(
            scenario_id=scenario_id,
            scenario_name=scenario.name,
            status="PENDING", 
            executor_id=current_user.id,
            executor_name=executor_name,
            device_serial=meta.get("device_serial"),
            platform=meta.get("platform"),
            device_info=meta.get("device_info"),
            batch_id=batch_id,
            batch_name=f"{scenario.name} 并发运行"
        )
        session.add(execution)
        session.commit()
        session.refresh(execution)
        execution_ids.append(execution.id)
        
    asyncio.create_task(_schedule_concurrent_runs(
        execution_ids=execution_ids,
        scenario_id=scenario_id,
        device_serials=runnable_serials,
        env_id=request.env_id
    ))
    
    return {
        "message": "Batch execution started", 
        "batch_id": batch_id,
        "execution_ids": execution_ids,
        "blocked_prechecks": blocked_prechecks,
    }


@router.get("/{scenario_id}/precheck")
def precheck_scenario_api(
    scenario_id: int,
    device_serial: str,
    env_id: Optional[int] = None,
    session: Session = Depends(get_session),
):
    """Precheck scenario executability on target device without execution."""
    scenario = session.get(TestScenario, scenario_id)
    if not scenario:
        raise HTTPException(status_code=404, detail="Scenario not found")
    return precheck_scenario_execution(
        session=session,
        scenario_id=scenario_id,
        device_serial=device_serial,
        env_id=env_id,
    )

# ---- WebSocket Execution ----

from fastapi import WebSocket, WebSocketDisconnect
from backend.socket_manager import manager
import time


def _build_ws_active_case_state(scenario_event: Dict[str, Any]) -> Dict[str, Any]:
    scenario_step = scenario_event.get("scenario_step")
    case_obj = scenario_event.get("case")
    return {
        "case_id": getattr(case_obj, "id", None),
        "case_name": scenario_event.get("case_name") or "Unknown",
        "alias": getattr(scenario_step, "alias", None),
        "step_name": scenario_event.get("step_name") or "Unknown",
        "started_at": time.time(),
        "steps": [],
        "last_screenshot_base64": None,
    }


async def _broadcast_ws_case_outcome(
    ws_key: str,
    case_status: Optional[str],
    duration: float,
    *,
    attachment: Optional[str] = None,
) -> None:
    if case_status == "success":
        await manager.broadcast_log(ws_key, "success", f"  ✓ 通过 (耗时 {duration:.2f}s)")
    elif case_status == "warning":
        await manager.broadcast_log(ws_key, "warning", f"  ⚠ 警告 (耗时 {duration:.2f}s)")
    elif case_status == "skipped":
        await manager.broadcast_log(ws_key, "info", f"  ↷ 全部跳过 (耗时 {duration:.2f}s)")
    else:
        await manager.broadcast_log(
            ws_key,
            "error",
            f"  ✗ 失败 (耗时 {duration:.2f}s)",
            attachment=attachment,
            attachment_type="image" if attachment else None,
        )


async def _broadcast_ws_execution_complete(
    ws_key: str,
    execution_id: int,
    summary: Dict[str, Any],
) -> None:
    report_id = summary.get("report_id")
    if report_id:
        await manager.broadcast_log(ws_key, "success", f"📊 报告已生成: {report_id}")
    elif summary.get("report_error"):
        await manager.broadcast_log(ws_key, "error", f"报告生成失败: {summary['report_error']}")

    scenario_status = summary.get("scenario_status", "FAIL")
    summary_msg = summary.get("summary_msg", "执行完成")
    final_status = "success" if scenario_status == "PASS" else "warning"

    await manager.broadcast_log(ws_key, final_status, summary_msg)
    await manager.send_message(
        ws_key,
        {
            "type": "run_complete",
            "success": scenario_status == "PASS",
            "status": scenario_status,
            "summary": summary_msg,
            "report_id": report_id,
            "execution_id": execution_id,
        },
    )


def _capture_legacy_ws_failure_screenshot_base64(
    runner: ScenarioRunner,
    *,
    execution_id: int,
    step_order: int,
) -> Optional[str]:
    driver = getattr(getattr(runner, "runner", None), "d", None)
    if not driver:
        return None

    try:
        image = driver.screenshot()
        return _encode_case_error_screenshot_base64(
            image,
            execution_id=execution_id,
            step_order=step_order,
        )
    except Exception as exc:
        logger.warning(
            "websocket scenario screenshot failed: execution_id=%s step_order=%s error=%s",
            execution_id,
            step_order,
            exc,
        )
        return None


def _build_ws_case_entry(
    active_case: Dict[str, Any],
    *,
    case_result: Optional[Dict[str, Any]] = None,
    force_status: Optional[str] = None,
) -> Tuple[Dict[str, Any], float]:
    now = time.time()
    started_at = float(active_case.get("started_at") or now)
    duration = max(0.0, now - started_at)
    resolved_result = case_result or {}
    case_status = force_status or _determine_case_status(
        active_case.get("steps") or [],
        case_success=bool(resolved_result.get("success")),
        case_is_warning=bool(resolved_result.get("is_warning")),
    )
    case_entry = {
        "case_id": resolved_result.get("case_id") or active_case.get("case_id"),
        "case_name": active_case.get("case_name"),
        "alias": active_case.get("alias"),
        "status": case_status,
        "duration": round(duration, 2),
        "steps": list(active_case.get("steps") or []),
    }
    return case_entry, duration


def _build_legacy_scenario_state() -> Dict[str, Any]:
    return {
        "cases_results": [],
        "global_step_order": 1,
        "active_case": None,
    }



def _handle_legacy_scenario_step_result(
    *,
    session: Session,
    execution_id: int,
    runner: ScenarioRunner,
    scenario_event: Dict[str, Any],
    active_case: Optional[Dict[str, Any]],
    global_step_order: int,
    commit_per_step: bool,
) -> Tuple[Dict[str, Any], int, Dict[str, Any]]:
    if active_case is None:
        active_case = _build_ws_active_case_state(scenario_event)

    case_step = scenario_event.get("step")
    step_res = scenario_event.get("step_result") or {}
    step_payload = step_res.get("step") or {}
    step_duration = float(step_res.get("duration") or 0)
    action_value = getattr(case_step, "action", None) or step_payload.get("action")
    selector_value = getattr(case_step, "selector", None) or step_payload.get("selector")
    description_value = getattr(case_step, "description", None) or step_payload.get("description")
    selector_type_value = getattr(case_step, "selector_type", None) or step_payload.get("selector_type")
    strategy = getattr(case_step, "error_strategy", None) or step_payload.get("error_strategy") or "ABORT"
    action_desc = f"{action_value} {selector_value or ''}".strip()
    step_log_entry = {
        "action": action_value,
        "description": description_value,
        "selector": selector_value,
        "selector_type": selector_type_value,
        "duration": round(step_duration, 2),
    }

    screenshot_base64 = str(step_res.get("screenshot") or "").strip() or None

    if step_res.get("is_skipped"):
        step_log_entry["status"] = "skipped"
    elif not step_res.get("success"):
        error_msg = step_res.get("error")
        if step_res.get("is_warning"):
            step_log_entry["status"] = "warning"
        else:
            step_log_entry["status"] = "failed"
            step_log_entry["strategy"] = strategy
        step_log_entry["error"] = error_msg

        if not screenshot_base64:
            screenshot_base64 = _capture_legacy_ws_failure_screenshot_base64(
                runner,
                execution_id=execution_id,
                step_order=global_step_order,
            )
    else:
        step_log_entry["status"] = "success"

    screenshot_path = _persist_step_screenshot(
        execution_id=execution_id,
        step_order=global_step_order,
        screenshot_b64=screenshot_base64,
    )
    if screenshot_base64:
        step_log_entry["screenshot"] = screenshot_base64
        active_case["last_screenshot_base64"] = screenshot_base64

    active_case["steps"].append(step_log_entry)

    test_result = TestResult(
        execution_id=execution_id,
        step_name=f"[{active_case['step_name']}] {description_value or action_value}",
        step_order=global_step_order,
        status=_ui_status_to_db_status(step_log_entry["status"]),
        duration=step_duration * 1000,
        error_message=step_res.get("error"),
        screenshot_path=screenshot_path,
    )
    session.add(test_result)
    if commit_per_step:
        session.commit()

    event_info = {
        "type": "step_result",
        "status": step_log_entry["status"],
        "action_desc": action_desc or action_value,
        "error": step_res.get("error"),
        "strategy": strategy,
        "step_log_entry": step_log_entry,
    }
    return active_case, global_step_order + 1, event_info



def _consume_legacy_scenario_event(
    *,
    session: Session,
    execution_id: int,
    runner: ScenarioRunner,
    scenario_event: Dict[str, Any],
    state: Dict[str, Any],
    commit_per_step: bool,
) -> Dict[str, Any]:
    event_type = scenario_event.get("type")

    if event_type == "scenario_abort":
        return {"type": event_type}

    if event_type == "case_start":
        state["active_case"] = _build_ws_active_case_state(scenario_event)
        return {
            "type": "case_start",
            "case_index": int(scenario_event.get("case_index") or 0),
            "total_cases": int(scenario_event.get("total_cases") or 0),
            "step_name": scenario_event.get("step_name") or "Unknown",
            "case_name": scenario_event.get("case_name") or "Unknown",
        }

    if event_type == "step_result":
        active_case, next_order, event_info = _handle_legacy_scenario_step_result(
            session=session,
            execution_id=execution_id,
            runner=runner,
            scenario_event=scenario_event,
            active_case=state.get("active_case"),
            global_step_order=int(state.get("global_step_order") or 1),
            commit_per_step=commit_per_step,
        )
        state["active_case"] = active_case
        state["global_step_order"] = next_order
        return event_info

    if event_type == "case_missing":
        case_index = int(scenario_event.get("case_index") or 0)
        step_name = scenario_event.get("step_name") or f"Step {case_index + 1}"
        case_id = scenario_event.get("case_id")
        missing_case_result = _build_synthetic_case_result(
            case_id=case_id,
            error_message=f"Case not found: {case_id}",
            description="case not found",
        )
        raw_item = dict(scenario_event.get("raw_result") or {})
        raw_item["alias"] = raw_item.get("alias") or getattr(scenario_event.get("scenario_step"), "alias", None)
        raw_item["case_name"] = raw_item.get("case_name") or scenario_event.get("case_name") or "Unknown"
        case_entry, next_order, _ = _persist_case_result_and_build_case_report(
            session=session,
            execution_id=execution_id,
            item=raw_item,
            case_result=missing_case_result,
            global_step_order=int(state.get("global_step_order") or 1),
            step_name_prefix=step_name,
            include_case_duration=True,
            commit_per_step=commit_per_step,
        )
        state["global_step_order"] = next_order
        state["cases_results"].append(case_entry)
        state["active_case"] = None
        return {
            "type": "case_missing",
            "case_index": case_index,
            "step_name": step_name,
            "case_id": case_id,
            "case_entry": case_entry,
        }

    if event_type == "case_exception":
        active_case = state.get("active_case")
        if active_case is None:
            active_case = _build_ws_active_case_state(scenario_event)
            active_case["case_id"] = scenario_event.get("case_id") or active_case.get("case_id")

        error_message = str(scenario_event.get("error") or "legacy scenario execution failed")
        active_case["steps"].append(
            {
                "status": "failed",
                "action": "system",
                "description": error_message,
                "selector": None,
                "selector_type": None,
                "duration": 0,
                "error": error_message,
            }
        )
        session.add(
            TestResult(
                execution_id=execution_id,
                step_name=f"[{active_case['step_name']}] {error_message}",
                step_order=int(state.get("global_step_order") or 1),
                status="FAIL",
                duration=0,
                error_message=error_message,
                screenshot_path=None,
            )
        )
        if commit_per_step:
            session.commit()
        state["global_step_order"] = int(state.get("global_step_order") or 1) + 1

        case_entry, duration = _build_ws_case_entry(active_case, force_status="failed")
        state["cases_results"].append(case_entry)
        state["active_case"] = None
        return {
            "type": "case_exception",
            "error": error_message,
            "case_entry": case_entry,
            "duration": duration,
            "attachment": active_case.get("last_screenshot_base64"),
        }

    if event_type == "case_complete":
        case_result = scenario_event.get("case_result") or {}
        active_case = state.get("active_case")
        if active_case is None:
            active_case = _build_ws_active_case_state(scenario_event)
            active_case["case_id"] = case_result.get("case_id") or active_case.get("case_id")

        case_entry, duration = _build_ws_case_entry(active_case, case_result=case_result)
        state["cases_results"].append(case_entry)
        state["active_case"] = None
        return {
            "type": "case_complete",
            "case_entry": case_entry,
            "duration": duration,
            "attachment": active_case.get("last_screenshot_base64"),
        }

    return {"type": str(event_type or "unknown")}


def _iter_legacy_scenario_event_infos(
    *,
    session: Session,
    execution_id: int,
    runner: ScenarioRunner,
    scenario_id: int,
    env_id: Optional[int],
    commit_per_step: bool,
):
    state = _build_legacy_scenario_state()
    scenario_iter = runner.iter_scenario_execution(scenario_id, session, env_id=env_id)
    while True:
        try:
            scenario_event = next(scenario_iter)
        except StopIteration:
            return state
        event_info = _consume_legacy_scenario_event(
            session=session,
            execution_id=execution_id,
            runner=runner,
            scenario_event=scenario_event,
            state=state,
            commit_per_step=commit_per_step,
        )
        yield event_info


def _iter_cross_platform_event_infos(
    *,
    raw_results: List[Dict[str, Any]],
    cases_results: List[Dict[str, Any]],
):
    total_cases = len(raw_results or [])
    for idx, item in enumerate(raw_results or []):
        step_name = item.get("alias") or f"Step {idx + 1}"
        case_name = item.get("case_name") or "未知用例"
        yield {
            "type": "case_start",
            "case_index": idx,
            "total_cases": total_cases,
            "step_name": step_name,
            "case_name": case_name,
        }

        case_entry = cases_results[idx] if idx < len(cases_results) else {
            "steps": [],
            "status": "failed",
            "duration": 0,
        }
        for step_entry in case_entry.get("steps", []) or []:
            action_desc = step_entry.get("action") or step_entry.get("description") or "unknown"
            yield {
                "type": "step_result",
                "status": step_entry.get("status"),
                "action_desc": action_desc,
                "error": step_entry.get("error"),
                "strategy": step_entry.get("strategy"),
                "emit_success": True,
            }

        attachment = None
        for step_entry in reversed(case_entry.get("steps", []) or []):
            if step_entry.get("status") == "failed" and step_entry.get("screenshot"):
                attachment = step_entry.get("screenshot")
                break
        yield {
            "type": "case_complete",
            "case_entry": case_entry,
            "duration": float(case_entry.get("duration") or 0),
            "attachment": attachment,
        }


async def _broadcast_ws_scenario_event(ws_key: str, event_info: Dict[str, Any]) -> None:
    event_type = event_info.get("type")

    if event_type == "scenario_abort":
        await manager.broadcast_log(ws_key, "warning", "⚠️ 收到中止信号，停止执行")
        return

    if event_type == "case_start":
        await manager.broadcast_log(
            ws_key,
            "info",
            f"👉 [{event_info.get('case_index', 0) + 1}/{event_info.get('total_cases', 0)}] 执行: "
            f"{event_info.get('step_name', 'Unknown')} ({event_info.get('case_name', 'Unknown')})",
        )
        return

    if event_type == "step_result":
        status = event_info.get("status")
        if status == "skipped":
            await manager.broadcast_log(ws_key, "info", f"    ↷ 跳过: {event_info.get('action_desc')}")
        elif status == "warning":
            await manager.broadcast_log(
                ws_key,
                "warning",
                f"    🟡 忽略错误: {event_info.get('error')} ({event_info.get('action_desc')})",
            )
        elif status == "failed":
            strategy = event_info.get("strategy")
            strategy_suffix = f" [策略: {strategy}]" if strategy else ""
            await manager.broadcast_log(
                ws_key,
                "error",
                f"    ❌ 失败: {event_info.get('error')} ({event_info.get('action_desc')}){strategy_suffix}",
            )
        elif status == "success" and event_info.get("emit_success"):
            await manager.broadcast_log(
                ws_key,
                "success",
                f"    ✓ 成功: {event_info.get('action_desc')}",
            )
        return

    if event_type == "case_missing":
        await manager.broadcast_log(
            ws_key,
            "warning",
            f"⚠️ 步骤 {event_info.get('case_index', 0) + 1} ({event_info.get('step_name')}): 用例不存在 "
            f"(ID: {event_info.get('case_id')})，跳过",
        )
        case_entry = event_info.get("case_entry") or {}
        await _broadcast_ws_case_outcome(
            ws_key,
            case_entry.get("status"),
            float(case_entry.get("duration") or 0),
        )
        return

    if event_type == "case_exception":
        await manager.broadcast_log(ws_key, "error", f"    ❌ 异常: {event_info.get('error')}")
        case_entry = event_info.get("case_entry") or {}
        await _broadcast_ws_case_outcome(
            ws_key,
            case_entry.get("status"),
            float(event_info.get("duration") or case_entry.get("duration") or 0),
            attachment=event_info.get("attachment"),
        )
        return

    if event_type == "case_complete":
        case_entry = event_info.get("case_entry") or {}
        await _broadcast_ws_case_outcome(
            ws_key,
            case_entry.get("status"),
            float(event_info.get("duration") or case_entry.get("duration") or 0),
            attachment=event_info.get("attachment"),
        )


async def _run_in_blocking_executor(executor: ThreadPoolExecutor, func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, partial(func, *args, **kwargs))


def _execute_cross_platform_scenario_ws(
    *,
    scenario_id: int,
    execution_id: int,
    device_serial: str,
    start_time: datetime,
    env_id: Optional[int],
) -> Dict[str, Any]:
    from sqlmodel import Session as SQLSession

    with SQLSession(engine) as session:
        scenario = session.get(TestScenario, scenario_id)
        if not scenario:
            raise RuntimeError(f"Scenario not found: {scenario_id}")

        execution = session.get(TestExecution, execution_id)
        if not execution:
            raise RuntimeError(f"Execution not found: {execution_id}")

        abort_event = _prepare_cross_platform_device_execution(
            session=session,
            execution=execution,
            device_serial=device_serial,
        )
        return _execute_cross_platform_scenario_core(
            session=session,
            scenario=scenario,
            execution=execution,
            scenario_id=scenario_id,
            device_serial=device_serial,
            start_time=start_time,
            env_id=env_id,
            abort_event=abort_event,
            commit_per_step=True,
        )


class _LegacyScenarioWsExecutionState:
    def __init__(
        self,
        *,
        scenario_id: int,
        execution_id: int,
        env_id: Optional[int],
        device_serial: Optional[str],
    ) -> None:
        from sqlmodel import Session as SQLSession

        self.session = SQLSession(engine)
        self.scenario_id = scenario_id
        self.env_id = env_id
        self.device_serial = device_serial
        self.execution = self.session.get(TestExecution, execution_id)
        if not self.execution:
            self.session.close()
            raise RuntimeError(f"Execution not found: {execution_id}")
        self.runner = ScenarioRunner(device_serial=device_serial)
        self.start_time = self.execution.start_time or datetime.now()
        self._event_iter = None

    def prepare(self) -> Optional[str]:
        prepared_serial, _abort_event = _prepare_legacy_scenario_device_execution(
            session=self.session,
            execution=self.execution,
            runner=self.runner,
        )
        if prepared_serial:
            self.device_serial = prepared_serial
        self._event_iter = _iter_legacy_scenario_event_infos(
            session=self.session,
            execution_id=self.execution.id,
            runner=self.runner,
            scenario_id=self.scenario_id,
            env_id=self.env_id,
            commit_per_step=True,
        )
        return self.device_serial

    def next_event(self) -> Dict[str, Any]:
        if self._event_iter is None:
            raise RuntimeError("legacy websocket execution is not prepared")

        try:
            return {"done": False, "event": next(self._event_iter)}
        except StopIteration as stop:
            legacy_state = stop.value or _build_legacy_scenario_state()
            scenario = self.session.get(TestScenario, self.scenario_id)
            if not scenario:
                raise RuntimeError(f"Scenario not found: {self.scenario_id}")
            final_summary = _finalize_scenario_execution(
                session=self.session,
                scenario=scenario,
                execution=self.execution,
                cases_results=legacy_state["cases_results"],
                start_time=self.start_time,
            )
            return {"done": True, "summary": final_summary}

    def close(self) -> None:
        self.session.close()


@router.websocket("/ws/run/{scenario_id}")
async def websocket_run_scenario(websocket: WebSocket, scenario_id: int, env_id: Optional[int] = None, device_serial: Optional[str] = None):
    """WebSocket endpoint: Run scenario with real-time logs"""
    ws_key = f"scenario:{scenario_id}"
    await manager.connect(websocket, ws_key)

    blocking_executor = ThreadPoolExecutor(max_workers=1)
    legacy_ws_state = None
    device_serial_ws = device_serial
    try:
        # 1. Get Scenario Data
        from sqlmodel import Session as SQLSession
        
        with SQLSession(engine) as session:
            scenario = session.get(TestScenario, scenario_id)
            if not scenario:
                await manager.broadcast_log(ws_key, "error", "场景不存在")
                return
            
            # Figure out executor_name for WebSocket
            executor_name = "System"
            if scenario.updater_id:
                updater = session.get(User, scenario.updater_id)
                if updater:
                    executor_name = updater.full_name or updater.username
                    
            # Create Execution Record
            start_time = datetime.now()
            ws_init_meta = _resolve_device_meta(
                session,
                device_serial,
                fallback_display="WebSocket Runner",
            )
            execution = TestExecution(
                scenario_id=scenario_id,
                scenario_name=scenario.name,
                status="RUNNING",
                start_time=start_time,
                executor_id=scenario.updater_id, # Approximate
                executor_name=executor_name,
                device_serial=ws_init_meta.get("device_serial"),
                platform=ws_init_meta.get("platform"),
                device_info=ws_init_meta.get("device_info"),
            )
            session.add(execution)
            session.commit()
            session.refresh(execution)
            
            # Get steps (ordered)
            steps_db = session.exec(select(ScenarioStep).where(ScenarioStep.scenario_id == scenario_id).order_by(ScenarioStep.order)).all()
            total_steps = len(steps_db)
            
            await manager.broadcast_log(ws_key, "info", f"🎬 开始执行场景: {scenario.name} (共 {total_steps} 个步骤)")

            use_cross_platform_runner = (
                is_flag_enabled(session, FLAG_CROSS_PLATFORM_RUNNER, default=False)
                and bool(device_serial)
            )

            if use_cross_platform_runner:
                await manager.broadcast_log(ws_key, "info", "🧠 使用跨端执行引擎")
                device_serial_ws = device_serial

                try:
                    scenario_precheck = precheck_scenario_execution(
                        session=session,
                        scenario_id=scenario_id,
                        device_serial=device_serial_ws,
                        env_id=env_id,
                    )
                except Exception as exc:
                    execution.status = "ERROR"
                    execution.end_time = datetime.now()
                    execution.duration = 0
                    session.add(execution)
                    session.commit()
                    await manager.broadcast_log(ws_key, "error", f"❌ 运行前预检异常: {exc}")
                    return

                if not scenario_precheck.get("ok"):
                    reason = _summarize_precheck_failure(scenario_precheck)
                    execution.status = "ERROR"
                    execution.end_time = datetime.now()
                    execution.duration = 0
                    session.add(execution)
                    session.commit()
                    await manager.broadcast_log(ws_key, "error", f"❌ 运行前预检未通过: {reason}")
                    await manager.send_message(
                        ws_key,
                        {
                            "type": "run_complete",
                            "success": False,
                            "status": "ERROR",
                            "summary": f"运行前预检未通过: {reason}",
                            "execution_id": execution.id,
                        },
                    )
                    return

                cross_summary = await _run_in_blocking_executor(
                    blocking_executor,
                    _execute_cross_platform_scenario_ws,
                    scenario_id=scenario_id,
                    execution_id=execution.id,
                    device_serial=device_serial_ws,
                    start_time=start_time,
                    env_id=env_id,
                )

                event_iter = _iter_cross_platform_event_infos(
                    raw_results=cross_summary.get("raw_results", []),
                    cases_results=cross_summary.get("cases_results", []),
                )
                while True:
                    try:
                        event_info = next(event_iter)
                    except StopIteration:
                        break
                    await _broadcast_ws_scenario_event(ws_key, event_info)

                await _broadcast_ws_execution_complete(ws_key, execution.id, cross_summary)
                return

            # 2. Prepare Runner
            try:
                legacy_ws_state = await _run_in_blocking_executor(
                    blocking_executor,
                    _LegacyScenarioWsExecutionState,
                    scenario_id=scenario_id,
                    execution_id=execution.id,
                    env_id=env_id,
                    device_serial=device_serial,
                )
                device_serial_ws = await _run_in_blocking_executor(
                    blocking_executor,
                    legacy_ws_state.prepare,
                )
                await manager.broadcast_log(ws_key, "info", "✅ 设备连接成功")
            except Exception as e:
                await manager.broadcast_log(ws_key, "error", f"❌ 设备连接失败: {e}")
                execution.status = "ERROR"
                execution.end_time = datetime.now()
                session.add(execution)
                session.commit()
                return

            while True:
                next_result = await _run_in_blocking_executor(
                    blocking_executor,
                    legacy_ws_state.next_event,
                )
                if next_result.get("done"):
                    final_summary = next_result.get("summary")
                    break
                event_info = next_result.get("event")
                await _broadcast_ws_scenario_event(ws_key, event_info)

            await _broadcast_ws_execution_complete(ws_key, execution.id, final_summary)

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected: {ws_key}")
    except Exception as e:
        logger.exception("场景 WebSocket 执行异常: ws_key=%s scenario_id=%s", ws_key, scenario_id)
        await manager.broadcast_log(ws_key, "error", f"❌ 系统异常: {str(e)}")
    finally:
        try:
            if legacy_ws_state is not None:
                await _run_in_blocking_executor(
                    blocking_executor,
                    legacy_ws_state.close,
                )
        except Exception as e:
            logger.warning("关闭场景 WebSocket 后台执行上下文失败: ws_key=%s error=%s", ws_key, e)
        # ★ 恢复设备状态 并 清除中止事件
        try:
            from sqlmodel import Session as SQLSession
            with SQLSession(engine) as s:
                if device_serial_ws:
                    restore_device_status_after_execution(s, device_serial_ws)
        except Exception as e:
            logger.warning("恢复设备状态失败（场景 WebSocket）: device=%s error=%s", device_serial_ws, e)
        if device_serial_ws:
            unregister_device_abort(device_serial_ws)
        blocking_executor.shutdown(wait=True)
        manager.disconnect(websocket, ws_key)
