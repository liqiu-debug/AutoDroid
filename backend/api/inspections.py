"""Android model-based smart inspection APIs."""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import queue
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse
from sqlalchemy import or_
from sqlmodel import Session, col, func, select

from backend.api import deps
from backend.database import get_session
from backend.device_execution_lease import legacy_fastbot_device_locked
from backend.feature_flags import (
    FLAG_CONTENT_ADDRESSED_ASSETS,
    FLAG_INSPECTION_COVERAGE_SCHEDULER_V2,
    FLAG_INSPECTION_BUSINESS_COVERAGE_V2,
    FLAG_INSPECTION_EXPLORATION_FAMILY_CONVERGENCE,
    FLAG_INSPECTION_IDENTITY_V2,
    FLAG_INSPECTION_SIMILARITY_CONVERGENCE,
    FLAG_INSPECTION_VISUAL_HOME_ACTIONS,
    FLAG_MODEL_INSPECTION,
    FLAG_TIERED_ASSET_RETENTION,
    is_flag_enabled,
    parse_bool_setting,
)
from backend.inspection.live import (
    inspection_live_registry,
    sanitize_action_map_payload,
)
from backend.inspection.haier_business_coverage import (
    freeze_manifest,
    haier_search_input_rule,
)
from backend.inspection.action_map import normalize_terminal_action_entries
from backend.inspection.engine import (
    execute_inspection_run,
    resolve_inspection_asset,
)
from backend.inspection.runtime import (
    abort_event_for_run,
    discard_abort_event,
    request_abort,
)
from backend.inspection.replay import (
    ReplayPlanError,
    build_replay_plan,
    derive_replay_eligibility,
    legacy_replay_eligibility,
    state_reachability_evidence,
    terminal_boundaries_for_state,
)
from backend.models import (
    AppPackage,
    AssetReference,
    CompatibilityRun,
    Device,
    Environment,
    InspectionBranchRun,
    InspectionCoverageContract,
    InspectionExplorationFamily,
    InspectionFamilyActionCoverage,
    InspectionFault,
    InspectionObservation,
    InspectionPageTemplate,
    InspectionProfile,
    InspectionRun,
    InspectionState,
    InspectionTransition,
    TestCase,
    User,
)
from backend.paths import project_path
from backend.schemas import (
    InspectionBranchRunRead,
    InspectionCoverageContractRead,
    InspectionExplorationFamilyListRead,
    InspectionExplorationFamilyRead,
    InspectionFamilyActionCoverageRead,
    InspectionFaultRead,
    InspectionProfileCreate,
    InspectionProfileRead,
    InspectionProfileUpdate,
    InspectionRunCreate,
    InspectionRunRead,
    InspectionObservationRead,
    InspectionRepresentativeUpdate,
    InspectionSelectionUpdate,
    PaginatedInspectionObservationRead,
    PaginatedInspectionReplayPathRead,
    PaginatedInspectionRunRead,
)
from backend.utils.pydantic_compat import dump_model

router = APIRouter()
ws_router = APIRouter()
TERMINAL_STATUSES = {"PASS", "WARNING", "FAIL", "ERROR", "ABORTED"}
ACTIVE_STATUSES = {"PENDING", "QUEUED", "RUNNING"}
GRAPH_SCHEMA_VERSION = 8
GRAPH_HIERARCHY_VERSION = 2
_MAX_ACTION_MAP_BYTES = 5 * 1024 * 1024
_MAX_GRAPH_ACTION_MAP_BYTES = 32 * 1024 * 1024
logger = logging.getLogger(__name__)

_APP_FAULT_TYPES = {
    "ANR",
    "APP_EXIT",
    "CRASH",
    "JAVA_CRASH",
    "NATIVE_CRASH",
    "UI_UNRESPONSIVE",
    "WHITE_SCREEN",
}
_INFRA_FAULT_TYPES = {
    "ADB_ERROR",
    "DEVICE_DISCONNECTED",
    "DEVICE_ERROR",
    "INFRA_FAULT",
    "MONITOR_ERROR",
}
_TERMINAL_EVIDENCE_STATUSES = {
    "ABORTED",
    "ACTION_ERROR",
    "ACTION_EXECUTION_FAILED",
    "ANR",
    "APP_EXIT",
    "BLOCKED",
    "BUDGET_LIMIT",
    "BUDGET_NOT_REACHED",
    "CANCELLED",
    "COORDINATE_STALE",
    "COORDINATE_UNSAFE",
    "DEVICE_DISCONNECTED",
    "DEVICE_ERROR",
    "ERROR",
    "EXTERNAL_APP",
    "EXTERNAL_NAVIGATION",
    "LOCATOR_AMBIGUOUS",
    "LOCATOR_DRIFT",
    "LOCATOR_NOT_FOUND",
    "PARENT_RECOVERY_CASCADE",
    "PARENT_RECOVERY_FAILED",
    "PATH_DIVERGED",
    "PATH_DIVERGED_CASCADE",
    "POLICY_BLOCKED",
    "QUEUE_TRUNCATED",
    "WHITE_SCREEN",
}

_PAGE_TITLE_BY_SUBTYPE = {
    "HOME": "首页",
    "CATALOG_CATEGORY": "分类页",
    "COMMUNITY_FEED": "许愿池",
    "COMMUNITY_DETAIL": "许愿池内容",
    "CART": "购物车",
    "PROFILE": "我的",
    "PRODUCT_DETAIL": "商品详情",
    "SERVICE_DETAIL": "服务详情",
    "PURCHASE_OPTIONS": "规格选择",
    "CHECKOUT": "确认订单",
    "CHECKOUT_CONFIRMATION": "结算确认",
    "CASHIER": "海尔收银台",
    "ORDER": "订单",
    "ORDER_DETAIL": "订单详情",
    "CONSUMABLE_LIST": "耗材列表",
    "PRODUCT_LIST": "商品列表",
    "SERVICE_LIST": "服务列表",
    "STORE_LIST": "附近门店",
    "STORE_DETAIL": "门店详情",
    "AUTH_GATE": "登录门槛",
    "MEMBER_BENEFITS": "会员权益",
    "FAVORITES": "商品收藏",
    "BROWSING_HISTORY": "历史浏览",
    "DIALOG": "弹窗",
    "OPAQUE": "不透明页面",
}


def _page_title(state: InspectionState, template: Optional[InspectionPageTemplate]) -> str:
    subtype = str(state.page_subtype or "UNKNOWN").upper()
    if subtype in _PAGE_TITLE_BY_SUBTYPE:
        return _PAGE_TITLE_BY_SUBTYPE[subtype]
    role = str(template.page_role if template is not None else "UNKNOWN").upper()
    return _PAGE_TITLE_BY_SUBTYPE.get(role, "页面")

_EFFECTIVE_FEATURE_FLAGS = {
    FLAG_MODEL_INSPECTION: FLAG_MODEL_INSPECTION,
    FLAG_INSPECTION_IDENTITY_V2: FLAG_INSPECTION_IDENTITY_V2,
    FLAG_INSPECTION_SIMILARITY_CONVERGENCE: (FLAG_INSPECTION_SIMILARITY_CONVERGENCE),
    FLAG_INSPECTION_EXPLORATION_FAMILY_CONVERGENCE: (FLAG_INSPECTION_EXPLORATION_FAMILY_CONVERGENCE),
    FLAG_INSPECTION_COVERAGE_SCHEDULER_V2: FLAG_INSPECTION_COVERAGE_SCHEDULER_V2,
    FLAG_INSPECTION_BUSINESS_COVERAGE_V2: FLAG_INSPECTION_BUSINESS_COVERAGE_V2,
    FLAG_INSPECTION_VISUAL_HOME_ACTIONS: FLAG_INSPECTION_VISUAL_HOME_ACTIONS,
    FLAG_CONTENT_ADDRESSED_ASSETS: FLAG_CONTENT_ADDRESSED_ASSETS,
    FLAG_TIERED_ASSET_RETENTION: FLAG_TIERED_ASSET_RETENTION,
}


def _cycle_summaries(states, transitions) -> List[Dict[str, Any]]:
    """Return deterministic SCC summaries without recursively expanding cycles."""
    node_ids = {int(item.id) for item in states if item.id is not None}
    adjacency: Dict[int, List[int]] = {item: [] for item in node_ids}
    self_loops = set()
    for edge in transitions:
        source = int(edge.from_state_id)
        target = int(edge.to_state_id) if edge.to_state_id is not None else None
        if source not in node_ids or target not in node_ids:
            continue
        adjacency[source].append(target)
        if source == target:
            self_loops.add(source)

    index = 0
    indexes: Dict[int, int] = {}
    lowlinks: Dict[int, int] = {}
    stack: List[int] = []
    on_stack = set()
    components: List[List[int]] = []

    def visit(node: int) -> None:
        nonlocal index
        indexes[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for target in adjacency.get(node, []):
            if target not in indexes:
                visit(target)
                lowlinks[node] = min(lowlinks[node], lowlinks[target])
            elif target in on_stack:
                lowlinks[node] = min(lowlinks[node], indexes[target])
        if lowlinks[node] != indexes[node]:
            return
        component = []
        while stack:
            member = stack.pop()
            on_stack.remove(member)
            component.append(member)
            if member == node:
                break
        if len(component) > 1 or any(member in self_loops for member in component):
            components.append(sorted(component))

    for node in sorted(node_ids):
        if node not in indexes:
            visit(node)
    return [
        {"id": index + 1, "state_ids": component, "size": len(component)}
        for index, component in enumerate(sorted(components, key=lambda item: item[0]))
    ]


def _delete_inspection_run_artifacts(run_id: int) -> bool:
    """Delete exactly one inspection report directory without following symlinks."""
    root = project_path("reports", "inspection").resolve()
    target = root / str(int(run_id))
    # The target name is derived from an integer, nevertheless keep the same
    # containment invariant used by asset serving.
    target.absolute().relative_to(root)
    if target.is_symlink():
        target.unlink()
        return True
    if not target.exists():
        return False
    if not target.is_dir():
        raise RuntimeError(f"巡检报告产物路径异常: {target}")
    shutil.rmtree(target)
    return True


def _ensure_enabled(session: Session) -> None:
    if not is_flag_enabled(session, FLAG_MODEL_INSPECTION):
        raise HTTPException(
            status_code=404,
            detail="模型化智能巡检尚未启用（Feature Flag: model_inspection）",
        )


def _profile_dict(row: InspectionProfile) -> Dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "package_name": row.package_name,
        "branches": row.branches or {},
        "input_rules": row.input_rules or [],
        "safety_rules": row.safety_rules or [],
        "sanitizer_rules": row.sanitizer_rules or [],
        "dynamic_text_patterns": row.dynamic_text_patterns or [],
        "budgets": row.budgets or {},
        "monitor_options": row.monitor_options or {},
        "user_id": row.user_id,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _profile_read(row: InspectionProfile) -> InspectionProfileRead:
    return InspectionProfileRead(**_profile_dict(row))


def _validate_profile_references(
    session: Session,
    payload: InspectionProfileCreate,
) -> None:
    for branch_key, branch in payload.branches.items():
        for role, case_id in (
            ("prepare_case_id", branch.prepare_case_id),
            ("entry_case_id", branch.entry_case_id),
        ):
            if session.get(TestCase, case_id) is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"{branch_key}.{role} 对应的用例不存在: {case_id}",
                )
        if branch.env_id is not None and session.get(Environment, branch.env_id) is None:
            raise HTTPException(
                status_code=400,
                detail=f"{branch_key}.env_id 对应的环境不存在: {branch.env_id}",
            )


def _current_effective_features(session: Session) -> Dict[str, bool]:
    return {name: bool(is_flag_enabled(session, flag)) for name, flag in _EFFECTIVE_FEATURE_FLAGS.items()}


def _run_effective_features(
    session: Session,
    run: InspectionRun,
) -> Dict[str, bool]:
    snapshot = run.profile_snapshot if isinstance(run.profile_snapshot, dict) else {}
    frozen = snapshot.get("effective_features")
    if isinstance(frozen, dict):
        return {name: parse_bool_setting(frozen.get(name), default=False) for name in _EFFECTIVE_FEATURE_FLAGS}
    # Historical snapshots sometimes carried rollout switches at the top
    # level. Do not substitute today's global values for an old run.
    return {name: parse_bool_setting(snapshot.get(name), default=False) for name in _EFFECTIVE_FEATURE_FLAGS}


def _read_state_action_map(state: InspectionState) -> Optional[Dict[str, Any]]:
    if state.id is None:
        return None
    relative_path = (
        Path("inspection") / str(state.run_id) / str(state.branch_key) / str(state.id) / "actions.json"
    ).as_posix()
    try:
        target = resolve_inspection_asset(relative_path, run_id=state.run_id)
        if not target.is_file() or target.stat().st_size > _MAX_ACTION_MAP_BYTES:
            return None
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _legacy_pending_action_count(state: InspectionState) -> int:
    payload = _read_state_action_map(state)
    if payload is None:
        return 0
    return sum(
        1
        for item in payload.get("actions") or []
        if isinstance(item, dict) and str(item.get("status") or "").upper() in {"PENDING", "ACTIVE", "NOT_REACHED"}
    )


def _effective_topology_type(transition: InspectionTransition) -> str:
    topology = str(transition.topology_type or "").strip().upper()
    if topology:
        return topology
    if transition.to_state_id is None:
        return "TERMINAL"
    if int(transition.from_state_id) == int(transition.to_state_id):
        return "SELF_LOOP"
    return "TREE"


