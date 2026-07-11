from typing import Any, Dict, List, Optional
import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select, func
from datetime import datetime

from backend.database import get_session, engine
from backend.cross_platform_execution import (
    precheck_case_execution,
    restore_device_status_after_execution,
)
from backend.models import TestScenario, ScenarioStep, User, TestCase, TestExecution
from backend.run_control import (
    ABORTED_STATUS,
    QUEUED_STATUS,
    registry,
    unregister_device_abort,
)
from backend.execution_limiter import get_execution_limiter
from backend.scenario_execution import (
    _execute_cross_platform_scenario_core,
    _merge_case_variables_with_context,
    _prepare_cross_platform_device_execution,
    _resolve_device_meta,
    _schedule_concurrent_runs,
    scenario_queue_task_id,
)
from backend.scenario_execution import execute_scenario_batch_background as execute_scenario_batch_background  # noqa: F401  执行链路迁移后的兼容再导出（backend.api.tasks 延迟导入）
from backend.schemas import (
    PaginatedTestScenarioRead,
    ScenarioRunRequest,
    ScenarioStepCreate,
    ScenarioStepRead,
    TestScenarioCreate,
    TestScenarioRead,
)
from backend.api import deps
import uuid
import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial

router = APIRouter()
logger = logging.getLogger(__name__)


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
    folder_id: Optional[int] = None,
    session: Session = Depends(get_session)
):
    """List scenarios with pagination and filtering (keyword / folder_id)"""
    from sqlalchemy.orm import aliased
    Creator = aliased(User)
    Updater = aliased(User)

    query = session.query(TestScenario, Creator.full_name, Creator.username, Updater.full_name, Updater.username)\
        .outerjoin(Creator, TestScenario.user_id == Creator.id)\
        .outerjoin(Updater, TestScenario.updater_id == Updater.id)

    if keyword:
        query = query.filter(TestScenario.name.contains(keyword))
    if folder_id is not None:
        query = query.filter(TestScenario.folder_id == folder_id)

    count_query = session.query(func.count(TestScenario.id))
    if keyword:
        count_query = count_query.filter(TestScenario.name.contains(keyword))
    if folder_id is not None:
        count_query = count_query.filter(TestScenario.folder_id == folder_id)
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
def delete_scenario(
    scenario_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    """Delete a scenario"""
    scenario = session.get(TestScenario, scenario_id)
    if not scenario:
        raise HTTPException(status_code=404, detail="Scenario not found")
    deps.ensure_owner_or_admin(scenario.user_id, current_user)

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
            # 跨端链路必须显式指定设备。
            blocked_prechecks.append(
                {"device_serial": serial, "reason": "请选择执行设备"}
            )
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

    limiter = get_execution_limiter()
    execution_tickets = []
    scheduled_serials = []
    runs_payload: List[Dict[str, Any]] = []
    try:
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

            # 并发超限时不再 429 拒绝，而是进入 FIFO 等待队列。
            ticket = limiter.enqueue(
                user_id=current_user.id,
                device_serial=serial,
                task_id=scenario_queue_task_id(execution.id),
                kind="scenario",
                target_id=scenario_id,
            )
            queued = ticket.lease is None
            if queued:
                execution.status = QUEUED_STATUS
                session.add(execution)
                session.commit()

            execution_ids.append(execution.id)
            execution_tickets.append(ticket)
            scheduled_serials.append(serial)
            runs_payload.append(
                {
                    "execution_id": execution.id,
                    "device_serial": serial,
                    "queued": queued,
                    "queue_position": ticket.initial_queue_position if queued else None,
                }
            )

        asyncio.create_task(_schedule_concurrent_runs(
            execution_ids=execution_ids,
            scenario_id=scenario_id,
            device_serials=scheduled_serials,
            env_id=request.env_id,
            execution_tickets=execution_tickets,
        ))
    except Exception:
        for ticket in execution_tickets:
            ticket.cancel()
        raise

    queued_count = sum(1 for item in runs_payload if item["queued"])
    return {
        "message": "Batch execution started",
        "batch_id": batch_id,
        "execution_ids": execution_ids,
        "blocked_prechecks": blocked_prechecks,
        "runs": runs_payload,
        "queued_count": queued_count,
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
            display = step_entry.get("report_display") if isinstance(step_entry.get("report_display"), dict) else {}
            action_desc = display.get("display_text") or step_entry.get("description") or step_entry.get("action") or "unknown"
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


@router.websocket("/ws/run/{scenario_id}")
async def websocket_run_scenario(websocket: WebSocket, scenario_id: int, env_id: Optional[int] = None, device_serial: Optional[str] = None):
    """WebSocket endpoint: Run scenario with real-time logs"""
    ws_key = f"scenario:{scenario_id}"
    await manager.connect(websocket, ws_key)

    blocking_executor = ThreadPoolExecutor(max_workers=1)
    device_serial_ws = device_serial
    execution_id_ws = None
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
            batch_id = str(uuid.uuid4())
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
                batch_id=batch_id,
                batch_name=f"{scenario.name} 实时运行",
            )
            session.add(execution)
            session.commit()
            session.refresh(execution)
            execution_id_ws = execution.id

            # Get steps (ordered)
            steps_db = session.exec(select(ScenarioStep).where(ScenarioStep.scenario_id == scenario_id).order_by(ScenarioStep.order)).all()
            total_steps = len(steps_db)

            await manager.broadcast_log(ws_key, "info", f"🎬 开始执行场景: {scenario.name} (共 {total_steps} 个步骤)")
            await manager.send_message(
                ws_key,
                {
                    "type": "run_start",
                    "status": "RUNNING",
                    "scenario_id": scenario_id,
                    "execution_id": execution.id,
                    "batch_id": batch_id,
                    "device_serial": device_serial,
                    "total_steps": total_steps,
                    "timestamp": datetime.now().isoformat(),
                },
            )

            if not device_serial:
                # 跨端链路必须显式指定设备。
                execution.status = "ERROR"
                execution.end_time = datetime.now()
                execution.duration = 0
                session.add(execution)
                session.commit()
                await manager.broadcast_log(ws_key, "error", "❌ 请选择执行设备")
                await manager.send_message(
                    ws_key,
                    {
                        "type": "run_complete",
                        "success": False,
                        "status": "ERROR",
                        "summary": "请选择执行设备",
                        "execution_id": execution.id,
                    },
                )
                return

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

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected: {ws_key}")
    except Exception as e:
        logger.exception("场景 WebSocket 执行异常: ws_key=%s scenario_id=%s", ws_key, scenario_id)
        await manager.broadcast_log(ws_key, "error", f"❌ 系统异常: {str(e)}")
    finally:
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
        if execution_id_ws is not None:
            for record in registry.active(kind="scenario", target_id=scenario_id):
                if record.execution_id == execution_id_ws:
                    registry.complete(
                        record.run_id,
                        ABORTED_STATUS if record.abort_event.is_set() else None,
                    )
        blocking_executor.shutdown(wait=True)
        manager.disconnect(websocket, ws_key)
