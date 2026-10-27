"""桌面客户端 Fastbot 本地执行上报（client-fastbot）。

桌面引擎在本机设备跑 Fastbot / 流畅度 / 冷热启动采集后，把结果 JSON 与产物
回传云端落库（FastbotTask + FastbotReport），复用既有读接口展示
（GET /api/fastbot/tasks、/reports/{id}、/startup/tasks、/fluency/sessions），
前端与读侧零改动：

- POST /client-fastbot/                 创建任务（run_id 幂等），status=RUNNING
- POST /client-fastbot/{id}/finish      收尾（status/counts + report JSON），upsert 报告
- POST /client-fastbot/{id}/artifacts   产物上传（path 相对 reports/fastbot/{id}/）

契约要点（桌面 cloud_bridge 对齐）：
- kind: fastbot（默认）| fluency | startup；fluency/startup 预置
  summary.session_type = fluency_manual / startup，既有列表接口据此归类，
  finish 时若未显式覆盖则保留该标记。
- report 六列与 FastbotReport 一一对应（performance_data / jank_data /
  jank_events / trace_artifacts / crash_events / summary），形状沿用
  backend.fastbot.runner.run_fastbot_task 的 result dict。
- 产物 path 约定与服务端自跑一致：replays/<name>.mp4 由
  GET /api/fastbot/replays/{id}/{filename} 直接服务；traces/<name>.perfetto-trace
  上传后 trace_artifacts[].path 填 reports/fastbot/{id}/traces/<name>，可触发
  平台侧 jank 分析（客户端已分析的产物带 analysis_status 可跳过）。
- 鉴权 get_current_user；写操作仅执行人本人或 admin。run_id 幂等仅对同一执行人。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from backend.api import deps
from backend.database import get_session
from backend.fastbot.reporting import FASTBOT_REPORTS_DIR
from backend.models import FastbotReport, FastbotTask, User
from backend.schemas import FastbotTaskRead

logger = logging.getLogger(__name__)

router = APIRouter()

# 产物落盘根目录；测试可整体改指临时目录
REPORTS_DIR = FASTBOT_REPORTS_DIR

CLIENT_FASTBOT_KINDS = {"fastbot", "fluency", "startup"}
SESSION_TYPE_BY_KIND = {"fluency": "fluency_manual", "startup": "startup"}
FINISH_STATUSES = {"COMPLETED", "FAILED"}
MAX_ARTIFACT_BYTES = 100 * 1024 * 1024
_ARTIFACT_CHUNK_BYTES = 1024 * 1024


class ClientFastbotTaskCreate(BaseModel):
    run_id: str = Field(..., description="客户端幂等键（uuid）；重试返回既有任务")
    kind: str = "fastbot"  # fastbot | fluency | startup
    package_name: str
    device_serial: str
    duration: int = 600
    throttle: int = 500
    ignore_crashes: bool = False
    capture_log: bool = True
    started_at: Optional[datetime] = None
    executor_name: Optional[str] = None


class ClientFastbotReportPayload(BaseModel):
    performance_data: List[Dict[str, Any]] = Field(default_factory=list)
    jank_data: List[Dict[str, Any]] = Field(default_factory=list)
    jank_events: List[Dict[str, Any]] = Field(default_factory=list)
    trace_artifacts: List[Dict[str, Any]] = Field(default_factory=list)
    crash_events: List[Dict[str, Any]] = Field(default_factory=list)
    summary: Dict[str, Any] = Field(default_factory=dict)


class ClientFastbotFinish(BaseModel):
    status: str = "COMPLETED"  # COMPLETED | FAILED
    total_crashes: Optional[int] = None  # 缺省从 report.summary.total_crashes 取
    total_anrs: Optional[int] = None
    finished_at: Optional[datetime] = None
    report: ClientFastbotReportPayload = Field(default_factory=ClientFastbotReportPayload)


def _normalize_kind(raw: Any) -> str:
    kind = str(raw or "fastbot").strip().lower()
    if kind not in CLIENT_FASTBOT_KINDS:
        raise HTTPException(status_code=400, detail=f"不支持的 kind: {raw}")
    return kind


def _get_task_or_404(session: Session, task_id: int) -> FastbotTask:
    task = session.get(FastbotTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


def _safe_artifact_rel_path(raw: Any) -> str:
    """产物相对路径校验：限定 reports/fastbot/<task_id>/ 之下，拒绝穿越/绝对路径。"""
    value = str(raw or "").strip().replace("\\", "/")
    if not value:
        raise HTTPException(status_code=400, detail="path 不能为空")
    if value.startswith("/") or (len(value) > 1 and value[1] == ":"):
        raise HTTPException(status_code=400, detail="path 必须为产物目录下的相对路径")
    parts = [item for item in value.split("/") if item not in ("", ".")]
    if not parts or any(item == ".." for item in parts):
        raise HTTPException(status_code=400, detail="非法产物路径")
    return "/".join(parts)


@router.post("", response_model=FastbotTaskRead)
@router.post("/", response_model=FastbotTaskRead)
def create_client_fastbot_task(
    payload: ClientFastbotTaskCreate,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    """登记一次客户端本地执行（status=RUNNING），返回 FastbotTaskRead。

    run_id 幂等：同一执行人重试返回既有任务；被他人占用返回 409。
    """
    run_id = str(payload.run_id or "").strip()
    if not run_id:
        raise HTTPException(status_code=400, detail="run_id 不能为空")
    kind = _normalize_kind(payload.kind)
    package_name = str(payload.package_name or "").strip()
    device_serial = str(payload.device_serial or "").strip()
    if not package_name or not device_serial:
        raise HTTPException(status_code=400, detail="package_name / device_serial 不能为空")

    existing = session.exec(select(FastbotTask).where(FastbotTask.run_id == run_id)).first()
    if existing is not None:
        if existing.executor_id != current_user.id and current_user.role != "admin":
            raise HTTPException(status_code=409, detail="run_id 已被其他任务占用")
        return existing

    task = FastbotTask(
        run_id=run_id,
        package_name=package_name,
        duration=int(payload.duration or 0),
        throttle=int(payload.throttle or 0),
        ignore_crashes=bool(payload.ignore_crashes),
        capture_log=bool(payload.capture_log),
        device_serial=device_serial,
        status="RUNNING",
        executor_id=current_user.id,
        executor_name=str(payload.executor_name or "").strip()
        or f"桌面客户端: {current_user.username}",
        started_at=payload.started_at or datetime.now(),
    )
    session.add(task)
    session.commit()
    session.refresh(task)

    session_type = SESSION_TYPE_BY_KIND.get(kind)
    if session_type:
        # 预置归类标记：startup/fluency 列表接口按 summary.session_type 分流，
        # 运行期（finish 前）也能被正确归类。
        session.add(
            FastbotReport(task_id=task.id, summary=json.dumps({"session_type": session_type}))
        )
        session.commit()

    logger.info("客户端 Fastbot 任务已登记: task=%s run_id=%s kind=%s", task.id, run_id, kind)
    return task


@router.post("/{task_id}/finish")
def finish_client_fastbot_task(
    task_id: int,
    payload: ClientFastbotFinish,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    """收尾：写/更新 FastbotReport 并落任务状态（COMPLETED/FAILED）。"""
    task = _get_task_or_404(session, task_id)
    deps.ensure_owner_or_admin(task.executor_id, current_user)

    status = str(payload.status or "COMPLETED").strip().upper()
    if status not in FINISH_STATUSES:
        raise HTTPException(status_code=400, detail=f"不支持的收尾状态: {payload.status}")

    report_payload = payload.report or ClientFastbotReportPayload()
    summary = dict(report_payload.summary or {})
    summary.setdefault("total_crashes", payload.total_crashes or 0)
    summary.setdefault("total_anrs", payload.total_anrs or 0)

    report = session.exec(select(FastbotReport).where(FastbotReport.task_id == task_id)).first()
    if report is not None and report.summary:
        try:
            existing_summary = json.loads(report.summary)
        except (ValueError, TypeError):
            existing_summary = {}
        if isinstance(existing_summary, dict):
            # 保留 create 时预置的 session_type 归类标记（除非显式覆盖）
            if "session_type" not in summary and existing_summary.get("session_type"):
                summary["session_type"] = existing_summary["session_type"]

    task.status = status
    task.total_crashes = int(payload.total_crashes if payload.total_crashes is not None else summary.get("total_crashes", 0) or 0)
    task.total_anrs = int(payload.total_anrs if payload.total_anrs is not None else summary.get("total_anrs", 0) or 0)
    task.finished_at = payload.finished_at or datetime.now()
    session.add(task)

    if report is None:
        report = FastbotReport(task_id=task_id)
        session.add(report)
    report.performance_data = json.dumps(report_payload.performance_data or [])
    report.jank_data = json.dumps(report_payload.jank_data or [])
    report.jank_events = json.dumps(report_payload.jank_events or [])
    report.trace_artifacts = json.dumps(report_payload.trace_artifacts or [])
    report.crash_events = json.dumps(report_payload.crash_events or [])
    report.summary = json.dumps(summary)
    session.add(report)
    session.commit()
    session.refresh(report)

    logger.info("客户端 Fastbot 任务收尾: task=%s status=%s", task_id, status)
    return {"task_id": task.id, "report_id": report.id, "status": task.status}


@router.post("/{task_id}/artifacts")
async def upload_client_fastbot_artifact(
    task_id: int,
    file: UploadFile = File(...),
    path: Optional[str] = Form(None),
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    """产物上传：写入 reports/fastbot/{task_id}/<path>（replays/…、traces/… 等）。"""
    task = _get_task_or_404(session, task_id)
    deps.ensure_owner_or_admin(task.executor_id, current_user)

    rel = _safe_artifact_rel_path(path)
    task_dir = os.path.abspath(os.path.join(REPORTS_DIR, str(task_id)))
    target = os.path.abspath(os.path.join(task_dir, rel))
    if not target.startswith(task_dir + os.sep):
        raise HTTPException(status_code=400, detail="非法产物路径")

    os.makedirs(os.path.dirname(target), exist_ok=True)
    size = 0
    try:
        with open(target, "wb") as handle:
            while True:
                chunk = await file.read(_ARTIFACT_CHUNK_BYTES)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_ARTIFACT_BYTES:
                    raise HTTPException(status_code=413, detail="产物超过大小上限")
                handle.write(chunk)
    except HTTPException:
        if os.path.isfile(target):
            os.remove(target)
        raise
    finally:
        await file.close()

    return {
        "task_id": task_id,
        "path": f"reports/fastbot/{task_id}/{rel}",
        "size": size,
    }
