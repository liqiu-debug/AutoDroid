"""AutoDroid 设备端执行 API（设备注册 / 心跳 / 执行结果回传）。

旧的「导出 zip 脚本包 → 设备任务队列 → 设备端 Agent 领取执行」链路已下线，
导出、预检、任务队列与脚本包下载端点均已删除；设备端执行改由 App 直跑链路
（/app-runs/*/script 下发 + /device-runs/report 回传）承担。

当前路由：
- device_router（设备侧，API Token 或 JWT）：设备注册 / 心跳 / 退出登录、
  执行结果回传（落库 TestExecution/TestResult -> HTML 报告 -> 复用飞书通知）

结果回传链路复用平台既有设施：
- 持久化: backend/scenario_results._persist_case_result_and_build_case_report
- 报告:   backend/report_generator.report_generator
- 通知:   backend/notification_service.NotificationService.send_report_card
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from backend.api import deps
from backend.autojs_export.generator import load_case_steps
from backend.database import get_session
from backend.device_presence import is_app_device
from backend.execution_errors import suggestion_for
from backend.ios_models import ios_marketing_name
from backend.models import (
    Device,
    ScenarioStep,
    TestCase,
    TestExecution,
    TestScenario,
    User,
)
from backend.notification_service import NotificationService
from backend.report_generator import report_generator
from backend.scenario_results import _persist_case_result_and_build_case_report
from backend.step_contract import normalize_platform, standard_step_to_legacy

logger = logging.getLogger(__name__)

device_router = APIRouter()

DEVICE_SERIAL_PREFIX = "autojs6:"
# iOS 端（AutoDroid-WDA 设备端执行器）设备 serial 前缀
DEVICE_SERIAL_PREFIX_IOS = "ios:"


# --------------------------------------------------------------------------- #
#  Schemas
# --------------------------------------------------------------------------- #
LEGACY_IOS_APP_DEFAULT_NAMES = frozenset({"AutoDroid · iPhone", "AutoDroid · iPad", "AutoDroid · iPod touch"})


class DeviceRegisterPayload(BaseModel):
    executor_status: Optional[str] = None
    platform: Optional[str] = None  # android（默认）| ios
    android_id: Optional[str] = None
    serial: Optional[str] = None
    # iOS：设备 UDID（平台侧能拿到时优先使用；沙箱内 App 拿不到则退回 IDFV）
    udid: Optional[str] = None
    name: Optional[str] = None
    model: Optional[str] = None
    brand: Optional[str] = None
    android_version: Optional[str] = None
    os_version: Optional[str] = None
    resolution: Optional[str] = None
    battery: Optional[int] = None


class DeviceReportStep(BaseModel):
    order: int
    action: Optional[str] = None
    status: str = "FAIL"
    duration: float = 0.0
    attempts: int = 1
    error: Optional[str] = None
    error_code: Optional[str] = None
    screenshot_base64: Optional[str] = None
    output: Optional[Dict[str, Any]] = None


class DeviceReportCase(BaseModel):
    case_id: Optional[int] = None
    name: Optional[str] = None
    alias: Optional[str] = None
    success: bool = False
    steps: List[DeviceReportStep] = Field(default_factory=list)
    exported_variables: Optional[Dict[str, Any]] = None


class DeviceReportPayload(BaseModel):
    # 旧任务链路（Agent 领取任务）的任务标识，已随任务队列下线：
    # 保留字段只为给残留的旧客户端返回明确报错，不再查任务表。
    task_id: Optional[str] = None
    # 上报来源平台：android（默认，兼容既有 AutoDroid App）| ios（WDA fork 设备端执行器）
    platform: Optional[str] = None
    # App 直跑：客户端生成的 run_id 做幂等标识（必填）
    run_id: Optional[str] = None
    # 是否发送飞书通知（默认发送；App 可逐次关闭）
    notify: bool = False  # 默认不发（与 Web 一致）：仅显式开启/定时任务勾选才发飞书
    format_version: Optional[int] = None
    kind: Optional[str] = None
    case_id: Optional[int] = None
    scenario_id: Optional[int] = None
    device: Optional[Dict[str, Any]] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    success: bool = False
    aborted: bool = False
    cases: List[DeviceReportCase] = Field(default_factory=list)
    logs: Optional[List[str]] = None


# --------------------------------------------------------------------------- #
#  设备侧：注册 / 心跳 / 退出登录
# --------------------------------------------------------------------------- #
def _device_serial_for(payload: DeviceRegisterPayload) -> str:
    platform = normalize_platform(payload.platform)
    identity = (
        str(payload.udid or "").strip()
        or str(payload.android_id or "").strip()
        or str(payload.serial or "").strip()
        or str(payload.name or "").strip()
        or "unknown"
    )
    if identity.startswith(DEVICE_SERIAL_PREFIX) or identity.startswith(DEVICE_SERIAL_PREFIX_IOS):
        return identity
    prefix = DEVICE_SERIAL_PREFIX_IOS if platform == "ios" else DEVICE_SERIAL_PREFIX
    return f"{prefix}{identity}"


@device_router.post("/register")
def register_device(
    payload: DeviceRegisterPayload,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    """AutoDroid 设备注册 / 心跳。设备进入平台设备中心。"""
    platform = normalize_platform(payload.platform)
    serial = _device_serial_for(payload)
    device = session.exec(select(Device).where(Device.serial == serial)).first()
    now = datetime.now()
    if not device:
        device = Device(serial=serial, platform=platform, status="IDLE")
    device.platform = platform
    device.connection_type = "wda" if platform == "ios" else "autojs6"
    device.app_last_seen_at = now
    device.app_logged_out_at = None
    device.app_owner_id = current_user.id
    device.app_executor_status = payload.executor_status if payload.executor_status in {"READY", "NOT_READY", "RUNNING"} else "UNKNOWN"
    device.status = "BUSY" if device.app_executor_status == "RUNNING" else "IDLE"
    if payload.model:
        # iOS App 上报机型标识（iPhone15,2）→ 市场名；Android 原样
        device.model = ios_marketing_name(payload.model) if platform == "ios" else str(payload.model)
    if payload.brand:
        device.brand = str(payload.brand)
    os_version = str(payload.os_version or payload.android_version or "").strip()
    if os_version:
        device.os_version = os_version
    if payload.android_version:
        device.android_version = str(payload.android_version)
    if payload.resolution:
        device.resolution = str(payload.resolution)
    if payload.name:
        device.custom_name = str(payload.name)
    elif device.custom_name in LEGACY_IOS_APP_DEFAULT_NAMES:
        # 旧版 iOS App 每次心跳上报「AutoDroid · iPhone」（UIDevice.model 只有 iPhone/iPad），
        # 盖住了型号；新版不再上报 name，清掉旧默认名让型号显示出来
        device.custom_name = None
    if device.status == "OFFLINE":
        device.status = "IDLE"
    device.updated_at = now
    session.add(device)
    session.commit()
    return {
        "device_serial": serial,
        "server_time": now.isoformat(),
    }


@device_router.post("/logout")
def logout_device(payload: DeviceRegisterPayload, session: Session = Depends(get_session),
                  current_user: User = Depends(deps.get_current_user)):
    device = session.exec(select(Device).where(Device.serial == _device_serial_for(payload))).first()
    if device and is_app_device(device):
        if device.app_owner_id != current_user.id:
            raise HTTPException(403, "只能退出当前账号注册的 App 设备")
        device.app_logged_out_at = datetime.now()
        device.status = "OFFLINE"
        device.app_executor_status = "UNKNOWN"
        session.add(device)
        session.commit()
    return {"ok": True}


# --------------------------------------------------------------------------- #
#  设备侧：结果回传
# --------------------------------------------------------------------------- #
def _device_display(payload: DeviceReportPayload) -> str:
    device = payload.device or {}
    model = str(device.get("model") or "").strip()
    identity = str(device.get("android_id") or device.get("serial") or "").strip()
    parts = [part for part in (model, identity) if part]
    if parts:
        return "AutoDroid · " + " · ".join(parts)
    return "AutoDroid 设备"


def _serial_from_device(payload: DeviceReportPayload) -> Optional[str]:
    """App 直跑：从上报的设备信息推导平台设备 serial（与注册接口规则一致）。"""
    device = payload.device or {}
    identity = str(device.get("android_id") or device.get("serial") or "").strip()
    if not identity:
        return None
    if identity.startswith(DEVICE_SERIAL_PREFIX) or identity.startswith(DEVICE_SERIAL_PREFIX_IOS):
        return identity
    prefix = DEVICE_SERIAL_PREFIX_IOS if normalize_platform(payload.platform) == "ios" else DEVICE_SERIAL_PREFIX
    return f"{prefix}{identity}"


def _resolve_duration(payload: DeviceReportPayload) -> float:
    try:
        started = float(payload.started_at or 0)
        finished = float(payload.finished_at or 0)
        duration = (finished - started) / 1000.0
        if duration < 0:
            return 0.0
        return min(duration, 24 * 3600)
    except Exception:
        return 0.0


def _build_case_result(
    session: Session,
    case: TestCase,
    reported: DeviceReportCase,
) -> Dict[str, Any]:
    """设备上报的用例结果 -> 平台 legacy case_result（供持久化管线消费）。"""
    standard_steps = load_case_steps(session, case)
    steps_by_order = {int(step.get("order") or 0): step for step in standard_steps}

    converted_steps: List[Dict[str, Any]] = []
    has_warning = False
    for reported_step in reported.steps:
        standard = steps_by_order.get(int(reported_step.order or 0))
        if standard:
            legacy_step = standard_step_to_legacy(standard)
        else:
            legacy_step = {
                "action": reported_step.action or "system",
                "selector": None,
                "selector_type": None,
                "value": None,
                "options": {},
                "description": "设备端上报步骤（平台未找到对应步骤）",
                "error_strategy": "ABORT",
                "timeout": 10,
            }
        status = str(reported_step.status or "").upper()
        entry: Dict[str, Any] = {
            "step": legacy_step,
            "success": status in ("PASS", "SKIP"),
            "duration": float(reported_step.duration or 0),
        }
        if reported_step.error:
            entry["error"] = reported_step.error
        if reported_step.error_code:
            entry["error_code"] = reported_step.error_code
            suggestion = suggestion_for(reported_step.error_code)
            if suggestion:
                entry["suggestion"] = suggestion
        attempts = int(reported_step.attempts or 1)
        if attempts > 1:
            entry["attempts"] = attempts
        if reported_step.output:
            entry["output"] = reported_step.output
        if reported_step.screenshot_base64:
            entry["screenshot"] = reported_step.screenshot_base64
        if status == "WARNING":
            entry["success"] = False
            entry["is_warning"] = True
            has_warning = True
        if status == "SKIP":
            entry["is_skipped"] = True
        converted_steps.append(entry)

    result: Dict[str, Any] = {
        "case_id": case.id,
        "success": bool(reported.success),
        "steps": converted_steps,
        "exported_variables": {
            str(key): "" if value is None else str(value)
            for key, value in dict(reported.exported_variables or {}).items()
        },
    }
    if has_warning and result["success"]:
        result["is_warning"] = True
    return result


def _aggregate_status(case_entries: List[Dict[str, Any]], aborted: bool) -> str:
    if aborted:
        return "ABORTED"
    statuses = [str(entry.get("status") or "") for entry in case_entries]
    if any(status == "failed" for status in statuses):
        return "FAIL"
    if statuses and all(status == "skipped" for status in statuses):
        return "WARNING"
    if any(status in ("warning", "skipped") for status in statuses):
        return "WARNING"
    return "PASS"


def _process_device_report(
    session: Session,
    payload: DeviceReportPayload,
    current_user: User,
) -> Dict[str, Any]:
    # Authentication may have opened a read transaction. Acquire SQLite's write
    # reservation before looking up the run, including across server processes.
    # Keep persistence atomic so simultaneous uploads cannot both create it.
    session.commit()
    try:
        connection = session.connection()
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        result = _persist_device_report(session, payload, current_user)
        session.commit()
        return result
    except Exception:
        session.rollback()
        raise


def _persist_device_report(
    session: Session,
    payload: DeviceReportPayload,
    current_user: User,
) -> Dict[str, Any]:
    # 上报来源：App 直跑（run_id）按 run_id 幂等落库。
    # 旧的 Agent 任务链路（task_id）已下线，残留的旧客户端收到明确报错。
    if payload.task_id:
        raise HTTPException(
            status_code=400,
            detail="旧任务链路已下线（task_id 无效），请改用 App 直跑上报：带 run_id",
        )
    if not payload.run_id:
        raise HTTPException(status_code=400, detail="上报需要 run_id（App 直跑）")
    if payload.case_id:
        kind = "case"
    elif payload.scenario_id:
        kind = "scenario"
    else:
        raise HTTPException(status_code=400, detail="App 直跑上报需要 case_id 或 scenario_id")
    ref_case_id = payload.case_id
    ref_scenario_id = payload.scenario_id

    # A lost HTTP response can make either app upload the same completed run
    # again. Return the original record without generating a second notification.
    previous = session.exec(
        select(TestExecution).where(TestExecution.batch_id == payload.run_id)
    ).all()
    if any(item.executor_id != current_user.id for item in previous):
        raise HTTPException(status_code=409, detail="运行编号已被其他账号使用")
    completed = next((item for item in previous if item.status != "RUNNING"), None)
    if completed is not None:
        return {"execution_id": completed.id, "status": completed.status, "report_id": completed.report_id}

    scenario: Optional[TestScenario] = None
    case: Optional[TestCase] = None
    if kind == "case":
        case = session.get(TestCase, ref_case_id) if ref_case_id else None
        if not case:
            raise HTTPException(status_code=404, detail="任务关联用例不存在")
        display_name = case.name
    else:
        scenario = session.get(TestScenario, ref_scenario_id) if ref_scenario_id else None
        if not scenario:
            raise HTTPException(status_code=404, detail="任务关联场景不存在")
        display_name = scenario.name

    reported_cases = list(payload.cases or [])
    if kind == "case" and not reported_cases:
        raise HTTPException(status_code=400, detail="上报数据缺少用例结果")

    # 场景/批量：按场景顺序对齐上报结果（缺失用例生成系统失败占位）
    ordered_reports: List[tuple[Optional[TestCase], DeviceReportCase]] = []
    if kind == "case":
        ordered_reports.append((case, reported_cases[0]))
    else:
        scenario_steps = session.exec(
            select(ScenarioStep)
            .where(ScenarioStep.scenario_id == scenario.id)
            .order_by(ScenarioStep.order)
        ).all()
        remaining = list(reported_cases)
        for scenario_step in scenario_steps:
            # Each scenario occurrence consumes one result, including repeated
            # references to the same case. Never borrow another case's result.
            index = next((i for i, item in enumerate(remaining)
                          if item.case_id == scenario_step.case_id), None)
            if index is None:
                index = next((i for i, item in enumerate(remaining) if item.case_id is None), None)
            matched = remaining.pop(index) if index is not None else None
            if matched is not None and not matched.alias:
                matched = matched.model_copy(update={"alias": scenario_step.alias})
            target_case = session.get(TestCase, scenario_step.case_id)
            if matched is None and target_case is not None:
                matched = DeviceReportCase(
                    case_id=target_case.id,
                    name=target_case.name,
                    alias=scenario_step.alias,
                    success=False,
                    steps=[DeviceReportStep(
                        order=0,
                        action="system",
                        status="FAIL",
                        error="设备端未上报该用例结果（执行中断）",
                        error_code="E2999_EXECUTION_ERROR",
                    )],
                )
            if target_case is not None and matched is not None:
                ordered_reports.append((target_case, matched))

    duration = _resolve_duration(payload)
    end_time = datetime.now()
    start_time = end_time - timedelta(seconds=duration)

    from backend.models import TestResult

    def _delete_execution_with_results(execution_id: int) -> None:
        old_results = session.exec(
            select(TestResult).where(TestResult.execution_id == execution_id)
        ).all()
        for row in old_results:
            session.delete(row)
        old_execution = session.get(TestExecution, execution_id)
        if old_execution:
            session.delete(old_execution)

    # Only incomplete records are rebuilt; completed runs returned above.
    for old_execution in previous:
        _delete_execution_with_results(old_execution.id)
    if previous:
        session.flush()

    device_serial = _serial_from_device(payload)
    batch_id = payload.run_id
    executor_id = current_user.id
    # 报告中心标题展示用例/场景名；执行方式（App/平台）由 run_source 表达。
    batch_name = display_name

    report_platform = normalize_platform(payload.platform)

    execution = TestExecution(
        scenario_id=scenario.id if scenario else 0,
        scenario_name=display_name,
        status="RUNNING",
        executor_id=executor_id,
        executor_name=current_user.full_name or current_user.username,
        device_serial=device_serial,
        platform=report_platform,
        device_info=_device_display(payload),
        batch_id=batch_id,
        batch_name=batch_name,
        run_source="app",
    )
    execution.start_time = start_time
    session.add(execution)
    session.flush()
    session.refresh(execution)

    case_entries: List[Dict[str, Any]] = []
    global_step_order = 1
    for target_case, reported in ordered_reports:
        case_result = _build_case_result(session, target_case, reported)
        entry, global_step_order, _ = _persist_case_result_and_build_case_report(
            session=session,
            execution_id=execution.id,
            item={
                "case_name": reported.name or target_case.name,
                "alias": reported.alias,
            },
            case_result=case_result,
            global_step_order=global_step_order,
        )
        case_entries.append(entry)
    session.flush()

    overall_status = _aggregate_status(case_entries, bool(payload.aborted))

    report_id: Optional[str] = None
    try:
        if scenario:
            report_id = report_generator.generate_scenario_report(
                scenario_id=scenario.id,
                scenario_name=scenario.name,
                cases_results=case_entries,
                start_time=start_time,
                end_time=end_time,
            )
        else:
            report_id = report_generator.generate_report(
                case_id=case.id,
                case_name=case.name,
                steps_results=case_entries[0]["steps"] if case_entries else [],
                start_time=start_time,
                end_time=end_time,
                variables=[
                    {"key": key, "value": value}
                    for key, value in dict((case_entries[0].get("exported_variables") or {})).items()
                ] if case_entries else None,
            )
    except Exception as exc:
        logger.warning("AutoDroid 报告生成失败: run=%s error=%s", payload.run_id, exc)

    execution.status = overall_status
    execution.end_time = end_time
    execution.duration = duration
    if report_id:
        execution.report_id = report_id
    session.add(execution)

    # 更新用例/场景最近运行状态（镜像平台行为）
    if case is not None:
        case.last_run_status = overall_status
        case.last_run_time = end_time
        session.add(case)
    if scenario is not None:
        scenario.last_run_status = overall_status
        scenario.last_run_time = end_time
        scenario.last_run_duration = int(duration)
        scenario.last_execution_id = execution.id
        if report_id:
            scenario.last_report_id = report_id
        session.add(scenario)

    total_steps = sum(len(entry.get("steps") or []) for entry in case_entries)
    passed_steps = sum(
        1
        for entry in case_entries
        for step in (entry.get("steps") or [])
        if step.get("status") == "success"
    )

    session.commit()

    # 飞书通知（复用平台卡片；失败不影响上报结果；App 可通过 notify=false 关闭）
    if payload.notify:
        try:
            errors = [
                str(step.get("error"))
                for entry in case_entries
                for step in (entry.get("steps") or [])
                if step.get("error")
            ]
            device_display = execution.device_info or "AutoDroid 设备"
            NotificationService.send_report_card(
                task_name=f"[AutoDroid] {display_name}",
                execution_id=execution.id,
                status=overall_status,
                total=total_steps,
                passed=passed_steps,
                failed=total_steps - passed_steps,
                duration_seconds=duration,
                errors=errors,
                device_count=1,
                passed_devices=[device_display] if overall_status == "PASS" else [],
                failed_devices=[] if overall_status == "PASS" else [device_display],
            )
        except Exception as exc:
            logger.error("AutoDroid 飞书通知发送失败: run=%s error=%s", payload.run_id, exc)
    else:
        logger.info("上报 notify=false，跳过飞书通知: run=%s", payload.run_id)

    return {
        "execution_id": execution.id,
        "status": overall_status,
        "report_id": report_id,
    }


@device_router.post("/report")
def submit_device_report(
    payload: DeviceReportPayload,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    """设备端执行结果回传：落库 -> 生成报告 -> 飞书通知。"""
    result = _process_device_report(session, payload, current_user)
    logger.info(
        "AutoDroid 结果回传完成: run=%s execution=%s status=%s",
        payload.run_id,
        result.get("execution_id"),
        result.get("status"),
    )
    return result