def _graph_action_records(
    states: List[InspectionState],
    transitions: List[InspectionTransition],
) -> List[Dict[str, Any]]:
    """Return one final outcome per State/action while preserving map-only actions."""
    records: List[Dict[str, Any]] = []
    index_by_key: Dict[tuple[int, str], int] = {}
    for transition in transitions:
        effective = _historical_transition_v4(transition)
        status = str(transition.status or "").strip().upper()
        execution_disposition = str(
            effective.get("execution_disposition") or ""
        ).strip().upper()
        # Transition rows predate the explicit ``invoked`` action-map field.
        # Use only statuses that prove a device operation was attempted as a
        # fallback; the action map below remains authoritative when present.
        inferred_invoked = status in {
            "PASS",
            "SELF_LOOP",
            "NO_EFFECT",
            "ERROR",
            "ACTION_ERROR",
            "APP_EXIT",
            "EXTERNAL_APP",
        } or execution_disposition == "RESULT_UNKNOWN"
        record = {
            "state_id": int(transition.from_state_id),
            "action_key": str(transition.action_key or ""),
            "status": status,
            "failure_type": str(effective.get("failure_type") or "").upper(),
            "execution_disposition": execution_disposition,
            "risk_type": str(transition.risk_type or "").strip(),
            "coordinate_only": bool(transition.coordinate_only),
            "invoked": inferred_invoked,
        }
        action_key = record["action_key"]
        if not action_key:
            records.append(record)
            continue
        key = (record["state_id"], action_key)
        previous_index = index_by_key.get(key)
        if previous_index is None:
            index_by_key[key] = len(records)
            records.append(record)
            continue
        previous = records[previous_index]
        record["risk_type"] = record["risk_type"] or previous["risk_type"]
        record["coordinate_only"] = bool(
            record["coordinate_only"] or previous["coordinate_only"]
        )
        # A later sampling/reuse row must not erase an earlier real device
        # invocation for the same State/action key.
        record["invoked"] = bool(record["invoked"] or previous.get("invoked"))
        records[previous_index] = record

    remaining_bytes = _MAX_GRAPH_ACTION_MAP_BYTES
    for state in states:
        if remaining_bytes <= 0 or state.id is None:
            break
        payload = _read_state_action_map(state)
        if payload is None:
            continue
        serialized_size = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if serialized_size > remaining_bytes:
            break
        remaining_bytes -= serialized_size
        normalize_terminal_action_entries(payload, phase="report")
        for index, entry in enumerate(payload.get("actions") or []):
            if not isinstance(entry, dict):
                continue
            action_key = str(entry.get("action_key") or "")
            key = (int(state.id), action_key) if action_key else None
            if key is not None and key in index_by_key:
                record = records[index_by_key[key]]
                map_status = str(entry.get("status") or "").strip().upper()
                if map_status not in {"", "PENDING", "QUEUED", "ACTIVE", "INVOKED"}:
                    record["status"] = map_status
                    record["failure_type"] = str(
                        entry.get("failure_type") or ""
                    ).strip().upper()
                    record["execution_disposition"] = str(
                        entry.get("execution_disposition") or ""
                    ).strip().upper()
                if not record["risk_type"] and entry.get("risk_type"):
                    record["risk_type"] = str(entry.get("risk_type"))
                record["coordinate_only"] = bool(
                    record["coordinate_only"] or entry.get("coordinate_only")
                )
                if "invoked" in entry:
                    record["invoked"] = bool(
                        record.get("invoked") or entry.get("invoked")
                    )
                continue
            record = {
                "state_id": int(state.id),
                "action_key": action_key or f"__map_{index}",
                "status": str(entry.get("status") or "").strip().upper(),
                "failure_type": str(entry.get("failure_type") or "").strip().upper(),
                "execution_disposition": str(entry.get("execution_disposition") or "").strip().upper(),
                "risk_type": str(entry.get("risk_type") or "").strip(),
                "coordinate_only": bool(entry.get("coordinate_only")),
                "invoked": bool(entry.get("invoked")),
            }
            if key is not None:
                index_by_key[key] = len(records)
            records.append(record)
    return records


def _state_frontier_values(
    state: InspectionState,
    *,
    run_status: str,
    include_legacy_actions: bool = False,
) -> Dict[str, Any]:
    persisted = str(state.expansion_status or "DISCOVERED").strip().upper()
    if persisted in {"ABORTED", "BUDGET_SKIPPED", "SCOPE_SKIPPED"}:
        expansion_status = persisted
    elif state.expansion_completed_at is not None or state.expanded_at is not None:
        expansion_status = "EXPANDED"
    elif persisted not in {"", "DISCOVERED"}:
        expansion_status = persisted
    elif state.queued_at is not None:
        expansion_status = "QUEUED" if str(run_status or "").upper() in ACTIVE_STATUSES else "DEFERRED"
    else:
        expansion_status = "DISCOVERED"

    pending = max(0, int(state.pending_action_count or 0))
    if include_legacy_actions and pending == 0:
        pending = _legacy_pending_action_count(state)
    return {
        "expansion_status": expansion_status,
        "pending_action_count": pending,
    }


def _frontier_summary(
    states: List[InspectionState],
    *,
    run_status: str,
    include_legacy_actions: bool = False,
) -> Dict[str, int]:
    values = [
        _state_frontier_values(
            state,
            run_status=run_status,
            include_legacy_actions=include_legacy_actions,
        )
        for state in states
    ]
    return {
        "queued": sum(1 for item in values if item["expansion_status"] == "QUEUED"),
        "deferred": sum(1 for item in values if item["expansion_status"] == "DEFERRED"),
        "pending": sum(int(item["pending_action_count"]) for item in values),
    }


def _branch_scope_config(run: InspectionRun, branch_key: str) -> str:
    snapshot = run.profile_snapshot if isinstance(run.profile_snapshot, dict) else {}
    branches = snapshot.get("branches")
    config = branches.get(branch_key) if isinstance(branches, dict) else None
    if not isinstance(config, dict):
        return "full"
    return str(config.get("scope") or "full").strip().lower()


_SCOPE_COVERED_DISPOSITIONS = {
    "EXECUTED",
    "RESULT_UNKNOWN",
    "FAMILY_REUSED",
    "CONTRACT_REUSED",
    "NAVIGATION_REUSED",
}


