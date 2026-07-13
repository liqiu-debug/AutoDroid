"""
场景执行编排

从 backend.api.scenarios 拆出的执行链路（无路由）：
- 单设备同步执行（_run_single_device_sync 及其 lease 包装）
- 并发批次调度（_schedule_concurrent_runs / execute_scenario_batch_background）
- 跨端场景运行（_run_scenario_cross_platform）与执行收尾（_finalize_scenario_execution）

结果转换与持久化见 backend.scenario_results，路由端点保留在 backend.api.scenarios。
"""
import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlmodel import Session, select

from backend.cross_platform_execution import (
    restore_device_status_after_execution,
    run_case_with_standard_runner,
)
from backend.database import engine
from backend.execution_limiter import (
    QueueAbortedError,
    QueueTimeoutError,
    get_execution_limiter,
)
from backend.models import Device, ScenarioStep, TestCase, TestExecution, TestScenario
from backend.run_control import (
    ABORTED_STATUS,
    QUEUED_STATUS,
    register_device_abort,
    registry,
    unregister_device_abort,
)
from backend.scenario_results import (
    _build_cases_results_from_raw_results,
    _build_scenario_summary_message,
    _convert_cross_result_to_legacy_case_result,
    _summarize_cases_results,
)
from backend.step_contract import normalize_error_strategy

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


def _finalize_scenario_execution(
    *,
    session: Session,
    scenario: TestScenario,
    execution: TestExecution,
    cases_results: List[Dict[str, Any]],
    start_time: datetime,
    status_override: Optional[str] = None,
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
    if status_override == ABORTED_STATUS:
        summary_msg = "执行已被用户终止"

    scenario_status = str(status_override or summary["scenario_status"]).upper()
    if status_override == ABORTED_STATUS:
        summary["last_failed_step_name"] = "用户终止"

    scenario.last_run_status = scenario_status
    scenario.last_run_time = end_time
    scenario.last_run_duration = int(total_duration)
    scenario.last_execution_id = execution.id
    scenario.last_executor = execution.executor_name
    scenario.last_failed_step = summary["last_failed_step_name"]
    if report_id:
        scenario.last_report_id = report_id
    session.add(scenario)

    execution.status = scenario_status
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
        "scenario_status": scenario_status,
    }


def _prepare_cross_platform_device_execution(
    *,
    session: Session,
    execution: TestExecution,
    device_serial: str,
):
    abort_event = register_device_abort(device_serial)
    run_record = registry.register(
        kind="scenario",
        target_id=execution.scenario_id,
        batch_id=execution.batch_id,
        device_serial=device_serial,
        abort_event=abort_event,
        execution_id=execution.id,
    )
    setattr(abort_event, "_autodroid_run_id", run_record.run_id)
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
        status_override=ABORTED_STATUS if abort_event and abort_event.is_set() else None,
    )

    return {
        "result": result,
        "raw_results": raw_results,
        "cases_results": cases_results,
        **final_summary,
    }


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
        if str(execution.status or "").upper() == ABORTED_STATUS:
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

            if not device_serial:
                # 跨端链路必须显式指定设备。
                logger.error(
                    "cross-platform scenario execution requires device_serial: scenario_id=%s execution_id=%s",
                    scenario_id,
                    execution_id,
                )
                execution.status = "ERROR"
                execution.end_time = datetime.now()
                execution.duration = 0
                session.add(execution)
                session.commit()
                return

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
        except Exception as e:
            logger.exception("background scenario execution failed: scenario_id=%s execution_id=%s", scenario_id, execution.id if execution else None)
            was_aborted = bool(abort_event and abort_event.is_set())
            scenario.last_run_status = ABORTED_STATUS if was_aborted else "FAIL"
            scenario.last_execution_id = execution.id if 'execution' in locals() else None
            if 'start_time' in locals():
                scenario.last_run_duration = int((datetime.now() - start_time).total_seconds())
            session.add(scenario)

            # Fail the execution record if exists
            if 'execution' in locals():
                execution.status = ABORTED_STATUS if was_aborted else "ERROR"
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
            registry.complete(
                getattr(abort_event, "_autodroid_run_id", None),
                ABORTED_STATUS if abort_event and abort_event.is_set() else None,
            )

def scenario_queue_task_id(execution_id: int) -> str:
    """场景执行在限流队列中的 task_id（供排队位置查询）。"""
    return f"scenario-exec:{execution_id}"


