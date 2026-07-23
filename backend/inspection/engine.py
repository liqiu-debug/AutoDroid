"""Deterministic breadth-first Android model inspection engine."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import re
import subprocess
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import func
from sqlmodel import Session, col, select

from backend.cross_platform_execution import run_case_with_standard_runner
from backend.database import engine
from backend.device_execution_lease import DeviceExecutionLease
from backend.feature_flags import (
    FLAG_INSPECTION_COVERAGE_SCHEDULER_V2,
    FLAG_INSPECTION_EXPLORATION_FAMILY_CONVERGENCE,
    FLAG_INSPECTION_SIMILARITY_CONVERGENCE,
    FLAG_INSPECTION_TSO_V2,
    FLAG_INSPECTION_VISUAL_HOME_ACTIONS,
    is_flag_enabled,
)
from backend.inspection.action_map import (
    build_action_map,
    finalize_action_map,
    read_action_map,
    update_action_map,
    write_action_map,
)
from backend.inspection.device import (
    CapturedPage,
    DeviceDisconnected,
    InspectionAborted,
    LocatorAmbiguous,
    LocatorDrift,
    connect_android,
    exact_parent_matches,
    is_white_screen,
    perform_action,
    ready_assertion_exists,
    wait_for_stable_page,
)
from backend.inspection.live import inspection_live_registry
from backend.inspection.monitor import InspectionMonitorSession
from backend.inspection.runtime import abort_event_for_run, discard_abort_event
from backend.inspection.sanitizer import InspectionArtifactSanitizer
from backend.inspection.semantics import (
    InspectionAction,
    PageModel,
    build_page_model,
    compare_exploration_families,
    compare_page_models,
    confirm_peer_navigation,
    coordinate_target_key,
    derive_instance_anchor,
    enumerate_actions,
    exploration_family_signature,
    locator_quality,
    phash_distance,
    visual_locator_matches,
)
from backend.models import (
    AppPackage,
    Device,
    GlobalVariable,
    InspectionBranchRun,
    InspectionCoverageContract,
    InspectionFault,
    InspectionExplorationFamily,
    InspectionFamilyActionCoverage,
    InspectionObservation,
    InspectionPageTemplate,
    InspectionRun,
    InspectionState,
    InspectionTransition,
    TestCase,
)
from backend.paths import project_path
from backend.run_control import register_device_abort, unregister_device_abort
from PIL import Image

logger = logging.getLogger(__name__)


@dataclass
class StateWork:
    state_id: int
    state_key: str
    cluster_key: str
    replay_key: str
    package_name: str
    activity: str
    screenshot_sha: str
    depth: int
    path: List[Dict[str, Any]]
    actions: List[InspectionAction]
    recovery_navigation_actions: List[InspectionAction] = field(default_factory=list)
    action_map: Dict[str, Any] = field(default_factory=dict)
    parent_state_id: Optional[int] = None
    semantic_key: str = ""
    template_key: str = ""
    role: str = "UNKNOWN"
    activity_family: str = ""
    observation_id: Optional[int] = None
    ancestry_state_ids: Tuple[int, ...] = field(default_factory=tuple)
    recovery_status: Optional[str] = None
    instance_anchor: str = ""
    exploration_family_id: Optional[int] = None
    exploration_mode: str = "INDEPENDENT"
    family_match_confidence: Optional[float] = None
    page_subtype: str = "UNKNOWN"
    coverage_status: str = "DISCOVERED"
    frontier_priority: int = 700
    frontier_reason: str = "DISCOVERED"
    viewport_semantic_keys: Tuple[str, ...] = field(default_factory=tuple)
    family_action_trail: Tuple[Tuple[int, str, bool], ...] = field(
        default_factory=tuple
    )


@dataclass
class PersistedState:
    work: Optional[StateWork]
    is_new: bool
    variant_capped: bool = False
    assign_incoming: bool = False
    observation_id: Optional[int] = None
    match_evidence: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class NavigationAnchor:
    group_key: str
    depth: int
    parent_state_id: Optional[int]


@dataclass(frozen=True)
class NavigationEntry:
    group_key: str
    state_id: int
    action: InspectionAction
    target_path: Tuple[Dict[str, Any], ...]


@dataclass
class BranchOutcome:
    status: str
    stop_reason: str
    hard_fault: bool = False
    warning: bool = False


class BranchPreparationFailed(RuntimeError):
    pass


class BudgetExceeded(RuntimeError):
    """Controlled convergence stop raised before exceeding a task budget."""

    _REASONS = {
        "DEADLINE": "达到时间预算",
        "STATES": "达到状态预算",
        "DEVICE_ACTIONS": "达到动作预算",
        "OBSERVATIONS": "达到采集预算",
        "ARTIFACT_BYTES": "达到资产容量预算",
        "NO_NEW_COVERAGE": "连续动作无新状态",
    }

    def __init__(self, code: str) -> None:
        self.code = str(code or "BUDGET").upper()
        self.reason = self._REASONS.get(self.code, "达到巡检预算")
        super().__init__(self.reason)


class ExplorationBudgetExceeded(BudgetExceeded):
    """The exploration share ended while the task-wide reserve remains."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.reason = (
            "探索阶段 90% 时间预算已用完"
            if self.code == "DEADLINE"
            else "探索阶段 90% 动作预算已用完"
            if self.code == "DEVICE_ACTIONS"
            else self.reason
        )
        self.args = (self.reason,)


class PathDiverged(RuntimeError):
    """A replay step no longer reaches its recorded logical page."""

    code = "PATH_DIVERGED"

    def __init__(
        self,
        *,
        phase: str,
        expected: str,
        actual: str,
        step_index: Optional[int] = None,
    ) -> None:
        self.phase = str(phase or "replay")
        self.expected = str(expected or "")
        self.actual = str(actual or "")
        self.step_index = step_index
        detail = f"{self.phase}: expected={self.expected or '-'} actual={self.actual or '-'}"
        if step_index is not None:
            detail = f"step={step_index + 1} {detail}"
        super().__init__(f"{self.code}: {detail}")


class BudgetGuard:
    """One atomic budget ledger shared by every branch in an inspection run."""

    DEFAULT_ARTIFACT_BYTES = 512 * 1024 * 1024

    def __init__(
        self,
        budgets: Optional[Dict[str, Any]] = None,
        *,
        started_at: Optional[float] = None,
    ) -> None:
        values = dict(budgets or {})
        duration = int(values.get("duration_seconds", 1800))
        self.deadline = float(started_at if started_at is not None else time.monotonic()) + max(0, duration)
        self.max_states = max(0, int(values.get("max_states", 200)))
        self.max_device_actions = max(
            0,
            int(values.get("max_device_actions", values.get("max_actions", 800))),
        )
        self.max_observations = max(
            0,
            int(values.get("max_observations", 400)),
        )
        total_artifact_bytes = max(
            0,
            int(values.get("max_artifact_bytes", self.DEFAULT_ARTIFACT_BYTES)),
        )
        default_fault_reserve = (
            64 * 1024 * 1024
            if total_artifact_bytes >= self.DEFAULT_ARTIFACT_BYTES
            else total_artifact_bytes // 8
        )
        self.max_fault_artifact_bytes = max(
            0,
            min(
                total_artifact_bytes,
                int(values.get("fault_artifact_bytes", default_fault_reserve)),
            ),
        )
        self.max_artifact_bytes = max(
            0,
            total_artifact_bytes - self.max_fault_artifact_bytes,
        )
        self.no_new_coverage_limit = max(
            0,
            int(
                values.get(
                    "no_new_coverage_limit",
                    values.get("no_new_state_limit", 100),
                )
            ),
        )
        self.states = 0
        self.device_actions = 0
        self.observations = 0
        self.artifact_bytes = 0
        self.fault_artifact_bytes = 0
        self.no_new_coverage = 0
        self._lock = threading.Lock()

    def check_deadline(self) -> None:
        if time.monotonic() >= self.deadline:
            raise BudgetExceeded("DEADLINE")

    def remaining_seconds(self, requested: Optional[float] = None) -> float:
        self.check_deadline()
        remaining = max(0.0, self.deadline - time.monotonic())
        if requested is None:
            return remaining
        return min(max(0.0, float(requested)), remaining)

    def before_device_interaction(
        self,
        operation: str,
        *,
        mutating: bool = False,
    ) -> None:
        del operation  # Retained in the API for diagnostics/instrumentation.
        self.check_deadline()
        if not mutating:
            return
        with self._lock:
            if self.device_actions >= self.max_device_actions:
                raise BudgetExceeded("DEVICE_ACTIONS")
            self.device_actions += 1

    def reserve_state(self) -> None:
        self.check_deadline()
        with self._lock:
            if self.states >= self.max_states:
                raise BudgetExceeded("STATES")
            self.states += 1

    def reserve_observation(self, artifact_bytes: int = 0) -> None:
        self.check_deadline()
        requested_bytes = max(0, int(artifact_bytes or 0))
        with self._lock:
            if self.observations >= self.max_observations:
                raise BudgetExceeded("OBSERVATIONS")
            if self.artifact_bytes + requested_bytes > self.max_artifact_bytes:
                raise BudgetExceeded("ARTIFACT_BYTES")
            self.observations += 1
            self.artifact_bytes += requested_bytes

    def reserve_persistence(
        self,
        *,
        new_state: bool,
        observation: bool,
        artifact_bytes: int = 0,
    ) -> None:
        """Atomically reserve all counters needed by one capture write."""
        self.check_deadline()
        requested_bytes = max(0, int(artifact_bytes or 0))
        with self._lock:
            if new_state and self.states >= self.max_states:
                raise BudgetExceeded("STATES")
            if observation and self.observations >= self.max_observations:
                raise BudgetExceeded("OBSERVATIONS")
            if observation and self.artifact_bytes + requested_bytes > self.max_artifact_bytes:
                raise BudgetExceeded("ARTIFACT_BYTES")
            if new_state:
                self.states += 1
            if observation:
                self.observations += 1
                self.artifact_bytes += requested_bytes

    def record_coverage(self, *, discovered: bool) -> None:
        with self._lock:
            if discovered:
                self.no_new_coverage = 0
                return
            self.no_new_coverage += 1
            if (
                self.no_new_coverage_limit > 0
                and self.no_new_coverage >= self.no_new_coverage_limit
            ):
                raise BudgetExceeded("NO_NEW_COVERAGE")

    def reserve_fault_artifact(self, artifact_bytes: int) -> None:
        self.check_deadline()
        requested_bytes = max(0, int(artifact_bytes or 0))
        with self._lock:
            if (
                self.fault_artifact_bytes + requested_bytes
                > self.max_fault_artifact_bytes
            ):
                raise BudgetExceeded("ARTIFACT_BYTES")
            self.fault_artifact_bytes += requested_bytes

    def snapshot(self) -> Dict[str, int]:
        with self._lock:
            return {
                "states": self.states,
                "device_actions": self.device_actions,
                "observations": self.observations,
                "artifact_bytes": self.artifact_bytes,
                "fault_artifact_bytes": self.fault_artifact_bytes,
                "no_new_coverage": self.no_new_coverage,
            }

    def for_branch(self, remaining_branches: int) -> "BranchBudgetView":
        divisor = max(1, int(remaining_branches or 1))
        with self._lock:
            now = time.monotonic()
            remaining_seconds = max(0.0, self.deadline - now)

            def share(remaining: int) -> int:
                return max(0, (max(0, remaining) + divisor - 1) // divisor)

            return BranchBudgetView(
                self,
                deadline=min(
                    self.deadline,
                    now + (remaining_seconds / divisor),
                ),
                max_states=share(self.max_states - self.states),
                max_device_actions=share(
                    self.max_device_actions - self.device_actions
                ),
                max_observations=share(
                    self.max_observations - self.observations
                ),
                max_artifact_bytes=share(
                    self.max_artifact_bytes - self.artifact_bytes
                ),
            )


class BranchBudgetView:
    """Fair per-branch allowance backed by one task-wide BudgetGuard."""

    def __init__(
        self,
        guard: BudgetGuard,
        *,
        deadline: float,
        max_states: int,
        max_device_actions: int,
        max_observations: int,
        max_artifact_bytes: int,
    ) -> None:
        self.guard = guard
        self.deadline = min(float(deadline), guard.deadline)
        self.max_states = max_states
        self.max_device_actions = max_device_actions
        self.max_observations = max_observations
        self.max_artifact_bytes = max_artifact_bytes
        self.states = 0
        self.device_actions = 0
        self.observations = 0
        self.artifact_bytes = 0
        self._lock = threading.Lock()

    def check_deadline(self) -> None:
        self.guard.check_deadline()
        if time.monotonic() >= self.deadline:
            raise BudgetExceeded("DEADLINE")

    def remaining_seconds(self, requested: Optional[float] = None) -> float:
        self.check_deadline()
        remaining = max(0.0, self.deadline - time.monotonic())
        if requested is None:
            return remaining
        return min(max(0.0, float(requested)), remaining)

    def before_device_interaction(
        self,
        operation: str,
        *,
        mutating: bool = False,
    ) -> None:
        self.check_deadline()
        if not mutating:
            self.guard.before_device_interaction(operation, mutating=False)
            return
        with self._lock:
            if self.device_actions >= self.max_device_actions:
                raise BudgetExceeded("DEVICE_ACTIONS")
            self.guard.before_device_interaction(operation, mutating=True)
            self.device_actions += 1

    def reserve_persistence(
        self,
        *,
        new_state: bool,
        observation: bool,
        artifact_bytes: int = 0,
    ) -> None:
        requested_bytes = max(0, int(artifact_bytes or 0))
        with self._lock:
            if new_state and self.states >= self.max_states:
                raise BudgetExceeded("STATES")
            if observation and self.observations >= self.max_observations:
                raise BudgetExceeded("OBSERVATIONS")
            if (
                observation
                and self.artifact_bytes + requested_bytes
                > self.max_artifact_bytes
            ):
                raise BudgetExceeded("ARTIFACT_BYTES")
            self.guard.reserve_persistence(
                new_state=new_state,
                observation=observation,
                artifact_bytes=requested_bytes,
            )
            if new_state:
                self.states += 1
            if observation:
                self.observations += 1
                self.artifact_bytes += requested_bytes

    def record_coverage(self, *, discovered: bool) -> None:
        self.guard.record_coverage(discovered=discovered)

    def reserve_fault_artifact(self, artifact_bytes: int) -> None:
        self.guard.reserve_fault_artifact(artifact_bytes)

    def snapshot(self) -> Dict[str, int]:
        return self.guard.snapshot()


class ExplorationBudgetView:
    """Hard 90% exploration share backed by the branch/task budget ledger."""

    def __init__(
        self,
        guard: Any,
        *,
        deadline: float,
        max_device_actions: int,
    ) -> None:
        self.guard = guard
        self.deadline = min(float(deadline), float(guard.deadline))
        self.max_device_actions = max(0, int(max_device_actions))
        self.device_actions = 0
        self._lock = threading.Lock()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.guard, name)

    def check_deadline(self) -> None:
        self.guard.check_deadline()
        if time.monotonic() >= self.deadline:
            raise ExplorationBudgetExceeded("DEADLINE")

    def remaining_seconds(self, requested: Optional[float] = None) -> float:
        self.check_deadline()
        remaining = max(0.0, self.deadline - time.monotonic())
        if requested is None:
            return remaining
        return min(max(0.0, float(requested)), remaining)

    def before_device_interaction(
        self,
        operation: str,
        *,
        mutating: bool = False,
    ) -> None:
        self.check_deadline()
        if not mutating:
            self.guard.before_device_interaction(operation, mutating=False)
            return
        with self._lock:
            if self.device_actions >= self.max_device_actions:
                raise ExplorationBudgetExceeded("DEVICE_ACTIONS")
            self.guard.before_device_interaction(operation, mutating=True)
            self.device_actions += 1

    def reserve_persistence(self, **kwargs: Any) -> None:
        self.guard.reserve_persistence(**kwargs)

    def reserve_state(self) -> None:
        self.guard.reserve_state()

    def reserve_observation(self, artifact_bytes: int = 0) -> None:
        self.guard.reserve_observation(artifact_bytes)

    def reserve_fault_artifact(self, artifact_bytes: int) -> None:
        self.guard.reserve_fault_artifact(artifact_bytes)

    def record_coverage(self, *, discovered: bool) -> None:
        self.guard.record_coverage(discovered=discovered)

    def snapshot(self) -> Dict[str, int]:
        return self.guard.snapshot()


def _now() -> datetime:
    return datetime.now()


def _safe_error(exc: BaseException, limit: int = 1000) -> str:
    text = str(exc or "").replace("\x00", "").strip()
    return text[:limit]


def _check_abort(event: threading.Event) -> None:
    if event.is_set():
        raise InspectionAborted("inspection cancelled")


def _budgeted_wait_for_stable_page(
    device,
    *,
    budget_guard: Optional[BudgetGuard] = None,
    **kwargs: Any,
) -> CapturedPage:
    if budget_guard is not None:
        kwargs["max_wait_seconds"] = budget_guard.remaining_seconds(
            kwargs.get("max_wait_seconds")
        )
    capture = wait_for_stable_page(device, **kwargs)
    if budget_guard is not None:
        budget_guard.check_deadline()
    return capture


def _reports_root() -> Path:
    return project_path("reports").resolve()


def _safe_report_path(path: Path) -> Path:
    reports_root = _reports_root()
    raw_candidate = path.absolute()
    try:
        raw_relative = raw_candidate.relative_to(reports_root)
    except ValueError as exc:
        raise ValueError("inspection artifact path escapes reports root") from exc
    if ".." in raw_relative.parts:
        raise ValueError("inspection artifact path contains traversal")
    current = reports_root
    for part in raw_relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("inspection artifact path contains a symlink")

    candidate = raw_candidate.resolve(strict=False)
    try:
        candidate.relative_to(reports_root)
    except ValueError as exc:
        raise ValueError("inspection artifact path escapes reports root") from exc
    return candidate


def _relative_report_path(path: Path) -> str:
    return _safe_report_path(path).relative_to(_reports_root()).as_posix()


def _state_action_map_path(run_id: int, branch_key: str, state_id: int) -> Path:
    return _safe_report_path(
        _reports_root()
        / "inspection"
        / str(int(run_id))
        / str(branch_key)
        / str(int(state_id))
        / "actions.json"
    )


def _capture_screen_size(
    capture: CapturedPage,
    fallback: Tuple[int, int],
) -> Tuple[int, int]:
    try:
        with Image.open(io.BytesIO(capture.screenshot_png)) as image:
            width, height = image.size
        if width > 0 and height > 0:
            return int(width), int(height)
    except Exception:
        pass
    return int(fallback[0]), int(fallback[1])


def _bounds_iou(left: Any, right: Any) -> float:
    if not (
        isinstance(left, (list, tuple))
        and len(left) == 4
        and isinstance(right, (list, tuple))
        and len(right) == 4
    ):
        return 0.0
    try:
        lx1, ly1, lx2, ly2 = (int(value) for value in left)
        rx1, ry1, rx2, ry2 = (int(value) for value in right)
    except (TypeError, ValueError):
        return 0.0
    intersection = max(0, min(lx2, rx2) - max(lx1, rx1)) * max(
        0, min(ly2, ry2) - max(ly1, ry1)
    )
    left_area = max(0, lx2 - lx1) * max(0, ly2 - ly1)
    right_area = max(0, rx2 - rx1) * max(0, ry2 - ry1)
    union = left_area + right_area - intersection
    return intersection / union if union > 0 else 0.0


def _rebind_action_on_capture(
    action: InspectionAction,
    capture: CapturedPage,
    *,
    screen_size: Tuple[int, int],
    safety_rules: Sequence[Dict[str, Any]],
    input_rules: Sequence[Dict[str, Any]],
    max_scrolls: int,
    coverage_scheduler: bool = False,
) -> Optional[InspectionAction]:
    candidates = enumerate_actions(
        capture.model,
        screen_size=_capture_screen_size(capture, screen_size),
        safety_rules=safety_rules,
        input_rules=input_rules,
        max_scrolls_per_direction=max_scrolls,
        coverage_scheduler_v2=coverage_scheduler,
    )
    matched = [
        candidate
        for candidate in candidates
        if candidate.action_type == action.action_type
        and candidate.action_role_key == action.action_role_key
        and (
            not action.action_anchor_key
            or candidate.action_anchor_key == action.action_anchor_key
        )
        and candidate.risk_type == action.risk_type
        and all(
            candidate.target_meta.get(key) == action.target_meta.get(key)
            for key in ("enabled", "checked", "selected")
        )
    ]
    if not matched:
        return None
    ranked = sorted(
        [
            (
                _bounds_iou(
                    action.target_meta.get("bounds"),
                    candidate.target_meta.get("bounds"),
                ),
                candidate,
            )
            for candidate in matched
        ],
        key=lambda item: item[0],
    )
    best_score, best = ranked[-1]
    if len(ranked) > 1 and best_score == ranked[-2][0]:
        raise LocatorAmbiguous("fresh action role matches multiple controls")
    if len(ranked) > 1 and best_score < 0.50:
        raise LocatorAmbiguous("fresh action role lacks a stable geometric match")
    return replace(
        best,
        action_key=action.action_key,
        action_role=action.action_role,
        action_role_key=action.action_role_key,
        action_anchor_key=action.action_anchor_key,
        action_group_key=action.action_group_key,
        action_instance_key=action.action_instance_key,
        sample_policy=action.sample_policy,
        input_rule_id=action.input_rule_id,
        input_variable_key=action.input_variable_key,
    )


def _persist_work_action_map(
    work: StateWork,
    *,
    finalize: bool = False,
    pending_status: str = "NOT_REACHED",
    reason: Optional[str] = None,
    phase: Optional[str] = None,
) -> None:
    if not work.action_map:
        return
    if finalize:
        finalize_action_map(
            work.action_map,
            pending_status=pending_status,
            reason=reason,
            phase=phase,
        )
    write_action_map(
        _state_action_map_path(
            int(work.action_map.get("run_id") or 0),
            str(work.action_map.get("branch_key") or ""),
            work.state_id,
        ),
        work.action_map,
    )


def _publish_live(run_id: int, event_type: str, **patch: Any) -> None:
    """Publish observation data without coupling viewers to device execution."""
    try:
        inspection_live_registry.publish(run_id, event_type, **patch)
    except Exception:
        logger.exception(
            "inspection live event publish failed: run=%s event=%s",
            run_id,
            event_type,
        )


def _live_page(work: StateWork) -> Dict[str, Any]:
    action_map = work.action_map or {}
    return {
        "state_id": work.state_id,
        "activity": work.activity,
        "foreground_package": work.package_name,
        "screen_width": action_map.get("screen_width"),
        "screen_height": action_map.get("screen_height"),
        "screenshot_path": action_map.get("screenshot_path"),
        "captured_at": action_map.get("captured_at"),
    }


def _live_actions(work: Optional[StateWork]) -> List[Dict[str, Any]]:
    if work is None:
        return []
    return [
        dict(item)
        for item in work.action_map.get("actions") or []
        if isinstance(item, dict)
    ]


def _live_action(
    work: Optional[StateWork],
    action_key: Optional[str],
) -> Optional[Dict[str, Any]]:
    if work is None or not action_key:
        return None
    return next(
        (
            dict(item)
            for item in work.action_map.get("actions") or []
            if isinstance(item, dict)
            and str(item.get("action_key") or "") == str(action_key)
        ),
        None,
    )


def resolve_inspection_asset(path_value: str, *, run_id: int) -> Path:
    """Resolve a stored asset and reject traversal and symlink escapes."""
    normalized = str(path_value or "").strip().lstrip("/")
    if not normalized:
        raise ValueError("empty inspection asset path")
    relative = Path(normalized)
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("inspection asset path contains traversal")
    expected_prefix = Path("inspection") / str(int(run_id))
    try:
        relative.relative_to(expected_prefix)
    except ValueError as exc:
        raise ValueError("asset is outside this inspection run") from exc

    reports_root = _reports_root()
    raw_run_root = reports_root / expected_prefix
    raw_candidate = reports_root / relative
    current = reports_root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("symlink assets are not allowed")

    run_root = raw_run_root.resolve(strict=False)
    candidate = raw_candidate.resolve(strict=False)
    try:
        candidate.relative_to(run_root)
    except ValueError as exc:
        raise ValueError("asset is outside this inspection run") from exc
    return candidate