def _scope_coverage_payload(
    session: Session,
    run: InspectionRun,
    branches: List[InspectionBranchRun],
    states: List[InspectionState],
    action_records: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Per-branch coverage ledger for single-page scoped branches.

    The credibility contract of single-page inspection: an explicit
    denominator (every enumerated action on the entry surface), the executed
    share, every skip reason, plus the surfaces that were reached but are not
    configured as an entry of any branch — the work list for the next batch
    of entry cases.
    """
    single_page_branches = [
        item
        for item in branches
        if _branch_scope_config(run, item.branch_key) == "single_page"
    ]
    if not single_page_branches:
        return None
    records_by_state: Dict[int, List[Dict[str, Any]]] = {}
    for record in action_records:
        records_by_state.setdefault(int(record["state_id"]), []).append(record)
    # Entry surfaces of every branch in the run (not only the filtered ones):
    # a surface reached out-of-scope is "configured" when any branch enters it.
    run_branches = session.exec(
        select(InspectionBranchRun).where(InspectionBranchRun.run_id == run.id)
    ).all()
    root_state_ids = {
        int(item.root_state_id)
        for item in run_branches
        if item.root_state_id is not None
    }
    configured_surfaces: Dict[str, str] = {}
    if root_state_ids:
        for root_state in session.exec(
            select(InspectionState).where(col(InspectionState.id).in_(root_state_ids))
        ).all():
            for key in (root_state.surface_key, root_state.semantic_key):
                if key:
                    configured_surfaces.setdefault(str(key), root_state.branch_key)
    branch_payloads: List[Dict[str, Any]] = []
    for branch in single_page_branches:
        branch_states = [
            item for item in states if item.branch_key == branch.branch_key
        ]
        in_scope_states = []
        out_of_scope_states = []
        for state in branch_states:
            if str(state.expansion_status or "").upper() == "SCOPE_SKIPPED":
                out_of_scope_states.append(state)
            else:
                in_scope_states.append(state)
        total = 0
        executed = 0
        skipped_by_status: Dict[str, int] = {}
        safety_blocked = 0
        for state in in_scope_states:
            if state.id is None:
                continue
            for record in records_by_state.get(int(state.id), []):
                status = str(record.get("status") or "").upper()
                if status == "OUT_OF_SCOPE":
                    continue
                total += 1
                disposition = str(
                    record.get("execution_disposition") or ""
                ).upper()
                if disposition in _SCOPE_COVERED_DISPOSITIONS:
                    executed += 1
                    continue
                skipped_by_status[status or "UNKNOWN"] = (
                    skipped_by_status.get(status or "UNKNOWN", 0) + 1
                )
                if status == "BLOCKED" or str(record.get("risk_type") or "").strip():
                    safety_blocked += 1
        unconfigured: Dict[str, Dict[str, Any]] = {}
        for state in out_of_scope_states:
            surface = str(state.surface_key or state.semantic_key or "")
            group_key = surface or f"state:{state.id}"
            entry = unconfigured.setdefault(
                group_key,
                {
                    "surface_key": surface or None,
                    "page_subtype": state.page_subtype,
                    "title": _PAGE_TITLE_BY_SUBTYPE.get(
                        str(state.page_subtype or "").upper()
                    )
                    or state.activity
                    or surface
                    or f"State {state.id}",
                    "activity": state.activity,
                    "state_ids": [],
                    "configured_branch_key": None,
                },
            )
            if state.id is not None:
                entry["state_ids"].append(int(state.id))
            for key in (state.surface_key, state.semantic_key):
                configured = configured_surfaces.get(str(key or ""))
                if configured and configured != branch.branch_key:
                    entry["configured_branch_key"] = configured
                    break
        branch_payloads.append(
            {
                "branch_key": branch.branch_key,
                "branch_name": branch.branch_name,
                "scope": "single_page",
                "in_scope_states": len(in_scope_states),
                "total_actions": total,
                "executed_actions": executed,
                "skipped_actions": max(0, total - executed),
                "skipped_by_status": skipped_by_status,
                "safety_blocked_actions": safety_blocked,
                "coverage_ratio": round(executed / total, 4) if total else 0.0,
                "unconfigured_surfaces": sorted(
                    unconfigured.values(),
                    key=lambda item: (-len(item["state_ids"]), str(item["title"])),
                ),
            }
        )
    return {"branches": branch_payloads}


def _historical_transition_v4(
    transition: InspectionTransition,
) -> Dict[str, Any]:
    status = str(transition.status or "").upper()
    reason = str(transition.reason or "")
    failure_type = str(transition.failure_type or "").strip().upper() or None
    disposition = str(transition.execution_disposition or "").strip().upper() or "EXECUTED"
    if failure_type is None:
        if status == "LOCATOR_DRIFT":
            failure_type = "COORDINATE_STALE" if "页面像素已变化" in reason else "LOCATOR_NOT_FOUND"
        elif status == "PATH_DIVERGED":
            failure_type = "PATH_DIVERGED"
        elif status == "UNSTABLE_PARENT":
            failure_type = "PARENT_RECOVERY_CASCADE" if "本动作组不再重复" in reason else "PARENT_RECOVERY_FAILED"
        elif status in {"ERROR", "ACTION_ERROR"}:
            failure_type = "ACTION_ERROR"
        elif status in {"APP_EXIT", "EXTERNAL_APP"}:
            failure_type = status
        elif status in {
            "COORDINATE_UNSAFE",
            "COORDINATE_STALE",
            "LOCATOR_NOT_FOUND",
            "LOCATOR_AMBIGUOUS",
            "PARENT_RECOVERY_FAILED",
            "PARENT_RECOVERY_CASCADE",
            "CANCELLED",
            "BUDGET_LIMIT",
        }:
            failure_type = status

    if disposition == "EXECUTED":
        if status == "LOCATOR_DRIFT":
            disposition = "SKIPPED" if failure_type == "COORDINATE_STALE" else "FAILED"
        elif status in {
            "UNSTABLE_PARENT",
            "PATH_DIVERGED",
            "NOT_REACHED",
            "PARENT_RECOVERY_FAILED",
            "PARENT_RECOVERY_CASCADE",
            "CANCELLED",
            "BUDGET_NOT_REACHED",
            "QUEUE_TRUNCATED",
        }:
            disposition = "NOT_REACHED"
        elif status == "COVERED_BY_FAMILY":
            disposition = "FAMILY_REUSED"
        elif status in {
            "BLOCKED",
            "COORDINATE_ONLY",
            "COORDINATE_UNSAFE",
            "COORDINATE_STALE",
            "AMBIGUOUS",
            "SKIPPED",
            "VARIANT_LIMIT",
            "BUDGET_LIMIT",
            "FILTERED_NON_ACTIONABLE",
            "COVERAGE_EXHAUSTED",
            "CYCLE_CONVERGED",
        }:
            disposition = "SKIPPED"
        elif status in {
            "ERROR",
            "ACTION_ERROR",
            "APP_EXIT",
            "EXTERNAL_APP",
            "LOCATOR_NOT_FOUND",
            "LOCATOR_AMBIGUOUS",
        }:
            disposition = "FAILED"
    return {
        "action_role_key": transition.action_role_key,
        "action_role": transition.action_role,
        "execution_disposition": disposition,
        "failure_type": failure_type,
        "coverage_source_transition_id": transition.coverage_source_transition_id,
        "recovery_attempt_count": max(
            0,
            int(transition.recovery_attempt_count or 0),
        ),
    }


def _branch_reads(
    session: Session,
    run_id: int,
    *,
    run_status: str,
    states: Optional[List[InspectionState]] = None,
    include_legacy_actions: bool = False,
) -> List[InspectionBranchRunRead]:
    rows = session.exec(
        select(InspectionBranchRun).where(InspectionBranchRun.run_id == run_id).order_by(InspectionBranchRun.id)
    ).all()
    run_states = states
    if run_states is None:
        run_states = session.exec(select(InspectionState).where(InspectionState.run_id == run_id)).all()
    states_by_branch: Dict[int, List[InspectionState]] = {}
    for state in run_states:
        states_by_branch.setdefault(int(state.branch_run_id), []).append(state)
    return [
        InspectionBranchRunRead.model_validate(item).model_copy(
            update={
                "phase": item.current_stage,
                "frontier": _frontier_summary(
                    states_by_branch.get(int(item.id), []),
                    run_status=run_status,
                    include_legacy_actions=include_legacy_actions,
                ),
            }
        )
        for item in rows
    ]


def _fault_reads(session: Session, run_id: int) -> List[InspectionFaultRead]:
    rows = session.exec(
        select(InspectionFault).where(InspectionFault.run_id == run_id).order_by(InspectionFault.id)
    ).all()
    fault_ids = {item.id for item in rows if item.id is not None}
    references = (
        session.exec(
            select(AssetReference).where(
                AssetReference.owner_type == "inspection_fault",
                col(AssetReference.owner_id).in_(fault_ids),
                AssetReference.released_at == None,  # noqa: E711
            )
        ).all()
        if fault_ids
        else []
    )
    assets_by_fault: Dict[int, Dict[str, str]] = {}
    supported_roles = {"full_log", "screenshot", "xml", "replay", "trace"}
    for reference in references:
        if reference.role not in supported_roles:
            continue
        assets_by_fault.setdefault(reference.owner_id, {})[f"{reference.role}_asset_id"] = reference.asset_id
    return [
        InspectionFaultRead.model_validate(item).model_copy(update=assets_by_fault.get(int(item.id), {}))
        for item in rows
    ]


def _replay_source_eligibility(
    status: str,
    *,
    states: Optional[List[InspectionState]] = None,
    branches: Optional[List[InspectionBranchRun]] = None,
) -> tuple[bool, Optional[str]]:
    normalized = str(status or "").strip().upper()
    if normalized in {"PASS", "WARNING", "FAIL", "COMPLETED", "FINISHED"}:
        identity_v2_states = (
            [item for item in states if int(item.identity_version or 1) >= 2]
            if states is not None
            else None
        )
        if identity_v2_states is not None and not identity_v2_states:
            return False, "旧版巡检报告缺少可回放的页面身份信息"
        if branches is not None:
            completed_branch_ids = {
                int(item.id)
                for item in branches
                if item.id is not None
                and str(item.status or "").strip().upper()
                in {"PASS", "WARNING", "FAIL", "COMPLETED", "FINISHED"}
            }
            if not completed_branch_ids:
                return False, "巡检业务线尚未形成可冻结的回放计划"
            if identity_v2_states is not None and not any(
                int(item.branch_run_id) in completed_branch_ids
                for item in identity_v2_states
            ):
                return False, "已完成业务线缺少可生成回放计划的页面"
        return True, None
    reasons = {
        "ABORTED": "任务已取消，已有采集证据仅供查看",
        "ERROR": "任务执行异常，不能作为兼容回放来源",
        "PENDING": "任务尚未开始",
        "QUEUED": "任务仍在等待执行",
        "RUNNING": "任务仍在运行",
    }
    return False, reasons.get(normalized, "任务尚未形成可冻结的回放报告")


def _last_active_state_id(states: List[InspectionState]) -> Optional[int]:
    candidates = [item for item in states if item.id is not None]
    if not candidates:
        return None

    def order_key(item: InspectionState):
        timestamp = (
            item.last_observed_at
            or item.expansion_completed_at
            or item.expanded_at
            or item.updated_at
            or item.created_at
        )
        return timestamp, int(item.id or 0)

    return int(max(candidates, key=order_key).id)


def _public_replay_scope(value: Any) -> str:
    normalized = str(value or "NONE").strip().upper()
    return {
        "FULL": "FULL_PATH",
        "FULL_PATH": "FULL_PATH",
        "SAFE_PREFIX": "PREFIX_TO_SAFETY_BOUNDARY",
        "PREFIX_TO_SAFETY_BOUNDARY": "PREFIX_TO_SAFETY_BOUNDARY",
        "DIAGNOSTIC_ONLY": "DIAGNOSTIC_ONLY",
    }.get(normalized, "NONE")


def _primary_terminal_outcome(boundaries: List[Dict[str, Any]]) -> str:
    outcomes = {
        str(item.get("terminal_outcome") or "NONE").strip().upper()
        for item in boundaries
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


def _fault_summary_counts(faults: List[InspectionFault]) -> Dict[str, int]:
    result = {
        "app_faults": 0,
        "infra_faults": 0,
        "automation_failures": 0,
    }
    for fault in faults:
        fault_type = str(fault.fault_type or "").strip().upper()
        count = max(1, int(fault.occurrence_count or 1))
        if fault_type in _INFRA_FAULT_TYPES or fault_type.startswith("DEVICE_"):
            result["infra_faults"] += count
        elif fault_type in _APP_FAULT_TYPES or "CRASH" in fault_type:
            result["app_faults"] += count
        else:
            result["automation_failures"] += count
    return result


def _state_replay_semantics(
    states: List[InspectionState],
    *,
    transitions: Optional[List[InspectionTransition]] = None,
    faults: Optional[List[InspectionFault]] = None,
    replay_source_eligible: bool = True,
) -> Dict[int, Dict[str, Any]]:
    """Derive node and summary semantics from the replay kernel once.

    The graph and compact run summary deliberately consume this same result.
    This prevents risk-only heuristics from drifting away from Replay Plan v3.
    """
    transitions_by_source: Dict[int, List[InspectionTransition]] = {}
    for transition in transitions or []:
        transitions_by_source.setdefault(
            int(transition.from_state_id), []
        ).append(transition)
    faults_by_state: Dict[int, List[InspectionFault]] = {}
    for fault in faults or []:
        if fault.state_id is not None:
            faults_by_state.setdefault(int(fault.state_id), []).append(fault)

    result: Dict[int, Dict[str, Any]] = {}
    for index, state in enumerate(states):
        state_id = int(state.id) if state.id is not None else -(index + 1)
        terminal_boundaries = terminal_boundaries_for_state(
            state_id,
            transitions=transitions_by_source.get(state_id, []),
            faults=faults_by_state.get(state_id, []),
        )
        raw_scope, terminal_boundaries = derive_replay_eligibility(
            state,
            terminal_boundaries,
        )
        replay_scope = (
            _public_replay_scope(raw_scope)
            if replay_source_eligible and int(state.identity_version or 1) >= 2
            else "NONE"
        )
        result[state_id] = {
            "reachability_evidence": state_reachability_evidence(state),
            "replay_scope": replay_scope,
            "replay_eligibility": legacy_replay_eligibility(replay_scope),
            "terminal_outcome": _primary_terminal_outcome(terminal_boundaries),
            "terminal_boundaries": terminal_boundaries,
        }
    return result


def _replay_path_summary(
    semantics: Dict[int, Dict[str, Any]],
    *,
    summary_available: bool,
) -> Dict[str, Any]:
    if not summary_available:
        return {
            "summary_available": False,
            "unavailable_reason": "IDENTITY_V2_REQUIRED",
        }

    full_path = 0
    safety_prefix = 0
    diagnostic_only = 0
    verified = 0
    observed = 0
    for item in semantics.values():
        scope = str(item.get("replay_scope") or "NONE").upper()
        replayable = scope in {"FULL_PATH", "PREFIX_TO_SAFETY_BOUNDARY"}
        if scope == "FULL_PATH":
            full_path += 1
        elif scope == "PREFIX_TO_SAFETY_BOUNDARY":
            safety_prefix += 1
        elif scope == "DIAGNOSTIC_ONLY":
            diagnostic_only += 1
        if replayable:
            evidence = str(item.get("reachability_evidence") or "UNKNOWN").upper()
            if evidence == "VERIFIED_TWICE":
                verified += 1
            elif evidence == "OBSERVED_ONCE":
                observed += 1
    replayable_count = full_path + safety_prefix
    return {
        "summary_available": True,
        # ``total`` remains the legacy replayable-path count. Candidate count
        # additionally exposes diagnostic-only chains without presenting them
        # as selectable compatibility paths.
        "total": replayable_count,
        "replayable_count": replayable_count,
        "candidate_count": replayable_count + diagnostic_only,
        "full": full_path,
        "full_path": full_path,
        "safe_prefix": safety_prefix,
        "safety_prefix": safety_prefix,
        "diagnostic_only": diagnostic_only,
        "verified_twice": verified,
        "observed_once": observed,
    }


def _run_list_summary(
    states: List[InspectionState],
    *,
    families: Optional[List[InspectionExplorationFamily]] = None,
    run_status: Optional[str] = None,
    branches: Optional[List[InspectionBranchRun]] = None,
    transitions: Optional[List[InspectionTransition]] = None,
    faults: Optional[List[InspectionFault]] = None,
    replay_source_eligible: Optional[bool] = None,
) -> Dict[str, Any]:
    """Build the compact list summary from the same evidence as Graph/Replay."""
    summary_available = any(
        int(state.identity_version or 1) >= 2 for state in states
    )
    if not summary_available:
        return {
            "schema_version": GRAPH_SCHEMA_VERSION,
            "summary_available": False,
            "summary_unavailable_reason": "IDENTITY_V2_REQUIRED",
            "replay_source_eligible": False,
        }
    family_rows = list(families or [])
    family_ids = {
        int(item.id)
        for item in family_rows
        if item.id is not None
    } or {
        int(state.exploration_family_id)
        for state in states
        if state.exploration_family_id is not None
        and int(state.identity_version or 1) >= 2
    }
    representative_ids = {
        int(item.representative_state_id)
        for item in family_rows
        if item.representative_state_id is not None
    }
    if representative_ids:
        expanded_family_ids = {
            int(state.exploration_family_id)
            for state in states
            if state.exploration_family_id is not None
            and int(state.id or 0) in representative_ids
            and str(state.expansion_status or "").upper() == "EXPANDED"
        }
    else:
        # Historical family rows did not always persist a representative id.
        expanded_family_ids = {
            int(state.exploration_family_id)
            for state in states
            if state.exploration_family_id is not None
            and int(state.exploration_family_id) in family_ids
            and str(state.exploration_mode or "").upper() == "FULL"
            and str(state.expansion_status or "").upper() == "EXPANDED"
        }
    family_total = len(family_ids)
    family_expanded = len(expanded_family_ids)

    source_eligible = (
        replay_source_eligible
        if replay_source_eligible is not None
        else _replay_source_eligibility(
            run_status or "PASS",
            states=states,
            branches=branches,
        )[0]
    )
    semantics = _state_replay_semantics(
        states,
        transitions=transitions,
        faults=faults,
        replay_source_eligible=source_eligible,
    )
    replay_paths = _replay_path_summary(
        semantics,
        summary_available=summary_available,
    )
    ratio = round(family_expanded / family_total, 4) if family_total else 0.0
    return {
        "schema_version": GRAPH_SCHEMA_VERSION,
        "summary_available": True,
        "replay_source_eligible": source_eligible,
        "family_coverage": {
            "total": family_total,
            "representatives_expanded": family_expanded,
            "ratio": ratio,
        },
        "replay_eligible_count": replay_paths["replayable_count"],
        "verified_path_count": replay_paths["verified_twice"],
        "observed_replay_paths": replay_paths["observed_once"],
        "replay_paths": replay_paths,
    }


def _run_read(
    session: Session,
    row: InspectionRun,
    *,
    include_detail: bool = False,
    states: Optional[List[InspectionState]] = None,
    families: Optional[List[InspectionExplorationFamily]] = None,
    branches: Optional[List[InspectionBranchRun]] = None,
    transitions: Optional[List[InspectionTransition]] = None,
    faults: Optional[List[InspectionFault]] = None,
) -> InspectionRunRead:
    run_states = states
    if run_states is None:
        run_states = session.exec(
            select(InspectionState).where(InspectionState.run_id == row.id)
        ).all()
    run_families = families
    if run_families is None:
        run_families = session.exec(
            select(InspectionExplorationFamily).where(
                InspectionExplorationFamily.run_id == row.id
            )
        ).all()
    run_branches = branches
    if run_branches is None:
        run_branches = session.exec(
            select(InspectionBranchRun).where(InspectionBranchRun.run_id == row.id)
        ).all()
    run_transitions = transitions
    if run_transitions is None:
        run_transitions = session.exec(
            select(InspectionTransition).where(
                InspectionTransition.run_id == row.id,
                or_(
                    col(InspectionTransition.status).in_(
                        _TERMINAL_EVIDENCE_STATUSES
                    ),
                    InspectionTransition.failure_type.is_not(None),
                    InspectionTransition.risk_type.is_not(None),
                ),
            )
        ).all()
    run_faults = faults
    if run_faults is None:
        run_faults = session.exec(
            select(InspectionFault).where(InspectionFault.run_id == row.id)
        ).all()
    replay_source_eligible, replay_source_reason = _replay_source_eligibility(
        row.status,
        states=run_states,
        branches=run_branches,
    )
    summary = _run_list_summary(
        run_states,
        families=run_families,
        run_status=row.status,
        branches=run_branches,
        transitions=run_transitions,
        faults=run_faults,
        replay_source_eligible=replay_source_eligible,
    )
    exploration_coverage = dict(summary.get("family_coverage") or {})
    if exploration_coverage:
        summary["exploration_coverage"] = exploration_coverage
    assessment = (
        dict(row.coverage_assessment or {})
        if isinstance(row.coverage_assessment, dict)
        else {}
    )
    if assessment:
        summary["business_coverage"] = {
            **dict(assessment.get("summary") or {}),
            "selected_scope_verdict": assessment.get("selected_scope_verdict"),
            "full_app_verdict": assessment.get("full_app_verdict"),
            "blind_spot_count": len(assessment.get("blind_spots") or []),
            "manifest": dict(assessment.get("manifest") or {}),
        }
    replay_evidence_available = bool(
        replay_source_eligible
        and int(summary.get("replay_eligible_count") or 0) > 0
    )
    replay_default_eligible = bool(
        replay_evidence_available
        and str(row.status or "").upper() == "PASS"
        and str(assessment.get("selected_scope_verdict") or "") == "COMPLETE"
    )
    summary["replay_evidence_available"] = replay_evidence_available
    summary["replay_default_eligible"] = replay_default_eligible
    latest_observation = None
    if include_detail:
        latest_observation = session.exec(
            select(InspectionObservation)
            .where(InspectionObservation.run_id == row.id)
            .order_by(
                col(InspectionObservation.captured_at).desc(),
                col(InspectionObservation.id).desc(),
            )
        ).first()
    return InspectionRunRead(
        id=row.id,
        name=row.name,
        profile_id=row.profile_id,
        package_name=row.package_name,
        package_id=row.package_id,
        package_source=row.package_source,
        profile_snapshot=row.profile_snapshot or {},
        device_serial=row.device_serial,
        selected_branches=row.selected_branches or [],
        coverage_manifest_id=row.coverage_manifest_id,
        coverage_manifest_version=row.coverage_manifest_version,
        coverage_manifest_hash=row.coverage_manifest_hash,
        coverage_manifest_snapshot=row.coverage_manifest_snapshot or {},
        coverage_assessment=assessment,
        coverage_verdict=row.coverage_verdict or "NOT_EVALUATED",
        coverage_evaluated_at=row.coverage_evaluated_at,
        status=row.status,
        current_stage=row.current_stage,
        phase=row.current_stage,
        frontier=_frontier_summary(
            run_states,
            run_status=row.status,
            include_legacy_actions=include_detail,
        ),
        effective_features=_run_effective_features(session, row),
        stop_reason=row.stop_reason,
        total_branches=row.total_branches,
        total_clusters=row.total_clusters,
        total_states=row.total_states,
        total_transitions=row.total_transitions,
        blocked_count=row.blocked_count,
        stable_count=row.stable_count,
        fault_count=row.fault_count,
        summary=summary,
        summary_available=bool(summary.get("summary_available")),
        summary_unavailable_reason=summary.get("summary_unavailable_reason"),
        replay_source_eligible=replay_source_eligible,
        replay_source_reason=replay_source_reason,
        replay_evidence_available=replay_evidence_available,
        replay_default_eligible=replay_default_eligible,
        last_active_state_id=(
            int(latest_observation.state_id)
            if latest_observation is not None
            else _last_active_state_id(run_states)
        ),
        last_observation_id=(
            int(latest_observation.id)
            if latest_observation is not None and latest_observation.id is not None
            else None
        ),
        error_message=row.error_message,
        executor_name=row.executor_name,
        created_at=row.created_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        branches=(
            _branch_reads(
                session,
                row.id,
                run_status=row.status,
                states=run_states,
                include_legacy_actions=include_detail,
            )
            if include_detail
            else []
        ),
        faults=_fault_reads(session, row.id) if include_detail else [],
    )


@router.get("/profiles", response_model=List[InspectionProfileRead])
def list_profiles(
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_active_user),
):
    _ensure_enabled(session)
    rows = session.exec(
        select(InspectionProfile).order_by(
            InspectionProfile.updated_at.desc(),
            InspectionProfile.id.desc(),
        )
    ).all()
    return [_profile_read(item) for item in rows]


@router.post("/profiles", response_model=InspectionProfileRead)
def create_profile(
    payload: InspectionProfileCreate,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_active_user),
):
    _ensure_enabled(session)
    _validate_profile_references(session, payload)
    data = dump_model(payload)
    row = InspectionProfile(
        name=data["name"],
        package_name=data["package_name"],
        branches=data["branches"],
        input_rules=data["input_rules"],
        safety_rules=data["safety_rules"],
        sanitizer_rules=data["sanitizer_rules"],
        dynamic_text_patterns=data["dynamic_text_patterns"],
        budgets=data["budgets"],
        monitor_options=data["monitor_options"],
        user_id=current_user.id,
        updater_id=current_user.id,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return _profile_read(row)


@router.get("/profiles/{profile_id}", response_model=InspectionProfileRead)
def get_profile(
    profile_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_active_user),
):
    _ensure_enabled(session)
    row = session.get(InspectionProfile, profile_id)
    if row is None:
        raise HTTPException(status_code=404, detail="巡检配置不存在")
    return _profile_read(row)


@router.put("/profiles/{profile_id}", response_model=InspectionProfileRead)
def update_profile(
    profile_id: int,
    payload: InspectionProfileUpdate,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_active_user),
):
    _ensure_enabled(session)
    row = session.get(InspectionProfile, profile_id)
    if row is None:
        raise HTTPException(status_code=404, detail="巡检配置不存在")
    _validate_profile_references(session, payload)
    data = dump_model(payload)
    for key in (
        "name",
        "package_name",
        "branches",
        "input_rules",
        "safety_rules",
        "sanitizer_rules",
        "dynamic_text_patterns",
        "budgets",
        "monitor_options",
    ):
        setattr(row, key, data[key])
    row.updater_id = current_user.id
    row.updated_at = datetime.now()
    session.add(row)
    session.commit()
    session.refresh(row)
    return _profile_read(row)


@router.delete("/profiles/{profile_id}")
def delete_profile(
    profile_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_active_user),
):
    _ensure_enabled(session)
    row = session.get(InspectionProfile, profile_id)
    if row is None:
        raise HTTPException(status_code=404, detail="巡检配置不存在")
    active = session.exec(
        select(InspectionRun).where(
            InspectionRun.profile_id == profile_id,
            col(InspectionRun.status).in_(["PENDING", "RUNNING", "QUEUED"]),
        )
    ).first()
    if active:
        raise HTTPException(status_code=409, detail="存在运行中的巡检任务")
    # Runs keep a full profile snapshot; deleting the reusable source is safe.
    runs = session.exec(select(InspectionRun).where(InspectionRun.profile_id == profile_id)).all()
    for run in runs:
        run.profile_id = None
        session.add(run)
    session.delete(row)
    session.commit()
    return {"ok": True}


@router.post("/runs", response_model=InspectionRunRead)
def create_run(
    payload: InspectionRunCreate,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_active_user),
):
    _ensure_enabled(session)
    from backend.artifact_store import (
        AssetCapacityExceeded,
        ensure_asset_capacity_for_new_run,
    )

    try:
        ensure_asset_capacity_for_new_run(session)
    except AssetCapacityExceeded as exc:
        raise HTTPException(
            status_code=507,
            detail={"message": str(exc), "storage": exc.status},
        ) from exc
    profile = session.get(InspectionProfile, payload.profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="巡检配置不存在")
    configured_branches = set((profile.branches or {}).keys())
    for branch_key in payload.branches:
        if branch_key not in configured_branches:
            raise HTTPException(
                status_code=400,
                detail=f"巡检配置中不存在业务线: {branch_key}",
            )
    device = session.exec(select(Device).where(Device.serial == payload.device_serial)).first()
    if device is None:
        raise HTTPException(status_code=404, detail="设备不存在")
    if str(device.platform or "android").lower() != "android":
        raise HTTPException(status_code=400, detail="智能巡检首期仅支持 Android")
    if (
        str(device.status or "").upper() != "IDLE"
        or device.lease_task_id
        or legacy_fastbot_device_locked(device.serial)
    ):
        raise HTTPException(
            status_code=409,
            detail=f"设备非空闲: {device.status} ({device.lease_task_id or '-'})",
        )

    package_source = "installed"
    package_id = payload.package_id
    if package_id is not None:
        package = session.get(AppPackage, package_id)
        if package is None:
            raise HTTPException(status_code=404, detail="安装包不存在")
        if str(package.platform or "android").lower() != "android":
            raise HTTPException(status_code=400, detail="巡检仅支持 Android 安装包")
        if package.package_name and package.package_name != profile.package_name:
            raise HTTPException(status_code=400, detail="安装包包名与巡检配置不一致")
        package_source = "package"

    profile_snapshot = _profile_dict(profile)
    profile_snapshot.pop("id", None)
    profile_snapshot.pop("user_id", None)
    profile_snapshot.pop("created_at", None)
    profile_snapshot.pop("updated_at", None)
    profile_snapshot["selected_branches"] = list(payload.branches)
    if payload.duration_seconds is not None:
        run_budgets = dict(profile_snapshot.get("budgets") or {})
        run_budgets["duration_seconds"] = int(payload.duration_seconds)
        profile_snapshot["budgets"] = run_budgets
        profile_snapshot["run_overrides"] = {
            "duration_seconds": int(payload.duration_seconds),
        }
    profile_snapshot["graph_hierarchy_version"] = GRAPH_HIERARCHY_VERSION
    profile_snapshot["graph_schema_version"] = GRAPH_SCHEMA_VERSION
    effective_features = _current_effective_features(session)
    coverage_manifest = None
    if effective_features.get(FLAG_INSPECTION_BUSINESS_COVERAGE_V2, False):
        coverage_manifest = freeze_manifest(profile.package_name, payload.branches)
        if coverage_manifest is not None:
            profile_snapshot["coverage_manifest"] = coverage_manifest
            input_rules = [
                dict(item)
                for item in profile_snapshot.get("input_rules") or []
                if isinstance(item, dict)
                and str(item.get("id") or "") != haier_search_input_rule()["id"]
            ]
            input_rules.insert(0, haier_search_input_rule())
            profile_snapshot["input_rules"] = input_rules
    profile_snapshot["effective_features"] = effective_features
    # Engine rollout readers consume top-level keys. Keep those values frozen
    # alongside the structured API snapshot.
    profile_snapshot.update(effective_features)
    run = InspectionRun(
        name=payload.name or f"{profile.name} - {datetime.now():%Y-%m-%d %H:%M}",
        profile_id=profile.id,
        package_name=profile.package_name,
        package_id=package_id,
        package_source=package_source,
        profile_snapshot=profile_snapshot,
        device_serial=payload.device_serial,
        selected_branches=list(payload.branches),
        coverage_manifest_id=(
            str(coverage_manifest.get("id")) if coverage_manifest else None
        ),
        coverage_manifest_version=(
            str(coverage_manifest.get("version")) if coverage_manifest else None
        ),
        coverage_manifest_hash=(
            str(coverage_manifest.get("hash")) if coverage_manifest else None
        ),
        coverage_manifest_snapshot=coverage_manifest or {},
        coverage_verdict="PENDING" if coverage_manifest else "NOT_EVALUATED",
        status="PENDING",
        current_stage="等待设备租约",
        total_branches=len(payload.branches),
        executor_id=current_user.id,
        executor_name=current_user.username,
    )
    session.add(run)
    session.flush()
    for branch_key in payload.branches:
        config = (profile.branches or {}).get(branch_key) or {}
        session.add(
            InspectionBranchRun(
                run_id=run.id,
                branch_key=branch_key,
                branch_name=str(config.get("name") or branch_key),
            )
        )
    session.commit()
    session.refresh(run)
    event = abort_event_for_run(run.id)
    background_tasks.add_task(execute_inspection_run, run.id, event)
    return _run_read(session, run, include_detail=True)


@router.get("/runs", response_model=PaginatedInspectionRunRead)
def list_runs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    status: Optional[str] = None,
    keyword: Optional[str] = None,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_active_user),
):
    _ensure_enabled(session)
    conditions = []
    if status:
        normalized_status = status.strip().upper()
        if normalized_status == "RUNNING":
            conditions.append(col(InspectionRun.status).in_(["PENDING", "QUEUED", "RUNNING"]))
        elif normalized_status == "FAIL":
            conditions.append(col(InspectionRun.status).in_(["FAIL", "ERROR"]))
        elif normalized_status != "ALL":
            conditions.append(InspectionRun.status == normalized_status)
    if keyword and keyword.strip():
        text = f"%{keyword.strip()}%"
        conditions.append(
            or_(
                InspectionRun.name.ilike(text),
                InspectionRun.package_name.ilike(text),
                InspectionRun.device_serial.ilike(text),
            )
        )
    count_query = select(func.count(InspectionRun.id))
    rows_query = select(InspectionRun)
    for condition in conditions:
        count_query = count_query.where(condition)
        rows_query = rows_query.where(condition)
    total = int(session.exec(count_query).one() or 0)
    rows = session.exec(
        rows_query.order_by(InspectionRun.id.desc()).offset((page - 1) * page_size).limit(page_size)
    ).all()
    run_ids = [int(item.id) for item in rows if item.id is not None]
    states_by_run: Dict[int, List[InspectionState]] = {run_id: [] for run_id in run_ids}
    families_by_run: Dict[int, List[InspectionExplorationFamily]] = {
        run_id: [] for run_id in run_ids
    }
    branches_by_run: Dict[int, List[InspectionBranchRun]] = {
        run_id: [] for run_id in run_ids
    }
    transitions_by_run: Dict[int, List[InspectionTransition]] = {
        run_id: [] for run_id in run_ids
    }
    faults_by_run: Dict[int, List[InspectionFault]] = {
        run_id: [] for run_id in run_ids
    }
    if run_ids:
        for state in session.exec(
            select(InspectionState)
            .where(col(InspectionState.run_id).in_(run_ids))
            .order_by(InspectionState.run_id, InspectionState.id)
        ).all():
            states_by_run.setdefault(int(state.run_id), []).append(state)
        for family in session.exec(
            select(InspectionExplorationFamily)
            .where(col(InspectionExplorationFamily.run_id).in_(run_ids))
            .order_by(InspectionExplorationFamily.run_id, InspectionExplorationFamily.id)
        ).all():
            families_by_run.setdefault(int(family.run_id), []).append(family)
        for branch in session.exec(
            select(InspectionBranchRun)
            .where(col(InspectionBranchRun.run_id).in_(run_ids))
            .order_by(InspectionBranchRun.run_id, InspectionBranchRun.id)
        ).all():
            branches_by_run.setdefault(int(branch.run_id), []).append(branch)
        for transition in session.exec(
            select(InspectionTransition)
            .where(
                col(InspectionTransition.run_id).in_(run_ids),
                or_(
                    col(InspectionTransition.status).in_(
                        _TERMINAL_EVIDENCE_STATUSES
                    ),
                    InspectionTransition.failure_type.is_not(None),
                    InspectionTransition.risk_type.is_not(None),
                ),
            )
            .order_by(InspectionTransition.run_id, InspectionTransition.id)
        ).all():
            transitions_by_run.setdefault(int(transition.run_id), []).append(
                transition
            )
        for fault in session.exec(
            select(InspectionFault)
            .where(col(InspectionFault.run_id).in_(run_ids))
            .order_by(InspectionFault.run_id, InspectionFault.id)
        ).all():
            faults_by_run.setdefault(int(fault.run_id), []).append(fault)
    return PaginatedInspectionRunRead(
        total=total,
        items=[
            _run_read(
                session,
                item,
                states=states_by_run.get(int(item.id), []),
                families=families_by_run.get(int(item.id), []),
                branches=branches_by_run.get(int(item.id), []),
                transitions=transitions_by_run.get(int(item.id), []),
                faults=faults_by_run.get(int(item.id), []),
            )
            for item in rows
        ],
    )


@router.get("/runs/{run_id}", response_model=InspectionRunRead)
def get_run(
    run_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_active_user),
):
    _ensure_enabled(session)
    row = session.get(InspectionRun, run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="巡检任务不存在")
    return _run_read(session, row, include_detail=True)


@router.get("/runs/{run_id}/coverage")
def get_run_coverage(
    run_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_active_user),
):
    _ensure_enabled(session)
    row = session.get(InspectionRun, run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="巡检任务不存在")
    assessment = (
        dict(row.coverage_assessment or {})
        if isinstance(row.coverage_assessment, dict)
        else {}
    )
    if not assessment:
        return {
            "available": False,
            "run_id": run_id,
            "coverage_verdict": row.coverage_verdict or "NOT_EVALUATED",
            "reason": (
                "RUN_NOT_FINISHED"
                if str(row.status or "").upper() in ACTIVE_STATUSES
                else "HISTORICAL_ASSESSMENT_NOT_BACKFILLED"
            ),
        }
    return {
        "available": True,
        "run_id": run_id,
        "coverage_verdict": row.coverage_verdict,
        "coverage_evaluated_at": row.coverage_evaluated_at,
        "assessment": assessment,
    }


@router.delete("/runs/{run_id}")
def delete_run(
    run_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_active_user),
):
    _ensure_enabled(session)
    row = session.get(InspectionRun, run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="巡检任务不存在")
    normalized_status = str(row.status or "").upper()
    if normalized_status in ACTIVE_STATUSES:
        raise HTTPException(status_code=400, detail="运行中或取消中的巡检任务无法删除")
    device = session.exec(
        select(Device).where(Device.serial == row.device_serial)
    ).first()
    expected_lease_owner = f"inspection:{run_id}"
    if (
        device is not None
        and str(device.lease_task_id or "") == expected_lease_owner
    ):
        raise HTTPException(
            status_code=400,
            detail="巡检执行器仍在释放设备，请稍后再删除",
        )
    if normalized_status == "ABORTED" and row.current_stage == "取消中":
        # A process restart can happen after cancel persisted ABORTED but before
        # the background worker wrote its final stage. With no owner-safe device
        # lease left, the worker can no longer mutate the device or report, so
        # make the terminal record self-consistent before artifact deletion.
        now = datetime.now()
        row.current_stage = "已取消"
        row.stop_reason = row.stop_reason or "用户取消"
        row.finished_at = row.finished_at or now
        stale_branches = session.exec(
            select(InspectionBranchRun).where(
                InspectionBranchRun.run_id == run_id,
                col(InspectionBranchRun.status).in_(ACTIVE_STATUSES),
            )
        ).all()
        for branch in stale_branches:
            branch.status = "ABORTED"
            branch.current_stage = "已取消"
            branch.stop_reason = branch.stop_reason or row.stop_reason
            branch.finished_at = branch.finished_at or now
            session.add(branch)
        session.add(row)
        session.commit()

    try:
        artifacts_deleted = _delete_inspection_run_artifacts(run_id)
    except Exception as exc:
        logger.exception("delete inspection run artifacts failed: %s", run_id)
        raise HTTPException(
            status_code=500,
            detail=f"删除智能巡检报告文件失败: {exc}",
        ) from exc

    # Compatibility reports own copied baselines and path snapshots. Detach the
    # source relationship so those reports remain readable after this deletion.
    compatibility_runs = session.exec(
        select(CompatibilityRun).where(CompatibilityRun.inspection_run_id == run_id)
    ).all()
    for compatibility_run in compatibility_runs:
        compatibility_run.inspection_run_id = None
        session.add(compatibility_run)

    faults = session.exec(select(InspectionFault).where(InspectionFault.run_id == run_id)).all()
    transitions = session.exec(select(InspectionTransition).where(InspectionTransition.run_id == run_id)).all()
    observations = session.exec(select(InspectionObservation).where(InspectionObservation.run_id == run_id)).all()
    states = session.exec(select(InspectionState).where(InspectionState.run_id == run_id)).all()
    families = session.exec(
        select(InspectionExplorationFamily).where(InspectionExplorationFamily.run_id == run_id)
    ).all()
    family_ids = {item.id for item in families if item.id is not None}
    family_coverage = (
        session.exec(
            select(InspectionFamilyActionCoverage).where(col(InspectionFamilyActionCoverage.family_id).in_(family_ids))
        ).all()
        if family_ids
        else []
    )
    branches = session.exec(select(InspectionBranchRun).where(InspectionBranchRun.run_id == run_id)).all()

    # Keep deletion order compatible with databases that enforce foreign keys.
    try:
        from backend.artifact_store import release_owner_references

        for state in states:
            release_owner_references(
                session,
                owner_type="inspection_regression",
                owner_id=int(state.id),
                commit=False,
            )
            release_owner_references(
                session,
                owner_type="inspection_state",
                owner_id=int(state.id),
                commit=False,
            )
        for fault in faults:
            release_owner_references(
                session,
                owner_type="inspection_fault",
                owner_id=int(fault.id),
                commit=False,
            )
        for observation in observations:
            release_owner_references(
                session,
                owner_type="inspection_observation",
                owner_id=int(observation.id),
                commit=False,
            )
    except (ImportError, AttributeError):
        logger.warning("CAS reference release unavailable during inspection delete", exc_info=True)
    for state in states:
        state.exploration_family_id = None
        session.add(state)
    for transition in transitions:
        transition.coverage_source_transition_id = None
        session.add(transition)
    for family in families:
        family.representative_state_id = None
        session.add(family)
    session.flush()
    for item in family_coverage:
        session.delete(item)
    for item in families:
        session.delete(item)
    for item in faults:
        session.delete(item)
    for item in transitions:
        session.delete(item)
    for item in observations:
        session.delete(item)
    for item in states:
        session.delete(item)
    for item in branches:
        session.delete(item)
    session.delete(row)
    session.commit()
    discard_abort_event(run_id)
    return {
        "success": True,
        "deleted_branches": len(branches),
        "deleted_states": len(states),
        "deleted_transitions": len(transitions),
        "deleted_observations": len(observations),
        "deleted_faults": len(faults),
        "deleted_families": len(families),
        "deleted_family_actions": len(family_coverage),
        "detached_compatibility_runs": len(compatibility_runs),
        "artifacts_deleted": artifacts_deleted,
    }


@router.post("/runs/{run_id}/cancel", response_model=InspectionRunRead)
def cancel_run(
    run_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_active_user),
):
    _ensure_enabled(session)
    row = session.get(InspectionRun, run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="巡检任务不存在")
    if str(row.status or "").upper() in TERMINAL_STATUSES:
        return _run_read(session, row, include_detail=True)
    request_abort(run_id)
    row.status = "ABORTED"
    row.current_stage = "取消中"
    row.stop_reason = "用户取消"
    session.add(row)
    session.commit()
    session.refresh(row)
    try:
        inspection_live_registry.publish(
            run_id,
            "RUN_STAGE",
            run_status="ABORTED",
            current_stage="取消中",
            reason="用户取消",
            overlay_visible=False,
        )
    except Exception:
        logger.exception("巡检取消实时状态发布失败: run=%s", run_id)
    return _run_read(session, row, include_detail=True)


@router.get("/runs/{run_id}/graph")
def get_run_graph(
    run_id: int,
    branch_key: Optional[str] = None,
    include_paths: bool = False,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_active_user),
):
    _ensure_enabled(session)
    run = session.get(InspectionRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="巡检任务不存在")
    state_query = select(InspectionState).where(InspectionState.run_id == run_id)
    transition_query = select(InspectionTransition).where(InspectionTransition.run_id == run_id)
    branch_query = select(InspectionBranchRun).where(InspectionBranchRun.run_id == run_id)
    if branch_key:
        state_query = state_query.where(InspectionState.branch_key == branch_key)
        branch_query = branch_query.where(InspectionBranchRun.branch_key == branch_key)
        branch = session.exec(branch_query).first()
        if branch is None:
            raise HTTPException(status_code=404, detail="巡检业务线不存在")
        transition_query = transition_query.where(InspectionTransition.branch_run_id == branch.id)
        branches = [branch]
    else:
        branches = session.exec(branch_query).all()
    states = session.exec(state_query.order_by(InspectionState.id)).all()
    transitions = session.exec(transition_query.order_by(InspectionTransition.sequence, InspectionTransition.id)).all()
    all_state_ids = [
        int(item)
        for item in session.exec(
            select(InspectionState.id)
            .where(InspectionState.run_id == run_id)
            .order_by(InspectionState.id)
        ).all()
        if item is not None
    ]
    display_index_by_state = {
        state_id: index
        for index, state_id in enumerate(all_state_ids, start=1)
    }
    state_frontier = {
        int(item.id): _state_frontier_values(
            item,
            run_status=run.status,
            include_legacy_actions=True,
        )
        for item in states
        if item.id is not None
    }
    frontier = _frontier_summary(
        states,
        run_status=run.status,
        include_legacy_actions=True,
    )
    template_ids = {item.template_id for item in states if item.template_id is not None}
    templates = {
        item.id: item
        for item in (
            session.exec(select(InspectionPageTemplate).where(col(InspectionPageTemplate.id).in_(template_ids))).all()
            if template_ids
            else []
        )
    }
    representative_ids = {
        item.representative_observation_id for item in states if item.representative_observation_id is not None
    }
    representative_observations = {
        item.id: item
        for item in (
            session.exec(
                select(InspectionObservation).where(col(InspectionObservation.id).in_(representative_ids))
            ).all()
            if representative_ids
            else []
        )
    }
    root_state_by_branch = {item.id: item.root_state_id for item in branches if item.id is not None}
    transition_by_id = {item.id: item for item in transitions if item.id is not None}
    fault_query = select(InspectionFault).where(InspectionFault.run_id == run_id)
    if branch_key and branches:
        fault_query = fault_query.where(
            InspectionFault.branch_run_id == int(branches[0].id)
        )
    faults = session.exec(fault_query.order_by(InspectionFault.id)).all()
    transitions_by_source: Dict[int, List[InspectionTransition]] = {}
    for transition in transitions:
        transitions_by_source.setdefault(int(transition.from_state_id), []).append(transition)
    replay_source_eligible, replay_source_reason = _replay_source_eligibility(
        run.status,
        states=states,
        branches=branches,
    )
    summary_available = any(
        int(state.identity_version or 1) >= 2 for state in states
    )
    state_replay_semantics = _state_replay_semantics(
        states,
        transitions=transitions,
        faults=faults,
        replay_source_eligible=replay_source_eligible,
    )
    selected_state_ids = [int(item.id) for item in states if item.id is not None]
    latest_observation = (
        session.exec(
            select(InspectionObservation)
            .where(col(InspectionObservation.state_id).in_(selected_state_ids))
            .order_by(
                col(InspectionObservation.captured_at).desc(),
                col(InspectionObservation.id).desc(),
            )
        ).first()
        if selected_state_ids
        else None
    )
    profile_snapshot = run.profile_snapshot if isinstance(run.profile_snapshot, dict) else {}
    try:
        hierarchy_version = max(
            1,
            int(profile_snapshot.get("graph_hierarchy_version", 1)),
        )
    except (TypeError, ValueError):
        hierarchy_version = 1

    def hierarchy_role(state: InspectionState) -> Optional[str]:
        if hierarchy_version < 2:
            return None
        if root_state_by_branch.get(state.branch_run_id) == state.id:
            return "BRANCH_ROOT"
        incoming = transition_by_id.get(state.incoming_transition_id)
        relation_type = str(incoming.relation_type if incoming is not None else "").strip().upper()
        if relation_type == "PEER":
            return "PEER"
        if relation_type == "VIEWPORT":
            return "VIEWPORT"
        # A v2 run may still contain transitions captured without relation
        # metadata. Preserve the persisted viewport/page classification.
        if str(state.stable_status or "").upper() == "VIEWPORT":
            return "VIEWPORT"
        if state.parent_state_id is not None or incoming is not None:
            return "PAGE"
        return "ORPHAN"

    display_width = max(3, len(str(max(1, len(all_state_ids)))))
    nodes = []
    boundaries_by_transition_id: Dict[int, Dict[str, Any]] = {}
    for state in states:
        template = templates.get(state.template_id)
        observation = representative_observations.get(state.representative_observation_id)
        state_transitions = transitions_by_source.get(int(state.id or 0), [])
        replay_semantics = state_replay_semantics.get(
            int(state.id or 0),
            {
                "reachability_evidence": "UNKNOWN",
                "replay_scope": "NONE",
                "replay_eligibility": "NONE",
                "terminal_outcome": "NONE",
                "terminal_boundaries": [],
            },
        )
        terminal_boundaries = list(
            replay_semantics.get("terminal_boundaries") or []
        )
        replay_scope = str(replay_semantics.get("replay_scope") or "NONE")
        replay_eligibility = str(
            replay_semantics.get("replay_eligibility") or "NONE"
        )
        for boundary in terminal_boundaries:
            transition_id = boundary.get("transition_id")
            if transition_id is not None:
                boundaries_by_transition_id[int(transition_id)] = boundary
        reachability_evidence = state_reachability_evidence(
            state,
            has_observation=observation is not None,
        )
        replay_semantics["reachability_evidence"] = reachability_evidence
        display_index = display_index_by_state.get(int(state.id or 0), 0)
        display_label = f"P{display_index:0{display_width}d}"
        image_width = (
            int(observation.original_width)
            if observation is not None and observation.original_width
            else None
        )
        image_height = (
            int(observation.original_height)
            if observation is not None and observation.original_height
            else None
        )
        action_summary = {
            "total": len(state_transitions),
            "passed": sum(
                1 for item in state_transitions
                if str(item.status or "").upper() == "PASS"
            ),
            "blocked": sum(
                1 for item in state_transitions
                if str(item.status or "").upper() == "BLOCKED"
            ),
            "failed": sum(
                1 for item in state_transitions
                if str(item.status or "").upper()
                in {
                    "ERROR", "ACTION_ERROR", "LOCATOR_NOT_FOUND",
                    "LOCATOR_AMBIGUOUS", "PATH_DIVERGED", "APP_EXIT",
                }
            ),
            "primary_action": next(
                (
                    str(item.action_role or item.action_key or "")
                    for item in state_transitions
                    if item.action_role or item.action_key
                ),
                None,
            ),
        }
        asset_url = None
        thumbnail_path = state.thumbnail_path or state.screenshot_path
        if thumbnail_path:
            asset_url = f"/api/inspections/runs/{run_id}/assets?path={quote(thumbnail_path, safe='')}"
        nodes.append(
            {
                "id": str(state.id),
                "state_id": state.id,
                "display_index": display_index,
                "display_label": display_label,
                "page_title": _page_title(state, template),
                "branch_key": state.branch_key,
                "cluster_key": state.cluster_key,
                "state_key": state.state_key,
                "template_id": state.template_id,
                "semantic_key": state.semantic_key,
                "identity_version": state.identity_version,
                "instance_anchor": state.instance_anchor,
                "exploration_family_id": state.exploration_family_id,
                "family_match_confidence": state.family_match_confidence,
                "family_match_evidence": state.family_match_evidence or {},
                "exploration_mode": state.exploration_mode,
                "page_subtype": state.page_subtype,
                "coverage_status": state.coverage_status,
                "frontier_priority": state.frontier_priority,
                "frontier_reason": state.frontier_reason,
                "expansion_status": state_frontier[int(state.id)]["expansion_status"],
                "pending_action_count": state_frontier[int(state.id)]["pending_action_count"],
                "last_action_cursor": state.last_action_cursor,
                "recovery_retry_count": state.recovery_retry_count,
                "expansion_completed_at": state.expansion_completed_at,
                "page_role": template.page_role if template is not None else None,
                "activity_family": (template.activity_family if template is not None else None),
                "representative_observation_id": state.representative_observation_id,
                "observation_count": state.observation_count,
                "last_observed_at": state.last_observed_at,
                "queued_at": state.queued_at,
                "expanded_at": state.expanded_at,
                "activity": state.activity,
                "depth": state.depth,
                "parent_state_id": state.parent_state_id,
                "incoming_transition_id": state.incoming_transition_id,
                "hierarchy_role": hierarchy_role(state),
                "thumbnail_url": asset_url,
                "thumbnail_path": thumbnail_path,
                "thumbnail_asset_id": (observation.thumbnail_asset_id if observation is not None else None),
                "image_width": image_width,
                "image_height": image_height,
                "image_aspect_ratio": (
                    round(image_width / image_height, 6)
                    if image_width and image_height
                    else None
                ),
                "screenshot_path": state.screenshot_path,
                "screenshot_asset_id": (observation.screenshot_asset_id if observation is not None else None),
                "xml_path": state.xml_path,
                "xml_asset_id": (observation.xml_asset_id if observation is not None else None),
                "asset_status": (
                    observation.asset_status
                    if observation is not None
                    else "LEGACY"
                    if state.screenshot_path or state.xml_path
                    else "UNAVAILABLE"
                ),
                "asset_available": bool(
                    (
                        observation is not None
                        and (
                            observation.screenshot_asset_id
                            or observation.xml_asset_id
                            or observation.thumbnail_asset_id
                        )
                    )
                    or state.screenshot_path
                    or state.xml_path
                ),
                "xml_url": (
                    f"/api/inspections/runs/{run_id}/assets?path={quote(state.xml_path, safe='')}"
                    if state.xml_path
                    else None
                ),
                "stable_status": state.stable_status,
                "reachability_evidence": reachability_evidence,
                "replay_scope": replay_scope,
                "replay_eligibility": replay_eligibility,
                "terminal_outcome": replay_semantics.get(
                    "terminal_outcome", "NONE"
                ),
                "boundary_evidence": sorted(
                    {
                        str(item.get("boundary_evidence") or "UNKNOWN").upper()
                        for item in terminal_boundaries
                    }
                ),
                "terminal_boundaries": terminal_boundaries,
                "action_summary": action_summary,
                "selected_for_regression": state.selected_for_regression,
                "locator_quality": state.locator_quality,
                "is_dynamic": state.is_dynamic,
                "is_opaque": state.is_opaque,
                "visit_count": state.visit_count,
                **(
                    {"first_path": state.first_path or []}
                    if include_paths is True
                    else {}
                ),
            }
        )
    links = []
    for item in transitions:
        historical = _historical_transition_v4(item)
        boundary = boundaries_by_transition_id.get(int(item.id or 0), {})
        effective_disposition = str(
            historical.get("execution_disposition") or ""
        ).upper()
        links.append({
            "id": str(item.id),
            "source": str(item.from_state_id),
            "target": str(item.to_state_id) if item.to_state_id is not None else None,
            "from_state_id": item.from_state_id,
            "to_state_id": item.to_state_id,
            "action_type": item.action_type,
            "action_key": item.action_key,
            "sequence": item.sequence,
            "status": item.status,
            "risk_type": item.risk_type,
            "reason": item.reason,
            "locator_candidates": item.locator_candidates or [],
            "target_meta": item.target_meta or {},
            "relation_type": item.relation_type,
            "relation_confidence": item.relation_confidence,
            "topology_type": _effective_topology_type(item),
            **historical,
            "terminal_outcome": str(
                boundary.get("terminal_outcome") or "NONE"
            ).upper(),
            "boundary_evidence": str(
                boundary.get("boundary_evidence") or "UNKNOWN"
            ).upper(),
            "source_observation_id": item.source_observation_id,
            "target_observation_id": item.target_observation_id,
            "traversal_count": item.traversal_count,
            "target_was_existing": item.target_was_existing,
            "coordinate_only": item.coordinate_only,
            "replayable": item.replayable,
            "duration_ms": item.duration_ms,
            "input_rule_id": item.input_rule_id,
            "input_variable_key": item.input_variable_key,
            "input_length": item.input_length,
            "error_message": item.error_message,
            "coverage_contract_id": item.coverage_contract_id,
            "action_group_key": item.action_group_key,
            "sampling_disposition": item.sampling_disposition,
            "visual_locator_evidence": item.visual_locator_evidence or {},
            "action_role": item.action_role,
            "action_role_key": item.action_role_key,
            "execution_mode": (
                "COVERED_BY_CONTRACT"
                if str(item.status or "").upper() == "COVERED_BY_CONTRACT"
                else "EXECUTED"
                if effective_disposition in {"EXECUTED", "RESULT_UNKNOWN"}
                else "NOT_EXECUTED"
            ),
            "reuse_source_transition_id": item.coverage_source_transition_id,
            "recovery_attempt_count": item.recovery_attempt_count,
        })
    tree: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for state in states:
        activity = state.activity or "Unknown Activity"
        branch_node = tree.setdefault(state.branch_key, {})
        cluster_node = branch_node.setdefault(
            activity,
            {"clusters": {}, "state_count": 0},
        )
        cluster_node["state_count"] += 1
        cluster_node["clusters"].setdefault(state.cluster_key, []).append(state.id)
    locator_counts: Dict[str, int] = {}
    risk_counts: Dict[str, int] = {}
    fault_counts: Dict[str, int] = {}
    for item in transitions:
        used = str((item.target_meta or {}).get("used_locator") or "").lower()
        if not used:
            candidates = list(item.locator_candidates or [])
            used = (
                str(candidates[0].get("by") or "").lower()
                if candidates and isinstance(candidates[0], dict)
                else "coordinate"
                if item.coordinate_only
                else "none"
            )
        locator_counts[used] = locator_counts.get(used, 0) + 1
    action_records = _graph_action_records(states, transitions)
    scope_coverage = _scope_coverage_payload(
        session,
        run,
        branches,
        states,
        action_records,
    )
    for item in action_records:
        risk_type = str(item.get("risk_type") or "").strip()
        if risk_type:
            risk_counts[risk_type] = risk_counts.get(risk_type, 0) + 1
    for item in faults:
        fault_counts[item.fault_type] = fault_counts.get(item.fault_type, 0) + item.occurrence_count
    cycles = _cycle_summaries(states, transitions)
    topology_counts: Dict[str, int] = {}
    for item in transitions:
        topology = _effective_topology_type(item)
        topology_counts[topology] = topology_counts.get(topology, 0) + 1
    aborted_states = sum(1 for item in state_frontier.values() if item["expansion_status"] == "ABORTED")
    terminal_unexecuted = sum(1 for item in action_records if item["execution_disposition"] == "NOT_REACHED")
    family_query = select(InspectionExplorationFamily).where(InspectionExplorationFamily.run_id == run_id)
    if branch_key and branches:
        family_query = family_query.where(InspectionExplorationFamily.branch_run_id == branches[0].id)
    family_rows = session.exec(family_query).all()
    family_count = len(family_rows)
    family_representative_ids = {
        int(item.representative_state_id)
        for item in family_rows
        if item.representative_state_id is not None
    }
    expanded_family_count = len(
        {
            int(state.exploration_family_id)
            for state in states
            if state.exploration_family_id is not None
            and int(state.id) in family_representative_ids
            and str(state.expansion_status or "") == "EXPANDED"
        }
    )
    contract_query = select(InspectionCoverageContract).where(
        InspectionCoverageContract.run_id == run_id
    )
    if branch_key and branches:
        contract_query = contract_query.where(
            InspectionCoverageContract.branch_run_id == branches[0].id
        )
    coverage_contracts = session.exec(
        contract_query.order_by(InspectionCoverageContract.id)
    ).all()
    contract_statuses: Dict[str, int] = {}
    for contract in coverage_contracts:
        status = str(contract.status or "PENDING").lower()
        contract_statuses[status] = contract_statuses.get(status, 0) + 1
    family_coverage_ratio = (
        expanded_family_count / family_count if family_count else 0.0
    )
    reachability_counts: Dict[str, int] = {}
    replay_counts: Dict[str, int] = {}
    for node in nodes:
        reachability = str(node.get("reachability_evidence") or "UNKNOWN")
        eligibility = str(node.get("replay_scope") or "NONE")
        reachability_counts[reachability] = reachability_counts.get(reachability, 0) + 1
        replay_counts[eligibility] = replay_counts.get(eligibility, 0) + 1
    fault_summary = _fault_summary_counts(faults)
    replay_path_summary = _replay_path_summary(
        state_replay_semantics,
        summary_available=summary_available,
    )
    assessment = (
        dict(run.coverage_assessment or {})
        if isinstance(run.coverage_assessment, dict)
        else {}
    )
    assessment_summary = dict(assessment.get("summary") or {})
    business_coverage = {
        **assessment_summary,
        "selected_scope_verdict": assessment.get("selected_scope_verdict"),
        "full_app_verdict": assessment.get("full_app_verdict"),
        "blind_spot_count": len(assessment.get("blind_spots") or []),
        "manifest": dict(assessment.get("manifest") or {}),
    } if assessment else {}
    replay_evidence_available = bool(
        replay_source_eligible
        and int(replay_path_summary.get("replayable_count") or 0) > 0
    )
    replay_default_eligible = bool(
        replay_evidence_available
        and str(run.status or "").upper() == "PASS"
        and str(assessment.get("selected_scope_verdict") or "") == "COMPLETE"
    )
    summary = {
        "summary_available": summary_available,
        **(
            {"summary_unavailable_reason": "IDENTITY_V2_REQUIRED"}
            if not summary_available
            else {}
        ),
        "page_family_coverage": {
            "expanded": expanded_family_count,
            "total": family_count,
            "ratio": round(family_coverage_ratio, 4),
        },
        "exploration_coverage": {
            "expanded": expanded_family_count,
            "total": family_count,
            "ratio": round(family_coverage_ratio, 4),
        },
        "business_coverage": business_coverage,
        "replay_evidence_available": replay_evidence_available,
        "replay_default_eligible": replay_default_eligible,
        "reached_pages": sum(
            1 for item in nodes
            if item.get("reachability_evidence") != "UNKNOWN"
        ),
        "replay_paths": {
            **replay_path_summary,
            **(
                {"default_selection_limit": 20}
                if summary_available
                else {}
            ),
        },
        **fault_summary,
        "real_faults": (
            fault_summary["app_faults"] + fault_summary["infra_faults"]
        ),
        "attention_issues": (
            fault_summary["app_faults"]
            + fault_summary["infra_faults"]
            + fault_summary["automation_failures"]
        ),
        "safety_boundaries": sum(
            1
            for node in nodes
            for boundary in node.get("terminal_boundaries") or []
            if str(boundary.get("terminal_outcome") or "") == "SAFETY_BLOCKED"
        ),
        "attention_boundaries": sum(
            1
            for node in nodes
            for boundary in node.get("terminal_boundaries") or []
            if bool(boundary.get("attention_required"))
        ),
    }
    diagnostics = {
        "frontier": frontier,
        "reachability": reachability_counts,
        "replay_eligibility": replay_counts,
        "replay_scope": replay_counts,
        "locator_methods": locator_counts,
        "risks": risk_counts,
        "faults": fault_counts,
        "topology": topology_counts,
    }
    return {
        "schema_version": GRAPH_SCHEMA_VERSION,
        "run_id": run_id,
        "status": run.status,
        "phase": run.current_stage,
        "summary_available": summary_available,
        "summary_unavailable_reason": (
            None if summary_available else "IDENTITY_V2_REQUIRED"
        ),
        "replay_source_eligible": replay_source_eligible,
        "replay_source_reason": replay_source_reason,
        "replay_evidence_available": replay_evidence_available,
        "replay_default_eligible": replay_default_eligible,
        "coverage_verdict": run.coverage_verdict or "NOT_EVALUATED",
        "coverage_assessment": assessment,
        "scope_coverage": scope_coverage,
        "paths_included": include_paths is True,
        "replay_paths_url": (
            f"/api/inspections/runs/{run_id}/replay-paths"
            + (
                f"?branch_key={quote(branch_key, safe='')}"
                if branch_key
                else ""
            )
        ),
        "last_active_state_id": (
            int(latest_observation.state_id)
            if latest_observation is not None
            else _last_active_state_id(states)
        ),
        "last_observation_id": (
            int(latest_observation.id)
            if latest_observation is not None
            and latest_observation.id is not None
            else None
        ),
        "frontier": frontier,
        "effective_features": _run_effective_features(session, run),
        "hierarchy_version": hierarchy_version,
        "nodes": nodes,
        "links": links,
        "tree": tree,
        "cycles": cycles,
        "summary": summary,
        "diagnostics": diagnostics,
        "coverage_contracts": [
            InspectionCoverageContractRead.model_validate(item).model_dump()
            for item in coverage_contracts
        ],
        "stats": {
            "states": len(states),
            "observations": sum(max(0, int(item.observation_count or 0)) for item in states),
            "transitions": len(transitions),
            "cycles": len(cycles),
            "topology": topology_counts,
            "families": family_count,
            "families_discovered": family_count,
            "family_representatives_expanded": expanded_family_count,
            "family_coverage_ratio": round(family_coverage_ratio, 4),
            "coverage_contracts": contract_statuses,
            "actual_device_actions": sum(
                1 for item in action_records if bool(item.get("invoked"))
            ),
            "sampled_out": sum(
                1 for item in action_records if item.get("status") == "SAMPLED_OUT"
            ),
            "covered_by_contract": sum(
                1
                for item in action_records
                if item.get("status") == "COVERED_BY_CONTRACT"
            ),
            "navigation_reused": sum(
                1
                for item in action_records
                if item.get("status") == "NAVIGATION_REUSED"
            ),
            "visual_entries": sum(
                1 for item in transitions if bool(item.visual_locator_evidence)
            ),
            "viewport_observations": int(
                session.exec(
                    select(func.count(InspectionObservation.id)).where(
                        InspectionObservation.run_id == run_id,
                        InspectionObservation.capture_kind == "VIEWPORT",
                    )
                ).one()
                or 0
            ),
            "blocked": sum(1 for item in action_records if item["status"] == "BLOCKED"),
            "stable": sum(1 for item in states if item.stable_status == "STABLE"),
            "viewports": sum(1 for item in states if item.stable_status == "VIEWPORT"),
            "ambiguous": sum(1 for item in action_records if item["status"] == "AMBIGUOUS"),
            "locator_ambiguous": sum(
                1
                for item in action_records
                if item["failure_type"] == "LOCATOR_AMBIGUOUS" or item["status"] == "LOCATOR_AMBIGUOUS"
            ),
            "locator_drift": sum(1 for item in transitions if item.status == "LOCATOR_DRIFT"),
            "coordinate_stale": sum(1 for item in action_records if item["failure_type"] == "COORDINATE_STALE"),
            "coordinate_unsafe": sum(
                1
                for item in action_records
                if item["failure_type"] == "COORDINATE_UNSAFE" or item["status"] == "COORDINATE_UNSAFE"
            ),
            "locator_not_found": sum(1 for item in action_records if item["failure_type"] == "LOCATOR_NOT_FOUND"),
            "path_diverged": sum(1 for item in action_records if item["failure_type"] == "PATH_DIVERGED"),
            "parent_recovery_failed": sum(
                1
                for item in action_records
                if item["failure_type"] in {"PARENT_RECOVERY_FAILED", "PARENT_RECOVERY_CASCADE"}
            ),
            "not_reached": sum(
                1
                for item in action_records
                if item["status"] == "NOT_REACHED"
            ),
            "cancelled": sum(1 for item in action_records if item["status"] == "CANCELLED"),
            "budget_not_reached": sum(
                1 for item in action_records if item["status"] in {"BUDGET_NOT_REACHED", "BUDGET_LIMIT"}
            ),
            "queue_truncated": sum(1 for item in action_records if item["status"] == "QUEUE_TRUNCATED"),
            "terminal_unexecuted": terminal_unexecuted,
            "result_unknown": sum(1 for item in action_records if item["execution_disposition"] == "RESULT_UNKNOWN"),
            "aborted_states": aborted_states,
            "action_errors": sum(
                1
                for item in action_records
                if item["failure_type"] == "ACTION_ERROR" or item["status"] in {"ACTION_ERROR", "ERROR"}
            ),
            "coordinate_only": sum(1 for item in action_records if item["coordinate_only"]),
            "locator_methods": locator_counts,
            "risks": risk_counts,
            "faults": fault_counts,
        },
    }


@router.get(
    "/runs/{run_id}/replay-paths",
    response_model=PaginatedInspectionReplayPathRead,
)
def list_replay_paths(
    run_id: int,
    branch_key: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_active_user),
):
    """List frozen replay paths without putting locator steps in Graph."""
    del current_user
    _ensure_enabled(session)
    run = session.get(InspectionRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="巡检任务不存在")
    status_eligible, status_reason = _replay_source_eligibility(run.status)
    if not status_eligible:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "REPLAY_SOURCE_NOT_ELIGIBLE",
                "message": status_reason,
                "run_status": run.status,
            },
        )
    branch_query = select(InspectionBranchRun).where(
        InspectionBranchRun.run_id == run_id
    )
    if branch_key:
        branch_query = branch_query.where(InspectionBranchRun.branch_key == branch_key)
    branches = session.exec(branch_query.order_by(InspectionBranchRun.id)).all()
    if branch_key and not branches:
        raise HTTPException(status_code=404, detail="巡检业务线不存在")
    branch_ids = [int(item.id) for item in branches if item.id is not None]
    source_states = (
        session.exec(
            select(InspectionState).where(
                InspectionState.run_id == run_id,
                col(InspectionState.branch_run_id).in_(branch_ids),
            )
        ).all()
        if branch_ids
        else []
    )
    source_eligible, source_reason = _replay_source_eligibility(
        run.status,
        states=source_states,
        branches=branches,
    )
    if not source_eligible:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "REPLAY_SOURCE_NOT_ELIGIBLE",
                "message": source_reason,
                "run_status": run.status,
            },
        )
    items: List[Dict[str, Any]] = []
    aggregate: Dict[str, int] = {
        "full_path": 0,
        "safety_prefix": 0,
        "diagnostic_only": 0,
        "verified_twice": 0,
        "observed_once": 0,
    }
    for branch in branches:
        try:
            plan = build_replay_plan(
                session,
                run_id,
                str(branch.branch_key),
                max_chains=20,
                include_all_candidates=True,
            )
        except ReplayPlanError as exc:
            if branch_key:
                raise HTTPException(status_code=409, detail=exc.to_dict()) from exc
            continue
        branch_aggregate: Dict[str, int] = {
            "full_path": 0,
            "safety_prefix": 0,
            "diagnostic_only": 0,
            "verified_twice": 0,
            "observed_once": 0,
        }
        for chain in plan.get("chains") or []:
            if not isinstance(chain, dict):
                continue
            item = dict(chain)
            item["branch_key"] = branch.branch_key
            item["branch_name"] = branch.branch_name
            scope = _public_replay_scope(
                item.get("replay_scope") or item.get("replay_eligibility")
            )
            item["replay_scope"] = scope
            item["replay_eligibility"] = legacy_replay_eligibility(scope)
            item.setdefault(
                "terminal_outcome",
                _primary_terminal_outcome(
                    list(item.get("terminal_boundaries") or [])
                ),
            )
            items.append(item)
            if scope == "PREFIX_TO_SAFETY_BOUNDARY":
                branch_aggregate["safety_prefix"] += 1
            elif scope == "FULL_PATH":
                branch_aggregate["full_path"] += 1
            elif scope == "DIAGNOSTIC_ONLY":
                branch_aggregate["diagnostic_only"] += 1
            evidence = str(item.get("evidence_level") or "OBSERVED_ONCE").lower()
            if evidence == "verified_twice":
                branch_aggregate["verified_twice"] += 1
            else:
                branch_aggregate["observed_once"] += 1
        plan_summary = (
            plan.get("summary") if isinstance(plan.get("summary"), dict) else {}
        )
        scope_counts = (
            plan_summary.get("replay_scope_counts")
            if isinstance(plan_summary.get("replay_scope_counts"), dict)
            else {}
        )
        evidence_counts = (
            plan_summary.get("evidence_counts")
            if isinstance(plan_summary.get("evidence_counts"), dict)
            else {}
        )
        if scope_counts or "diagnostic_only_count" in plan_summary:
            branch_aggregate["full_path"] = int(
                scope_counts.get(
                    "FULL_PATH", plan_summary.get("full_path_count", 0)
                )
                or 0
            )
            branch_aggregate["safety_prefix"] = int(
                scope_counts.get(
                    "PREFIX_TO_SAFETY_BOUNDARY",
                    plan_summary.get("safe_prefix_count", 0),
                )
                or 0
            )
            branch_aggregate["diagnostic_only"] = int(
                plan_summary.get("diagnostic_only_count", 0) or 0
            )
        if evidence_counts:
            branch_aggregate["verified_twice"] = int(
                evidence_counts.get("VERIFIED_TWICE", 0) or 0
            )
            branch_aggregate["observed_once"] = int(
                evidence_counts.get("OBSERVED_ONCE", 0) or 0
            )
        for key, value in branch_aggregate.items():
            aggregate[key] += value
    items.sort(
        key=lambda item: (
            0 if item.get("evidence_level") == "VERIFIED_TWICE" else 1,
            0 if item.get("replay_eligibility") == "SAFE_PREFIX" else 1,
            str(item.get("branch_key") or ""),
            int(item.get("depth") or 0),
            str(item.get("path_key") or ""),
        )
    )
    total = len(items)
    start = (page - 1) * page_size
    return PaginatedInspectionReplayPathRead(
        run_id=run_id,
        branch_key=branch_key,
        total=total,
        page=page,
        page_size=page_size,
        summary={
            "total": total,
            "candidate_count": (
                aggregate["full_path"] + aggregate["safety_prefix"]
                + aggregate["diagnostic_only"]
            ),
            "replayable_count": (
                aggregate["full_path"] + aggregate["safety_prefix"]
            ),
            "default_selection_limit": 20,
            "full": aggregate["full_path"],
            "full_path": aggregate["full_path"],
            "safe_prefix": aggregate["safety_prefix"],
            "safety_prefix": aggregate["safety_prefix"],
            "diagnostic_only": aggregate["diagnostic_only"],
            "verified_twice": aggregate["verified_twice"],
            "observed_once": aggregate["observed_once"],
        },
        items=items[start : start + page_size],
    )


@router.get(
    "/runs/{run_id}/families",
    response_model=InspectionExplorationFamilyListRead,
)
def list_run_families(
    run_id: int,
    branch_key: Optional[str] = None,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_active_user),
):
    _ensure_enabled(session)
    run = session.get(InspectionRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="巡检任务不存在")

    query = select(InspectionExplorationFamily).where(InspectionExplorationFamily.run_id == run_id)
    if branch_key:
        branch = session.exec(
            select(InspectionBranchRun).where(
                InspectionBranchRun.run_id == run_id,
                InspectionBranchRun.branch_key == branch_key,
            )
        ).first()
        if branch is None:
            raise HTTPException(status_code=404, detail="巡检业务线不存在")
        query = query.where(InspectionExplorationFamily.branch_run_id == branch.id)
    families = session.exec(
        query.order_by(
            InspectionExplorationFamily.branch_run_id,
            InspectionExplorationFamily.id,
        )
    ).all()
    family_ids = {item.id for item in families if item.id is not None}
    coverage_rows = (
        session.exec(
            select(InspectionFamilyActionCoverage)
            .where(col(InspectionFamilyActionCoverage.family_id).in_(family_ids))
            .order_by(
                InspectionFamilyActionCoverage.family_id,
                InspectionFamilyActionCoverage.id,
            )
        ).all()
        if family_ids
        else []
    )
    contract_rows = (
        session.exec(
            select(InspectionCoverageContract)
            .where(
                InspectionCoverageContract.run_id == run_id,
                col(InspectionCoverageContract.source_family_id).in_(family_ids),
            )
            .order_by(
                InspectionCoverageContract.source_family_id,
                InspectionCoverageContract.id,
            )
        ).all()
        if family_ids
        else []
    )
    states = (
        session.exec(
            select(InspectionState).where(
                InspectionState.run_id == run_id,
                col(InspectionState.exploration_family_id).in_(family_ids),
            )
        ).all()
        if family_ids
        else []
    )
    coverage_by_family: Dict[int, List[InspectionFamilyActionCoverageRead]] = {}
    for item in coverage_rows:
        coverage_by_family.setdefault(int(item.family_id), []).append(
            InspectionFamilyActionCoverageRead.model_validate(item)
        )
    contracts_by_family: Dict[int, List[InspectionCoverageContractRead]] = {}
    for item in contract_rows:
        if item.source_family_id is None:
            continue
        contracts_by_family.setdefault(int(item.source_family_id), []).append(
            InspectionCoverageContractRead.model_validate(item)
        )
    states_by_family: Dict[int, List[InspectionState]] = {}
    for state in states:
        if state.exploration_family_id is not None:
            states_by_family.setdefault(int(state.exploration_family_id), []).append(state)
    items = []
    for family in families:
        family_states = states_by_family.get(int(family.id), [])
        items.append(
            InspectionExplorationFamilyRead.model_validate(family).model_copy(
                update={
                    "member_count": max(
                        int(family.member_count or 0),
                        len(family_states),
                    ),
                    "frontier": _frontier_summary(
                        family_states,
                        run_status=run.status,
                        include_legacy_actions=True,
                    ),
                    "action_coverage": coverage_by_family.get(
                        int(family.id),
                        [],
                    ),
                    "coverage_contracts": contracts_by_family.get(
                        int(family.id),
                        [],
                    ),
                }
            )
        )
    run_states = session.exec(select(InspectionState).where(InspectionState.run_id == run_id)).all()
    return InspectionExplorationFamilyListRead(
        schema_version=GRAPH_SCHEMA_VERSION,
        run_id=run_id,
        phase=run.current_stage,
        frontier=_frontier_summary(
            run_states,
            run_status=run.status,
            include_legacy_actions=True,
        ),
        effective_features=_run_effective_features(session, run),
        items=items,
    )


@router.get(
    "/runs/{run_id}/states/{state_id}/observations",
    response_model=PaginatedInspectionObservationRead,
)
def list_state_observations(
    run_id: int,
    state_id: int,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    observation_id: Optional[int] = None,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_active_user),
):
    _ensure_enabled(session)
    state = session.get(InspectionState, state_id)
    if state is None or state.run_id != run_id:
        raise HTTPException(status_code=404, detail="巡检状态不存在")
    conditions = [
        InspectionObservation.run_id == run_id,
        InspectionObservation.state_id == state_id,
    ]
    if observation_id is not None:
        conditions.append(InspectionObservation.id == observation_id)
    base = select(InspectionObservation).where(*conditions)
    total = int(session.exec(select(func.count(InspectionObservation.id)).where(*conditions)).one() or 0)
    items = session.exec(
        base.order_by(
            col(InspectionObservation.captured_at).desc(),
            col(InspectionObservation.id).desc(),
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": items,
    }


@router.put(
    "/runs/{run_id}/states/{state_id}/representative-observation",
    response_model=InspectionObservationRead,
)
def update_representative_observation(
    run_id: int,
    state_id: int,
    payload: InspectionRepresentativeUpdate,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_active_user),
):
    _ensure_enabled(session)
    state = session.get(InspectionState, state_id)
    if state is None or state.run_id != run_id:
        raise HTTPException(status_code=404, detail="巡检状态不存在")
    observation = session.get(InspectionObservation, payload.observation_id)
    if observation is None or observation.run_id != run_id or observation.state_id != state_id:
        raise HTTPException(status_code=400, detail="Observation 不属于该巡检状态")

    state_observations = session.exec(
        select(InspectionObservation).where(
            InspectionObservation.run_id == run_id,
            InspectionObservation.state_id == state_id,
        )
    ).all()
    for item in state_observations:
        item.is_representative = item.id == observation.id
        session.add(item)
    state.representative_observation_id = observation.id
    state.updated_at = datetime.now()
    session.add(state)
    session.commit()
    session.refresh(observation)
    return observation


@router.put("/runs/{run_id}/regression-selection")
def update_regression_selection(
    run_id: int,
    payload: InspectionSelectionUpdate,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_active_user),
):
    _ensure_enabled(session)
    run = session.get(InspectionRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="巡检任务不存在")
    requested = set(payload.state_ids)
    explicit_observation_ids = set(payload.observation_ids)
    states = session.exec(select(InspectionState).where(InspectionState.run_id == run_id)).all()
    state_by_id = {item.id: item for item in states}
    explicit_observations = (
        session.exec(
            select(InspectionObservation).where(col(InspectionObservation.id).in_(explicit_observation_ids))
        ).all()
        if explicit_observation_ids
        else []
    )
    found_observation_ids = {item.id for item in explicit_observations}
    missing_observations = explicit_observation_ids - found_observation_ids
    if missing_observations:
        raise HTTPException(
            status_code=400,
            detail=f"Observation 不存在: {sorted(missing_observations)}",
        )
    observations_by_state: Dict[int, InspectionObservation] = {}
    for observation in explicit_observations:
        if observation.run_id != run_id:
            raise HTTPException(
                status_code=400,
                detail=f"Observation 不属于该任务: {observation.id}",
            )
        if observation.state_id in observations_by_state:
            raise HTTPException(
                status_code=400,
                detail=f"同一状态只能冻结一个 Observation: {observation.state_id}",
            )
        observations_by_state[observation.state_id] = observation
        requested.add(observation.state_id)
    existing_ids = {item.id for item in states}
    missing = requested - existing_ids
    if missing:
        raise HTTPException(status_code=400, detail=f"状态不属于该任务: {sorted(missing)}")
    invalid = [
        item.id
        for item in states
        if item.id in requested and (item.stable_status != "STABLE" or item.locator_quality == "COORDINATE_ONLY")
    ]
    if invalid:
        raise HTTPException(
            status_code=400,
            detail=f"只能选择稳定且非坐标定位的状态: {invalid}",
        )
    for state in states:
        state.selected_for_regression = state.id in requested
        state.updated_at = datetime.now()
        session.add(state)
    frozen_observations: Dict[int, InspectionObservation] = dict(observations_by_state)
    for state_id in requested:
        if state_id in frozen_observations:
            continue
        state = state_by_id[state_id]
        if state.representative_observation_id is None:
            continue
        observation = session.get(
            InspectionObservation,
            state.representative_observation_id,
        )
        if observation is not None and observation.state_id == state_id:
            frozen_observations[state_id] = observation

    try:
        import backend.artifact_store as artifact_store

        pinned_class = getattr(
            artifact_store,
            "RETENTION_PINNED",
            getattr(artifact_store, "RETENTION_PIN", "PINNED"),
        )
        for state in states:
            artifact_store.release_owner_references(
                session,
                owner_type="inspection_regression",
                owner_id=int(state.id),
                commit=False,
            )
        for state_id, observation in frozen_observations.items():
            for role, asset_id in (
                ("screenshot", observation.screenshot_asset_id),
                ("xml", observation.xml_asset_id),
                ("thumbnail", observation.thumbnail_asset_id),
                ("action_map", observation.action_map_asset_id),
            ):
                if not asset_id:
                    continue
                artifact_store.upsert_reference(
                    session,
                    asset_id=asset_id,
                    owner_type="inspection_regression",
                    owner_id=int(state_id),
                    role=role,
                    retention_class=pinned_class,
                    pinned_reason="regression baseline",
                    commit=False,
                )
    except (ImportError, AttributeError):
        logger.warning("CAS regression pinning unavailable", exc_info=True)
    session.commit()
    return {
        "run_id": run_id,
        "state_ids": sorted(requested),
        "observation_ids": sorted(item.id for item in frozen_observations.values() if item.id is not None),
    }


@router.get("/runs/{run_id}/assets")
def get_run_asset(
    run_id: int,
    path: str = Query(..., min_length=1),
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_active_user),
):
    _ensure_enabled(session)
    if session.get(InspectionRun, run_id) is None:
        raise HTTPException(status_code=404, detail="巡检任务不存在")
    try:
        target = resolve_inspection_asset(path, run_id=run_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not target.is_file():
        raise HTTPException(status_code=404, detail="巡检产物不存在")
    media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(path=str(target), media_type=media_type, filename=target.name)


def _restore_latest_live_page(
    run: InspectionRun,
    session: Session,
) -> Optional[Dict[str, Any]]:
    """Restore only the authoritative expansion owner after process restart."""
    expanding_states = session.exec(
        select(InspectionState)
        .where(
            InspectionState.run_id == run.id,
            InspectionState.expansion_status == "EXPANDING",
        )
        .order_by(col(InspectionState.id))
    ).all()
    if len(expanding_states) != 1:
        if len(expanding_states) > 1:
            logger.warning(
                "skip ambiguous restored inspection owner: run=%s states=%s",
                run.id,
                [item.id for item in expanding_states],
            )
        return None
    state = expanding_states[0]
    if state.id is None or run.id is None:
        return None

    canonical_screenshot: Optional[str] = None
    if state.screenshot_path:
        try:
            screenshot_target = resolve_inspection_asset(
                state.screenshot_path,
                run_id=run.id,
            )
            if screenshot_target.is_file():
                canonical_screenshot = state.screenshot_path
        except (OSError, ValueError) as exc:
            logger.warning(
                "skip unsafe restored inspection screenshot: run=%s state=%s error=%s",
                run.id,
                state.id,
                exc,
            )

    public_map: Dict[str, Any] = {
        "actions": [],
        "run_id": run.id,
        "state_id": state.id,
        "branch_key": state.branch_key,
    }
    relative_path = (
        Path("inspection") / str(run.id) / str(state.branch_key) / str(state.id) / "actions.json"
    ).as_posix()
    try:
        target = resolve_inspection_asset(relative_path, run_id=run.id)
        if target.is_file() and target.stat().st_size <= 5 * 1024 * 1024:
            payload = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                normalize_terminal_action_entries(
                    payload,
                    phase=run.current_stage or "report",
                )
                public_map = sanitize_action_map_payload(
                    payload,
                    run_id=run.id,
                    state_id=state.id,
                    branch_key=state.branch_key,
                    activity=state.activity or "",
                    screenshot_path=canonical_screenshot or "",
                )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        logger.warning(
            "skip invalid restored inspection action map: run=%s state=%s error=%s",
            run.id,
            state.id,
            exc,
        )

    page: Dict[str, Any] = {
        "state_id": state.id,
        "activity": state.activity,
        "foreground_package": state.foreground_package,
    }
    for key in ("screen_width", "screen_height", "captured_at"):
        if public_map.get(key) is not None:
            page[key] = public_map[key]
    if canonical_screenshot:
        page["screenshot_path"] = canonical_screenshot
    return {
        "branch_key": state.branch_key,
        "page": page,
        "actions": list(public_map.get("actions") or []),
        "expansion_owner_state_id": state.id,
        # The process-local revision clock is intentionally restarted.  Its
        # purpose is to distinguish later owner switches in this registry.
        "expansion_epoch": 1,
        "expansion_status": "EXPANDING",
    }


def _ensure_live_snapshot(
    run: InspectionRun,
    *,
    session: Optional[Session] = None,
) -> Dict[str, Any]:
    """Return process-local live state, seeding it after a server restart."""
    snapshot = inspection_live_registry.snapshot(run.id)
    if snapshot is not None:
        return snapshot
    snapshot = inspection_live_registry.start_run(
        run.id,
        run.device_serial,
        str(run.status or "PENDING"),
    )
    restored = _restore_latest_live_page(run, session) if session is not None else None
    if restored is not None:
        action_panel = {
            "state_id": restored["expansion_owner_state_id"],
            "expansion_epoch": restored["expansion_epoch"],
            "expansion_status": restored["expansion_status"],
            "page": restored["page"],
            "actions": restored["actions"],
            "current_action": None,
            "canvas_matches_panel": False,
        }
        snapshot = inspection_live_registry.publish(
            run.id,
            "PAGE_ACTIONS",
            branch_key=restored["branch_key"],
            page=restored["page"],
            actions=restored["actions"],
            current_action=None,
            action_panel=action_panel,
            expansion_owner_state_id=restored["expansion_owner_state_id"],
            expansion_epoch=restored["expansion_epoch"],
            device_context={
                "phase": "recover",
                "canvas_matches_panel": False,
            },
            canvas_matches_panel=False,
            overlay_visible=False,
            run_status=run.status,
        )
    if str(run.status or "").upper() in TERMINAL_STATUSES:
        return inspection_live_registry.finish_run(
            run.id,
            str(run.status or "ERROR"),
            str(run.current_stage or "任务已结束"),
            reason=run.stop_reason or ("任务执行异常" if run.error_message else None),
        )
    if run.current_stage:
        return inspection_live_registry.publish(
            run.id,
            "RUN_STAGE",
            current_stage=run.current_stage,
            run_status=run.status,
        )
    return snapshot


@router.post("/runs/{run_id}/live-session")
def create_run_live_session(
    run_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user_no_token),
):
    """Issue short-lived, one-use tickets for live events and read-only video."""
    _ensure_enabled(session)
    run = session.get(InspectionRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="巡检任务不存在")
    if current_user.id is None:
        raise HTTPException(status_code=401, detail="登录状态无效")
    _ensure_live_snapshot(run, session=session)
    try:
        issued = inspection_live_registry.create_live_session(run_id, current_user.id)
    except RuntimeError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    issued["run_id"] = run_id
    issued["event_ws_url"] = f"/ws/inspections/runs/{run_id}/live?ticket={issued['event_ticket']}"
    issued["video_ws_url"] = f"/ws/inspections/runs/{run_id}/video?ticket={issued['video_ticket']}"
    issued["video_available"] = str(run.status or "").upper() in ACTIVE_STATUSES
    return issued


@router.get("/runs/{run_id}/live")
def get_run_live_snapshot(
    run_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_active_user),
):
    _ensure_enabled(session)
    run = session.get(InspectionRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="巡检任务不存在")
    return _ensure_live_snapshot(run, session=session)


@router.get("/runs/{run_id}/states/{state_id}/action-map")
def get_state_action_map(
    run_id: int,
    state_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_active_user),
):
    """Read one engine-produced, sanitized historical action overlay map."""
    _ensure_enabled(session)
    run = session.get(InspectionRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="巡检任务不存在")
    state = session.get(InspectionState, state_id)
    if state is None or state.run_id != run_id:
        raise HTTPException(status_code=404, detail="巡检状态不存在")
    relative_path = (
        Path("inspection") / str(run_id) / str(state.branch_key) / str(state_id) / "actions.json"
    ).as_posix()
    try:
        target = resolve_inspection_asset(relative_path, run_id=run_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not target.is_file():
        raise HTTPException(status_code=404, detail="控件动作地图不存在")
    if target.stat().st_size > 5 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="控件动作地图文件过大")
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        logger.warning(
            "invalid inspection action map: run=%s state=%s error=%s",
            run_id,
            state_id,
            exc,
        )
        raise HTTPException(status_code=500, detail="控件动作地图损坏") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=500, detail="控件动作地图格式错误")
    normalize_terminal_action_entries(
        payload,
        phase=run.current_stage or "report",
    )
    return sanitize_action_map_payload(
        payload,
        run_id=run_id,
        state_id=state_id,
        branch_key=state.branch_key,
        activity=state.activity or "",
        screenshot_path=state.screenshot_path or "",
    )


async def _close_live_websocket(
    websocket: WebSocket,
    *,
    code: int,
    reason: str,
) -> None:
    try:
        await websocket.close(code=code, reason=reason[:120])
    except Exception:
        pass


@ws_router.websocket("/ws/inspections/runs/{run_id}/live")
async def inspection_live_events(
    websocket: WebSocket,
    run_id: int,
    ticket: str = Query(..., min_length=20, max_length=256),
):
    """Stream complete latest-state snapshots; disconnect never aborts the run."""
    try:
        claim = inspection_live_registry.consume_ticket(
            ticket,
            run_id=run_id,
            kind="event",
        )
    except ValueError as exc:
        await _close_live_websocket(websocket, code=4401, reason=str(exc))
        return

    subscription = None
    try:
        try:
            subscription = inspection_live_registry.subscribe(run_id)
        except KeyError:
            await _close_live_websocket(
                websocket,
                code=4404,
                reason="inspection live state not found",
            )
            return
        snapshot = subscription.get(timeout=0.1)
        if snapshot is None:
            await _close_live_websocket(
                websocket,
                code=4404,
                reason="inspection live state not found",
            )
            return
        await websocket.accept()
        await websocket.send_json(snapshot)
        if snapshot.get("terminal"):
            await websocket.close(code=1000, reason="inspection run finished")
            return

        while True:
            try:
                next_snapshot = await asyncio.to_thread(subscription.get, 5.0)
            except queue.Empty:
                # A complete-snapshot heartbeat also lets the server observe a
                # browser that disappeared while the run emitted no events.
                next_snapshot = inspection_live_registry.snapshot(run_id)
            if next_snapshot is None:
                break
            await websocket.send_json(next_snapshot)
            if next_snapshot.get("terminal"):
                await websocket.close(code=1000, reason="inspection run finished")
                break
    except WebSocketDisconnect:
        logger.info("巡检实时事件 WebSocket 断开: run=%s", run_id)
    except RuntimeError as exc:
        # Starlette raises RuntimeError when a client vanishes during a send.
        logger.debug("巡检实时事件连接结束: run=%s error=%s", run_id, exc)
    except Exception:
        logger.exception("巡检实时事件流异常: run=%s", run_id)
        await _close_live_websocket(websocket, code=4500, reason="内部错误")
    finally:
        if subscription is not None:
            subscription.close()
        inspection_live_registry.release_channel(claim.session_id, "event")


@ws_router.websocket("/ws/inspections/runs/{run_id}/video")
async def inspection_live_video(
    websocket: WebSocket,
    run_id: int,
    ticket: str = Query(..., min_length=20, max_length=256),
):
    """Proxy the existing read-only scrcpy broadcast for an inspection run."""
    try:
        claim = inspection_live_registry.consume_ticket(
            ticket,
            run_id=run_id,
            kind="video",
        )
    except ValueError as exc:
        await _close_live_websocket(websocket, code=4401, reason=str(exc))
        return

    generator = None
    try:
        snapshot = inspection_live_registry.snapshot(run_id)
        if snapshot is None:
            await _close_live_websocket(
                websocket,
                code=4404,
                reason="inspection live state not found",
            )
            return
        if snapshot.get("terminal"):
            await websocket.accept()
            await websocket.close(code=1000, reason="inspection run finished")
            return
        serial = str(snapshot.get("device_serial") or "").strip()
        if not serial:
            await _close_live_websocket(websocket, code=4404, reason="device not found")
            return

        from backend.device_stream.manager import device_manager

        generator = device_manager.get_video_generator(serial)
        await websocket.accept()

        def _next_chunk():
            latest = inspection_live_registry.snapshot(run_id)
            if latest is None or latest.get("terminal"):
                return None
            try:
                return next(generator)
            except StopIteration:
                return None

        while True:
            chunk = await asyncio.to_thread(_next_chunk)
            if chunk is None:
                latest = inspection_live_registry.snapshot(run_id)
                if latest is None or latest.get("terminal"):
                    await websocket.close(
                        code=1000,
                        reason="inspection run finished",
                    )
                break
            latest = inspection_live_registry.snapshot(run_id)
            if latest is None or latest.get("terminal"):
                await websocket.close(code=1000, reason="inspection run finished")
                break
            if not chunk:
                continue
            await websocket.send_bytes(chunk)
    except WebSocketDisconnect:
        logger.info("巡检实时视频 WebSocket 断开: run=%s", run_id)
    except ValueError as exc:
        logger.warning("巡检实时视频不可用: run=%s error=%s", run_id, exc)
        await _close_live_websocket(websocket, code=4404, reason=str(exc))
    except RuntimeError as exc:
        logger.debug("巡检实时视频连接结束: run=%s error=%s", run_id, exc)
    except Exception:
        logger.exception("巡检实时视频流异常: run=%s", run_id)
        await _close_live_websocket(websocket, code=4500, reason="内部错误")
    finally:
        if generator is not None:
            try:
                generator.close()
            except Exception:
                pass
        inspection_live_registry.release_channel(claim.session_id, "video")