def _run_single_device_sync_queued(
    execution_id: int,
    scenario_id: int,
    device_serial: Optional[str] = None,
    env_id: Optional[int] = None,
    ticket=None,
):
    """带排队语义的单设备执行入口：无空闲槽位时进入 FIFO 队列等待。"""
    limiter = get_execution_limiter()
    if ticket is None:
        # 定时任务等未预先入队的调用方在此入队
        ticket = limiter.enqueue(
            user_id=0,
            device_serial=device_serial,
            task_id=scenario_queue_task_id(execution_id),
            kind="scenario",
            target_id=scenario_id,
        )

    lease = ticket.lease
    if lease is None:
        lease = _wait_for_queued_scenario_slot(
            ticket=ticket,
            execution_id=execution_id,
            scenario_id=scenario_id,
            device_serial=device_serial,
        )
        if lease is None:
            return None

    try:
        return _run_single_device_sync(
            execution_id=execution_id,
            scenario_id=scenario_id,
            device_serial=device_serial,
            env_id=env_id,
        )
    finally:
        lease.release()


def _wait_for_queued_scenario_slot(
    *,
    ticket,
    execution_id: int,
    scenario_id: int,
    device_serial: Optional[str],
):
    """排队等待场景执行槽位；返回 lease，取消/超时则完成收尾并返回 None。"""
    from sqlmodel import Session as SQLSession

    limiter = get_execution_limiter()

    batch_id = None
    with SQLSession(engine) as session:
        execution = session.get(TestExecution, execution_id)
        if not execution or str(execution.status or "").upper() == ABORTED_STATUS:
            ticket.cancel()
            return None
        batch_id = execution.batch_id
        if str(execution.status or "").upper() != QUEUED_STATUS:
            execution.status = QUEUED_STATUS
            session.add(execution)
            session.commit()

    # 排队中的任务尚未占用设备：注册独立中止事件的 QUEUED run 记录，
    # 供报告中心展示与 /runs/cancel 终止排队。
    abort_event = threading.Event()
    run_record = registry.register(
        kind="scenario",
        target_id=scenario_id,
        batch_id=batch_id,
        device_serial=device_serial,
        abort_event=abort_event,
        execution_id=execution_id,
        status=QUEUED_STATUS,
        metadata={"limiter_task_id": ticket.task_id},
    )
    try:
        lease = ticket.wait(timeout=limiter.queue_timeout, abort_event=abort_event)
    except QueueAbortedError:
        _mark_scenario_execution_not_run(execution_id, ABORTED_STATUS, "排队中被终止")
        registry.complete(run_record.run_id, ABORTED_STATUS)
        return None
    except QueueTimeoutError as exc:
        _mark_scenario_execution_not_run(execution_id, "ERROR", str(exc))
        registry.complete(run_record.run_id, "ERROR")
        return None

    # 获得槽位：移除排队记录；执行链路随后注册新的 RUNNING 记录
    registry.complete(run_record.run_id)
    return lease


def _mark_scenario_execution_not_run(execution_id: int, status: str, reason: str) -> None:
    from sqlmodel import Session as SQLSession

    with SQLSession(engine) as session:
        execution = session.get(TestExecution, execution_id)
        if not execution:
            return
        execution.status = status
        execution.end_time = datetime.now()
        session.add(execution)
        session.commit()
        logger.warning(
            "scenario execution did not run: execution_id=%s status=%s reason=%s",
            execution_id,
            status,
            reason,
        )


async def _schedule_concurrent_runs(
    execution_ids: List[int],
    scenario_id: int,
    device_serials: List[str],
    env_id: Optional[int] = None,
    execution_tickets: Optional[List[Any]] = None,
):
    """使用 ThreadPoolExecutor 并发执行每个设备的测试"""
    loop = asyncio.get_running_loop()

    # 创建线程池，最大 worker 数量与设备数量一致，保证并发
    max_workers = len(device_serials) if device_serials else 1
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        tasks = []
        for index, (exec_id, serial) in enumerate(zip(execution_ids, device_serials)):
            ticket = (
                execution_tickets[index]
                if execution_tickets is not None and index < len(execution_tickets)
                else None
            )
            # run_in_executor 将同步阻塞的 Runner 任务放入线程池调度
            task = loop.run_in_executor(
                executor,
                _run_single_device_sync_queued,  # 传入同步目标函数
                exec_id,
                scenario_id,
                serial,
                env_id,
                ticket,
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
