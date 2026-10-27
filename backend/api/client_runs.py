"""桌面客户端执行上报（client-runs）与模板图上传（client-templates）。

桌面本地引擎驱动设备 I/O，结果按阶段回传云端落库：
- POST /client-runs/                 创建执行记录（run_id 幂等，TestExecution.batch_id）
- POST /client-runs/{run}/steps     批量步骤结果（screenshot_base64 可选）
- POST /client-runs/{run}/finish    收尾状态/耗时 -> HTML 报告 -> 飞书通知
- POST /client-templates            multipart 模板图 -> static/images/element_<hash>.png

{run} 既接受创建时返回的 execution_id，也接受客户端幂等键 run_id。

结果回传链路复用 AutoDroid 设备端设施（不重造报告管线）：
- 结果转换: backend.api.autojs._build_case_result / _aggregate_status
- 持久化:  backend.scenario_results._persist_case_result_and_build_case_report
- 报告:    backend.report_generator.report_generator
- 通知:    backend.notification_service.NotificationService.send_report_card

run_source 取值对齐 models.TestExecution.run_source 注释与前端 runSourceLabel：
'platform'（默认，平台控机/客户端本地执行）| 'app'（App/设备端执行）。
新增取值会落到前端「—」分支，故此处只放行既有两值。

步骤批次的用例报告条目暂存于 reports/client_runs/exec_<id>.json（与 HTML 报告、
screenshots 同属 reports 产物目录），finish 时据此生成报告；单用例允许多次
steps 增量上报（同 case 键自动合并步骤）。
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from backend.api import deps
from backend.api.autojs import (
    DeviceReportCase,
    DeviceReportStep,
    _aggregate_status,
    _build_case_result,
)
from backend.database import get_session
from backend.models import (
    TestCase,
    TestExecution,
    TestScenario,
    User,
)
from backend.notification_service import NotificationService
from backend.paths import project_path
from backend.report_generator import report_generator
from backend.scenario_results import (
    _determine_case_status,
    _persist_case_result_and_build_case_report,
)
from backend.step_contract import normalize_platform

logger = logging.getLogger(__name__)

router = APIRouter()
template_router = APIRouter()

RUN_SOURCES = {"platform", "app"}
RUN_KINDS = {"case", "scenario"}
# 与 crop_template 同目录；引用格式保持 "static/images/<filename>"
TEMPLATE_IMAGE_DIR = project_path("static", "images")
TEMPLATE_IMAGE_REF_PREFIX = "static/images"
MAX_TEMPLATE_IMAGE_BYTES = 10 * 1024 * 1024
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


# --------------------------------------------------------------------------- #
#  Schemas
# --------------------------------------------------------------------------- #
class ClientRunCreate(BaseModel):
    run_id: str = Field(..., description="客户端幂等键（uuid）；重试返回既有执行记录")
    kind: str = "case"  # case | scenario
    case_id: Optional[int] = None
    scenario_id: Optional[int] = None
    name: Optional[str] = None
    device_serial: Optional[str] = None
    platform: Optional[str] = None  # android（默认）| ios
    run_source: Optional[str] = None  # platform（默认）| app
    # 桌面契约字段（engine/core/cloud_bridge.py）：started_at 为 epoch 秒或 ISO 串，
    # env_id 为执行时注入的全局变量环境；创建时采用（落 start_time / env_id），不得丢弃
    started_at: Optional[datetime] = None
    env_id: Optional[int] = None


class ClientRunStep(BaseModel):
    order: int
    action: Optional[str] = None
    status: str = "FAIL"  # PASS | FAIL | SKIP | WARNING
    duration: float = 0.0
    attempts: int = 1
    error: Optional[str] = None
    error_code: Optional[str] = None
    screenshot_base64: Optional[str] = None
    output: Optional[Dict[str, Any]] = None


class ClientRunStepsPayload(BaseModel):
    """单个用例的一批步骤结果（同 case 键的多批自动合并）。"""

    case_id: Optional[int] = None
    name: Optional[str] = None
    case_name: Optional[str] = None  # 桌面兼容双写（name 缺省时兜底），契约字段为 name
    alias: Optional[str] = None
    success: Optional[bool] = None  # 缺省按步骤推导；空步骤批次默认 False（对齐 DeviceReportCase）
    exported_variables: Optional[Dict[str, Any]] = None
    steps: List[ClientRunStep] = Field(default_factory=list)

    def display_name(self) -> Optional[str]:
        return self.name or self.case_name or None


class ClientRunFinishPayload(BaseModel):
    status: Optional[str] = None  # 缺省按用例聚合：PASS/FAIL/WARNING/ABORTED/ERROR
    aborted: bool = False
    duration: Optional[float] = None  # 秒；缺省用 now - start_time
    error: Optional[str] = None
    notify: bool = True  # 与 device-runs 一致：默认发飞书卡片，可逐次关闭
    exported_variables: Optional[Dict[str, Any]] = None


# --------------------------------------------------------------------------- #
#  执行记录状态（用例报告条目暂存）
# --------------------------------------------------------------------------- #
def _run_state_path(execution_id: int) -> Path:
    return project_path("reports", "client_runs", f"exec_{execution_id}.json")


def _load_run_state(execution_id: int) -> Dict[str, Any]:
    path = _run_state_path(execution_id)
    if not path.is_file():
        return {"execution_id": execution_id, "cases": [], "next_step_order": 1}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("client-runs 状态读取失败，按空状态继续: %s error=%s", path, exc)
        return {"execution_id": execution_id, "cases": [], "next_step_order": 1}
    if not isinstance(data, dict):
        return {"execution_id": execution_id, "cases": [], "next_step_order": 1}
    data.setdefault("cases", [])
    data.setdefault("next_step_order", 1)
    return data


def _save_run_state(execution_id: int, state: Dict[str, Any]) -> None:
    path = _run_state_path(execution_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def _case_state_key(reported: DeviceReportCase) -> Tuple[Any, Any, Any]:
    return (reported.case_id, reported.alias or "", reported.name or "")


# --------------------------------------------------------------------------- #
#  通用校验
# --------------------------------------------------------------------------- #
def _resolve_execution(session: Session, run_ref: str) -> TestExecution:
    ref = str(run_ref or "").strip()
    if not ref:
        raise HTTPException(status_code=404, detail="执行记录不存在")
    execution = session.exec(select(TestExecution).where(TestExecution.batch_id == ref)).first()
    if execution is not None:
        return execution
    if ref.isdigit():
        execution = session.get(TestExecution, int(ref))
        if execution is not None:
            return execution
    raise HTTPException(status_code=404, detail="执行记录不存在")


def _require_run_owner(execution: TestExecution, current_user: User) -> None:
    role = getattr(current_user, "role", None)
    if role == "admin":
        return
    if execution.executor_id != current_user.id:
        raise HTTPException(status_code=403, detail="无权操作该执行记录")


def _normalize_run_source(raw: Optional[str]) -> str:
    source = str(raw or "").strip().lower() or "platform"
    if source not in RUN_SOURCES:
        raise HTTPException(
            status_code=400,
            detail=f"run_source 仅支持 {'/'.join(sorted(RUN_SOURCES))}",
        )
    return source


def _normalize_finish_status(raw: Optional[str]) -> Optional[str]:
    text = str(raw or "").strip().upper()
    if not text:
        return None
    aliases = {
        "PASS": "PASS",
        "PASSED": "PASS",
        "SUCCESS": "PASS",
        "FAIL": "FAIL",
        "FAILED": "FAIL",
        "FAILURE": "FAIL",
        "WARNING": "WARNING",
        "WARN": "WARNING",
        "SKIP": "WARNING",
        "SKIPPED": "WARNING",
        "ABORTED": "ABORTED",
        "ABORT": "ABORTED",
        "CANCELED": "ABORTED",
        "CANCELLED": "ABORTED",
        "ERROR": "ERROR",
    }
    mapped = aliases.get(text)
    if mapped is None:
        raise HTTPException(status_code=400, detail=f"非法执行状态: {raw}")
    return mapped


def _payload_steps_to_report_case(payload: ClientRunStepsPayload) -> DeviceReportCase:
    steps = [
        DeviceReportStep(
            order=step.order,
            action=step.action,
            status=step.status,
            duration=step.duration,
            attempts=step.attempts,
            error=step.error,
            error_code=step.error_code,
            screenshot_base64=step.screenshot_base64,
            output=step.output,
        )
        for step in payload.steps
    ]
    # 空步骤批次不得被 all([])==True 推导为 PASS：仅显式 success=True 才算通过
    # （对齐 DeviceReportCase.success 默认 False）
    derived_success = bool(steps) and all(
        str(step.status or "").strip().upper() in ("PASS", "SKIP") for step in steps
    )
    return DeviceReportCase(
        case_id=payload.case_id,
        name=payload.display_name(),
        alias=payload.alias,
        success=bool(payload.success) if payload.success is not None else derived_success,
        steps=steps,
        exported_variables=payload.exported_variables,
    )


def _coerce_naive_datetime(value: Optional[datetime]) -> Optional[datetime]:
    """started_at 归一到 naive 本地时间（与 datetime.now() 口径一致，避免 aware/naive 相减）。

    桌面发送 epoch 秒（pydantic 解析为 UTC aware）或 ISO 串（可带时区）。
    """
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone().replace(tzinfo=None)
    return value


# --------------------------------------------------------------------------- #
#  client-runs：创建 / 步骤 / 收尾
# --------------------------------------------------------------------------- #
@router.post("")
@router.post("/")
async def create_client_run(
    payload: ClientRunCreate,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    """创建执行记录（run_id 幂等）：已存在则原样返回（created=false）。

    并发加固：batch_id 允许同批次多设备共用（scenario 批量执行一 batch 多行），
    不能加 UNIQUE；改为对齐 autojs._persist_device_report 的 BEGIN IMMEDIATE
    写保留串行化——查重与插入同处一个写事务，并发创建只落一条记录。
    """
    run_id = str(payload.run_id or "").strip()
    if not run_id:
        raise HTTPException(status_code=400, detail="run_id 不能为空")
    kind = str(payload.kind or "case").strip().lower()
    if kind not in RUN_KINDS:
        raise HTTPException(status_code=400, detail="kind 仅支持 case / scenario")
    run_source = _normalize_run_source(payload.run_source)

    # 鉴权可能已开启读事务；先收尾，再抢 SQLite 写保留（跨进程同样生效）
    session.commit()
    try:
        connection = session.connection()
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        result = _create_client_run_locked(session, payload, current_user, run_id, kind, run_source)
        session.commit()
    except Exception:
        session.rollback()
        raise

    if result.get("created") and result.get("state"):
        _save_run_state(result["execution_id"], result.pop("state"))
    else:
        result.pop("state", None)
    return result


def _create_client_run_locked(
    session: Session,
    payload: ClientRunCreate,
    current_user: User,
    run_id: str,
    kind: str,
    run_source: str,
) -> Dict[str, Any]:
    previous = session.exec(select(TestExecution).where(TestExecution.batch_id == run_id)).all()
    if previous:
        if any(item.executor_id != current_user.id for item in previous):
            raise HTTPException(status_code=409, detail="运行编号已被其他账号使用")
        execution = previous[0]
        return {
            "run_id": run_id,
            "id": execution.id,
            "execution_id": execution.id,
            "status": execution.status,
            "created": False,
            "start_time": execution.start_time.isoformat() if execution.start_time else None,
        }

    case: Optional[TestCase] = None
    scenario: Optional[TestScenario] = None
    if kind == "case":
        if not payload.case_id:
            raise HTTPException(status_code=400, detail="kind=case 需要 case_id")
        case = session.get(TestCase, payload.case_id)
        if not case:
            raise HTTPException(status_code=404, detail="用例不存在")
        display_name = str(payload.name or case.name)
    else:
        if not payload.scenario_id:
            raise HTTPException(status_code=400, detail="kind=scenario 需要 scenario_id")
        scenario = session.get(TestScenario, payload.scenario_id)
        if not scenario:
            raise HTTPException(status_code=404, detail="场景不存在")
        display_name = str(payload.name or scenario.name)

    device_serial = str(payload.device_serial or "").strip() or None
    start_time = _coerce_naive_datetime(payload.started_at) or datetime.now()
    execution = TestExecution(
        scenario_id=scenario.id if scenario else 0,
        scenario_name=display_name,
        status="RUNNING",
        executor_id=current_user.id,
        executor_name=current_user.full_name or current_user.username,
        device_serial=device_serial,
        platform=normalize_platform(payload.platform),
        device_info=f"桌面客户端 · {device_serial}" if device_serial else "桌面客户端",
        batch_id=run_id,
        batch_name=display_name,
        run_source=run_source,
        start_time=start_time,
        env_id=payload.env_id,
    )
    session.add(execution)
    session.flush()
    session.refresh(execution)

    logger.info("client-runs 创建执行记录: run_id=%s execution=%s kind=%s", run_id, execution.id, kind)
    return {
        "run_id": run_id,
        "id": execution.id,
        "execution_id": execution.id,
        "status": execution.status,
        "created": True,
        "start_time": execution.start_time.isoformat() if execution.start_time else None,
        "state": {
            "execution_id": execution.id,
            "run_id": run_id,
            "kind": kind,
            "case_id": case.id if case else None,
            "scenario_id": scenario.id if scenario else None,
            "display_name": display_name,
            "env_id": payload.env_id,
            "cases": [],
            "next_step_order": 1,
        },
    }


@router.post("/{run_ref}/steps")
async def report_client_run_steps(
    run_ref: str,
    payload: ClientRunStepsPayload,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    """批量步骤结果上报：立即落 TestResult + reports/screenshots，用例报告条目累积到收尾。"""
    execution = _resolve_execution(session, run_ref)
    _require_run_owner(execution, current_user)
    if execution.status != "RUNNING":
        raise HTTPException(status_code=409, detail=f"执行记录已结束（{execution.status}），无法继续上报步骤")

    state = _load_run_state(execution.id)
    case_id = payload.case_id or state.get("case_id")
    if not case_id:
        raise HTTPException(status_code=400, detail="步骤上报需要 case_id")
    case = session.get(TestCase, int(case_id))
    if not case:
        raise HTTPException(status_code=404, detail="用例不存在")

    reported = _payload_steps_to_report_case(payload)
    reported = reported.model_copy(
        update={
            "case_id": case.id,
            "name": payload.display_name() or case.name,
        }
    )
    case_result = _build_case_result(session, case, reported)
    next_order = int(state.get("next_step_order") or 1)
    case_entry, next_order, _ = _persist_case_result_and_build_case_report(
        session=session,
        execution_id=execution.id,
        item={
            "case_name": payload.display_name() or case.name,
            "alias": payload.alias,
        },
        case_result=case_result,
        global_step_order=next_order,
    )
    session.commit()

    # 同 case 键的增量批次合并步骤，形成单条用例报告条目
    merged = False
    for entry in state["cases"]:
        if (entry.get("case_id"), entry.get("alias") or "", entry.get("case_name") or "") == _case_state_key(reported):
            entry["steps"] = list(entry.get("steps") or []) + list(case_entry.get("steps") or [])
            entry["success"] = bool(entry.get("success")) and bool(reported.success)
            entry["exported_variables"] = {
                **dict(entry.get("exported_variables") or {}),
                **dict(reported.exported_variables or {}),
            }
            merged = True
            break
    if not merged:
        state["cases"].append(
            {
                "case_id": case.id,
                "case_name": payload.display_name() or case.name,
                "alias": payload.alias,
                "success": bool(reported.success),
                "exported_variables": dict(reported.exported_variables or {}),
                "steps": list(case_entry.get("steps") or []),
            }
        )
    state["next_step_order"] = int(next_order)
    _save_run_state(execution.id, state)

    total_steps = sum(len(entry.get("steps") or []) for entry in state["cases"])
    return {
        "run_id": execution.batch_id,
        "execution_id": execution.id,
        "status": execution.status,
        "accepted_steps": len(case_entry.get("steps") or []),
        "total_steps": total_steps,
    }


@router.post("/{run_ref}/finish")
async def finish_client_run(
    run_ref: str,
    payload: ClientRunFinishPayload,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    """收尾：聚合状态/耗时 -> 生成 HTML 报告 -> 飞书通知（notify 对齐 device-runs）。

    幂等：已结束的执行记录直接返回既有结果，不重复生成报告/通知。
    """
    execution = _resolve_execution(session, run_ref)
    _require_run_owner(execution, current_user)
    if execution.status != "RUNNING":
        return {
            "run_id": execution.batch_id,
            "execution_id": execution.id,
            "status": execution.status,
            "report_id": execution.report_id,
            "duration": execution.duration,
            "finished": False,
        }

    state = _load_run_state(execution.id)
    case_entries = _build_case_entries(state)
    overall_status = _resolve_overall_status(payload, case_entries)

    end_time = datetime.now()
    if payload.duration is not None:
        duration = max(0.0, min(float(payload.duration), 24 * 3600))
    else:
        duration = max(0.0, (end_time - execution.start_time).total_seconds())

    case = session.get(TestCase, state.get("case_id")) if state.get("case_id") else None
    scenario = session.get(TestScenario, state.get("scenario_id")) if state.get("scenario_id") else None
    if case is None and scenario is None and execution.batch_id:
        # 状态文件缺失时回退：用执行记录关联定位
        scenario = session.get(TestScenario, execution.scenario_id) if execution.scenario_id else None
    display_name = (
        state.get("display_name")
        or (scenario.name if scenario else None)
        or (case.name if case else None)
        or execution.scenario_name
    )

    exported_variables: Dict[str, Any] = dict(payload.exported_variables or {})
    for entry in state["cases"]:
        exported_variables.update(dict(entry.get("exported_variables") or {}))

    report_id: Optional[str] = None
    try:
        if scenario is not None and (state.get("kind") == "scenario" or case is None):
            report_id = report_generator.generate_scenario_report(
                scenario_id=scenario.id,
                scenario_name=scenario.name,
                cases_results=case_entries,
                start_time=execution.start_time,
                end_time=end_time,
            )
        elif case is not None:
            report_id = report_generator.generate_report(
                case_id=case.id,
                case_name=case.name,
                steps_results=case_entries[0]["steps"] if case_entries else [],
                start_time=execution.start_time,
                end_time=end_time,
                variables=[
                    {"key": key, "value": value} for key, value in exported_variables.items()
                ] or None,
            )
    except Exception as exc:
        logger.warning("client-runs 报告生成失败: run=%s error=%s", execution.batch_id, exc)

    execution.status = overall_status
    execution.end_time = end_time
    execution.duration = duration
    if report_id:
        execution.report_id = report_id
    session.add(execution)

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
    session.commit()

    total_steps = sum(len(entry.get("steps") or []) for entry in case_entries)
    passed_steps = sum(
        1
        for entry in case_entries
        for step in (entry.get("steps") or [])
        if step.get("status") == "success"
    )
    if payload.notify:
        _send_notify_card(
            display_name=display_name,
            execution=execution,
            overall_status=overall_status,
            total_steps=total_steps,
            passed_steps=passed_steps,
            duration=duration,
            case_entries=case_entries,
            extra_error=payload.error,
        )
    else:
        logger.info("client-runs notify=false，跳过飞书通知: run=%s", execution.batch_id)

    logger.info(
        "client-runs 收尾完成: run=%s execution=%s status=%s report=%s",
        execution.batch_id,
        execution.id,
        overall_status,
        report_id,
    )
    return {
        "run_id": execution.batch_id,
        "execution_id": execution.id,
        "status": overall_status,
        "report_id": report_id,
        "duration": duration,
        "total_steps": total_steps,
        "passed_steps": passed_steps,
        "failed_steps": total_steps - passed_steps,
        "finished": True,
    }


def _build_case_entries(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """暂存的用例步骤 -> 报告用例条目（status 聚合对齐平台口径）。"""
    entries: List[Dict[str, Any]] = []
    for item in state.get("cases") or []:
        steps = list(item.get("steps") or [])
        has_warning = any(step.get("status") == "warning" for step in steps)
        case_success = bool(item.get("success"))
        entry = {
            "case_id": item.get("case_id"),
            "case_name": item.get("case_name"),
            "alias": item.get("alias"),
            "status": _determine_case_status(
                steps,
                case_success=case_success,
                case_is_warning=bool(case_success and has_warning),
            ),
            "steps": steps,
        }
        entries.append(entry)
    return entries


def _resolve_overall_status(payload: ClientRunFinishPayload, case_entries: List[Dict[str, Any]]) -> str:
    explicit = _normalize_finish_status(payload.status)
    if explicit:
        return explicit
    if payload.aborted:
        return "ABORTED"
    if not case_entries:
        return "FAIL"
    return _aggregate_status(case_entries, False)


def _send_notify_card(
    *,
    display_name: str,
    execution: TestExecution,
    overall_status: str,
    total_steps: int,
    passed_steps: int,
    duration: float,
    case_entries: List[Dict[str, Any]],
    extra_error: Optional[str] = None,
) -> None:
    """飞书卡片与 device-runs（autojs）同参同行为；失败不影响上报结果。"""
    try:
        errors = [
            str(step.get("error"))
            for entry in case_entries
            for step in (entry.get("steps") or [])
            if step.get("error")
        ]
        if extra_error:
            errors.append(str(extra_error))
        device_display = execution.device_info or "桌面客户端设备"
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
        logger.error("client-runs 飞书通知发送失败: run=%s error=%s", execution.batch_id, exc)


# --------------------------------------------------------------------------- #
#  client-templates：模板图上传
# --------------------------------------------------------------------------- #
def _validate_png(data: bytes) -> None:
    if not data:
        raise HTTPException(status_code=400, detail="模板图为空")
    if len(data) > MAX_TEMPLATE_IMAGE_BYTES:
        raise HTTPException(status_code=400, detail="模板图超过大小限制（10MB）")
    if not data.startswith(PNG_MAGIC):
        raise HTTPException(status_code=400, detail="模板图必须是 PNG 格式")
    try:
        from PIL import Image

        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"模板图解析失败: {exc}")
    if getattr(image, "format", None) != "PNG":
        raise HTTPException(status_code=400, detail="模板图必须是 PNG 格式")


@template_router.post("")
@template_router.post("/")
async def upload_client_template(
    file: UploadFile = File(...),
    current_user: User = Depends(deps.get_current_user),
):
    """上传图像模板（click_image/assert_image 素材）。

    校验 PNG 后按内容哈希存 static/images/element_<hash>.png，
    返回 {"image_path": "static/images/element_<hash>.png"}（与 crop_template 引用格式一致）。
    """
    data = await file.read()
    _validate_png(data)

    digest = hashlib.sha256(data).hexdigest()[:16]
    filename = f"element_{digest}.png"
    target = TEMPLATE_IMAGE_DIR / filename
    TEMPLATE_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        tmp = target.with_suffix(".png.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)
    logger.info("client-templates 模板图已保存: %s/%s", TEMPLATE_IMAGE_REF_PREFIX, filename)
    return {"image_path": f"{TEMPLATE_IMAGE_REF_PREFIX}/{filename}"}