def _serialize_action(
    action: InspectionAction,
    *,
    expected_source_semantic_key: Optional[str] = None,
    expected_target_semantic_key: Optional[str] = None,
    expected_target_role: Optional[str] = None,
    expected_target_template_key: Optional[str] = None,
    expected_source_signature: Optional[Dict[str, Any]] = None,
    expected_target_signature: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    payload = {
        "action_type": action.action_type,
        "action_key": action.action_key,
        "locator_candidates": [dict(item) for item in action.locator_candidates],
        "target_meta": dict(action.target_meta),
        "coordinate_only": action.coordinate_only,
        "replayable": action.replayable,
        "risk_type": action.risk_type,
        "blocked_reason": action.blocked_reason,
        "input_rule_id": action.input_rule_id,
        "input_variable_key": action.input_variable_key,
        "action_role": action.action_role,
        "action_role_key": action.action_role_key,
        "action_anchor_key": action.action_anchor_key,
        "action_group_key": action.action_group_key,
        "action_instance_key": action.action_instance_key,
        "sample_policy": action.sample_policy,
    }
    expectations = {
        "expected_source_semantic_key": expected_source_semantic_key,
        "expected_target_semantic_key": expected_target_semantic_key,
        "expected_target_role": expected_target_role,
        "expected_target_template_key": expected_target_template_key,
    }
    payload.update(
        {
            key: str(value)
            for key, value in expectations.items()
            if value not in (None, "")
        }
    )
    if expected_source_signature:
        payload["expected_source_signature"] = dict(expected_source_signature)
    if expected_target_signature:
        payload["expected_target_signature"] = dict(expected_target_signature)
    return payload


def _deserialize_action(payload: Dict[str, Any]) -> InspectionAction:
    return InspectionAction(
        action_type=str(payload.get("action_type") or "click"),
        action_key=str(payload.get("action_key") or ""),
        locator_candidates=[
            dict(item)
            for item in payload.get("locator_candidates") or []
            if isinstance(item, dict)
        ],
        target_meta=dict(payload.get("target_meta") or {}),
        coordinate_only=bool(payload.get("coordinate_only")),
        replayable=bool(payload.get("replayable", True)),
        risk_type=payload.get("risk_type"),
        blocked_reason=payload.get("blocked_reason"),
        input_rule_id=payload.get("input_rule_id"),
        input_variable_key=payload.get("input_variable_key"),
        action_role=payload.get("action_role"),
        action_role_key=payload.get("action_role_key"),
        action_anchor_key=payload.get("action_anchor_key"),
        action_group_key=payload.get("action_group_key"),
        action_instance_key=payload.get("action_instance_key"),
        sample_policy=str(payload.get("sample_policy") or "ALL"),
    )


def _page_logical_key(page: PageModel) -> str:
    return str(page.semantic_key or page.replay_key or page.state_key or "")


def _work_page_logical_key(work: StateWork) -> str:
    return str(work.semantic_key or work.replay_key or work.state_key or "")


def _work_logical_key(work: StateWork) -> str:
    semantic = _work_page_logical_key(work)
    if work.instance_anchor:
        return f"instance:{work.instance_anchor}:state:{semantic}"
    return semantic


_FAMILY_ACTION_CYCLE_WINDOW = 12
_MAX_CONSECUTIVE_VIEWPORT_HANDOFFS = 2
_PRIMARY_ENTRY_CONTINUATION_PRIORITY = 350
_PROFILE_CONTINUATION_PRIORITY = 150
_OVERLAY_CLEANUP_ACTION_ROLES = frozenset({"FILTER_CLOSE", "DIALOG_CLOSE"})


def _primary_entry_continuation_priority(work: StateWork) -> int:
    """Keep the authenticated account surface from starving behind lists.

    Product/detail and checkout work still use the lower 100 tier.  Profile
    actions are finite and carry the app's orders, settings, benefits,
    favorites, and history surfaces, so they should resume before ordinary
    catalog/store continuations once the first entry survey is complete.
    """
    if str(work.page_subtype or "").upper() == "PROFILE" or str(
        work.role or ""
    ).upper() == "PROFILE":
        return _PROFILE_CONTINUATION_PRIORITY
    return _PRIMARY_ENTRY_CONTINUATION_PRIORITY


def _is_overlay_cleanup_action(action: InspectionAction) -> bool:
    return str(action.action_role or "") in _OVERLAY_CLEANUP_ACTION_ROLES


def _replay_model_expectation(
    model: PageModel,
    *,
    instance_anchor: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "version": 1,
        "package": str(model.package_name or "").casefold(),
        "activity_family": str(model.activity_family or ""),
        "role": str(model.role or ""),
        "instance_anchor": str(
            instance_anchor or derive_instance_anchor(model) or ""
        ),
        "content_anchor": str(derive_instance_anchor(model) or ""),
        "structure_tokens": list(model.template_tokens),
        "action_tokens": list(model.action_tokens),
        "control_tokens": list(model.control_tokens),
        "risk_tokens": list(model.risk_tokens),
    }


def _multiset_token_similarity(left: Sequence[str], right: Sequence[str]) -> float:
    left_counts = Counter(str(item) for item in left)
    right_counts = Counter(str(item) for item in right)
    if not left_counts and not right_counts:
        return 1.0
    intersection = sum((left_counts & right_counts).values())
    union = sum((left_counts | right_counts).values())
    return intersection / union if union else 1.0


def _replay_signature_matches(
    capture: CapturedPage,
    payload: Dict[str, Any],
    *,
    phase: str,
) -> bool:
    expected = payload.get(f"expected_{phase}_signature")
    if not isinstance(expected, dict):
        return False
    model = capture.model
    expected_content_anchor = str(expected.get("content_anchor") or "")
    actual_content_anchor = str(derive_instance_anchor(model) or "")
    expected_instance_anchor = str(expected.get("instance_anchor") or "")
    actual_instance_anchor = actual_content_anchor
    if phase == "target" and expected_instance_anchor:
        source_signature = payload.get("expected_source_signature")
        source_anchor = (
            str(source_signature.get("instance_anchor") or "")
            if isinstance(source_signature, dict)
            else ""
        )
        actual_instance_anchor = derive_instance_anchor(
            model,
            incoming_action=_deserialize_action(payload),
            source_instance_anchor=source_anchor or None,
        )
    return bool(
        expected_content_anchor
        and expected_content_anchor == actual_content_anchor
        and (
            phase != "target"
            or not expected_instance_anchor
            or expected_instance_anchor == actual_instance_anchor
        )
        and str(expected.get("package") or "").casefold()
        == str(model.package_name or "").casefold()
        and str(expected.get("activity_family") or "")
        == str(model.activity_family or "")
        and str(expected.get("role") or "") == str(model.role or "")
        and _multiset_token_similarity(
            expected.get("structure_tokens") or [],
            model.template_tokens,
        )
        >= 0.97
        and tuple(expected.get("action_tokens") or ()) == tuple(model.action_tokens)
        and tuple(expected.get("control_tokens") or ()) == tuple(model.control_tokens)
        and tuple(expected.get("risk_tokens") or ()) == tuple(model.risk_tokens)
    )


def _family_action_pair(
    work: StateWork,
    action: InspectionAction,
    *,
    include_scroll: bool = False,
) -> Optional[Tuple[int, str]]:
    role = str(action.action_role or "")
    if (
        work.exploration_family_id is None
        or not action.action_role_key
        or _is_overlay_cleanup_action(action)
        or role.startswith("INSTANCE:")
        or role.startswith("ITEM_OPEN:")
        or (role.startswith("SCROLL:") and not include_scroll)
    ):
        return None
    return int(work.exploration_family_id), str(action.action_role_key)


def _coverage_contract_identity(
    *,
    branch_run_id: int,
    source_family_id: Optional[int],
    source_page_subtype: str,
    action_group_key: str,
    action_role: Optional[str],
) -> Tuple[str, str]:
    scope = "NAVIGATION" if str(action_role or "").startswith("NAV:") else "FAMILY_ACTION"
    payload = {
        "version": 1,
        "branch_run_id": int(branch_run_id),
        "scope": scope,
        "source_family_id": None if scope == "NAVIGATION" else source_family_id,
        "source_page_subtype": (
            "ANY" if scope == "NAVIGATION" else str(source_page_subtype or "UNKNOWN")
        ),
        "action_group_key": str(action_group_key or ""),
    }
    return (
        hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        scope,
    )


def _family_action_cycle_period(
    trail: Sequence[Tuple[int, str, bool]],
) -> Optional[int]:
    """Return a repeated 2-6 edge period inside the last 12 path transitions."""
    recent = list(trail[-_FAMILY_ACTION_CYCLE_WINDOW:])
    for period in range(2, 7):
        if len(recent) < period * 2:
            continue
        first = recent[-period * 2 : -period]
        second = recent[-period:]
        if [(item[0], item[1]) for item in first] != [
            (item[0], item[1]) for item in second
        ]:
            continue
        if any(bool(item[2]) for item in second):
            continue
        return period
    return None


def _extend_family_action_trail(
    work: StateWork,
    action: InspectionAction,
) -> Tuple[Tuple[int, str, bool], ...]:
    pair = _family_action_pair(work, action, include_scroll=True)
    if pair is None:
        return tuple(work.family_action_trail[-_FAMILY_ACTION_CYCLE_WINDOW:])
    known_pairs = {
        (int(item[0]), str(item[1])) for item in work.family_action_trail
    }
    return tuple(
        (
            *work.family_action_trail,
            (pair[0], pair[1], pair not in known_pairs),
        )[-_FAMILY_ACTION_CYCLE_WINDOW:]
    )


def _validate_replay_expectation(
    capture: CapturedPage,
    payload: Dict[str, Any],
    *,
    phase: str,
    step_index: int,
) -> None:
    expected_key = str(payload.get(f"expected_{phase}_semantic_key") or "")
    if expected_key:
        actual_key = _page_logical_key(capture.model)
        if actual_key != expected_key and not _replay_signature_matches(
            capture,
            payload,
            phase=phase,
        ):
            raise PathDiverged(
                phase=phase,
                expected=expected_key,
                actual=actual_key,
                step_index=step_index,
            )
    if phase != "target":
        return
    expected_role = str(payload.get("expected_target_role") or "")
    if expected_role and str(capture.model.role or "") != expected_role:
        raise PathDiverged(
            phase="target_role",
            expected=expected_role,
            actual=str(capture.model.role or ""),
            step_index=step_index,
        )
    expected_template = str(payload.get("expected_target_template_key") or "")
    if (
        expected_template
        and str(capture.model.template_key or "") != expected_template
        and not _replay_signature_matches(capture, payload, phase=phase)
    ):
        raise PathDiverged(
            phase="target_template",
            expected=expected_template,
            actual=str(capture.model.template_key or ""),
            step_index=step_index,
        )


def _confirm_replay_expectation(
    *,
    device,
    capture: CapturedPage,
    payload: Dict[str, Any],
    phase: str,
    step_index: int,
    package_name: str,
    abort_event: threading.Event,
    dynamic_patterns: Sequence[str],
    stable_wait_seconds: float,
    budget_guard: Optional[BudgetGuard] = None,
) -> CapturedPage:
    """Require two stable mismatches before classifying a replay divergence."""
    try:
        _validate_replay_expectation(
            capture,
            payload,
            phase=phase,
            step_index=step_index,
        )
        return capture
    except PathDiverged:
        _check_abort(abort_event)
        if budget_guard is not None:
            budget_guard.before_device_interaction(
                f"confirm_replay_{phase}_divergence"
            )
        confirmation = _budgeted_wait_for_stable_page(
            device,
            budget_guard=budget_guard,
            expected_package=package_name,
            abort_event=abort_event,
            max_wait_seconds=stable_wait_seconds,
            dynamic_patterns=dynamic_patterns,
        )
        _validate_replay_expectation(
            confirmation,
            payload,
            phase=phase,
            step_index=step_index,
        )
        return confirmation


def _navigation_metadata(action: InspectionAction) -> Dict[str, Any]:
    value = action.target_meta.get("navigation")
    return dict(value) if isinstance(value, dict) else {}


def _is_unambiguous_active_navigation_action(action: InspectionAction) -> bool:
    """Return true only when one navigation member is observably active."""
    navigation = _navigation_metadata(action)
    member_index = navigation.get("member_index")
    active_indices = navigation.get("active_member_indices")
    return bool(
        isinstance(member_index, int)
        and isinstance(active_indices, list)
        and len(active_indices) == 1
        and active_indices[0] == member_index
    )


def _path_score(path: Sequence[Dict[str, Any]]) -> Tuple[int, int, int]:
    input_count = sum(
        1 for item in path if str(item.get("action_type") or "") == "input"
    )
    description_count = sum(
        1
        for item in path
        if any(
            str(candidate.get("by") or "").lower() == "description"
            for candidate in item.get("locator_candidates") or []
            if isinstance(candidate, dict)
        )
    )
    return len(path), input_count, -description_count


def _consecutive_scroll_repetitions(
    path: Sequence[Dict[str, Any]],
    action: InspectionAction,
) -> int:
    """Count this direction/container only within the current scroll chain."""
    count = 0
    for item in reversed(path):
        if str(item.get("action_type") or "") != "scroll":
            break
        if str(item.get("action_key") or "") != action.action_key:
            break
        count += 1
    return count


def _is_viewport_path(path: Sequence[Dict[str, Any]]) -> bool:
    """A path ending in scroll represents a viewport, not a business state."""
    return bool(
        path
        and str(path[-1].get("action_type") or "").strip().lower() == "scroll"
    )


def _navigation_business_owner(
    work: StateWork,
    tracked_work: Dict[int, StateWork],
) -> StateWork:
    """Resolve a scroll viewport to its nearest logical business owner."""
    current = work
    visited: set[int] = set()
    while _is_viewport_path(current.path) and current.state_id not in visited:
        visited.add(current.state_id)
        parent = tracked_work.get(int(current.parent_state_id or 0))
        if parent is None:
            candidates = [
                candidate
                for candidate in tracked_work.values()
                if candidate.state_id not in visited
                and not _is_viewport_path(candidate.path)
                and _path_has_prefix(work.path, candidate.path)
            ]
            if candidates:
                return max(
                    candidates,
                    key=lambda item: (len(item.path), -item.depth, item.state_id),
                )
            break
        current = parent
    return current


def _back_action(depth: int) -> InspectionAction:
    key = hashlib.sha256(f"back:{depth}".encode("utf-8")).hexdigest()
    return InspectionAction(
        action_type="back",
        action_key=key,
        locator_candidates=[],
        target_meta={"depth": depth},
    )


class TransitionBuffer:
    def __init__(
        self,
        run_id: int,
        branch_run_id: int,
        on_append: Optional[Callable[[Dict[str, Any], Optional[int]], None]] = None,
        coverage_scheduler: bool = False,
    ) -> None:
        self.run_id = run_id
        self.branch_run_id = branch_run_id
        self.items: List[Tuple[Dict[str, Any], Optional[int], bool]] = []
        self.on_append = on_append
        self.coverage_scheduler = bool(coverage_scheduler)

    def append(
        self,
        payload: Dict[str, Any],
        target_state_id: Optional[int],
        *,
        assign_incoming: bool = False,
    ) -> None:
        self.items.append((dict(payload), target_state_id, assign_incoming))
        if self.on_append is not None:
            try:
                self.on_append(dict(payload), target_state_id)
            except Exception:
                logger.exception(
                    "inspection transition observer failed: run=%s branch=%s",
                    self.run_id,
                    self.branch_run_id,
                )
        if assign_incoming or len(self.items) >= 10:
            self.flush()

    def flush(self) -> None:
        if not self.items:
            return
        pending = self.items
        self.items = []
        with Session(engine) as session:
            branch = session.get(InspectionBranchRun, self.branch_run_id)
            root_state_id = branch.root_state_id if branch is not None else None
            rows: List[Tuple[InspectionTransition, Optional[int], bool]] = []
            for payload, target_state_id, assign_incoming in pending:
                row = InspectionTransition(
                    run_id=self.run_id,
                    branch_run_id=self.branch_run_id,
                    **payload,
                )
                session.add(row)
                rows.append((row, target_state_id, assign_incoming))
            session.flush()
            for row, state_id, assign_incoming in rows:
                if state_id is None or not assign_incoming:
                    continue
                state = session.get(InspectionState, state_id)
                if state:
                    if state.id == root_state_id:
                        if state.incoming_transition_id is not None:
                            state.incoming_transition_id = None
                            state.updated_at = _now()
                            session.add(state)
                        continue
                    state.incoming_transition_id = row.id
                    state.updated_at = _now()
                    session.add(state)
            for row, _, _ in rows:
                role = str(row.action_role or "")
                if (
                    not row.action_role_key
                    or role.startswith(("INSTANCE:", "ITEM_OPEN:"))
                    or row.status not in {"PASS", "SELF_LOOP"}
                ):
                    continue
                state = session.get(InspectionState, row.from_state_id)
                if state is None or state.exploration_family_id is None:
                    continue
                coverage = session.exec(
                    select(InspectionFamilyActionCoverage).where(
                        InspectionFamilyActionCoverage.family_id
                        == state.exploration_family_id,
                        InspectionFamilyActionCoverage.action_role_key
                        == row.action_role_key,
                    )
                ).first()
                if (
                    coverage is not None
                    and coverage.status == "SUCCESS"
                    and coverage.source_state_id == row.from_state_id
                    and coverage.source_transition_id is None
                ):
                    coverage.source_transition_id = row.id
                    coverage.updated_at = _now()
                    session.add(coverage)
            if self.coverage_scheduler:
                for row, _, _ in rows:
                    if (
                        row.status not in {"PASS", "SELF_LOOP"}
                        or row.to_state_id is None
                        or not row.action_group_key
                        or row.sampling_disposition != "CONTRACT_SAMPLE"
                    ):
                        continue
                    source = session.get(InspectionState, row.from_state_id)
                    target = session.get(InspectionState, row.to_state_id)
                    if source is None or target is None:
                        continue
                    contract_key, scope = _coverage_contract_identity(
                        branch_run_id=self.branch_run_id,
                        source_family_id=source.exploration_family_id,
                        source_page_subtype=source.page_subtype,
                        action_group_key=row.action_group_key,
                        action_role=row.action_role,
                    )
                    contract = session.exec(
                        select(InspectionCoverageContract).where(
                            InspectionCoverageContract.branch_run_id
                            == self.branch_run_id,
                            InspectionCoverageContract.contract_key == contract_key,
                        )
                    ).first()
                    target_template = (
                        session.get(InspectionPageTemplate, target.template_id)
                        if target.template_id is not None
                        else None
                    )
                    target_role = str(
                        target_template.page_role
                        if target_template is not None
                        else "UNKNOWN"
                    )
                    risk_signature = str(row.risk_type or "SAFE")
                    control_signature = json.dumps(
                        {
                            key: (row.target_meta or {}).get(key)
                            for key in ("enabled", "checked", "selected")
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    required_samples = (
                        1
                        if scope == "NAVIGATION"
                        or str(source.page_subtype or "")
                        in {"CONSUMABLE_LIST", "PRODUCT_LIST", "SERVICE_LIST", "STORE_LIST"}
                        else 2
                    )
                    if contract is None:
                        contract = InspectionCoverageContract(
                            run_id=self.run_id,
                            branch_run_id=self.branch_run_id,
                            contract_key=contract_key,
                            scope=scope,
                            source_family_id=(
                                None
                                if scope == "NAVIGATION"
                                else source.exploration_family_id
                            ),
                            source_page_subtype=str(
                                source.page_subtype or "UNKNOWN"
                            ),
                            action_group_key=str(row.action_group_key),
                            action_role=row.action_role,
                            target_family_id=target.exploration_family_id,
                            target_page_role=target_role,
                            status="PENDING",
                            required_samples=required_samples,
                            risk_signature=risk_signature,
                            control_signature=control_signature,
                            created_at=_now(),
                        )
                    conflict = bool(
                        (
                            contract.target_family_id is not None
                            and target.exploration_family_id is not None
                            and int(contract.target_family_id)
                            != int(target.exploration_family_id)
                        )
                        or (
                            contract.target_page_role
                            and contract.target_page_role != target_role
                        )
                        or (
                            contract.risk_signature
                            and contract.risk_signature != risk_signature
                        )
                        or (
                            contract.control_signature
                            and contract.control_signature != control_signature
                        )
                    )
                    anchors = list(contract.source_instance_anchors or [])
                    transition_ids = list(contract.sample_transition_ids or [])
                    source_anchor = str(source.instance_anchor or "")
                    if conflict:
                        contract.status = "CONFLICT"
                        contract.failure_count = int(contract.failure_count or 0) + 1
                        contract.last_error = "目标页面族、角色、风险或控件状态不一致"
                    elif source_anchor not in anchors:
                        anchors.append(source_anchor)
                        contract.success_count = int(contract.success_count or 0) + 1
                        contract.status = (
                            "VERIFIED"
                            if int(contract.success_count) >= int(contract.required_samples)
                            else "PROVISIONAL"
                        )
                    if row.id is not None and int(row.id) not in transition_ids:
                        transition_ids.append(int(row.id))
                    contract.source_instance_anchors = anchors
                    contract.sample_transition_ids = transition_ids
                    contract.updated_at = _now()
                    session.add(contract)
                    session.flush()
                    row.coverage_contract_id = contract.id
                    session.add(row)
            session.commit()


def _transition_payload(
    *,
    from_state_id: int,
    sequence: int,
    action: InspectionAction,
    status: str,
    to_state_id: Optional[int] = None,
    reason: Optional[str] = None,
    duration_ms: float = 0.0,
    used_locator: Optional[str] = None,
    input_length: Optional[int] = None,
    error_message: Optional[str] = None,
    relation_type: Optional[str] = None,
    relation_confidence: Optional[float] = None,
    topology_type: Optional[str] = None,
    source_observation_id: Optional[int] = None,
    target_observation_id: Optional[int] = None,
    traversal_count: int = 1,
    target_was_existing: bool = False,
    execution_disposition: Optional[str] = None,
    failure_type: Optional[str] = None,
    coverage_source_transition_id: Optional[int] = None,
    coverage_contract_id: Optional[int] = None,
    sampling_disposition: Optional[str] = None,
    recovery_attempt_count: int = 0,
) -> Dict[str, Any]:
    target_meta = dict(action.target_meta)
    if used_locator:
        target_meta["used_locator"] = used_locator
    normalized_status = str(status or "").upper()
    effective_disposition = (
        str(execution_disposition)
        if execution_disposition is not None
        else "SKIPPED"
        if normalized_status == "BLOCKED"
        else "EXECUTED"
    )
    effective_failure_type = (
        failure_type
        if failure_type is not None
        else "SAFETY_BLOCKED"
        if normalized_status == "BLOCKED"
        else None
    )
    return {
        "from_state_id": from_state_id,
        "to_state_id": to_state_id,
        "sequence": sequence,
        "action_type": action.action_type,
        "action_key": action.action_key,
        "locator_candidates": [dict(item) for item in action.locator_candidates],
        "target_meta": target_meta,
        "status": status,
        "risk_type": action.risk_type,
        "reason": reason,
        "coordinate_only": action.coordinate_only,
        "replayable": bool(
            action.replayable
            and status not in {"AMBIGUOUS", "LOCATOR_AMBIGUOUS", "BLOCKED"}
        ),
        "duration_ms": duration_ms,
        "input_rule_id": action.input_rule_id,
        "input_variable_key": action.input_variable_key,
        "input_length": input_length,
        "relation_type": relation_type,
        "relation_confidence": relation_confidence,
        "topology_type": topology_type,
        "action_role_key": action.action_role_key,
        "action_role": action.action_role,
        "execution_disposition": effective_disposition,
        "failure_type": effective_failure_type,
        "coverage_source_transition_id": coverage_source_transition_id,
        "coverage_contract_id": coverage_contract_id,
        "action_group_key": action.action_group_key,
        "sampling_disposition": sampling_disposition,
        "visual_locator_evidence": dict(
            (action.target_meta or {}).get("visual_locator") or {}
        ),
        "recovery_attempt_count": max(0, int(recovery_attempt_count or 0)),
        "source_observation_id": source_observation_id,
        "target_observation_id": target_observation_id,
        "traversal_count": max(1, int(traversal_count or 1)),
        "target_was_existing": bool(target_was_existing),
        # Driver failures frequently include full XPath/selector text or
        # user-entered values. Detailed diagnostics belong in redacted fault
        # logs, not browser-facing transition rows.
        "error_message": "动作执行异常" if error_message else None,
    }


def _run_case(
    *,
    case_id: int,
    device_serial: str,
    env_id: Optional[int],
    abort_event: threading.Event,
    budget_guard: Optional[BudgetGuard] = None,
) -> bool:
    _check_abort(abort_event)
    if budget_guard is not None:
        budget_guard.check_deadline()

    def reserve_device_step(action: str) -> None:
        if budget_guard is not None:
            budget_guard.before_device_interaction(
                f"case_step:{str(action or 'unknown')}",
                mutating=True,
            )

    with Session(engine) as session:
        case = session.get(TestCase, int(case_id))
        if case is None:
            raise RuntimeError(f"巡检用例不存在: {case_id}")
        result = run_case_with_standard_runner(
            session=session,
            case=case,
            device_serial=device_serial,
            env_id=env_id,
            abort_event=abort_event,
            before_device_step=reserve_device_step,
        )
    if budget_guard is not None:
        budget_guard.check_deadline()
    return bool(result.get("success"))


def _try_run_case(**kwargs) -> bool:
    try:
        return _run_case(**kwargs)
    except InspectionAborted:
        raise
    except BudgetExceeded:
        raise
    except Exception as exc:
        logger.warning(
            "inspection case failed: case=%s error=%s",
            kwargs.get("case_id"),
            _safe_error(exc),
        )
        return False


def _prepare_branch(
    *,
    device,
    branch_config: Dict[str, Any],
    device_serial: str,
    abort_event: threading.Event,
    stage_callback: Optional[Callable[[str, str], None]] = None,
    budget_guard: Optional[BudgetGuard] = None,
) -> None:
    entry_case_id = int(branch_config.get("entry_case_id") or 0)
    prepare_case_id = int(branch_config.get("prepare_case_id") or 0)
    env_id = branch_config.get("env_id")
    assertion = dict(branch_config.get("ready_assertion") or {})

    if stage_callback:
        stage_callback("entry", "执行进入用例")
    _try_run_case(
        case_id=entry_case_id,
        device_serial=device_serial,
        env_id=env_id,
        abort_event=abort_event,
        budget_guard=budget_guard,
    )
    if budget_guard is not None:
        budget_guard.before_device_interaction("ready_assertion")
    ready = ready_assertion_exists(device, assertion, abort_event=abort_event)
    if budget_guard is not None:
        budget_guard.check_deadline()
    if ready:
        return

    if stage_callback:
        stage_callback("prepare", "执行准备用例")
    prepare_ok = _try_run_case(
        case_id=prepare_case_id,
        device_serial=device_serial,
        env_id=env_id,
        abort_event=abort_event,
        budget_guard=budget_guard,
    )
    if stage_callback:
        stage_callback("entry", "重新执行进入用例")
    entry_ok = _try_run_case(
        case_id=entry_case_id,
        device_serial=device_serial,
        env_id=env_id,
        abort_event=abort_event,
        budget_guard=budget_guard,
    )
    if budget_guard is not None:
        budget_guard.before_device_interaction("ready_assertion")
    ready = ready_assertion_exists(device, assertion, abort_event=abort_event)
    if budget_guard is not None:
        budget_guard.check_deadline()
    if ready:
        # prepare may fail when the device already had the desired state.
        return
    raise BranchPreparationFailed(
        "业务线未达到就绪状态 "
        f"(prepare={'PASS' if prepare_ok else 'FAIL'}, "
        f"entry={'PASS' if entry_ok else 'FAIL'})"
    )


def _input_rule(
    input_rules: Sequence[Dict[str, Any]],
    rule_id: Optional[str],
) -> Optional[Dict[str, Any]]:
    for rule in input_rules:
        if str(rule.get("id") or "") == str(rule_id or ""):
            return dict(rule)
    return None


def _environment_secret_values(env_id: Optional[int]) -> List[str]:
    """Preload branch secrets for artifact/log redaction without persisting them.

    Entry and prepare cases may use credentials before the exploration engine
    executes its first input action.  Loading the secret values up front keeps
    Crash/ANR logs raised during branch preparation covered by the same
    inspection-only redaction boundary.
    """
    if env_id is None:
        return []
    try:
        normalized_env_id = int(env_id)
    except (TypeError, ValueError):
        return []
    with Session(engine) as session:
        rows = session.exec(
            select(GlobalVariable).where(
                GlobalVariable.env_id == normalized_env_id,
                GlobalVariable.is_secret == True,  # noqa: E712
            )
        ).all()
    values: List[str] = []
    for row in rows:
        value = str(row.value or "")
        if value and value not in values:
            values.append(value)
    return values


def _resolve_input_value(
    *,
    action: InspectionAction,
    input_rules: Sequence[Dict[str, Any]],
    env_id: Optional[int],
    secret_values: List[str],
) -> Tuple[str, Optional[str], int]:
    rule = _input_rule(input_rules, action.input_rule_id)
    if rule is None:
        raise PermissionError("输入动作缺少允许规则")
    source = str(rule.get("value_source") or "literal").lower()
    password = bool(action.target_meta.get("password"))
    allow_sensitive = bool(rule.get("allow_sensitive"))
    variable_key: Optional[str] = None
    is_secret = False

    if source == "environment":
        variable_key = str(rule.get("variable_key") or "").strip()
        if not variable_key or env_id is None:
            raise PermissionError("环境变量输入缺少 env_id 或变量键")
        with Session(engine) as session:
            variable = session.exec(
                select(GlobalVariable).where(
                    GlobalVariable.env_id == int(env_id),
                    GlobalVariable.key == variable_key,
                )
            ).first()
            if variable is None:
                raise PermissionError(f"环境变量不存在: {variable_key}")
            value = str(variable.value or "")
            is_secret = bool(variable.is_secret)
    else:
        value = str(rule.get("value") or "")

    if password and (source != "environment" or not is_secret or not allow_sensitive):
        value = ""
        raise PermissionError("密码输入必须显式允许并使用 is_secret=true 的环境变量")
    if is_secret and value:
        secret_values.append(value)
    return value, variable_key, len(value)


def _redact(value: str, secret_values: Iterable[str]) -> str:
    result = str(value or "")
    for secret in secret_values:
        if secret:
            result = result.replace(secret, "***")
    return result


def _redact_payload(value: Any, secret_values: Sequence[str]) -> Any:
    try:
        serialized = json.dumps(value, ensure_ascii=False)
        return json.loads(_redact(serialized, secret_values))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _capture_dropbox(serial: str) -> str:
    try:
        completed = subprocess.run(
            [
                "adb",
                "-s",
                serial,
                "shell",
                "dumpsys",
                "dropbox",
                "--print",
                "data_app_crash",
                "data_app_anr",
                "system_app_crash",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=8,
        )
        return completed.stdout.decode("utf-8", errors="replace")[-250_000:]
    except Exception:
        return ""


def _run_async_blocking(factory):
    """Run an async package operation from either a worker or event-loop thread."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(factory())

    result: List[Any] = []
    errors: List[BaseException] = []

    def runner() -> None:
        try:
            result.append(asyncio.run(factory()))
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=runner, name="inspection-async-bridge")
    worker.start()
    worker.join()
    if errors:
        raise errors[0]
    return result[0] if result else None


def _install_requested_package(package_id: int, device_serial: str) -> None:
    from backend.api.packages import install_app_package_to_device

    async def install():
        with Session(engine) as session:
            return await install_app_package_to_device(
                session=session,
                package_id=package_id,
                serial=device_serial,
                require_idle=False,
                uninstall_first=False,
                allow_uninstall_retry=True,
                allow_downgrade=True,
            )

    _run_async_blocking(install)


def _validated_fault_artifact(
    path_value: Any,
    *,
    run_id: int,
) -> Optional[str]:
    normalized = str(path_value or "").strip()
    if not normalized:
        return None
    # The shared scrcpy recorder returns paths relative to PROJECT_ROOT
    # (``reports/...``), while inspection assets are stored relative to the
    # reports root (``inspection/...``). Canonicalize only this exact leading
    # component before applying the normal run containment checks.
    raw_relative = Path(normalized)
    if (
        not raw_relative.is_absolute()
        and raw_relative.parts
        and raw_relative.parts[0] == "reports"
    ):
        raw_relative = Path(*raw_relative.parts[1:])
        normalized = raw_relative.as_posix()
    try:
        target = resolve_inspection_asset(normalized, run_id=run_id)
    except ValueError:
        return None
    if not target.is_file():
        return None
    return _relative_report_path(target)


def _validated_replay_artifact(
    replay: Dict[str, Any],
    *,
    run_id: int,
) -> Optional[str]:
    validated = _validated_fault_artifact(replay.get("path"), run_id=run_id)
    if validated:
        return validated
    # A prior validation attempt may have blanked the path even though the
    # recorder completed the MP4. The basename is safe to recover only from
    # this run's fixed monitor/replays directory.
    filename = str(replay.get("filename") or "").strip()
    if not filename or Path(filename).name != filename:
        return None
    return _validated_fault_artifact(
        f"inspection/{int(run_id)}/monitor/replays/{filename}",
        run_id=run_id,
    )


def _fault_event_with_replay(
    monitor: Optional[InspectionMonitorSession],
    fault_type: str,
    *,
    full_log: str = "",
    budget_guard: Optional[BudgetGuard] = None,
) -> Dict[str, Any]:
    event: Dict[str, Any] = {
        "time": datetime.now().strftime("%H:%M:%S"),
    }
    if full_log:
        event["full_log"] = full_log
    if monitor:
        try:
            if budget_guard is not None:
                budget_guard.before_device_interaction(
                    "capture_fault_replay",
                    mutating=True,
                )
            replay = monitor.capture_replay(fault_type)
            if budget_guard is not None:
                budget_guard.check_deadline()
        except BudgetExceeded:
            replay = None
        if replay:
            event["replay"] = replay
    return event


def _persist_fault_assets(fault_id: int) -> None:
    """Pin every available fault artifact without making fault capture brittle."""
    try:
        from backend.artifact_store import (
            RETENTION_PINNED,
            content_addressed_assets_enabled,
            store_file,
            upsert_reference,
        )

        with Session(engine) as session:
            row = session.get(InspectionFault, int(fault_id))
            if row is None or not content_addressed_assets_enabled(session):
                return
            for role, path_value in (
                ("full_log", row.full_log_path),
                ("screenshot", row.screenshot_path),
                ("xml", row.xml_path),
                ("replay", row.replay_path),
                ("trace", row.trace_path),
            ):
                if not path_value:
                    continue
                source = resolve_inspection_asset(path_value, run_id=row.run_id)
                if not source.is_file():
                    continue
                asset = store_file(session, source, commit=False)
                upsert_reference(
                    session,
                    asset_id=asset.id,
                    owner_type="inspection_fault",
                    owner_id=int(row.id),
                    role=role,
                    retention_class=RETENTION_PINNED,
                    pinned_reason=f"inspection fault evidence {row.id}",
                    commit=False,
                )
            session.commit()
    except Exception:
        logger.exception("inspection fault CAS persistence degraded: fault=%s", fault_id)


def _persist_fault(
    *,
    run_id: int,
    branch_run_id: Optional[int],
    state_id: Optional[int],
    fault_type: str,
    summary: str,
    event: Optional[Dict[str, Any]],
    recent_actions: Sequence[Dict[str, Any]],
    secret_values: Sequence[str],
    device_serial: str,
    budget_guard: Optional[BudgetGuard] = None,
) -> None:
    event = dict(event or {})
    full_log = _redact(str(event.get("full_log") or ""), secret_values)
    replay = (
        dict(event.get("replay") or {})
        if isinstance(event.get("replay"), dict)
        else None
    )
    replay_path = None
    if replay is not None:
        replay_path = _validated_replay_artifact(
            replay,
            run_id=run_id,
        )
        replay["path"] = replay_path or ""
        replay["error"] = _redact(str(replay.get("error") or ""), secret_values)
        if str(replay.get("status") or "").upper() == "READY" and not replay_path:
            replay["status"] = "FAILED"
            replay["error"] = "回放产物路径无效或文件不存在"
    trace_path = _validated_fault_artifact(event.get("trace_path"), run_id=run_id)
    dropbox = ""
    if fault_type in {"CRASH", "ANR"}:
        try:
            if budget_guard is not None:
                budget_guard.before_device_interaction("capture_dropbox")
            dropbox = _redact(_capture_dropbox(device_serial), secret_values)
            if budget_guard is not None:
                budget_guard.check_deadline()
        except BudgetExceeded:
            dropbox = ""
    signature_source = f"{fault_type}|{summary}|{full_log[:1000]}"
    signature = hashlib.sha256(signature_source.encode("utf-8")).hexdigest()

    with Session(engine) as session:
        existing = session.exec(
            select(InspectionFault).where(
                InspectionFault.run_id == run_id,
                InspectionFault.signature == signature,
            )
        ).first()
        if existing:
            existing.occurrence_count += 1
            existing.updated_at = _now()
            session.add(existing)
            session.commit()
            return
        if budget_guard is not None:
            reserved_bytes = len(full_log.encode("utf-8")) + len(
                dropbox.encode("utf-8")
            )
            for artifact_path in (replay_path, trace_path):
                if not artifact_path:
                    continue
                try:
                    reserved_bytes += resolve_inspection_asset(
                        artifact_path,
                        run_id=run_id,
                    ).stat().st_size
                except (OSError, ValueError):
                    continue
            try:
                budget_guard.reserve_fault_artifact(reserved_bytes)
            except BudgetExceeded:
                full_log = ""
                dropbox = ""
        state = session.get(InspectionState, state_id) if state_id else None
        run = session.get(InspectionRun, run_id)
        device = session.exec(
            select(Device).where(Device.serial == device_serial)
        ).first()
        package = (
            session.get(AppPackage, run.package_id)
            if run is not None and run.package_id is not None
            else None
        )
        safe_recent_actions = (
            _redact_payload(list(recent_actions)[-20:], list(secret_values)) or []
        )
        safe_shortest_path = (
            _redact_payload(
                list(state.first_path or []) if state else [],
                list(secret_values),
            )
            or []
        )
        row = InspectionFault(
            run_id=run_id,
            branch_run_id=branch_run_id,
            state_id=state_id,
            fault_type=fault_type,
            signature=signature,
            summary=_redact(summary, secret_values)[:1000],
            screenshot_path=state.screenshot_path if state else None,
            xml_path=state.xml_path if state else None,
            replay_path=replay_path,
            trace_path=trace_path,
            details={
                "time": event.get("time"),
                "last_actions": safe_recent_actions,
                "current_action": (
                    safe_recent_actions[-1] if safe_recent_actions else None
                ),
                "shortest_path": safe_shortest_path,
                "dropbox_available": bool(dropbox),
                "replay": replay,
                "activity": state.activity if state else None,
                "device": {
                    "serial": device_serial,
                    "model": device.model if device else None,
                    "brand": device.brand if device else None,
                    "os_version": (
                        device.os_version or device.android_version
                        if device
                        else None
                    ),
                    "resolution": device.resolution if device else None,
                },
                "version": {
                    "package_name": run.package_name if run else None,
                    "version_name": package.version_name if package else None,
                    "version_code": package.version_code if package else None,
                },
            },
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        fault_id = row.id

    fault_dir = _safe_report_path(
        _reports_root() / "inspection" / str(run_id) / "faults" / str(fault_id)
    )
    fault_dir.mkdir(parents=True, exist_ok=True)
    chunks = []
    if full_log:
        chunks.append(full_log)
    if dropbox:
        chunks.append("\n\n===== DROPBOX / ANR =====\n" + dropbox)
    log_path = None
    if chunks:
        target = _safe_report_path(fault_dir / "device.log")
        target.write_text("".join(chunks), encoding="utf-8")
        log_path = _relative_report_path(target)
    with Session(engine) as session:
        row = session.get(InspectionFault, fault_id)
        if row:
            row.full_log_path = log_path
            row.updated_at = _now()
            session.add(row)
            session.commit()
    _persist_fault_assets(int(fault_id))


def _state_actions(
    capture: CapturedPage,
    *,
    screen_size: Tuple[int, int],
    safety_rules: Sequence[Dict[str, Any]],
    input_rules: Sequence[Dict[str, Any]],
    max_scrolls: int,
    depth: int,
    screenshot_png: bytes = b"",
    enable_visual_home_actions: bool = False,
    coverage_scheduler: bool = False,
) -> List[InspectionAction]:
    actions = enumerate_actions(
        capture.model,
        screen_size=screen_size,
        screenshot_png=screenshot_png,
        enable_visual_home_actions=enable_visual_home_actions,
        coverage_scheduler_v2=coverage_scheduler,
        safety_rules=safety_rules,
        input_rules=input_rules,
        max_scrolls_per_direction=max_scrolls,
    )
    # Business actions on the current viewport are explored before scrolling.
    # On HOME, cover the shared bottom navigation before page-specific peer
    # destinations such as nearby stores, so child pages can reuse every Tab.
    def action_order(item: InspectionAction) -> int:
        role = str(item.action_role or "")
        if role in {"BUY_NOW", "CHECKOUT", "PLACE_ORDER", "FILTER_CLOSE"}:
            return 0
        if role == "ADD_CART":
            return 5
        if item.action_type == "scroll":
            return 70
        if item.action_type == "back" or role == "BACK":
            return 90
        if str(item.action_role or "").startswith("VISUAL_HOME:"):
            return 60
        navigation = _navigation_metadata(item)
        if navigation:
            if (
                capture.model.page_subtype == "HOME"
                and navigation.get("group_region") == "bottom"
            ):
                return 1
            return 20 if navigation.get("group_region") == "bottom" else 30
        return 10

    actions = sorted(actions, key=action_order)
    has_dismissible_overlay = any(
        node.visible
        and "dialog" in node.class_name.lower()
        for node in capture.model.nodes
    )
    if has_dismissible_overlay:
        actions.append(_back_action(depth))
    return actions


def _profile_bool(profile: Dict[str, Any], *keys: str) -> Optional[bool]:
    for key in keys:
        if key not in profile:
            continue
        value = profile.get(key)
        if isinstance(value, bool):
            return value
        normalized = str(value or "").strip().lower()
        if normalized in {"1", "true", "yes", "on", "enabled"}:
            return True
        if normalized in {"0", "false", "no", "off", "disabled"}:
            return False
    return None


def _identity_options(profile: Dict[str, Any]) -> Tuple[bool, bool]:
    identity = _profile_bool(
        profile,
        "inspection_identity_v2",
        "identity_v2",
        "inspection_tso_v2",
    )
    convergence = _profile_bool(
        profile,
        "inspection_similarity_convergence",
        "similarity_convergence",
    )
    if identity is not None and convergence is not None:
        return identity, convergence
    try:
        with Session(engine) as session:
            if identity is None:
                identity = bool(
                    is_flag_enabled(session, FLAG_INSPECTION_TSO_V2)
                    or is_flag_enabled(
                        session,
                        "inspection_identity_v2",
                        default=False,
                    )
                )
            if convergence is None:
                convergence = is_flag_enabled(
                    session,
                    FLAG_INSPECTION_SIMILARITY_CONVERGENCE,
                )
    except Exception:
        identity = bool(identity)
        convergence = bool(convergence)
    return bool(identity), bool(convergence)


def _family_convergence_option(profile: Dict[str, Any]) -> bool:
    configured = _profile_bool(
        profile,
        "inspection_exploration_family_convergence",
        "exploration_family_convergence",
    )
    if configured is not None:
        return bool(configured)
    try:
        with Session(engine) as session:
            return is_flag_enabled(
                session,
                FLAG_INSPECTION_EXPLORATION_FAMILY_CONVERGENCE,
            )
    except Exception:
        return False


def _coverage_scheduler_options(profile: Dict[str, Any]) -> Tuple[bool, bool]:
    scheduler = _profile_bool(
        profile,
        "inspection_coverage_scheduler_v2",
        "coverage_scheduler_v2",
    )
    visual = _profile_bool(
        profile,
        "inspection_visual_home_actions",
        "visual_home_actions",
    )
    try:
        with Session(engine) as session:
            if scheduler is None:
                scheduler = is_flag_enabled(
                    session,
                    FLAG_INSPECTION_COVERAGE_SCHEDULER_V2,
                )
            if visual is None:
                visual = is_flag_enabled(
                    session,
                    FLAG_INSPECTION_VISUAL_HOME_ACTIONS,
                )
    except Exception:
        scheduler = bool(scheduler)
        visual = bool(visual)
    return bool(scheduler), bool(scheduler and visual)


def _thumbnail_bytes(png_bytes: bytes) -> bytes:
    output = io.BytesIO()
    with Image.open(io.BytesIO(png_bytes)) as thumbnail:
        thumbnail = thumbnail.convert("RGB")
        thumbnail.thumbnail((240, 240), Image.Resampling.LANCZOS)
        thumbnail.save(output, format="JPEG", quality=78, optimize=True)
    return output.getvalue()


def _observation_candidate_model(
    session: Session,
    state: InspectionState,
) -> Optional[PageModel]:
    xml = ""
    observation_id = getattr(state, "representative_observation_id", None)
    if observation_id:
        observation = session.get(InspectionObservation, observation_id)
        asset_id = getattr(observation, "xml_asset_id", None) if observation else None
        if asset_id:
            try:
                from backend.artifact_store import read_asset

                xml = read_asset(session, asset_id).body.decode("utf-8")
            except Exception:
                xml = ""
    if not xml and state.xml_path:
        try:
            xml = resolve_inspection_asset(state.xml_path, run_id=state.run_id).read_text(
                encoding="utf-8"
            )
        except (OSError, ValueError, UnicodeDecodeError):
            xml = ""
    if not xml:
        return None
    try:
        return build_page_model(
            xml,
            package_name=str(state.foreground_package or ""),
            activity=str(state.activity or ""),
            screenshot_phash=str(state.perceptual_hash or ""),
        )
    except ValueError:
        return None


def _best_similarity_candidate(
    session: Session,
    *,
    branch_run_id: int,
    capture: CapturedPage,
    instance_anchor: str,
) -> Tuple[Optional[InspectionState], Dict[str, Any]]:
    compatible_templates = session.exec(
        select(InspectionPageTemplate).where(
            InspectionPageTemplate.package_name == capture.package_name,
            InspectionPageTemplate.fingerprint_version == 2,
            InspectionPageTemplate.activity_family == capture.model.activity_family,
            InspectionPageTemplate.page_role == capture.model.role,
        )
    ).all()
    template_ids = [item.id for item in compatible_templates if item.id is not None]
    if not template_ids:
        return None, {}
    candidates = session.exec(
        select(InspectionState).where(
            InspectionState.branch_run_id == branch_run_id,
            InspectionState.template_id.in_(template_ids),
            InspectionState.instance_anchor == instance_anchor,
        )
    ).all()
    best_state: Optional[InspectionState] = None
    best_payload: Dict[str, Any] = {}
    best_score = -1.0
    for candidate in candidates:
        model = _observation_candidate_model(session, candidate)
        if model is None:
            continue
        similarity = compare_page_models(capture.model, model)
        if similarity.score <= best_score:
            continue
        best_score = similarity.score
        best_state = candidate
        best_payload = similarity.to_dict()
        best_payload["candidate_state_id"] = candidate.id
    return best_state, best_payload


def _match_or_create_exploration_family(
    session: Session,
    *,
    run_id: int,
    branch_run_id: int,
    capture: CapturedPage,
    screen_size: Tuple[int, int],
) -> Tuple[InspectionExplorationFamily, Dict[str, Any], bool]:
    signature = exploration_family_signature(
        capture.model,
        screen_size=screen_size,
    )
    family_key = str(signature["family_key"])
    exact = session.exec(
        select(InspectionExplorationFamily).where(
            InspectionExplorationFamily.branch_run_id == branch_run_id,
            InspectionExplorationFamily.fingerprint_version == 2,
            InspectionExplorationFamily.family_key == family_key,
        )
    ).first()
    if exact is not None:
        return exact, {"match_type": "EXACT_FAMILY", "score": 1.0}, False

    candidates = session.exec(
        select(InspectionExplorationFamily).where(
            InspectionExplorationFamily.branch_run_id == branch_run_id,
            InspectionExplorationFamily.fingerprint_version == 2,
            InspectionExplorationFamily.page_role == capture.model.role,
            InspectionExplorationFamily.activity_family
            == capture.model.activity_family,
        )
    ).all()
    best_family: Optional[InspectionExplorationFamily] = None
    best_evidence: Dict[str, Any] = {}
    best_score = -1.0
    for candidate in candidates:
        prototypes = session.exec(
            select(InspectionState)
            .where(InspectionState.exploration_family_id == candidate.id)
            .order_by(col(InspectionState.id).asc())
            .limit(3)
        ).all()
        representative = (
            session.get(InspectionState, candidate.representative_state_id)
            if candidate.representative_state_id is not None
            else None
        )
        if representative is not None and all(
            item.id != representative.id for item in prototypes
        ):
            prototypes.insert(0, representative)
        for prototype in prototypes:
            prototype_model = _observation_candidate_model(session, prototype)
            if prototype_model is None:
                continue
            similarity = compare_exploration_families(
                prototype_model,
                capture.model,
                right_screen_size=screen_size,
            )
            if not similarity.equivalent or similarity.score <= best_score:
                continue
            best_family = candidate
            best_score = similarity.score
            best_evidence = {
                "match_type": "SIMILAR_FAMILY",
                "prototype_state_id": prototype.id,
                **similarity.to_dict(),
            }
    if best_family is not None:
        return best_family, best_evidence, False

    family = InspectionExplorationFamily(
        run_id=run_id,
        branch_run_id=branch_run_id,
        family_key=family_key,
        fingerprint_version=2,
        page_role=capture.model.role,
        activity_family=capture.model.activity_family,
        signature=signature,
        member_count=0,
        created_at=_now(),
    )
    session.add(family)
    session.flush()
    return family, {"match_type": "NEW_FAMILY", "score": None}, True


def _persist_observation_assets(
    session: Session,
    *,
    observation: InspectionObservation,
    screenshot_png: bytes,
    xml: str,
    thumbnail_jpeg: bytes,
    action_map: Dict[str, Any],
) -> None:
    try:
        from backend.artifact_store import (
            content_addressed_assets_enabled,
            store_image_bytes,
            store_json,
            store_text_bytes,
            upsert_reference,
        )

        if not content_addressed_assets_enabled(session):
            observation.asset_status = "LEGACY"
            observation.metadata_only = True
            session.add(observation)
            return

        screenshot_asset = store_image_bytes(session, screenshot_png, commit=False)
        xml_asset = store_text_bytes(
            session,
            xml.encode("utf-8"),
            media_type="application/xml",
            suffix="xml",
            commit=False,
        )
        thumbnail_asset = store_image_bytes(session, thumbnail_jpeg, commit=False)
        action_map_asset = store_json(session, action_map, commit=False)
        observation.screenshot_asset_id = screenshot_asset.id
        observation.xml_asset_id = xml_asset.id
        observation.thumbnail_asset_id = thumbnail_asset.id
        observation.action_map_asset_id = action_map_asset.id
        observation.asset_status = "AVAILABLE"
        session.add(observation)
        session.flush()
        retention_class = "HOT"
        for role, asset_id in (
            ("screenshot", screenshot_asset.id),
            ("xml", xml_asset.id),
            ("thumbnail", thumbnail_asset.id),
            ("action_map", action_map_asset.id),
        ):
            upsert_reference(
                session,
                asset_id=asset_id,
                owner_type="inspection_observation",
                owner_id=int(observation.id),
                role=role,
                retention_class=retention_class,
                commit=False,
            )
    except Exception as exc:
        logger.warning(
            "inspection observation asset persistence degraded: observation=%s error=%s",
            observation.id,
            _safe_error(exc),
        )
        observation.asset_status = "UNAVAILABLE"
        observation.metadata_only = True
        session.add(observation)


def _pin_observation_assets(
    observation_id: Optional[int],
    *,
    reason: str = "REGRESSION",
) -> None:
    if observation_id is None:
        return
    try:
        from backend.artifact_store import upsert_reference

        with Session(engine) as session:
            observation = session.get(InspectionObservation, int(observation_id))
            if observation is None:
                return
            for role, asset_id in (
                ("screenshot", observation.screenshot_asset_id),
                ("xml", observation.xml_asset_id),
                ("thumbnail", observation.thumbnail_asset_id),
                ("action_map", observation.action_map_asset_id),
            ):
                if not asset_id:
                    continue
                upsert_reference(
                    session,
                    asset_id=asset_id,
                    owner_type="inspection_regression",
                    owner_id=int(observation.state_id),
                    role=role,
                    retention_class="PINNED",
                    pinned_reason=reason,
                    commit=False,
                )
            session.commit()
    except Exception as exc:
        logger.warning(
            "inspection observation pin degraded: observation=%s error=%s",
            observation_id,
            _safe_error(exc),
        )


def _persist_state(
    *,
    run_id: int,
    branch_run: InspectionBranchRun,
    capture: CapturedPage,
    depth: int,
    parent_state_id: Optional[int],
    path: List[Dict[str, Any]],
    sanitizer: InspectionArtifactSanitizer,
    screen_size: Tuple[int, int],
    safety_rules: Sequence[Dict[str, Any]],
    input_rules: Sequence[Dict[str, Any]],
    max_scrolls: int,
    max_variants: int,
    secret_values: Sequence[str] = (),
    prefer_hierarchy: bool = False,
    mark_branch_root: bool = False,
    identity_v2: bool = False,
    similarity_convergence: bool = False,
    family_convergence: bool = False,
    coverage_scheduler: bool = False,
    visual_home_actions: bool = False,
    budget_guard: Optional[BudgetGuard] = None,
    ancestry_state_ids: Sequence[int] = (),
    capture_kind: str = "DISCOVERY",
    instance_anchor_override: Optional[str] = None,
    preferred_state_id: Optional[int] = None,
    preferred_match_type: Optional[str] = None,
) -> PersistedState:
    if mark_branch_root and (depth != 0 or parent_state_id is not None):
        raise ValueError("inspection branch root must use depth=0 and no parent")
    is_viewport = _is_viewport_path(path)
    effective_screen_size = _capture_screen_size(capture, screen_size)
    incoming_action = (
        _deserialize_action(dict(path[-1]))
        if path and isinstance(path[-1], dict)
        else None
    )
    actions = _state_actions(
        capture,
        screen_size=effective_screen_size,
        safety_rules=safety_rules,
        input_rules=input_rules,
        max_scrolls=max_scrolls,
        depth=depth,
        screenshot_png=capture.screenshot_png,
        enable_visual_home_actions=visual_home_actions,
        coverage_scheduler=coverage_scheduler,
    )
    action_keys = {item.action_key for item in actions}
    recovery_navigation_actions = [
        item
        for item in enumerate_actions(
            capture.model,
            screen_size=effective_screen_size,
            screenshot_png=capture.screenshot_png,
            enable_visual_home_actions=visual_home_actions,
            coverage_scheduler_v2=coverage_scheduler,
            safety_rules=safety_rules,
            input_rules=input_rules,
            max_scrolls_per_direction=max_scrolls,
            include_current_navigation=True,
        )
        if item.action_key not in action_keys and _navigation_metadata(item)
    ]
    semantic_key = str(capture.model.semantic_key or capture.model.replay_key)
    sanitized = None
    thumbnail_jpeg = b""
    observation_id: Optional[int] = None
    match_evidence: Dict[str, Any] = {}
    template_id: Optional[int] = None
    assign_incoming = False
    store_full_observation = bool(identity_v2)
    cas_enabled = False
    effective_capture_kind = str(capture_kind or "DISCOVERY").upper()
    if coverage_scheduler and is_viewport:
        effective_capture_kind = "VIEWPORT"
    instance_anchor = ""
    exploration_family: Optional[InspectionExplorationFamily] = None
    family_evidence: Dict[str, Any] = {}
    family_is_new = False
    source_state: Optional[InspectionState] = None
    if effective_capture_kind in {"ROOT", "STATE", "TRANSITION"}:
        effective_capture_kind = "DISCOVERY"

    with Session(engine) as session:
        persisted_branch = session.get(InspectionBranchRun, branch_run.id)
        if persisted_branch is None:
            raise RuntimeError(f"inspection branch disappeared: {branch_run.id}")
        if identity_v2:
            try:
                from backend.artifact_store import content_addressed_assets_enabled

                cas_enabled = content_addressed_assets_enabled(session)
            except Exception:
                cas_enabled = False
            source_state = (
                session.get(InspectionState, parent_state_id)
                if parent_state_id is not None
                else None
            )
            instance_anchor = str(
                instance_anchor_override
                or derive_instance_anchor(
                    capture.model,
                    incoming_action=incoming_action,
                    source_instance_anchor=(
                        str(source_state.instance_anchor or "")
                        if source_state is not None
                        else None
                    ),
                )
            )
        template = None
        if identity_v2:
            template = session.exec(
                select(InspectionPageTemplate).where(
                    InspectionPageTemplate.package_name == capture.package_name,
                    InspectionPageTemplate.fingerprint_version == 2,
                    InspectionPageTemplate.template_key == capture.model.template_key,
                )
            ).first()
            template_id = template.id if template is not None else None

        existing = None
        if identity_v2 and preferred_state_id is not None:
            preferred = session.get(InspectionState, int(preferred_state_id))
            preferred_template = (
                session.get(InspectionPageTemplate, preferred.template_id)
                if preferred is not None and preferred.template_id is not None
                else None
            )
            preferred_viewport_role_match = bool(
                preferred_template is not None
                and str(preferred_template.page_role or "")
                == str(capture.model.role or "")
                and str(capture.model.role or "")
                in {"HOME", "LIST", "PRODUCT_DETAIL"}
            )
            preferred_overlay_return_match = bool(
                coverage_scheduler
                and str(preferred_match_type or "").upper() == "OVERLAY_RETURN"
                and preferred is not None
                and int(preferred.run_id) == int(run_id)
                and int(preferred.branch_run_id) == int(branch_run.id)
                and str(preferred.foreground_package or "").casefold()
                == str(capture.package_name or "").casefold()
                and str(preferred.activity or "") == str(capture.activity or "")
                and str(preferred.instance_anchor or "") == instance_anchor
                and str(preferred.page_subtype or "UNKNOWN")
                == str(capture.model.page_subtype or "UNKNOWN")
            )
            preferred_viewport_match = bool(
                coverage_scheduler
                and is_viewport
                and preferred is not None
                and int(preferred.run_id) == int(run_id)
                and int(preferred.branch_run_id) == int(branch_run.id)
                and str(preferred.foreground_package or "").casefold()
                == str(capture.package_name or "").casefold()
                and str(preferred.activity or "") == str(capture.activity or "")
                and str(preferred.instance_anchor or "") == instance_anchor
                and (
                    str(preferred.page_subtype or "UNKNOWN")
                    == str(capture.model.page_subtype or "UNKNOWN")
                    or preferred_viewport_role_match
                )
            )
            if (
                preferred is not None
                and int(preferred.run_id) == int(run_id)
                and int(preferred.branch_run_id) == int(branch_run.id)
                and str(preferred.foreground_package or "").casefold()
                == str(capture.package_name or "").casefold()
                and str(preferred.activity or "") == str(capture.activity or "")
                and (
                    preferred_viewport_match
                    or preferred_overlay_return_match
                    or
                    str(preferred.state_key or "")
                    == str(capture.model.state_key or "")
                    or str(preferred.semantic_key or "") == semantic_key
                )
            ):
                existing = preferred
                match_evidence = {
                    "match_type": (
                        "OVERLAY_RETURN"
                        if preferred_overlay_return_match
                        else "VIEWPORT_OBSERVATION"
                        if preferred_viewport_match
                        else "SOURCE_EXACT_STATE"
                        if str(preferred.state_key or "")
                        == str(capture.model.state_key or "")
                        else "SOURCE_SEMANTIC"
                    ),
                    "score": 1.0,
                }
        if existing is None:
            exact_state_query = select(InspectionState).where(
                InspectionState.run_id == run_id,
                InspectionState.branch_run_id == branch_run.id,
                InspectionState.state_key == capture.model.state_key,
            )
            if identity_v2:
                exact_state_query = exact_state_query.where(
                    InspectionState.instance_anchor == instance_anchor
                )
            existing = session.exec(exact_state_query).first()
            if existing is not None:
                match_evidence = {"match_type": "EXACT_STATE", "score": 1.0}
        if existing is None and identity_v2:
            existing = session.exec(
                select(InspectionState).where(
                    InspectionState.run_id == run_id,
                    InspectionState.branch_run_id == branch_run.id,
                    InspectionState.semantic_key == semantic_key,
                    InspectionState.instance_anchor == instance_anchor,
                )
            ).first()
            if existing is not None:
                match_evidence = {"match_type": "SEMANTIC", "score": 1.0}

        if existing is None and identity_v2:
            candidate, evidence = _best_similarity_candidate(
                session,
                branch_run_id=int(branch_run.id),
                capture=capture,
                instance_anchor=instance_anchor,
            )
            if evidence:
                match_evidence = {"match_type": "SIMILARITY_SHADOW", **evidence}
            if (
                similarity_convergence
                and candidate is not None
                and bool(evidence.get("equivalent"))
            ):
                existing = candidate
                match_evidence["match_type"] = "SIMILARITY_CONVERGED"

        if identity_v2:
            existing_family_id = (
                existing.exploration_family_id if existing is not None else None
            )
            source_template = (
                session.get(InspectionPageTemplate, source_state.template_id)
                if source_state is not None and source_state.template_id is not None
                else None
            )
            source_is_modal = bool(
                source_template is not None and source_template.is_modal
            )
            target_is_modal = capture.model.role == "DIALOG"
            source_is_opaque = bool(
                source_state is not None and source_state.is_opaque
            )
            target_is_opaque = bool(
                capture.model.is_opaque or capture.model.role == "OPAQUE"
            )
            viewport_family_compatible = bool(
                is_viewport
                and source_state is not None
                and source_state.exploration_family_id is not None
                and source_template is not None
                and str(source_state.foreground_package or "").casefold()
                == str(capture.package_name or "").casefold()
                and source_template.activity_family == capture.model.activity_family
                and source_template.page_role == capture.model.role
                and source_is_modal == target_is_modal
                and source_is_opaque == target_is_opaque
            )
            source_family_id = (
                source_state.exploration_family_id
                if viewport_family_compatible and source_state is not None
                else None
            )
            inherited_family_id = existing_family_id or source_family_id
            if inherited_family_id is not None:
                exploration_family = session.get(
                    InspectionExplorationFamily,
                    inherited_family_id,
                )
                family_evidence = {
                    "match_type": (
                        "EXISTING_STATE_FAMILY"
                        if existing_family_id is not None
                        else "VIEWPORT_INHERITED"
                    ),
                    "score": 1.0,
                }
            if exploration_family is None:
                (
                    exploration_family,
                    family_evidence,
                    family_is_new,
                ) = _match_or_create_exploration_family(
                    session,
                    run_id=run_id,
                    branch_run_id=int(branch_run.id),
                    capture=capture,
                    screen_size=effective_screen_size,
                )

        is_new = existing is None
        if (
            identity_v2
            and existing is not None
            and int(existing.id) in {int(item) for item in ancestry_state_ids}
        ):
            effective_capture_kind = "CYCLE"
        elif (
            identity_v2
            and existing is not None
            and effective_capture_kind == "DISCOVERY"
        ):
            effective_capture_kind = "REVISIT"
        if identity_v2 and existing is not None:
            exceptional_capture = effective_capture_kind in {
                "VERIFICATION",
                "CYCLE",
                "FAULT",
                "BASELINE",
            }
            representative = (
                session.get(
                    InspectionObservation,
                    existing.representative_observation_id,
                )
                if existing.representative_observation_id is not None
                else None
            )
            representative_phash = str(
                getattr(representative, "screenshot_phash", None)
                or getattr(representative, "perceptual_hash", None)
                or existing.perceptual_hash
                or ""
            )
            full_count = int(
                session.exec(
                    select(func.count(InspectionObservation.id)).where(
                        InspectionObservation.state_id == existing.id,
                        InspectionObservation.metadata_only == False,  # noqa: E712
                        col(InspectionObservation.capture_kind).notin_(
                            ["VERIFICATION", "CYCLE", "FAULT", "BASELINE"]
                        ),
                    )
                ).one()
                or 0
            )
            visually_redundant = bool(
                representative_phash
                and capture.perceptual_hash
                and phash_distance(
                    representative_phash,
                    capture.perceptual_hash,
                ) <= 8
            )
            store_full_observation = bool(
                exceptional_capture
                or (full_count < 3 and not visually_redundant)
            )
        if is_new and not identity_v2:
            variants = session.exec(
                select(func.count(InspectionState.id)).where(
                    InspectionState.run_id == run_id,
                    InspectionState.branch_run_id == branch_run.id,
                    InspectionState.cluster_key == capture.model.cluster_key,
                )
            ).one()
            if int(variants or 0) >= max_variants:
                return PersistedState(
                    work=None,
                    is_new=False,
                    variant_capped=True,
                )

        if (identity_v2 and store_full_observation) or is_new:
            sanitized = sanitizer.sanitize(capture.xml, capture.screenshot_png)
            thumbnail_jpeg = _thumbnail_bytes(sanitized.screenshot_png)
        preview_action_map = build_action_map(
            run_id=run_id,
            branch_key=branch_run.branch_key,
            state_id=0,
            activity=capture.activity,
            screen_size=effective_screen_size,
            actions=actions,
            sanitizer_rules=sanitizer.rules,
            secret_values=secret_values,
        )
        artifact_bytes = 0
        if (
            identity_v2
            and cas_enabled
            and store_full_observation
            and sanitized is not None
        ):
            artifact_bytes = (
                len(sanitized.screenshot_png)
                + len(sanitized.xml.encode("utf-8"))
                + len(thumbnail_jpeg)
                + len(json.dumps(preview_action_map, ensure_ascii=False).encode("utf-8"))
            )
        if budget_guard is not None:
            budget_guard.reserve_persistence(
                new_state=is_new,
                observation=identity_v2,
                artifact_bytes=artifact_bytes,
            )

        if identity_v2 and template is None:
            template = InspectionPageTemplate(
                package_name=capture.package_name,
                activity=capture.activity,
                fingerprint_version=2,
                template_key=capture.model.template_key,
                observation_count=0,
                first_seen_at=_now(),
            )
            signature = dict(capture.model.signature)
            optional_template_values = {
                "activity_family": capture.model.activity_family,
                "page_role": capture.model.role,
                "is_modal": capture.model.role == "DIALOG",
                "structure_signature": list(
                    signature.get(
                        "structure_tokens",
                        signature.get("template_tokens", []),
                    )
                ),
                "action_signature": list(signature.get("action_tokens", [])),
                "anchor_signature": list(
                    signature.get(
                        "anchor_tokens",
                        signature.get("landmark_keys", []),
                    )
                ),
                "control_state_signature": list(
                    signature.get(
                        "control_state_tokens",
                        signature.get("control_tokens", []),
                    )
                ),
                "risk_signature": list(signature.get("risk_tokens", [])),
            }
            for field_name, value in optional_template_values.items():
                if field_name in InspectionPageTemplate.model_fields:
                    setattr(template, field_name, value)
            session.add(template)
            session.flush()
            template_id = template.id

        if existing is None:
            row = InspectionState(
                run_id=run_id,
                branch_run_id=branch_run.id,
                branch_key=branch_run.branch_key,
                cluster_key=capture.model.cluster_key,
                state_key=capture.model.state_key,
                template_id=template_id,
                semantic_key=semantic_key if identity_v2 else None,
                identity_version=2 if identity_v2 else 1,
                instance_anchor=instance_anchor if identity_v2 else None,
                exploration_family_id=(
                    exploration_family.id
                    if identity_v2 and exploration_family is not None
                    else None
                ),
                family_match_confidence=(
                    float(family_evidence["score"])
                    if identity_v2
                    and exploration_family is not None
                    and family_evidence.get("score") is not None
                    else None
                ),
                family_match_evidence=(
                    dict(family_evidence)
                    if identity_v2 and exploration_family is not None
                    else {}
                ),
                exploration_mode=(
                    "FULL"
                    if identity_v2 and family_is_new
                    else "DELTA_ONLY"
                    if identity_v2 and family_convergence
                    else "INDEPENDENT"
                ),
                page_subtype=str(capture.model.page_subtype or "UNKNOWN"),
                coverage_status="DISCOVERED",
                frontier_priority=700,
                frontier_reason="DISCOVERED",
                expansion_status="DISCOVERED",
                pending_action_count=len(actions),
                activity=capture.activity,
                foreground_package=capture.package_name,
                depth=depth,
                parent_state_id=parent_state_id,
                screenshot_sha=capture.screenshot_sha,
                perceptual_hash=capture.perceptual_hash,
                stable_status="VIEWPORT" if is_viewport else "UNVERIFIED",
                selected_for_regression=False,
                locator_quality=locator_quality(actions),
                is_dynamic=(
                    capture.model.has_dynamic_text
                    or capture.stable_by in {"perceptual", "timeout"}
                ),
                is_opaque=capture.model.is_opaque,
                first_path=list(path),
            )
            session.add(row)
            session.flush()
            assign_incoming = not mark_branch_root
        else:
            row = existing
            row.visit_count += 1
            if identity_v2 and not row.semantic_key:
                row.semantic_key = semantic_key
                row.identity_version = 2
                row.template_id = template_id
            if identity_v2:
                row.instance_anchor = row.instance_anchor or instance_anchor
                row.page_subtype = str(
                    row.page_subtype
                    if row.page_subtype not in {None, "", "UNKNOWN"}
                    else capture.model.page_subtype or "UNKNOWN"
                )
                if exploration_family is not None and row.exploration_family_id is None:
                    row.exploration_family_id = exploration_family.id
                    row.family_match_confidence = (
                        float(family_evidence["score"])
                        if family_evidence.get("score") is not None
                        else None
                    )
                    row.family_match_evidence = dict(family_evidence)
            path_changed = _path_score(path) < _path_score(row.first_path or [])
            if path_changed:
                row.first_path = list(path)
            incoming = (
                session.get(InspectionTransition, row.incoming_transition_id)
                if row.incoming_transition_id is not None
                else None
            )
            incoming_relation = str(
                incoming.relation_type if incoming is not None else ""
            ).upper()
            is_root = bool(mark_branch_root or persisted_branch.root_state_id == row.id)
            hierarchy_changed = False
            if mark_branch_root:
                hierarchy_changed = bool(
                    row.depth != depth or row.parent_state_id != parent_state_id
                )
            elif not is_root and not (
                incoming_relation == "PEER" and not prefer_hierarchy
            ):
                hierarchy_changed = bool(
                    depth < row.depth
                    or (
                        prefer_hierarchy
                        and depth == row.depth
                        and row.parent_state_id != parent_state_id
                    )
                )
            if hierarchy_changed:
                row.depth = depth
                row.parent_state_id = parent_state_id
            candidate_matches_hierarchy = bool(
                row.depth == depth and row.parent_state_id == parent_state_id
            )
            assign_incoming = bool(
                not is_root
                and (
                    hierarchy_changed
                    or (incoming is None and candidate_matches_hierarchy)
                    or (
                        prefer_hierarchy
                        and incoming_relation != "PEER"
                        and candidate_matches_hierarchy
                    )
                )
            )
            row.updated_at = _now()
            session.add(row)

        state_id = int(row.id)
        if identity_v2 and exploration_family is not None:
            representative_state = (
                session.get(
                    InspectionState,
                    exploration_family.representative_state_id,
                )
                if exploration_family.representative_state_id is not None
                else None
            )
            if (
                representative_state is None
                or representative_state.exploration_family_id
                != exploration_family.id
            ):
                exploration_family.representative_state_id = state_id
                row.exploration_mode = "FULL"
            elif family_convergence and state_id != int(
                exploration_family.representative_state_id
            ):
                row.exploration_mode = "DELTA_ONLY"
            exploration_family.member_count = int(
                session.exec(
                    select(func.count(InspectionState.id)).where(
                        InspectionState.exploration_family_id
                        == exploration_family.id
                    )
                ).one()
                or 0
            )
            exploration_family.updated_at = _now()
            session.add(exploration_family)
            session.add(row)
        if mark_branch_root:
            persisted_branch.root_state_id = state_id
            row.incoming_transition_id = None
            session.add(persisted_branch)
            branch_run.root_state_id = state_id

        state_dir = _safe_report_path(
            _reports_root()
            / "inspection"
            / str(run_id)
            / branch_run.branch_key
            / str(state_id)
        )
        screenshot_path = _safe_report_path(state_dir / "screenshot.png")
        screenshot_relative = _relative_report_path(screenshot_path)
        current_action_map = build_action_map(
            run_id=run_id,
            branch_key=branch_run.branch_key,
            state_id=state_id,
            activity=capture.activity,
            screen_size=effective_screen_size,
            actions=actions,
            screenshot_path=(
                screenshot_relative if is_new else row.screenshot_path
            ),
            sanitizer_rules=sanitizer.rules,
            secret_values=secret_values,
        )
        action_map_path = _state_action_map_path(
            run_id,
            branch_run.branch_key,
            state_id,
        )
        if not is_new and action_map_path.exists():
            action_map = read_action_map(action_map_path)
            existing_action_keys = {
                str(item.get("action_key") or "")
                for item in action_map.get("actions") or []
                if isinstance(item, dict)
            }
            action_map.setdefault("actions", []).extend(
                dict(item)
                for item in current_action_map.get("actions") or []
                if isinstance(item, dict)
                and str(item.get("action_key") or "") not in existing_action_keys
            )
        else:
            action_map = current_action_map

        if identity_v2:
            sequence = int(
                session.exec(
                    select(func.count(InspectionObservation.id)).where(
                        InspectionObservation.run_id == run_id
                    )
                ).one()
                or 0
            ) + 1
            observation = InspectionObservation(
                run_id=run_id,
                branch_run_id=branch_run.id,
                state_id=state_id,
                template_id=template_id,
                sequence=sequence,
                capture_kind=effective_capture_kind,
                package_name=capture.package_name,
                activity=capture.activity,
                exact_cluster_key=capture.model.cluster_key,
                exact_replay_key=capture.model.replay_key,
                exact_state_key=capture.model.state_key,
                screenshot_sha=capture.screenshot_sha,
                perceptual_hash=capture.perceptual_hash,
                stable_by=capture.stable_by,
                is_representative=is_new,
                original_width=effective_screen_size[0],
                original_height=effective_screen_size[1],
                match_confidence=float(match_evidence.get("score", 1.0)),
                match_evidence=dict(match_evidence),
                asset_status=(
                    "AVAILABLE"
                    if cas_enabled and store_full_observation
                    else "LEGACY"
                    if not cas_enabled
                    else "METADATA_ONLY"
                ),
                metadata_only=not (cas_enabled and store_full_observation),
                captured_at=_now(),
            )
            if "screenshot_phash" in InspectionObservation.model_fields:
                observation.screenshot_phash = capture.perceptual_hash
            session.add(observation)
            session.flush()
            observation_id = int(observation.id)
            if cas_enabled and store_full_observation and sanitized is not None:
                _persist_observation_assets(
                    session,
                    observation=observation,
                    screenshot_png=sanitized.screenshot_png,
                    xml=sanitized.xml,
                    thumbnail_jpeg=thumbnail_jpeg,
                    action_map=current_action_map,
                )
            row.observation_count = int(row.observation_count or 0) + 1
            row.last_observed_at = _now()
            if row.representative_observation_id is None:
                row.representative_observation_id = observation_id
                observation.is_representative = True
                session.add(observation)
            if template is not None:
                template.observation_count = int(template.observation_count or 0) + 1
                template.last_seen_at = _now()
                template.updated_at = _now()
                session.add(template)
            session.add(row)

        session.commit()
        session.refresh(row)
        persisted_row = {
            "state_key": str(row.state_key),
            "cluster_key": str(row.cluster_key),
            "package_name": str(row.foreground_package or capture.package_name),
            "activity": str(row.activity or capture.activity),
            "screenshot_sha": str(row.screenshot_sha or capture.screenshot_sha),
            "depth": int(row.depth),
            "path": list(row.first_path or []),
            "parent_state_id": row.parent_state_id,
            "semantic_key": str(row.semantic_key or semantic_key),
            "instance_anchor": str(row.instance_anchor or instance_anchor),
            "exploration_family_id": row.exploration_family_id,
            "exploration_mode": str(row.exploration_mode or "INDEPENDENT"),
            "family_match_confidence": row.family_match_confidence,
            "page_subtype": str(row.page_subtype or "UNKNOWN"),
            "coverage_status": str(row.coverage_status or "DISCOVERED"),
            "frontier_priority": int(row.frontier_priority or 700),
            "frontier_reason": str(row.frontier_reason or "DISCOVERED"),
        }

    if is_new:
        xml_path = _safe_report_path(state_dir / "hierarchy.xml")
        screenshot_path = _safe_report_path(state_dir / "screenshot.png")
        written = sanitizer.write(
            xml=capture.xml,
            screenshot_png=capture.screenshot_png,
            xml_path=xml_path,
            screenshot_path=screenshot_path,
        )
        thumbnail_path = _safe_report_path(state_dir / "thumbnail.jpg")
        thumbnail_path.write_bytes(
            thumbnail_jpeg or _thumbnail_bytes(written.screenshot_png)
        )
        with Session(engine) as session:
            saved = session.get(InspectionState, state_id)
            if saved is None:
                raise RuntimeError(f"inspection state disappeared: {state_id}")
            saved.xml_path = _relative_report_path(xml_path)
            saved.screenshot_path = _relative_report_path(screenshot_path)
            saved.thumbnail_path = _relative_report_path(thumbnail_path)
            saved.updated_at = _now()
            session.add(saved)
            session.commit()
        action_map["screenshot_path"] = _relative_report_path(screenshot_path)
        current_action_map["screenshot_path"] = _relative_report_path(screenshot_path)
        write_action_map(action_map_path, action_map)
    elif not action_map_path.exists():
        write_action_map(action_map_path, action_map)

    ancestry = tuple(int(item) for item in ancestry_state_ids)
    if not ancestry or ancestry[-1] != state_id:
        ancestry = (*ancestry, state_id)
    work = StateWork(
        state_id=state_id,
        state_key=persisted_row["state_key"],
        cluster_key=persisted_row["cluster_key"],
        replay_key=capture.model.replay_key,
        package_name=persisted_row["package_name"],
        activity=persisted_row["activity"],
        screenshot_sha=persisted_row["screenshot_sha"],
        depth=persisted_row["depth"],
        path=persisted_row["path"],
        actions=actions,
        recovery_navigation_actions=recovery_navigation_actions,
        action_map=action_map,
        parent_state_id=persisted_row["parent_state_id"],
        semantic_key=(
            persisted_row["semantic_key"]
            if identity_v2
            else capture.model.replay_key
        ),
        template_key=str(capture.model.template_key or ""),
        role=str(capture.model.role or "UNKNOWN"),
        activity_family=str(capture.model.activity_family or ""),
        observation_id=observation_id,
        ancestry_state_ids=ancestry,
        instance_anchor=persisted_row["instance_anchor"],
        exploration_family_id=persisted_row["exploration_family_id"],
        exploration_mode=persisted_row["exploration_mode"],
        family_match_confidence=persisted_row["family_match_confidence"],
        page_subtype=persisted_row["page_subtype"],
        coverage_status=persisted_row["coverage_status"],
        frontier_priority=persisted_row["frontier_priority"],
        frontier_reason=persisted_row["frontier_reason"],
    )
    return PersistedState(
        work=work,
        is_new=is_new,
        assign_incoming=assign_incoming,
        observation_id=observation_id,
        match_evidence=match_evidence,
    )


def _dump_xml(device) -> str:
    try:
        return str(device.dump_hierarchy(compressed=False) or "")
    except TypeError:
        return str(device.dump_hierarchy() or "")
    except Exception as exc:
        raise DeviceDisconnected(f"XML hierarchy 获取失败: {exc}") from exc


def _probe_ui_automation_responsive(
    device,
    *,
    abort_event: threading.Event,
    attempts: int = 2,
    budget_guard: Optional[BudgetGuard] = None,
) -> bool:
    """Independently verify UI automation after an unexpected action error.

    A selector disappearing is handled separately as locator drift. Other
    action exceptions are only promoted to the hard UI_UNRESPONSIVE fault when
    both foreground-app and hierarchy probes fail repeatedly.
    """
    for attempt in range(max(1, int(attempts))):
        _check_abort(abort_event)
        try:
            if budget_guard is not None:
                budget_guard.before_device_interaction("probe_app_current")
            current = device.app_current()
            if budget_guard is not None:
                budget_guard.before_device_interaction("probe_hierarchy")
            xml = _dump_xml(device)
            if budget_guard is not None:
                budget_guard.check_deadline()
            if isinstance(current, dict) and bool(str(xml or "").strip()):
                return True
        except InspectionAborted:
            raise
        except BudgetExceeded:
            raise
        except Exception as exc:
            logger.debug(
                "inspection UI health probe failed: attempt=%s error=%s",
                attempt + 1,
                _safe_error(exc),
            )
        if attempt + 1 < attempts and abort_event.wait(0.25):
            raise InspectionAborted("inspection cancelled")
    return False


def _longest_replayed_prefix(
    capture: Optional[CapturedPage],
    path: Sequence[Dict[str, Any]],
) -> int:
    """Number of leading path steps already satisfied by the current page.

    Uses the semantic keys serialized into each path step. Returns len(path)
    when the page already matches the final target, k when it matches the
    source of step k, and -1 when the position is unknown.
    """
    if capture is None or not path:
        return -1
    current_key = _page_logical_key(capture.model)
    if not current_key:
        return -1
    final_target = str(path[-1].get("expected_target_semantic_key") or "")
    if final_target and current_key == final_target:
        return len(path)
    for index in range(len(path) - 1, -1, -1):
        source_key = str(path[index].get("expected_source_semantic_key") or "")
        if source_key and current_key == source_key:
            return index
    return -1


def _replay_path(
    *,
    device,
    path: Sequence[Dict[str, Any]],
    branch_config: Dict[str, Any],
    device_serial: str,
    package_name: str,
    abort_event: threading.Event,
    input_rules: Sequence[Dict[str, Any]],
    dynamic_patterns: Sequence[str],
    stable_wait_seconds: float,
    secret_values: List[str],
    allow_discovery_scroll: bool = False,
    stage_callback: Optional[Callable[[str, str], None]] = None,
    budget_guard: Optional[BudgetGuard] = None,
    current_capture: Optional[CapturedPage] = None,
) -> Tuple[Optional[CapturedPage], bool]:
    _check_abort(abort_event)
    if current_capture is not None and current_capture.package_name == package_name:
        prefix_len = _longest_replayed_prefix(current_capture, path)
        # prefix_len == len(path) is not taken as a shortcut: callers verify
        # the current page against the target before calling here, so a full
        # key match with a failed caller check means the strict fingerprint
        # disagrees and only a deterministic replay can resolve it.
        if 0 <= prefix_len < len(path):
            if stage_callback:
                stage_callback(
                    "replay_path",
                    f"从当前页面增量续放 {prefix_len}/{len(path)}",
                )
            try:
                capture, unique = _replay_path_suffix(
                    device=device,
                    capture=current_capture,
                    path=path[prefix_len:],
                    branch_config=branch_config,
                    package_name=package_name,
                    abort_event=abort_event,
                    input_rules=input_rules,
                    dynamic_patterns=dynamic_patterns,
                    stable_wait_seconds=stable_wait_seconds,
                    secret_values=secret_values,
                    stage_callback=stage_callback,
                    budget_guard=budget_guard,
                )
                if unique and capture is not None:
                    return capture, True
            except InspectionAborted:
                raise
            except BudgetExceeded:
                raise
            except (PathDiverged, LocatorAmbiguous, LocatorDrift, PermissionError):
                pass
            except Exception as exc:
                logger.debug(
                    "inspection incremental replay failed, falling back to "
                    "full replay: %s",
                    _safe_error(exc),
                )
            # Incremental continuation is best-effort only; a failure here
            # falls through to the deterministic entry-case full replay.
    if stage_callback:
        stage_callback("replay_root", "回根执行进入用例")
    _try_run_case(
        case_id=int(branch_config.get("entry_case_id") or 0),
        device_serial=device_serial,
        env_id=branch_config.get("env_id"),
        abort_event=abort_event,
        budget_guard=budget_guard,
    )
    if budget_guard is not None:
        budget_guard.before_device_interaction("ready_assertion")
    ready = ready_assertion_exists(
        device,
        dict(branch_config.get("ready_assertion") or {}),
        abort_event=abort_event,
    )
    if budget_guard is not None:
        budget_guard.check_deadline()
    if not ready:
        return None, False

    if stage_callback:
        stage_callback("stabilizing", "等待根页面稳定")
    if budget_guard is not None:
        budget_guard.before_device_interaction("capture_replay_root")
    capture = _budgeted_wait_for_stable_page(
        device,
        budget_guard=budget_guard,
        expected_package=package_name,
        abort_event=abort_event,
        max_wait_seconds=stable_wait_seconds,
        dynamic_patterns=dynamic_patterns,
    )
    unique = True
    for path_index, payload in enumerate(path):
        _check_abort(abort_event)
        if budget_guard is not None:
            budget_guard.check_deadline()
        capture = _confirm_replay_expectation(
            device=device,
            capture=capture,
            payload=dict(payload),
            phase="source",
            step_index=path_index,
            package_name=package_name,
            abort_event=abort_event,
            dynamic_patterns=dynamic_patterns,
            stable_wait_seconds=stable_wait_seconds,
            budget_guard=budget_guard,
        )
        action = _deserialize_action(dict(payload))
        discovery_scroll = bool(
            allow_discovery_scroll
            and action.action_type == "scroll"
            and not action.risk_type
        )
        if (
            action.risk_type
            or (
                (action.coordinate_only or not action.replayable)
                and not discovery_scroll
            )
        ):
            return None, False
        input_value: Optional[str] = None
        try:
            if action.action_type == "input":
                input_value, _, _ = _resolve_input_value(
                    action=action,
                    input_rules=input_rules,
                    env_id=branch_config.get("env_id"),
                    secret_values=secret_values,
                )
            if stage_callback:
                stage_callback(
                    "replay_path",
                    f"回放路径动作 {path_index + 1}/{len(path)}",
                )
            if budget_guard is not None:
                budget_guard.before_device_interaction(
                    "perform_replay_action",
                    mutating=True,
                )
            perform_action(
                device,
                action,
                current_xml=capture.xml,
                input_value=input_value,
            )
        except (LocatorAmbiguous, LocatorDrift, PermissionError):
            unique = False
            return None, unique
        finally:
            input_value = None
        if stage_callback:
            stage_callback("stabilizing", "等待回放页面稳定")
        if budget_guard is not None:
            budget_guard.before_device_interaction("capture_replay_target")
        capture = _budgeted_wait_for_stable_page(
            device,
            budget_guard=budget_guard,
            expected_package=package_name,
            abort_event=abort_event,
            max_wait_seconds=stable_wait_seconds,
            dynamic_patterns=dynamic_patterns,
        )
        if capture.package_name != package_name:
            return capture, False
        capture = _confirm_replay_expectation(
            device=device,
            capture=capture,
            payload=dict(payload),
            phase="target",
            step_index=path_index,
            package_name=package_name,
            abort_event=abort_event,
            dynamic_patterns=dynamic_patterns,
            stable_wait_seconds=stable_wait_seconds,
            budget_guard=budget_guard,
        )
    return capture, unique


def _shared_path_prefix_len(
    left: Sequence[Dict[str, Any]],
    right: Sequence[Dict[str, Any]],
) -> int:
    length = 0
    for a, b in zip(left, right):
        if str(a.get("action_key") or "") != str(b.get("action_key") or ""):
            break
        length += 1
    return length


def _is_primary_entry_surface(work: StateWork) -> bool:
    """Return whether a work item is a root bottom-navigation destination.

    These destinations are peer pages, so their serialized path starts with
    the navigation action that discovered them while retaining depth zero.
    Keeping this classification on the work item lets the coverage scheduler
    give every primary surface an initial turn before following one surface's
    deep child chain.
    """
    if int(work.depth or 0) != 0:
        return False
    if (
        str(work.page_subtype or "").upper() == "HOME"
        and not work.path
    ):
        return True
    for payload in work.path or []:
        if not isinstance(payload, dict):
            continue
        navigation = (payload.get("target_meta") or {}).get("navigation")
        if not isinstance(navigation, dict):
            continue
        if (
            str(navigation.get("group_region") or "").lower() == "bottom"
            and str(payload.get("sample_policy") or "") == "RUN_NAV_ONCE"
        ):
            return True
    return False


def _coverage_representative_priority(work: StateWork) -> int:
    """Choose a bounded frontier tier for a newly discovered page.

    Unknown and opaque surfaces are still captured, but they should not be
    able to starve recognized business pages when their structure is noisy.
    Critical commerce pages stay ahead of ordinary representatives.
    """
    subtype = str(work.page_subtype or "UNKNOWN").upper()
    role = str(work.role or "UNKNOWN").upper()
    if subtype in {
        "PRODUCT_DETAIL",
        "PURCHASE_OPTIONS",
        "CHECKOUT",
        "CASHIER",
        "ORDER",
    } or role in {
        "PRODUCT_DETAIL",
        "CHECKOUT",
        "CASHIER",
        "ORDER",
    }:
        return 100
    if subtype in {"UNKNOWN", "OPAQUE"} or role in {"UNKNOWN", "OPAQUE"}:
        return 550
    return 200


def _pop_most_local(
    queue: Deque[StateWork],
    current_path: Optional[Sequence[Dict[str, Any]]],
    *,
    coverage_scheduler: bool = False,
) -> StateWork:
    """Pop the next BFS state, preferring path locality within one layer.

    Only states sharing the front state's depth are considered, so the
    breadth-first layer order is preserved; among them the state whose replay
    path shares the longest action prefix with the device's current position
    wins, minimizing entry-case replays.
    """
    if len(queue) <= 1:
        return queue.popleft()
    if coverage_scheduler:
        best_index = min(
            range(len(queue)),
            key=lambda index: (
                int(queue[index].frontier_priority or 700),
                0 if _is_primary_entry_surface(queue[index]) else 1,
                index if _is_primary_entry_surface(queue[index]) else 0,
                (
                    0
                    if _is_primary_entry_surface(queue[index])
                    else -_shared_path_prefix_len(
                        queue[index].path,
                        current_path or (),
                    )
                ),
                queue[index].depth,
                index,
            ),
        )
        work = queue[best_index]
        del queue[best_index]
        return work
    if not current_path:
        return queue.popleft()
    depth = queue[0].depth
    best_index = 0
    best_score = -1
    for index, work in enumerate(queue):
        if work.depth != depth:
            break
        score = _shared_path_prefix_len(work.path, current_path)
        if score > best_score:
            best_index = index
            best_score = score
    work = queue[best_index]
    del queue[best_index]
    return work


def _path_has_prefix(
    path: Sequence[Dict[str, Any]],
    prefix: Sequence[Dict[str, Any]],
) -> bool:
    if len(prefix) > len(path):
        return False
    return all(
        str(path[index].get("action_key") or "")
        == str(payload.get("action_key") or "")
        for index, payload in enumerate(prefix)
    )


def _paths_equivalent(
    left: Sequence[Dict[str, Any]],
    right: Sequence[Dict[str, Any]],
) -> bool:
    """Compare replay paths by their stable action keys."""

    return _path_has_prefix(left, right) and _path_has_prefix(right, left)


def _select_navigation_entry(
    entries: Sequence[NavigationEntry],
    *,
    group_key: str,
    parent: StateWork,
) -> Optional[NavigationEntry]:
    candidates = [
        entry
        for entry in entries
        if entry.group_key == group_key
        and _path_has_prefix(parent.path, entry.target_path)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda item: (len(item.target_path), item.state_id))


def _replay_path_suffix(
    *,
    device,
    capture: CapturedPage,
    path: Sequence[Dict[str, Any]],
    branch_config: Dict[str, Any],
    package_name: str,
    abort_event: threading.Event,
    input_rules: Sequence[Dict[str, Any]],
    dynamic_patterns: Sequence[str],
    stable_wait_seconds: float,
    secret_values: List[str],
    stage_callback: Optional[Callable[[str, str], None]] = None,
    budget_guard: Optional[BudgetGuard] = None,
) -> Tuple[Optional[CapturedPage], bool]:
    current = capture
    for path_index, payload in enumerate(path):
        _check_abort(abort_event)
        if budget_guard is not None:
            budget_guard.check_deadline()
        current = _confirm_replay_expectation(
            device=device,
            capture=current,
            payload=dict(payload),
            phase="source",
            step_index=path_index,
            package_name=package_name,
            abort_event=abort_event,
            dynamic_patterns=dynamic_patterns,
            stable_wait_seconds=stable_wait_seconds,
            budget_guard=budget_guard,
        )
        action = _deserialize_action(dict(payload))
        replayable_scroll = bool(
            action.action_type == "scroll" and not action.risk_type
        )
        if action.risk_type or (
            (action.coordinate_only or not action.replayable)
            and not replayable_scroll
        ):
            return current, False
        input_value: Optional[str] = None
        try:
            if action.action_type == "input":
                input_value, _, _ = _resolve_input_value(
                    action=action,
                    input_rules=input_rules,
                    env_id=branch_config.get("env_id"),
                    secret_values=secret_values,
                )
            if stage_callback:
                stage_callback(
                    "recover_peer_path",
                    f"回放源页面路径 {path_index + 1}/{len(path)}",
                )
            if budget_guard is not None:
                budget_guard.before_device_interaction(
                    "perform_replay_suffix_action",
                    mutating=True,
                )
            perform_action(
                device,
                action,
                current_xml=current.xml,
                input_value=input_value,
            )
        except (LocatorAmbiguous, LocatorDrift, PermissionError):
            return current, False
        finally:
            input_value = None
        if budget_guard is not None:
            budget_guard.before_device_interaction("capture_replay_suffix")
        current = _budgeted_wait_for_stable_page(
            device,
            budget_guard=budget_guard,
            expected_package=package_name,
            abort_event=abort_event,
            max_wait_seconds=stable_wait_seconds,
            dynamic_patterns=dynamic_patterns,
        )
        if current.package_name != package_name:
            return current, False
        current = _confirm_replay_expectation(
            device=device,
            capture=current,
            payload=dict(payload),
            phase="target",
            step_index=path_index,
            package_name=package_name,
            abort_event=abort_event,
            dynamic_patterns=dynamic_patterns,
            stable_wait_seconds=stable_wait_seconds,
            budget_guard=budget_guard,
        )
    return current, True


def _restore_parent_after_transition(
    *,
    device,
    parent: StateWork,
    target_capture: CapturedPage,
    relation_type: str,
    navigation_group_key: Optional[str],
    navigation_entries: Sequence[NavigationEntry],
    branch_config: Dict[str, Any],
    device_serial: str,
    package_name: str,
    abort_event: threading.Event,
    input_rules: Sequence[Dict[str, Any]],
    dynamic_patterns: Sequence[str],
    stable_wait_seconds: float,
    secret_values: List[str],
    stage_callback: Optional[Callable[[str, str], None]] = None,
    budget_guard: Optional[BudgetGuard] = None,
) -> Optional[CapturedPage]:
    """Restore one action's exact source state with bounded fallbacks."""
    _check_abort(abort_event)
    current: Optional[CapturedPage] = target_capture
    can_try_back = True

    if relation_type == "PEER" and navigation_group_key:
        entry = _select_navigation_entry(
            navigation_entries,
            group_key=navigation_group_key,
            parent=parent,
        )
        if (
            entry is not None
            and entry.action.replayable
            and not entry.action.coordinate_only
            and not entry.action.risk_type
        ):
            try:
                if stage_callback:
                    stage_callback("recover_peer", "点击源页面导航项")
                if budget_guard is not None:
                    budget_guard.before_device_interaction(
                        "recover_peer_action",
                        mutating=True,
                    )
                perform_action(
                    device,
                    entry.action,
                    current_xml=target_capture.xml,
                )
                if budget_guard is not None:
                    budget_guard.before_device_interaction("capture_recovered_peer")
                current = _budgeted_wait_for_stable_page(
                    device,
                    budget_guard=budget_guard,
                    expected_package=package_name,
                    abort_event=abort_event,
                    max_wait_seconds=stable_wait_seconds,
                    dynamic_patterns=dynamic_patterns,
                )
                suffix = parent.path[len(entry.target_path) :]
                if suffix and current.package_name == package_name:
                    current, unique = _replay_path_suffix(
                        device=device,
                        capture=current,
                        path=suffix,
                        branch_config=branch_config,
                        package_name=package_name,
                        abort_event=abort_event,
                        input_rules=input_rules,
                        dynamic_patterns=dynamic_patterns,
                        stable_wait_seconds=stable_wait_seconds,
                        secret_values=secret_values,
                        stage_callback=stage_callback,
                        budget_guard=budget_guard,
                    )
                    if not unique:
                        can_try_back = False
                if _capture_matches_parent(current, parent):
                    return current
                can_try_back = bool(
                    current is not None
                    and current.package_name == target_capture.package_name
                    and current.activity == target_capture.activity
                    and current.model.replay_key == target_capture.model.replay_key
                )
            except InspectionAborted:
                raise
            except BudgetExceeded:
                raise
            except PathDiverged:
                parent.recovery_status = PathDiverged.code
                return None
            except LocatorDrift:
                try:
                    if budget_guard is not None:
                        budget_guard.before_device_interaction(
                            "capture_after_locator_drift"
                        )
                    current = _budgeted_wait_for_stable_page(
                        device,
                        budget_guard=budget_guard,
                        expected_package=package_name,
                        abort_event=abort_event,
                        max_wait_seconds=stable_wait_seconds,
                        dynamic_patterns=dynamic_patterns,
                    )
                    if _capture_matches_parent(current, parent):
                        return current
                    can_try_back = bool(
                        current.package_name == target_capture.package_name
                        and current.activity == target_capture.activity
                        and current.model.replay_key
                        == target_capture.model.replay_key
                    )
                except InspectionAborted:
                    raise
                except BudgetExceeded:
                    raise
                except PathDiverged:
                    parent.recovery_status = PathDiverged.code
                    return None
                except Exception:
                    current = None
                    can_try_back = False
            except (LocatorAmbiguous, PermissionError):
                current = target_capture
                can_try_back = True
            except Exception:
                can_try_back = False

    if can_try_back:
        try:
            if stage_callback:
                stage_callback("recover_parent", "尝试一次系统返回")
            if budget_guard is not None:
                budget_guard.before_device_interaction(
                    "press_back",
                    mutating=True,
                )
            device.press("back")
            if abort_event.wait(0.5):
                raise InspectionAborted("inspection cancelled")
            if budget_guard is not None:
                budget_guard.before_device_interaction("capture_after_back")
            current = _budgeted_wait_for_stable_page(
                device,
                budget_guard=budget_guard,
                expected_package=package_name,
                abort_event=abort_event,
                max_wait_seconds=min(1.5, stable_wait_seconds),
                dynamic_patterns=dynamic_patterns,
            )
            if _capture_matches_parent(current, parent):
                return current
            if stable_wait_seconds > 1.5:
                # The first capture after Back uses a shortened wait; give a
                # slow page one full-length stabilization pass before paying
                # for the entry-case replay.
                if budget_guard is not None:
                    budget_guard.before_device_interaction(
                        "capture_after_back_retry"
                    )
                current = _budgeted_wait_for_stable_page(
                    device,
                    budget_guard=budget_guard,
                    expected_package=package_name,
                    abort_event=abort_event,
                    max_wait_seconds=stable_wait_seconds,
                    dynamic_patterns=dynamic_patterns,
                )
                if _capture_matches_parent(current, parent):
                    return current
        except InspectionAborted:
            raise
        except BudgetExceeded:
            raise
        except PathDiverged:
            parent.recovery_status = PathDiverged.code
            return None
        except Exception:
            current = None

    if stage_callback:
        stage_callback("recover_parent", "完整回放源页面路径")
    try:
        replayed, unique = _replay_path(
            device=device,
            path=parent.path,
            branch_config=branch_config,
            device_serial=device_serial,
            package_name=package_name,
            abort_event=abort_event,
            input_rules=input_rules,
            dynamic_patterns=dynamic_patterns,
            stable_wait_seconds=stable_wait_seconds,
            secret_values=secret_values,
            allow_discovery_scroll=True,
            stage_callback=stage_callback,
            budget_guard=budget_guard,
            current_capture=current,
        )
    except InspectionAborted:
        raise
    except BudgetExceeded:
        raise
    except PathDiverged:
        parent.recovery_status = PathDiverged.code
        return None
    except Exception as exc:
        logger.warning(
            "inspection parent recovery replay failed: state=%s error=%s",
            parent.state_id,
            _safe_error(exc),
        )
        return None
    if unique and _capture_matches_parent(replayed, parent):
        return replayed
    return None


def _capture_matches_parent(
    capture: Optional[CapturedPage],
    parent: StateWork,
) -> bool:
    if (
        capture is None
        or capture.package_name != parent.package_name
        or capture.activity != parent.activity
    ):
        return False
    if parent.semantic_key:
        if parent.semantic_key in {
            _page_logical_key(capture.model),
            capture.model.replay_key,
        }:
            return True
        if _page_logical_key(capture.model) in set(parent.viewport_semantic_keys):
            return bool(
                str(capture.model.role or "") == str(parent.role or "")
                and str(capture.model.page_subtype or "UNKNOWN")
                == str(parent.page_subtype or "UNKNOWN")
            )
    elif capture.model.replay_key == parent.replay_key:
        return True
    is_viewport = _is_viewport_path(parent.path)
    if not parent.instance_anchor and not is_viewport:
        return False
    try:
        with Session(engine) as session:
            state = session.get(InspectionState, parent.state_id)
            expected_model = (
                _observation_candidate_model(session, state)
                if state is not None
                else None
            )
        if expected_model is None:
            return False
        if str(parent.page_subtype or "UNKNOWN") in {
            "PROFILE",
            "CATALOG_CATEGORY",
            "COMMUNITY_FEED",
            "SETTINGS",
            "ADDRESS_LIST",
            "ADDRESS_FORM",
            "INVOICE_FORM",
        }:
            page_similarity = compare_page_models(expected_model, capture.model)
            page_evidence = page_similarity.evidence
            family_similarity = compare_exploration_families(
                expected_model,
                capture.model,
            )
            family_evidence = family_similarity.evidence
            anchor_similarity = max(
                float(page_evidence.get("anchor_similarity", 0.0)),
                float(page_evidence.get("landmark_similarity", 0.0)),
            )
            return bool(
                expected_model.role == capture.model.role
                and expected_model.activity_family
                == capture.model.activity_family
                and expected_model.page_subtype == capture.model.page_subtype
                and derive_instance_anchor(expected_model)
                == derive_instance_anchor(capture.model)
                and family_similarity.equivalent
                and float(page_evidence.get("structure_similarity", 0.0))
                >= 0.97
                and float(page_evidence.get("action_similarity", 0.0)) >= 0.90
                and anchor_similarity >= 0.90
                and bool(page_evidence.get("risk_signature_match"))
                and bool(family_evidence.get("risk_match", True))
                and not family_evidence.get("control_conflicts")
            )
        if is_viewport:
            page_similarity = compare_page_models(expected_model, capture.model)
            page_evidence = page_similarity.evidence
            family_similarity = compare_exploration_families(
                expected_model,
                capture.model,
            )
            family_evidence = family_similarity.evidence
            anchor_similarity = max(
                float(page_evidence.get("anchor_similarity", 0.0)),
                float(page_evidence.get("landmark_similarity", 0.0)),
            )
            return bool(
                expected_model.role == capture.model.role
                and expected_model.activity_family
                == capture.model.activity_family
                and float(page_evidence.get("structure_similarity", 0.0))
                >= 0.96
                and float(page_evidence.get("action_similarity", 0.0)) >= 0.90
                and anchor_similarity >= 0.90
                and bool(page_evidence.get("risk_signature_match"))
                and not family_evidence.get("control_conflicts")
            )
        similarity = compare_page_models(expected_model, capture.model)
        evidence = similarity.evidence
        return bool(
            similarity.score >= 0.97
            and float(evidence.get("structure_similarity", 0.0)) >= 0.97
            and float(evidence.get("action_similarity", 0.0)) == 1.0
            and bool(evidence.get("control_state_match"))
            and bool(evidence.get("risk_signature_match"))
            and derive_instance_anchor(expected_model)
            == derive_instance_anchor(capture.model)
        )
    except Exception:
        return False


def _overlay_return_owner(
    *,
    parent: StateWork,
    action: InspectionAction,
    capture: CapturedPage,
    tracked_work: Dict[int, StateWork],
) -> Optional[StateWork]:
    """Resolve a dismissed transient panel back to its owning business state."""
    if (
        str(parent.page_subtype or "UNKNOWN").upper()
        not in {"FILTER_PANEL", "MODAL_PANEL", "PURCHASE_OPTIONS"}
        or (
            str(action.action_type or "").lower() != "back"
            and str(action.action_role or "").upper()
            not in {"FILTER_CLOSE", "DIALOG_CLOSE", "BACK"}
        )
        or parent.parent_state_id is None
    ):
        return None
    owner = tracked_work.get(int(parent.parent_state_id))
    if owner is None or owner.state_id == parent.state_id:
        return None
    return owner if _capture_matches_parent(capture, owner) else None


def _ensure_parent(
    *,
    device,
    parent: StateWork,
    branch_config: Dict[str, Any],
    device_serial: str,
    package_name: str,
    abort_event: threading.Event,
    input_rules: Sequence[Dict[str, Any]],
    dynamic_patterns: Sequence[str],
    stable_wait_seconds: float,
    secret_values: List[str],
    stage_callback: Optional[Callable[[str, str], None]] = None,
    budget_guard: Optional[BudgetGuard] = None,
) -> Optional[CapturedPage]:
    _check_abort(abort_event)
    if stage_callback:
        stage_callback("recover_parent", "验证并恢复父状态")
    parent.recovery_status = None
    if budget_guard is not None:
        budget_guard.before_device_interaction("exact_parent_probe")
    quick_exact = exact_parent_matches(
        device,
        package_name=parent.package_name,
        activity=parent.activity,
        screenshot_sha_value=parent.screenshot_sha,
    )
    if budget_guard is not None:
        budget_guard.check_deadline()

    # Exact screenshot equality remains the cheap shortcut signal, but a miss
    # must not immediately restart the app. Dynamic banners, clocks and small
    # animations routinely change pixels while the business state is still the
    # same. Perform one full, target-package-scoped structural verification on
    # the current page before falling back to entry-case replay.
    if budget_guard is not None:
        budget_guard.before_device_interaction("capture_parent")
    current_capture = _budgeted_wait_for_stable_page(
        device,
        budget_guard=budget_guard,
        expected_package=package_name,
        abort_event=abort_event,
        max_wait_seconds=(
            1.0 if quick_exact else min(stable_wait_seconds, 1.5)
        ),
        dynamic_patterns=dynamic_patterns,
    )
    if _capture_matches_parent(current_capture, parent):
        return current_capture

    try:
        capture, unique = _replay_path(
            device=device,
            path=parent.path,
            branch_config=branch_config,
            device_serial=device_serial,
            package_name=package_name,
            abort_event=abort_event,
            input_rules=input_rules,
            dynamic_patterns=dynamic_patterns,
            stable_wait_seconds=stable_wait_seconds,
            secret_values=secret_values,
            allow_discovery_scroll=True,
            stage_callback=stage_callback,
            budget_guard=budget_guard,
            current_capture=current_capture,
        )
    except PathDiverged:
        parent.recovery_status = PathDiverged.code
        return None
    if not unique or capture is None:
        return None
    if not _capture_matches_parent(capture, parent):
        return None
    return capture


def _mark_branch_started(branch_run_id: int) -> InspectionBranchRun:
    with Session(engine) as session:
        branch = session.get(InspectionBranchRun, branch_run_id)
        if branch is None:
            raise RuntimeError(f"inspection branch missing: {branch_run_id}")
        branch.status = "RUNNING"
        branch.current_stage = "准备业务线"
        branch.started_at = _now()
        session.add(branch)
        session.commit()
        session.refresh(branch)
        return branch


def _finish_branch(branch_run_id: int, outcome: BranchOutcome) -> None:
    with Session(engine) as session:
        branch = session.get(InspectionBranchRun, branch_run_id)
        if branch is None:
            return
        states = session.exec(
            select(InspectionState).where(InspectionState.branch_run_id == branch_run_id)
        ).all()
        transitions = session.exec(
            select(InspectionTransition).where(
                InspectionTransition.branch_run_id == branch_run_id
            )
        ).all()
        faults = session.exec(
            select(InspectionFault).where(
                InspectionFault.branch_run_id == branch_run_id
            )
        ).all()
        branch.status = outcome.status
        branch.current_stage = "完成" if outcome.status != "ABORTED" else "已取消"
        branch.stop_reason = outcome.stop_reason
        branch.state_count = len(states)
        branch.transition_count = len(transitions)
        branch.blocked_count = sum(
            1
            for item in transitions
            if item.status in {"BLOCKED", "COORDINATE_ONLY", "AMBIGUOUS"}
        )
        branch.stable_count = sum(
            1
            for item in states
            if str(item.stable_status or "").upper()
            in {"STABLE", "VERIFIED_TWICE"}
        )
        branch.fault_count = sum(item.occurrence_count for item in faults)
        branch.finished_at = _now()
        session.add(branch)
        session.commit()


def _verify_stable_paths(
    *,
    run_id: int,
    branch_run_id: int,
    device,
    branch_config: Dict[str, Any],
    device_serial: str,
    package_name: str,
    abort_event: threading.Event,
    input_rules: Sequence[Dict[str, Any]],
    dynamic_patterns: Sequence[str],
    stable_wait_seconds: float,
    deadline: float,
    secret_values: List[str],
    stage_callback: Optional[Callable[[str, str], None]] = None,
    budget_guard: Optional[BudgetGuard] = None,
    max_paths: Optional[int] = None,
    representative_only: bool = False,
) -> int:
    with Session(engine) as session:
        states = session.exec(
            select(InspectionState)
            .where(
                InspectionState.run_id == run_id,
                InspectionState.branch_run_id == branch_run_id,
            )
            .order_by(InspectionState.depth, InspectionState.id)
        ).all()
        representative_ids: set[int] = set()
        if representative_only:
            representative_ids = {
                int(item)
                for item in session.exec(
                    select(InspectionExplorationFamily.representative_state_id).where(
                        InspectionExplorationFamily.branch_run_id == branch_run_id,
                        InspectionExplorationFamily.representative_state_id.is_not(None),
                    )
                ).all()
                if item is not None
            }
        template_ids = {state.template_id for state in states if state.template_id}
        templates = {
            int(item.id): item
            for item in (
                session.exec(
                    select(InspectionPageTemplate).where(
                        col(InspectionPageTemplate.id).in_(template_ids)
                    )
                ).all()
                if template_ids
                else []
            )
            if item.id is not None
        }
        if representative_only:
            states = [
                state
                for state in states
                if state.depth == 0
                or (
                    int(state.id) in representative_ids
                    and str(state.expansion_status or "") == "EXPANDED"
                )
            ]
            role_priority = {
                "CHECKOUT": 0,
                "ORDER": 0,
                "PRODUCT_DETAIL": 1,
                "LIST": 3,
                "HOME": 4,
            }
            subtype_priority = {
                "CONSUMABLE_LIST": 2,
                "PRODUCT_LIST": 2,
                "SERVICE_LIST": 2,
                "CATALOG_CATEGORY": 3,
                "HOME": 4,
            }
            states.sort(
                key=lambda state: (
                    min(
                        role_priority.get(
                            str(
                                templates.get(state.template_id).page_role
                                if templates.get(state.template_id) is not None
                                else "UNKNOWN"
                            ),
                            5,
                        ),
                        subtype_priority.get(str(state.page_subtype or "UNKNOWN"), 5),
                    ),
                    state.depth,
                    state.id,
                )
            )
            if max_paths is not None:
                states = states[: max(0, int(max_paths))]
        candidates = [
            (
                state.id,
                state.cluster_key,
                state.semantic_key,
                state.representative_observation_id,
                list(state.first_path or []),
                state.depth,
            )
            for state in states
        ]

        def _step_matches(left: Any, right: Any) -> bool:
            """Match a recorded path prefix without depending on volatile fields."""
            if not isinstance(left, dict) or not isinstance(right, dict):
                return False
            for key in (
                "action_type",
                "action_key",
                "action_role",
                "expected_source_semantic_key",
                "expected_target_semantic_key",
            ):
                left_value = str(left.get(key) or "")
                right_value = str(right.get(key) or "")
                # Legacy paths did not persist the role/semantic fields.  The
                # action key remains the safe fallback for those records.
                if left_value and right_value and left_value != right_value:
                    return False
            return bool(
                str(left.get("action_key") or "")
                == str(right.get("action_key") or "")
            )

        def _checkpoint_states(path: Sequence[Dict[str, Any]]) -> List[Tuple[int, Optional[int]]]:
            """Resolve the exact states visited by a path, including its root.

            Verification historically marked only the endpoint.  That made a
            path whose final action was blocked look as if none of its safe
            prefix had been exercised.  Prefix matching is deliberately based
            on the serialized path and semantic target, rather than a fuzzy
            page match, so duplicate instances remain auditable.
            """
            resolved: List[Tuple[int, Optional[int]]] = []
            for prefix_length in range(len(path) + 1):
                prefix = list(path[:prefix_length])
                expected_key = (
                    str(prefix[-1].get("expected_target_semantic_key") or "")
                    if prefix
                    else ""
                )
                matches = [
                    item
                    for item in states
                    if len(item.first_path or []) == prefix_length
                    and all(
                        _step_matches(recorded, expected)
                        for recorded, expected in zip(item.first_path or [], prefix)
                    )
                    and (
                        not expected_key
                        or str(item.semantic_key or "") == expected_key
                    )
                    and str(item.stable_status or "").upper() != "VIEWPORT"
                ]
                if not matches:
                    continue
                matches.sort(
                    key=lambda item: (
                        str(item.expansion_status or "") == "EXPANDED",
                        item.representative_observation_id is not None,
                        -(int(item.id or 0)),
                    ),
                    reverse=True,
                )
                chosen = matches[0]
                resolved.append(
                    (
                        int(chosen.id),
                        (
                            int(chosen.representative_observation_id)
                            if chosen.representative_observation_id is not None
                            else None
                        ),
                    )
                )
            return resolved

    verified_state_ids: set[int] = set()
    for (
        state_id,
        expected_cluster,
        expected_semantic,
        representative_observation_id,
        path,
        depth,
    ) in candidates:
        _check_abort(abort_event)
        if budget_guard is not None:
            budget_guard.check_deadline()
        elif time.monotonic() >= deadline:
            break
        # A pure scroll only exposes another viewport of the same business
        # page. It is useful for discovery and topology, but it is not a
        # regression target and must not restart the app twice for stability
        # verification.
        if _is_viewport_path(path):
            with Session(engine) as session:
                state = session.get(InspectionState, state_id)
                if state:
                    state.stable_status = "VIEWPORT"
                    state.selected_for_regression = False
                    state.updated_at = _now()
                    session.add(state)
                    session.commit()
            continue
        # Branch preparation proves one arrival only. An empty path has no
        # replayed locator sequence, so it must not be promoted to twice
        # verified merely because the root was used to start exploration.
        if depth == 0 and not path:
            with Session(engine) as session:
                state = session.get(InspectionState, state_id)
                if state:
                    if str(state.stable_status or "").upper() not in {
                        "UNSTABLE",
                        "PATH_DIVERGED",
                    }:
                        state.stable_status = "UNVERIFIED"
                    state.selected_for_regression = False
                    state.updated_at = _now()
                    session.add(state)
                    session.commit()
            continue
        if any(
            bool(item.get("coordinate_only"))
            or not bool(item.get("replayable", True))
            or (
                bool(item.get("risk_type"))
                and str(
                    item.get("status")
                    or item.get("final_status")
                    or item.get("result")
                    or ""
                ).upper() not in {"PASS", "EXECUTED", "SUCCESS"}
                and str(item.get("execution_disposition") or "").upper()
                != "EXECUTED"
            )
            for item in path
        ):
            continue
        try:
            first, first_unique = _replay_path(
                device=device,
                path=path,
                branch_config=branch_config,
                device_serial=device_serial,
                package_name=package_name,
                abort_event=abort_event,
                input_rules=input_rules,
                dynamic_patterns=dynamic_patterns,
                stable_wait_seconds=stable_wait_seconds,
                secret_values=secret_values,
                stage_callback=stage_callback,
                budget_guard=budget_guard,
            )
        except PathDiverged:
            first, first_unique = None, False
        if budget_guard is not None:
            budget_guard.check_deadline()
        elif time.monotonic() >= deadline:
            break
        try:
            second, second_unique = _replay_path(
                device=device,
                path=path,
                branch_config=branch_config,
                device_serial=device_serial,
                package_name=package_name,
                abort_event=abort_event,
                input_rules=input_rules,
                dynamic_patterns=dynamic_patterns,
                stable_wait_seconds=stable_wait_seconds,
                secret_values=secret_values,
                stage_callback=stage_callback,
                budget_guard=budget_guard,
            )
        except PathDiverged:
            second, second_unique = None, False
        stable = bool(
            first_unique
            and second_unique
            and first is not None
            and second is not None
            and (
                _page_logical_key(first.model) == expected_semantic
                if expected_semantic
                else first.model.cluster_key == expected_cluster
            )
            and (
                _page_logical_key(second.model) == expected_semantic
                if expected_semantic
                else second.model.cluster_key == expected_cluster
            )
        )
        checkpoint_states = _checkpoint_states(path) if stable else []
        if stable and not checkpoint_states:
            checkpoint_states = [(int(state_id), representative_observation_id)]
        with Session(engine) as session:
            checkpoint_ids = {
                int(item[0]) for item in checkpoint_states
            }
            # Always retain the endpoint result, even when a legacy path cannot
            # be matched back to all of its prefixes.
            checkpoint_ids.add(int(state_id))
            for checkpoint_id in checkpoint_ids:
                state = session.get(InspectionState, checkpoint_id)
                if state is None:
                    continue
                if str(state.stable_status or "").upper() == "VIEWPORT":
                    continue
                state.stable_status = (
                    "VERIFIED_TWICE"
                    if stable and not list(state.first_path or [])
                    else "STABLE"
                    if stable
                    else "UNSTABLE"
                )
                state.selected_for_regression = stable
                state.updated_at = _now()
                session.add(state)
            session.commit()
        if stable:
            verified_state_ids.update(checkpoint_ids)
            observation_ids = {
                observation_id
                for _, observation_id in checkpoint_states
                if observation_id is not None
            }
            if representative_observation_id is not None:
                observation_ids.add(representative_observation_id)
            for observation_id in observation_ids:
                if observation_id is not None:
                    _pin_observation_assets(observation_id)
    return len(verified_state_ids)


def _execute_branch(
    *,
    run_id: int,
    branch_run_id: int,
    device,
    device_serial: str,
    package_name: str,
    profile: Dict[str, Any],
    branch_config: Dict[str, Any],
    abort_event: threading.Event,
    monitor: Optional[InspectionMonitorSession],
    budget_guard: Optional[BudgetGuard] = None,
) -> BranchOutcome:
    branch = _mark_branch_started(branch_run_id)
    budgets = dict(profile.get("budgets") or {})

    def budget_value(key: str, default: Any) -> Any:
        value = budgets.get(key)
        return default if value is None else value

    branch_guard = budget_guard or BudgetGuard(budgets)
    guard: Any = branch_guard
    max_depth = int(budget_value("max_depth", 12))
    max_scrolls = int(budget_value("max_scrolls_per_direction", 3))
    max_variants = int(budget_value("max_variants_per_cluster", 5))
    stable_wait = float(budget_value("stable_wait_seconds", 5.0))
    identity_v2, similarity_convergence = _identity_options(profile)
    coverage_scheduler, visual_home_actions = _coverage_scheduler_options(profile)
    coverage_scheduler = bool(identity_v2 and coverage_scheduler)
    if coverage_scheduler:
        # Coverage runs should not spend most of their device budget waiting
        # on a long-tail animation. The stable-page sampler still requires a
        # second matching sample; the cap only bounds the fallback timeout.
        stable_wait = min(stable_wait, 3.0)
    coverage_scroll_budget = (
        max(0, int(budget_value("max_coverage_scroll_actions", 50)))
        if coverage_scheduler
        else None
    )
    visual_home_actions = bool(coverage_scheduler and visual_home_actions)
    family_convergence = bool(
        identity_v2
        and (_family_convergence_option(profile) or coverage_scheduler)
    )
    safety_rules = [
        dict(item) for item in profile.get("safety_rules") or [] if isinstance(item, dict)
    ]
    input_rules = [
        dict(item) for item in profile.get("input_rules") or [] if isinstance(item, dict)
    ]
    sanitizer_rules = [
        dict(item)
        for item in profile.get("sanitizer_rules") or []
        if isinstance(item, dict)
    ]
    dynamic_patterns = [
        str(item)
        for item in profile.get("dynamic_text_patterns") or []
        if str(item or "").strip()
    ]
    sanitizer = InspectionArtifactSanitizer(sanitizer_rules)
    guard.before_device_interaction("window_size")
    screen_size = tuple(int(item) for item in device.window_size())
    guard.check_deadline()
    deadline = branch_guard.deadline
    exploration_started_at = time.monotonic()
    exploration_deadline = exploration_started_at + max(
        0.0,
        (deadline - exploration_started_at) * 0.90,
    )
    exploration_device_action_limit = max(
        0,
        int(branch_guard.max_device_actions * 0.90),
    )
    guard = ExplorationBudgetView(
        branch_guard,
        deadline=exploration_deadline,
        max_device_actions=exploration_device_action_limit,
    )
    hard_fault = False
    warning = False
    sequence = 0
    recent_actions: Deque[Dict[str, Any]] = deque(maxlen=20)
    secret_values: List[str] = _environment_secret_values(
        branch_config.get("env_id")
    )
    monitor_index = len(monitor.crash_events) if monitor else 0
    tracked_work: Dict[int, StateWork] = {}
    navigation_anchors: Dict[str, NavigationAnchor] = {}
    navigation_entries: List[NavigationEntry] = []
    queued_state_ids: set[int] = set()
    enqueued_state_ids: set[int] = set()
    expanded_state_ids: set[int] = set()
    attempted_actions: set[Tuple[str, str]] = set()
    ready_captures: Dict[int, CapturedPage] = {}
    traversal_counts: Dict[Tuple[int, str, int], int] = {}
    progress: Dict[str, int] = {
        "states": 0,
        "transitions": 0,
        "actions_total": 0,
        "actions_finished": 0,
        "blocked": 0,
        "faults": 0,
    }
    active_budget_parent: Optional[StateWork] = None
    active_budget_action: Optional[InspectionAction] = None
    active_budget_sequence = 0
    local_warning_reasons: List[str] = []
    termination_reason: Optional[str] = None
    current_phase: Optional[str] = None
    current_stage: Optional[str] = None
    finalize_pending_status = "FILTERED_NON_ACTIONABLE"
    finalize_reason: Optional[str] = "动作未进入可执行探索前沿"
    finalize_phase: Optional[str] = None
    terminal_frontier_status: Optional[str] = None
    deferred_state_ids: set[int] = set()
    frontier_statuses: Dict[int, str] = {}
    family_attempt_signatures: set[Tuple[int, str, int]] = set()
    live_expansion_owner_state_id: Optional[int] = None
    live_expansion_epoch = 0
    stale_live_action_map_state_ids: set[int] = set()
    scroll_invocation_counts: Dict[Tuple[int, str], int] = {}
    coverage_scroll_invocations = 0
    action_group_attempts: Counter[Tuple[str, str]] = Counter()
    sampled_action_groups: set[Tuple[str, str]] = set()
    consecutive_viewport_handoffs = 0
    # Root bottom-navigation destinations form the first coverage boundary.
    # Track their first turn separately so a deep child chain cannot starve
    # the remaining primary surfaces.
    primary_entry_surface_started_ids: set[int] = set()

    def record_observed_navigation(work: StateWork) -> None:
        if not coverage_scheduler:
            return
        for action in work.recovery_navigation_actions:
            navigation = _navigation_metadata(action)
            if (
                not action.action_group_key
                or not _is_unambiguous_active_navigation_action(action)
            ):
                continue
            contract_key, scope = _coverage_contract_identity(
                branch_run_id=branch_run_id,
                source_family_id=work.exploration_family_id,
                source_page_subtype=work.page_subtype,
                action_group_key=action.action_group_key,
                action_role=action.action_role,
            )
            with Session(engine) as session:
                contract = session.exec(
                    select(InspectionCoverageContract).where(
                        InspectionCoverageContract.branch_run_id == branch_run_id,
                        InspectionCoverageContract.contract_key == contract_key,
                    )
                ).first()
                if contract is None:
                    contract = InspectionCoverageContract(
                        run_id=run_id,
                        branch_run_id=branch_run_id,
                        contract_key=contract_key,
                        scope=scope,
                        source_family_id=None,
                        source_page_subtype=str(work.page_subtype or "UNKNOWN"),
                        action_group_key=str(action.action_group_key),
                        action_role=action.action_role,
                        target_family_id=work.exploration_family_id,
                        target_page_role=work.role,
                        status="VERIFIED",
                        required_samples=1,
                        success_count=1,
                        source_instance_anchors=[str(work.instance_anchor or "")],
                        risk_signature=str(action.risk_type or "SAFE"),
                        control_signature=json.dumps(
                            {
                                key: (action.target_meta or {}).get(key)
                                for key in ("enabled", "checked", "selected")
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        created_at=_now(),
                    )
                elif contract.status != "CONFLICT":
                    contract.status = "VERIFIED"
                    contract.success_count = max(1, int(contract.success_count or 0))
                    contract.updated_at = _now()
                session.add(contract)
                session.commit()

    def track_work(work: Optional[StateWork]) -> Optional[StateWork]:
        if work is None:
            return None
        existing = tracked_work.get(work.state_id)
        if existing is not None:
            incoming_action_entries = [
                dict(item)
                for item in work.action_map.get("actions") or []
                if isinstance(item, dict)
            ]
            work.action_map = existing.action_map
            existing_action_keys = {
                str(item.get("action_key") or "")
                for item in existing.action_map.get("actions") or []
                if isinstance(item, dict)
            }
            existing.action_map.setdefault("actions", []).extend(
                item
                for item in incoming_action_entries
                if str(item.get("action_key") or "") not in existing_action_keys
            )
            merged_actions = list(existing.actions)
            known_action_keys = {item.action_key for item in merged_actions}
            for action in work.actions:
                if action.action_key not in known_action_keys:
                    merged_actions.append(action)
                    known_action_keys.add(action.action_key)
            existing.actions = merged_actions
            work.actions = merged_actions
            merged_recovery_actions = list(existing.recovery_navigation_actions)
            known_recovery_keys = {
                item.action_key for item in merged_recovery_actions
            }
            for action in work.recovery_navigation_actions:
                if action.action_key not in known_recovery_keys:
                    merged_recovery_actions.append(action)
                    known_recovery_keys.add(action.action_key)
            existing.recovery_navigation_actions = merged_recovery_actions
            work.recovery_navigation_actions = merged_recovery_actions
        navigation_owner = _navigation_business_owner(work, tracked_work)
        record_observed_navigation(work)
        navigation_actions = [
            *work.actions,
            *work.recovery_navigation_actions,
        ]
        for action in navigation_actions:
            group_key = str(_navigation_metadata(action).get("group_key") or "")
            if not group_key:
                continue
            anchor = navigation_anchors.get(group_key)
            candidate = NavigationAnchor(
                group_key=group_key,
                depth=navigation_owner.depth,
                parent_state_id=navigation_owner.parent_state_id,
            )
            if anchor is None or candidate.depth < anchor.depth:
                navigation_anchors[group_key] = candidate
        for action in navigation_actions:
            navigation = _navigation_metadata(action)
            group_key = str(navigation.get("group_key") or "")
            anchor = navigation_anchors.get(group_key)
            member_index = navigation.get("member_index")
            active_member_indices = navigation.get("active_member_indices")
            if (
                not group_key
                or anchor is None
                or navigation_owner.depth != anchor.depth
                or navigation_owner.parent_state_id != anchor.parent_state_id
                or not isinstance(member_index, int)
                or not isinstance(active_member_indices, list)
                or len(active_member_indices) != 1
                or active_member_indices[0] != member_index
            ):
                continue
            navigation_entries[:] = [
                entry
                for entry in navigation_entries
                if not (
                    entry.group_key == group_key
                    and entry.state_id == navigation_owner.state_id
                )
            ]
            navigation_entries.append(
                NavigationEntry(
                    group_key=group_key,
                    state_id=navigation_owner.state_id,
                    action=action,
                    target_path=tuple(
                        dict(item) for item in navigation_owner.path
                    ),
                )
            )
        if existing is not None:
            if (
                work.depth < existing.depth
                or (
                    work.depth == existing.depth
                    and work.parent_state_id != existing.parent_state_id
                )
            ):
                tracked_work[work.state_id] = work
            return work
        tracked_work[work.state_id] = work
        progress["states"] = len(tracked_work)
        progress["actions_total"] += len(work.actions)
        return work

    def mark_state_runtime(state_id: int, field_name: str) -> None:
        with Session(engine) as session:
            state = session.get(InspectionState, int(state_id))
            if state is None or not hasattr(state, field_name):
                return
            if getattr(state, field_name, None) is None:
                setattr(state, field_name, _now())
                state.updated_at = _now()
                session.add(state)
                session.commit()

    def clear_state_runtime(state_id: int, field_name: str) -> None:
        with Session(engine) as session:
            state = session.get(InspectionState, int(state_id))
            if state is None or not hasattr(state, field_name):
                return
            if getattr(state, field_name, None) is not None:
                setattr(state, field_name, None)
                state.updated_at = _now()
                session.add(state)
                session.commit()

    def update_state_frontier(
        state_id: int,
        *,
        status: Optional[str] = None,
        pending_count: Optional[int] = None,
        action_cursor: Optional[int] = None,
        completed: bool = False,
    ) -> None:
        with Session(engine) as session:
            state = session.get(InspectionState, int(state_id))
            if state is None:
                return
            if status is not None:
                state.expansion_status = str(status)
            if pending_count is not None:
                state.pending_action_count = max(0, int(pending_count))
            if action_cursor is not None:
                state.last_action_cursor = max(0, int(action_cursor))
            if completed:
                state.expansion_completed_at = _now()
            state.updated_at = _now()
            session.add(state)
            session.commit()
        if status is not None:
            frontier_statuses[int(state_id)] = str(status)
        pending_action_count = sum(
            1
            for tracked in tracked_work.values()
            for item in tracked.action_map.get("actions") or []
            if isinstance(item, dict)
            and str(item.get("status") or "").upper()
            in {"PENDING", "ACTIVE", "INVOKED"}
        )
        frontier = {
            "queued_count": sum(
                value == "QUEUED" for value in frontier_statuses.values()
            ),
            "deferred_count": sum(
                value == "DEFERRED" for value in frontier_statuses.values()
            ),
            "pending_action_count": pending_action_count,
            "expanding_count": sum(
                value == "EXPANDING" for value in frontier_statuses.values()
            ),
        }
        _publish_live(
            run_id,
            "FRONTIER_UPDATED",
            branch_key=branch.branch_key,
            phase=current_phase or "explore",
            current_stage=current_stage or "更新探索前沿",
            run_status="RUNNING",
            progress=dict(progress),
            frontier=frontier,
        )

    def pending_actions(
        work: StateWork,
        actions: Optional[Sequence[InspectionAction]] = None,
    ) -> List[InspectionAction]:
        candidates = list(work.actions if actions is None else actions)
        logical_key = _work_logical_key(work)
        return [
            action
            for action in candidates
            if (logical_key, action.action_key) not in attempted_actions
        ]

    def state_actions_terminal(work: StateWork) -> bool:
        canonical = tracked_work.get(work.state_id, work)
        logical_key = _work_logical_key(canonical)
        actions = list(canonical.actions)
        entries = {
            str(item.get("action_key") or ""): item
            for item in canonical.action_map.get("actions") or []
            if isinstance(item, dict)
        }
        nonterminal = {"", "PENDING", "ACTIVE", "INVOKED"}
        for action in actions:
            entry = entries.get(action.action_key)
            if entry is not None:
                if str(entry.get("status") or "").upper() in nonterminal:
                    return False
                continue
            if (logical_key, action.action_key) not in attempted_actions:
                return False
        return True

    def mark_state_expanded_if_terminal(work: StateWork) -> bool:
        if not state_actions_terminal(work):
            return False
        expanded_state_ids.add(work.state_id)
        mark_state_runtime(work.state_id, "expanded_at")
        update_state_frontier(
            work.state_id,
            status="EXPANDED",
            pending_count=0,
            completed=True,
        )
        return True

    def finalize_unqueued_work(
        work: StateWork,
        *,
        action_status: str,
        state_status: str,
        reason: str,
    ) -> None:
        if state_actions_terminal(work):
            mark_state_expanded_if_terminal(work)
            return
        finalize_action_map(
            work.action_map,
            pending_status=action_status,
            reason=reason,
            phase=current_phase or "explore",
        )
        _persist_work_action_map(work)
        if state_status == "EXPANDED":
            expanded_state_ids.add(work.state_id)
            mark_state_runtime(work.state_id, "expanded_at")
        update_state_frontier(
            work.state_id,
            status=state_status,
            pending_count=0,
            completed=True,
        )

    def enqueue(
        work: StateWork,
        *,
        front: bool = False,
        actions: Optional[Sequence[InspectionAction]] = None,
        priority: Optional[int] = None,
        reason: Optional[str] = None,
    ) -> bool:
        remaining = pending_actions(work, actions)
        if actions is not None and not remaining:
            return False
        if work.state_id in expanded_state_ids and not remaining:
            return False
        is_primary_entry_surface = bool(
            coverage_scheduler and _is_primary_entry_surface(work)
        )
        queued_work = (
            work
            if actions is None and len(remaining) == len(work.actions)
            else replace(work, actions=remaining)
        )
        if coverage_scheduler:
            action_roles = {
                str(item.action_role or "") for item in remaining
            }
            if priority is None:
                if is_primary_entry_surface:
                    if work.state_id not in primary_entry_surface_started_ids:
                        priority = 40
                        reason = reason or "PRIMARY_ENTRY_SURFACE"
                    else:
                        # After every primary surface has received its first
                        # turn, let newly discovered business pages (100/200)
                        # and high-value paths (300) progress before returning
                        # to another long root-surface action list.
                        priority = _primary_entry_continuation_priority(work)
                        reason = reason or (
                            "PROFILE_CONTINUATION"
                            if priority == _PROFILE_CONTINUATION_PRIORITY
                            else "PRIMARY_ENTRY_CONTINUATION"
                        )
                elif str(work.exploration_mode or "").upper() == "FULL":
                    priority = _coverage_representative_priority(work)
                    reason = reason or (
                        "LOW_CONFIDENCE_REPRESENTATIVE"
                        if priority >= 500
                        else "NEW_FAMILY_REPRESENTATIVE"
                    )
                elif action_roles & {"BUY_NOW", "CHECKOUT", "PLACE_ORDER"}:
                    priority = 300
                    reason = reason or "HIGH_VALUE_PATH"
                elif any(
                    str(item.sample_policy or "") == "HOME_VISUAL"
                    for item in remaining
                ):
                    priority = 600
                    reason = reason or "HOME_VISUAL_ENTRY"
                elif any(item.action_type != "scroll" for item in remaining):
                    priority = 500
                    reason = reason or "UNCOVERED_ACTION_GROUP"
                elif remaining:
                    priority = 700
                    reason = reason or "COVERAGE_SCROLL"
                else:
                    priority = 800
                    reason = reason or "REPEATED_INSTANCE"
            queued_work.frontier_priority = int(priority)
            queued_work.frontier_reason = str(reason or "COVERAGE_FRONTIER")
            work.frontier_priority = queued_work.frontier_priority
            work.frontier_reason = queued_work.frontier_reason
        if work.state_id in queued_state_ids:
            for index, queued in enumerate(queue):
                if queued.state_id != work.state_id:
                    continue
                queued_keys = {item.action_key for item in queued.actions}
                queued.actions.extend(
                    action
                    for action in remaining
                    if action.action_key not in queued_keys
                )
                if coverage_scheduler:
                    requested_priority = int(queued_work.frontier_priority or 700)
                    if requested_priority < int(queued.frontier_priority or 700):
                        queued.frontier_priority = requested_priority
                        queued.frontier_reason = queued_work.frontier_reason
                    work.frontier_priority = queued.frontier_priority
                    work.frontier_reason = queued.frontier_reason
                    with Session(engine) as session:
                        state = session.get(InspectionState, int(work.state_id))
                        if state is not None:
                            state.frontier_priority = int(queued.frontier_priority)
                            state.frontier_reason = str(queued.frontier_reason)
                            state.updated_at = _now()
                            session.add(state)
                            session.commit()
                if front and index:
                    del queue[index]
                    queue.appendleft(queued)
                update_state_frontier(
                    work.state_id,
                    status="QUEUED",
                    pending_count=len(pending_actions(queued)),
                )
                return True
            queued_state_ids.discard(work.state_id)
        if work.state_id in expanded_state_ids:
            expanded_state_ids.discard(work.state_id)
            clear_state_runtime(work.state_id, "expanded_at")
        if front:
            queue.appendleft(queued_work)
        else:
            queue.append(queued_work)
        queued_state_ids.add(work.state_id)
        enqueued_state_ids.add(work.state_id)
        mark_state_runtime(work.state_id, "queued_at")
        if coverage_scheduler:
            with Session(engine) as session:
                state = session.get(InspectionState, int(work.state_id))
                if state is not None:
                    state.frontier_priority = int(work.frontier_priority)
                    state.frontier_reason = str(work.frontier_reason)
                    state.updated_at = _now()
                    session.add(state)
                    session.commit()
        update_state_frontier(
            work.state_id,
            status="QUEUED",
            pending_count=len(remaining),
        )
        return True

    def family_coverage(
        work: StateWork,
        action: InspectionAction,
        *,
        create: bool = False,
    ) -> Optional[InspectionFamilyActionCoverage]:
        family_pair = _family_action_pair(work, action)
        if (
            not family_convergence
            or family_pair is None
            or str(work.exploration_mode or "").upper() != "DELTA_ONLY"
        ):
            return None
        family_id, coverage_key = family_pair
        with Session(engine) as session:
            row = session.exec(
                select(InspectionFamilyActionCoverage).where(
                    InspectionFamilyActionCoverage.family_id
                    == family_id,
                    InspectionFamilyActionCoverage.action_role_key
                    == coverage_key,
                )
            ).first()
            if row is None and create:
                row = InspectionFamilyActionCoverage(
                    family_id=family_id,
                    action_role_key=coverage_key,
                    action_role=action.action_role,
                    status="PENDING",
                    max_attempts=2,
                    created_at=_now(),
                )
                session.add(row)
                session.commit()
                session.refresh(row)
            if (
                row is not None
                and row.source_state_id is not None
                and int(row.source_state_id) == int(work.state_id)
            ):
                return None
            return row

    def defer_parent_recovery(
        work: StateWork,
        actions: Sequence[InspectionAction],
    ) -> bool:
        if work.recovery_status == PathDiverged.code:
            return False
        with Session(engine) as session:
            state = session.get(InspectionState, work.state_id)
            if state is None or int(state.recovery_retry_count or 0) >= 1:
                return False
            state.recovery_retry_count = int(state.recovery_retry_count or 0) + 1
            state.expansion_status = "DEFERRED"
            state.pending_action_count = len(actions)
            state.updated_at = _now()
            session.add(state)
            session.commit()
        queued = enqueue(work, actions=actions)
        if queued:
            deferred_state_ids.add(work.state_id)
            update_state_frontier(
                work.state_id,
                status="DEFERRED",
                pending_count=len(actions),
            )
            _publish_live(
                run_id,
                "ACTION_DEFERRED",
                branch_key=branch.branch_key,
                phase="recover",
                current_stage="父页面恢复失败，动作已延迟一次",
                run_status="RUNNING",
                overlay_visible=False,
                canvas_matches_panel=False,
                device_context={
                    "phase": "recover",
                    "canvas_matches_panel": False,
                },
                progress=dict(progress),
            )
        return queued

    def recovery_attempt_count(work: StateWork) -> int:
        with Session(engine) as session:
            state = session.get(InspectionState, work.state_id)
            return int(state.recovery_retry_count or 0) if state is not None else 0

    def reset_parent_recovery(work: StateWork) -> None:
        work.recovery_status = None
        with Session(engine) as session:
            state = session.get(InspectionState, work.state_id)
            if state is None or int(state.recovery_retry_count or 0) == 0:
                return
            state.recovery_retry_count = 0
            state.updated_at = _now()
            session.add(state)
            session.commit()

    def scroll_invocation_key(
        work: StateWork,
        action: InspectionAction,
    ) -> Tuple[int, str]:
        owner = _navigation_business_owner(work, tracked_work)
        metadata = action.target_meta or {}
        resource_id = str(metadata.get("resource_id") or "").strip().casefold()
        ancestor = str(metadata.get("ancestor_semantic") or "").strip().casefold()
        if resource_id:
            container = f"resource:{resource_id}"
        elif ancestor:
            container = f"ancestor:{ancestor}"
        else:
            class_name = str(metadata.get("class") or "").strip().casefold()
            region = str(metadata.get("relative_bucket") or "").strip().casefold()
            container = f"geometry:{class_name}:{region}"
        direction_role = str(
            action.action_role
            or metadata.get("direction")
            or action.action_key
        )
        return int(owner.state_id), f"{direction_role}|{container}"

    def update_family_coverage(
        work: StateWork,
        action: InspectionAction,
        *,
        status: str,
        increment_attempt: bool = False,
        error: Optional[str] = None,
    ) -> None:
        family_pair = _family_action_pair(work, action)
        if not family_convergence or family_pair is None:
            return
        family_id, coverage_key = family_pair
        attempt_signature = (
            family_id,
            coverage_key,
            int(work.state_id),
        )
        with Session(engine) as session:
            row = session.exec(
                select(InspectionFamilyActionCoverage).where(
                    InspectionFamilyActionCoverage.family_id
                    == family_id,
                    InspectionFamilyActionCoverage.action_role_key
                    == coverage_key,
                )
            ).first()
            if row is None:
                row = InspectionFamilyActionCoverage(
                    family_id=family_id,
                    action_role_key=coverage_key,
                    action_role=action.action_role,
                    status="PENDING",
                    max_attempts=2,
                    created_at=_now(),
                )
            if increment_attempt and attempt_signature not in family_attempt_signatures:
                family_attempt_signatures.add(attempt_signature)
                row.attempt_count = int(row.attempt_count or 0) + 1
            normalized = str(status or "").upper()
            if normalized == "PASS" or (
                normalized == "SELF_LOOP"
                and not str(action.action_role or "").startswith("CATEGORY_TAB:")
            ):
                row.status = "SUCCESS"
                row.source_state_id = work.state_id
                row.last_error = None
            elif normalized in {
                "LOCATOR_NOT_FOUND",
                "LOCATOR_AMBIGUOUS",
                "COORDINATE_STALE",
                "LOCATOR_DRIFT",
                "AMBIGUOUS",
                "ACTION_ERROR",
                "ERROR",
                "PARENT_RECOVERY_FAILED",
                "UNSTABLE_PARENT",
                "PATH_DIVERGED",
                "NO_EFFECT",
                "SELF_LOOP",
                "APP_EXIT",
                "EXTERNAL_APP",
            }:
                if row.status != "SUCCESS":
                    row.status = "FAILED"
                    row.last_error = _safe_error(error or normalized)
            row.updated_at = _now()
            session.add(row)
            session.commit()

    def set_action_status(
        work: StateWork,
        action: InspectionAction,
        status: str,
        *,
        sequence_value: Optional[int] = None,
        invoked: Optional[bool] = None,
        invocation_unknown: Optional[bool] = None,
        reason: Optional[str] = None,
        error: Optional[str] = None,
        increment_attempt: bool = False,
        execution_disposition: Optional[str] = None,
        failure_type: Optional[str] = None,
        coverage_source_transition_id: Optional[int] = None,
        coverage_contract_id: Optional[int] = None,
        sampling_disposition: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        updated = update_action_map(
            work.action_map,
            action,
            status=status,
            sequence=sequence_value,
            invoked=invoked,
            invocation_unknown=invocation_unknown,
            reason=reason,
            error=error,
            increment_attempt=increment_attempt,
            execution_disposition=execution_disposition,
            failure_type=failure_type,
            coverage_source_transition_id=coverage_source_transition_id,
            coverage_contract_id=coverage_contract_id,
            sampling_disposition=sampling_disposition,
            secret_values=secret_values,
        )
        pending_count = sum(
            1
            for item in work.action_map.get("actions") or []
            if str(item.get("status") or "").upper()
            in {"PENDING", "ACTIVE", "INVOKED"}
        )
        update_state_frontier(
            work.state_id,
            pending_count=pending_count,
            action_cursor=sequence_value,
        )
        if str(execution_disposition or "").upper() != "NOT_REACHED":
            update_family_coverage(
                work,
                action,
                status=status,
                increment_attempt=increment_attempt,
                error=error,
            )
        return updated

    def coverage_contract_for(
        work: StateWork,
        action: InspectionAction,
        *,
        include_catalog_family: bool = False,
    ) -> Optional[InspectionCoverageContract]:
        if (
            not coverage_scheduler
            or not action.action_group_key
            or (
                work.exploration_family_id is None
                and not str(action.action_role or "").startswith("NAV:")
            )
        ):
            return None
        contract_key, _ = _coverage_contract_identity(
            branch_run_id=branch_run_id,
            source_family_id=work.exploration_family_id,
            source_page_subtype=work.page_subtype,
            action_group_key=action.action_group_key,
            action_role=action.action_role,
        )
        with Session(engine) as session:
            contract = session.exec(
                select(InspectionCoverageContract).where(
                    InspectionCoverageContract.branch_run_id == branch_run_id,
                    InspectionCoverageContract.contract_key == contract_key,
                )
            ).first()
            if contract is not None or not include_catalog_family:
                return contract
            if work.page_subtype != "CATALOG_CATEGORY":
                return None
            return session.exec(
                select(InspectionCoverageContract)
                .where(
                    InspectionCoverageContract.branch_run_id == branch_run_id,
                    InspectionCoverageContract.source_family_id
                    == work.exploration_family_id,
                    InspectionCoverageContract.source_page_subtype
                    == "CATALOG_CATEGORY",
                    InspectionCoverageContract.status == "VERIFIED",
                    col(InspectionCoverageContract.action_role).startswith(
                        "ITEM_OPEN:"
                    ),
                )
                .order_by(col(InspectionCoverageContract.id).asc())
            ).first()

    def needs_contract_confirmation(work: StateWork) -> bool:
        """Return whether this captured instance can provide sample two."""
        if not coverage_scheduler or work.exploration_family_id is None:
            return False
        group_keys = {
            str(action.action_group_key)
            for action in pending_actions(work)
            if action.action_group_key
            and str(action.sample_policy or "") == "FAMILY_TWO_SAMPLES"
        }
        instance_anchor = str(work.instance_anchor or "")
        if not group_keys or not instance_anchor:
            return False
        with Session(engine) as session:
            contracts = session.exec(
                select(InspectionCoverageContract).where(
                    InspectionCoverageContract.branch_run_id == branch_run_id,
                    InspectionCoverageContract.source_family_id
                    == work.exploration_family_id,
                    InspectionCoverageContract.source_page_subtype
                    == work.page_subtype,
                    InspectionCoverageContract.status == "PROVISIONAL",
                    col(InspectionCoverageContract.action_group_key).in_(group_keys),
                )
            ).all()
        return any(
            instance_anchor
            not in {
                str(anchor)
                for anchor in (contract.source_instance_anchors or [])
                if str(anchor)
            }
            for contract in contracts
        )

    def sampling_group_signature(
        work: StateWork,
        action: InspectionAction,
    ) -> Tuple[str, str]:
        """Scope representative sampling to a business page instance.

        Sorting, filtering, or switching a category can produce another State
        while retaining the same instance anchor. Using state_id here caused
        every variant to sample the same action group again.
        """
        instance_scope = (
            f"instance:{work.instance_anchor}"
            if work.instance_anchor
            else f"state:{work.state_id}"
        )
        return instance_scope, str(action.action_group_key or action.action_key)

    def sampling_skip(
        work: StateWork,
        action: InspectionAction,
    ) -> Optional[Tuple[str, str, str, Optional[InspectionCoverageContract]]]:
        if not coverage_scheduler or not action.action_group_key:
            return None
        # Closing an already-open overlay is required to restore the device
        # cursor. It is not reusable business coverage and must always run.
        if _is_overlay_cleanup_action(action):
            return None
        group_signature = sampling_group_signature(work, action)
        role = str(action.action_role or "")
        contract = coverage_contract_for(
            work,
            action,
            include_catalog_family=bool(
                work.page_subtype == "CATALOG_CATEGORY"
                and (
                    role.startswith(("ITEM_OPEN:", "CATEGORY_TAB:", "SORT:", "SCROLL:"))
                    or role == "FILTER_OPEN"
                )
            ),
        )
        if contract is not None and contract.status == "VERIFIED":
            if role.startswith("NAV:"):
                return (
                    "NAVIGATION_REUSED",
                    "该底部导航目的地已在本业务线访问",
                    "NAVIGATION_REUSED",
                    contract,
                )
            return (
                "COVERED_BY_CONTRACT",
                "覆盖契约已由不同页面实例验证",
                "CONTRACT_REUSED",
                contract,
            )
        if (
            str(action.sample_policy or "")
            in {"PAGE_ONE", "FAMILY_ONE", "FAMILY_TWO_SAMPLES"}
            and group_signature in sampled_action_groups
        ):
            return (
                "SAMPLED_OUT",
                "同一页面动作组已成功采样一个代表控件",
                "SAMPLED_OUT",
                contract,
            )
        if (
            str(action.sample_policy or "")
            in {"PAGE_ONE", "FAMILY_ONE", "FAMILY_TWO_SAMPLES"}
            and action_group_attempts[group_signature] >= 2
        ):
            return (
                "SAMPLED_OUT",
                "动作组已尝试两个候选，停止遍历其余同组控件",
                "SAMPLED_OUT",
                contract,
            )
        return None

    def finalize_capture_reuse(work: StateWork) -> bool:
        """Finalize already-covered actions while the target capture is current.

        A DELTA_ONLY page used to enter the normal frontier with every action
        still pending, even when its family or coverage contract could already
        explain all of them.  That forced a later path replay merely to write
        skip results.  Set those terminal results at discovery time and only
        queue actions that can still add device coverage.
        """
        nonlocal sequence
        finalized_any = False
        for action in pending_actions(work):
            resolution = sampling_skip(work, action)
            source_transition_id: Optional[int] = None
            coverage_contract_id: Optional[int] = None
            if resolution is not None:
                status, reason, disposition, contract = resolution
                if contract is not None:
                    coverage_contract_id = int(contract.id)
                    if contract.sample_transition_ids:
                        source_transition_id = int(
                            contract.sample_transition_ids[-1]
                        )
            else:
                coverage = family_coverage(work, action)
                if (
                    coverage is not None
                    and coverage.status == "SUCCESS"
                    and coverage.source_transition_id is None
                ):
                    transition_buffer.flush()
                    coverage = family_coverage(work, action)
                if (
                    coverage is None
                    or coverage.status != "SUCCESS"
                    or coverage.source_transition_id is None
                ):
                    continue
                status = "COVERED_BY_FAMILY"
                reason = "同构页面族中的相同动作已覆盖"
                disposition = "FAMILY_REUSED"
                source_transition_id = int(coverage.source_transition_id)

            sequence += 1
            attempted_actions.add((_work_logical_key(work), action.action_key))
            set_action_status(
                work,
                action,
                status,
                sequence_value=sequence,
                reason=reason,
                execution_disposition=disposition,
                coverage_source_transition_id=source_transition_id,
                coverage_contract_id=coverage_contract_id,
                sampling_disposition=disposition,
            )
            transition_buffer.append(
                _transition_payload(
                    from_state_id=work.state_id,
                    sequence=sequence,
                    action=action,
                    status=status,
                    reason=reason,
                    execution_disposition=disposition,
                    coverage_source_transition_id=source_transition_id,
                    coverage_contract_id=coverage_contract_id,
                    sampling_disposition=disposition,
                    source_observation_id=work.observation_id,
                ),
                None,
            )
            _publish_live(
                run_id,
                (
                    "ACTION_COVERED_BY_CONTRACT"
                    if status == "COVERED_BY_CONTRACT"
                    else "ACTION_NAVIGATION_REUSED"
                    if status == "NAVIGATION_REUSED"
                    else "ACTION_SAMPLED_OUT"
                    if status == "SAMPLED_OUT"
                    else "ACTION_COVERED_BY_FAMILY"
                ),
                branch_key=branch.branch_key,
                phase=current_phase or "coverage_explore",
                current_stage="采集时复用已验证覆盖",
                run_status="RUNNING",
                progress=dict(progress),
            )
            finalized_any = True

        if not finalized_any:
            return False
        transition_buffer.flush()
        _persist_work_action_map(work)
        return mark_state_expanded_if_terminal(work)

    def publish_stage(phase: str, stage: str) -> None:
        nonlocal current_phase, current_stage
        phase_changed = str(phase or "") != str(current_phase or "")
        current_phase = str(phase or "")
        current_stage = str(stage or "")
        with Session(engine) as session:
            current_branch = session.get(InspectionBranchRun, branch_run_id)
            if current_branch is not None:
                current_branch.current_stage = current_stage
                session.add(current_branch)
                session.commit()
        if phase_changed:
            _publish_live(
                run_id,
                "PHASE_CHANGED",
                branch_key=branch.branch_key,
                phase=current_phase,
                current_stage=current_stage,
                run_status="RUNNING",
                overlay_visible=False,
                canvas_matches_panel=False,
                device_context={
                    "phase": current_phase,
                    "canvas_matches_panel": False,
                },
                progress=dict(progress),
            )
        _publish_live(
            run_id,
            "OVERLAY_CLEAR",
            branch_key=branch.branch_key,
            phase=phase,
            current_stage=stage,
            run_status="RUNNING",
            canvas_matches_panel=False,
            device_context={
                "phase": phase,
                "canvas_matches_panel": False,
            },
            progress=dict(progress),
        )
        _publish_live(
            run_id,
            "RUN_STAGE",
            branch_key=branch.branch_key,
            phase=phase,
            current_stage=stage,
            run_status="RUNNING",
            progress=dict(progress),
        )

    def live_action_panel_patch(
        work: StateWork,
        *,
        current_action: Optional[Dict[str, Any]] = None,
        canvas_matches_panel: bool,
        activate: bool = False,
        expansion_status: str = "EXPANDING",
    ) -> Dict[str, Any]:
        nonlocal live_expansion_owner_state_id, live_expansion_epoch
        if activate and live_expansion_owner_state_id != work.state_id:
            live_expansion_epoch += 1
            live_expansion_owner_state_id = work.state_id
        if live_expansion_owner_state_id != work.state_id:
            return {}
        canvas_matches_panel = bool(
            canvas_matches_panel
            and work.state_id not in stale_live_action_map_state_ids
        )
        page = _live_page(work)
        actions = _live_actions(work)
        panel = {
            "state_id": work.state_id,
            "expansion_epoch": live_expansion_epoch,
            "expansion_status": expansion_status,
            "page": page,
            "actions": actions,
            "current_action": current_action,
            "canvas_matches_panel": bool(canvas_matches_panel),
        }
        return {
            # Keep the v1 top-level fields as a compatibility mirror for two
            # releases.  New clients use action_panel as the stable owner.
            "page": page,
            "actions": actions,
            "current_action": current_action,
            "action_panel": panel,
            "expansion_owner_state_id": work.state_id,
            "expansion_epoch": live_expansion_epoch,
            "canvas_matches_panel": bool(canvas_matches_panel),
            "device_context": {
                **({"state_id": work.state_id} if canvas_matches_panel else {}),
                "activity": work.activity,
                "foreground_package": work.package_name,
                "phase": current_phase or "explore",
                "canvas_matches_panel": bool(canvas_matches_panel),
            },
        }

    def publish_page_actions(
        work: StateWork,
        capture: Optional[CapturedPage] = None,
    ) -> None:
        if capture is not None and capture.screenshot_sha == work.screenshot_sha:
            stale_live_action_map_state_ids.discard(work.state_id)
        panel_patch = live_action_panel_patch(
            work,
            current_action=None,
            canvas_matches_panel=True,
            activate=True,
        )
        _publish_live(
            run_id,
            "PAGE_ACTIONS",
            branch_key=branch.branch_key,
            phase=current_phase or "explore",
            current_stage="探索页面动作",
            run_status="RUNNING",
            **panel_patch,
            overlay_visible=bool(panel_patch.get("canvas_matches_panel")),
            progress=dict(progress),
        )

    def transition_observer(
        payload: Dict[str, Any],
        target_state_id: Optional[int],
    ) -> None:
        progress["transitions"] += 1
        progress["actions_finished"] += 1
        if str(payload.get("status") or "") in {
            "BLOCKED",
            "COORDINATE_ONLY",
            "AMBIGUOUS",
        }:
            progress["blocked"] += 1
        work = tracked_work.get(int(payload.get("from_state_id") or 0))
        target_work = tracked_work.get(int(target_state_id or 0))
        current_action = _live_action(work, payload.get("action_key"))
        if current_action is None:
            current_action = {
                "action_key": payload.get("action_key"),
                "action_type": payload.get("action_type"),
                "global_sequence": payload.get("sequence"),
                "status": payload.get("status"),
                "risk_type": payload.get("risk_type"),
                "coordinate_only": payload.get("coordinate_only"),
                "replayable": payload.get("replayable"),
                "reason": "动作已结束" if payload.get("reason") else None,
                "error": (
                    "动作执行异常" if payload.get("error_message") else None
                ),
            }
        status = str(payload.get("status") or "").upper()
        same_page = bool(
            work is not None
            and target_work is not None
            and work.state_id == target_work.state_id
        )
        overlay_still_matches = same_page or status in {
            "BLOCKED",
            "COORDINATE_ONLY",
            "NO_EFFECT",
            "SKIPPED",
            "AMBIGUOUS",
            "LOCATOR_AMBIGUOUS",
            "LOCATOR_NOT_FOUND",
            "COORDINATE_UNSAFE",
            "COVERED_BY_FAMILY",
            "COVERAGE_EXHAUSTED",
            "UNSTABLE_PARENT",
        }
        panel_patch = (
            live_action_panel_patch(
                work,
                current_action=current_action,
                canvas_matches_panel=overlay_still_matches,
            )
            if work is not None
            else {}
        )
        _publish_live(
            run_id,
            "ACTION_FINISHED",
            branch_key=branch.branch_key,
            phase=current_phase or "explore",
            current_stage="记录动作结果",
            run_status="RUNNING",
            **panel_patch,
            overlay_visible=bool(panel_patch.get("canvas_matches_panel")),
            progress=dict(progress),
        )

    transition_buffer = TransitionBuffer(
        run_id,
        branch_run_id,
        on_append=transition_observer,
        coverage_scheduler=coverage_scheduler,
    )

    def drain_monitor_events(state_id: Optional[int]) -> int:
        nonlocal monitor_index, hard_fault
        if monitor is None:
            return 0
        events = monitor.snapshot_events(monitor_index)
        monitor_index += len(events)
        for event_item in events:
            fault_type = str(event_item.get("type") or "CRASH").upper()
            _persist_fault(
                run_id=run_id,
                branch_run_id=branch_run_id,
                state_id=state_id,
                fault_type=fault_type,
                summary=f"检测到 {fault_type}",
                event=event_item,
                recent_actions=list(recent_actions),
                secret_values=secret_values,
                device_serial=device_serial,
                budget_guard=guard,
            )
            hard_fault = True
            progress["faults"] += 1
        return len(events)

    def verify_representative_paths() -> int:
        """Switch to the task reserve before any verification interaction."""
        nonlocal guard
        guard = branch_guard
        publish_stage(
            "representative_verification" if coverage_scheduler else "verify",
            "代表验证" if coverage_scheduler else "验证稳定路径",
        )
        verified = _verify_stable_paths(
            run_id=run_id,
            branch_run_id=branch_run_id,
            device=device,
            branch_config=branch_config,
            device_serial=device_serial,
            package_name=package_name,
            abort_event=abort_event,
            input_rules=input_rules,
            dynamic_patterns=dynamic_patterns,
            stable_wait_seconds=stable_wait,
            deadline=deadline,
            secret_values=secret_values,
            budget_guard=branch_guard,
            max_paths=10 if coverage_scheduler else None,
            representative_only=coverage_scheduler,
        )
        drain_monitor_events(next(iter(tracked_work), None))
        return verified

    try:
        publish_stage("prepare", "准备业务线")
        _prepare_branch(
            device=device,
            branch_config=branch_config,
            device_serial=device_serial,
            abort_event=abort_event,
            stage_callback=publish_stage,
            budget_guard=guard,
        )
        publish_stage("stabilizing", "等待根页面稳定")
        guard.before_device_interaction("capture_root")
        root_capture = _budgeted_wait_for_stable_page(
            device,
            budget_guard=guard,
            expected_package=package_name,
            abort_event=abort_event,
            max_wait_seconds=stable_wait,
            dynamic_patterns=dynamic_patterns,
        )
        if root_capture.package_name != package_name:
            raise BranchPreparationFailed(
                f"根页面前台包名不一致: {root_capture.package_name or '-'}"
            )
        root = _persist_state(
            run_id=run_id,
            branch_run=branch,
            capture=root_capture,
            depth=0,
            parent_state_id=None,
            path=[],
            sanitizer=sanitizer,
            screen_size=screen_size,
            safety_rules=safety_rules,
            input_rules=input_rules,
            max_scrolls=max_scrolls,
            max_variants=max_variants,
            secret_values=secret_values,
            mark_branch_root=True,
            identity_v2=identity_v2,
            similarity_convergence=similarity_convergence,
            family_convergence=family_convergence,
            coverage_scheduler=coverage_scheduler,
            visual_home_actions=visual_home_actions,
            budget_guard=guard,
        )
        if not root.work:
            raise RuntimeError("无法保存巡检根状态")
        track_work(root.work)
        publish_stage(
            "entry_survey" if coverage_scheduler else "explore",
            "入口普查" if coverage_scheduler else "广度优先探索",
        )
        branch.root_state_id = root.work.state_id
        if not root.work.semantic_key:
            root.work.semantic_key = str(
                root_capture.model.semantic_key
                if identity_v2
                else root_capture.model.replay_key
            )
        if not root.work.ancestry_state_ids:
            root.work.ancestry_state_ids = (root.work.state_id,)

        queue: Deque[StateWork] = deque()
        enqueue(root.work)
        current_device_path: Optional[List[Dict[str, Any]]] = None
        frontier_reconcile_failures = 0
        last_frontier_reconcile_sequence: Optional[int] = None
        frontier_incomplete_ids: set[int] = set()

        while True:
            exploration_limit_reason: Optional[str] = None
            if time.monotonic() >= exploration_deadline:
                exploration_limit_reason = "探索阶段 90% 时间预算已用完"
            elif (
                guard.device_actions >= exploration_device_action_limit
            ):
                exploration_limit_reason = "探索阶段 90% 动作预算已用完"
            if exploration_limit_reason is not None:
                warning = True
                local_warning_reasons.append(exploration_limit_reason)
                termination_reason = exploration_limit_reason
                for unfinished in tracked_work.values():
                    if state_actions_terminal(unfinished):
                        mark_state_expanded_if_terminal(unfinished)
                        continue
                    finalize_unqueued_work(
                        unfinished,
                        action_status="BUDGET_NOT_REACHED",
                        state_status="BUDGET_SKIPPED",
                        reason=(
                            f"{exploration_limit_reason}，保留最后 10% 用于验证代表路径"
                        ),
                    )
                queue.clear()
                queued_state_ids.clear()
                _publish_live(
                    run_id,
                    "PHASE_CHANGED",
                    branch_key=branch.branch_key,
                    phase=(
                        "representative_verification"
                        if coverage_scheduler
                        else "verify"
                    ),
                    current_stage=exploration_limit_reason,
                    run_status="RUNNING",
                    progress=dict(progress),
                )
                break
            if not queue:
                unresolved = (
                    set(queued_state_ids)
                    | {
                        state_id
                        for state_id, work in tracked_work.items()
                        if state_id in enqueued_state_ids
                        and state_id not in expanded_state_ids
                        and not state_actions_terminal(work)
                    }
                )
                if not unresolved:
                    break
                if last_frontier_reconcile_sequence is not None:
                    if sequence > last_frontier_reconcile_sequence:
                        frontier_reconcile_failures = 0
                    else:
                        frontier_reconcile_failures += 1
                        if frontier_reconcile_failures >= 2:
                            frontier_incomplete_ids = set(unresolved)
                            break
                queued_state_ids.difference_update(unresolved)
                recovered_state_ids: List[int] = []
                for state_id in sorted(unresolved):
                    work = tracked_work.get(state_id)
                    if work is not None and enqueue(
                        work,
                        actions=pending_actions(work),
                    ):
                        recovered_state_ids.append(state_id)
                _publish_live(
                    run_id,
                    "FRONTIER_UPDATED",
                    branch_key=branch.branch_key,
                    phase=current_phase or "explore",
                    current_stage="重建丢失的探索前沿",
                    run_status="RUNNING",
                    progress=dict(progress),
                )
                if recovered_state_ids:
                    current_device_path = None
                last_frontier_reconcile_sequence = sequence
                continue

            _check_abort(abort_event)
            guard.check_deadline()

            parent = (
                _pop_most_local(
                    queue,
                    current_device_path,
                    coverage_scheduler=True,
                )
                if coverage_scheduler
                else _pop_most_local(queue, current_device_path)
            )
            active_budget_parent = None
            active_budget_action = None
            queued_state_ids.discard(parent.state_id)
            if coverage_scheduler and _is_primary_entry_surface(parent):
                primary_entry_surface_started_ids.add(int(parent.state_id))
            if (
                coverage_scheduler
                and current_phase == "entry_survey"
                and parent.state_id != root.work.state_id
            ):
                publish_stage("coverage_explore", "覆盖探索")
            candidate_handoff = ready_captures.get(parent.state_id)
            if candidate_handoff is not None and _paths_equivalent(
                parent.path,
                current_device_path or (),
            ):
                handoff_capture = ready_captures.pop(parent.state_id)
            else:
                # A capture is only safe to reuse while the device is still on
                # the exact path that produced it. Queue priority may defer a
                # child for several turns; replay its path when the cursor has
                # moved instead of executing stale coordinates on another page.
                ready_captures.pop(parent.state_id, None)
                handoff_capture = None
            if handoff_capture is None:
                consecutive_viewport_handoffs = 0
            if parent.state_id in expanded_state_ids:
                deferred_state_ids.discard(parent.state_id)
                continue
            resumed = parent.state_id in deferred_state_ids
            deferred_state_ids.discard(parent.state_id)
            update_state_frontier(
                parent.state_id,
                status="EXPANDING",
                pending_count=len(pending_actions(parent)),
            )
            if resumed:
                _publish_live(
                    run_id,
                    "ACTION_RESUMED",
                    branch_key=branch.branch_key,
                    phase="recover",
                    current_stage="恢复延迟动作",
                    run_status="RUNNING",
                    overlay_visible=False,
                    canvas_matches_panel=False,
                    device_context={
                        "phase": "recover",
                        "canvas_matches_panel": False,
                    },
                    progress=dict(progress),
                )
            # A ready capture is written only for the viewport State returned by
            # the immediately preceding persistence call. Trust that association
            # instead of replaying a just-completed scroll because dynamic HOME
            # content can legitimately alter the exact fingerprint meanwhile.
            parent_capture = handoff_capture
            if parent_capture is None:
                parent_capture = _ensure_parent(
                    device=device,
                    parent=parent,
                    branch_config=branch_config,
                    device_serial=device_serial,
                    package_name=package_name,
                    abort_event=abort_event,
                    input_rules=input_rules,
                    dynamic_patterns=dynamic_patterns,
                    stable_wait_seconds=stable_wait,
                    secret_values=secret_values,
                    stage_callback=publish_stage,
                    budget_guard=guard,
                )
            if parent_capture is not None:
                reset_parent_recovery(parent)
            if parent_capture is None:
                warning = True
                if defer_parent_recovery(parent, parent.actions):
                    local_warning_reasons.append("父状态恢复失败，已延迟一次")
                    current_device_path = None
                    continue
                recovery_status = (
                    "PATH_DIVERGED"
                    if parent.recovery_status == PathDiverged.code
                    else "PARENT_RECOVERY_FAILED"
                )
                recovery_attempts = recovery_attempt_count(parent)
                recovery_reason = (
                    "回放路径已偏离，父状态动作未执行"
                    if recovery_status == PathDiverged.code
                    else "父状态恢复重试已耗尽"
                    if recovery_attempts >= 1
                    else "父状态恢复失败，动作未执行"
                )
                local_warning_reasons.append(
                    "回放路径偏离" if recovery_status == PathDiverged.code else "父状态恢复失败"
                )
                for recovery_index, action in enumerate(parent.actions):
                    action_status = (
                        "QUEUE_TRUNCATED"
                        if recovery_status == PathDiverged.code
                        and recovery_index > 0
                        else recovery_status
                    )
                    action_reason = (
                        "父状态回放路径已偏离，后续动作未进入执行阶段"
                        if action_status == "QUEUE_TRUNCATED"
                        else recovery_reason
                    )
                    failure_type = (
                        "PATH_DIVERGED_CASCADE"
                        if action_status == "QUEUE_TRUNCATED"
                        else action_status
                    )
                    attempted_actions.add(
                        (_work_logical_key(parent), action.action_key)
                    )
                    sequence += 1
                    set_action_status(
                        parent,
                        action,
                        action_status,
                        sequence_value=sequence,
                        reason=action_reason,
                        execution_disposition="NOT_REACHED",
                        failure_type=failure_type,
                    )
                    transition_buffer.append(
                        _transition_payload(
                            from_state_id=parent.state_id,
                            sequence=sequence,
                            action=action,
                            status=action_status,
                            reason=action_reason,
                            execution_disposition="NOT_REACHED",
                            failure_type=failure_type,
                            recovery_attempt_count=recovery_attempts,
                        ),
                        None,
                    )
                transition_buffer.flush()
                _persist_work_action_map(parent)
                mark_state_expanded_if_terminal(parent)
                current_device_path = None
                continue

            if str(current_phase or "").startswith(("recover", "replay")):
                publish_stage(
                    "coverage_explore" if coverage_scheduler else "explore",
                    "覆盖探索" if coverage_scheduler else "探索页面",
                )
            publish_page_actions(parent, parent_capture)
            invoked_coordinate_targets = set()
            blocked_coordinate_targets = {
                key
                for item in parent.actions
                if item.risk_type
                for key in [coordinate_target_key(item)]
                if key is not None
            }
            parent_recovery_exhausted = False
            viewport_handoff_path: Optional[List[Dict[str, Any]]] = None
            priority_handoff_path: Optional[List[Dict[str, Any]]] = None
            rebind_capture: Optional[CapturedPage] = None

            for action_index, action in enumerate(parent.actions):
                _check_abort(abort_event)
                if (
                    guard.device_actions >= exploration_device_action_limit
                ):
                    # Leave this and subsequent actions pending. The outer loop
                    # finalizes the frontier consistently before entering the
                    # reserved representative-verification budget.
                    break
                sequence += 1
                active_budget_parent = parent
                active_budget_action = action
                active_budget_sequence = sequence
                guard.check_deadline()

                attempted_key = (_work_logical_key(parent), action.action_key)
                if (
                    attempted_key in attempted_actions
                    and not _is_overlay_cleanup_action(action)
                ):
                    reason = "同一逻辑页面动作已尝试，不再重复执行"
                    set_action_status(
                        parent,
                        action,
                        "SKIPPED",
                        sequence_value=sequence,
                        reason=reason,
                    )
                    transition_buffer.append(
                        _transition_payload(
                            from_state_id=parent.state_id,
                            sequence=sequence,
                            action=action,
                            status="SKIPPED",
                            reason=reason,
                            source_observation_id=parent.observation_id,
                        ),
                        None,
                    )
                    continue
                attempted_actions.add(attempted_key)

                if action.risk_type:
                    set_action_status(
                        parent,
                        action,
                        "BLOCKED",
                        sequence_value=sequence,
                        reason=action.blocked_reason or action.risk_type,
                        execution_disposition="SKIPPED",
                        failure_type="SAFETY_BLOCKED",
                    )
                    transition_buffer.append(
                        _transition_payload(
                            from_state_id=parent.state_id,
                            sequence=sequence,
                            action=action,
                            status="BLOCKED",
                            reason=action.blocked_reason or action.risk_type,
                            execution_disposition="SKIPPED",
                            failure_type="SAFETY_BLOCKED",
                        ),
                        None,
                    )
                    warning = warning or action.risk_type == "UNMAPPED_INPUT"
                    continue
                if (
                    action.coordinate_only
                    and action.action_type == "click"
                    and not bool(action.target_meta.get("coordinate_authorized"))
                ):
                    reason = "纯坐标动作未被安全规则显式放行"
                    set_action_status(
                        parent,
                        action,
                        "COORDINATE_UNSAFE",
                        sequence_value=sequence,
                        reason=reason,
                        execution_disposition="SKIPPED",
                        failure_type="COORDINATE_UNSAFE",
                    )
                    transition_buffer.append(
                        _transition_payload(
                            from_state_id=parent.state_id,
                            sequence=sequence,
                            action=action,
                            status="COORDINATE_UNSAFE",
                            reason=reason,
                            execution_disposition="SKIPPED",
                            failure_type="COORDINATE_UNSAFE",
                            source_observation_id=parent.observation_id,
                        ),
                        None,
                    )
                    continue
                sample_skip = sampling_skip(parent, action)
                if sample_skip is not None:
                    (
                        skip_status,
                        skip_reason,
                        skip_disposition,
                        skip_contract,
                    ) = sample_skip
                    source_transition_id = (
                        int(skip_contract.sample_transition_ids[-1])
                        if skip_contract is not None
                        and skip_contract.sample_transition_ids
                        else None
                    )
                    set_action_status(
                        parent,
                        action,
                        skip_status,
                        sequence_value=sequence,
                        reason=skip_reason,
                        execution_disposition=skip_disposition,
                        coverage_source_transition_id=source_transition_id,
                        coverage_contract_id=(
                            skip_contract.id if skip_contract is not None else None
                        ),
                        sampling_disposition=skip_disposition,
                    )
                    transition_buffer.append(
                        _transition_payload(
                            from_state_id=parent.state_id,
                            sequence=sequence,
                            action=action,
                            status=skip_status,
                            reason=skip_reason,
                            execution_disposition=skip_disposition,
                            coverage_source_transition_id=source_transition_id,
                            coverage_contract_id=(
                                skip_contract.id
                                if skip_contract is not None
                                else None
                            ),
                            sampling_disposition=skip_disposition,
                            source_observation_id=parent.observation_id,
                        ),
                        None,
                    )
                    _publish_live(
                        run_id,
                        (
                            "ACTION_COVERED_BY_CONTRACT"
                            if skip_status == "COVERED_BY_CONTRACT"
                            else "ACTION_NAVIGATION_REUSED"
                            if skip_status == "NAVIGATION_REUSED"
                            else "ACTION_SAMPLED_OUT"
                        ),
                        branch_key=branch.branch_key,
                        phase="coverage_explore",
                        current_stage="复用已验证覆盖" if skip_contract else "代表采样已完成",
                        run_status="RUNNING",
                        progress=dict(progress),
                    )
                    continue
                coverage = family_coverage(parent, action)
                if coverage is not None and coverage.status == "SUCCESS":
                    if coverage.source_transition_id is None:
                        transition_buffer.flush()
                        coverage = family_coverage(parent, action)
                if (
                    coverage is not None
                    and coverage.status == "SUCCESS"
                    and coverage.source_transition_id is not None
                ):
                    reason = "同构页面族中的相同动作已覆盖"
                    set_action_status(
                        parent,
                        action,
                        "COVERED_BY_FAMILY",
                        sequence_value=sequence,
                        reason=reason,
                        execution_disposition="FAMILY_REUSED",
                        coverage_source_transition_id=coverage.source_transition_id,
                    )
                    transition_buffer.append(
                        _transition_payload(
                            from_state_id=parent.state_id,
                            sequence=sequence,
                            action=action,
                            status="COVERED_BY_FAMILY",
                            reason=reason,
                            execution_disposition="FAMILY_REUSED",
                            coverage_source_transition_id=(
                                coverage.source_transition_id
                            ),
                            source_observation_id=parent.observation_id,
                        ),
                        None,
                    )
                    panel_patch = live_action_panel_patch(
                        parent,
                        current_action=_live_action(
                            parent,
                            action.action_key,
                        ),
                        canvas_matches_panel=True,
                    )
                    _publish_live(
                        run_id,
                        "ACTION_COVERED_BY_FAMILY",
                        branch_key=branch.branch_key,
                        phase=current_phase or "explore",
                        current_stage="复用同构页面族动作覆盖",
                        run_status="RUNNING",
                        **panel_patch,
                        overlay_visible=bool(
                            panel_patch.get("canvas_matches_panel")
                        ),
                        progress=dict(progress),
                    )
                    continue
                if (
                    coverage is not None
                    and coverage.status == "FAILED"
                    and int(coverage.attempt_count or 0)
                    >= int(coverage.max_attempts or 2)
                ):
                    reason = "同构动作已达到跨页面尝试上限"
                    set_action_status(
                        parent,
                        action,
                        "COVERAGE_EXHAUSTED",
                        sequence_value=sequence,
                        reason=reason,
                        execution_disposition="SKIPPED",
                        failure_type="COVERAGE_EXHAUSTED",
                    )
                    transition_buffer.append(
                        _transition_payload(
                            from_state_id=parent.state_id,
                            sequence=sequence,
                            action=action,
                            status="COVERAGE_EXHAUSTED",
                            reason=reason,
                            execution_disposition="SKIPPED",
                            failure_type="COVERAGE_EXHAUSTED",
                            source_observation_id=parent.observation_id,
                        ),
                        None,
                    )
                    continue
                if action.coordinate_only and action.action_type not in {
                    "click",
                    "scroll",
                }:
                    set_action_status(
                        parent,
                        action,
                        "COORDINATE_ONLY",
                        sequence_value=sequence,
                        reason="无稳定 description/text/约束 XPath，仅记录不回放",
                    )
                    transition_buffer.append(
                        _transition_payload(
                            from_state_id=parent.state_id,
                            sequence=sequence,
                            action=action,
                            status="COORDINATE_ONLY",
                            reason="无稳定 description/text/约束 XPath，仅记录不回放",
                        ),
                        None,
                    )
                    warning = True
                    continue
                scroll_key: Optional[Tuple[int, str]] = None
                if action.action_type == "scroll":
                    scroll_key = scroll_invocation_key(parent, action)
                    count = max(
                        scroll_invocation_counts.get(scroll_key, 0),
                        _consecutive_scroll_repetitions(
                            parent.path,
                            action,
                        ),
                    )
                    if count >= max_scrolls:
                        set_action_status(
                            parent,
                            action,
                            "SKIPPED",
                            sequence_value=sequence,
                            reason="达到该滚动方向次数上限",
                        )
                        transition_buffer.append(
                            _transition_payload(
                                from_state_id=parent.state_id,
                                sequence=sequence,
                                action=action,
                                status="SKIPPED",
                                reason="达到该滚动方向次数上限",
                            ),
                            None,
                        )
                        continue
                    if (
                        coverage_scroll_budget is not None
                        and coverage_scroll_invocations >= coverage_scroll_budget
                    ):
                        reason = "达到本次覆盖探索滚动总预算"
                        warning = True
                        if reason not in local_warning_reasons:
                            local_warning_reasons.append(reason)
                        if termination_reason is None:
                            termination_reason = reason
                        set_action_status(
                            parent,
                            action,
                            "NO_NEW_COVERAGE",
                            sequence_value=sequence,
                            reason=reason,
                            execution_disposition="SKIPPED",
                            failure_type="COVERAGE_SCROLL_LIMIT",
                        )
                        transition_buffer.append(
                            _transition_payload(
                                from_state_id=parent.state_id,
                                sequence=sequence,
                                action=action,
                                status="NO_NEW_COVERAGE",
                                reason=reason,
                                execution_disposition="SKIPPED",
                                failure_type="COVERAGE_SCROLL_LIMIT",
                            ),
                            None,
                        )
                        continue

                parent_was_recovered = False
                if (
                    not parent_recovery_exhausted
                    and not _capture_matches_parent(parent_capture, parent)
                ):
                    parent_capture = _ensure_parent(
                        device=device,
                        parent=parent,
                        branch_config=branch_config,
                        device_serial=device_serial,
                        package_name=package_name,
                        abort_event=abort_event,
                        input_rules=input_rules,
                        dynamic_patterns=dynamic_patterns,
                        stable_wait_seconds=stable_wait,
                        secret_values=secret_values,
                        stage_callback=publish_stage,
                        budget_guard=guard,
                    )
                    parent_was_recovered = parent_capture is not None
                if parent_capture is None:
                    deferred_actions = list(parent.actions[action_index:])
                    attempted_actions.discard(attempted_key)
                    if defer_parent_recovery(parent, deferred_actions):
                        warning = True
                        local_warning_reasons.append(
                            "父状态恢复失败，剩余动作已延迟一次"
                        )
                        break
                    attempted_actions.add(attempted_key)
                    recovery_status = (
                        "PATH_DIVERGED"
                        if parent.recovery_status == PathDiverged.code
                        else "PARENT_RECOVERY_FAILED"
                    )
                    recovery_attempts = recovery_attempt_count(parent)
                    recovery_reason = (
                        "父状态回放路径已偏离，动作未执行"
                        if recovery_status == PathDiverged.code
                        else "父状态恢复重试已耗尽"
                        if recovery_attempts >= 1
                        else "父状态恢复失败，动作未执行"
                    )
                    set_action_status(
                        parent,
                        action,
                        recovery_status,
                        sequence_value=sequence,
                        reason=recovery_reason,
                        execution_disposition="NOT_REACHED",
                        failure_type=recovery_status,
                    )
                    transition_buffer.append(
                        _transition_payload(
                            from_state_id=parent.state_id,
                            sequence=sequence,
                            action=action,
                            status=recovery_status,
                            reason=recovery_reason,
                            execution_disposition="NOT_REACHED",
                            failure_type=recovery_status,
                            recovery_attempt_count=recovery_attempts,
                        ),
                        None,
                    )
                    warning = True
                    local_warning_reasons.append(
                        "回放路径偏离"
                        if parent.recovery_status == PathDiverged.code
                        else "父状态恢复失败"
                    )
                    # A failed deterministic parent recovery is a property of
                    # the whole parent action group, not of just this action.
                    # Retrying the same entry case once for every remaining
                    # action creates an apparent restart loop and delays
                    # already-discovered child states. Record the untouched
                    # remainder without further device operations, then let
                    # BFS advance to the next queued state.
                    for remaining in parent.actions[action_index + 1 :]:
                        attempted_actions.add(
                            (_work_logical_key(parent), remaining.action_key)
                        )
                        sequence += 1
                        if remaining.risk_type:
                            status = "BLOCKED"
                            reason = (
                                remaining.blocked_reason
                                or remaining.risk_type
                            )
                        elif (
                            remaining.coordinate_only
                            and remaining.action_type not in {"click", "scroll"}
                        ):
                            status = "COORDINATE_ONLY"
                            reason = (
                                "无稳定 description/text/约束 XPath，"
                                "仅记录不回放"
                            )
                        else:
                            status = (
                                "QUEUE_TRUNCATED"
                                if recovery_status == PathDiverged.code
                                else recovery_status
                            )
                            reason = (
                                "父状态回放路径已偏离，剩余动作未执行"
                                if status == "QUEUE_TRUNCATED"
                                else recovery_reason
                            )
                        not_reached = status in {
                            "PARENT_RECOVERY_FAILED",
                            "PATH_DIVERGED",
                            "QUEUE_TRUNCATED",
                        }
                        failure_type = (
                            "PATH_DIVERGED_CASCADE"
                            if status == "QUEUE_TRUNCATED"
                            and recovery_status == PathDiverged.code
                            else status
                            if not_reached
                            else "SAFETY_BLOCKED"
                            if status == "BLOCKED"
                            else None
                        )
                        set_action_status(
                            parent,
                            remaining,
                            status,
                            sequence_value=sequence,
                            reason=reason,
                            execution_disposition=(
                                "NOT_REACHED" if not_reached else "SKIPPED"
                            ),
                            failure_type=failure_type,
                        )
                        transition_buffer.append(
                            _transition_payload(
                                from_state_id=parent.state_id,
                                sequence=sequence,
                                action=remaining,
                                status=status,
                                reason=reason,
                                execution_disposition=(
                                    "NOT_REACHED" if not_reached else "SKIPPED"
                                ),
                                failure_type=failure_type,
                                recovery_attempt_count=recovery_attempts,
                            ),
                            None,
                        )
                    break
                parent_recovery_exhausted = False
                if not _capture_matches_parent(parent_capture, parent):
                    # Defensive guard: overlays must only be drawn over the
                    # exact StateWork they describe.
                    publish_stage("recover_parent", "父状态匹配失败")
                    parent_capture = None
                    continue
                reset_parent_recovery(parent)
                if parent_was_recovered:
                    publish_page_actions(parent, parent_capture)
                    rebind_capture = None

                coordinate_target = coordinate_target_key(action)
                if action.coordinate_only and action.action_type == "click":
                    if coordinate_target is None:
                        reason = "坐标动作的 bounds 或源画布尺寸无效，拒绝调用设备"
                        set_action_status(
                            parent,
                            action,
                            "LOCATOR_AMBIGUOUS",
                            sequence_value=sequence,
                            reason=reason,
                            execution_disposition="FAILED",
                            failure_type="LOCATOR_AMBIGUOUS",
                        )
                        transition_buffer.append(
                            _transition_payload(
                                from_state_id=parent.state_id,
                                sequence=sequence,
                                action=action,
                                status="LOCATOR_AMBIGUOUS",
                                reason=reason,
                                execution_disposition="FAILED",
                                failure_type="LOCATOR_AMBIGUOUS",
                            ),
                            None,
                        )
                        warning = True
                        continue
                    if coordinate_target in blocked_coordinate_targets:
                        reason = "同一坐标存在危险动作，按更严格安全结果拦截"
                        set_action_status(
                            parent,
                            action,
                            "BLOCKED",
                            sequence_value=sequence,
                            reason=reason,
                            execution_disposition="SKIPPED",
                            failure_type="SAFETY_BLOCKED",
                        )
                        transition_buffer.append(
                            _transition_payload(
                                from_state_id=parent.state_id,
                                sequence=sequence,
                                action=action,
                                status="BLOCKED",
                                reason=reason,
                                execution_disposition="SKIPPED",
                                failure_type="SAFETY_BLOCKED",
                            ),
                            None,
                        )
                        continue
                    visual_locator = (action.target_meta or {}).get("visual_locator")
                    visual_fresh = bool(
                        isinstance(visual_locator, dict)
                        and visual_locator_matches(
                            action,
                            parent_capture.model,
                            parent_capture.screenshot_png,
                        )
                    )
                    if isinstance(visual_locator, dict) and not visual_fresh:
                        reason = "HOME 视觉图块或页面实例已变化，拒绝坐标点击"
                        set_action_status(
                            parent,
                            action,
                            "VISUAL_STALE",
                            sequence_value=sequence,
                            reason=reason,
                            execution_disposition="SKIPPED",
                            failure_type="VISUAL_STALE",
                        )
                        transition_buffer.append(
                            _transition_payload(
                                from_state_id=parent.state_id,
                                sequence=sequence,
                                action=action,
                                status="VISUAL_STALE",
                                reason=reason,
                                execution_disposition="SKIPPED",
                                failure_type="VISUAL_STALE",
                                sampling_disposition="VISUAL_STALE",
                            ),
                            None,
                        )
                        warning = True
                        continue
                    if (
                        parent_capture.screenshot_sha != parent.screenshot_sha
                        and not visual_fresh
                    ):
                        reason = "页面像素已变化，拒绝使用采集时保存的坐标"
                        set_action_status(
                            parent,
                            action,
                            "COORDINATE_STALE",
                            sequence_value=sequence,
                            reason=reason,
                            execution_disposition="SKIPPED",
                            failure_type="COORDINATE_STALE",
                        )
                        transition_buffer.append(
                            _transition_payload(
                                from_state_id=parent.state_id,
                                sequence=sequence,
                                action=action,
                                status="COORDINATE_STALE",
                                reason=reason,
                                execution_disposition="SKIPPED",
                                failure_type="COORDINATE_STALE",
                            ),
                            None,
                        )
                        warning = True
                        continue
                    if coordinate_target in invoked_coordinate_targets:
                        reason = "同一物理坐标已处理，本动作不再调用设备"
                        set_action_status(
                            parent,
                            action,
                            "SKIPPED",
                            sequence_value=sequence,
                            reason=reason,
                        )
                        transition_buffer.append(
                            _transition_payload(
                                from_state_id=parent.state_id,
                                sequence=sequence,
                                action=action,
                                status="SKIPPED",
                                reason=reason,
                            ),
                            None,
                        )
                        continue

                input_value: Optional[str] = None
                input_length: Optional[int] = None
                used_locator = None
                rebind_attempt_count = 0
                preflight_locator_miss = False
                started = time.monotonic()
                if coverage_scheduler and action.action_group_key:
                    action_group_attempts[
                        sampling_group_signature(parent, action)
                    ] += 1
                set_action_status(
                    parent,
                    action,
                    "ACTIVE",
                    sequence_value=sequence,
                    increment_attempt=True,
                )
                try:
                    if rebind_capture is not None:
                        rebound = _rebind_action_on_capture(
                            action,
                            rebind_capture,
                            screen_size=screen_size,
                            safety_rules=safety_rules,
                            input_rules=input_rules,
                            max_scrolls=max_scrolls,
                            coverage_scheduler=coverage_scheduler,
                        )
                        if rebound is None:
                            preflight_locator_miss = True
                            raise LocatorDrift(
                                "最新采集未暴露该动作，跳过设备调用"
                            )
                        action = rebound
                    if action.action_type == "input":
                        input_value, variable_key, input_length = _resolve_input_value(
                            action=action,
                            input_rules=input_rules,
                            env_id=branch_config.get("env_id"),
                            secret_values=secret_values,
                        )
                        if variable_key:
                            action = InspectionAction(
                                **{
                                    **action.__dict__,
                                    "input_variable_key": variable_key,
                                }
                            )
                    execution_action = action
                    panel_patch = live_action_panel_patch(
                        parent,
                        current_action=_live_action(
                            parent,
                            action.action_key,
                        ),
                        canvas_matches_panel=True,
                    )
                    _publish_live(
                        run_id,
                        "ACTION_STARTED",
                        branch_key=branch.branch_key,
                        phase=current_phase or "explore",
                        current_stage="执行页面动作",
                        run_status="RUNNING",
                        **panel_patch,
                        overlay_visible=bool(
                            panel_patch.get("canvas_matches_panel")
                        ),
                        progress=dict(progress),
                    )
                    if coordinate_target is not None:
                        invoked_coordinate_targets.add(coordinate_target)
                    while True:
                        guard.before_device_interaction(
                            "perform_exploration_action",
                            mutating=True,
                        )
                        try:
                            used_locator = perform_action(
                                device,
                                execution_action,
                                current_xml=parent_capture.xml,
                                input_value=input_value,
                                allow_coordinate_discovery=bool(
                                    execution_action.coordinate_only
                                    and execution_action.action_type == "click"
                                ),
                            )
                            if scroll_key is not None:
                                scroll_invocation_counts[scroll_key] = (
                                    scroll_invocation_counts.get(scroll_key, 0) + 1
                                )
                                coverage_scroll_invocations += 1
                            break
                        except LocatorDrift:
                            if rebind_attempt_count >= 1:
                                raise
                            guard.before_device_interaction(
                                "capture_after_locator_not_found"
                            )
                            fresh_capture = _budgeted_wait_for_stable_page(
                                device,
                                budget_guard=guard,
                                expected_package=package_name,
                                abort_event=abort_event,
                                # Re-binding is bounded to one immediate
                                # screenshot/XML sample. Waiting for another
                                # stability window turns stale controls into a
                                # multi-second queue tax without adding useful
                                # locator evidence.
                                max_wait_seconds=0.0,
                                dynamic_patterns=dynamic_patterns,
                            )
                            parent_capture = fresh_capture
                            if not _capture_matches_parent(fresh_capture, parent):
                                raise
                            rebind_capture = fresh_capture
                            rebound = _rebind_action_on_capture(
                                action,
                                fresh_capture,
                                screen_size=screen_size,
                                safety_rules=safety_rules,
                                input_rules=input_rules,
                                max_scrolls=max_scrolls,
                                coverage_scheduler=coverage_scheduler,
                            )
                            if rebound is None:
                                raise LocatorDrift(
                                    "fresh page no longer exposes the action role"
                                )
                            execution_action = rebound
                            rebind_attempt_count += 1
                            stale_live_action_map_state_ids.add(parent.state_id)
                            # The persisted action board is bound to the
                            # original capture.  Until all displayed bounds are
                            # rebuilt from this fresh XML, drawing them over the
                            # live canvas would be misleading even though the
                            # single execution locator was rebound safely.
                            _publish_live(
                                run_id,
                                "ACTION_REBOUND",
                                branch_key=branch.branch_key,
                                phase="recover",
                                current_stage="定位器已在最新页面重新绑定",
                                run_status="RUNNING",
                                **live_action_panel_patch(
                                    parent,
                                    current_action=_live_action(
                                        parent,
                                        action.action_key,
                                    ),
                                    canvas_matches_panel=False,
                                ),
                                overlay_visible=False,
                                progress=dict(progress),
                            )
                    rebind_capture = None
                    guard.check_deadline()
                    set_action_status(
                        parent,
                        action,
                        "INVOKED",
                        sequence_value=sequence,
                        invoked=True,
                    )
                    _publish_live(
                        run_id,
                        "ACTION_INVOKED",
                        branch_key=branch.branch_key,
                        phase=current_phase or "explore",
                        current_stage="设备调用已返回",
                        run_status="RUNNING",
                        **live_action_panel_patch(
                            parent,
                            current_action=_live_action(
                                parent,
                                action.action_key,
                            ),
                            canvas_matches_panel=False,
                        ),
                        overlay_visible=False,
                        progress=dict(progress),
                    )
                except InspectionAborted:
                    raise
                except BudgetExceeded:
                    raise
                except LocatorAmbiguous as exc:
                    set_action_status(
                        parent,
                        action,
                        "LOCATOR_AMBIGUOUS",
                        sequence_value=sequence,
                        reason="定位候选无法唯一解析",
                        error=_safe_error(exc),
                        execution_disposition="FAILED",
                        failure_type="LOCATOR_AMBIGUOUS",
                    )
                    transition_buffer.append(
                        _transition_payload(
                            from_state_id=parent.state_id,
                            sequence=sequence,
                            action=action,
                            status="LOCATOR_AMBIGUOUS",
                            reason="定位候选无法唯一解析",
                            duration_ms=(time.monotonic() - started) * 1000,
                            error_message=_safe_error(exc),
                            execution_disposition="FAILED",
                            failure_type="LOCATOR_AMBIGUOUS",
                            recovery_attempt_count=rebind_attempt_count,
                        ),
                        None,
                    )
                    warning = True
                    continue
                except LocatorDrift as exc:
                    disposition = (
                        "SKIPPED" if preflight_locator_miss else "FAILED"
                    )
                    locator_reason = (
                        "最新采集未暴露该动作，跳过设备调用"
                        if preflight_locator_miss
                        else "最新页面中未找到可唯一执行的定位器"
                    )
                    set_action_status(
                        parent,
                        action,
                        "LOCATOR_NOT_FOUND",
                        sequence_value=sequence,
                        reason=locator_reason,
                        error=_safe_error(exc),
                        execution_disposition=disposition,
                        failure_type="LOCATOR_NOT_FOUND",
                    )
                    transition_buffer.append(
                        _transition_payload(
                            from_state_id=parent.state_id,
                            sequence=sequence,
                            action=action,
                            status="LOCATOR_NOT_FOUND",
                            reason=locator_reason,
                            duration_ms=(time.monotonic() - started) * 1000,
                            error_message=_safe_error(exc),
                            execution_disposition=disposition,
                            failure_type="LOCATOR_NOT_FOUND",
                            recovery_attempt_count=rebind_attempt_count,
                        ),
                        None,
                    )
                    warning = True
                    continue
                except PermissionError as exc:
                    set_action_status(
                        parent,
                        action,
                        "BLOCKED",
                        sequence_value=sequence,
                        reason=_safe_error(exc),
                        execution_disposition="SKIPPED",
                        failure_type="SAFETY_BLOCKED",
                    )
                    transition_buffer.append(
                        _transition_payload(
                            from_state_id=parent.state_id,
                            sequence=sequence,
                            action=action,
                            status="BLOCKED",
                            reason=_safe_error(exc),
                            duration_ms=(time.monotonic() - started) * 1000,
                            execution_disposition="SKIPPED",
                            failure_type="SAFETY_BLOCKED",
                        ),
                        None,
                    )
                    # A configured safety boundary is an expected terminal
                    # outcome. Unknown input and unclassified permission errors
                    # still surface as task warnings because they require
                    # configuration or locator attention.
                    if not action.risk_type or action.risk_type == "UNMAPPED_INPUT":
                        warning = True
                    continue
                except Exception as exc:
                    if isinstance(exc, DeviceDisconnected):
                        set_action_status(
                            parent,
                            action,
                            "ERROR",
                            sequence_value=sequence,
                            invocation_unknown=True,
                            reason="设备在动作调用期间断连",
                            error=_safe_error(exc),
                            execution_disposition="RESULT_UNKNOWN",
                            failure_type="INFRA_FAULT",
                        )
                        transition_buffer.append(
                            _transition_payload(
                                from_state_id=parent.state_id,
                                sequence=sequence,
                                action=action,
                                status="ERROR",
                                reason="设备在动作调用期间断连",
                                duration_ms=(time.monotonic() - started) * 1000,
                                error_message=_safe_error(exc),
                                execution_disposition="RESULT_UNKNOWN",
                                failure_type="INFRA_FAULT",
                            ),
                            None,
                        )
                        _persist_work_action_map(parent)
                        raise
                    ui_responsive = _probe_ui_automation_responsive(
                        device,
                        abort_event=abort_event,
                        budget_guard=guard,
                    )
                    if ui_responsive:
                        set_action_status(
                            parent,
                            action,
                            "ACTION_ERROR",
                            sequence_value=sequence,
                            invocation_unknown=True,
                            reason="动作异常，但独立 UI 健康检查正常",
                            error=_safe_error(exc),
                            execution_disposition="FAILED",
                            failure_type="AUTOMATION_FAILED",
                        )
                        transition_buffer.append(
                            _transition_payload(
                                from_state_id=parent.state_id,
                                sequence=sequence,
                                action=action,
                                status="ACTION_ERROR",
                                reason="动作异常，但独立 UI 健康检查正常",
                                duration_ms=(time.monotonic() - started) * 1000,
                                error_message=_safe_error(exc),
                                execution_disposition="FAILED",
                                failure_type="AUTOMATION_FAILED",
                            ),
                            None,
                        )
                        parent_capture = None
                        warning = True
                        continue
                    set_action_status(
                        parent,
                        action,
                        "ERROR",
                        sequence_value=sequence,
                        invocation_unknown=True,
                        reason="设备动作失败",
                        error=_safe_error(exc),
                        execution_disposition="FAILED",
                        failure_type="APP_FAULT",
                    )
                    transition_buffer.append(
                        _transition_payload(
                            from_state_id=parent.state_id,
                            sequence=sequence,
                            action=action,
                            status="ERROR",
                            reason="设备动作失败",
                            duration_ms=(time.monotonic() - started) * 1000,
                            error_message=_safe_error(exc),
                            execution_disposition="FAILED",
                            failure_type="APP_FAULT",
                        ),
                        None,
                    )
                    _persist_fault(
                        run_id=run_id,
                        branch_run_id=branch_run_id,
                        state_id=parent.state_id,
                        fault_type="UI_UNRESPONSIVE",
                        summary="设备动作执行异常或 UI 无响应",
                        event=_fault_event_with_replay(
                            monitor,
                            "UI_UNRESPONSIVE",
                            full_log=_safe_error(exc),
                            budget_guard=guard,
                        ),
                        recent_actions=[
                            *list(recent_actions),
                            _serialize_action(action),
                        ],
                        secret_values=secret_values,
                        device_serial=device_serial,
                        budget_guard=guard,
                    )
                    parent_capture = None
                    hard_fault = True
                    continue
                finally:
                    input_value = None

                recent_actions.append(_serialize_action(action))

                publish_stage("stabilizing", "等待动作后页面稳定")
                try:
                    guard.before_device_interaction("capture_action_target")
                    target_capture = _budgeted_wait_for_stable_page(
                        device,
                        budget_guard=guard,
                        expected_package=package_name,
                        abort_event=abort_event,
                        max_wait_seconds=stable_wait,
                        dynamic_patterns=dynamic_patterns,
                    )
                except BudgetExceeded:
                    raise
                except InspectionAborted:
                    set_action_status(
                        parent,
                        action,
                        "CANCELLED",
                        sequence_value=sequence,
                        invoked=True,
                        invocation_unknown=True,
                        reason="动作已调用，等待结果时任务取消",
                        execution_disposition="RESULT_UNKNOWN",
                        failure_type="CANCELLED",
                    )
                    transition_buffer.append(
                        _transition_payload(
                            from_state_id=parent.state_id,
                            sequence=sequence,
                            action=action,
                            status="CANCELLED",
                            reason="动作已调用，等待结果时任务取消",
                            duration_ms=(time.monotonic() - started) * 1000,
                            used_locator=used_locator,
                            input_length=input_length,
                            execution_disposition="RESULT_UNKNOWN",
                            failure_type="CANCELLED",
                            topology_type="TERMINAL",
                        ),
                        None,
                    )
                    _persist_work_action_map(parent)
                    raise
                except Exception as exc:
                    capture_failure_type = (
                        "INFRA_FAULT"
                        if isinstance(exc, DeviceDisconnected)
                        else "AUTOMATION_FAILED"
                    )
                    set_action_status(
                        parent,
                        action,
                        "ERROR",
                        sequence_value=sequence,
                        invoked=True,
                        invocation_unknown=True,
                        reason="动作后页面稳定采集失败",
                        error=_safe_error(exc),
                        execution_disposition="RESULT_UNKNOWN",
                        failure_type=capture_failure_type,
                    )
                    transition_buffer.append(
                        _transition_payload(
                            from_state_id=parent.state_id,
                            sequence=sequence,
                            action=action,
                            status="ERROR",
                            reason="动作后页面稳定采集失败",
                            duration_ms=(time.monotonic() - started) * 1000,
                            used_locator=used_locator,
                            input_length=input_length,
                            error_message=_safe_error(exc),
                            execution_disposition="RESULT_UNKNOWN",
                            failure_type=capture_failure_type,
                        ),
                        None,
                    )
                    _persist_work_action_map(parent)
                    raise
                duration_ms = (time.monotonic() - started) * 1000
                drain_monitor_events(parent.state_id)

                if target_capture.package_name != package_name:
                    fault_type = (
                        "APP_EXIT"
                        if not target_capture.package_name
                        or target_capture.package_name in {
                            "com.android.launcher",
                            "com.google.android.apps.nexuslauncher",
                        }
                        else "EXTERNAL_APP"
                    )
                    if fault_type == "APP_EXIT":
                        _persist_fault(
                            run_id=run_id,
                            branch_run_id=branch_run_id,
                            state_id=parent.state_id,
                            fault_type=fault_type,
                            summary=f"目标 APP 异常退出到 {target_capture.package_name or 'unknown'}",
                            event=_fault_event_with_replay(
                                monitor,
                                fault_type,
                                budget_guard=guard,
                            ),
                            recent_actions=list(recent_actions),
                            secret_values=secret_values,
                            device_serial=device_serial,
                            budget_guard=guard,
                        )
                        hard_fault = True
                    else:
                        warning = True
                    set_action_status(
                        parent,
                        action,
                        fault_type,
                        sequence_value=sequence,
                        invoked=True,
                        reason=(
                            f"前台包切换为 {target_capture.package_name or '-'}"
                        ),
                        execution_disposition="EXECUTED",
                        failure_type=(
                            "APP_FAULT"
                            if fault_type == "APP_EXIT"
                            else "EXTERNAL_NAVIGATION"
                        ),
                    )
                    transition_buffer.append(
                        _transition_payload(
                            from_state_id=parent.state_id,
                            sequence=sequence,
                            action=action,
                            status=fault_type,
                            reason=f"前台包切换为 {target_capture.package_name or '-'}",
                            duration_ms=duration_ms,
                            used_locator=used_locator,
                            input_length=input_length,
                            execution_disposition="EXECUTED",
                            failure_type=(
                                "APP_FAULT"
                                if fault_type == "APP_EXIT"
                                else "EXTERNAL_NAVIGATION"
                            ),
                        ),
                        None,
                    )
                    try:
                        publish_stage("recover_parent", "从外部页面返回")
                        guard.before_device_interaction(
                            "press_back_from_external",
                            mutating=True,
                        )
                        device.press("back")
                    except BudgetExceeded:
                        raise
                    except Exception:
                        pass
                    parent_capture = None
                    continue

                if (
                    action.coordinate_only
                    and action.action_type == "click"
                    and target_capture.package_name == parent_capture.package_name
                    and target_capture.activity == parent_capture.activity
                    and target_capture.model.replay_key
                    == parent_capture.model.replay_key
                    and target_capture.screenshot_sha == parent_capture.screenshot_sha
                ):
                    reason = "坐标点击后页面完全无变化，本动作不再重试"
                    set_action_status(
                        parent,
                        action,
                        "NO_EFFECT",
                        sequence_value=sequence,
                        invoked=True,
                        reason=reason,
                    )
                    transition_buffer.append(
                        _transition_payload(
                            from_state_id=parent.state_id,
                            sequence=sequence,
                            action=action,
                            status="NO_EFFECT",
                            reason=reason,
                            duration_ms=duration_ms,
                            used_locator=used_locator,
                            input_length=input_length,
                        ),
                        None,
                    )
                    guard.record_coverage(discovered=False)
                    warning = True
                    parent_capture = target_capture
                    continue

                if is_white_screen(target_capture.screenshot_png):
                    _persist_fault(
                        run_id=run_id,
                        branch_run_id=branch_run_id,
                        state_id=parent.state_id,
                        fault_type="WHITE_SCREEN",
                        summary="检测到持续白屏",
                        event=_fault_event_with_replay(
                            monitor,
                            "WHITE_SCREEN",
                            budget_guard=guard,
                        ),
                        recent_actions=list(recent_actions),
                        secret_values=secret_values,
                        device_serial=device_serial,
                        budget_guard=guard,
                    )
                    hard_fault = True

                is_viewport_scroll = action.action_type == "scroll"
                target_logical_key = str(
                    target_capture.model.semantic_key
                    if identity_v2
                    else target_capture.model.replay_key
                )
                same_state = target_logical_key == _work_page_logical_key(parent)
                overlay_return_owner = (
                    _overlay_return_owner(
                        parent=parent,
                        action=action,
                        capture=target_capture,
                        tracked_work=tracked_work,
                    )
                    if coverage_scheduler
                    else None
                )
                navigation_confirmation = None
                navigation_group_key: Optional[str] = None
                if (
                    not same_state
                    and not is_viewport_scroll
                    and _navigation_metadata(action)
                ):
                    navigation_confirmation = confirm_peer_navigation(
                        action.target_meta,
                        target_capture.model,
                        screen_size=_capture_screen_size(target_capture, screen_size),
                    )
                    if navigation_confirmation.matched:
                        navigation_group_key = navigation_confirmation.group_key
                        navigation_meta = _navigation_metadata(action)
                        navigation_meta["confirmation"] = (
                            navigation_confirmation.to_dict()
                        )
                        action = InspectionAction(
                            **{
                                **action.__dict__,
                                "target_meta": {
                                    **action.target_meta,
                                    "navigation": navigation_meta,
                                },
                            }
                        )

                if same_state:
                    relation_type = "SELF"
                    relation_confidence = 1.0
                elif is_viewport_scroll:
                    relation_type = "VIEWPORT"
                    relation_confidence = 1.0
                elif navigation_confirmation and navigation_confirmation.matched:
                    relation_type = "PEER"
                    relation_confidence = navigation_confirmation.confidence
                else:
                    relation_type = "CHILD"
                    relation_confidence = 1.0

                if overlay_return_owner is not None:
                    target_depth = overlay_return_owner.depth
                    target_parent_state_id = overlay_return_owner.parent_state_id
                elif relation_type == "PEER" and navigation_group_key:
                    anchor = navigation_anchors.get(navigation_group_key)
                    if anchor is None:
                        anchor = NavigationAnchor(
                            group_key=navigation_group_key,
                            depth=parent.depth,
                            parent_state_id=parent.parent_state_id,
                        )
                        navigation_anchors[navigation_group_key] = anchor
                    target_depth = anchor.depth
                    target_parent_state_id = anchor.parent_state_id
                elif is_viewport_scroll:
                    target_depth = parent.depth
                    target_parent_state_id = parent.state_id
                else:
                    target_depth = parent.depth + 1
                    target_parent_state_id = parent.state_id

                source_instance_anchor = (
                    parent.instance_anchor
                    or derive_instance_anchor(parent_capture.model)
                )
                target_instance_anchor = str(
                    overlay_return_owner.instance_anchor
                    if overlay_return_owner is not None
                    else source_instance_anchor
                    if same_state
                    else derive_instance_anchor(
                        target_capture.model,
                        incoming_action=action,
                        source_instance_anchor=source_instance_anchor,
                    )
                )
                child_path = parent.path + [
                    _serialize_action(
                        action,
                        expected_source_semantic_key=(
                            parent_capture.model.semantic_key
                            or parent_capture.model.replay_key
                        ),
                        expected_target_semantic_key=(
                            target_capture.model.semantic_key
                            or target_capture.model.replay_key
                        ),
                        expected_target_role=target_capture.model.role,
                        expected_target_template_key=target_capture.model.template_key,
                        expected_source_signature=_replay_model_expectation(
                            parent_capture.model,
                            instance_anchor=source_instance_anchor,
                        ),
                        expected_target_signature=_replay_model_expectation(
                            target_capture.model,
                            instance_anchor=target_instance_anchor,
                        ),
                    )
                ]
                persisted = _persist_state(
                    run_id=run_id,
                    branch_run=branch,
                    capture=target_capture,
                    depth=target_depth,
                    parent_state_id=target_parent_state_id,
                    path=child_path,
                    sanitizer=sanitizer,
                    screen_size=screen_size,
                    safety_rules=safety_rules,
                    input_rules=input_rules,
                    max_scrolls=max_scrolls,
                    max_variants=max_variants,
                    secret_values=secret_values,
                    prefer_hierarchy=relation_type == "PEER",
                    identity_v2=identity_v2,
                    similarity_convergence=similarity_convergence,
                    family_convergence=family_convergence,
                    coverage_scheduler=coverage_scheduler,
                    visual_home_actions=visual_home_actions,
                    budget_guard=guard,
                    ancestry_state_ids=parent.ancestry_state_ids,
                    instance_anchor_override=(
                        overlay_return_owner.instance_anchor
                        if overlay_return_owner is not None
                        else source_instance_anchor
                        if same_state
                        or (coverage_scheduler and is_viewport_scroll)
                        else None
                    ),
                    preferred_state_id=(
                        overlay_return_owner.state_id
                        if overlay_return_owner is not None
                        else parent.state_id
                        if same_state
                        or (coverage_scheduler and is_viewport_scroll)
                        else None
                    ),
                    preferred_match_type=(
                        "OVERLAY_RETURN"
                        if overlay_return_owner is not None
                        else None
                    ),
                )
                viewport_work: Optional[StateWork] = None
                viewport_new_action_groups: set[str] = set()
                if persisted.variant_capped:
                    set_action_status(
                        parent,
                        action,
                        "VARIANT_LIMIT",
                        sequence_value=sequence,
                        invoked=True,
                        reason="页面簇状态变体达到上限",
                    )
                    transition_buffer.append(
                        _transition_payload(
                            from_state_id=parent.state_id,
                            sequence=sequence,
                            action=action,
                            status="VARIANT_LIMIT",
                            reason="页面簇状态变体达到上限",
                            duration_ms=duration_ms,
                            used_locator=used_locator,
                            input_length=input_length,
                        ),
                        None,
                    )
                    warning = True
                    guard.record_coverage(discovered=False)
                elif persisted.work:
                    if not persisted.work.semantic_key:
                        persisted.work.semantic_key = target_logical_key
                    if not persisted.work.ancestry_state_ids:
                        persisted.work.ancestry_state_ids = (
                            *parent.ancestry_state_ids,
                            persisted.work.state_id,
                        )
                    persisted.work.family_action_trail = (
                        _extend_family_action_trail(parent, action)
                    )
                    if is_viewport_scroll and coverage_scheduler:
                        viewport_keys = tuple(
                            dict.fromkeys(
                                (
                                    *parent.viewport_semantic_keys,
                                    target_logical_key,
                                )
                            )
                        )[-8:]
                        parent.viewport_semantic_keys = viewport_keys
                        persisted.work.viewport_semantic_keys = viewport_keys
                        known_groups = {
                            str(item.action_group_key)
                            for item in parent.actions
                            if item.action_group_key
                        }
                        viewport_new_action_groups = {
                            str(item.action_group_key)
                            for item in persisted.work.actions
                            if item.action_group_key
                            and item.action_type != "scroll"
                            and str(item.action_group_key) not in known_groups
                        }
                    family_cycle_period = _family_action_cycle_period(
                        persisted.work.family_action_trail
                    )
                    track_work(persisted.work)
                    target_state_id = persisted.work.state_id
                    if (
                        target_state_id == parent.state_id
                        or _work_logical_key(persisted.work) == _work_logical_key(parent)
                    ):
                        topology_type = "SELF_LOOP"
                    elif target_state_id in parent.ancestry_state_ids:
                        topology_type = "CYCLE_BACK"
                    elif not persisted.is_new:
                        topology_type = "REVISIT"
                    else:
                        topology_type = "TREE"
                    status = (
                        "CYCLE_CONVERGED"
                        if family_cycle_period is not None
                        else "NO_NEW_COVERAGE"
                        if is_viewport_scroll
                        and coverage_scheduler
                        and not viewport_new_action_groups
                        else "SELF_LOOP"
                        if topology_type == "SELF_LOOP"
                        else "PASS"
                    )
                    sample_policy = str(action.sample_policy or "")
                    successful_group_sample = bool(
                        coverage_scheduler
                        and action.action_group_key
                        and not _is_overlay_cleanup_action(action)
                        and sample_policy
                        in {
                            "FAMILY_TWO_SAMPLES",
                            "FAMILY_ONE",
                            "PAGE_ONE",
                            "RUN_NAV_ONCE",
                        }
                        and status in {"PASS", "SELF_LOOP"}
                        and not (
                            sample_policy == "FAMILY_ONE"
                            and status == "SELF_LOOP"
                        )
                    )
                    if successful_group_sample:
                        sampled_action_groups.add(
                            sampling_group_signature(parent, action)
                        )
                    contract_sample = bool(
                        successful_group_sample
                        and sample_policy
                        in {
                            "FAMILY_TWO_SAMPLES",
                            "PAGE_ONE",
                            "RUN_NAV_ONCE",
                        }
                    )
                    traversal_key = (
                        parent.state_id,
                        action.action_key,
                        target_state_id,
                    )
                    traversal_counts[traversal_key] = (
                        traversal_counts.get(traversal_key, 0) + 1
                    )
                    action_reason = (
                        f"12 次转移窗口内检测到长度 {family_cycle_period} 的同构页面族动作周期，停止扩展"
                        if family_cycle_period is not None
                        else "滚动未发现新动作组、页面子类型或视觉区域"
                        if status == "NO_NEW_COVERAGE"
                        else "同页视口扩展"
                        if is_viewport_scroll
                        else "平级导航切换"
                        if relation_type == "PEER"
                        else None
                    )
                    set_action_status(
                        parent,
                        action,
                        status,
                        sequence_value=sequence,
                        invoked=True,
                        reason=action_reason,
                    )
                    transition_buffer.append(
                        _transition_payload(
                            from_state_id=parent.state_id,
                            to_state_id=persisted.work.state_id,
                            sequence=sequence,
                            action=action,
                            status=status,
                            reason=action_reason,
                            duration_ms=duration_ms,
                            used_locator=used_locator,
                            input_length=input_length,
                            relation_type=relation_type,
                            relation_confidence=relation_confidence,
                            topology_type=topology_type,
                            source_observation_id=parent.observation_id,
                            target_observation_id=persisted.observation_id,
                            traversal_count=traversal_counts[traversal_key],
                            target_was_existing=not persisted.is_new,
                            sampling_disposition=(
                                "CONTRACT_SAMPLE"
                                if contract_sample
                                else "NO_NEW_COVERAGE"
                                if status == "NO_NEW_COVERAGE"
                                else None
                            ),
                        ),
                        persisted.work.state_id,
                        assign_incoming=persisted.assign_incoming,
                    )
                    if relation_type == "PEER" and navigation_group_key:
                        navigation_entries[:] = [
                            entry
                            for entry in navigation_entries
                            if not (
                                entry.group_key == navigation_group_key
                                and entry.state_id == persisted.work.state_id
                            )
                        ]
                        navigation_entries.append(
                            NavigationEntry(
                                group_key=navigation_group_key,
                                state_id=persisted.work.state_id,
                                action=action,
                                target_path=tuple(
                                    dict(item) for item in persisted.work.path
                                ),
                            )
                    )
                    guard.record_coverage(discovered=persisted.is_new)
                    capture_only_terminal = False
                    if coverage_scheduler and target_state_id != parent.state_id:
                        # Persist the transition first so a just-established
                        # contract or family source has an auditable Transition
                        # ID before the target's reuse edges reference it.
                        transition_buffer.flush()
                        capture_only_terminal = finalize_capture_reuse(
                            persisted.work
                        )
                    coordinate_capture_handoff = bool(
                        coverage_scheduler
                        and action.action_type == "click"
                        and action.coordinate_only
                        and target_state_id != parent.state_id
                        and str(persisted.work.exploration_mode or "").upper()
                        in {"FULL", "DELTA_ONLY"}
                        and bool(pending_actions(persisted.work))
                    )
                    can_expand_target = bool(
                        action.action_type == "scroll"
                        or action.replayable
                        or coordinate_capture_handoff
                    )
                    is_root_bottom_navigation_survey = bool(
                        coverage_scheduler
                        and parent.depth == 0
                        and parent.page_subtype == "HOME"
                        and str(action.sample_policy or "") == "RUN_NAV_ONCE"
                        and _navigation_metadata(action).get("group_region")
                        == "bottom"
                    )
                    if capture_only_terminal:
                        pass
                    elif persisted.work.depth > max_depth:
                        finalize_unqueued_work(
                            persisted.work,
                            action_status="BUDGET_NOT_REACHED",
                            state_status="BUDGET_SKIPPED",
                            reason="目标状态超过最大探索深度",
                        )
                        warning = True
                    elif family_cycle_period is not None:
                        if persisted.work.state_id not in queued_state_ids:
                            finalize_unqueued_work(
                                persisted.work,
                                action_status="QUEUE_TRUNCATED",
                                state_status="ABORTED",
                                reason="同构页面族动作周期已收敛，停止该路径扩展",
                            )
                    elif can_expand_target:
                        if is_viewport_scroll:
                            if (
                                target_logical_key != _work_page_logical_key(parent)
                                and (
                                    not coverage_scheduler
                                    or bool(viewport_new_action_groups)
                                )
                            ):
                                viewport_work = persisted.work
                        elif coverage_scheduler and coordinate_capture_handoff:
                            remaining_parent_actions = list(
                                parent.actions[action_index + 1 :]
                            )
                            enqueue(
                                parent,
                                actions=remaining_parent_actions,
                                reason="PARENT_CONTINUATION",
                            )
                            ready_captures[persisted.work.state_id] = target_capture
                            enqueue(
                                persisted.work,
                                front=True,
                                priority=(
                                    20
                                    if str(
                                        persisted.work.exploration_mode or ""
                                    ).upper()
                                    == "FULL"
                                    else 120
                                ),
                                reason=(
                                    "COORDINATE_DISCOVERY_HANDOFF"
                                    if str(
                                        persisted.work.exploration_mode or ""
                                    ).upper()
                                    == "FULL"
                                    else "COORDINATE_DELTA_HANDOFF"
                                ),
                            )
                            priority_handoff_path = list(persisted.work.path)
                        elif (
                            coverage_scheduler
                            and persisted.is_new
                            and str(persisted.work.exploration_mode or "").upper()
                            == "FULL"
                        ):
                            if is_root_bottom_navigation_survey:
                                enqueue(
                                    persisted.work,
                                    priority=40,
                                    reason=(
                                        "PRIMARY_ENTRY_SURFACE"
                                    ),
                                )
                            else:
                                remaining_parent_actions = list(
                                    parent.actions[action_index + 1 :]
                                )
                                enqueue(
                                    parent,
                                    actions=remaining_parent_actions,
                                    reason="PARENT_CONTINUATION",
                                )
                                ready_captures[persisted.work.state_id] = (
                                    target_capture
                                )
                                enqueue(
                                    persisted.work,
                                    front=True,
                                    priority=(
                                        20
                                        if coordinate_capture_handoff
                                        else _coverage_representative_priority(
                                            persisted.work
                                        )
                                    ),
                                    reason=(
                                        "COORDINATE_DISCOVERY_HANDOFF"
                                        if coordinate_capture_handoff
                                        else "LOW_CONFIDENCE_REPRESENTATIVE"
                                        if _coverage_representative_priority(
                                            persisted.work
                                        )
                                        >= 500
                                        else "CAPTURED_NEW_FAMILY_REPRESENTATIVE"
                                    ),
                                )
                                priority_handoff_path = list(
                                    persisted.work.path
                                )
                        elif (
                            coverage_scheduler
                            and persisted.is_new
                            and needs_contract_confirmation(persisted.work)
                        ):
                            remaining_parent_actions = list(
                                parent.actions[action_index + 1 :]
                            )
                            enqueue(
                                parent,
                                actions=remaining_parent_actions,
                                reason="PARENT_CONTINUATION",
                            )
                            ready_captures[persisted.work.state_id] = target_capture
                            enqueue(
                                persisted.work,
                                front=True,
                                priority=max(
                                    120,
                                    _coverage_representative_priority(
                                        persisted.work
                                    ),
                                ),
                                reason="CAPTURED_CONTRACT_CONFIRMATION",
                            )
                            priority_handoff_path = list(persisted.work.path)
                        else:
                            enqueue(persisted.work)
                    else:
                        finalize_unqueued_work(
                            persisted.work,
                            action_status="QUEUE_TRUNCATED",
                            state_status="ABORTED",
                            reason="进入动作不可回放，目标状态未加入探索队列",
                        )

                if is_viewport_scroll:
                    # A scroll does not create a business navigation level and
                    # must never be restored with Android Back. Direct handoff
                    # avoids replaying the scroll, but a bounded streak lets
                    # already-discovered business pages make progress.
                    parent_capture = target_capture
                    if (
                        coverage_scheduler
                        and persisted.work is not None
                        and persisted.work.state_id == parent.state_id
                    ):
                        ready_captures[parent.state_id] = target_capture
                    if target_logical_key != _work_page_logical_key(parent):
                        remaining_parent_actions = list(
                            parent.actions[action_index + 1 :]
                        )
                        has_competing_frontier = bool(
                            queue or remaining_parent_actions
                        )
                        direct_handoff = bool(
                            viewport_work is not None
                            and (
                                consecutive_viewport_handoffs
                                < _MAX_CONSECUTIVE_VIEWPORT_HANDOFFS
                                or not has_competing_frontier
                            )
                        )
                        if direct_handoff:
                            enqueue(
                                parent,
                                front=True,
                                actions=remaining_parent_actions,
                            )
                            if enqueue(viewport_work, front=True):
                                ready_captures[viewport_work.state_id] = (
                                    target_capture
                                )
                                consecutive_viewport_handoffs += 1
                        elif viewport_work is not None:
                            enqueue(viewport_work)
                            enqueue(
                                parent,
                                actions=remaining_parent_actions,
                            )
                        else:
                            enqueue(
                                parent,
                                front=True,
                                actions=remaining_parent_actions,
                            )
                        viewport_handoff_path = (
                            list(viewport_work.path)
                            if viewport_work is not None
                            else list(child_path)
                        )
                        break
                    continue

                if priority_handoff_path is not None:
                    parent_capture = target_capture
                    break

                if same_state:
                    parent_capture = target_capture
                else:
                    parent_capture = _restore_parent_after_transition(
                        device=device,
                        parent=parent,
                        target_capture=target_capture,
                        relation_type=relation_type,
                        navigation_group_key=navigation_group_key,
                        navigation_entries=navigation_entries,
                        branch_config=branch_config,
                        device_serial=device_serial,
                        package_name=package_name,
                        abort_event=abort_event,
                        input_rules=input_rules,
                        dynamic_patterns=dynamic_patterns,
                        stable_wait_seconds=stable_wait,
                        secret_values=secret_values,
                        stage_callback=publish_stage,
                        budget_guard=guard,
                    )
                    parent_recovery_exhausted = parent_capture is None
                    if parent_capture is not None:
                        publish_page_actions(parent, parent_capture)

            transition_buffer.flush()
            _persist_work_action_map(parent)
            if not mark_state_expanded_if_terminal(parent):
                enqueue(parent, actions=pending_actions(parent))
            current_device_path = (
                priority_handoff_path
                if priority_handoff_path is not None
                else viewport_handoff_path
                if viewport_handoff_path is not None
                else list(parent.path)
                if _capture_matches_parent(parent_capture, parent)
                else None
            )

        transition_buffer.flush()
        active_budget_parent = None
        active_budget_action = None
        if frontier_incomplete_ids:
            termination_reason = "FRONTIER_INCOMPLETE: 连续重建后探索前沿仍未耗尽"
            finalize_pending_status = "QUEUE_TRUNCATED"
            finalize_reason = termination_reason
            finalize_phase = current_phase or "explore"
            terminal_frontier_status = "ABORTED"
            logger.warning(
                "inspection frontier not exhausted before verification: "
                "run=%s branch=%s states=%s",
                run_id,
                branch_run_id,
                sorted(frontier_incomplete_ids),
            )
            return BranchOutcome(
                status="WARNING",
                stop_reason=termination_reason,
                warning=True,
            )
        if local_warning_reasons:
            logger.info(
                "inspection exploration drained after local warnings: "
                "run=%s branch=%s reasons=%s",
                run_id,
                branch_run_id,
                sorted(set(local_warning_reasons)),
            )
        verify_representative_paths()

        with Session(engine) as session:
            states = session.exec(
                select(InspectionState).where(
                    InspectionState.branch_run_id == branch_run_id
                )
            ).all()
            if any(item.is_opaque for item in states):
                warning = True
            transitions = session.exec(
                select(InspectionTransition).where(
                    InspectionTransition.branch_run_id == branch_run_id
                )
            ).all()
            if any(
                item.coordinate_only
                or item.risk_type == "UNMAPPED_INPUT"
                or item.status
                in {
                    "AMBIGUOUS",
                    "LOCATOR_DRIFT",
                    "ACTION_ERROR",
                    "UNSTABLE_PARENT",
                    "VARIANT_LIMIT",
                    "ERROR",
                }
                for item in transitions
            ):
                warning = True

        status = "FAIL" if hard_fault else "WARNING" if warning else "PASS"
        return BranchOutcome(
            status=status,
            stop_reason=termination_reason or "队列自然耗尽",
            hard_fault=hard_fault,
            warning=warning,
        )
    except ExplorationBudgetExceeded as exc:
        finalize_pending_status = "BUDGET_NOT_REACHED"
        finalize_reason = exc.reason
        finalize_phase = current_phase or "explore"
        terminal_frontier_status = "BUDGET_SKIPPED"
        warning = True
        if active_budget_parent is not None and active_budget_action is not None:
            current = _live_action(
                active_budget_parent,
                active_budget_action.action_key,
            )
            current_status = str((current or {}).get("status") or "").upper()
            if current_status in {"ACTIVE", "INVOKED"}:
                was_invoked = current_status == "INVOKED"
                disposition = "RESULT_UNKNOWN" if was_invoked else "NOT_REACHED"
                set_action_status(
                    active_budget_parent,
                    active_budget_action,
                    "BUDGET_LIMIT",
                    sequence_value=active_budget_sequence,
                    invoked=was_invoked,
                    invocation_unknown=was_invoked,
                    reason=exc.reason,
                    execution_disposition=disposition,
                    failure_type="BUDGET_LIMIT",
                )
                transition_buffer.append(
                    _transition_payload(
                        from_state_id=active_budget_parent.state_id,
                        sequence=active_budget_sequence,
                        action=active_budget_action,
                        status="BUDGET_LIMIT",
                        reason=exc.reason,
                        topology_type="TERMINAL",
                        source_observation_id=active_budget_parent.observation_id,
                        execution_disposition=disposition,
                        failure_type="BUDGET_LIMIT",
                    ),
                    None,
                )
        for unfinished in tracked_work.values():
            finalize_unqueued_work(
                unfinished,
                action_status="BUDGET_NOT_REACHED",
                state_status="BUDGET_SKIPPED",
                reason=f"{exc.reason}，保留最后 10% 用于验证代表路径",
            )
        transition_buffer.flush()
        active_budget_parent = None
        active_budget_action = None
        try:
            verify_representative_paths()
        except BudgetExceeded:
            logger.info(
                "inspection verification reserve exhausted: run=%s branch=%s",
                run_id,
                branch_run_id,
            )
        return BranchOutcome(
            status="WARNING",
            stop_reason=exc.reason,
            warning=True,
        )
    except InspectionAborted:
        finalize_pending_status = "CANCELLED"
        finalize_reason = "用户取消或设备解锁"
        finalize_phase = current_phase or "explore"
        terminal_frontier_status = "ABORTED"
        if active_budget_parent is not None and active_budget_action is not None:
            current = _live_action(
                active_budget_parent,
                active_budget_action.action_key,
            )
            current_status = str((current or {}).get("status") or "").upper()
            if current_status in {"ACTIVE", "INVOKED"}:
                was_invoked = current_status == "INVOKED"
                disposition = "RESULT_UNKNOWN" if was_invoked else "NOT_REACHED"
                set_action_status(
                    active_budget_parent,
                    active_budget_action,
                    "CANCELLED",
                    sequence_value=active_budget_sequence,
                    invoked=was_invoked,
                    invocation_unknown=was_invoked,
                    reason=finalize_reason,
                    execution_disposition=disposition,
                    failure_type="CANCELLED",
                )
                transition_buffer.append(
                    _transition_payload(
                        from_state_id=active_budget_parent.state_id,
                        sequence=active_budget_sequence,
                        action=active_budget_action,
                        status="CANCELLED",
                        reason=finalize_reason,
                        topology_type="TERMINAL",
                        source_observation_id=active_budget_parent.observation_id,
                        execution_disposition=disposition,
                        failure_type="CANCELLED",
                    ),
                    None,
                )
                transition_buffer.flush()
        raise
    except BudgetExceeded as exc:
        finalize_pending_status = "BUDGET_NOT_REACHED"
        finalize_reason = exc.reason
        finalize_phase = current_phase or "explore"
        terminal_frontier_status = "BUDGET_SKIPPED"
        if active_budget_parent is not None and active_budget_action is not None:
            current = _live_action(
                active_budget_parent,
                active_budget_action.action_key,
            )
            current_status = str((current or {}).get("status") or "").upper()
            if current_status in {"ACTIVE", "INVOKED"}:
                was_invoked = current_status == "INVOKED"
                disposition = "RESULT_UNKNOWN" if was_invoked else "NOT_REACHED"
                reason = exc.reason
                set_action_status(
                    active_budget_parent,
                    active_budget_action,
                    "BUDGET_LIMIT",
                    sequence_value=active_budget_sequence,
                    invoked=was_invoked,
                    invocation_unknown=was_invoked,
                    reason=reason,
                    execution_disposition=disposition,
                    failure_type="BUDGET_LIMIT",
                )
                transition_buffer.append(
                    _transition_payload(
                        from_state_id=active_budget_parent.state_id,
                        sequence=active_budget_sequence,
                        action=active_budget_action,
                        status="BUDGET_LIMIT",
                        reason=reason,
                        topology_type="TERMINAL",
                        source_observation_id=active_budget_parent.observation_id,
                        execution_disposition=disposition,
                        failure_type="BUDGET_LIMIT",
                    ),
                    None,
                )
                transition_buffer.flush()
        drain_monitor_events(None)
        return BranchOutcome(
            status="WARNING",
            stop_reason=exc.reason,
            warning=True,
        )
    except BranchPreparationFailed as exc:
        drain_monitor_events(None)
        return BranchOutcome(status="FAIL", stop_reason=_safe_error(exc), hard_fault=True)
    finally:
        try:
            drain_monitor_events(None)
        except Exception:
            logger.exception(
                "inspection monitor event drain failed: run=%s branch=%s",
                run_id,
                branch_run_id,
            )
        transition_buffer.flush()
        for work in tracked_work.values():
            try:
                finalize_action_map(
                    work.action_map,
                    pending_status=finalize_pending_status,
                    reason=finalize_reason,
                    phase=finalize_phase,
                )
                _persist_work_action_map(work)
                if (
                    terminal_frontier_status is not None
                    and work.state_id not in expanded_state_ids
                ):
                    update_state_frontier(
                        work.state_id,
                        status=terminal_frontier_status,
                        pending_count=0,
                        completed=True,
                    )
            except Exception:
                logger.exception(
                    "inspection action map finalization failed: run=%s state=%s",
                    run_id,
                    work.state_id,
                )
        try:
            live_snapshot = inspection_live_registry.snapshot(run_id) or {}
            live_state_id = (live_snapshot.get("page") or {}).get("state_id")
            live_work = tracked_work.get(int(live_state_id or 0))
            if live_work is not None:
                _publish_live(
                    run_id,
                    "PAGE_ACTIONS",
                    branch_key=branch.branch_key,
                    phase="finalize",
                    current_stage="保存历史动作地图",
                    run_status="RUNNING",
                    page=_live_page(live_work),
                    actions=_live_actions(live_work),
                    current_action=None,
                    overlay_visible=False,
                    canvas_matches_panel=False,
                    device_context={
                        "phase": "finalize",
                        "canvas_matches_panel": False,
                    },
                    progress=dict(progress),
                )
        except Exception:
            logger.exception(
                "inspection finalized action map publish failed: run=%s branch=%s",
                run_id,
                branch_run_id,
            )
        for index in range(len(secret_values)):
            secret_values[index] = ""


def _update_run_summary(
    run_id: int,
    *,
    final_status: Optional[str] = None,
    stop_reason: Optional[str] = None,
) -> None:
    with Session(engine) as session:
        run = session.get(InspectionRun, run_id)
        if run is None:
            return
        branches = session.exec(
            select(InspectionBranchRun).where(InspectionBranchRun.run_id == run_id)
        ).all()
        states = session.exec(
            select(InspectionState).where(InspectionState.run_id == run_id)
        ).all()
        transitions = session.exec(
            select(InspectionTransition).where(InspectionTransition.run_id == run_id)
        ).all()
        faults = session.exec(
            select(InspectionFault).where(InspectionFault.run_id == run_id)
        ).all()
        run.total_branches = len(branches)
        run.total_states = len(states)
        run.total_clusters = len({item.cluster_key for item in states})
        run.total_transitions = len(transitions)
        run.blocked_count = sum(
            1
            for item in transitions
            if item.status in {"BLOCKED", "COORDINATE_ONLY", "AMBIGUOUS"}
        )
        run.stable_count = sum(
            1
            for item in states
            if str(item.stable_status or "").upper()
            in {"STABLE", "VERIFIED_TWICE"}
        )
        run.fault_count = sum(item.occurrence_count for item in faults)
        if final_status:
            run.status = final_status
            run.current_stage = "完成" if final_status != "ABORTED" else "已取消"
            if stop_reason:
                run.stop_reason = stop_reason
            run.finished_at = _now()
        session.add(run)
        session.commit()


def _finish_active_branches(run_id: int, *, status: str, reason: str) -> None:
    with Session(engine) as session:
        branches = session.exec(
            select(InspectionBranchRun).where(
                InspectionBranchRun.run_id == run_id,
                InspectionBranchRun.status.in_(["PENDING", "RUNNING"]),
            )
        ).all()
        for branch in branches:
            states = session.exec(
                select(InspectionState).where(
                    InspectionState.branch_run_id == branch.id
                )
            ).all()
            transitions = session.exec(
                select(InspectionTransition).where(
                    InspectionTransition.branch_run_id == branch.id
                )
            ).all()
            faults = session.exec(
                select(InspectionFault).where(
                    InspectionFault.branch_run_id == branch.id
                )
            ).all()
            branch.status = status
            branch.current_stage = "已取消" if status == "ABORTED" else "异常结束"
            branch.stop_reason = reason
            branch.state_count = len(states)
            branch.transition_count = len(transitions)
            branch.blocked_count = sum(
                1
                for item in transitions
                if item.status in {"BLOCKED", "COORDINATE_ONLY", "AMBIGUOUS"}
            )
            branch.stable_count = sum(
                1
                for item in states
                if str(item.stable_status or "").upper()
                in {"STABLE", "VERIFIED_TWICE"}
            )
            branch.fault_count = sum(item.occurrence_count for item in faults)
            branch.finished_at = _now()
            session.add(branch)
        session.commit()


def execute_inspection_run(
    run_id: int,
    abort_event: Optional[threading.Event] = None,
) -> None:
    """Background entrypoint used by the API and scheduler."""
    event = abort_event or abort_event_for_run(run_id)
    lease: Optional[DeviceExecutionLease] = None
    monitor: Optional[InspectionMonitorSession] = None
    registered_abort = False
    final_status = "ERROR"
    final_stop_reason: Optional[str] = None
    outcomes: List[BranchOutcome] = []
    budget_guard: Optional[BudgetGuard] = None

    with Session(engine) as session:
        run = session.get(InspectionRun, run_id)
        if run is None:
            discard_abort_event(run_id)
            return
        device_serial = run.device_serial
        package_name = run.package_name
        profile = dict(run.profile_snapshot or {})
        budget_guard = BudgetGuard(dict(profile.get("budgets") or {}))
        executor_id = int(run.executor_id or 0)
        package_id = run.package_id

    try:
        inspection_live_registry.start_run(run_id, device_serial, "PENDING")
    except Exception:
        logger.exception("inspection live registry initialization failed: run=%s", run_id)

    try:
        _check_abort(event)
        _publish_live(
            run_id,
            "RUN_STAGE",
            phase="lease",
            current_stage="获取设备租约",
            run_status="PENDING",
            overlay_visible=False,
        )
        lease = DeviceExecutionLease.acquire(
            user_id=executor_id,
            serial=device_serial,
            task_id=f"inspection:{run_id}",
            kind="inspection",
            abort_event=event,
        )
        register_device_abort(device_serial, event)
        registered_abort = True
        _check_abort(event)
        with Session(engine) as session:
            run = session.get(InspectionRun, run_id)
            if run is None:
                return
            run.status = "RUNNING"
            run.current_stage = "连接设备"
            run.started_at = run.started_at or _now()
            session.add(run)
            session.commit()

        _publish_live(
            run_id,
            "RUN_STAGE",
            phase="connect",
            current_stage="连接设备",
            run_status="RUNNING",
            overlay_visible=False,
        )

        if package_id is not None:
            with Session(engine) as session:
                run = session.get(InspectionRun, run_id)
                if run:
                    run.current_stage = "安装巡检 APK"
                    session.add(run)
                    session.commit()
            _publish_live(
                run_id,
                "RUN_STAGE",
                phase="install",
                current_stage="安装巡检 APK",
                run_status="RUNNING",
                overlay_visible=False,
            )
            _check_abort(event)
            budget_guard.before_device_interaction(
                "install_package",
                mutating=True,
            )
            _install_requested_package(int(package_id), device_serial)
            budget_guard.check_deadline()
            _check_abort(event)

        budget_guard.before_device_interaction("connect_device")
        device = connect_android(device_serial)
        budget_guard.check_deadline()
        monitor_options = dict(profile.get("monitor_options") or {})
        monitor = InspectionMonitorSession(
            device_serial=device_serial,
            package_name=package_name,
            run_id=run_id,
            report_dir=_safe_report_path(
                _reports_root() / "inspection" / str(run_id) / "monitor"
            ),
            capture_log=bool(monitor_options.get("capture_log", True)),
            enable_performance_monitor=bool(
                monitor_options.get("enable_performance_monitor", True)
            ),
            enable_jank_frame_monitor=bool(
                monitor_options.get("enable_jank_frame_monitor", False)
            ),
            enable_perfetto_trace=bool(
                monitor_options.get("enable_perfetto_trace", False)
            ),
            enable_local_replay=bool(
                monitor_options.get("enable_local_replay", True)
            ),
        )
        try:
            _publish_live(
                run_id,
                "RUN_STAGE",
                phase="monitor",
                current_stage="启动故障与性能监控",
                run_status="RUNNING",
                overlay_visible=False,
            )
            monitor.start()
        except Exception:
            logger.exception("inspection monitoring degraded: run=%s", run_id)
            monitor = None

        selected_branches = list(profile.get("selected_branches") or [])
        if not selected_branches:
            with Session(engine) as session:
                run = session.get(InspectionRun, run_id)
                selected_branches = list(run.selected_branches or []) if run else []
        branch_configs = dict(profile.get("branches") or {})
        with Session(engine) as session:
            branch_rows = session.exec(
                select(InspectionBranchRun)
                .where(InspectionBranchRun.run_id == run_id)
                .order_by(InspectionBranchRun.id)
            ).all()
            branch_ids = {
                row.branch_key: row.id
                for row in branch_rows
                if row.branch_key in selected_branches
            }

        for branch_index, branch_key in enumerate(selected_branches):
            _check_abort(event)
            _publish_live(
                run_id,
                "RUN_STAGE",
                branch_key=branch_key,
                phase="branch",
                current_stage=f"巡检业务线: {branch_key}",
                run_status="RUNNING",
                overlay_visible=False,
            )
            branch_id = branch_ids.get(branch_key)
            config = branch_configs.get(branch_key)
            if not branch_id or not isinstance(config, dict):
                missing_outcome = BranchOutcome(
                    status="FAIL",
                    stop_reason=f"业务线配置不存在: {branch_key}",
                    hard_fault=True,
                )
                outcomes.append(missing_outcome)
                if branch_id:
                    _finish_branch(branch_id, missing_outcome)
                continue
            with Session(engine) as session:
                run = session.get(InspectionRun, run_id)
                if run:
                    run.current_stage = f"巡检业务线: {branch_key}"
                    session.add(run)
                    session.commit()
            outcome = _execute_branch(
                run_id=run_id,
                branch_run_id=branch_id,
                device=device,
                device_serial=device_serial,
                package_name=package_name,
                profile=profile,
                branch_config=dict(config),
                abort_event=event,
                monitor=monitor,
                budget_guard=budget_guard.for_branch(
                    len(selected_branches) - branch_index
                ),
            )
            outcomes.append(outcome)
            _finish_branch(branch_id, outcome)
            _update_run_summary(run_id)

        _check_abort(event)
        if any(item.status == "ERROR" for item in outcomes):
            final_status = "ERROR"
        elif any(item.status == "FAIL" for item in outcomes):
            final_status = "FAIL"
        elif any(item.status == "WARNING" for item in outcomes):
            final_status = "WARNING"
        else:
            final_status = "PASS"
        final_stop_reason = next(
            (
                item.stop_reason
                for item in outcomes
                if item.status == final_status and item.stop_reason
            ),
            None,
        )
    except BudgetExceeded as exc:
        final_status = "WARNING"
        final_stop_reason = exc.reason
        _finish_active_branches(
            run_id,
            status="WARNING",
            reason=exc.reason,
        )
    except InspectionAborted:
        final_status = "ABORTED"
        final_stop_reason = "用户取消或设备解锁"
        _finish_active_branches(
            run_id,
            status="ABORTED",
            reason="用户取消或设备解锁",
        )
    except DeviceDisconnected as exc:
        final_status = "ERROR"
        final_stop_reason = "设备断连"
        _persist_fault(
            run_id=run_id,
            branch_run_id=None,
            state_id=None,
            fault_type="DEVICE_DISCONNECTED",
            summary="巡检设备断连",
            event=_fault_event_with_replay(
                monitor,
                "DEVICE_DISCONNECTED",
                full_log=_safe_error(exc),
                budget_guard=budget_guard,
            ),
            recent_actions=[],
            secret_values=[],
            device_serial=device_serial,
            budget_guard=budget_guard,
        )
        _finish_active_branches(
            run_id,
            status="ERROR",
            reason="设备断连",
        )
        with Session(engine) as session:
            run = session.get(InspectionRun, run_id)
            if run:
                run.error_message = _safe_error(exc)
                run.stop_reason = "设备断连"
                session.add(run)
                session.commit()
    except Exception as exc:
        logger.exception("inspection run failed: run=%s", run_id)
        final_status = "ERROR"
        final_stop_reason = "基础设施异常"
        _finish_active_branches(
            run_id,
            status="ERROR",
            reason="基础设施异常",
        )
        with Session(engine) as session:
            run = session.get(InspectionRun, run_id)
            if run:
                run.error_message = _safe_error(exc)
                run.stop_reason = "基础设施异常"
                session.add(run)
                session.commit()
    finally:
        if monitor:
            try:
                monitor.stop()
            except Exception:
                logger.exception("inspection monitor stop failed: run=%s", run_id)
        if registered_abort:
            try:
                unregister_device_abort(device_serial)
            except Exception:
                logger.exception(
                    "inspection device abort unregister failed: run=%s serial=%s",
                    run_id,
                    device_serial,
                )
        if lease:
            try:
                lease.release()
            except Exception:
                logger.exception(
                    "inspection device lease release failed: run=%s serial=%s",
                    run_id,
                    device_serial,
                )
        try:
            _update_run_summary(
                run_id,
                final_status=final_status,
                stop_reason=final_stop_reason,
            )
        except Exception:
            logger.exception("inspection run summary update failed: run=%s", run_id)
        try:
            inspection_live_registry.finish_run(
                run_id,
                final_status,
                "完成" if final_status != "ABORTED" else "已取消",
                reason=final_stop_reason,
            )
        except Exception:
            logger.exception(
                "inspection live terminal publish failed: run=%s status=%s",
                run_id,
                final_status,
            )
        finally:
            discard_abort_event(run_id)
