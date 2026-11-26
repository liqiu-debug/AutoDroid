"""Durable startup commands, fencing and shared Fastbot report persistence.

Commands reserve the real device before dispatch. A command is never reassigned
after a client disappears: the old attempt is closed and can only upload its
evidence through the recovery handshake, never restart a measurement.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import HTTPException
from sqlalchemy import case, func, or_, update
from sqlmodel import Session, select

from backend.database import engine
from backend.device_execution_lease import DeviceExecutionLease
from backend.models import ClientInstance, Device, FastbotReport, FastbotTask, StartupExecution

logger = logging.getLogger(__name__)
PROTOCOL_VERSION = 1
PRESENCE_SECONDS = 90
LEASE_SECONDS = 90
STOP_GRACE_SECONDS = 120
QUEUE_SECONDS = 1800
SERVER_HEARTBEAT_SECONDS = 5
TERMINAL = {"COMPLETED", "FAILED", "ABORTED"}
REPORT_COLUMNS = ("performance_data", "jank_data", "jank_events", "trace_artifacts", "crash_events", "summary")
_leases: Dict[int, DeviceExecutionLease] = {}
_stops: Dict[int, threading.Event] = {}
_runtime_lock = threading.RLock()


def default_capabilities(platform: Optional[str] = None) -> dict:
    from backend.startup.contract import startup_capabilities
    return startup_capabilities(platform, trace_supported=platform != "ios", web_dispatch=True)


def register_instance(session: Session, user, instance_id: str, capabilities=None, *, allow_rebind=False) -> ClientInstance:
    instance_id = str(instance_id or "").strip()
    if not instance_id or len(instance_id) > 200:
        raise HTTPException(400, "client_instance_id 不能为空且长度不能超过 200")
    node = session.get(ClientInstance, instance_id)
    if node and node.owner_id != user.id:
        if not allow_rebind and not node.logged_out_at:
            raise HTTPException(403, "客户端实例属于其他账号")
        # Rebind only after an explicit logout or the existing physical-device
        # registration endpoint. Fence the old account's in-flight work; a
        # heartbeat/claim alone cannot take over an active logged-in instance.
        prior = session.exec(select(StartupExecution).where(
            StartupExecution.client_instance_id == instance_id,
            StartupExecution.status.in_(["QUEUED", "RUNNING"]))).all()
        for command in prior:
            changed = session.exec(update(StartupExecution).where(
                StartupExecution.id == command.id,
                StartupExecution.status.in_(["QUEUED", "RUNNING"]),
            ).values(updated_at=datetime.now()))
            if changed.rowcount != 1:
                continue
            session.refresh(command)
            if command.status == "QUEUED":
                close_command(session, command, "ABORTED", reason="客户端已切换账号，排队任务取消")
            else:
                command.cancel_requested = True
                close_command(session, command, "ABORTED", reason="客户端已切换账号，采集已中断；仅恢复结果补传",
                              release_device=False)
        node.owner_id = user.id
        node.startup_capabilities = {}
    if node is None:
        node = ClientInstance(id=instance_id, owner_id=user.id)
    if capabilities is not None:
        if not isinstance(capabilities, dict):
            raise HTTPException(422, "startup_capabilities 必须是对象")
        if len(json.dumps(capabilities)) > 16000:
            raise HTTPException(422, "客户端能力数据过大")
        node.startup_capabilities = dict(capabilities)
    node.last_seen_at = datetime.now()
    node.logged_out_at = None
    session.add(node)
    return node


def can_control(device: Device, user) -> bool:
    return user.role == "admin" or device.owner_id == user.id


def execution_for_task(session: Session, task_id: int) -> Optional[StartupExecution]:
    return session.exec(select(StartupExecution).where(StartupExecution.task_id == task_id)).first()


def node_for_device(session: Session, device: Device) -> tuple[str, Optional[ClientInstance], str]:
    """Never treat a client-managed device as a server ADB device."""
    if device.client_instance_id or device.last_seen_at is not None:
        node = session.get(ClientInstance, str(device.client_instance_id or ""))
        if node is None:
            return "desktop", None, "客户端版本过旧，尚未上报冷热启动能力，请升级客户端"
        if node.owner_id != device.owner_id:
            return "desktop", node, "设备归属与客户端账号不一致，请重新连接设备"
        now = datetime.now()
        if node.logged_out_at or (now - node.last_seen_at).total_seconds() > PRESENCE_SECONDS:
            return "desktop", node, "设备所属桌面客户端已离线"
        if device.last_seen_at is None or (now - device.last_seen_at).total_seconds() > PRESENCE_SECONDS:
            return "desktop", node, "客户端设备心跳已过期，请重新连接设备"
        caps = node.startup_capabilities or {}
        if caps.get("protocol_version") != PROTOCOL_VERSION or not caps.get("web_dispatch"):
            return "desktop", node, "客户端不支持当前冷热启动协议或 Web 派发，请升级客户端"
        if device.platform not in caps.get("platforms", []):
            return "desktop", node, f"客户端未提供 {device.platform} 冷热启动能力"
        return "desktop", node, ""
    return "server", None, ""


def validate_device_config(device: Device, config: dict, caps: dict) -> None:
    if device.platform not in {"android", "ios"} or str(device.serial).startswith(("ios:", "autojs6:")):
        raise HTTPException(400, f"设备 {device.serial} 不支持主机启动采集，请选择物理设备")
    if config.get("platform") and config["platform"] != device.platform:
        raise HTTPException(400, "一次启动测试只能选择同一平台设备")
    ready = config.get("ready_check") or {}
    locators = caps.get("ready_locators_by_platform") or caps.get("ready_locators") or {}
    supported = locators.get(device.platform, []) if isinstance(locators, dict) else locators
    if ready.get("enabled") and ready.get("locator_type") not in supported:
        raise HTTPException(400, f"{device.platform} 执行节点不支持就绪定位方式 {ready.get('locator_type')}")
    if device.platform == "ios" and config.get("activity_name"):
        raise HTTPException(400, "iOS 启动测试不使用 Android Activity")
    from backend.startup.contract import normalize_config
    try:
        normalize_config(config, platform=device.platform, device_serial=device.serial)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def config_digest(config: dict) -> str:
    stable = {key: value for key, value in config.items() if key not in {"request_id", "device_serials"}}
    return hashlib.sha256(json.dumps(stable, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _summary(session: Session, task_id: int) -> dict:
    report = session.exec(select(FastbotReport).where(FastbotReport.task_id == task_id)).first()
    try:
        value = json.loads(report.summary) if report and report.summary else {}
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def save_report(session: Session, command: StartupExecution, payload: dict) -> FastbotReport:
    report = session.exec(select(FastbotReport).where(FastbotReport.task_id == command.task_id)).first()
    if report is None:
        report = FastbotReport(task_id=command.task_id)
    summary = dict(payload.get("summary") or {})
    from backend.startup.contract import MEASUREMENT_VERSION
    resolved_component = (summary.get("startup_config") or {}).get("resolved_component")
    display_config = {**command.config}
    if resolved_component:
        display_config["resolved_component"] = resolved_component
    summary.update(session_type="startup", startup_config=display_config,
                   platform=command.platform, execution_node=command.executor_kind,
                   run_id=command.run_id, measurement_version=summary.get("measurement_version", MEASUREMENT_VERSION))
    summary["progress"] = command.progress
    if command.terminal_reason:
        summary["interruption_reason"] = command.terminal_reason
    for column in REPORT_COLUMNS:
        value = summary if column == "summary" else payload.get(column, [])
        setattr(report, column, json.dumps(value, ensure_ascii=False))
    session.add(report)
    return report


def _release_reservation(session: Session, command: StartupExecution) -> None:
    # A disconnected or newly owned device must not be resurrected by an old run.
    session.exec(update(Device).where(
        Device.serial == command.device_serial,
        Device.lease_task_id == f"startup:{command.task_id}",
    ).values(status=case((func.upper(Device.status) == "BUSY", "IDLE"), else_=Device.status), lease_task_id=None, lease_kind=None,
             lease_acquired_at=None, updated_at=datetime.now()))
    command.reservation_release_at = None
    session.add(command)
    with _runtime_lock:
        lease = _leases.pop(command.task_id, None)
        _stops.pop(command.task_id, None)
    if lease:
        # The DB owner is cleared in this transaction; release only memory here.
        lease._released = True
        lease.limiter_lease.release()


def close_command(session: Session, command: StartupExecution, status: str, *, reason=None, report=None, release_device=True):
    command.status = status
    command.updated_at = datetime.now()
    command.terminal_reason = reason or command.terminal_reason
    command.lease_expires_at = None
    task = session.get(FastbotTask, command.task_id)
    if task:
        task.status = status
        task.finished_at = datetime.now()
        session.add(task)
    if report is None:
        existing = session.exec(select(FastbotReport).where(FastbotReport.task_id == command.task_id)).first()
        report = {key: json.loads(getattr(existing, key) or ("{}" if key == "summary" else "[]"))
                  for key in REPORT_COLUMNS} if existing else {}
    report.setdefault("summary", {})["status"] = status
    if reason:
        report["summary"]["error"] = reason
    saved = save_report(session, command, report)
    if task:
        summary = report.get("summary") or {}
        task.total_crashes = int(summary.get("total_crashes", 0) or 0)
        task.total_anrs = int(summary.get("total_anrs", 0) or 0)
    if release_device:
        _release_reservation(session, command)
    else:
        # A missing heartbeat is not proof that the physical device stopped.
        # Keep the reservation until this attempt acknowledges completion/recovery.
        with _runtime_lock:
            lease = _leases.pop(command.task_id, None)
            _stops.pop(command.task_id, None)
        if lease:
            lease._released = True
            lease.limiter_lease.release()
        command.reservation_release_at = datetime.now() + timedelta(seconds=STOP_GRACE_SECONDS)
    session.add(command)
    return saved


def submit_commands(session: Session, user, config: dict, devices: list[Device], *, local_run_id=None) -> list[StartupExecution]:
    request_id = str(config.get("request_id") or uuid.uuid4())
    request_key = f"{user.id}:{request_id}"
    digest = config_digest(config)
    existing = session.exec(select(StartupExecution).where(StartupExecution.request_key == request_key)).all()
    if existing:
        if {c.device_serial for c in existing} != {d.serial for d in devices} or any(c.config_digest != digest for c in existing):
            raise HTTPException(409, "request_id 已用于不同的启动配置")
        return list(existing)
    now = datetime.now()
    commands = []
    try:
        for device in devices:
            if not can_control(device, user):
                raise HTTPException(403, f"无权控制设备 {device.serial}")
            kind, node, reason = node_for_device(session, device)
            if reason:
                raise HTTPException(400, reason)
            caps = node.startup_capabilities if node else default_capabilities(device.platform)
            validate_device_config(device, config, caps)
            external_run_id = local_run_id or str(uuid.uuid5(uuid.NAMESPACE_URL, f"startup:{request_key}:{device.serial}"))
            if session.exec(select(FastbotTask).where(FastbotTask.run_id == external_run_id)).first():
                raise HTTPException(409, "run_id 已被其他任务占用")
            task = FastbotTask(run_id=external_run_id,
                package_name=config["package_name"], device_serial=device.serial,
                duration=len(config.get("startup_modes", ["cold", "hot"])) * int(config.get("iterations", 3)),
                throttle=int(config.get("cooldown_sec", 3)) * 1000, ignore_crashes=True,
                capture_log=bool(config.get("capture_log", True)), status="QUEUED",
                executor_id=user.id, executor_name=user.full_name or user.username)
            session.add(task)
            session.flush()
            claimed = session.exec(update(Device).where(
                Device.id == device.id, Device.owner_id == device.owner_id,
                func.upper(Device.status) == "IDLE",
                or_(Device.lease_task_id.is_(None), Device.lease_task_id == ""),
                Device.client_instance_id == device.client_instance_id,
            ).values(status="BUSY", lease_task_id=f"startup:{task.id}", lease_kind="startup",
                     lease_acquired_at=now, updated_at=now))
            if claimed.rowcount != 1:
                raise HTTPException(409, f"设备正忙或归属已变化: {device.serial}")
            from backend.startup.contract import normalize_config
            frozen_config = normalize_config(config, platform=device.platform, device_serial=device.serial)
            command = StartupExecution(task_id=task.id, run_id=task.run_id, request_key=request_key,
                owner_id=user.id, node_owner_id=node.owner_id if node else user.id,
                device_serial=device.serial, platform=device.platform, executor_kind=kind,
                client_instance_id=device.client_instance_id if kind == "desktop" else None,
                config={**frozen_config, "request_id": request_id}, config_digest=digest,
                queue_expires_at=now + timedelta(seconds=QUEUE_SECONDS),
                progress={"completed_count": 0, "requested_count": task.duration, "valid_count": 0,
                          "invalid_count": 0, "phase": "queued"})
            session.add(command)
            session.flush()
            save_report(session, command, {"summary": {"startup_runs": [], "startup_aggregate": {},
                                                     "requested_count": task.duration}})
            commands.append(command)
        session.commit()
    except Exception:
        session.rollback()
        # Unique request/device constraint resolves simultaneous retries.
        existing = session.exec(select(StartupExecution).where(StartupExecution.request_key == request_key)).all()
        if existing and {c.device_serial for c in existing} == {d.serial for d in devices} and all(c.config_digest == digest for c in existing):
            return list(existing)
        raise
    return commands


def command_read(command: StartupExecution, task: FastbotTask, *, include_guard=False) -> dict:
    result = {"id": task.id, "task_id": task.id, "run_id": command.run_id,
              "platform": command.platform, "executor_kind": command.executor_kind,
              "client_instance_id": command.client_instance_id, "progress": command.progress,
              "cancel_requested": command.cancel_requested, "attempt": command.attempt,
              "status": task.status, "config": command.config,
              "device_serial": command.device_serial,
              "lease_expires_at": command.lease_expires_at.astimezone().isoformat() if command.lease_expires_at else None}
    if include_guard:
        result["claim_token"] = command.claim_token
    return result


def _acquire_execution_lease(command: StartupExecution, db_engine) -> DeviceExecutionLease:
    lease = DeviceExecutionLease.acquire(user_id=command.owner_id, serial=command.device_serial,
        task_id=f"startup:{command.task_id}", kind="startup", platform=command.platform,
        reserved_owner=True, check_legacy_fastbot=False, timeout=0.1, db_engine=db_engine)
    with _runtime_lock:
        _leases[command.task_id] = lease
        # The server starts heartbeat/shutdown control before resource waits.
        # Keep that exact event so every cancellation path reaches its runner.
        _stops.setdefault(command.task_id, threading.Event())
    return lease


def claim_command(session: Session, user, instance_id: str, *, task_id=None, capabilities=None, active_run_ids=None) -> Optional[dict]:
    node = register_instance(session, user, instance_id, capabilities)
    session.commit()
    if (node.startup_capabilities or {}).get("protocol_version") != PROTOCOL_VERSION or not (node.startup_capabilities or {}).get("web_dispatch"):
        raise HTTPException(409, "客户端尚未提供支持的冷热启动能力")
    reap_commands(session)
    query = select(StartupExecution).where(StartupExecution.client_instance_id == node.id,
        StartupExecution.node_owner_id == user.id, StartupExecution.status.in_(["QUEUED", "RUNNING"]))
    if task_id is not None:
        query = query.where(StartupExecution.task_id == task_id)
    if active_run_ids:
        query = query.where(StartupExecution.run_id.notin_(active_run_ids))
    commands = session.exec(query.order_by(case((StartupExecution.status == "QUEUED", 0), else_=1), StartupExecution.id)).all()
    for command in commands:
        device = session.exec(select(Device).where(Device.serial == command.device_serial)).first()
        if not device or device.client_instance_id != node.id or device.owner_id != node.owner_id:
            close_command(session, command, "ABORTED", reason="设备归属已改变，未再次启动",
                          release_device=command.status == "QUEUED")
            session.commit()
            continue
        validate_device_config(device, command.config, node.startup_capabilities)
        if command.status == "RUNNING":
            return command_read(command, session.get(FastbotTask, command.task_id), include_guard=True)
        token = secrets.token_urlsafe(32)
        now = datetime.now()
        changed = session.exec(update(StartupExecution).where(StartupExecution.id == command.id,
            StartupExecution.status == "QUEUED", StartupExecution.attempt == command.attempt).values(
                status="RUNNING", attempt=command.attempt + 1, claim_token=token,
                lease_expires_at=now + timedelta(seconds=LEASE_SECONDS), updated_at=now))
        if changed.rowcount != 1:
            session.rollback()
            continue
        task = session.get(FastbotTask, command.task_id)
        task.status = "RUNNING"
        task.started_at = now
        session.add(task)
        session.commit()
        session.refresh(command)
        try:
            _acquire_execution_lease(command, session.get_bind())
        except Exception as exc:
            close_command(session, command, "FAILED", reason=f"执行资源不可用: {exc}")
            session.commit()
            raise HTTPException(409, "设备执行资源已被其他任务占用") from exc
        return command_read(command, task, include_guard=True)
    return None


def guard_command(session: Session, user, task_id: int, instance_id: str, attempt: int,
                  claim_token: str, *, allow_terminal=False) -> StartupExecution:
    reap_commands(session)
    command = execution_for_task(session, task_id)
    if not command:
        raise HTTPException(404, "冷热启动命令不存在")
    # An administrator may submit to a user's node, but cannot impersonate it.
    if command.node_owner_id != user.id or command.client_instance_id != instance_id:
        raise HTTPException(403, "执行节点或账号与任务不一致")
    if command.attempt != attempt or not secrets.compare_digest(command.claim_token or "", claim_token or ""):
        raise HTTPException(409, "执行认领已失效，请保留本地结果")
    if command.status in TERMINAL and not allow_terminal:
        raise HTTPException(409, "任务已结束，不能继续执行或修改进度")
    node = session.get(ClientInstance, instance_id)
    if not allow_terminal and (not node or node.logged_out_at or node.owner_id != user.id):
        raise HTTPException(409, "客户端登录已结束，请停止采集后重新登录同步")
    device = session.exec(select(Device).where(Device.serial == command.device_serial)).first()
    if not allow_terminal and (not device or device.owner_id != user.id or device.client_instance_id != instance_id):
        raise HTTPException(409, "设备归属已改变，停止当前测试")
    return command


def normalize_progress(payload: dict, summary=None) -> dict:
    raw = dict(payload or {})
    summary = summary or {}
    return {"completed_count": int(raw.get("completed_count", raw.get("completed", summary.get("completed_count", 0))) or 0),
            "requested_count": int(raw.get("requested_count", raw.get("requested", summary.get("requested_count", 0))) or 0),
            "valid_count": int(raw.get("valid_count", summary.get("valid_count", 0)) or 0),
            "invalid_count": int(raw.get("invalid_count", summary.get("invalid_count", 0)) or 0),
            "phase": raw.get("phase", raw.get("status", "running")),
            "current_mode": raw.get("current_mode"), "current_iteration": raw.get("current_iteration")}


def record_progress(session: Session, command: StartupExecution, progress: dict, partial_report=None):
    fenced = session.exec(update(StartupExecution).where(
        StartupExecution.id == command.id, StartupExecution.status == "RUNNING",
        StartupExecution.attempt == command.attempt, StartupExecution.claim_token == command.claim_token,
    ).values(updated_at=datetime.now()))
    if fenced.rowcount != 1:
        session.rollback()
        raise HTTPException(409, "任务已经结束，迟到的进度未写入")
    session.refresh(command)
    summary = (partial_report or {}).get("summary") or {}
    incoming = normalize_progress(progress or summary.get("progress") or {}, summary)
    if incoming["completed_count"] >= int(command.progress.get("completed_count", 0)):
        command.progress = incoming
        if partial_report:
            save_report(session, command, partial_report)
    command.updated_at = datetime.now()
    command.lease_expires_at = datetime.now() + timedelta(seconds=LEASE_SECONDS)
    session.add(command)
    session.commit()
    return {"task_id": command.task_id, "status": command.status,
            "cancel_requested": command.cancel_requested,
            "lease_expires_at": command.lease_expires_at.astimezone().isoformat()}


def finish_command(session: Session, command: StartupExecution, status: str, report: dict) -> dict:
    if status not in TERMINAL:
        raise HTTPException(400, "收尾状态必须是 COMPLETED/FAILED/ABORTED")
    # Obtain the write reservation before inspecting the final state; a late
    # progress/finish cannot race an already committed terminal result.
    session.exec(update(StartupExecution).where(
        StartupExecution.id == command.id, StartupExecution.attempt == command.attempt,
        StartupExecution.claim_token == command.claim_token,
    ).values(updated_at=datetime.now()))
    session.refresh(command)
    if command.status in TERMINAL:
        # Recovery may add missing evidence to an interrupted attempt. Never
        # change the terminal result or overwrite a completed report on retries.
        if command.status == "ABORTED" and command.terminal_reason:
            report.setdefault("summary", {})["recovered_partial_result"] = True
            report["summary"]["status"] = "ABORTED"
            save_report(session, command, report)
            _release_reservation(session, command)
            session.commit()
        report_row = session.exec(select(FastbotReport).where(FastbotReport.task_id == command.task_id)).first()
    else:
        if command.cancel_requested:
            status = "ABORTED"
        summary = report.get("summary") or {}
        command.progress = normalize_progress(summary.get("progress") or {}, summary)
        report_row = close_command(session, command, status, report=report)
        session.commit()
    return {"task_id": command.task_id, "report_id": report_row.id,
            "status": command.status, "cancel_requested": command.cancel_requested}


def cancel_command(session: Session, command: StartupExecution, user):
    if user.role != "admin" and command.owner_id != user.id:
        raise HTTPException(403, "无权取消此启动任务")
    session.exec(update(StartupExecution).where(StartupExecution.id == command.id).values(updated_at=datetime.now()))
    session.refresh(command)
    if command.status in TERMINAL:
        session.commit()
        return
    command.cancel_requested = True
    if command.status == "QUEUED":
        close_command(session, command, "ABORTED", reason="排队期间已取消")
    else:
        with _runtime_lock:
            stop = _stops.get(command.task_id)
        if stop:
            stop.set()
        session.add(command)
    session.commit()


def reap_commands(session: Session, *, restart=False) -> int:
    now = datetime.now()
    changed = 0
    quarantined = session.exec(select(StartupExecution).where(
        StartupExecution.status == "ABORTED", StartupExecution.reservation_release_at.is_not(None),
        StartupExecution.reservation_release_at <= now)).all()
    for command in quarantined:
        _release_reservation(session, command)
        summary = _summary(session, command.task_id)
        summary.update(reservation_released_after_grace=True, measurement_resumed=False,
                       stop_grace_seconds=STOP_GRACE_SECONDS)
        report = session.exec(select(FastbotReport).where(FastbotReport.task_id == command.task_id)).first()
        if report:
            report.summary = json.dumps(summary, ensure_ascii=False)
            session.add(report)
        changed += 1
    commands = session.exec(select(StartupExecution).where(StartupExecution.status.in_(["QUEUED", "RUNNING"]))).all()
    for command in commands:
        reason = None
        if command.status == "QUEUED" and command.queue_expires_at < now:
            reason = "等待执行节点超时，任务未启动"
        elif command.status == "RUNNING" and (restart or not command.lease_expires_at or command.lease_expires_at < now):
            with _runtime_lock:
                live_stop = _stops.get(command.task_id)
            if command.executor_kind == "server" and live_stop and not restart:
                live_stop.set()
                command.cancel_requested = True
                session.add(command)
                continue
            reason = "服务重启中断了采集" if restart else "执行节点失联，已停止等待；已完成轮次保留"
        if reason:
            # Fence the stale snapshot against a heartbeat/final report that
            # committed while this sweep was reading other commands.
            condition = StartupExecution.status == command.status
            if command.status == "RUNNING" and not restart:
                condition = condition & or_(StartupExecution.lease_expires_at.is_(None), StartupExecution.lease_expires_at < now)
            changed_row = session.exec(update(StartupExecution).where(
                StartupExecution.id == command.id, condition,
            ).values(updated_at=now))
            if changed_row.rowcount != 1:
                continue
            session.refresh(command)
            close_command(session, command, "ABORTED", reason=reason,
                          release_device=command.executor_kind != "desktop" or command.status == "QUEUED")
            changed += 1
    session.commit()
    return changed


def recover_startup_runs():
    with Session(engine) as session:
        reap_commands(session, restart=True)


def register_startup_reaper():
    from backend.scheduler_service import get_scheduler
    def reap():
        with Session(engine) as session:
            reap_commands(session)
    get_scheduler().scheduler.add_job(reap, "interval", seconds=15, id="startup-command-reaper", replace_existing=True)


def shutdown_startup_runs():
    with _runtime_lock:
        for stop in _stops.values():
            stop.set()


def make_server_adapter(session: Session, device: Device, *, artifact_dir=None):
    if device.platform == "android":
        from backend.startup.android import AndroidStartupAdapter
        return AndroidStartupAdapter(device.serial, artifact_dir=artifact_dir)
    from backend.cross_platform_execution import resolve_ios_wda_url
    from backend.api.recording import _recording_ios_session_pool
    from backend.drivers.ios_driver import IOSDriver
    from backend.startup.ios import IOSStartupAdapter
    _recording_ios_session_pool.drain_for_exclusive_execution(device.serial)
    driver = IOSDriver(device.serial, wda_url=resolve_ios_wda_url(session, device.serial))
    return IOSStartupAdapter(driver, device_serial=device.serial, artifact_dir=artifact_dir)


def canonicalize_artifacts(result: dict, task_id: int) -> dict:
    """Only translate file paths; leave metric/status strings intact."""
    prefix = f"reports/fastbot/{task_id}/"
    def visit(value):
        if isinstance(value, list):
            return [visit(item) for item in value]
        if isinstance(value, dict):
            return {key: (prefix + str(item).lstrip("/") if key in {"path", "screenshot", "log_path", "screenshot_path", "trace_path"}
                         and isinstance(item, str) and item.startswith(("traces/", "logs/", "screenshots/")) else visit(item))
                    for key, item in value.items()}
        return value
    return visit(result)


def _renew_server_command(command: StartupExecution, stop: threading.Event, db_engine) -> bool:
    """Renew control state only; an analysis heartbeat never replaces a report."""
    with Session(db_engine) as session:
        now = datetime.now()
        changed = session.exec(update(StartupExecution).where(
            StartupExecution.id == command.id, StartupExecution.executor_kind == "server",
            StartupExecution.status == "RUNNING", StartupExecution.attempt == command.attempt,
            StartupExecution.claim_token == command.claim_token,
        ).values(lease_expires_at=now + timedelta(seconds=LEASE_SECONDS), updated_at=now))
        session.commit()
        if changed.rowcount != 1:
            stop.set()
            return False
        current = session.get(StartupExecution, command.id)
        if current.cancel_requested:
            stop.set()
        return True


async def _server_heartbeat(command: StartupExecution, stop: threading.Event, db_engine):
    from backend.startup.runtime import call_sync
    while True:
        try:
            if not await call_sync(_renew_server_command, command, stop, db_engine):
                return
        except Exception:
            logger.exception("Startup task %s heartbeat failed", command.task_id)
        await asyncio.sleep(SERVER_HEARTBEAT_SECONDS)


async def _analyze_server_startup_traces(result: dict, command: StartupExecution, artifact_dir: Path):
    from backend.startup.trace_analysis import analyze_startup_trace
    root = artifact_dir.resolve()
    prefix = f"reports/fastbot/{command.task_id}/"
    for artifact in result.get("trace_artifacts", []):
        if artifact.get("analysis_status") not in {None, "PENDING"}:
            continue
        relative = artifact.get("path")
        if isinstance(relative, str) and relative.startswith(prefix):
            relative = relative[len(prefix):]
        path = root / str(relative or "")
        valid = (isinstance(relative, str) and relative.startswith("traces/")
                 and relative.endswith(".perfetto-trace") and not path.is_symlink()
                 and path.resolve().is_relative_to(root))
        if not valid:
            analysis = {"analysis_status": "UNAVAILABLE", "reason": "原轮次 Trace 路径无效"}
        else:
            try:
                analysis = await asyncio.to_thread(analyze_startup_trace, path,
                    command.config["package_name"], platform_name=command.platform)
            except Exception as exc:
                analysis = {"analysis_status": "UNAVAILABLE", "reason": f"原轮次 Trace 分析失败：{exc}"}
        # Infrastructure errors have a terminal, visible state, never PENDING.
        # Preserve the more specific tool status inside the evidence payload.
        status = analysis.get("analysis_status")
        if status not in {"ANALYZED", "PARTIAL", "UNAVAILABLE"}:
            analysis["source_status"] = status or "FAILED"
            analysis["analysis_status"] = "UNAVAILABLE"
        analysis["analysis_level"] = "startup_evidence"
        artifact.update(analysis_status=analysis["analysis_status"], analysis_level="startup_evidence", analysis=analysis)
        if analysis.get("reason"):
            artifact["analysis_note"] = analysis["reason"]
    result.setdefault("summary", {})["analyzed_trace_count"] = sum(
        artifact.get("analysis_status") == "ANALYZED" for artifact in result.get("trace_artifacts", []))


async def execute_server_command(task_id: int, *, db_engine=None):
    from backend.fastbot.reporting import FASTBOT_REPORTS_DIR
    from backend.startup.runner import run_startup
    target_engine = db_engine or engine
    adapter = None
    command = None
    heartbeat = None
    runner_owns_cleanup = False
    try:
        with Session(target_engine) as session:
            command = execution_for_task(session, task_id)
            if not command or command.status != "QUEUED" or command.executor_kind != "server":
                return
            changed = session.exec(update(StartupExecution).where(
                StartupExecution.id == command.id, StartupExecution.status == "QUEUED",
            ).values(status="RUNNING", attempt=command.attempt + 1,
                     claim_token=secrets.token_urlsafe(32),
                     lease_expires_at=datetime.now() + timedelta(seconds=LEASE_SECONDS)))
            if changed.rowcount != 1:
                session.rollback()
                return
            task = session.get(FastbotTask, task_id)
            task.status = "RUNNING"
            task.started_at = datetime.now()
            session.add(command)
            session.add(task)
            session.commit()
            session.refresh(command)
            config = dict(command.config)
        with _runtime_lock:
            stop = _stops.setdefault(task_id, threading.Event())
        # Start before limiter acquisition and WDA construction. Neither a
        # long preflight nor offline trace analysis implies executor loss.
        heartbeat = asyncio.create_task(_server_heartbeat(command, stop, target_engine))
        await asyncio.to_thread(_acquire_execution_lease, command, target_engine)
        artifact_dir = Path(FASTBOT_REPORTS_DIR) / str(task_id)
        with Session(target_engine) as session:
            current = execution_for_task(session, task_id)
            if current.cancel_requested:
                stop.set()
            device = session.exec(select(Device).where(Device.serial == command.device_serial)).one()
            adapter = await asyncio.to_thread(make_server_adapter, session, device,
                artifact_dir=artifact_dir)
        async def progress(report):
            report = canonicalize_artifacts(report, task_id)
            with Session(target_engine) as session:
                current = execution_for_task(session, task_id)
                if current and current.status not in TERMINAL:
                    if current.cancel_requested:
                        stop.set()
                    record_progress(session, current, (report.get("summary") or {}).get("progress") or {}, report)
        runner_owns_cleanup = True
        result = await run_startup(adapter, config, stop_event=stop, progress=progress)
        # The runner has awaited all device IO and adapter.close(). These are
        # bounded local evidence queries on the retained original sample.
        await _analyze_server_startup_traces(result, command, artifact_dir)
        result = canonicalize_artifacts(result, task_id)
        with Session(target_engine) as session:
            current = execution_for_task(session, task_id)
            if current and current.status not in TERMINAL:
                finish_command(session, current, str((result.get("summary") or {}).get("status") or "COMPLETED"), result)
    except Exception as exc:
        logger.exception("Startup task %s failed", task_id)
        with Session(target_engine) as session:
            current = execution_for_task(session, task_id)
            if current and current.status not in TERMINAL:
                close_command(session, current, "FAILED", reason=str(exc))
                session.commit()
    finally:
        if adapter and not runner_owns_cleanup and hasattr(adapter, "close"):
            try:
                returned = adapter.close()
                if hasattr(returned, "__await__"):
                    await returned
            except Exception:
                logger.exception("Startup adapter close failed")
        if heartbeat:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)


def execute_server_batch(task_ids: list[int]):
    async def run():
        await asyncio.gather(*(execute_server_command(task_id) for task_id in task_ids))
    asyncio.run(run())
