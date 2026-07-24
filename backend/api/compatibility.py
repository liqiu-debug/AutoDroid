"""Visual compatibility testing for Android production APKs."""
from __future__ import annotations

import asyncio
import io
import logging
import re
import shutil
import shlex
import threading
import time
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Annotated, Any, Callable, Dict, List, Optional, Sequence, Tuple

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy import or_
from sqlmodel import Session, select, func, col

from backend.api import deps
from backend.api.packages import (
    _resolve_package_file_path,
    _run_adb_command,
    install_app_package_to_device,
)
from backend.artifact_store import (
    AssetCapacityExceeded,
    AssetGone,
    AssetNotFound,
    RETENTION_HOT,
    RETENTION_PINNED,
    content_addressed_assets_enabled,
    ensure_asset_capacity_for_new_run,
    read_asset,
    release_owner_references,
    retention_expiry,
    store_file,
    store_image_bytes,
    store_text,
    upsert_reference,
)
from backend.cross_platform_execution import (
    restore_device_status_after_execution,
    run_case_with_standard_runner,
)
from backend.database import engine, get_session
from backend.device_execution_lease import (
    DeviceExecutionLease,
    legacy_fastbot_device_locked,
)
from backend.compatibility_replay import (
    build_replay_plan,
    branch_config_for_run,
    entry_case_safety_issues,
    package_snapshot_digest,
    plan_digest_matches,
    read_installed_package,
    read_installed_package_sync,
    select_and_freeze_chains,
    source_branch_exists,
    source_package_snapshot,
)
from backend.models import (
    AppPackage,
    AssetReference,
    CompatPageSet,
    CompatibilityCell,
    CompatibilityPageResult,
    CompatibilityRun,
    Device,
    InspectionRun,
    InspectionObservation,
    InspectionState,
    StoredAsset,
    TestCase,
    User,
)
from backend.inspection.device import (
    InspectionAborted,
    connect_android,
    ready_assertion_exists,
)
from backend.inspection.engine import (
    _deserialize_action,
    _environment_secret_values,
    _prepare_branch,
    _replay_path,
    _resolve_input_value,
    resolve_inspection_asset,
)
from backend.inspection.sanitizer import InspectionArtifactSanitizer
from backend.inspection.replay import execute_replay_chain
from backend.paths import project_path
from backend.feature_flags import (
    FLAG_COMPATIBILITY_INSTALLED_REPLAY,
    FLAG_MODEL_INSPECTION,
    is_flag_enabled,
)
from backend.schemas import (
    CompatPageDefinition,
    CompatPageSetCreate,
    CompatPageSetRead,
    CompatPageSetUpdate,
    CompatibilityCellRead,
    CompatibilityPageResultRead,
    CompatibilityReplayIssue,
    CompatibilityReplayPreflightRead,
    CompatibilityReplayPreflightRequest,
    CompatibilityRunCreate,
    CompatibilityRunRead,
    PaginatedCompatibilityRunRead,
)
from backend.utils.pydantic_compat import dump_model
from backend.run_control import register_device_abort, unregister_device_abort

logger = logging.getLogger(__name__)
router = APIRouter()

TERMINAL_STATUSES = {"PASS", "WARNING", "FAIL", "ERROR", "ABORTED"}


def _compat_replay_scope(chain: Dict[str, Any]) -> str:
    value = str(
        chain.get("replay_scope") or chain.get("replay_eligibility") or "FULL"
    ).strip().upper()
    return {
        "FULL": "FULL_PATH",
        "FULL_PATH": "FULL_PATH",
        "SAFE_PREFIX": "PREFIX_TO_SAFETY_BOUNDARY",
        "PREFIX_TO_SAFETY_BOUNDARY": "PREFIX_TO_SAFETY_BOUNDARY",
        "DIAGNOSTIC_ONLY": "DIAGNOSTIC_ONLY",
    }.get(value, "NONE")


def _compat_terminal_outcome(chain: Dict[str, Any]) -> str:
    explicit = str(chain.get("terminal_outcome") or "").strip().upper()
    if explicit:
        return explicit
    outcomes = {
        str(item.get("terminal_outcome") or "NONE").strip().upper()
        for item in chain.get("terminal_boundaries") or []
        if isinstance(item, dict)
    }
    for outcome in (
        "APP_FAULT",
        "INFRA_FAULT",
        "AUTOMATION_FAILED",
        "EXTERNAL_NAVIGATION",
        "LOCATOR_FAILED",
        "SAFETY_BLOCKED",
        "BUDGET_STOP",
        "CANCELLED",
    ):
        if outcome in outcomes:
            return outcome
    return "NONE"
_CRASH_PATTERN = re.compile(r"FATAL EXCEPTION|ANR in|Application Not Responding", re.I)
_RUN_ABORT_EVENTS: Dict[int, threading.Event] = {}
_RUN_ABORT_LOCK = threading.Lock()


def _now() -> datetime:
    return datetime.now()


def _dump_page(page: Any) -> Dict[str, Any]:
    raw = dump_model(page)
    return dict(raw or {}) if isinstance(raw, dict) else {}


def _page_key(index: int, page: Dict[str, Any]) -> str:
    candidate = str(page.get("key") or "").strip()
    if candidate:
        return re.sub(r"[^a-zA-Z0-9_.-]+", "_", candidate)[:80]
    name = str(page.get("name") or f"page_{index}").strip()
    safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "_", name).strip("_")
    return f"{index:02d}_{safe_name or 'page'}"[:80]


def _optional_page_case_id(page: Dict[str, Any]) -> Optional[int]:
    value = page.get("case_id")
    return int(value) if value not in (None, "", 0, "0") else None


def _active_compatibility_asset(
    session: Session,
    asset_id: Optional[str],
) -> Optional[StoredAsset]:
    if not asset_id:
        return None
    row = session.get(StoredAsset, asset_id)
    if row is None or row.status != "ACTIVE":
        return None
    return row


def _normalize_pages(raw_pages: List[Any]) -> List[Dict[str, Any]]:
    pages: List[Dict[str, Any]] = []
    for index, item in enumerate(raw_pages or [], start=1):
        page = _dump_page(item)
        if not page.get("key"):
            page["key"] = _page_key(index, page)
        pages.append(page)
    return pages


