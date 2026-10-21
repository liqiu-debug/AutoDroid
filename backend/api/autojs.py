"""AutoDroid 脚本导出 + 设备端执行 API。

两组路由：
- router（浏览器侧，JWT）：用例/场景 -> AutoDroid 脚本包导出与预检
- device_router（设备侧，API Token 或 JWT）：设备注册、任务领取、
  脚本包下载、执行结果回传（落库 TestExecution/TestResult -> HTML 报告
  -> 复用飞书通知）

结果回传链路复用平台既有设施：
- 持久化: backend/scenario_results._persist_case_result_and_build_case_report
- 报告:   backend/report_generator.report_generator
- 通知:   backend/notification_service.NotificationService.send_report_card
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from sqlmodel import Session, or_, select

from backend.api import deps
from backend.autojs_export import packager
from backend.autojs_export.generator import load_case_steps
from backend.autojs_export.packager import PROJECT_ROOT
from backend.autojs_export.precheck import precheck_case_script
from backend.database import get_session
from backend.device_presence import is_app_device
from backend.execution_errors import suggestion_for
from backend.models import (
    CaseFolder,
    Device,
    DeviceRunTask,
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

router = APIRouter()
device_router = APIRouter()

PACKAGE_DIR = PROJECT_ROOT / "uploads" / "autojs6_packages"
CLAIM_TIMEOUT = timedelta(minutes=15)
DEVICE_SERIAL_PREFIX = "autojs6:"
# iOS 端（AutoDroid-WDA 设备端执行器）设备 serial 前缀
DEVICE_SERIAL_PREFIX_IOS = "ios:"


# --------------------------------------------------------------------------- #
#  Schemas
# --------------------------------------------------------------------------- #
class BatchExportRequest(BaseModel):
    case_ids: List[int] = Field(default_factory=list)
    folder_id: Optional[int] = None
    name: Optional[str] = None
    env_id: Optional[int] = None
    platform: Optional[str] = None


class DeviceRunTaskCreate(BaseModel):
    kind: str  # case | scenario
    case_id: Optional[int] = None
    scenario_id: Optional[int] = None
    env_id: Optional[int] = None
    device_serial: Optional[str] = None
    name: Optional[str] = None
    platform: Optional[str] = None


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
    task_id: Optional[str] = None
    # 上报来源平台：android（默认，兼容既有 AutoDroid App）| ios（WDA fork 设备端执行器）
    platform: Optional[str] = None
    # App 直跑模式：无任务时用客户端生成的 run_id 做幂等标识
    run_id: Optional[str] = None
    # 是否发送飞书通知（默认发送；App 可逐次关闭）
    notify: bool = True
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
#  导出（浏览器侧）
# --------------------------------------------------------------------------- #
def _file_response(content: bytes, filename: str, media_type: str) -> Response:
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
        },
    )


def _load_case(session: Session, case_id: int) -> TestCase:
    case = session.get(TestCase, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="用例不存在")
    return case


@router.get("/cases/{case_id}/precheck")
def precheck_case(
    case_id: int,
    env_id: Optional[int] = None,
    platform: str = Query(default="android"),
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    """可导出性预检（不生成文件）。"""
    from backend.autojs_export.generator import build_case_script

    case = _load_case(session, case_id)
    platform = normalize_platform(platform)
    case_script = build_case_script(session, case, env_id=env_id, platform=platform)
    return precheck_case_script(case_script, project_root=PROJECT_ROOT, platform=platform)


@router.post("/cases/{case_id}/export")
def export_case(
    case_id: int,
    format: str = Query(default="zip", pattern="^(zip|file)$"),
    env_id: Optional[int] = None,
    platform: str = Query(default="android"),
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    """导出单用例脚本包（zip 目录包或单文件 .js）。"""
    case = _load_case(session, case_id)
    platform = normalize_platform(platform)
    if format == "file":
        result = packager.build_single_file(session, case, env_id=env_id, platform=platform)
        return _file_response(result["content"], result["filename"], "application/javascript")
    result = packager.build_case_package(session, case, env_id=env_id, platform=platform)
    return _file_response(result["content"], result["filename"], "application/zip")


def _collect_folder_case_ids(session: Session, folder_id: int) -> List[int]:
    """递归收集目录及其子目录下的用例。"""
    folder_ids = [folder_id]
    frontier = [folder_id]
    while frontier:
        current = frontier.pop()
        children = session.exec(
            select(CaseFolder).where(CaseFolder.parent_id == current)
        ).all()
        for child in children:
            if child.id not in folder_ids:
                folder_ids.append(child.id)
                frontier.append(child.id)
    cases = session.exec(select(TestCase).where(TestCase.folder_id.in_(folder_ids))).all()
    return [case.id for case in cases if case.id is not None]


@router.post("/cases/export-batch")
def export_cases_batch(
    request: BatchExportRequest,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    """批量导出：多选用例或按目录（递归）打包为一个 zip。"""
    case_ids: List[int] = []
    for case_id in request.case_ids or []:
        if case_id not in case_ids:
            case_ids.append(case_id)
    if request.folder_id:
        for case_id in _collect_folder_case_ids(session, request.folder_id):
            if case_id not in case_ids:
                case_ids.append(case_id)
    if not case_ids:
        raise HTTPException(status_code=400, detail="请选择要导出的用例或目录")

    cases: List[TestCase] = []
    for case_id in case_ids:
        case = session.get(TestCase, case_id)
        if case:
            cases.append(case)
    if not cases:
        raise HTTPException(status_code=404, detail="未找到可导出的用例")

    batch_name = request.name or (f"folder_{request.folder_id}" if request.folder_id else "batch")
    result = packager.build_batch_package(
        session, cases, env_id=request.env_id, batch_name=batch_name, platform=normalize_platform(request.platform)
    )
    return _file_response(result["content"], result["filename"], "application/zip")


@router.get("/scenarios/{scenario_id}/precheck")
def precheck_scenario(
    scenario_id: int,
    env_id: Optional[int] = None,
    platform: str = Query(default="android"),
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    scenario = session.get(TestScenario, scenario_id)
    if not scenario:
        raise HTTPException(status_code=404, detail="场景不存在")
    result = packager.build_scenario_package(session, scenario, env_id=env_id, platform=normalize_platform(platform))
    return result["precheck"]


@router.post("/scenarios/{scenario_id}/export")
def export_scenario(
    scenario_id: int,
    env_id: Optional[int] = None,
    platform: str = Query(default="android"),
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    scenario = session.get(TestScenario, scenario_id)
    if not scenario:
        raise HTTPException(status_code=404, detail="场景不存在")
    result = packager.build_scenario_package(session, scenario, env_id=env_id, platform=normalize_platform(platform))
    return _file_response(result["content"], result["filename"], "application/zip")


# --------------------------------------------------------------------------- #
#  设备侧：注册 / 任务领取 / 包下载
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
        device.model = str(payload.model)
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


@device_router.get("/pending")
def claim_pending_task(
    serial: str = Query(..., description="设备 serial（register 返回值）"),
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    """领取一个待执行任务。

    领取使用条件更新保证原子性（UPDATE ... WHERE status='PENDING'），
    同一任务即使被多个 Agent 实例并发轮询也只会被领取一次；
    超时未上报的 CLAIMED 任务自动回收。
    """
    from sqlalchemy import update as sa_update

    now = datetime.now()
    stale = session.exec(
        select(DeviceRunTask).where(
            DeviceRunTask.status == "CLAIMED",
            DeviceRunTask.claimed_at < now - CLAIM_TIMEOUT,
        )
    ).all()
    for task in stale:
        task.status = "PENDING"
        task.claimed_at = None
        session.add(task)
    if stale:
        session.commit()

    candidates = session.exec(
        select(DeviceRunTask)
        .where(
            DeviceRunTask.status == "PENDING",
            or_(
                DeviceRunTask.device_serial.is_(None),
                DeviceRunTask.device_serial == serial,
            ),
        )
        .order_by(DeviceRunTask.created_at)
        .limit(3)
    ).all()

    for candidate in candidates:
        result = session.execute(
            sa_update(DeviceRunTask)
            .where(
                DeviceRunTask.id == candidate.id,
                DeviceRunTask.status == "PENDING",  # 条件更新：并发下只有一个赢家
            )
            .values(
                status="CLAIMED",
                claimed_at=now,
                device_serial=candidate.device_serial or serial,
            )
        )
        session.commit()
        if result.rowcount == 1:
            session.refresh(candidate)
            return {
                "task": {
                    "task_id": candidate.task_id,
                    "kind": candidate.kind,
                    "name": candidate.package_name,
                    "case_id": candidate.case_id,
                    "scenario_id": candidate.scenario_id,
                    "package_url": f"/api/device-runs/packages/{candidate.task_id}",
                }
            }
    return {"task": None}


@device_router.get("/packages/{task_id}")
def download_package(
    task_id: str,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    task = session.exec(select(DeviceRunTask).where(DeviceRunTask.task_id == task_id)).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not task.package_path or not Path(task.package_path).is_file():
        raise HTTPException(status_code=404, detail="脚本包不存在或已被清理")
    return FileResponse(
        task.package_path,
        media_type="application/zip",
        filename=task.package_name or f"{task_id}.zip",
    )


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
    # 两种上报来源：
    # 1) Agent 领取的任务（task_id）：任务状态收尾 + 报告 + 通知；
    # 2) App 直跑（run_id，无任务）：按 run_id 幂等落库，不涉及任务表。
    task: Optional[DeviceRunTask] = None
    if payload.task_id:
        task = session.exec(
            select(DeviceRunTask).where(DeviceRunTask.task_id == payload.task_id)
        ).first()
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在（task_id 未注册）")
        kind = task.kind
        ref_case_id = task.case_id
        ref_scenario_id = task.scenario_id
    else:
        if not payload.run_id:
            raise HTTPException(status_code=400, detail="上报需要 task_id（Agent 任务）或 run_id（App 直跑）")
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
    previous = []
    if task is not None and task.execution_id:
        existing = session.get(TestExecution, task.execution_id)
        previous = [existing] if existing else []
    elif task is None:
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

    if task is not None:
        device_serial = task.device_serial
        batch_id = None
        executor_id = task.created_by or current_user.id
    else:
        device_serial = _serial_from_device(payload)
        batch_id = payload.run_id
        executor_id = current_user.id
    # 报告中心标题展示用例/场景名；执行方式（App/平台）由 run_source 表达。
    batch_name = display_name

    report_platform = normalize_platform(payload.platform)
    if not payload.platform and task is not None and str(task.device_serial or "").startswith(DEVICE_SERIAL_PREFIX_IOS):
        report_platform = "ios"

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
        logger.warning("AutoDroid 报告生成失败: task=%s run=%s error=%s", payload.task_id, payload.run_id, exc)

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

    # 任务收尾（仅 Agent 任务）
    if task is not None:
        task.status = "DONE"
        task.execution_id = execution.id
        task.finished_at = end_time
        task.error = None
        task.result_summary = {
            "status": overall_status,
            "execution_id": execution.id,
            "total_steps": total_steps,
            "passed_steps": passed_steps,
            "failed_steps": total_steps - passed_steps,
            "duration": round(duration, 2),
        }
        session.add(task)
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
            logger.error("AutoDroid 飞书通知发送失败: task=%s run=%s error=%s", payload.task_id, payload.run_id, exc)
    else:
        logger.info("上报 notify=false，跳过飞书通知: task=%s run=%s", payload.task_id, payload.run_id)

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
        "AutoDroid 结果回传完成: task=%s execution=%s status=%s",
        payload.task_id,
        result.get("execution_id"),
        result.get("status"),
    )
    return result


# --------------------------------------------------------------------------- #
#  任务管理（浏览器侧）
# --------------------------------------------------------------------------- #
def _task_to_dict(task: DeviceRunTask) -> Dict[str, Any]:
    return {
        "id": task.id,
        "task_id": task.task_id,
        "kind": task.kind,
        "case_id": task.case_id,
        "scenario_id": task.scenario_id,
        "device_serial": task.device_serial,
        "status": task.status,
        "package_name": task.package_name,
        "execution_id": task.execution_id,
        "error": task.error,
        "result_summary": task.result_summary,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "claimed_at": task.claimed_at.isoformat() if task.claimed_at else None,
        "finished_at": task.finished_at.isoformat() if task.finished_at else None,
    }


@router.post("/tasks")
def create_device_run_task(
    request: DeviceRunTaskCreate,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    """创建 AutoDroid 设备端任务（生成脚本包等待设备领取）。"""
    kind = str(request.kind or "").strip().lower()
    platform = normalize_platform(request.platform)
    if request.device_serial:
        device = session.exec(select(Device).where(Device.serial == request.device_serial)).first()
        if device is not None:
            platform = normalize_platform(device.platform)
    if kind == "case":
        case = session.get(TestCase, request.case_id) if request.case_id else None
        if not case:
            raise HTTPException(status_code=404, detail="用例不存在")
        package = packager.build_case_package(session, case, env_id=request.env_id, platform=platform)
        display_name = request.name or f"用例: {case.name}"
    elif kind == "scenario":
        scenario = session.get(TestScenario, request.scenario_id) if request.scenario_id else None
        if not scenario:
            raise HTTPException(status_code=404, detail="场景不存在")
        package = packager.build_scenario_package(session, scenario, env_id=request.env_id, platform=platform)
        display_name = request.name or f"场景: {scenario.name}"
    else:
        raise HTTPException(status_code=400, detail="kind 仅支持 case/scenario")

    if not package["precheck"].get("ok"):
        raise HTTPException(
            status_code=400,
            detail={
                "message": "脚本包预检未通过，请先修复导出问题",
                "issues": package["precheck"].get("issues") or [],
            },
        )

    task_id = str(uuid4())
    PACKAGE_DIR.mkdir(parents=True, exist_ok=True)
    package_path = PACKAGE_DIR / f"{task_id}.zip"
    package_path.write_bytes(package["content"])

    task = DeviceRunTask(
        task_id=task_id,
        kind=kind,
        case_id=request.case_id if kind == "case" else None,
        scenario_id=request.scenario_id if kind == "scenario" else None,
        env_id=request.env_id,
        device_serial=request.device_serial,
        status="PENDING",
        package_name=display_name,
        package_path=str(package_path),
        created_by=current_user.id,
    )
    session.add(task)
    session.commit()
    session.refresh(task)
    return _task_to_dict(task)


@router.get("/tasks")
def list_device_run_tasks(
    status: Optional[str] = None,
    limit: int = Query(default=50, le=200),
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    query = select(DeviceRunTask).order_by(DeviceRunTask.created_at.desc()).limit(limit)
    if status:
        query = (
            select(DeviceRunTask)
            .where(DeviceRunTask.status == status)
            .order_by(DeviceRunTask.created_at.desc())
            .limit(limit)
        )
    tasks = session.exec(query).all()
    return {"items": [_task_to_dict(task) for task in tasks]}


@router.post("/tasks/{task_id}/cancel")
def cancel_device_run_task(
    task_id: str,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    task = session.exec(select(DeviceRunTask).where(DeviceRunTask.task_id == task_id)).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.status in ("DONE", "FAILED", "CANCELED"):
        raise HTTPException(status_code=400, detail=f"任务已结束（{task.status}），无法取消")
    task.status = "CANCELED"
    task.finished_at = datetime.now()
    session.add(task)
    session.commit()
    return _task_to_dict(task)
