"""桌面客户端兼容性回放上报面（P4 功能2：回放执行本地化支撑）。

参考 client-runs 模式新增。桌面引擎本地执行「升级后链路回放」（installed_replay）
后经本面上报落库；重资产（多设备矩阵 / 证据资产 CAS / 视觉 diff 对比）保留云端。

契约（供桌面 engine/core/compatibility_service.py 与 cloud_bridge 对齐）：
- POST /api/client-compatibility/runs
    创建客户端执行的 INSTALLED_REPLAY run：installed_package 由桌面本机 adb 读取
    后上报（云端不做设备 adb），plan/device 摘要对齐桌面本地预检结果；run 与
    cells 建档后保持 PENDING（client_executed=True），云端不派发后台执行 worker。
- POST /api/client-compatibility/runs/{run_id}/report
    上报回放结果：逐 cell × 逐 chain 幂等 upsert CompatibilityPageResult（含
    逐步 trace、checkpoints、边界证据），截图 PNG / hierarchy XML 经证据资产
    CAS 落云端；全部 cell 终态后收尾 run 状态（PASS/WARNING/FAIL/ERROR/ABORTED）。
    可分次上报（先 RUNNING 中间态、后终态），重复上报同一 chain 以最后一次为准。

仅支持 execution_mode=INSTALLED_REPLAY 且 client_executed=True 的 run；鉴权
get_current_user，写操作限 run 创建人本人或 admin。对比（COMPARISON）与多设备
矩阵仍走云端 POST /api/compatibility/runs。

安全红线：token 只经 Authorization 头；无明文凭据落库。
"""
from __future__ import annotations

import base64
import binascii
import logging
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session, col, select

from backend.api import deps
from backend.api.compatibility import (
    TERMINAL_STATUSES,
    _now,
    _persist_replay_capture,
    _record_replay_result,
    _replay_preflight_result,
    _run_read,
    _update_run_summary,
    _validate_devices,
    _validate_replay_source,
)
from backend.compatibility_replay import (
    package_snapshot_digest,
    select_and_freeze_chains,
)
from backend.database import get_session
from backend.feature_flags import FLAG_COMPATIBILITY_INSTALLED_REPLAY, is_flag_enabled
from backend.models import CompatibilityCell, CompatibilityRun, User
from backend.schemas import CompatibilityRunCreate, CompatibilityRunRead

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_CAPTURE_BYTES = 8 * 1024 * 1024


class ClientReplayChainReport(BaseModel):
    """单条冻结链路的回放结果（chain_id 对齐 plan.chains 的 chain_id/path_key）。"""

    chain_id: str
    status: str = "FAIL"  # PASS | WARNING | FAIL
    reason: Optional[str] = None
    failure_type: Optional[str] = None
    failed_step_index: Optional[int] = None
    completed_checkpoints: int = 0
    duration_ms: float = 0
    warning_codes: List[str] = Field(default_factory=list)
    boundary_evidence: Optional[str] = None
    trace: List[Dict[str, Any]] = Field(default_factory=list)
    # 证据资产（可选）：PNG base64 / hierarchy XML 文本，落云端 CAS
    screenshot_png_base64: Optional[str] = None
    xml_text: Optional[str] = None
    activity: Optional[str] = None


class ClientReplayCellReport(BaseModel):
    """单设备执行单元结果。"""

    device_serial: str
    status: str = "FAIL"  # PASS | WARNING | FAIL | ERROR | ABORTED
    error_message: Optional[str] = None
    current_stage: Optional[str] = None
    installed_package_snapshot: Optional[Dict[str, Any]] = None
    chains: List[ClientReplayChainReport] = Field(default_factory=list)


class ClientReplayReportPayload(BaseModel):
    cells: List[ClientReplayCellReport] = Field(default_factory=list)


class ClientReplayRunCreate(CompatibilityRunCreate):
    """客户端创建回放 run：附加桌面本机读取的安装包快照。"""

    installed_package: Dict[str, Any] = Field(default_factory=dict)


def _decode_png(raw: Optional[str]) -> Optional[bytes]:
    if not raw:
        return None
    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"截图 base64 非法: {exc}") from exc
    if len(data) > MAX_CAPTURE_BYTES:
        raise HTTPException(status_code=413, detail="截图超过大小上限")
    return data


def _frozen_chain_index(run: CompatibilityRun) -> Dict[str, Dict[str, Any]]:
    """run.page_set_snapshot（冻结链路）按 chain_id / path_key 建索引。"""
    index: Dict[str, Dict[str, Any]] = {}
    for item in run.page_set_snapshot or []:
        if not isinstance(item, dict):
            continue
        for key in (
            item.get("chain_id"),
            item.get("path_key"),
            item.get("prefix_path_key"),
            item.get("id"),
        ):
            if key not in (None, ""):
                index.setdefault(str(key), item)
    return index