def _page_set_read(row: CompatPageSet) -> CompatPageSetRead:
    return CompatPageSetRead(
        id=row.id,
        name=row.name,
        description=row.description,
        pages=[CompatPageDefinition(**page) for page in _normalize_pages(row.pages or [])],
        user_id=row.user_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _page_set_snapshot_read(row: CompatibilityRun) -> Optional[CompatPageSetRead]:
    pages = _normalize_pages(row.page_set_snapshot or [])
    if not pages and not row.page_set_name:
        return None
    return CompatPageSetRead(
        id=row.page_set_id or 0,
        name=row.page_set_name or "已删除页面合集",
        description=None,
        pages=[CompatPageDefinition(**page) for page in pages],
        user_id=row.user_id,
        created_at=row.created_at,
        updated_at=None,
    )


def _page_result_read(row: CompatibilityPageResult) -> CompatibilityPageResultRead:
    return CompatibilityPageResultRead(
        id=row.id,
        run_id=row.run_id,
        cell_id=row.cell_id,
        page_key=row.page_key,
        page_name=row.page_name,
        path_key=row.path_key,
        source_state_id=row.source_state_id,
        source_observation_id=row.source_observation_id,
        evidence_level=row.evidence_level,
        failure_type=row.failure_type,
        failed_step_index=row.failed_step_index,
        replay_trace=row.replay_trace or [],
        case_id=row.case_id,
        status=row.status,
        reason=row.reason,
        required_text=row.required_text,
        baseline_screenshot_path=row.baseline_screenshot_path,
        candidate_screenshot_path=row.candidate_screenshot_path,
        diff_screenshot_path=row.diff_screenshot_path,
        baseline_xml_path=row.baseline_xml_path,
        candidate_xml_path=row.candidate_xml_path,
        baseline_screenshot_asset_id=row.baseline_screenshot_asset_id,
        candidate_screenshot_asset_id=row.candidate_screenshot_asset_id,
        diff_screenshot_asset_id=row.diff_screenshot_asset_id,
        baseline_xml_asset_id=row.baseline_xml_asset_id,
        candidate_xml_asset_id=row.candidate_xml_asset_id,
        baseline_activity=row.baseline_activity,
        candidate_activity=row.candidate_activity,
        metrics=row.metrics or {},
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _cell_read(
    session: Session,
    row: CompatibilityCell,
    include_pages: bool = True,
    *,
    page_rows: Optional[Sequence[CompatibilityPageResult]] = None,
) -> CompatibilityCellRead:
    pages: List[CompatibilityPageResultRead] = []
    if include_pages:
        resolved_page_rows = page_rows
        if resolved_page_rows is None:
            resolved_page_rows = session.exec(
                select(CompatibilityPageResult)
                .where(CompatibilityPageResult.cell_id == row.id)
                .order_by(CompatibilityPageResult.id)
            ).all()
        pages = [_page_result_read(item) for item in resolved_page_rows]

    return CompatibilityCellRead(
        id=row.id,
        run_id=row.run_id,
        device_serial=row.device_serial,
        device_info=row.device_info,
        os_version=row.os_version,
        resolution=row.resolution,
        is_baseline=bool(row.is_baseline),
        status=row.status,
        current_stage=row.current_stage,
        old_install_status=row.old_install_status,
        new_install_status=row.new_install_status,
        preflight_at=row.preflight_at,
        installed_package_snapshot=row.installed_package_snapshot or {},
        error_message=row.error_message,
        started_at=row.started_at,
        finished_at=row.finished_at,
        pages=pages,
    )


def _run_read(
    session: Session,
    row: CompatibilityRun,
    include_detail: bool = False,
    *,
    page_set: Optional[CompatPageSet] = None,
    page_set_loaded: bool = False,
) -> CompatibilityRunRead:
    if not page_set_loaded:
        page_set = session.get(CompatPageSet, row.page_set_id) if row.page_set_id else None
    page_set_read = _page_set_read(page_set) if page_set else _page_set_snapshot_read(row)
    cells: List[CompatibilityCellRead] = []
    if include_detail:
        cell_rows = session.exec(
            select(CompatibilityCell)
            .where(CompatibilityCell.run_id == row.id)
            .order_by(CompatibilityCell.id)
        ).all()
        cell_ids = [int(item.id) for item in cell_rows if item.id is not None]
        pages_by_cell: Dict[int, List[CompatibilityPageResult]] = {
            cell_id: [] for cell_id in cell_ids
        }
        if cell_ids:
            page_rows = session.exec(
                select(CompatibilityPageResult)
                .where(col(CompatibilityPageResult.cell_id).in_(cell_ids))
                .order_by(CompatibilityPageResult.cell_id, CompatibilityPageResult.id)
            ).all()
            for page_row in page_rows:
                pages_by_cell.setdefault(int(page_row.cell_id), []).append(page_row)
        cells = [
            _cell_read(
                session,
                item,
                include_pages=True,
                page_rows=pages_by_cell.get(int(item.id), []),
            )
            for item in cell_rows
        ]

    replay_snapshot = [
        dict(item)
        for item in (row.page_set_snapshot or [])
        if isinstance(item, dict)
    ]
    if str(row.execution_mode or "").upper() == "INSTALLED_REPLAY":
        snapshot_pages = replay_snapshot
    else:
        snapshot_pages = [
            CompatPageDefinition(**page)
            for page in _normalize_pages(replay_snapshot)
        ]
    return CompatibilityRunRead(
        id=row.id,
        name=row.name,
        page_set_id=row.page_set_id,
        page_set_name=row.page_set_name,
        page_set_snapshot=snapshot_pages,
        source_type=row.source_type or "page_set",
        inspection_run_id=row.inspection_run_id,
        inspection_state_ids=row.inspection_state_ids or [],
        inspection_observation_ids=row.inspection_observation_ids or [],
        source_coverage_snapshot=row.source_coverage_snapshot or {},
        old_package_id=row.old_package_id,
        new_package_id=row.new_package_id,
        package_name=row.package_name,
        execution_mode=str(row.execution_mode or "COMPARISON").lower(),
        replay_branch_key=row.replay_branch_key,
        replay_plan_version=row.replay_plan_version,
        replay_plan_digest=row.replay_plan_digest,
        duration_seconds=int(row.replay_duration_seconds or 3600),
        source_package_snapshot=row.source_package_snapshot or {},
        target_package_snapshot=row.target_package_snapshot or {},
        manual_install_confirmed_at=row.manual_install_confirmed_at,
        compare_mode=(
            row.compare_mode
            if str(row.execution_mode or "").upper() == "INSTALLED_REPLAY"
            else row.compare_mode or "version"
        ),
        baseline_device_serial=row.baseline_device_serial,
        mode=(
            row.mode
            if str(row.execution_mode or "").upper() == "INSTALLED_REPLAY"
            else row.mode or "upgrade"
        ),
        env_id=row.env_id,
        device_serials=row.device_serials or [],
        thresholds=row.thresholds or {},
        status=row.status,
        total_cells=row.total_cells,
        total_pages=row.total_pages,
        pass_count=row.pass_count,
        warning_count=row.warning_count,
        fail_count=row.fail_count,
        error_message=row.error_message,
        executor_name=row.executor_name,
        created_at=row.created_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        page_set=page_set_read,
        cells=cells,
    )


def _validate_page_set(session: Session, page_set: CompatPageSet) -> List[Dict[str, Any]]:
    pages = _normalize_pages(page_set.pages or [])
    if not pages:
        raise HTTPException(status_code=400, detail="页面集合不能为空")

    for page in pages:
        case_id = int(page.get("case_id") or 0)
        if not case_id or not session.get(TestCase, case_id):
            raise HTTPException(status_code=400, detail=f"页面用例不存在: {case_id}")
    return pages


def _validate_inspection_source(
    session: Session,
    *,
    inspection_run_id: int,
    inspection_state_ids: List[int],
    inspection_observation_ids: List[int],
    package_name: str,
) -> Tuple[InspectionRun, List[InspectionState], List[Dict[str, Any]]]:
    source_run = session.get(InspectionRun, inspection_run_id)
    if not source_run:
        raise HTTPException(status_code=404, detail="巡检来源任务不存在")
    if source_run.package_name != package_name:
        raise HTTPException(status_code=400, detail="巡检来源与测试包的 package_name 不一致")
    if str(source_run.status or "").upper() not in {"PASS", "WARNING", "FAIL"}:
        raise HTTPException(status_code=400, detail="巡检来源任务尚未完成")
    assessment = (
        dict(source_run.coverage_assessment or {})
        if isinstance(source_run.coverage_assessment, dict)
        else {}
    )
    default_eligible = bool(
        str(source_run.status or "").upper() == "PASS"
        and str(assessment.get("selected_scope_verdict") or "") == "COMPLETE"
    )
    if (
        not inspection_state_ids
        and not inspection_observation_ids
        and not default_eligible
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "该巡检报告不允许空选择自动采用路径；"
                "请人工明确选择稳定 State 或 Observation"
            ),
        )

    explicit_observations: Dict[int, InspectionObservation] = {}
    requested_observation_ids = list(inspection_observation_ids or [])
    if requested_observation_ids:
        observation_rows = session.exec(
            select(InspectionObservation).where(
                col(InspectionObservation.id).in_(requested_observation_ids)
            )
        ).all()
        observations_by_id = {int(item.id): item for item in observation_rows}
        missing_observations = set(requested_observation_ids) - set(observations_by_id)
        if missing_observations:
            raise HTTPException(
                status_code=400,
                detail=f"巡检 Observation 不存在: {sorted(missing_observations)}",
            )
        ordered_observations = [
            observations_by_id[item_id] for item_id in requested_observation_ids
        ]
        invalid_run = [
            item.id for item in ordered_observations if item.run_id != inspection_run_id
        ]
        if invalid_run:
            raise HTTPException(
                status_code=400,
                detail=f"巡检 Observation 不属于来源任务: {invalid_run}",
            )
        for observation in ordered_observations:
            if observation.state_id in explicit_observations:
                raise HTTPException(
                    status_code=400,
                    detail=f"每个状态只能选择一个 Observation: state={observation.state_id}",
                )
            explicit_observations[observation.state_id] = observation
        observed_state_ids = list(explicit_observations)
        if inspection_state_ids and set(inspection_state_ids) != set(observed_state_ids):
            raise HTTPException(
                status_code=400,
                detail="inspection_state_ids 必须与显式 Observation 的状态一一对应",
            )
        state_rows = session.exec(
            select(InspectionState).where(
                InspectionState.run_id == inspection_run_id,
                col(InspectionState.id).in_(observed_state_ids),
            )
        ).all()
        states_by_id = {int(item.id): item for item in state_rows}
        states = [states_by_id[item_id] for item_id in observed_state_ids if item_id in states_by_id]
    else:
        query = select(InspectionState).where(InspectionState.run_id == inspection_run_id)
        if inspection_state_ids:
            query = query.where(col(InspectionState.id).in_(inspection_state_ids))
        else:
            query = query.where(InspectionState.selected_for_regression == True)  # noqa: E712
        states = session.exec(
            query.order_by(InspectionState.depth, InspectionState.id)
        ).all()
    if not states:
        raise HTTPException(status_code=400, detail="没有可用的巡检稳定状态")
    requested = set(inspection_state_ids or explicit_observations)
    found = {item.id for item in states}
    if requested - found:
        raise HTTPException(
            status_code=400,
            detail=f"部分巡检状态不存在或不属于来源任务: {sorted(requested - found)}",
        )
    selected_observations: Dict[int, Optional[InspectionObservation]] = {}
    invalid_observations: List[int] = []
    for state in states:
        observation = explicit_observations.get(state.id)
        if observation is None and not explicit_observations:
            if state.representative_observation_id:
                candidate = session.get(
                    InspectionObservation,
                    state.representative_observation_id,
                )
                if (
                    candidate is not None
                    and candidate.state_id == state.id
                    and candidate.run_id == inspection_run_id
                ):
                    observation = candidate
            if observation is None:
                observation = session.exec(
                    select(InspectionObservation)
                    .where(
                        InspectionObservation.state_id == state.id,
                        InspectionObservation.run_id == inspection_run_id,
                        InspectionObservation.is_representative == True,  # noqa: E712
                    )
                    .order_by(col(InspectionObservation.captured_at).desc())
                ).first()
        selected_observations[state.id] = observation
        if explicit_observations and (
            observation is None
            or _active_compatibility_asset(session, observation.screenshot_asset_id) is None
            or _active_compatibility_asset(session, observation.xml_asset_id) is None
        ):
            invalid_observations.append(int(observation.id) if observation else state.id)
    if invalid_observations:
        raise HTTPException(
            status_code=400,
            detail=f"显式 Observation 缺少可用截图/XML 资产: {invalid_observations}",
        )

    invalid = []
    for item in states:
        observation = selected_observations.get(item.id)
        has_cas = bool(
            observation
            and _active_compatibility_asset(session, observation.screenshot_asset_id)
            and _active_compatibility_asset(session, observation.xml_asset_id)
        )
        has_legacy = bool(item.screenshot_path and item.xml_path)
        if (
            str(item.stable_status or "").upper()
            not in {"STABLE", "VERIFIED_TWICE"}
            or (not has_cas and not has_legacy)
            or item.locator_quality == "COORDINATE_ONLY"
        ):
            invalid.append(item.id)
    if invalid:
        raise HTTPException(
            status_code=400,
            detail=f"巡检状态不是稳定可回放资产: {invalid}",
        )

    snapshot = dict(source_run.profile_snapshot or {})
    branch_configs = dict(snapshot.get("branches") or {})
    input_rules = [
        dict(item) for item in snapshot.get("input_rules") or [] if isinstance(item, dict)
    ]
    sanitizer_rules = [
        dict(item)
        for item in snapshot.get("sanitizer_rules") or []
        if isinstance(item, dict)
    ]
    dynamic_patterns = [
        str(item)
        for item in snapshot.get("dynamic_text_patterns") or []
        if str(item or "").strip()
    ]
    stable_wait = float((snapshot.get("budgets") or {}).get("stable_wait_seconds") or 5.0)
    pages: List[Dict[str, Any]] = []
    for state in states:
        branch_config = branch_configs.get(state.branch_key)
        if not isinstance(branch_config, dict):
            raise HTTPException(
                status_code=400,
                detail=f"巡检状态缺少业务线快照: {state.id}",
            )
        observation = selected_observations.get(state.id)
        page = {
                "key": f"inspection_state_{state.id}",
                "name": f"{state.branch_key} · {state.activity or state.cluster_key[:8]}",
                "case_id": None,
                "settle_seconds": 0,
                "required_text": None,
                "inspection_state_id": state.id,
                "inspection_path": list(state.first_path or []),
                "branch_key": state.branch_key,
                "branch_config": dict(branch_config),
                "input_rules": input_rules,
                "sanitizer_rules": sanitizer_rules,
                "dynamic_text_patterns": dynamic_patterns,
                "stable_wait_seconds": stable_wait,
                "baseline_screenshot_path": state.screenshot_path,
                "baseline_xml_path": state.xml_path,
                "baseline_activity": state.activity,
            }
        if observation is not None:
            page.update(
                {
                    "inspection_observation_id": observation.id,
                    "baseline_screenshot_asset_id": observation.screenshot_asset_id,
                    "baseline_xml_asset_id": observation.xml_asset_id,
                }
            )
        pages.append(page)
    return source_run, states, pages


def _copy_inspection_baselines(
    *,
    session: Session,
    compatibility_run_id: int,
    inspection_run_id: int,
    pages: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    copied: List[Dict[str, Any]] = []
    reports_root = project_path("reports").resolve()
    use_cas = content_addressed_assets_enabled(session)
    for page in pages:
        item = dict(page)
        key = _page_key(len(copied) + 1, item)
        for source_field, asset_field, filename, role in (
            (
                "baseline_screenshot_path",
                "baseline_screenshot_asset_id",
                "screenshot.png",
                f"baseline:{key}:screenshot",
            ),
            (
                "baseline_xml_path",
                "baseline_xml_asset_id",
                "hierarchy.xml",
                f"baseline:{key}:xml",
            ),
        ):
            source_value = str(item.get(source_field) or "")
            existing_asset = None
            existing_asset_id = str(item.get(asset_field) or "")
            if use_cas and existing_asset_id:
                candidate_asset = session.get(StoredAsset, existing_asset_id)
                if candidate_asset is not None and candidate_asset.status == "ACTIVE":
                    try:
                        read_asset(session, candidate_asset.id, transparent=True)
                        existing_asset = candidate_asset
                    except (AssetGone, AssetNotFound):
                        existing_asset = None
            if use_cas:
                if existing_asset is None:
                    source = resolve_inspection_asset(
                        source_value,
                        run_id=inspection_run_id,
                    )
                    if not source.is_file():
                        raise HTTPException(
                            status_code=404,
                            detail=f"巡检基线文件不存在: {source_value}",
                        )
                    asset = store_file(session, source, commit=False)
                else:
                    asset = existing_asset
                item[asset_field] = asset.id
                upsert_reference(
                    session,
                    asset_id=asset.id,
                    owner_type="compatibility_run",
                    owner_id=compatibility_run_id,
                    role=role,
                    retention_class=RETENTION_PINNED,
                    pinned_reason=(
                        f"inspection baseline for compatibility run {compatibility_run_id}"
                    ),
                    commit=False,
                )
                # The source path remains the rollout fallback.  The PIN keeps
                # the CAS copy available after the source inspection is pruned.
                item[source_field] = source_value
                continue

            source = resolve_inspection_asset(
                source_value,
                run_id=inspection_run_id,
            )
            if not source.is_file():
                raise HTTPException(
                    status_code=404,
                    detail=f"巡检基线文件不存在: {source_value}",
                )
            target_dir = (
                reports_root
                / "compatibility"
                / str(compatibility_run_id)
                / "inspection_baseline"
                / key
            )
            target_dir.mkdir(parents=True, exist_ok=True)
            destination = (target_dir / filename).resolve()
            destination.relative_to(reports_root)
            shutil.copy2(source, destination)
            item[source_field] = destination.relative_to(reports_root).as_posix()
        copied.append(item)
    if use_cas:
        session.commit()
    return copied


def _inspection_page_groups(
    pages: List[Dict[str, Any]],
) -> List[Tuple[str, List[Dict[str, Any]]]]:
    groups: Dict[str, List[Dict[str, Any]]] = {}
    order: List[str] = []
    for page in pages:
        key = str(page.get("branch_key") or "").strip() or "default"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(page)
    return [(key, groups[key]) for key in order]


def _prepare_inspection_page_group(
    *,
    serial: str,
    page: Dict[str, Any],
    abort_event: threading.Event,
) -> None:
    config = page.get("branch_config")
    if not isinstance(config, dict):
        raise RuntimeError("巡检路径缺少业务线配置快照")
    _prepare_branch(
        device=connect_android(serial),
        branch_config=dict(config),
        device_serial=serial,
        abort_event=abort_event,
    )


def _validate_packages(session: Session, old_package_id: Optional[int], new_package_id: int) -> Tuple[Optional[AppPackage], AppPackage]:
    new_pkg = session.get(AppPackage, new_package_id)
    if not new_pkg:
        raise HTTPException(status_code=404, detail="新版安装包不存在")
    if str(new_pkg.platform or "android").strip().lower() != "android":
        raise HTTPException(status_code=400, detail="兼容性测试 v1 仅支持 Android APK")
    if not new_pkg.package_name:
        raise HTTPException(status_code=400, detail="安装包缺少包名，无法执行兼容性测试")
    if not _resolve_package_file_path(new_pkg.file_path).exists():
        raise HTTPException(status_code=404, detail=f"APK 文件已被删除: {new_pkg.version_name or new_pkg.id}")

    if old_package_id is None:
        return None, new_pkg

    old_pkg = session.get(AppPackage, old_package_id)
    if not old_pkg:
        raise HTTPException(status_code=404, detail="旧版安装包不存在")
    if str(old_pkg.platform or "android").strip().lower() != "android":
        raise HTTPException(status_code=400, detail="兼容性测试 v1 仅支持 Android APK")
    if not old_pkg.package_name:
        raise HTTPException(status_code=400, detail="旧版安装包缺少包名，无法执行兼容性测试")
    if old_pkg.package_name != new_pkg.package_name:
        raise HTTPException(status_code=400, detail="旧版和新版 APK 必须属于同一个 package_name")
    if not _resolve_package_file_path(old_pkg.file_path).exists():
        raise HTTPException(status_code=404, detail=f"APK 文件已被删除: {old_pkg.version_name or old_pkg.id}")
    return old_pkg, new_pkg


def _validate_devices(session: Session, serials: List[str]) -> List[Device]:
    devices = session.exec(select(Device).where(col(Device.serial).in_(serials))).all()
    by_serial = {item.serial: item for item in devices}
    missing = [serial for serial in serials if serial not in by_serial]
    if missing:
        raise HTTPException(status_code=404, detail=f"设备不存在: {', '.join(missing)}")

    validated: List[Device] = []
    for serial in serials:
        device = by_serial[serial]
        platform = str(device.platform or "android").strip().lower()
        if platform != "android":
            raise HTTPException(status_code=400, detail=f"兼容性测试 v1 仅支持 Android 设备: {serial}")
        status = str(device.status or "IDLE").strip().upper()
        if (
            status != "IDLE"
            or device.lease_task_id
            or legacy_fastbot_device_locked(serial)
        ):
            raise HTTPException(status_code=400, detail=f"设备非空闲，无法启动兼容性任务: {serial} ({status})")
        validated.append(device)
    return validated


def _validate_replay_source(
    session: Session,
    *,
    inspection_run_id: int,
    branch_key: str,
) -> InspectionRun:
    source_run = session.get(InspectionRun, inspection_run_id)
    if source_run is None:
        raise HTTPException(status_code=404, detail="巡检来源任务不存在")
    if str(source_run.status or "").upper() not in {"PASS", "WARNING", "FAIL"}:
        raise HTTPException(status_code=400, detail="巡检来源任务尚未完成")
    if not source_branch_exists(session, inspection_run_id, branch_key):
        raise HTTPException(status_code=400, detail="巡检来源不包含所选业务线")
    if not str(source_run.package_name or "").strip():
        raise HTTPException(status_code=400, detail="巡检来源缺少 package_name")
    return source_run


def _replay_preflight_result(
    session: Session,
    *,
    source_run: InspectionRun,
    branch_key: str,
    installed_package: Dict[str, Any],
    max_chains: int,
) -> Dict[str, Any]:
    blockers: List[Dict[str, str]] = []
    warnings: List[Dict[str, str]] = []
    branch_config = branch_config_for_run(source_run, branch_key)
    if not branch_config:
        blockers.append(
            {
                "code": "BRANCH_SNAPSHOT_MISSING",
                "message": "巡检报告缺少所选业务线的冻结配置",
            }
        )
    else:
        blockers.extend(
            entry_case_safety_issues(
                session,
                branch_config,
                package_name=source_run.package_name,
            )
        )

    source_package = source_package_snapshot(session, source_run)
    if not bool(installed_package.get("installed")):
        blockers.append(
            {
                "code": "PACKAGE_NOT_INSTALLED",
                "message": f"设备未安装 {source_run.package_name}",
            }
        )
    elif str(installed_package.get("package_name") or "") != source_run.package_name:
        blockers.append(
            {
                "code": "PACKAGE_NAME_MISMATCH",
                "message": "设备包名与巡检来源不一致",
            }
        )
    if installed_package.get("error"):
        blockers.append(
            {
                "code": "PACKAGE_METADATA_UNAVAILABLE",
                "message": str(installed_package.get("error")),
            }
        )

    source_version = str(source_package.get("version_code") or "").strip()
    installed_version = str(installed_package.get("version_code") or "").strip()
    if not source_version:
        warnings.append(
            {
                "code": "SOURCE_VERSION_UNKNOWN",
                "message": "历史巡检未记录来源版本，人工确认后仍可回放",
            }
        )
    elif not installed_version:
        warnings.append(
            {
                "code": "TARGET_VERSION_UNKNOWN",
                "message": "无法读取设备 versionCode，人工确认后仍可回放",
            }
        )
    elif source_version == installed_version:
        warnings.append(
            {
                "code": "SAME_VERSION_REPLAY",
                "message": "设备版本与巡检来源相同，本次结果用于链路回放验收",
            }
        )

    plan: Dict[str, Any]
    try:
        plan = build_replay_plan(
            session,
            inspection_run_id=int(source_run.id),
            branch_key=branch_key,
            max_chains=max_chains,
        )
    except Exception as exc:
        plan = {
            "plan_version": 3,
            "digest": "",
            "summary": {},
            "chains": [],
            "excluded": {},
        }
        blockers.append(
            {
                "code": str(getattr(exc, "code", "REPLAY_PLAN_UNAVAILABLE")),
                "message": str(exc),
            }
        )
    chains = [dict(item) for item in plan.get("chains") or [] if isinstance(item, dict)]
    for chain in chains:
        scope = _compat_replay_scope(chain)
        chain["replay_scope"] = scope
        chain["replay_eligibility"] = {
            "FULL_PATH": "FULL",
            "PREFIX_TO_SAFETY_BOUNDARY": "SAFE_PREFIX",
        }.get(scope, "NONE")
        chain["terminal_outcome"] = _compat_terminal_outcome(chain)
        chain.setdefault("boundary_evidence", "NOT_APPLICABLE")
    if not chains and not any(item["code"] == "REPLAY_PLAN_UNAVAILABLE" for item in blockers):
        blockers.append(
            {
                "code": "NO_REPLAYABLE_CHAINS",
                "message": "所选业务线没有安全、可定位的回放链路",
            }
        )
    installed_package = dict(installed_package)
    device_digest = package_snapshot_digest(installed_package)
    installed_package["snapshot_digest"] = device_digest
    return {
        "execution_mode": "installed_replay",
        "inspection_run_id": int(source_run.id),
        "branch_key": branch_key,
        "package_name": source_run.package_name,
        "source_package": source_package,
        "installed_package": installed_package,
        "blockers": blockers,
        "warnings": warnings,
        "plan_digest": str(plan.get("digest") or plan.get("plan_digest") or ""),
        "device_snapshot_digest": device_digest,
        "plan_version": int(plan.get("plan_version") or 1),
        "summary": dict(plan.get("summary") or {}),
        "chains": chains,
        "available_prefixes": [
            str(item.get("chain_id") or item.get("path_key") or "")
            for item in chains
            if _compat_replay_scope(item) == "PREFIX_TO_SAFETY_BOUNDARY"
        ],
        "excluded": plan.get("excluded") or {},
    }


def _abort_event_for_run(run_id: int) -> threading.Event:
    with _RUN_ABORT_LOCK:
        event = _RUN_ABORT_EVENTS.get(run_id)
        if event is None:
            event = threading.Event()
            _RUN_ABORT_EVENTS[run_id] = event
        return event


def _discard_abort_event(run_id: int) -> None:
    with _RUN_ABORT_LOCK:
        _RUN_ABORT_EVENTS.pop(run_id, None)


def _is_cancelled(session: Session, run_id: int, event: threading.Event) -> bool:
    if event.is_set():
        return True
    run = session.get(CompatibilityRun, run_id)
    return bool(run and str(run.status or "").upper() == "ABORTED")


def _set_device_busy(session: Session, serial: str) -> None:
    device = session.exec(select(Device).where(Device.serial == serial)).first()
    if not device:
        return
    device.status = "BUSY"
    device.updated_at = _now()
    session.add(device)
    session.commit()


def _update_run_summary(session: Session, run_id: int, *, final: bool = False) -> None:
    run = session.get(CompatibilityRun, run_id)
    if not run:
        return

    page_rows = session.exec(
        select(CompatibilityPageResult).where(CompatibilityPageResult.run_id == run_id)
    ).all()
    pass_count = sum(1 for item in page_rows if str(item.status).upper() == "PASS")
    warning_count = sum(1 for item in page_rows if str(item.status).upper() == "WARNING")
    fail_count = sum(1 for item in page_rows if str(item.status).upper() in {"FAIL", "ERROR"})

    cells = session.exec(select(CompatibilityCell).where(CompatibilityCell.run_id == run_id)).all()
    cell_statuses = {str(item.status or "").upper() for item in cells}

    run.pass_count = pass_count
    run.warning_count = warning_count
    run.fail_count = fail_count
    run.total_pages = len(page_rows)

    if str(run.status or "").upper() == "ABORTED":
        if final:
            run.finished_at = run.finished_at or _now()
    elif fail_count > 0 or "FAIL" in cell_statuses or "ERROR" in cell_statuses:
        run.status = "FAIL" if final and cell_statuses <= TERMINAL_STATUSES else "RUNNING"
    elif warning_count > 0 or "WARNING" in cell_statuses:
        run.status = "WARNING" if final and cell_statuses <= TERMINAL_STATUSES else "RUNNING"
    elif cells and all(str(item.status or "").upper() == "PASS" for item in cells):
        run.status = "PASS" if final else "RUNNING"
    elif final:
        run.status = "ERROR"
        run.error_message = run.error_message or "兼容性任务未产生有效结果"
    else:
        run.status = "RUNNING"

    if final and not run.finished_at:
        run.finished_at = _now()
    session.add(run)
    session.commit()


def _store_text(path: Path, content: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content or "", encoding="utf-8")
    return _report_asset_path(path)


def _store_png_bytes(path: Path, data: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return _report_asset_path(path)


def _report_asset_path(path: Path) -> str:
    reports_root = project_path("reports").resolve()
    return path.resolve().relative_to(reports_root).as_posix()


def _delete_run_artifacts(run_id: int) -> bool:
    compatibility_root = project_path("reports", "compatibility").resolve()
    target = (compatibility_root / str(run_id)).resolve()
    target.relative_to(compatibility_root)
    if not target.exists():
        return False
    if not target.is_dir():
        raise RuntimeError(f"兼容性报告产物路径异常: {target}")
    shutil.rmtree(target)
    return True


async def _capture_logcat_errors(serial: str, package_name: str) -> str:
    try:
        quoted_serial = shlex.quote(str(serial))
        output = await _run_adb_command(
            f"adb -s {quoted_serial} logcat -d -t 300 {shlex.quote('*:E')}",
            timeout=20,
        )
    except Exception as exc:
        return f"logcat capture failed: {exc}"
    lines = []
    for line in output.splitlines():
        lowered = line.lower()
        if package_name in line or "fatal exception" in lowered or " anr " in lowered or "anr in" in lowered:
            lines.append(line)
    return "\n".join(lines[-80:])


async def _clear_logcat(serial: str) -> None:
    try:
        quoted_serial = shlex.quote(str(serial))
        await _run_adb_command(f"adb -s {quoted_serial} logcat -c", timeout=10)
    except Exception:
        logger.debug("logcat clear failed for %s", serial, exc_info=True)


async def _ensure_package_installed(serial: str, package_name: str) -> None:
    output = await _run_adb_command(
        f"adb -s {shlex.quote(serial)} shell pm path {shlex.quote(package_name)}",
        timeout=20,
    )
    if "package:" not in output:
        raise RuntimeError(f"设备当前未安装 {package_name}，无法使用当前版本作为基线")


async def _capture_activity(serial: str) -> str:
    try:
        quoted_serial = shlex.quote(str(serial))
        output = await _run_adb_command(
            f"adb -s {quoted_serial} shell dumpsys window | grep -E 'mCurrentFocus|mFocusedApp' | head -2",
            timeout=10,
        )
        return output.strip()
    except Exception:
        return ""


async def _capture_snapshot(
    *,
    serial: str,
    package_name: str,
    run_id: int,
    cell_id: int,
    phase: str,
    page: Dict[str, Any],
) -> Dict[str, Any]:
    import uiautomator2 as u2

    page_key = str(page.get("key") or "page")
    base_dir = project_path("reports", "compatibility", str(run_id), str(cell_id), phase, page_key)
    device = u2.connect(serial)
    screenshot = device.screenshot(format="pillow")
    buffer = io.BytesIO()
    screenshot.save(buffer, format="PNG")
    image_bytes = buffer.getvalue()
    xml_text = str(device.dump_hierarchy() or "")
    activity = await _capture_activity(serial)
    logcat_errors = await _capture_logcat_errors(serial, package_name)

    screenshot_path = _store_png_bytes(base_dir / "screenshot.png", image_bytes)
    xml_path = _store_text(base_dir / "hierarchy.xml", xml_text)
    _store_text(base_dir / "logcat_errors.txt", logcat_errors)

    return {
        "screenshot_path": screenshot_path,
        "screenshot_bytes": image_bytes,
        "xml_path": xml_path,
        "xml_text": xml_text,
        "activity": activity,
        "logcat_errors": logcat_errors,
    }


def _persist_snapshot_assets(session: Session, snapshot: Dict[str, Any]) -> Dict[str, Any]:
    if not content_addressed_assets_enabled(session):
        return snapshot

    screenshot_bytes = bytes(snapshot.get("screenshot_bytes") or b"")
    if screenshot_bytes:
        screenshot_asset = store_image_bytes(session, screenshot_bytes, commit=False)
    else:
        screenshot_path = _resolve_report_asset_path(snapshot.get("screenshot_path"))
        screenshot_asset = store_file(session, screenshot_path, commit=False)
    xml_asset = store_text(
        session,
        str(snapshot.get("xml_text") or ""),
        media_type="application/xml",
        suffix="xml",
        commit=False,
    )
    snapshot["screenshot_asset_id"] = screenshot_asset.id
    snapshot["xml_asset_id"] = xml_asset.id
    session.commit()
    return snapshot


def _resolve_report_asset_path(path: Optional[str]) -> Path:
    if not path:
        raise FileNotFoundError("report asset path is empty")
    reports_root = project_path("reports").resolve()
    candidate = (reports_root / str(path)).resolve()
    candidate.relative_to(reports_root)
    return candidate


def _load_cas_asset_bytes(
    asset_id: Optional[str],
    *,
    session: Optional[Session],
) -> Optional[bytes]:
    if not asset_id:
        return None
    if session is not None:
        if not content_addressed_assets_enabled(session):
            return None
        try:
            return read_asset(session, asset_id, transparent=True).body
        except (AssetGone, AssetNotFound):
            logger.warning("CAS asset unavailable; falling back to legacy path: %s", asset_id)
            return None

    with Session(engine) as local_session:
        if not content_addressed_assets_enabled(local_session):
            return None
        try:
            return read_asset(local_session, asset_id, transparent=True).body
        except (AssetGone, AssetNotFound):
            logger.warning("CAS asset unavailable; falling back to legacy path: %s", asset_id)
            return None


def _load_report_asset_bytes(
    path: Optional[str],
    *,
    asset_id: Optional[str] = None,
    session: Optional[Session] = None,
) -> bytes:
    cas_body = _load_cas_asset_bytes(asset_id, session=session)
    if cas_body is not None:
        return cas_body
    if not path:
        return b""
    return _resolve_report_asset_path(path).read_bytes()


def _load_report_text(
    path: Optional[str],
    *,
    asset_id: Optional[str] = None,
    session: Optional[Session] = None,
) -> str:
    try:
        return _load_report_asset_bytes(
            path,
            asset_id=asset_id,
            session=session,
        ).decode("utf-8")
    except Exception:
        return ""


def _normalize_xml(xml_text: str) -> str:
    text = re.sub(r'bounds="[^"]*"', "", xml_text or "")
    text = re.sub(r'(focused|selected|checked|index)="[^"]*"', "", text)
    return text


def _normalize_activity(raw: str) -> str:
    """从 dumpsys window 焦点行提取 `包名/Activity` 组件，剥离窗口 hash 等噪音以便跨设备比较。

    相对写法（com.pkg/.ui.Main）展开为完整组件，不同 ROM 输出风格才可等值比较。
    """
    match = re.search(r'([A-Za-z][A-Za-z0-9_.]*)/(\.?[A-Za-z0-9_.$]+)', raw or "")
    if not match:
        return ""
    package, activity = match.group(1), match.group(2)
    if activity.startswith("."):
        activity = package + activity
    return f"{package}/{activity}"


def compare_page_snapshots(
    *,
    baseline: Dict[str, Any],
    candidate: Dict[str, Any],
    page: Dict[str, Any],
    thresholds: Dict[str, Any],
    run_id: int,
    cell_id: int,
    session: Optional[Session] = None,
) -> Dict[str, Any]:
    try:
        from PIL import Image, ImageChops
        import numpy as np
    except Exception as exc:
        return {
            "status": "FAIL",
            "reason": f"图像对比依赖缺失: {exc}",
            "metrics": {},
            "diff_screenshot_path": None,
        }

    baseline_img = Image.open(
        io.BytesIO(
            _load_report_asset_bytes(
                baseline.get("screenshot_path"),
                asset_id=baseline.get("screenshot_asset_id"),
                session=session,
            )
        )
    ).convert("RGB")
    candidate_img = Image.open(
        io.BytesIO(
            _load_report_asset_bytes(
                candidate.get("screenshot_path"),
                asset_id=candidate.get("screenshot_asset_id"),
                session=session,
            )
        )
    ).convert("RGB")
    size_changed = baseline_img.size != candidate_img.size
    if size_changed:
        candidate_img = candidate_img.resize(baseline_img.size)

    diff = ImageChops.difference(baseline_img, candidate_img)
    diff_arr = np.asarray(diff)
    changed = np.any(diff_arr > 24, axis=2)
    pixel_diff_ratio = float(np.count_nonzero(changed) / max(1, changed.size))
    mean_abs_diff = float(diff_arr.mean() / 255.0)
    visual_similarity = max(0.0, min(1.0, 1.0 - mean_abs_diff))

    try:
        from skimage.metrics import structural_similarity
        import cv2

        gray_a = cv2.cvtColor(np.asarray(baseline_img), cv2.COLOR_RGB2GRAY)
        gray_b = cv2.cvtColor(np.asarray(candidate_img), cv2.COLOR_RGB2GRAY)
        ssim_score = float(structural_similarity(gray_a, gray_b))
    except Exception:
        ssim_score = visual_similarity

    baseline_xml = _normalize_xml(str(baseline.get("xml_text") or ""))
    candidate_xml = _normalize_xml(str(candidate.get("xml_text") or ""))
    xml_similarity = SequenceMatcher(None, baseline_xml, candidate_xml).ratio() if (baseline_xml or candidate_xml) else 1.0
    xml_diff_ratio = 1.0 - xml_similarity

    crash_text = "\n".join([
        str(baseline.get("logcat_errors") or ""),
        str(candidate.get("logcat_errors") or ""),
    ])
    has_crash = bool(_CRASH_PATTERN.search(crash_text))
    required_text = str(page.get("required_text") or "").strip()
    required_text_missing = bool(
        required_text
        and required_text not in candidate_xml
    )

    reasons: List[str] = []
    status = "PASS"
    if has_crash:
        status = "FAIL"
        reasons.append("检测到 Crash/ANR 日志")
    if required_text_missing:
        status = "FAIL"
        reasons.append(f"新版页面缺少必需文本: {required_text}")

    pixel_warn = float(thresholds.get("pixel_diff_ratio_warn", 0.03))
    ssim_warn = float(thresholds.get("ssim_warn", 0.96))
    xml_warn = float(thresholds.get("xml_diff_ratio_warn", 0.35))
    if status != "FAIL":
        if size_changed:
            status = "WARNING"
            reasons.append("截图尺寸发生变化")
        if pixel_diff_ratio > pixel_warn or ssim_score < ssim_warn:
            status = "WARNING"
            reasons.append("视觉差异超过阈值")
        if xml_diff_ratio > xml_warn:
            status = "WARNING"
            reasons.append("UI 层级差异超过阈值")

    metrics = {
        "pixel_diff_ratio": round(pixel_diff_ratio, 6),
        "ssim": round(ssim_score, 6),
        "visual_similarity": round(visual_similarity, 6),
        "xml_diff_ratio": round(xml_diff_ratio, 6),
        "size_changed": size_changed,
        "has_crash_or_anr": has_crash,
        "required_text_missing": required_text_missing,
    }

    diff_path = None
    if status != "PASS":
        overlay_arr = np.asarray(candidate_img).copy()
        overlay_arr[changed] = [255, 64, 64]
        diff_img = Image.fromarray(overlay_arr)
        diff_path = _store_png_bytes(
            project_path(
                "reports",
                "compatibility",
                str(run_id),
                str(cell_id),
                "diff",
                f"{page.get('key')}.png",
            ),
            _image_to_png_bytes(diff_img),
        )

    return {
        "status": status,
        "reason": "；".join(reasons) if reasons else None,
        "metrics": metrics,
        "diff_screenshot_path": diff_path,
    }


def _image_to_png_bytes(image: Any) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _render_on_demand_diff(
    baseline_bytes: bytes,
    candidate_bytes: bytes,
) -> bytes:
    from PIL import Image, ImageChops
    import numpy as np

    baseline = Image.open(io.BytesIO(baseline_bytes)).convert("RGB")
    candidate = Image.open(io.BytesIO(candidate_bytes)).convert("RGB")
    compare_candidate = candidate if candidate.size == baseline.size else candidate.resize(
        baseline.size,
        Image.Resampling.LANCZOS,
    )
    changed = np.any(
        np.asarray(ImageChops.difference(baseline, compare_candidate)) > 24,
        axis=2,
    )
    overlay = np.asarray(compare_candidate).copy()
    overlay[changed] = [255, 64, 64]
    return _image_to_png_bytes(Image.fromarray(overlay))


def _get_or_create_page_diff(
    session: Session,
    row: CompatibilityPageResult,
) -> Dict[str, Any]:
    if row.diff_screenshot_asset_id:
        existing = _active_compatibility_asset(session, row.diff_screenshot_asset_id)
        if existing is not None:
            return {
                "asset_id": existing.id,
                "url": f"/api/assets/{existing.id}",
                "cached": True,
                "expires_at": None,
            }

    now = _now()
    cached_reference = session.exec(
        select(AssetReference).where(
            AssetReference.owner_type == "compatibility_page_result",
            AssetReference.owner_id == row.id,
            AssetReference.role == "on_demand_diff",
            AssetReference.released_at == None,  # noqa: E711
            AssetReference.expires_at != None,  # noqa: E711
            AssetReference.expires_at > now,
        )
    ).first()
    if cached_reference is not None:
        existing = _active_compatibility_asset(session, cached_reference.asset_id)
        if existing is not None:
            return {
                "asset_id": existing.id,
                "url": f"/api/assets/{existing.id}",
                "cached": True,
                "expires_at": cached_reference.expires_at,
            }

    baseline = _load_report_asset_bytes(
        row.baseline_screenshot_path,
        asset_id=row.baseline_screenshot_asset_id,
        session=session,
    )
    candidate = _load_report_asset_bytes(
        row.candidate_screenshot_path,
        asset_id=row.candidate_screenshot_asset_id,
        session=session,
    )
    if not baseline or not candidate:
        raise HTTPException(status_code=410, detail="页面截图资产已不可用")
    diff = store_image_bytes(
        session,
        _render_on_demand_diff(baseline, candidate),
        commit=False,
    )
    expires_at = retention_expiry(RETENTION_HOT, now=now)
    upsert_reference(
        session,
        asset_id=diff.id,
        owner_type="compatibility_page_result",
        owner_id=int(row.id),
        role="on_demand_diff",
        retention_class=RETENTION_HOT,
        expires_at=expires_at,
        commit=False,
    )
    session.commit()
    return {
        "asset_id": diff.id,
        "url": f"/api/assets/{diff.id}",
        "cached": False,
        "expires_at": expires_at,
    }


def compare_device_pages(
    *,
    baseline: Dict[str, Any],
    candidate: Dict[str, Any],
    page: Dict[str, Any],
    thresholds: Dict[str, Any],
    run_id: int,
    cell_id: int,
    session: Optional[Session] = None,
) -> Dict[str, Any]:
    """机型对比：候选设备页面 vs 基准设备页面。

    结构语义为主：Crash/必需文本/Activity 不一致直接 FAIL；归一化 XML 结构差异触发 WARNING；
    像素/SSIM 仅在两台设备分辨率相同时参与判定，跨分辨率仅计算展示（size 差异是预期，不告警）。
    """
    try:
        from PIL import Image, ImageChops
        import numpy as np
    except Exception as exc:
        return {
            "status": "FAIL",
            "reason": f"图像对比依赖缺失: {exc}",
            "metrics": {},
            "diff_screenshot_path": None,
        }

    reasons: List[str] = []
    status = "PASS"

    if candidate.get("has_crash_or_anr"):
        status = "FAIL"
        reasons.append("检测到 Crash/ANR 日志")
    required_text = str(page.get("required_text") or "").strip()
    if candidate.get("required_text_missing"):
        status = "FAIL"
        reasons.append(f"页面缺少必需文本: {required_text}")

    baseline_activity = _normalize_activity(str(baseline.get("activity") or ""))
    candidate_activity = _normalize_activity(str(candidate.get("activity") or ""))
    activity_mismatch = bool(
        baseline_activity and candidate_activity and baseline_activity != candidate_activity
    )
    if activity_mismatch:
        status = "FAIL"
        reasons.append(f"页面 Activity 与基准不一致: {candidate_activity} != {baseline_activity}")

    baseline_img = Image.open(
        io.BytesIO(
            _load_report_asset_bytes(
                baseline.get("screenshot_path"),
                asset_id=baseline.get("screenshot_asset_id"),
                session=session,
            )
        )
    ).convert("RGB")
    candidate_img = Image.open(
        io.BytesIO(
            _load_report_asset_bytes(
                candidate.get("screenshot_path"),
                asset_id=candidate.get("screenshot_asset_id"),
                session=session,
            )
        )
    ).convert("RGB")
    same_resolution = baseline_img.size == candidate_img.size
    compare_img = candidate_img if same_resolution else candidate_img.resize(baseline_img.size)

    diff = ImageChops.difference(baseline_img, compare_img)
    diff_arr = np.asarray(diff)
    changed = np.any(diff_arr > 24, axis=2)
    pixel_diff_ratio = float(np.count_nonzero(changed) / max(1, changed.size))
    mean_abs_diff = float(diff_arr.mean() / 255.0)
    visual_similarity = max(0.0, min(1.0, 1.0 - mean_abs_diff))

    try:
        from skimage.metrics import structural_similarity
        import cv2

        gray_a = cv2.cvtColor(np.asarray(baseline_img), cv2.COLOR_RGB2GRAY)
        gray_b = cv2.cvtColor(np.asarray(compare_img), cv2.COLOR_RGB2GRAY)
        ssim_score = float(structural_similarity(gray_a, gray_b))
    except Exception:
        ssim_score = visual_similarity

    baseline_xml = _normalize_xml(str(baseline.get("xml_text") or ""))
    candidate_xml = _normalize_xml(str(candidate.get("xml_text") or ""))
    xml_similarity = SequenceMatcher(None, baseline_xml, candidate_xml).ratio() if (baseline_xml or candidate_xml) else 1.0
    xml_diff_ratio = 1.0 - xml_similarity

    pixel_warn = float(thresholds.get("pixel_diff_ratio_warn", 0.03))
    ssim_warn = float(thresholds.get("ssim_warn", 0.96))
    xml_warn = float(thresholds.get("xml_diff_ratio_warn", 0.35))
    if status != "FAIL":
        if xml_diff_ratio > xml_warn:
            status = "WARNING"
            reasons.append("UI 层级与基准设备差异超过阈值")
        if same_resolution and (pixel_diff_ratio > pixel_warn or ssim_score < ssim_warn):
            status = "WARNING"
            reasons.append("视觉差异超过阈值")

    metrics = {
        "pixel_diff_ratio": round(pixel_diff_ratio, 6),
        "ssim": round(ssim_score, 6),
        "visual_similarity": round(visual_similarity, 6),
        "xml_diff_ratio": round(xml_diff_ratio, 6),
        "same_resolution": same_resolution,
        "has_crash_or_anr": bool(candidate.get("has_crash_or_anr")),
        "required_text_missing": bool(candidate.get("required_text_missing")),
        "activity_mismatch": activity_mismatch,
        "baseline_device_serial": str(baseline.get("device_serial") or ""),
    }

    diff_path = None
    if same_resolution and status != "PASS":
        overlay_arr = np.asarray(candidate_img).copy()
        overlay_arr[changed] = [255, 64, 64]
        diff_img = Image.fromarray(overlay_arr)
        diff_path = _store_png_bytes(
            project_path(
                "reports",
                "compatibility",
                str(run_id),
                str(cell_id),
                "diff",
                f"{page.get('key')}.png",
            ),
            _image_to_png_bytes(diff_img),
        )

    return {
        "status": status,
        "reason": "；".join(reasons) if reasons else None,
        "metrics": metrics,
        "diff_screenshot_path": diff_path,
    }


def _run_inspection_path_capture(
    *,
    serial: str,
    package_name: str,
    run_id: int,
    cell_id: int,
    phase: str,
    page: Dict[str, Any],
    abort_event: threading.Event,
) -> Dict[str, Any]:
    device = connect_android(serial)
    branch_config = dict(page.get("branch_config") or {})
    secret_values: List[str] = _environment_secret_values(
        branch_config.get("env_id")
    )
    capture, unique = _replay_path(
        device=device,
        path=list(page.get("inspection_path") or []),
        branch_config=branch_config,
        device_serial=serial,
        package_name=package_name,
        abort_event=abort_event,
        input_rules=[
            dict(item)
            for item in page.get("input_rules") or []
            if isinstance(item, dict)
        ],
        dynamic_patterns=list(page.get("dynamic_text_patterns") or []),
        stable_wait_seconds=float(page.get("stable_wait_seconds") or 5.0),
        secret_values=secret_values,
    )
    try:
        if not unique or capture is None:
            raise RuntimeError("巡检稳定路径无法唯一回放")
        if capture.package_name != package_name:
            raise RuntimeError(f"巡检路径回放进入外部包: {capture.package_name or '-'}")
        page_key = str(page.get("key") or "page")
        base_dir = project_path(
            "reports",
            "compatibility",
            str(run_id),
            str(cell_id),
            phase,
            page_key,
        )
        reports_root = project_path("reports").resolve()
        screenshot_path = (base_dir / "screenshot.png").resolve(strict=False)
        xml_path = (base_dir / "hierarchy.xml").resolve(strict=False)
        screenshot_path.relative_to(reports_root)
        xml_path.relative_to(reports_root)
        artifacts = InspectionArtifactSanitizer(
            [
                dict(item)
                for item in page.get("sanitizer_rules") or []
                if isinstance(item, dict)
            ]
        ).write(
            xml=capture.xml,
            screenshot_png=capture.screenshot_png,
            xml_path=xml_path,
            screenshot_path=screenshot_path,
        )
        resolved_screenshot = screenshot_path.resolve()
        resolved_xml = xml_path.resolve()
        resolved_screenshot.relative_to(reports_root)
        resolved_xml.relative_to(reports_root)
        return {
            "screenshot_path": resolved_screenshot.relative_to(reports_root).as_posix(),
            "screenshot_bytes": artifacts.screenshot_png,
            "screenshot_asset_id": artifacts.screenshot_asset_id,
            "xml_path": resolved_xml.relative_to(reports_root).as_posix(),
            "xml_text": artifacts.xml,
            "xml_asset_id": artifacts.xml_asset_id,
            "activity": capture.activity,
            "logcat_errors": "",
        }
    finally:
        for index in range(len(secret_values)):
            secret_values[index] = ""


async def _run_page_capture(
    *,
    session: Session,
    run: CompatibilityRun,
    cell: CompatibilityCell,
    page: Dict[str, Any],
    phase: str,
    abort_event: threading.Event,
) -> Dict[str, Any]:
    if page.get("inspection_state_id"):
        await _clear_logcat(cell.device_serial)
        snapshot = await asyncio.to_thread(
            _run_inspection_path_capture,
            serial=cell.device_serial,
            package_name=run.package_name,
            run_id=run.id,
            cell_id=cell.id,
            phase=phase,
            page=page,
            abort_event=abort_event,
        )
        snapshot["logcat_errors"] = await _capture_logcat_errors(
            cell.device_serial,
            run.package_name,
        )
        return _persist_snapshot_assets(session, snapshot)

    case = session.get(TestCase, int(page.get("case_id") or 0))
    if not case:
        raise RuntimeError(f"页面用例不存在: {page.get('case_id')}")

    await _clear_logcat(cell.device_serial)
    result = await asyncio.to_thread(
        _run_case_for_capture,
        int(page.get("case_id") or 0),
        cell.device_serial,
        run.env_id,
        abort_event,
    )
    if not result.get("success"):
        raise RuntimeError("页面进入用例执行失败")

    settle = max(0, int(page.get("settle_seconds") or 0))
    if settle:
        await asyncio.sleep(settle)

    snapshot = await _capture_snapshot(
        serial=cell.device_serial,
        package_name=run.package_name,
        run_id=run.id,
        cell_id=cell.id,
        phase=phase,
        page=page,
    )
    return _persist_snapshot_assets(session, snapshot)


def _run_case_for_capture(
    case_id: int,
    serial: str,
    env_id: Optional[int],
    abort_event: threading.Event,
    before_device_step: Optional[Callable[[str], None]] = None,
    before_step: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    from sqlmodel import Session as SQLSession

    with SQLSession(engine) as local_session:
        case = local_session.get(TestCase, case_id)
        if not case:
            raise RuntimeError(f"页面用例不存在: {case_id}")
        return run_case_with_standard_runner(
            session=local_session,
            case=case,
            device_serial=serial,
            env_id=env_id,
            abort_event=abort_event,
            before_device_step=before_device_step,
            before_step=before_step,
        )


def _persist_replay_capture(
    session: Session,
    *,
    run: CompatibilityRun,
    cell: CompatibilityCell,
    chain: Dict[str, Any],
    capture,
) -> Dict[str, Any]:
    page_key = _page_key(
        1,
        {
            "key": chain.get("chain_id") or chain.get("path_key"),
            "name": chain.get("name") or "replay",
        },
    )
    base_dir = project_path(
        "reports",
        "compatibility",
        str(run.id),
        str(cell.id),
        "replay",
        page_key,
    )
    reports_root = project_path("reports").resolve()
    screenshot_path = (base_dir / "screenshot.png").resolve(strict=False)
    xml_path = (base_dir / "hierarchy.xml").resolve(strict=False)
    screenshot_path.relative_to(reports_root)
    xml_path.relative_to(reports_root)
    artifacts = InspectionArtifactSanitizer(
        [
            dict(item)
            for item in chain.get("sanitizer_rules") or []
            if isinstance(item, dict)
        ]
    ).write(
        xml=capture.xml,
        screenshot_png=capture.screenshot_png,
        xml_path=xml_path,
        screenshot_path=screenshot_path,
    )
    snapshot = {
        "screenshot_path": screenshot_path.relative_to(reports_root).as_posix(),
        "screenshot_bytes": artifacts.screenshot_png,
        "xml_path": xml_path.relative_to(reports_root).as_posix(),
        "xml_text": artifacts.xml,
        "activity": capture.activity,
    }
    return _persist_snapshot_assets(session, snapshot)


def _record_replay_result(
    session: Session,
    *,
    run: CompatibilityRun,
    cell: CompatibilityCell,
    chain: Dict[str, Any],
    result: Dict[str, Any],
    snapshot: Optional[Dict[str, Any]] = None,
) -> CompatibilityPageResult:
    chain_id = str(chain.get("chain_id") or chain.get("path_key") or "")
    row = session.exec(
        select(CompatibilityPageResult).where(
            CompatibilityPageResult.cell_id == cell.id,
            CompatibilityPageResult.page_key == chain_id,
        )
    ).first()
    if row is None:
        row = CompatibilityPageResult(
            run_id=int(run.id),
            cell_id=int(cell.id),
            page_key=chain_id,
            created_at=_now(),
        )
    display_label = str(chain.get("display_label") or "").strip()
    source_page_name = str(
        chain.get("page_name") or chain.get("name") or chain_id
    ).strip()
    row.page_name = (
        f"{display_label} · {source_page_name}"
        if display_label and source_page_name
        else display_label or source_page_name
    )
    row.path_key = str(chain.get("path_key") or chain_id)
    row.source_state_id = (
        int(chain.get("endpoint_state_id"))
        if chain.get("endpoint_state_id")
        else None
    )
    row.source_observation_id = (
        int(chain.get("source_observation_id"))
        if chain.get("source_observation_id")
        else None
    )
    row.evidence_level = str(chain.get("evidence_level") or "OBSERVED_ONCE")
    row.status = str(result.get("status") or "FAIL").upper()
    row.reason = str(result.get("reason") or "") or None
    row.failure_type = str(result.get("failure_type") or "") or None
    row.failed_step_index = (
        int(result.get("failed_step_index"))
        if result.get("failed_step_index") is not None
        else None
    )
    row.replay_trace = [
        dict(item)
        for item in result.get("trace") or []
        if isinstance(item, dict)
    ]
    trace_duration_ms = sum(
        float(item.get("duration_ms") or 0)
        for item in row.replay_trace
        if isinstance(item, dict)
    )
    source_boundary_evidence = str(
        chain.get("boundary_evidence") or "NOT_APPLICABLE"
    ).upper()
    replay_boundary_evidence = str(
        result.get("boundary_evidence") or source_boundary_evidence
    ).upper()
    boundary_results = [
        {
            "step_index": item.get("step_index"),
            "status": item.get("status"),
            "boundary_evidence": str(
                item.get("boundary_evidence") or "NOT_VERIFIABLE"
            ).upper(),
            "failure_type": item.get("failure_type"),
            "reason": item.get("reason"),
            "action_role": item.get("action_role"),
        }
        for item in row.replay_trace
        if isinstance(item, dict)
        and str(item.get("status") or "").upper().startswith("BOUNDARY_")
    ]
    row.metrics = {
        "completed_checkpoints": int(result.get("completed_checkpoints") or 0),
        "checkpoint_count": len(chain.get("checkpoints") or []),
        "duration_ms": round(
            float(result.get("duration_ms") or trace_duration_ms),
            2,
        ),
        "warning_codes": list(result.get("warning_codes") or []),
        "display_index": chain.get("display_index"),
        "display_label": display_label,
        "page_name": source_page_name,
        "source_observation_index": chain.get("source_observation_index"),
        "reachability_evidence": str(
            chain.get("reachability_evidence")
            or chain.get("evidence_level")
            or "OBSERVED_ONCE"
        ),
        "replay_eligibility": str(chain.get("replay_eligibility") or "FULL"),
        "replay_scope": _compat_replay_scope(chain),
        "terminal_outcome": _compat_terminal_outcome(chain),
        "source_boundary_evidence": source_boundary_evidence,
        "replay_boundary_evidence": replay_boundary_evidence,
        # Compatibility readers before schema v4 used this scalar.  It now
        # intentionally represents the target-version probe result.
        "boundary_evidence": replay_boundary_evidence,
        "boundary_results": boundary_results,
        "prefix_path_key": chain.get("prefix_path_key") or chain.get("path_key"),
        "terminal_boundaries": [
            dict(item)
            for item in chain.get("terminal_boundaries") or []
            if isinstance(item, dict)
        ],
    }
    if result.get("asset_error"):
        row.metrics["asset_error"] = str(result.get("asset_error"))[:500]
    row.updated_at = _now()
    if snapshot:
        row.candidate_screenshot_path = snapshot.get("screenshot_path")
        row.candidate_screenshot_asset_id = snapshot.get("screenshot_asset_id")
        row.candidate_xml_path = snapshot.get("xml_path")
        row.candidate_xml_asset_id = snapshot.get("xml_asset_id")
        row.candidate_activity = str(snapshot.get("activity") or "") or None
    session.add(row)
    session.flush()
    _sync_page_asset_references(session, row)
    session.commit()
    return row


def _budget_not_reached_result() -> Dict[str, Any]:
    return {
        "status": "WARNING",
        "reason": "回放任务已达到时间预算",
        "failure_type": "BUDGET_NOT_REACHED",
        "failed_step_index": None,
        "trace": [],
        "completed_checkpoints": 0,
        "warning_codes": ["BUDGET_NOT_REACHED"],
    }


async def _execute_cell_installed_replay_body(
    session: Session,
    run: CompatibilityRun,
    cell: CompatibilityCell,
    pages: List[Dict[str, Any]],
    abort_event: threading.Event,
    run_id: int,
) -> None:
    """Replay frozen chains on the already-installed package.

    This path intentionally has no install/prepare/baseline/diff calls.  The
    entry case is the only reset operation and is run once per chain; the
    replay kernel performs current-XML rebinding and stores a safe trace.
    """
    del run_id
    duration = max(300, min(3600, int(run.replay_duration_seconds or 3600)))
    deadline = time.monotonic() + duration
    cell.old_install_status = "SKIPPED"
    cell.new_install_status = "SKIPPED"
    cell.current_stage = "复核设备已安装版本"
    session.add(cell)
    session.commit()

    installed = await read_installed_package(
        cell.device_serial,
        run.package_name,
    )
    expected_snapshot = dict(run.target_package_snapshot or {})
    expected_digest = str(
        expected_snapshot.get("snapshot_digest")
        or package_snapshot_digest(expected_snapshot)
    )
    actual_digest = package_snapshot_digest(installed)
    cell.installed_package_snapshot = dict(installed)
    session.add(cell)
    session.commit()
    if (
        not installed.get("installed")
        or installed.get("package_name") != run.package_name
        or actual_digest != expected_digest
    ):
        reason = (
            "租约后设备安装包与预检快照不一致"
            if installed.get("installed")
            else "租约后设备未安装巡检包"
        )
        for chain in pages:
            _record_replay_result(
                session,
                run=run,
                cell=cell,
                chain=chain,
                result={
                    "status": "FAIL",
                    "reason": reason,
                    "failure_type": "PACKAGE_SNAPSHOT_CHANGED",
                    "trace": [],
                    "completed_checkpoints": 0,
                    "warning_codes": [],
                },
            )
        cell.status = "FAIL"
        cell.error_message = reason
        cell.current_stage = "设备版本校验失败"
        cell.finished_at = _now()
        session.add(cell)
        session.commit()
        return

    source_run = (
        session.get(InspectionRun, run.inspection_run_id)
        if run.inspection_run_id
        else None
    )
    profile_snapshot = dict(source_run.profile_snapshot or {}) if source_run else {}
    branch_config = dict(
        pages[0].get("branch_config")
        or (
            branch_config_for_run(
                session.get(InspectionRun, run.inspection_run_id),
                str(run.replay_branch_key or ""),
            )
            if source_run
            else {}
        )
    )
    if source_run is None or not branch_config:
        reason = "回放任务缺少业务线冻结配置"
        for chain in pages:
            _record_replay_result(
                session,
                run=run,
                cell=cell,
                chain=chain,
                result={
                    "status": "FAIL",
                    "reason": reason,
                    "failure_type": "INVALID_REPLAY_PLAN",
                    "trace": [],
                    "completed_checkpoints": 0,
                    "warning_codes": [],
                },
            )
        cell.status = "FAIL"
        cell.error_message = reason
        cell.finished_at = _now()
        session.add(cell)
        session.commit()
        return

    input_rules = [
        dict(item)
        for item in profile_snapshot.get("input_rules") or []
        if isinstance(item, dict)
    ]
    safety_rules = [
        dict(item)
        for item in profile_snapshot.get("safety_rules") or []
        if isinstance(item, dict)
    ]
    dynamic_patterns = [
        str(item)
        for item in profile_snapshot.get("dynamic_text_patterns") or []
    ]
    secret_values = _environment_secret_values(branch_config.get("env_id"))
    entry_case_id = int(branch_config.get("entry_case_id") or 0)

    def guard_device_step(_label: str) -> None:
        if abort_event.is_set():
            raise InspectionAborted("replay cancelled")
        if time.monotonic() >= deadline:
            raise InspectionAborted("replay time budget reached")

    def resolve_input(step: Dict[str, Any]) -> Optional[str]:
        action = _deserialize_action(dict(step))
        value, _, _ = _resolve_input_value(
            action=action,
            input_rules=input_rules,
            env_id=branch_config.get("env_id"),
            secret_values=secret_values,
        )
        return value

    async def mark_budget_for_remaining(start_index: int) -> None:
        for remaining in pages[start_index:]:
            _record_replay_result(
                session,
                run=run,
                cell=cell,
                chain=remaining,
                result=_budget_not_reached_result(),
            )

    def record_chain_failure(
        chain: Dict[str, Any],
        *,
        reason: str,
        failure_type: str,
        warning_codes: Optional[List[str]] = None,
    ) -> None:
        """Keep one result row for every chain even when entry/setup fails."""
        _record_replay_result(
            session,
            run=run,
            cell=cell,
            chain=chain,
            result={
                "status": "FAIL",
                "reason": reason,
                "failure_type": failure_type,
                "trace": [],
                "completed_checkpoints": 0,
                "warning_codes": list(warning_codes or []),
            },
        )

    def record_cancelled_remaining(start_index: int) -> None:
        for remaining in pages[start_index:]:
            _record_replay_result(
                session,
                run=run,
                cell=cell,
                chain=remaining,
                result={
                    "status": "CANCELLED",
                    "reason": "用户取消回放任务",
                    "failure_type": "CANCELLED",
                    "trace": [],
                    "completed_checkpoints": 0,
                    "warning_codes": [],
                },
            )

    def persist_capture_best_effort(
        chain: Dict[str, Any],
        capture: Any,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """Keep replay outcomes durable when screenshot/XML persistence fails."""
        if capture is None:
            return None, None
        try:
            return (
                _persist_replay_capture(
                    session,
                    run=run,
                    cell=cell,
                    chain=chain,
                    capture=capture,
                ),
                None,
            )
        except Exception as exc:  # pragma: no cover - real filesystem faults
            logger.exception(
                "persist installed replay capture failed: run=%s cell=%s chain=%s",
                run.id,
                cell.id,
                chain.get("chain_id") or chain.get("path_key"),
            )
            return None, f"{type(exc).__name__}: {exc}"

    def annotate_asset_failure(
        result: Dict[str, Any],
        asset_error: Optional[str],
    ) -> None:
        if not asset_error:
            return
        warning_codes = list(result.get("warning_codes") or [])
        if "ASSET_PERSIST_FAILED" not in warning_codes:
            warning_codes.append("ASSET_PERSIST_FAILED")
        result["warning_codes"] = warning_codes
        result["asset_error"] = asset_error
        # Do not mask a real replay failure. A successful chain without its
        # evidence is a warning rather than a pass.
        if str(result.get("status") or "").upper() == "PASS":
            result["status"] = "WARNING"
            result["reason"] = "链路已执行，但截图/XML 证据保存失败"
            result["failure_type"] = "ASSET_PERSIST_FAILED"

    chain_index = 0
    cancelled_chain_recorded = False
    try:
        for chain_index, chain in enumerate(pages):
            if abort_event.is_set():
                raise asyncio.CancelledError()
            if time.monotonic() >= deadline:
                await mark_budget_for_remaining(chain_index)
                break
            cell.current_stage = f"回放链路 {chain_index + 1}/{len(pages)}"
            session.add(cell)
            session.commit()

            # Attribute Crash/ANR evidence to this chain only.  Without a
            # clear, errors emitted by an earlier chain (or before the run)
            # would be observed again and incorrectly fail every later chain.
            await _clear_logcat(cell.device_serial)

            try:
                entry_result = await asyncio.to_thread(
                    _run_case_for_capture,
                    entry_case_id,
                    cell.device_serial,
                    branch_config.get("env_id"),
                    abort_event,
                    guard_device_step,
                    guard_device_step,
                )
            except InspectionAborted:
                raise
            except Exception:
                record_chain_failure(
                    chain,
                    reason="entry 用例执行异常，无法建立业务根页面",
                    failure_type="ENTRY_CASE_FAILED",
                )
                for remaining in pages[chain_index + 1 :]:
                    record_chain_failure(
                        remaining,
                        reason="entry 用例执行异常，后续链路未开始",
                        failure_type="ENTRY_CASE_FAILED",
                    )
                break
            if time.monotonic() >= deadline:
                await mark_budget_for_remaining(chain_index)
                break

            # A ready assertion alone is not proof that the entry case worked:
            # a stale page from a failed reset can still contain the same text.
            # Never replay actions after an unsuccessful entry case.
            if not isinstance(entry_result, dict) or not entry_result.get("success"):
                record_chain_failure(
                    chain,
                    reason="entry 用例未成功，无法建立业务根页面",
                    failure_type="ENTRY_CASE_FAILED",
                    warning_codes=["ENTRY_CASE_FAILED"],
                )
                for remaining in pages[chain_index + 1 :]:
                    record_chain_failure(
                        remaining,
                        reason="entry 用例未成功，后续链路未开始",
                        failure_type="ENTRY_CASE_FAILED",
                        warning_codes=["ENTRY_CASE_FAILED"],
                    )
                break

            remaining_before_ready = deadline - time.monotonic()
            if remaining_before_ready <= 0:
                await mark_budget_for_remaining(chain_index)
                break
            ready_assertion = dict(branch_config.get("ready_assertion") or {})
            try:
                device = await asyncio.to_thread(connect_android, cell.device_serial)
                try:
                    requested_ready_timeout = float(
                        ready_assertion.get("timeout") or 5
                    )
                except (TypeError, ValueError):
                    requested_ready_timeout = 5.0
                ready_assertion["timeout"] = min(
                    max(1.0, requested_ready_timeout),
                    max(1.0, remaining_before_ready),
                )
                ready = await asyncio.to_thread(
                    ready_assertion_exists,
                    device,
                    ready_assertion,
                    abort_event=abort_event,
                )
            except InspectionAborted:
                raise
            except Exception:
                record_chain_failure(
                    chain,
                    reason="设备连接或 ready assertion 执行失败",
                    failure_type="DEVICE_ERROR",
                )
                for remaining in pages[chain_index + 1 :]:
                    record_chain_failure(
                        remaining,
                        reason="设备连接或 ready assertion 失败，后续链路未开始",
                        failure_type="DEVICE_ERROR",
                    )
                break
            if not ready:
                record_chain_failure(
                    chain,
                    reason="entry 用例后未保留业务根页面",
                    failure_type="STATE_NOT_PRESERVED",
                )
                for remaining in pages[chain_index + 1 :]:
                    record_chain_failure(
                        remaining,
                        reason="业务根页面未保留，后续链路未开始",
                        failure_type="STATE_NOT_PRESERVED",
                    )
                break
            if time.monotonic() >= deadline:
                await mark_budget_for_remaining(chain_index)
                break

            def before_action(_label: str) -> None:
                guard_device_step(_label)

            result_obj = await asyncio.to_thread(
                execute_replay_chain,
                device,
                chain,
                package_name=run.package_name,
                abort_event=abort_event,
                dynamic_patterns=dynamic_patterns,
                safety_rules=safety_rules,
                input_rules=input_rules,
                input_resolver=resolve_input,
                stable_wait_seconds=float(chain.get("stable_wait_seconds") or 5.0),
                before_device_action=before_action,
            )
            result = result_obj.to_dict()
            if (
                result.get("failure_type") == "CANCELLED"
                and not abort_event.is_set()
                and time.monotonic() >= deadline
            ):
                # Preserve the partial trace and last capture for the chain
                # that reached the deadline. Untouched later chains receive
                # the empty BUDGET_NOT_REACHED result below.
                result["status"] = "WARNING"
                result["reason"] = "当前链路执行触达时间预算，已停止"
                result["failure_type"] = "BUDGET_LIMIT"
                warning_codes = list(result.get("warning_codes") or [])
                if "BUDGET_LIMIT" not in warning_codes:
                    warning_codes.append("BUDGET_LIMIT")
                result["warning_codes"] = warning_codes
            if abort_event.is_set():
                # Preserve a partial trace/capture returned by the replay
                # kernel before handing control to the cell cancellation
                # handler. Previously this branch discarded the evidence and
                # rewrote the current chain as an empty CANCELLED row.
                result["status"] = "CANCELLED"
                result["reason"] = "用户取消回放任务"
                result["failure_type"] = "CANCELLED"
                capture_snapshot, asset_error = persist_capture_best_effort(
                    chain,
                    result_obj.last_capture,
                )
                annotate_asset_failure(result, asset_error)
                _record_replay_result(
                    session,
                    run=run,
                    cell=cell,
                    chain=chain,
                    result=result,
                    snapshot=capture_snapshot,
                )
                cancelled_chain_recorded = True
                raise asyncio.CancelledError()
            capture_snapshot, asset_error = persist_capture_best_effort(
                chain,
                result_obj.last_capture,
            )
            annotate_asset_failure(result, asset_error)
            logcat = await _capture_logcat_errors(
                cell.device_serial,
                run.package_name,
            )
            if _CRASH_PATTERN.search(logcat or ""):
                result["status"] = "FAIL"
                result["reason"] = "检测到 Crash/ANR 日志"
                result["failure_type"] = "CRASH_OR_ANR"
            _record_replay_result(
                session,
                run=run,
                cell=cell,
                chain=chain,
                result=result,
                snapshot=capture_snapshot,
            )
            if time.monotonic() >= deadline:
                await mark_budget_for_remaining(chain_index + 1)
                break
    except InspectionAborted:
        if abort_event.is_set():
            record_cancelled_remaining(chain_index)
            raise asyncio.CancelledError()
        await mark_budget_for_remaining(chain_index)
    except asyncio.CancelledError:
        start_index = chain_index + 1 if cancelled_chain_recorded else chain_index
        record_cancelled_remaining(start_index)
        raise
    finally:
        for index in range(len(secret_values)):
            secret_values[index] = ""

    page_rows = session.exec(
        select(CompatibilityPageResult).where(
            CompatibilityPageResult.cell_id == cell.id
        )
    ).all()
    statuses = {str(item.status or "").upper() for item in page_rows}
    if any(item in statuses for item in {"FAIL", "ERROR"}):
        cell.status = "FAIL"
    elif "WARNING" in statuses or (run.target_package_snapshot or {}).get(
        "preflight_warnings"
    ):
        cell.status = "WARNING"
    else:
        cell.status = "PASS"
    cell.current_stage = "完成"
    cell.finished_at = _now()
    session.add(cell)
    session.commit()


async def _execute_cell(run_id: int, cell_id: int, pages: List[Dict[str, Any]], abort_event: threading.Event) -> None:
    from sqlmodel import Session as SQLSession

    lease: Optional[DeviceExecutionLease] = None
    registered_abort = False
    with SQLSession(engine) as session:
        run = session.get(CompatibilityRun, run_id)
        cell = session.get(CompatibilityCell, cell_id)
        if not run or not cell:
            return

        # Cancellation can race with BackgroundTasks starting a cell.  Keep
        # the initial transition and cancel endpoint under the same process
        # lock so a pending/aborted cell can never be resurrected as RUNNING.
        with _RUN_ABORT_LOCK:
            session.refresh(run)
            session.refresh(cell)
            if (
                abort_event.is_set()
                or str(run.status or "").upper() == "ABORTED"
                or str(cell.status or "").upper() == "ABORTED"
            ):
                return
            cell.status = "RUNNING"
            cell.current_stage = "准备设备"
            cell.started_at = _now()
            session.add(cell)
            if not run.started_at:
                run.started_at = _now()
            run.status = "RUNNING"
            session.add(run)
            session.commit()

        try:
            lease = await asyncio.to_thread(
                DeviceExecutionLease.acquire,
                user_id=int(run.user_id or 0),
                serial=cell.device_serial,
                task_id=f"compatibility:{run_id}:{cell_id}",
                kind="compatibility",
                abort_event=abort_event,
                db_engine=engine,
            )
            register_device_abort(cell.device_serial, abort_event)
            registered_abort = True
            if _is_cancelled(session, run_id, abort_event):
                raise asyncio.CancelledError()

            if str(run.execution_mode or "").upper() == "INSTALLED_REPLAY":
                await _execute_cell_installed_replay_body(
                    session,
                    run,
                    cell,
                    pages,
                    abort_event,
                    run_id,
                )
            elif run.compare_mode == "snapshot":
                await _execute_cell_snapshot_body(
                    session,
                    run,
                    cell,
                    pages,
                    abort_event,
                    run_id,
                )
            elif run.compare_mode == "device":
                await _execute_cell_device_body(session, run, cell, pages, abort_event, run_id)
            else:
                await _execute_cell_version_body(session, run, cell, pages, abort_event, run_id)
        except asyncio.CancelledError:
            cell.status = "ABORTED"
            cell.current_stage = "已取消"
            cell.finished_at = _now()
            session.add(cell)
            session.commit()
        except Exception as exc:
            logger.exception("compatibility cell failed: run=%s cell=%s", run_id, cell_id)
            cell.status = "FAIL"
            cell.current_stage = "失败"
            cell.error_message = str(exc)
            if cell.new_install_status == "RUNNING":
                cell.new_install_status = "FAIL"
            if cell.old_install_status == "RUNNING":
                cell.old_install_status = "FAIL"
            cell.finished_at = _now()
            session.add(cell)
            session.commit()
        finally:
            if registered_abort:
                unregister_device_abort(cell.device_serial)
            if lease:
                try:
                    await asyncio.to_thread(lease.release)
                except Exception:
                    logger.exception(
                        "compatibility device lease release failed: %s",
                        cell.device_serial,
                    )
            _update_run_summary(session, run_id, final=False)


async def _execute_cell_snapshot_body(
    session: Session,
    run: CompatibilityRun,
    cell: CompatibilityCell,
    pages: List[Dict[str, Any]],
    abort_event: threading.Event,
    run_id: int,
) -> None:
    """Compare a new APK against copied, sanitized inspection baselines."""
    cell.current_stage = "安装测试包"
    cell.old_install_status = "SKIPPED"
    cell.new_install_status = "RUNNING"
    session.add(cell)
    session.commit()
    await install_app_package_to_device(
        session=session,
        package_id=run.new_package_id,
        serial=cell.device_serial,
        require_idle=False,
        uninstall_first=(run.mode == "clean"),
        allow_uninstall_retry=(run.mode == "clean"),
        allow_downgrade=True,
    )
    cell.new_install_status = "PASS"
    session.add(cell)
    session.commit()

    for branch_key, branch_pages in _inspection_page_groups(pages):
        if _is_cancelled(session, run_id, abort_event):
            raise asyncio.CancelledError()
        cell.current_stage = f"准备巡检业务线: {branch_key}"
        session.add(cell)
        session.commit()
        try:
            await asyncio.to_thread(
                _prepare_inspection_page_group,
                serial=cell.device_serial,
                page=branch_pages[0],
                abort_event=abort_event,
            )
        except Exception as exc:
            if _is_cancelled(session, run_id, abort_event):
                raise asyncio.CancelledError()
            for page in branch_pages:
                baseline = {
                    "screenshot_path": page.get("baseline_screenshot_path"),
                    "screenshot_asset_id": page.get("baseline_screenshot_asset_id"),
                    "xml_path": page.get("baseline_xml_path"),
                    "xml_asset_id": page.get("baseline_xml_asset_id"),
                    "activity": page.get("baseline_activity") or "",
                }
                _record_capture_failure(
                    session,
                    run,
                    cell,
                    page,
                    "candidate",
                    RuntimeError(f"业务线准备失败: {exc}"),
                    baseline=baseline,
                )
            continue

        for page in branch_pages:
            if _is_cancelled(session, run_id, abort_event):
                raise asyncio.CancelledError()
            cell.current_stage = f"回放巡检路径: {page.get('name')}"
            session.add(cell)
            session.commit()
            baseline = {
                "screenshot_path": page.get("baseline_screenshot_path"),
                "screenshot_asset_id": page.get("baseline_screenshot_asset_id"),
                "xml_path": page.get("baseline_xml_path"),
                "xml_asset_id": page.get("baseline_xml_asset_id"),
                "xml_text": _load_report_text(
                    page.get("baseline_xml_path"),
                    asset_id=page.get("baseline_xml_asset_id"),
                    session=session,
                ),
                "activity": page.get("baseline_activity") or "",
                "logcat_errors": "",
            }
            try:
                candidate = await _run_page_capture(
                    session=session,
                    run=run,
                    cell=cell,
                    page=page,
                    phase="candidate",
                    abort_event=abort_event,
                )
                _record_compare_result(session, run, cell, page, baseline, candidate)
            except Exception as exc:
                _record_capture_failure(
                    session,
                    run,
                    cell,
                    page,
                    "candidate",
                    exc,
                    baseline=baseline,
                )

    page_rows = session.exec(
        select(CompatibilityPageResult).where(
            CompatibilityPageResult.cell_id == cell.id
        )
    ).all()
    statuses = {str(item.status or "").upper() for item in page_rows}
    if not page_rows or any(item in statuses for item in {"FAIL", "ERROR"}):
        cell.status = "FAIL"
    elif "WARNING" in statuses:
        cell.status = "WARNING"
    else:
        cell.status = "PASS"
    cell.current_stage = "完成"
    cell.finished_at = _now()
    session.add(cell)
    session.commit()


async def _execute_cell_version_body(
    session: Session,
    run: CompatibilityRun,
    cell: CompatibilityCell,
    pages: List[Dict[str, Any]],
    abort_event: threading.Event,
    run_id: int,
) -> None:
    """版本对比（纵向）：同设备安装旧版→采集基线→安装新版→采集并逐页对比。"""
    if pages and pages[0].get("inspection_state_id"):
        await _execute_cell_inspection_version_body(
            session,
            run,
            cell,
            pages,
            abort_event,
            run_id,
        )
        return

    if run.old_package_id is None:
        cell.current_stage = "检查当前版本"
        cell.old_install_status = "SKIPPED"
        session.add(cell)
        session.commit()
        try:
            await _ensure_package_installed(cell.device_serial, run.package_name)
        except Exception:
            cell.old_install_status = "FAIL"
            session.add(cell)
            session.commit()
            raise
    else:
        cell.current_stage = "安装旧版本"
        cell.old_install_status = "RUNNING"
        session.add(cell)
        session.commit()
        await install_app_package_to_device(
            session=session,
            package_id=run.old_package_id,
            serial=cell.device_serial,
            require_idle=False,
            uninstall_first=True,
            allow_uninstall_retry=True,
            allow_downgrade=True,
        )
        cell.old_install_status = "PASS"
        session.add(cell)
        session.commit()

    baseline_by_key: Dict[str, Dict[str, Any]] = {}
    for page in pages:
        if _is_cancelled(session, run_id, abort_event):
            raise asyncio.CancelledError()
        cell.current_stage = f"采集旧版: {page.get('name')}"
        session.add(cell)
        session.commit()
        try:
            baseline_by_key[str(page.get("key"))] = await _run_page_capture(
                session=session,
                run=run,
                cell=cell,
                page=page,
                phase="baseline",
                abort_event=abort_event,
            )
        except Exception as exc:
            _record_capture_failure(session, run, cell, page, "baseline", exc)

    if _is_cancelled(session, run_id, abort_event):
        raise asyncio.CancelledError()

    cell.current_stage = "安装新版本"
    cell.new_install_status = "RUNNING"
    session.add(cell)
    session.commit()
    await install_app_package_to_device(
        session=session,
        package_id=run.new_package_id,
        serial=cell.device_serial,
        require_idle=False,
        uninstall_first=(run.mode == "clean"),
        allow_uninstall_retry=False,
        allow_downgrade=False,
    )
    cell.new_install_status = "PASS"
    session.add(cell)
    session.commit()

    for page in pages:
        if _is_cancelled(session, run_id, abort_event):
            raise asyncio.CancelledError()
        cell.current_stage = f"采集新版: {page.get('name')}"
        session.add(cell)
        session.commit()
        baseline = baseline_by_key.get(str(page.get("key")))
        try:
            candidate = await _run_page_capture(
                session=session,
                run=run,
                cell=cell,
                page=page,
                phase="candidate",
                abort_event=abort_event,
            )
            if not baseline:
                raise RuntimeError("旧版基线采集失败，无法对比")
            _record_compare_result(session, run, cell, page, baseline, candidate)
        except Exception as exc:
            _record_capture_failure(session, run, cell, page, "candidate", exc, baseline=baseline)

    page_rows = session.exec(
        select(CompatibilityPageResult).where(CompatibilityPageResult.cell_id == cell.id)
    ).all()
    statuses = {str(item.status or "").upper() for item in page_rows}
    if any(item in statuses for item in {"FAIL", "ERROR"}):
        cell.status = "FAIL"
    elif "WARNING" in statuses:
        cell.status = "WARNING"
    else:
        cell.status = "PASS"
    cell.current_stage = "完成"
    cell.finished_at = _now()
    session.add(cell)
    session.commit()


async def _execute_cell_inspection_version_body(
    session: Session,
    run: CompatibilityRun,
    cell: CompatibilityCell,
    pages: List[Dict[str, Any]],
    abort_event: threading.Event,
    run_id: int,
) -> None:
    """Replay each login-state branch across an in-place APK upgrade.

    Each branch gets its own old-version installation so guest/authenticated
    states cannot contaminate one another.  The prepare case runs only before
    the old-version baseline.  After ``adb install -r`` the candidate path uses
    entry + stable locators and never invokes login/logout preparation.
    """
    if run.old_package_id is None:
        raise RuntimeError("巡检 Version 模式缺少显式旧版 APK")

    cell.old_install_status = "RUNNING"
    cell.new_install_status = "RUNNING"
    session.add(cell)
    session.commit()
    installed_new = False

    for branch_key, branch_pages in _inspection_page_groups(pages):
        if _is_cancelled(session, run_id, abort_event):
            raise asyncio.CancelledError()

        cell.current_stage = f"安装旧版并准备业务线: {branch_key}"
        session.add(cell)
        session.commit()
        await install_app_package_to_device(
            session=session,
            package_id=run.old_package_id,
            serial=cell.device_serial,
            require_idle=False,
            uninstall_first=True,
            allow_uninstall_retry=True,
            allow_downgrade=True,
        )
        cell.old_install_status = "PASS"
        session.add(cell)
        session.commit()

        try:
            await asyncio.to_thread(
                _prepare_inspection_page_group,
                serial=cell.device_serial,
                page=branch_pages[0],
                abort_event=abort_event,
            )
        except Exception as exc:
            if _is_cancelled(session, run_id, abort_event):
                raise asyncio.CancelledError()
            for page in branch_pages:
                _record_capture_failure(
                    session,
                    run,
                    cell,
                    page,
                    "baseline",
                    RuntimeError(f"旧版业务线准备失败: {exc}"),
                )
            continue

        baseline_by_key: Dict[str, Dict[str, Any]] = {}
        for page in branch_pages:
            if _is_cancelled(session, run_id, abort_event):
                raise asyncio.CancelledError()
            cell.current_stage = f"采集旧版: {page.get('name')}"
            session.add(cell)
            session.commit()
            try:
                baseline_by_key[str(page.get("key"))] = await _run_page_capture(
                    session=session,
                    run=run,
                    cell=cell,
                    page=page,
                    phase="baseline",
                    abort_event=abort_event,
                )
            except Exception as exc:
                _record_capture_failure(
                    session,
                    run,
                    cell,
                    page,
                    "baseline",
                    exc,
                )

        if _is_cancelled(session, run_id, abort_event):
            raise asyncio.CancelledError()
        cell.current_stage = f"覆盖升级新版（保持 {branch_key} 登录态）"
        session.add(cell)
        session.commit()
        await install_app_package_to_device(
            session=session,
            package_id=run.new_package_id,
            serial=cell.device_serial,
            require_idle=False,
            uninstall_first=False,
            allow_uninstall_retry=False,
            allow_downgrade=False,
        )
        installed_new = True
        cell.new_install_status = "PASS"
        session.add(cell)
        session.commit()

        # Deliberately no prepare case here: upgrade compatibility must validate
        # that the old login state survives the in-place installation.
        for page in branch_pages:
            if _is_cancelled(session, run_id, abort_event):
                raise asyncio.CancelledError()
            cell.current_stage = f"采集升级后新版: {page.get('name')}"
            session.add(cell)
            session.commit()
            baseline = baseline_by_key.get(str(page.get("key")))
            try:
                candidate = await _run_page_capture(
                    session=session,
                    run=run,
                    cell=cell,
                    page=page,
                    phase="candidate",
                    abort_event=abort_event,
                )
                if not baseline:
                    raise RuntimeError("旧版基线采集失败，无法对比")
                _record_compare_result(
                    session,
                    run,
                    cell,
                    page,
                    baseline,
                    candidate,
                )
            except Exception as exc:
                _record_capture_failure(
                    session,
                    run,
                    cell,
                    page,
                    "candidate",
                    exc,
                    baseline=baseline,
                )

    if not installed_new:
        cell.new_install_status = "SKIPPED"
    page_rows = session.exec(
        select(CompatibilityPageResult).where(
            CompatibilityPageResult.cell_id == cell.id
        )
    ).all()
    statuses = {str(item.status or "").upper() for item in page_rows}
    if not page_rows or any(item in statuses for item in {"FAIL", "ERROR"}):
        cell.status = "FAIL"
    elif "WARNING" in statuses:
        cell.status = "WARNING"
    else:
        cell.status = "PASS"
    cell.current_stage = "完成"
    cell.finished_at = _now()
    session.add(cell)
    session.commit()


async def _execute_cell_device_body(
    session: Session,
    run: CompatibilityRun,
    cell: CompatibilityCell,
    pages: List[Dict[str, Any]],
    abort_event: threading.Event,
    run_id: int,
) -> None:
    """机型对比（横向）：单次安装测试包，每页采集一次并落 PENDING 页面行；横向对比在所有 cell 完成后统一进行。"""
    cell.current_stage = "安装测试包"
    cell.old_install_status = "SKIPPED"
    cell.new_install_status = "RUNNING"
    session.add(cell)
    session.commit()
    await install_app_package_to_device(
        session=session,
        package_id=run.new_package_id,
        serial=cell.device_serial,
        require_idle=False,
        uninstall_first=(run.mode == "clean"),
        allow_uninstall_retry=(run.mode == "clean"),
        allow_downgrade=True,
    )
    cell.new_install_status = "PASS"
    session.add(cell)
    session.commit()

    if pages and pages[0].get("inspection_state_id"):
        groups = _inspection_page_groups(pages)
    else:
        groups = [("page_set", pages)]
    for branch_key, branch_pages in groups:
        if _is_cancelled(session, run_id, abort_event):
            raise asyncio.CancelledError()
        if branch_key != "page_set":
            cell.current_stage = f"准备巡检业务线: {branch_key}"
            session.add(cell)
            session.commit()
            try:
                await asyncio.to_thread(
                    _prepare_inspection_page_group,
                    serial=cell.device_serial,
                    page=branch_pages[0],
                    abort_event=abort_event,
                )
            except Exception as exc:
                if _is_cancelled(session, run_id, abort_event):
                    raise asyncio.CancelledError()
                for page in branch_pages:
                    _record_capture_failure(
                        session,
                        run,
                        cell,
                        page,
                        "candidate",
                        RuntimeError(f"业务线准备失败: {exc}"),
                    )
                continue

        for page in branch_pages:
            if _is_cancelled(session, run_id, abort_event):
                raise asyncio.CancelledError()
            cell.current_stage = f"采集页面: {page.get('name')}"
            session.add(cell)
            session.commit()
            try:
                snapshot = await _run_page_capture(
                    session=session,
                    run=run,
                    cell=cell,
                    page=page,
                    phase="candidate",
                    abort_event=abort_event,
                )
                _record_device_capture(session, run, cell, page, snapshot)
            except Exception as exc:
                _record_capture_failure(session, run, cell, page, "candidate", exc)

    # 采集阶段结束：cell 状态暂留 RUNNING，终态由 join 后的横向对比统一收敛
    cell.current_stage = "采集完成，等待横向对比"
    session.add(cell)
    session.commit()


def _store_diff_asset(session: Session, path: Optional[str]) -> Optional[str]:
    if not path or not content_addressed_assets_enabled(session):
        return None
    return store_file(session, _resolve_report_asset_path(path), commit=False).id


def _sync_page_asset_references(
    session: Session,
    row: CompatibilityPageResult,
    *,
    candidate_is_baseline: bool = False,
) -> None:
    if not content_addressed_assets_enabled(session) or row.id is None:
        return

    release_owner_references(
        session,
        owner_type="compatibility_page_result",
        owner_id=row.id,
        commit=False,
    )
    del candidate_is_baseline
    references = (
        (
            "baseline_screenshot",
            row.baseline_screenshot_asset_id,
            RETENTION_PINNED,
        ),
        ("baseline_xml", row.baseline_xml_asset_id, RETENTION_PINNED),
        (
            "candidate_screenshot",
            row.candidate_screenshot_asset_id,
            RETENTION_PINNED,
        ),
        ("candidate_xml", row.candidate_xml_asset_id, RETENTION_PINNED),
        ("diff_screenshot", row.diff_screenshot_asset_id, RETENTION_PINNED),
    )
    for role, asset_id, retention_class in references:
        if not asset_id:
            continue
        upsert_reference(
            session,
            asset_id=asset_id,
            owner_type="compatibility_page_result",
            owner_id=row.id,
            role=role,
            retention_class=retention_class,
            pinned_reason=(
                f"compatibility report evidence {row.id}"
                if retention_class == RETENTION_PINNED
                else None
            ),
            commit=False,
        )


def _record_capture_failure(
    session: Session,
    run: CompatibilityRun,
    cell: CompatibilityCell,
    page: Dict[str, Any],
    phase: str,
    exc: Exception,
    baseline: Optional[Dict[str, Any]] = None,
) -> None:
    existing = session.exec(
        select(CompatibilityPageResult)
        .where(
            CompatibilityPageResult.cell_id == cell.id,
            CompatibilityPageResult.page_key == str(page.get("key")),
        )
    ).first()
    row = existing or CompatibilityPageResult(
        run_id=run.id,
        cell_id=cell.id,
        page_key=str(page.get("key") or ""),
        page_name=str(page.get("name") or ""),
        case_id=_optional_page_case_id(page),
        required_text=page.get("required_text"),
    )
    row.status = "FAIL"
    row.reason = f"{phase} 采集失败: {exc}"
    if baseline:
        row.baseline_screenshot_path = baseline.get("screenshot_path")
        row.baseline_screenshot_asset_id = baseline.get("screenshot_asset_id")
        row.baseline_xml_path = baseline.get("xml_path")
        row.baseline_xml_asset_id = baseline.get("xml_asset_id")
        row.baseline_activity = baseline.get("activity")
    row.updated_at = _now()
    session.add(row)
    session.flush()
    _sync_page_asset_references(session, row)
    session.commit()


def _record_device_capture(
    session: Session,
    run: CompatibilityRun,
    cell: CompatibilityCell,
    page: Dict[str, Any],
    snapshot: Dict[str, Any],
) -> None:
    """机型对比模式：采集即落 PENDING 页面行，并持久化单机自检事实（Crash/必需文本），横向对比在 join 后统一进行。"""
    has_crash = bool(_CRASH_PATTERN.search(str(snapshot.get("logcat_errors") or "")))
    required_text = str(page.get("required_text") or "").strip()
    normalized_xml = _normalize_xml(str(snapshot.get("xml_text") or ""))
    required_text_missing = bool(required_text and required_text not in normalized_xml)

    existing = session.exec(
        select(CompatibilityPageResult)
        .where(
            CompatibilityPageResult.cell_id == cell.id,
            CompatibilityPageResult.page_key == str(page.get("key")),
        )
    ).first()
    row = existing or CompatibilityPageResult(
        run_id=run.id,
        cell_id=cell.id,
        page_key=str(page.get("key") or ""),
        page_name=str(page.get("name") or ""),
        case_id=_optional_page_case_id(page),
        required_text=page.get("required_text"),
    )
    row.status = "PENDING"
    row.reason = None
    row.candidate_screenshot_path = snapshot.get("screenshot_path")
    row.candidate_screenshot_asset_id = snapshot.get("screenshot_asset_id")
    row.candidate_xml_path = snapshot.get("xml_path")
    row.candidate_xml_asset_id = snapshot.get("xml_asset_id")
    row.candidate_activity = _normalize_activity(str(snapshot.get("activity") or ""))
    row.metrics = {
        "has_crash_or_anr": has_crash,
        "required_text_missing": required_text_missing,
        "resolution": cell.resolution or "",
    }
    row.updated_at = _now()
    session.add(row)
    session.flush()
    _sync_page_asset_references(
        session,
        row,
        candidate_is_baseline=bool(cell.is_baseline),
    )
    session.commit()


def _record_compare_result(
    session: Session,
    run: CompatibilityRun,
    cell: CompatibilityCell,
    page: Dict[str, Any],
    baseline: Dict[str, Any],
    candidate: Dict[str, Any],
) -> None:
    comparison = compare_page_snapshots(
        baseline=baseline,
        candidate=candidate,
        page=page,
        thresholds=run.thresholds or {},
        run_id=run.id,
        cell_id=cell.id,
        session=session,
    )
    row = session.exec(
        select(CompatibilityPageResult)
        .where(
            CompatibilityPageResult.cell_id == cell.id,
            CompatibilityPageResult.page_key == str(page.get("key")),
        )
    ).first()
    if row is None:
        row = CompatibilityPageResult(
            run_id=run.id,
            cell_id=cell.id,
            page_key=str(page.get("key") or ""),
            page_name=str(page.get("name") or ""),
            case_id=_optional_page_case_id(page),
            required_text=page.get("required_text"),
        )
    row.status = comparison.get("status") or "FAIL"
    row.reason = comparison.get("reason")
    row.baseline_screenshot_path = baseline.get("screenshot_path")
    row.baseline_screenshot_asset_id = baseline.get("screenshot_asset_id")
    row.candidate_screenshot_path = candidate.get("screenshot_path")
    row.candidate_screenshot_asset_id = candidate.get("screenshot_asset_id")
    row.diff_screenshot_path = comparison.get("diff_screenshot_path")
    row.diff_screenshot_asset_id = _store_diff_asset(
        session,
        row.diff_screenshot_path,
    )
    row.baseline_xml_path = baseline.get("xml_path")
    row.baseline_xml_asset_id = baseline.get("xml_asset_id")
    row.candidate_xml_path = candidate.get("xml_path")
    row.candidate_xml_asset_id = candidate.get("xml_asset_id")
    row.baseline_activity = baseline.get("activity")
    row.candidate_activity = candidate.get("activity")
    row.metrics = comparison.get("metrics") or {}
    row.updated_at = _now()
    session.add(row)
    session.flush()
    _sync_page_asset_references(session, row)
    session.commit()


def _execute_run_background(run_id: int, pages: List[Dict[str, Any]]) -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_execute_run_async(run_id, pages))
    finally:
        loop.close()


async def _execute_run_async(run_id: int, pages: List[Dict[str, Any]]) -> None:
    from sqlmodel import Session as SQLSession

    abort_event = _abort_event_for_run(run_id)
    try:
        with SQLSession(engine) as session:
            run = session.get(CompatibilityRun, run_id)
            compare_mode = str((run.compare_mode if run else None) or "version")
            cells = session.exec(
                select(CompatibilityCell).where(CompatibilityCell.run_id == run_id)
            ).all()

        await asyncio.gather(*[
            _execute_cell(run_id, cell.id, pages, abort_event)
            for cell in cells
            if cell.id is not None
        ])

        if compare_mode == "device":
            await asyncio.to_thread(_run_cross_device_comparison, run_id, pages, abort_event)

        with SQLSession(engine) as session:
            _update_run_summary(session, run_id, final=True)
    except Exception as exc:
        logger.exception("compatibility run failed: %s", run_id)
        with SQLSession(engine) as session:
            run = session.get(CompatibilityRun, run_id)
            if run:
                # A cancellation request is authoritative even if a worker
                # raises while unwinding a device/lease operation.  Do not
                # turn an intentional ABORTED run into ERROR in the outer
                # exception handler.
                if str(run.status or "").upper() != "ABORTED":
                    run.status = "ERROR"
                    run.error_message = str(exc)
                run.finished_at = _now()
                session.add(run)
                session.commit()
    finally:
        _discard_abort_event(run_id)


def _baseline_snapshot_from_row(
    session: Session,
    cell: CompatibilityCell,
    row: CompatibilityPageResult,
) -> Dict[str, Any]:
    return {
        "screenshot_path": row.candidate_screenshot_path,
        "screenshot_asset_id": row.candidate_screenshot_asset_id,
        "xml_path": row.candidate_xml_path,
        "xml_asset_id": row.candidate_xml_asset_id,
        "xml_text": _load_report_text(
            row.candidate_xml_path,
            asset_id=row.candidate_xml_asset_id,
            session=session,
        ),
        "activity": row.candidate_activity or "",
        "device_serial": cell.device_serial,
    }


def _finalize_standalone_page_status(row: CompatibilityPageResult, *, is_baseline: bool) -> None:
    """依据采集期持久化的单机自检事实（Crash/必需文本）收敛页面状态。"""
    metrics = dict(row.metrics or {})
    reasons: List[str] = []
    status = "PASS"
    if metrics.get("has_crash_or_anr"):
        status = "FAIL"
        reasons.append("检测到 Crash/ANR 日志")
    if metrics.get("required_text_missing"):
        status = "FAIL"
        reasons.append(f"页面缺少必需文本: {row.required_text or ''}")
    if is_baseline:
        metrics["is_baseline"] = True
    row.status = status
    row.reason = "；".join(reasons) if reasons else None
    row.metrics = metrics
    row.updated_at = _now()


def _run_cross_device_comparison(run_id: int, pages: List[Dict[str, Any]], abort_event: threading.Event) -> None:
    """机型对比：所有 cell 采集完成后，非基准设备逐页与基准设备横向对比。"""
    from sqlmodel import Session as SQLSession

    with SQLSession(engine) as session:
        run = session.get(CompatibilityRun, run_id)
        if not run:
            return
        cells = session.exec(
            select(CompatibilityCell).where(CompatibilityCell.run_id == run_id).order_by(CompatibilityCell.id)
        ).all()
        baseline_cell = next((item for item in cells if item.is_baseline), None)
        if baseline_cell is None:
            baseline_cell = next(
                (item for item in cells if item.device_serial == run.baseline_device_serial), None
            )

        rows = session.exec(
            select(CompatibilityPageResult).where(CompatibilityPageResult.run_id == run_id)
        ).all()
        rows_by_cell_page: Dict[Tuple[int, str], CompatibilityPageResult] = {
            (item.cell_id, item.page_key): item for item in rows
        }

        cancelled = _is_cancelled(session, run_id, abort_event)

        # 先收敛基准设备自身页面状态（仅单机自检，不与他机对比）
        baseline_rows: Dict[str, CompatibilityPageResult] = {}
        if baseline_cell is not None:
            for page in pages:
                page_key = str(page.get("key") or "")
                row = rows_by_cell_page.get((baseline_cell.id, page_key))
                if row is None:
                    continue
                baseline_rows[page_key] = row
                if str(row.status or "").upper() == "PENDING" and not cancelled:
                    _finalize_standalone_page_status(row, is_baseline=True)
                    session.add(row)
                    session.flush()
                    _sync_page_asset_references(
                        session,
                        row,
                        candidate_is_baseline=True,
                    )
            session.commit()

        for cell in cells:
            if baseline_cell is not None and cell.id == baseline_cell.id:
                continue
            if not cancelled and _is_cancelled(session, run_id, abort_event):
                cancelled = True
            for page in pages:
                page_key = str(page.get("key") or "")
                row = rows_by_cell_page.get((cell.id, page_key))
                if row is None or str(row.status or "").upper() != "PENDING":
                    continue
                if cancelled:
                    continue

                cell_stage_owner = session.get(CompatibilityCell, cell.id)
                if cell_stage_owner and cell_stage_owner.status == "RUNNING":
                    cell_stage_owner.current_stage = f"横向对比: {page.get('name')}"
                    session.add(cell_stage_owner)
                    session.commit()

                baseline_row = baseline_rows.get(page_key)
                if (
                    baseline_cell is None
                    or baseline_row is None
                    or not (
                        baseline_row.candidate_screenshot_asset_id
                        or baseline_row.candidate_screenshot_path
                    )
                    or not (
                        baseline_row.candidate_xml_asset_id
                        or baseline_row.candidate_xml_path
                    )
                ):
                    row.status = "ERROR"
                    row.reason = "基准设备页面采集失败，无法横向对比"
                    row.updated_at = _now()
                    session.add(row)
                    session.flush()
                    _sync_page_asset_references(session, row)
                    session.commit()
                    continue

                baseline_snapshot = _baseline_snapshot_from_row(
                    session,
                    baseline_cell,
                    baseline_row,
                )
                capture_metrics = dict(row.metrics or {})
                candidate_snapshot = {
                    "screenshot_path": row.candidate_screenshot_path,
                    "screenshot_asset_id": row.candidate_screenshot_asset_id,
                    "xml_path": row.candidate_xml_path,
                    "xml_asset_id": row.candidate_xml_asset_id,
                    "xml_text": _load_report_text(
                        row.candidate_xml_path,
                        asset_id=row.candidate_xml_asset_id,
                        session=session,
                    ),
                    "activity": row.candidate_activity or "",
                    "has_crash_or_anr": bool(capture_metrics.get("has_crash_or_anr")),
                    "required_text_missing": bool(capture_metrics.get("required_text_missing")),
                }
                try:
                    comparison = compare_device_pages(
                        baseline=baseline_snapshot,
                        candidate=candidate_snapshot,
                        page=page,
                        thresholds=run.thresholds or {},
                        run_id=run_id,
                        cell_id=cell.id,
                        session=session,
                    )
                    row.status = comparison.get("status") or "FAIL"
                    row.reason = comparison.get("reason")
                    row.diff_screenshot_path = comparison.get("diff_screenshot_path")
                    row.diff_screenshot_asset_id = _store_diff_asset(
                        session,
                        row.diff_screenshot_path,
                    )
                    merged_metrics = dict(capture_metrics)
                    merged_metrics.update(comparison.get("metrics") or {})
                    row.metrics = merged_metrics
                except Exception as exc:
                    logger.exception(
                        "cross-device compare failed: run=%s cell=%s page=%s", run_id, cell.id, page_key
                    )
                    row.status = "FAIL"
                    row.reason = f"横向对比失败: {exc}"
                row.baseline_screenshot_path = baseline_row.candidate_screenshot_path
                row.baseline_screenshot_asset_id = (
                    baseline_row.candidate_screenshot_asset_id
                )
                row.baseline_xml_path = baseline_row.candidate_xml_path
                row.baseline_xml_asset_id = baseline_row.candidate_xml_asset_id
                row.baseline_activity = baseline_row.candidate_activity
                row.updated_at = _now()
                session.add(row)
                session.flush()
                _sync_page_asset_references(session, row)
                session.commit()

        # 收敛各 cell 终态（采集阶段结束时留 RUNNING，由此统一定级）
        for cell in cells:
            current = session.get(CompatibilityCell, cell.id)
            if not current or str(current.status or "").upper() not in {"PENDING", "RUNNING"}:
                continue
            if cancelled:
                current.status = "ABORTED"
                current.current_stage = "已取消"
            else:
                page_rows = session.exec(
                    select(CompatibilityPageResult).where(CompatibilityPageResult.cell_id == current.id)
                ).all()
                statuses = {str(item.status or "").upper() for item in page_rows}
                if not page_rows or any(item in statuses for item in {"FAIL", "ERROR"}):
                    current.status = "FAIL"
                    if not page_rows:
                        current.error_message = current.error_message or "未产生页面结果"
                elif "WARNING" in statuses:
                    current.status = "WARNING"
                elif "PENDING" in statuses:
                    current.status = "FAIL"
                    current.error_message = current.error_message or "横向对比未完成"
                else:
                    current.status = "PASS"
                current.current_stage = "完成"
            current.finished_at = current.finished_at or _now()
            session.add(current)
        session.commit()


@router.get("/page-sets", response_model=List[CompatPageSetRead])
def list_page_sets(
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    rows = session.exec(select(CompatPageSet).order_by(CompatPageSet.created_at.desc())).all()
    return [_page_set_read(item) for item in rows]


@router.post("/page-sets", response_model=CompatPageSetRead)
def create_page_set(
    payload: CompatPageSetCreate,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    pages = _normalize_pages([dump_model(item) for item in payload.pages])
    row = CompatPageSet(
        name=payload.name,
        description=payload.description,
        pages=pages,
        user_id=current_user.id,
        updater_id=current_user.id,
        created_at=_now(),
        updated_at=_now(),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return _page_set_read(row)


@router.put("/page-sets/{page_set_id}", response_model=CompatPageSetRead)
def update_page_set(
    page_set_id: int,
    payload: CompatPageSetUpdate,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    row = session.get(CompatPageSet, page_set_id)
    if not row:
        raise HTTPException(status_code=404, detail="页面集合不存在")
    row.name = payload.name
    row.description = payload.description
    row.pages = _normalize_pages([dump_model(item) for item in payload.pages])
    row.updater_id = current_user.id
    row.updated_at = _now()
    session.add(row)
    session.commit()
    session.refresh(row)
    return _page_set_read(row)


@router.delete("/page-sets/{page_set_id}")
def delete_page_set(
    page_set_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    row = session.get(CompatPageSet, page_set_id)
    if not row:
        raise HTTPException(status_code=404, detail="页面集合不存在")
    referenced_runs = session.exec(
        select(CompatibilityRun).where(CompatibilityRun.page_set_id == page_set_id)
    ).all()
    snapshot_pages = _normalize_pages(row.pages or [])
    for run in referenced_runs:
        run.page_set_name = run.page_set_name or row.name
        run.page_set_snapshot = run.page_set_snapshot or snapshot_pages
        run.page_set_id = None
        session.add(run)
    session.delete(row)
    session.commit()
    return {"success": True, "detached_runs": len(referenced_runs)}


@router.get("/runs", response_model=PaginatedCompatibilityRunRead)
def list_runs(
    skip: int = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    keyword: Optional[str] = None,
    status: Optional[str] = None,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    """分页获取兼容性任务，keyword 匹配任务名/包名，status 支持 pass/warning/fail/running/all"""
    conditions = []
    if keyword:
        conditions.append(
            or_(
                col(CompatibilityRun.name).contains(keyword),
                col(CompatibilityRun.package_name).contains(keyword),
            )
        )
    normalized_status = str(status or "").strip().upper()
    if normalized_status and normalized_status != "ALL":
        if normalized_status == "RUNNING":
            conditions.append(col(CompatibilityRun.status).in_(["RUNNING", "PENDING"]))
        elif normalized_status == "FAIL":
            conditions.append(col(CompatibilityRun.status).in_(["FAIL", "ERROR"]))
        else:
            conditions.append(CompatibilityRun.status == normalized_status)

    count_query = select(func.count(col(CompatibilityRun.id)))
    query = select(CompatibilityRun)
    for condition in conditions:
        count_query = count_query.where(condition)
        query = query.where(condition)

    total = session.exec(count_query).one()
    rows = session.exec(
        query
        .order_by(col(CompatibilityRun.created_at).desc())
        .offset(skip)
        .limit(limit)
    ).all()
    page_set_ids = {
        int(item.page_set_id)
        for item in rows
        if item.page_set_id is not None
    }
    page_sets_by_id = {
        int(item.id): item
        for item in (
            session.exec(
                select(CompatPageSet).where(col(CompatPageSet.id).in_(page_set_ids))
            ).all()
            if page_set_ids
            else []
        )
        if item.id is not None
    }
    return PaginatedCompatibilityRunRead(
        total=total,
        items=[
            _run_read(
                session,
                item,
                include_detail=False,
                page_set=page_sets_by_id.get(int(item.page_set_id or 0)),
                page_set_loaded=True,
            )
            for item in rows
        ],
    )


@router.post(
    "/replay-preflight",
    response_model=CompatibilityReplayPreflightRead,
)
async def replay_preflight(
    payload: CompatibilityReplayPreflightRequest,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    del current_user
    if not is_flag_enabled(session, FLAG_COMPATIBILITY_INSTALLED_REPLAY):
        raise HTTPException(
            status_code=404,
            detail="升级后链路回放尚未启用",
        )
    _validate_devices(session, [payload.device_serial])
    source_run = _validate_replay_source(
        session,
        inspection_run_id=payload.inspection_run_id,
        branch_key=payload.branch_key,
    )
    installed = await read_installed_package(
        payload.device_serial,
        source_run.package_name,
    )
    return CompatibilityReplayPreflightRead(
        **_replay_preflight_result(
            session,
            source_run=source_run,
            branch_key=payload.branch_key,
            installed_package=installed,
            max_chains=payload.max_chains,
        )
    )


@router.post("/runs", response_model=CompatibilityRunRead)
def create_run(
    payload: CompatibilityRunCreate,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    try:
        ensure_asset_capacity_for_new_run(session)
    except AssetCapacityExceeded as exc:
        raise HTTPException(
            status_code=507,
            detail={
                "message": str(exc),
                "storage": exc.status,
            },
        ) from exc
    replay_mode = payload.execution_mode == "installed_replay"
    page_set = None
    inspection_source = None
    inspection_states: List[InspectionState] = []
    replay_preflight_data: Optional[Dict[str, Any]] = None
    old_pkg: Optional[AppPackage] = None
    new_pkg: Optional[AppPackage] = None
    if replay_mode:
        if not is_flag_enabled(session, FLAG_COMPATIBILITY_INSTALLED_REPLAY):
            raise HTTPException(status_code=404, detail="升级后链路回放尚未启用")
        devices = _validate_devices(session, payload.device_serials)
        inspection_source = _validate_replay_source(
            session,
            inspection_run_id=int(payload.inspection_run_id or 0),
            branch_key=str(payload.replay_branch_key or ""),
        )
        installed = read_installed_package_sync(
            payload.device_serials[0],
            inspection_source.package_name,
        )
        replay_preflight_data = _replay_preflight_result(
            session,
            source_run=inspection_source,
            branch_key=str(payload.replay_branch_key),
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
        if replay_preflight_data["device_snapshot_digest"] != payload.device_snapshot_digest:
            raise HTTPException(
                status_code=409,
                detail="设备安装包在预检后发生变化，请重新预检",
            )
        if replay_preflight_data["plan_digest"] != payload.plan_digest:
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
                branch_key=str(payload.replay_branch_key),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        state_ids = {
            int(page.get("endpoint_state_id"))
            for page in pages
            if page.get("endpoint_state_id")
        }
        if state_ids:
            inspection_states = session.exec(
                select(InspectionState).where(col(InspectionState.id).in_(state_ids))
            ).all()
        page_set_name = f"巡检 #{inspection_source.id} · {payload.replay_branch_key} 链路回放"
    else:
        old_pkg, new_pkg = _validate_packages(
            session,
            payload.old_package_id,
            int(payload.new_package_id or 0),
        )

    if not replay_mode and payload.source_type == "inspection":
        if not is_flag_enabled(session, FLAG_MODEL_INSPECTION):
            raise HTTPException(
                status_code=404,
                detail="模型化智能巡检尚未启用（Feature Flag: model_inspection）",
            )
        inspection_source, inspection_states, pages = _validate_inspection_source(
            session,
            inspection_run_id=int(payload.inspection_run_id or 0),
            inspection_state_ids=list(payload.inspection_state_ids),
            inspection_observation_ids=list(payload.inspection_observation_ids),
            package_name=new_pkg.package_name,
        )
        page_set_name = f"巡检 #{inspection_source.id}"
    elif not replay_mode:
        page_set = session.get(CompatPageSet, payload.page_set_id)
        if not page_set:
            raise HTTPException(status_code=404, detail="页面集合不存在")
        pages = _validate_page_set(session, page_set)
        page_set_name = page_set.name
    if not replay_mode:
        devices = _validate_devices(session, payload.device_serials)

    thresholds = dump_model(payload.thresholds) if payload.thresholds is not None else {}
    observation_ids = list(payload.inspection_observation_ids or [])
    for page in pages:
        observation_id = page.get("inspection_observation_id") or page.get(
            "source_observation_id"
        )
        if observation_id and int(observation_id) not in observation_ids:
            observation_ids.append(int(observation_id))

    run = CompatibilityRun(
        name=payload.name,
        page_set_id=page_set.id if page_set else None,
        page_set_name=page_set_name,
        page_set_snapshot=(
            pages if replay_mode else [] if inspection_source else pages
        ),
        source_type=payload.source_type,
        inspection_run_id=inspection_source.id if inspection_source else None,
        inspection_state_ids=[item.id for item in inspection_states],
        inspection_observation_ids=observation_ids,
        source_coverage_snapshot=(
            {
                "inspection_run_id": int(inspection_source.id),
                "manifest_id": inspection_source.coverage_manifest_id,
                "manifest_version": inspection_source.coverage_manifest_version,
                "manifest_hash": inspection_source.coverage_manifest_hash,
                "coverage_verdict": inspection_source.coverage_verdict,
                "selected_scope_verdict": (
                    (inspection_source.coverage_assessment or {}).get(
                        "selected_scope_verdict"
                    )
                    if isinstance(inspection_source.coverage_assessment, dict)
                    else None
                ),
                "full_app_verdict": (
                    (inspection_source.coverage_assessment or {}).get(
                        "full_app_verdict"
                    )
                    if isinstance(inspection_source.coverage_assessment, dict)
                    else None
                ),
                "assessment_origin": (
                    (inspection_source.coverage_assessment or {}).get(
                        "assessment_origin"
                    )
                    if isinstance(inspection_source.coverage_assessment, dict)
                    else None
                ),
                "frozen_at": datetime.now().isoformat(),
            }
            if inspection_source
            else {}
        ),
        old_package_id=old_pkg.id if old_pkg else None,
        new_package_id=new_pkg.id if new_pkg else None,
        package_name=(
            inspection_source.package_name
            if replay_mode and inspection_source
            else new_pkg.package_name
        ),
        execution_mode="INSTALLED_REPLAY" if replay_mode else "COMPARISON",
        replay_branch_key=payload.replay_branch_key if replay_mode else None,
        replay_plan_version=(
            int(replay_preflight_data["plan_version"])
            if replay_preflight_data
            else None
        ),
        replay_plan_digest=(
            str(replay_preflight_data["plan_digest"])
            if replay_preflight_data
            else None
        ),
        replay_duration_seconds=int(payload.duration_seconds or 3600),
        source_package_snapshot=(
            dict(replay_preflight_data["source_package"])
            if replay_preflight_data
            else {}
        ),
        target_package_snapshot=(
            {
                **dict(replay_preflight_data["installed_package"]),
                "preflight_warnings": list(
                    replay_preflight_data.get("warnings") or []
                ),
            }
            if replay_preflight_data
            else {}
        ),
        manual_install_confirmed_at=_now() if replay_mode else None,
        compare_mode=payload.compare_mode,
        baseline_device_serial=payload.baseline_device_serial,
        mode=payload.mode,
        env_id=payload.env_id,
        device_serials=payload.device_serials,
        thresholds=thresholds,
        status="PENDING",
        total_cells=len(devices),
        total_pages=0,
        user_id=current_user.id,
        executor_name=current_user.full_name or current_user.username,
        created_at=_now(),
    )
    session.add(run)
    session.commit()
    session.refresh(run)

    if inspection_source and not replay_mode:
        try:
            pages = _copy_inspection_baselines(
                session=session,
                compatibility_run_id=run.id,
                inspection_run_id=inspection_source.id,
                pages=pages,
            )
        except Exception:
            shutil.rmtree(
                project_path("reports", "compatibility", str(run.id)),
                ignore_errors=True,
            )
            release_owner_references(
                session,
                owner_type="compatibility_run",
                owner_id=run.id,
                commit=False,
            )
            session.delete(run)
            session.commit()
            raise
        run.page_set_snapshot = pages
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
                is_baseline=(
                    not replay_mode
                    and payload.compare_mode == "device"
                    and device.serial == payload.baseline_device_serial
                ),
                status="PENDING",
                preflight_at=_now() if replay_mode else None,
                installed_package_snapshot=(
                    dict(replay_preflight_data["installed_package"])
                    if replay_preflight_data
                    else {}
                ),
                old_install_status="SKIPPED" if replay_mode else None,
                new_install_status="SKIPPED" if replay_mode else None,
            )
        )
    session.commit()

    background_tasks.add_task(_execute_run_background, run.id, pages)
    return _run_read(session, run, include_detail=True)


@router.get("/runs/{run_id}", response_model=CompatibilityRunRead)
def get_run(
    run_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    row = session.get(CompatibilityRun, run_id)
    if not row:
        raise HTTPException(status_code=404, detail="兼容性任务不存在")
    return _run_read(session, row, include_detail=True)


@router.get("/runs/{run_id}/pages/{page_result_id}/diff")
def get_or_create_page_diff(
    run_id: int,
    page_result_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    del current_user
    run = session.get(CompatibilityRun, run_id)
    row = session.get(CompatibilityPageResult, page_result_id)
    if run is None or row is None or row.run_id != run_id:
        raise HTTPException(status_code=404, detail="兼容性页面结果不存在")
    if str(run.execution_mode or "").upper() == "INSTALLED_REPLAY":
        raise HTTPException(
            status_code=409,
            detail="已安装版本回放不生成视觉 diff",
        )
    return _get_or_create_page_diff(session, row)


@router.post("/runs/{run_id}/cancel")
def cancel_run(
    run_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    row = session.get(CompatibilityRun, run_id)
    if not row:
        raise HTTPException(status_code=404, detail="兼容性任务不存在")
    if str(row.status or "").upper() in TERMINAL_STATUSES:
        return {"success": True, "status": row.status}

    event = _abort_event_for_run(run_id)
    # Coordinate the DB transition with _execute_cell's startup transition;
    # otherwise a worker that has just loaded the row can commit RUNNING after
    # this endpoint commits ABORTED.
    with _RUN_ABORT_LOCK:
        event.set()
        row = session.get(CompatibilityRun, run_id)
        if row is None:
            raise HTTPException(status_code=404, detail="兼容性任务不存在")
        row.status = "ABORTED"
        row.finished_at = row.finished_at or _now()
        session.add(row)

        cells = session.exec(
            select(CompatibilityCell).where(
                CompatibilityCell.run_id == run_id,
                col(CompatibilityCell.status).in_(["PENDING", "RUNNING"]),
            )
        ).all()
        for cell in cells:
            if str(cell.status or "").upper() == "PENDING":
                cell.status = "ABORTED"
                cell.current_stage = "已取消"
                cell.finished_at = _now()
                session.add(cell)
        session.commit()
    return {"success": True, "status": "ABORTED"}


@router.delete("/runs/{run_id}")
def delete_run(
    run_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    row = session.get(CompatibilityRun, run_id)
    if not row:
        raise HTTPException(status_code=404, detail="兼容性任务不存在")
    if str(row.status or "").upper() in {"PENDING", "RUNNING"}:
        raise HTTPException(status_code=400, detail="运行中的兼容性任务无法删除")

    # ABORTED is a terminal *requested* state, but the background worker may
    # still be unwinding a device action and writing page results.  Deleting
    # here would race those writes and can leave orphaned assets/rows.  Wait
    # until every cell reaches a terminal state before allowing deletion.
    cells = session.exec(
        select(CompatibilityCell).where(CompatibilityCell.run_id == run_id)
    ).all()
    active_cells = [
        cell
        for cell in cells
        if str(cell.status or "").upper() not in TERMINAL_STATUSES
    ]
    if active_cells and str(row.status or "").upper() == "ABORTED":
        raise HTTPException(
            status_code=409,
            detail="兼容性任务后台执行尚未完全退出，稍后再删除",
        )

    try:
        artifacts_deleted = _delete_run_artifacts(run_id)
    except Exception as exc:
        logger.exception("delete compatibility run artifacts failed: %s", run_id)
        raise HTTPException(status_code=500, detail=f"删除兼容性报告文件失败: {exc}") from exc

    page_results = session.exec(
        select(CompatibilityPageResult).where(CompatibilityPageResult.run_id == run_id)
    ).all()
    cells = session.exec(
        select(CompatibilityCell).where(CompatibilityCell.run_id == run_id)
    ).all()
    for result in page_results:
        release_owner_references(
            session,
            owner_type="compatibility_page_result",
            owner_id=result.id,
            commit=False,
        )
        session.delete(result)
    for cell in cells:
        release_owner_references(
            session,
            owner_type="compatibility_cell",
            owner_id=cell.id,
            commit=False,
        )
        session.delete(cell)
    release_owner_references(
        session,
        owner_type="compatibility_run",
        owner_id=run_id,
        commit=False,
    )
    session.delete(row)
    session.commit()
    _discard_abort_event(run_id)
    return {
        "success": True,
        "deleted_pages": len(page_results),
        "deleted_cells": len(cells),
        "artifacts_deleted": artifacts_deleted,
    }