def _require_client_run(session: Session, run_id: int, current_user: User) -> CompatibilityRun:
    run = session.get(CompatibilityRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="兼容性任务不存在")
    deps.ensure_owner_or_admin(run.user_id, current_user)
    if str(run.execution_mode or "").upper() != "INSTALLED_REPLAY":
        raise HTTPException(
            status_code=400,
            detail="client-compatibility 仅支持升级后链路回放（installed_replay）",
        )
    if not bool(run.client_executed):
        raise HTTPException(
            status_code=409,
            detail="该任务由云端执行，不能接收客户端上报",
        )
    return run


@router.post("/runs", response_model=CompatibilityRunRead)
def create_client_replay_run(
    payload: ClientReplayRunCreate,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    """创建客户端本地执行的回放 run（云端不派发后台 worker，等待 report 收尾）。"""
    if not is_flag_enabled(session, FLAG_COMPATIBILITY_INSTALLED_REPLAY):
        raise HTTPException(status_code=404, detail="升级后链路回放尚未启用")
    if str(payload.execution_mode or "").lower() != "installed_replay":
        raise HTTPException(
            status_code=400,
            detail="client-compatibility 仅支持 installed_replay 回放",
        )
    devices = _validate_devices(session, payload.device_serials)
    inspection_source = _validate_replay_source(
        session,
        inspection_run_id=int(payload.inspection_run_id or 0),
        branch_key=str(payload.replay_branch_key or ""),
    )

    installed = dict(payload.installed_package or {})
    if not installed:
        raise HTTPException(
            status_code=400,
            detail="缺少设备安装包快照（installed_package，由客户端本机 adb 读取上报）",
        )
    installed.setdefault("package_name", inspection_source.package_name)
    installed.setdefault("installed", True)
    installed.setdefault("source", "device")

    replay_preflight_data = _replay_preflight_result(
        session,
        source_run=inspection_source,
        branch_key=str(payload.replay_branch_key or ""),
        installed_package=installed,
        max_chains=20,
    )
    blockers = list(replay_preflight_data.get("blockers") or [])
    if blockers:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "回放预检未通过",
                "blockers": blockers,
                "warnings": replay_preflight_data.get("warnings") or [],
            },
        )
    if payload.device_snapshot_digest and (
        replay_preflight_data["device_snapshot_digest"] != payload.device_snapshot_digest
    ):
        raise HTTPException(
            status_code=409,
            detail="设备安装包在预检后发生变化，请重新预检",
        )
    if payload.plan_digest and replay_preflight_data["plan_digest"] != payload.plan_digest:
        raise HTTPException(
            status_code=409,
            detail="巡检报告的回放计划已变化，请重新预检",
        )

    plan = {
        "plan_version": replay_preflight_data["plan_version"],
        "digest": replay_preflight_data["plan_digest"],
        "chains": replay_preflight_data["chains"],
        "summary": replay_preflight_data["summary"],
        "excluded": replay_preflight_data["excluded"],
    }
    try:
        pages = select_and_freeze_chains(
            plan,
            payload.selected_path_ids or payload.selected_chain_ids,
            source_run=inspection_source,
            branch_key=str(payload.replay_branch_key or ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    page_set_name = f"巡检 #{inspection_source.id} · {payload.replay_branch_key} 链路回放"
    run = CompatibilityRun(
        name=payload.name,
        page_set_id=None,
        page_set_name=page_set_name,
        page_set_snapshot=pages,
        source_type=payload.source_type,
        inspection_run_id=inspection_source.id,
        inspection_state_ids=list(payload.inspection_state_ids or []),
        inspection_observation_ids=list(payload.inspection_observation_ids or []),
        source_coverage_snapshot={},
        package_name=inspection_source.package_name,
        execution_mode="INSTALLED_REPLAY",
        replay_branch_key=str(payload.replay_branch_key or ""),
        replay_plan_version=int(replay_preflight_data["plan_version"]),
        replay_plan_digest=str(replay_preflight_data["plan_digest"] or ""),
        replay_duration_seconds=int(payload.duration_seconds or 3600),
        source_package_snapshot=dict(replay_preflight_data["source_package"]),
        target_package_snapshot={
            **dict(replay_preflight_data["installed_package"]),
            "preflight_warnings": list(replay_preflight_data.get("warnings") or []),
        },
        manual_install_confirmed_at=_now(),
        compare_mode=payload.compare_mode,
        baseline_device_serial=payload.baseline_device_serial,
        mode=payload.mode,
        env_id=payload.env_id,
        device_serials=list(payload.device_serials),
        thresholds={},
        status="PENDING",
        total_cells=len(devices),
        total_pages=0,
        user_id=current_user.id,
        executor_name=current_user.full_name or current_user.username,
        client_executed=True,
        created_at=_now(),
    )
    session.add(run)
    session.commit()
    session.refresh(run)

    for device in devices:
        display = device.custom_name or device.market_name or device.model or device.serial
        session.add(
            CompatibilityCell(
                run_id=run.id,
                device_serial=device.serial,
                device_info=display,
                os_version=device.os_version or device.android_version,
                resolution=device.resolution,
                status="PENDING",
                preflight_at=_now(),
                installed_package_snapshot=dict(
                    replay_preflight_data.get("installed_package") or {}
                ),
                old_install_status="SKIPPED",
                new_install_status="SKIPPED",
            )
        )
    session.commit()
    logger.info(
        "[client-compatibility] 创建客户端回放 run #%s（%s 台设备，%s 条链路）",
        run.id,
        len(devices),
        len(pages),
    )
    return _run_read(session, run, include_detail=True)


@router.post("/runs/{run_id}/report")
def report_client_replay_results(
    run_id: int,
    payload: ClientReplayReportPayload,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    """上报客户端本地回放结果（逐 cell × 逐 chain 幂等 upsert，全终态后收尾 run）。"""
    run = _require_client_run(session, run_id, current_user)
    if str(run.status or "").upper() in TERMINAL_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"兼容性任务已收尾（{run.status}），不能重复上报",
        )

    chains_index = _frozen_chain_index(run)
    reported_cells = 0
    reported_chains = 0
    for cell_report in payload.cells:
        cell = session.exec(
            select(CompatibilityCell).where(
                CompatibilityCell.run_id == run.id,
                CompatibilityCell.device_serial == cell_report.device_serial,
            )
        ).first()
        if cell is None:
            raise HTTPException(
                status_code=404,
                detail=f"兼容性任务不存在设备执行单元: {cell_report.device_serial}",
            )
        if cell_report.installed_package_snapshot:
            cell.installed_package_snapshot = dict(cell_report.installed_package_snapshot)
        if cell_report.current_stage is not None:
            cell.current_stage = cell_report.current_stage

        for chain_report in cell_report.chains:
            chain = chains_index.get(str(chain_report.chain_id))
            if chain is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"回放计划不存在所选链路: {chain_report.chain_id}",
                )
            capture = None
            screenshot = _decode_png(chain_report.screenshot_png_base64)
            if screenshot is not None or chain_report.xml_text:
                capture = SimpleNamespace(
                    xml=str(chain_report.xml_text or ""),
                    screenshot_png=screenshot or b"",
                    activity=str(chain_report.activity or "") or None,
                )
            snapshot = None
            if capture is not None:
                snapshot = _persist_replay_capture(
                    session,
                    run=run,
                    cell=cell,
                    chain=chain,
                    capture=capture,
                )
            _record_replay_result(
                session,
                run=run,
                cell=cell,
                chain=chain,
                result={
                    "status": str(chain_report.status or "FAIL").upper(),
                    "reason": chain_report.reason or "",
                    "failure_type": chain_report.failure_type or "",
                    "failed_step_index": chain_report.failed_step_index,
                    "completed_checkpoints": chain_report.completed_checkpoints,
                    "duration_ms": chain_report.duration_ms,
                    "warning_codes": list(chain_report.warning_codes or []),
                    "boundary_evidence": chain_report.boundary_evidence or "",
                    "trace": [dict(item) for item in chain_report.trace or []],
                },
                snapshot=snapshot,
            )
            reported_chains += 1

        status = str(cell_report.status or "FAIL").upper()
        if status not in TERMINAL_STATUSES:
            # 中间态（RUNNING 等）：只更新阶段，不收尾
            cell.status = status
            session.add(cell)
            session.commit()
            reported_cells += 1
            continue
        cell.status = status
        cell.error_message = cell_report.error_message
        cell.finished_at = cell.finished_at or _now()
        session.add(cell)
        session.commit()
        reported_cells += 1

    cells = session.exec(
        select(CompatibilityCell).where(CompatibilityCell.run_id == run.id)
    ).all()
    all_terminal = bool(cells) and all(
        str(item.status or "").upper() in TERMINAL_STATUSES for item in cells
    )
    # 状态汇总统一走云端 _update_run_summary（按页面结果 + 单元状态汇总，
    # 全 cell 终态才收尾 run；中间上报转 RUNNING）
    _update_run_summary(session, run.id, final=all_terminal)
    session.refresh(run)
    logger.info(
        "[client-compatibility] run #%s 上报 %s 个单元 / %s 条链路（final=%s）",
        run.id,
        reported_cells,
        reported_chains,
        all_terminal,
    )
    return {
        "success": True,
        "run_id": run.id,
        "run_status": run.status,
        "finished": all_terminal,
        "reported_cells": reported_cells,
        "reported_chains": reported_chains,
    }
