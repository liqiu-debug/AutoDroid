"""Installed-version inspection replay planning and execution.

This module deliberately does not install applications, execute entry cases, or
compare screenshots.  It consumes a completed inspection graph and exposes a
small, auditable kernel for replaying safe semantic paths on the version that is
already installed on a device.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from sqlmodel import Session, select

from backend.inspection.device import (
    CapturedPage,
    DeviceDisconnected,
    InspectionAborted,
    LocatorAmbiguous,
    LocatorDrift,
    perform_action,
    wait_for_stable_page,
)
from backend.inspection.semantics import (
    InspectionAction,
    PageModel,
    derive_instance_anchor,
    enumerate_actions,
)
from backend.models import (
    InspectionBranchRun,
    InspectionExplorationFamily,
    InspectionFault,
    InspectionObservation,
    InspectionPageTemplate,
    InspectionRun,
    InspectionState,
    InspectionTransition,
)


REPLAY_PLAN_VERSION = 3
MAX_REPLAY_CHAINS = 20
REACHABILITY_OBSERVED_ONCE = "OBSERVED_ONCE"
REACHABILITY_VERIFIED_TWICE = "VERIFIED_TWICE"
REACHABILITY_UNSTABLE = "UNSTABLE"
REACHABILITY_UNKNOWN = "UNKNOWN"

REPLAY_SCOPE_FULL_PATH = "FULL_PATH"
REPLAY_SCOPE_SAFETY_PREFIX = "PREFIX_TO_SAFETY_BOUNDARY"
REPLAY_SCOPE_DIAGNOSTIC_ONLY = "DIAGNOSTIC_ONLY"
REPLAY_SCOPE_NONE = "NONE"

BOUNDARY_EVIDENCE_VERIFIED = "VERIFIED"
BOUNDARY_EVIDENCE_NOT_VERIFIABLE = "NOT_VERIFIABLE"
BOUNDARY_EVIDENCE_CHANGED = "CHANGED"

_COMPLETED_RUN_STATUSES = {"PASS", "WARNING", "FAIL", "COMPLETED", "FINISHED"}
_COMPLETED_BRANCH_STATUSES = {"PASS", "WARNING", "FAIL", "COMPLETED", "FINISHED"}
_SUPPORTED_ACTION_TYPES = {"back", "click", "input", "scroll"}
_MAIN_ENTRY_SUBTYPES = {
    "HOME",
    "CATALOG_CATEGORY",
    "COMMUNITY_FEED",
    "CART",
    "PROFILE",
}
_CRITICAL_SUBTYPES = {
    "CHECKOUT",
    "CASHIER",
    "ORDER",
    "ORDER_DETAIL",
    "PRODUCT_DETAIL",
    "PURCHASE_OPTIONS",
}
_SPECIAL_LIST_SUBTYPES = {
    "CONSUMABLE_LIST",
    "PRODUCT_LIST",
    "SERVICE_LIST",
    "STORE_LIST",
    "STORE_DETAIL",
}


class ReplayPlanError(ValueError):
    """A stable error code suitable for an API preflight response."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> Dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class ReplayActionBinding:
    status: str
    action: Optional[InspectionAction] = None
    failure_type: Optional[str] = None
    reason: Optional[str] = None
    risk_type: Optional[str] = None
    candidate_count: int = 0
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "failure_type": self.failure_type,
            "reason": self.reason,
            "risk_type": self.risk_type,
            "candidate_count": self.candidate_count,
            "evidence": dict(self.evidence),
        }


@dataclass
class ReplayExecutionResult:
    status: str
    reason: Optional[str]
    failure_type: Optional[str]
    trace: List[Dict[str, Any]]
    completed_checkpoints: int
    boundary_evidence: str = "NOT_APPLICABLE"
    warning_codes: List[str] = field(default_factory=list)
    failed_step_index: Optional[int] = None
    last_capture: Optional[CapturedPage] = field(default=None, repr=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "failure_type": self.failure_type,
            "trace": [dict(item) for item in self.trace],
            "completed_checkpoints": self.completed_checkpoints,
            "boundary_evidence": self.boundary_evidence,
            "warning_codes": list(self.warning_codes),
            "failed_step_index": self.failed_step_index,
        }


def normalise_terminal_outcome(value: Any) -> str:
    """Map historical transition/fault names to the public terminal axis."""
    text = str(value or "").strip().upper()
    if text in {"BLOCKED", "PAYMENT", "DESTRUCTIVE", "EXTERNAL_SIDE_EFFECT", "SAFETY_BLOCKED"}:
        return "SAFETY_BLOCKED"
    if text in {
        "LOCATOR_DRIFT",
        "LOCATOR_NOT_FOUND",
        "LOCATOR_AMBIGUOUS",
        "COORDINATE_STALE",
        "COORDINATE_UNSAFE",
        "PARENT_RECOVERY_FAILED",
        "PATH_DIVERGED",
        "PATH_DIVERGED_CASCADE",
        "PARENT_RECOVERY_CASCADE",
    }:
        return "LOCATOR_FAILED"
    if text in {"CRASH", "ANR", "APP_EXIT", "WHITE_SCREEN", "UI_UNRESPONSIVE", "APP_FAULT"}:
        return "APP_FAULT"
    if text in {
        "ACTION_ERROR",
        "ACTION_EXECUTION_FAILED",
        "AUTOMATION_ERROR",
        "AUTOMATION_FAILED",
        "ERROR",
        "EXECUTION_ERROR",
        "INVALID_REPLAY_PLAN",
        "INPUT_VALUE_MISSING",
        "FRONTIER_INCOMPLETE",
    }:
        return "AUTOMATION_FAILED"
    if text in {"EXTERNAL_APP", "EXTERNAL_NAVIGATION"}:
        return "EXTERNAL_NAVIGATION"
    if text in {"DEVICE_DISCONNECTED", "DEVICE_ERROR", "INFRA_FAULT"}:
        return "INFRA_FAULT"
    if text in {"BUDGET_LIMIT", "BUDGET_NOT_REACHED", "QUEUE_TRUNCATED", "BUDGET_STOP"}:
        return "BUDGET_STOP"
    if text in {"CANCELLED", "ABORTED"}:
        return "CANCELLED"
    return "NONE"


def legacy_replay_eligibility(replay_scope: Any) -> str:
    """Return the v2/v6 value while callers migrate to the replay-scope axis."""
    scope = str(replay_scope or "").strip().upper()
    if scope in {REPLAY_SCOPE_FULL_PATH, "FULL"}:
        return "FULL"
    if scope in {REPLAY_SCOPE_SAFETY_PREFIX, "SAFE_PREFIX"}:
        return "SAFE_PREFIX"
    return "NONE"


def _normalise_terminal_outcome(value: Any) -> str:
    """Backward-compatible private alias used by older imports/tests."""
    return normalise_terminal_outcome(value)


def _transition_terminal_outcome(transition: InspectionTransition) -> str:
    failure_outcome = normalise_terminal_outcome(transition.failure_type)
    if failure_outcome != "NONE":
        return failure_outcome
    status = str(transition.status or "").upper()
    disposition = str(transition.execution_disposition or "").upper()
    status_outcome = normalise_terminal_outcome(status)
    if status_outcome != "NONE":
        return status_outcome
    risk = str(transition.risk_type or "").upper()
    risk_outcome = normalise_terminal_outcome(risk)
    # A risk label describes what an action could do, not what happened.  A
    # PAYMENT action that was deliberately allowed and executed must remain a
    # normal transition.  Historical rows without failure_type are considered
    # safety boundaries only when they explicitly record non-execution.
    if risk_outcome == "SAFETY_BLOCKED" and (
        status in {"SKIPPED", "NOT_INVOKED", "POLICY_BLOCKED"}
        or disposition in {"SKIPPED", "NOT_INVOKED"}
    ):
        return risk_outcome
    return "NONE"


def _boundary_evidence_from_action(
    outcome: str,
    action: Mapping[str, Any],
) -> str:
    """Source reports prove a boundary existed, not that a new build preserves it.

    A semantic source locator is sufficient evidence that the inspection observed
    the boundary control. Coordinate-only and selector-free historical records
    remain probe candidates, but are explicitly not claimed as verified.
    """
    if str(outcome or "").upper() != "SAFETY_BLOCKED":
        return BOUNDARY_EVIDENCE_NOT_VERIFIABLE
    locators = [
        item
        for item in (action.get("locator_candidates") or [])
        if isinstance(item, Mapping) and str(item.get("selector") or "").strip()
    ]
    if locators and not bool(action.get("coordinate_only")):
        return BOUNDARY_EVIDENCE_VERIFIED
    return BOUNDARY_EVIDENCE_NOT_VERIFIABLE


def aggregate_boundary_evidence(
    boundaries: Iterable[Mapping[str, Any]],
) -> str:
    """Aggregate only safety-boundary evidence with conservative precedence."""
    values = {
        str(item.get("boundary_evidence") or "").strip().upper()
        for item in boundaries
        if str(item.get("terminal_outcome") or "").strip().upper()
        == "SAFETY_BLOCKED"
    }
    if BOUNDARY_EVIDENCE_CHANGED in values:
        return BOUNDARY_EVIDENCE_CHANGED
    if BOUNDARY_EVIDENCE_NOT_VERIFIABLE in values:
        return BOUNDARY_EVIDENCE_NOT_VERIFIABLE
    if BOUNDARY_EVIDENCE_VERIFIED in values:
        return BOUNDARY_EVIDENCE_VERIFIED
    return "NOT_APPLICABLE"


def _action_snapshot_from_transition(transition: InspectionTransition) -> Dict[str, Any]:
    """Create a replay-safe, selector-free action snapshot for a boundary."""
    return {
        "action_type": str(transition.action_type or "click"),
        "action_key": str(transition.action_key or ""),
        "locator_candidates": [
            dict(item) for item in (transition.locator_candidates or [])
            if isinstance(item, Mapping)
        ],
        "target_meta": dict(transition.target_meta or {}),
        "coordinate_only": bool(transition.coordinate_only),
        "replayable": bool(transition.replayable),
        "risk_type": transition.risk_type,
        "blocked_reason": transition.reason,
        "action_role": transition.action_role,
        "action_role_key": transition.action_role_key,
        "action_group_key": transition.action_group_key,
    }


def _action_snapshot_from_fault(fault: InspectionFault) -> Dict[str, Any]:
    details = fault.details if isinstance(fault.details, Mapping) else {}
    current = details.get("current_action")
    if not isinstance(current, Mapping):
        current = {}
    return {
        "action_type": str(current.get("action_type") or "click"),
        "action_key": str(current.get("action_key") or ""),
        "locator_candidates": [
            dict(item) for item in (current.get("locator_candidates") or [])
            if isinstance(item, Mapping)
        ],
        "target_meta": dict(current.get("target_meta") or {}),
        "coordinate_only": bool(current.get("coordinate_only")),
        "replayable": bool(current.get("replayable", True)),
        "risk_type": current.get("risk_type"),
        "blocked_reason": current.get("blocked_reason"),
        "action_role": current.get("action_role"),
        "action_role_key": current.get("action_role_key"),
        "action_group_key": current.get("action_group_key"),
    }


def terminal_boundaries_for_state(
    state_id: Optional[int],
    transitions: Iterable[InspectionTransition] = (),
    faults: Iterable[InspectionFault] = (),
) -> List[Dict[str, Any]]:
    """Return user-facing terminal boundaries without treating them as page failure."""
    boundaries: List[Dict[str, Any]] = []
    for transition in transitions:
        if int(transition.from_state_id or 0) != int(state_id or 0):
            continue
        outcome = _transition_terminal_outcome(transition)
        status = str(transition.status or "").upper()
        if outcome == "NONE" and status not in {
            "BLOCKED", "LOCATOR_DRIFT", "LOCATOR_NOT_FOUND", "LOCATOR_AMBIGUOUS",
            "COORDINATE_STALE", "COORDINATE_UNSAFE", "ACTION_ERROR", "ERROR",
            "APP_EXIT", "WHITE_SCREEN", "BUDGET_LIMIT", "CANCELLED",
        }:
            continue
        action = _action_snapshot_from_transition(transition)
        boundaries.append({
            "boundary_id": f"transition-{transition.id}",
            "boundary_type": (
                "SAFETY_BLOCKED" if outcome == "SAFETY_BLOCKED" else
                "APP_FAULT" if outcome == "APP_FAULT" else
                "AUTOMATION_FAILED" if outcome == "AUTOMATION_FAILED" else
                "EXTERNAL_NAVIGATION" if outcome == "EXTERNAL_NAVIGATION" else
                "INFRA_FAULT" if outcome == "INFRA_FAULT" else
                "LOCATOR_FAILED" if outcome == "LOCATOR_FAILED" else
                "BUDGET_STOP" if outcome == "BUDGET_STOP" else status or "TERMINAL"
            ),
            "terminal_outcome": outcome,
            "boundary_evidence": _boundary_evidence_from_action(outcome, action),
            "state_id": int(state_id) if state_id is not None else None,
            "transition_id": transition.id,
            "action_key": transition.action_key,
            "action_role": transition.action_role,
            "risk_type": transition.risk_type,
            "reason": transition.reason,
            "status": status,
            "failure_type": transition.failure_type,
            "attention_required": bool(
                outcome != "SAFETY_BLOCKED"
                or str(transition.risk_type or "").upper() == "UNMAPPED_INPUT"
            ),
            "action": action,
        })
    for fault in faults:
        if fault.state_id is not None and int(fault.state_id) != int(state_id or 0):
            continue
        outcome = normalise_terminal_outcome(fault.fault_type)
        if outcome == "NONE":
            outcome = "AUTOMATION_FAILED"
        action = _action_snapshot_from_fault(fault)
        boundaries.append({
            "boundary_id": f"fault-{fault.id}",
            "boundary_type": outcome,
            "terminal_outcome": outcome,
            "boundary_evidence": BOUNDARY_EVIDENCE_NOT_VERIFIABLE,
            "state_id": int(state_id) if state_id is not None else fault.state_id,
            "fault_id": fault.id,
            "fault_type": fault.fault_type,
            "transition_id": fault.transition_id,
            "action_key": (
                str((action.get("action_key") or ""))
                or None
            ),
            "action_role": action.get("action_role"),
            "reason": fault.summary,
            "occurrence_count": int(fault.occurrence_count or 1),
            "attention_required": True,
            "action": action,
        })
    boundaries.sort(key=lambda item: (str(item.get("boundary_type")), int(item.get("transition_id") or item.get("fault_id") or 0)))
    return boundaries


def state_reachability_evidence(
    state: InspectionState,
    *,
    has_observation: bool = False,
) -> str:
    status = str(state.stable_status or "").upper()
    observed = bool(
        has_observation
        or int(state.observation_count or 0) > 0
        or state.screenshot_path
        or state.xml_path
    )
    if status == "VERIFIED_TWICE" and observed:
        return REACHABILITY_VERIFIED_TWICE
    if status == "STABLE" and observed:
        # Historical runs marked an empty root path STABLE without replaying it.
        # Only independent captures can substantiate a twice-verified root.
        if not list(state.first_path or []) and int(state.observation_count or 0) < 2:
            return REACHABILITY_OBSERVED_ONCE
        return REACHABILITY_VERIFIED_TWICE
    if status in {"UNSTABLE", "PATH_DIVERGED"}:
        return REACHABILITY_UNSTABLE
    if observed:
        return REACHABILITY_OBSERVED_ONCE
    return REACHABILITY_UNKNOWN


def _safe_path_prefix(path: Sequence[Mapping[str, Any]]) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Split at the first unsafe step; the unsafe action is never replayed."""
    prefix: List[Dict[str, Any]] = []
    for index, raw_step in enumerate(path):
        reason = _path_safety_failure([raw_step])
        if reason is None:
            prefix.append(dict(raw_step))
            continue
        step = dict(raw_step) if isinstance(raw_step, Mapping) else {}
        outcome = normalise_terminal_outcome(step.get("risk_type") or reason)
        if outcome == "NONE":
            outcome = (
                "SAFETY_BLOCKED"
                if reason == "HISTORICAL_RISK"
                else "LOCATOR_FAILED"
                if reason in {
                    "NOT_REPLAYABLE",
                    "COORDINATE_ONLY",
                    "MISSING_ACTION_ROLE",
                    "MISSING_SEMANTIC_LOCATOR",
                }
                else "AUTOMATION_FAILED"
            )
        action = {
            key: value for key, value in step.items()
            if key in {
                "action_type", "action_key", "locator_candidates", "target_meta",
                "coordinate_only", "replayable", "risk_type", "blocked_reason",
                "action_role", "action_role_key", "action_group_key",
            }
        }
        boundary = {
            "boundary_id": f"path-step-{index}",
            "boundary_type": outcome,
            "terminal_outcome": outcome,
            "boundary_evidence": _boundary_evidence_from_action(outcome, action),
            "step_index": index,
            "action_key": step.get("action_key"),
            "action_role": step.get("action_role"),
            "risk_type": step.get("risk_type"),
            "reason": step.get("blocked_reason") or reason,
            "safety_failure": reason,
            "attention_required": bool(
                outcome != "SAFETY_BLOCKED"
                or str(step.get("risk_type") or "").upper() == "UNMAPPED_INPUT"
            ),
            "action": action,
        }
        return prefix, boundary
    return prefix, None


def derive_replay_eligibility(
    state: InspectionState,
    terminal_boundaries: Sequence[Mapping[str, Any]] = (),
) -> Tuple[str, List[Dict[str, Any]]]:
    prefix, path_boundary = _safe_path_prefix(state.first_path or [])
    boundaries = [dict(item) for item in terminal_boundaries]
    if path_boundary is not None:
        boundaries.insert(0, path_boundary)
    reachability = state_reachability_evidence(state)
    if str(state.expansion_status or "").upper() == "ABORTED" and not prefix:
        return REPLAY_SCOPE_NONE, boundaries
    if not (
        int(state.observation_count or 0) > 0
        or state.representative_observation_id is not None
        or state.screenshot_path
        or state.xml_path
    ):
        return REPLAY_SCOPE_NONE, boundaries
    if bool(state.is_opaque) or str(state.page_subtype or "").upper() == "OPAQUE":
        return REPLAY_SCOPE_NONE, boundaries
    # A failed action after reaching this State is terminal evidence for that
    # outgoing branch.  It does not invalidate the path that already reached
    # the State; only reachability evidence for the State itself may do that.
    if reachability == REACHABILITY_UNSTABLE:
        return REPLAY_SCOPE_DIAGNOSTIC_ONLY, boundaries
    safety_boundaries = [
        item
        for item in boundaries
        if str(item.get("terminal_outcome") or "").upper() == "SAFETY_BLOCKED"
    ]
    if safety_boundaries:
        return REPLAY_SCOPE_SAFETY_PREFIX, boundaries
    if path_boundary is not None:
        return REPLAY_SCOPE_DIAGNOSTIC_ONLY, boundaries
    return REPLAY_SCOPE_FULL_PATH, boundaries


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _json_copy(value: Any) -> Any:
    return json.loads(_canonical_json(value))


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _path_key(path: Sequence[Mapping[str, Any]]) -> str:
    identity = [
        {
            "action_type": str(step.get("action_type") or ""),
            "action_key": str(step.get("action_key") or ""),
            "action_role": str(step.get("action_role") or ""),
            "expected_source_semantic_key": str(
                step.get("expected_source_semantic_key") or ""
            ),
            "expected_target_semantic_key": str(
                step.get("expected_target_semantic_key") or ""
            ),
        }
        for step in path
    ]
    return _sha256_json({"version": REPLAY_PLAN_VERSION, "steps": identity})


def _step_records_safety_block(step: Mapping[str, Any]) -> bool:
    failure_outcome = normalise_terminal_outcome(step.get("failure_type"))
    if failure_outcome == "SAFETY_BLOCKED":
        return True
    status = str(
        step.get("status")
        or step.get("final_status")
        or step.get("result")
        or ""
    ).upper()
    disposition = str(step.get("execution_disposition") or "").upper()
    if status in {"PASS", "EXECUTED", "SUCCESS"} or disposition == "EXECUTED":
        return False
    if status in {"BLOCKED", "SKIPPED", "NOT_INVOKED", "POLICY_BLOCKED"}:
        return True
    if disposition in {"SKIPPED", "NOT_INVOKED"}:
        return True
    # Identity-v2 rows written before explicit disposition used a blocked
    # reason or replayable=false together with the risk category.
    return bool(
        step.get("blocked_reason")
        or (step.get("risk_type") and not bool(step.get("replayable", True)))
    )


def _path_safety_failure(path: Sequence[Any]) -> Optional[str]:
    for raw_step in path:
        if not isinstance(raw_step, Mapping):
            return "INVALID_PATH_STEP"
        step = raw_step
        action_type = str(step.get("action_type") or "").lower()
        if action_type not in _SUPPORTED_ACTION_TYPES:
            return "UNSUPPORTED_ACTION"
        # Risk describes the action.  It becomes a boundary only when the
        # persisted execution says the action was not invoked.
        if step.get("risk_type") and _step_records_safety_block(step):
            return "HISTORICAL_RISK"
        if not bool(step.get("replayable", True)):
            return "NOT_REPLAYABLE"
        if bool(step.get("coordinate_only")):
            return "COORDINATE_ONLY"
        if not str(step.get("action_role") or ""):
            return "MISSING_ACTION_ROLE"
        if action_type != "back" and not list(step.get("locator_candidates") or []):
            return "MISSING_SEMANTIC_LOCATOR"
        if not str(step.get("expected_source_semantic_key") or ""):
            return "MISSING_SOURCE_EXPECTATION"
        if not str(step.get("expected_target_semantic_key") or ""):
            return "MISSING_TARGET_EXPECTATION"
        if not isinstance(step.get("expected_source_signature"), Mapping):
            return "MISSING_SOURCE_SIGNATURE"
        if not isinstance(step.get("expected_target_signature"), Mapping):
            return "MISSING_TARGET_SIGNATURE"
    return None


def _state_observation_id(
    state: InspectionState,
    observations_by_state: Mapping[int, Sequence[InspectionObservation]],
) -> Optional[int]:
    if state.representative_observation_id is not None:
        return int(state.representative_observation_id)
    rows = list(observations_by_state.get(int(state.id or 0), ()))
    if not rows:
        return None
    rows.sort(
        key=lambda item: (
            bool(item.is_representative),
            int(item.sequence or 0),
            int(item.id or 0),
        ),
        reverse=True,
    )
    return int(rows[0].id) if rows[0].id is not None else None


def _expected_signature_for_state(
    state: InspectionState,
    template: Optional[InspectionPageTemplate],
) -> Dict[str, Any]:
    signature: Dict[str, Any] = {
        "version": 1,
        "package": str(state.foreground_package or "").casefold(),
        "activity_family": str(template.activity_family if template else ""),
        "role": str(template.page_role if template else "UNKNOWN"),
        "instance_anchor": str(state.instance_anchor or ""),
        "content_anchor": str(state.instance_anchor or ""),
        "structure_tokens": list(template.structure_signature if template else []),
        "action_tokens": list(template.action_signature if template else []),
        "control_tokens": list(
            template.control_state_signature if template else []
        ),
        "risk_tokens": list(template.risk_signature if template else []),
    }
    return signature


def _best_state_for_checkpoint(
    *,
    semantic_key: str,
    expected_path_length: int,
    endpoint: InspectionState,
    states_by_semantic: Mapping[str, Sequence[InspectionState]],
) -> Optional[InspectionState]:
    if (
        endpoint.semantic_key == semantic_key
        and len(endpoint.first_path or []) == expected_path_length
    ):
        return endpoint
    rows = list(states_by_semantic.get(semantic_key, ()))
    if not rows:
        return None
    rows.sort(
        key=lambda item: (
            len(item.first_path or []) == expected_path_length,
            str(item.expansion_status or "") == "EXPANDED",
            item.representative_observation_id is not None,
            -(int(item.id or 0)),
        ),
        reverse=True,
    )
    return rows[0]


def _checkpoint(
    *,
    index: int,
    semantic_key: str,
    signature: Mapping[str, Any],
    state: Optional[InspectionState],
    template: Optional[InspectionPageTemplate],
    family: Optional[InspectionExplorationFamily],
    observation_id: Optional[int],
) -> Dict[str, Any]:
    expected = dict(signature)
    role = str(expected.get("role") or (template.page_role if template else "UNKNOWN"))
    page_subtype = str(state.page_subtype if state else "UNKNOWN")
    instance_anchor = str(
        expected.get("instance_anchor")
        or (state.instance_anchor if state else "")
        or expected.get("content_anchor")
        or ""
    )
    return {
        "checkpoint_index": index,
        "state_id": int(state.id) if state is not None and state.id is not None else None,
        "source_observation_id": observation_id,
        "semantic_key": semantic_key,
        "role": role,
        "page_subtype": page_subtype,
        "instance_anchor": instance_anchor,
        "activity_family": str(
            expected.get("activity_family")
            or (template.activity_family if template else "")
            or ""
        ),
        "template_key": str(template.template_key if template else ""),
        "family_id": int(family.id) if family and family.id is not None else None,
        "family_key": str(family.family_key if family else ""),
        "expectation": _json_copy(expected),
    }


def _build_checkpoints(
    *,
    endpoint: InspectionState,
    path: Sequence[Mapping[str, Any]],
    root_state: Optional[InspectionState],
    states_by_semantic: Mapping[str, Sequence[InspectionState]],
    templates: Mapping[int, InspectionPageTemplate],
    families: Mapping[int, InspectionExplorationFamily],
    observations_by_state: Mapping[int, Sequence[InspectionObservation]],
) -> List[Dict[str, Any]]:
    if not path:
        state = endpoint if endpoint is not None else root_state
        if state is None:
            return []
        template = templates.get(int(state.template_id or 0))
        family = families.get(int(state.exploration_family_id or 0))
        return [
            _checkpoint(
                index=0,
                semantic_key=str(state.semantic_key or state.state_key or ""),
                signature=_expected_signature_for_state(state, template),
                state=state,
                template=template,
                family=family,
                observation_id=_state_observation_id(state, observations_by_state),
            )
        ]

    checkpoints: List[Dict[str, Any]] = []
    first = path[0]
    source_semantic = str(first.get("expected_source_semantic_key") or "")
    source_state = _best_state_for_checkpoint(
        semantic_key=source_semantic,
        expected_path_length=0,
        endpoint=endpoint,
        states_by_semantic=states_by_semantic,
    ) or root_state
    source_template = templates.get(int(source_state.template_id or 0)) if source_state else None
    source_family = (
        families.get(int(source_state.exploration_family_id or 0))
        if source_state
        else None
    )
    checkpoints.append(
        _checkpoint(
            index=0,
            semantic_key=source_semantic,
            signature=dict(first.get("expected_source_signature") or {}),
            state=source_state,
            template=source_template,
            family=source_family,
            observation_id=(
                _state_observation_id(source_state, observations_by_state)
                if source_state
                else None
            ),
        )
    )
    for step_index, step in enumerate(path):
        target_semantic = str(step.get("expected_target_semantic_key") or "")
        target_state = _best_state_for_checkpoint(
            semantic_key=target_semantic,
            expected_path_length=step_index + 1,
            endpoint=endpoint,
            states_by_semantic=states_by_semantic,
        )
        target_template = (
            templates.get(int(target_state.template_id or 0)) if target_state else None
        )
        target_family = (
            families.get(int(target_state.exploration_family_id or 0))
            if target_state
            else None
        )
        checkpoints.append(
            _checkpoint(
                index=step_index + 1,
                semantic_key=target_semantic,
                signature=dict(step.get("expected_target_signature") or {}),
                state=target_state,
                template=target_template,
                family=target_family,
                observation_id=(
                    _state_observation_id(target_state, observations_by_state)
                    if target_state
                    else None
                ),
            )
        )
    return checkpoints


def _coverage_tokens(checkpoints: Sequence[Mapping[str, Any]]) -> set[str]:
    # Keep a root checkpoint selectable even when an old identity-v2 capture
    # has no classified role/family metadata.
    tokens: set[str] = {"ROOT"} if checkpoints else set()
    for item in checkpoints:
        role = str(item.get("role") or "UNKNOWN").upper()
        subtype = str(item.get("page_subtype") or "UNKNOWN").upper()
        family_id = item.get("family_id")
        if role != "UNKNOWN":
            tokens.add(f"ROLE:{role}")
        if subtype != "UNKNOWN":
            tokens.add(f"SUBTYPE:{subtype}")
        if subtype in _MAIN_ENTRY_SUBTYPES:
            tokens.add(f"ENTRY:{subtype}")
        if family_id is not None:
            tokens.add(f"FAMILY:{int(family_id)}")
    return tokens


def _boundary_coverage_tokens(boundaries: Sequence[Mapping[str, Any]]) -> set[str]:
    tokens: set[str] = set()
    for item in boundaries:
        outcome = str(item.get("terminal_outcome") or "NONE").upper()
        role = str(item.get("action_role") or item.get("fault_type") or "UNKNOWN")
        risk = str(item.get("risk_type") or "")
        if outcome in {"SAFETY_BLOCKED", "APP_FAULT"}:
            tokens.add(f"BOUNDARY:{outcome}:{risk}:{role}")
        else:
            tokens.add(f"BOUNDARY:{outcome}")
    return tokens


def _token_weight(token: str) -> int:
    _, _, value = token.partition(":")
    if token.startswith("ENTRY:"):
        return 250
    if token.startswith("BOUNDARY:APP_FAULT"):
        return 320
    if token.startswith("BOUNDARY:SAFETY_BLOCKED"):
        return 280
    if token.startswith("BOUNDARY:"):
        return 170
    if token.startswith("SUBTYPE:") and value in _CRITICAL_SUBTYPES:
        return 300
    if token.startswith("SUBTYPE:") and value in _SPECIAL_LIST_SUBTYPES:
        return 180
    if token.startswith("ROLE:") and value in {
        "CHECKOUT",
        "ORDER",
        "PRODUCT_DETAIL",
    }:
        return 160
    if token.startswith("SUBTYPE:"):
        return 80
    if token.startswith("ROLE:"):
        return 45
    if token.startswith("FAMILY:"):
        return 12
    if token == "ROOT":
        return 1
    return 1


def _select_chains(
    candidates: Sequence[Dict[str, Any]],
    max_chains: int,
) -> List[Dict[str, Any]]:
    remaining = list(candidates)
    selected: List[Dict[str, Any]] = []
    covered: set[str] = set()
    while remaining and len(selected) < max_chains:
        ranked: List[Tuple[Tuple[Any, ...], Dict[str, Any]]] = []
        for candidate in remaining:
            marginal = set(candidate["_coverage_tokens"]) - covered
            gain = sum(_token_weight(token) for token in marginal)
            critical = sum(1 for token in marginal if _token_weight(token) >= 160)
            evidence_rank = 1 if candidate["evidence_level"] == "VERIFIED_TWICE" else 0
            ranked.append(
                (
                    (
                        gain,
                        critical,
                        len(marginal),
                        evidence_rank,
                        -len(candidate["first_path"]),
                        candidate["path_key"],
                    ),
                    candidate,
                )
            )
        score, chosen = max(ranked, key=lambda item: item[0])
        if score[0] <= 0:
            break
        selected.append(chosen)
        covered.update(chosen["_coverage_tokens"])
        remaining.remove(chosen)
    return selected


def _prefix_tree(chains: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    nodes: Dict[str, Dict[str, Any]] = {
        "root": {
            "node_id": "root",
            "parent_id": None,
            "depth": 0,
            "action_key": None,
            "action_role": None,
            "chain_ids": [],
        }
    }
    for chain in chains:
        chain_id = str(chain.get("chain_id") or "")
        nodes["root"]["chain_ids"].append(chain_id)
        parent_id = "root"
        prefix: List[Dict[str, str]] = []
        for depth, step in enumerate(chain.get("first_path") or [], start=1):
            prefix.append(
                {
                    "action_key": str(step.get("action_key") or ""),
                    "target": str(step.get("expected_target_semantic_key") or ""),
                }
            )
            node_id = _sha256_json(prefix)[:24]
            node = nodes.setdefault(
                node_id,
                {
                    "node_id": node_id,
                    "parent_id": parent_id,
                    "depth": depth,
                    "action_key": str(step.get("action_key") or ""),
                    "action_role": str(step.get("action_role") or ""),
                    "chain_ids": [],
                },
            )
            node["chain_ids"].append(chain_id)
            parent_id = node_id
    for node in nodes.values():
        node["chain_ids"] = sorted(set(node["chain_ids"]))
    return {"root_id": "root", "nodes": sorted(nodes.values(), key=lambda row: (row["depth"], row["node_id"]))}


def build_replay_plan(
    session: Session,
    run_id: int,
    branch_key: str,
    max_chains: int = MAX_REPLAY_CHAINS,
    *,
    include_all_candidates: bool = False,
) -> Dict[str, Any]:
    """Build a deterministic safe replay plan for one inspection branch."""

    if not 1 <= int(max_chains) <= MAX_REPLAY_CHAINS:
        raise ReplayPlanError(
            "INVALID_MAX_CHAINS",
            f"max_chains must be between 1 and {MAX_REPLAY_CHAINS}",
        )
    run = session.get(InspectionRun, int(run_id))
    if run is None:
        raise ReplayPlanError("RUN_NOT_FOUND", "inspection run was not found")
    if str(run.status or "").upper() not in _COMPLETED_RUN_STATUSES:
        raise ReplayPlanError(
            "RUN_NOT_COMPLETE",
            "inspection run must be completed before replay planning",
        )
    normalized_branch = str(branch_key or "").strip()
    branch = session.exec(
        select(InspectionBranchRun).where(
            InspectionBranchRun.run_id == int(run_id),
            InspectionBranchRun.branch_key == normalized_branch,
        )
    ).first()
    if branch is None:
        raise ReplayPlanError("BRANCH_NOT_FOUND", "inspection branch was not found")
    if str(branch.status or "").upper() not in _COMPLETED_BRANCH_STATUSES:
        raise ReplayPlanError(
            "BRANCH_NOT_COMPLETE",
            "inspection branch must be completed before replay planning",
        )

    states = list(
        session.exec(
            select(InspectionState)
            .where(
                InspectionState.run_id == int(run_id),
                InspectionState.branch_run_id == int(branch.id),
            )
            .order_by(InspectionState.id)
        ).all()
    )
    all_run_state_ids = [
        int(item)
        for item in session.exec(
            select(InspectionState.id)
            .where(InspectionState.run_id == int(run_id))
            .order_by(InspectionState.id)
        ).all()
        if item is not None
    ]
    display_index_by_state = {
        state_id: index
        for index, state_id in enumerate(all_run_state_ids, start=1)
    }
    display_width = max(3, len(str(max(1, len(all_run_state_ids)))))
    if not states or not any(int(item.identity_version or 1) >= 2 for item in states):
        raise ReplayPlanError(
            "IDENTITY_V2_REQUIRED",
            "installed replay requires an identity v2 inspection run",
        )
    transitions = list(
        session.exec(
            select(InspectionTransition)
            .where(InspectionTransition.branch_run_id == int(branch.id))
            .order_by(InspectionTransition.id)
        ).all()
    )
    transition_by_id = {
        int(item.id): item for item in transitions if item.id is not None
    }
    observations = list(
        session.exec(
            select(InspectionObservation)
            .where(InspectionObservation.branch_run_id == int(branch.id))
            .order_by(InspectionObservation.sequence, InspectionObservation.id)
        ).all()
    )
    observations_by_state: Dict[int, List[InspectionObservation]] = defaultdict(list)
    for observation in observations:
        observations_by_state[int(observation.state_id)].append(observation)
    faults = list(
        session.exec(
            select(InspectionFault)
            .where(InspectionFault.branch_run_id == int(branch.id))
            .order_by(InspectionFault.id)
        ).all()
    )
    faults_by_state: Dict[int, List[InspectionFault]] = defaultdict(list)
    for fault in faults:
        if fault.state_id is not None:
            faults_by_state[int(fault.state_id)].append(fault)
    template_ids = {
        int(item.template_id) for item in states if item.template_id is not None
    }
    family_ids = {
        int(item.exploration_family_id)
        for item in states
        if item.exploration_family_id is not None
    }
    templates = {
        int(item.id): item
        for item in session.exec(
            select(InspectionPageTemplate).where(
                InspectionPageTemplate.id.in_(template_ids)
            )
        ).all()
        if item.id is not None
    } if template_ids else {}
    families = {
        int(item.id): item
        for item in session.exec(
            select(InspectionExplorationFamily).where(
                InspectionExplorationFamily.id.in_(family_ids)
            )
        ).all()
        if item.id is not None
    } if family_ids else {}
    states_by_semantic: Dict[str, List[InspectionState]] = defaultdict(list)
    for state in states:
        if state.semantic_key:
            states_by_semantic[str(state.semantic_key)].append(state)
    root_state = next(
        (item for item in states if item.id == branch.root_state_id),
        next((item for item in states if not item.first_path), None),
    )

    excluded_by_reason: Counter[str] = Counter()
    excluded_samples: Dict[str, List[int]] = defaultdict(list)

    def exclude(state: InspectionState, reason: str) -> None:
        excluded_by_reason[reason] += 1
        samples = excluded_samples[reason]
        if state.id is not None and len(samples) < 10:
            samples.append(int(state.id))

    candidates_by_path: Dict[str, Dict[str, Any]] = {}
    for state in states:
        if int(state.identity_version or 1) < 2:
            exclude(state, "IDENTITY_V1_STATE")
            continue
        if bool(state.is_opaque) or str(state.page_subtype or "").upper() == "OPAQUE":
            exclude(state, "OPAQUE_STATE")
            continue
        path = list(state.first_path or [])
        full_checkpoints = _build_checkpoints(
            endpoint=state,
            path=path,
            root_state=root_state,
            states_by_semantic=states_by_semantic,
            templates=templates,
            families=families,
            observations_by_state=observations_by_state,
        )
        if len(full_checkpoints) != len(path) + 1:
            exclude(state, "INCOMPLETE_CHECKPOINTS")
            continue

        # A risk, locator failure, or fault is a terminal boundary, not a
        # reason to discard the successful prefix.  Find the earliest fault
        # action that belongs to this path before splitting it.
        prefix_path, path_boundary = _safe_path_prefix(path)
        boundary_index: Optional[int] = (
            int(path_boundary.get("step_index"))
            if path_boundary is not None and path_boundary.get("step_index") is not None
            else None
        )
        boundary_from_fault: Optional[Dict[str, Any]] = None
        fault_boundary_index: Optional[int] = None
        fault_boundary_selected = False
        for step_index, step in enumerate(path):
            source_checkpoint = full_checkpoints[step_index]
            target_checkpoint = full_checkpoints[step_index + 1]
            source_id = source_checkpoint.get("state_id")
            target_id = target_checkpoint.get("state_id")
            for fault in faults:
                fault_action = _action_snapshot_from_fault(fault)
                action_key = str(fault_action.get("action_key") or "")
                if fault.transition_id is not None:
                    target_state = next(
                        (
                            item for item in states
                            if item.id is not None
                            and int(item.id) == int(target_id or 0)
                        ),
                        None,
                    )
                    transition_match = bool(
                        target_state is not None
                        and target_state.incoming_transition_id is not None
                        and int(fault.transition_id)
                        == int(target_state.incoming_transition_id)
                    )
                else:
                    transition_match = False
                current_action_match = bool(
                    fault.state_id is not None
                    and int(fault.state_id) == int(source_id or 0)
                    and (
                        not action_key
                        or action_key == str(step.get("action_key") or "")
                    )
                )
                target_fault_match = bool(
                    fault.state_id is not None
                    and int(fault.state_id) == int(target_id or 0)
                    and not action_key
                )
                if transition_match or current_action_match or target_fault_match:
                    if fault_boundary_index is None or step_index < fault_boundary_index:
                        fault_boundary_index = step_index
                        boundary_from_fault = terminal_boundaries_for_state(
                            target_id if target_fault_match else source_id,
                            faults=[fault],
                        )[0]
                    break
            if fault_boundary_index == step_index:
                break
        if fault_boundary_index is not None and (
            boundary_index is None or fault_boundary_index <= boundary_index
        ):
            boundary_index = fault_boundary_index
            path_boundary = boundary_from_fault
            fault_boundary_selected = True
        if boundary_index is not None:
            prefix_path = prefix_path[:boundary_index]
            if fault_boundary_selected and boundary_from_fault is not None:
                path_boundary = boundary_from_fault
            if path_boundary is not None:
                path_boundary = dict(path_boundary)
                path_boundary.setdefault("step_index", boundary_index)
                exclude(
                    state,
                    str(
                        path_boundary.get("safety_failure")
                        or path_boundary.get("terminal_outcome")
                        or "TERMINAL_BOUNDARY"
                    ),
                )

        # Resolve the state at the safe prefix endpoint.  This is important
        # for A -> B -> [PAYMENT BLOCKED]: the chain endpoint is B, never the
        # synthetic/unsafe target after the blocked action.
        safe_endpoint: Optional[InspectionState] = state
        if len(prefix_path) != len(path):
            endpoint_checkpoint = full_checkpoints[len(prefix_path)]
            safe_endpoint = next(
                (item for item in states if item.id == endpoint_checkpoint.get("state_id")),
                None,
            )
            if safe_endpoint is None:
                safe_endpoint = _best_state_for_checkpoint(
                    semantic_key=str(endpoint_checkpoint.get("semantic_key") or ""),
                    expected_path_length=len(prefix_path),
                    endpoint=state,
                    states_by_semantic=states_by_semantic,
                )
            if safe_endpoint is None:
                exclude(state, "SAFE_PREFIX_STATE_MISSING")
                continue
        if prefix_path:
            incoming = transition_by_id.get(int(safe_endpoint.incoming_transition_id or 0))
            if incoming is None or str(incoming.status or "").upper() != "PASS":
                exclude(state, "INCOMING_TRANSITION_NOT_PASS")
                continue
        observation_id = _state_observation_id(safe_endpoint, observations_by_state)
        if observation_id is None:
            exclude(state, "MISSING_OBSERVATION")
            continue
        checkpoints = _build_checkpoints(
            endpoint=safe_endpoint,
            path=prefix_path,
            root_state=root_state,
            states_by_semantic=states_by_semantic,
            templates=templates,
            families=families,
            observations_by_state=observations_by_state,
        )
        if len(checkpoints) != len(prefix_path) + 1:
            exclude(state, "INCOMPLETE_SAFE_PREFIX_CHECKPOINTS")
            continue
        for checkpoint in checkpoints:
            checkpoint_state_id = int(checkpoint.get("state_id") or 0)
            checkpoint_index = display_index_by_state.get(checkpoint_state_id)
            if checkpoint_index is not None:
                checkpoint["display_index"] = checkpoint_index
                checkpoint["display_label"] = (
                    f"P{checkpoint_index:0{display_width}d}"
                )
            checkpoint["page_name"] = str(
                checkpoint.get("page_subtype")
                or checkpoint.get("role")
                or "UNKNOWN"
            )
        endpoint_boundaries = terminal_boundaries_for_state(
            safe_endpoint.id,
            transitions=transitions,
            faults=faults_by_state.get(int(safe_endpoint.id or 0), []),
        )
        if path_boundary is not None:
            endpoint_boundaries.insert(0, dict(path_boundary))
        # Stable, deterministic boundary deduplication makes multiple blocked
        # actions on one page visible without creating duplicate chains.
        unique_boundaries: List[Dict[str, Any]] = []
        seen_boundary_ids: set[str] = set()
        for boundary in endpoint_boundaries:
            boundary_id = str(boundary.get("boundary_id") or _sha256_json(boundary)[:20])
            if boundary_id in seen_boundary_ids:
                continue
            seen_boundary_ids.add(boundary_id)
            boundary["boundary_id"] = boundary_id
            unique_boundaries.append(boundary)
        endpoint_boundaries = unique_boundaries
        key = _path_key(prefix_path)
        covered_roles = sorted(
            {
                str(item["role"])
                for item in checkpoints
                if str(item.get("role") or "UNKNOWN") != "UNKNOWN"
            }
        )
        covered_subtypes = sorted(
            {
                str(item["page_subtype"])
                for item in checkpoints
                if str(item.get("page_subtype") or "UNKNOWN") != "UNKNOWN"
            }
        )
        covered_family_ids = sorted(
            {int(item["family_id"]) for item in checkpoints if item.get("family_id") is not None}
        )
        covered_family_keys = sorted(
            {str(item["family_key"]) for item in checkpoints if item.get("family_key")}
        )
        endpoint_label = str(safe_endpoint.page_subtype or "UNKNOWN")
        if endpoint_label == "UNKNOWN" and checkpoints:
            endpoint_label = str(checkpoints[-1].get("role") or "UNKNOWN")
        evidence_level = state_reachability_evidence(
            safe_endpoint,
            has_observation=observation_id is not None,
        )
        display_index = display_index_by_state.get(int(safe_endpoint.id or 0))
        display_label = (
            f"P{display_index:0{display_width}d}"
            if display_index is not None
            else ""
        )
        endpoint_observations = observations_by_state.get(
            int(safe_endpoint.id or 0),
            [],
        )
        observation_index = next(
            (
                index
                for index, item in enumerate(endpoint_observations, start=1)
                if int(item.id or 0) == int(observation_id or 0)
            ),
            None,
        )
        replay_scope, _ = derive_replay_eligibility(
            safe_endpoint,
            endpoint_boundaries,
        )
        if path_boundary is not None and str(
            path_boundary.get("terminal_outcome") or ""
        ).upper() != "SAFETY_BLOCKED":
            replay_scope = REPLAY_SCOPE_DIAGNOSTIC_ONLY
        replay_eligibility = legacy_replay_eligibility(replay_scope)
        candidate: Dict[str, Any] = {
            "chain_id": f"chain-{key[:20]}",
            "path_key": key,
            "prefix_path_key": key,
            "name": endpoint_label,
            "page_name": endpoint_label,
            "display_index": display_index,
            "display_label": display_label,
            "endpoint_state_id": int(safe_endpoint.id or 0),
            "source_observation_id": _state_observation_id(safe_endpoint, observations_by_state),
            "source_observation_index": observation_index,
            "evidence_level": evidence_level,
            "reachability_evidence": evidence_level,
            "replay_scope": replay_scope,
            "replay_eligibility": replay_eligibility,
            "boundary_evidence": aggregate_boundary_evidence(endpoint_boundaries),
            "terminal_boundaries": _json_copy(endpoint_boundaries),
            "first_path": _json_copy(prefix_path),
            "checkpoints": checkpoints,
            "covered_roles": covered_roles,
            "covered_subtypes": covered_subtypes,
            "covered_family_ids": covered_family_ids,
            "covered_family_keys": covered_family_keys,
            "depth": len(prefix_path),
            "source_path_keys": [_path_key(path)],
            "_coverage_tokens": (
                _coverage_tokens(checkpoints)
                | _boundary_coverage_tokens(endpoint_boundaries)
            ),
        }
        previous = candidates_by_path.get(key)
        if previous is None:
            candidates_by_path[key] = candidate
            continue
        # Merge observations and terminal boundaries for the same safe prefix.
        previous["terminal_boundaries"] = list(previous.get("terminal_boundaries") or [])
        boundary_ids = {
            str(item.get("boundary_id")) for item in previous["terminal_boundaries"]
        }
        for boundary in candidate.get("terminal_boundaries") or []:
            if str(boundary.get("boundary_id")) not in boundary_ids:
                previous["terminal_boundaries"].append(boundary)
                boundary_ids.add(str(boundary.get("boundary_id")))
        previous["source_path_keys"] = sorted(
            set(previous.get("source_path_keys") or [])
            | set(candidate.get("source_path_keys") or [])
        )
        if (
            evidence_level == "VERIFIED_TWICE"
            and previous.get("evidence_level") != "VERIFIED_TWICE"
        ):
            for field in (
                "evidence_level",
                "reachability_evidence",
                "endpoint_state_id",
                "source_observation_id",
                "source_observation_index",
                "display_index",
                "display_label",
                "page_name",
                "checkpoints",
            ):
                previous[field] = candidate[field]
        merged_scope, _ = derive_replay_eligibility(
            safe_endpoint,
            previous["terminal_boundaries"],
        )
        if previous.get("replay_scope") == REPLAY_SCOPE_DIAGNOSTIC_ONLY:
            merged_scope = REPLAY_SCOPE_DIAGNOSTIC_ONLY
        previous["replay_scope"] = merged_scope
        previous["replay_eligibility"] = legacy_replay_eligibility(merged_scope)
        previous["boundary_evidence"] = aggregate_boundary_evidence(
            previous["terminal_boundaries"]
        )
        excluded_by_reason["DUPLICATE_PATH"] += 1
        if state.id is not None and len(excluded_samples["DUPLICATE_PATH"]) < 10:
            excluded_samples["DUPLICATE_PATH"].append(int(state.id))

    candidates = sorted(candidates_by_path.values(), key=lambda item: item["path_key"])
    replay_candidates = [
        item
        for item in candidates
        if item.get("replay_scope")
        in {REPLAY_SCOPE_FULL_PATH, REPLAY_SCOPE_SAFETY_PREFIX}
    ]
    diagnostic_candidates = [
        item for item in candidates if item not in replay_candidates
    ]
    for candidate in diagnostic_candidates:
        state_id = int(candidate.get("endpoint_state_id") or 0)
        reason = (
            "UNSTABLE_OR_DIVERGED"
            if candidate.get("reachability_evidence") == REACHABILITY_UNSTABLE
            else "DIAGNOSTIC_ONLY"
        )
        excluded_by_reason[reason] += 1
        if state_id and len(excluded_samples[reason]) < 10:
            excluded_samples[reason].append(state_id)
    selected = (
        list(replay_candidates)
        if include_all_candidates
        else _select_chains(replay_candidates, int(max_chains))
    )
    selected_keys = {item["path_key"] for item in selected}
    not_selected = [
        item
        for item in replay_candidates
        if item["path_key"] not in selected_keys
    ]
    if not_selected:
        excluded_by_reason["NOT_SELECTED_BY_SET_COVER"] += len(not_selected)
        excluded_samples["NOT_SELECTED_BY_SET_COVER"] = [
            int(item["endpoint_state_id"]) for item in not_selected[:10]
        ]
    public_chains: List[Dict[str, Any]] = []
    for candidate in selected:
        public_chains.append(
            {key: value for key, value in candidate.items() if not key.startswith("_")}
        )
    public_chains.sort(
        key=lambda item: (
            0 if item["evidence_level"] == "VERIFIED_TWICE" else 1,
            item["depth"],
            item["path_key"],
        )
    )
    evidence_counts = Counter(item["evidence_level"] for item in public_chains)
    eligibility_counts = Counter(item.get("replay_eligibility", "FULL") for item in public_chains)
    scope_counts = Counter(item.get("replay_scope", REPLAY_SCOPE_FULL_PATH) for item in public_chains)
    boundary_count = sum(len(item.get("terminal_boundaries") or []) for item in public_chains)
    covered_roles = sorted(
        {role for item in public_chains for role in item["covered_roles"]}
    )
    covered_subtypes = sorted(
        {role for item in public_chains for role in item["covered_subtypes"]}
    )
    covered_family_ids = sorted(
        {
            family_id
            for item in public_chains
            for family_id in item["covered_family_ids"]
        }
    )
    excluded = {
        "total": int(sum(excluded_by_reason.values())),
        "by_reason": dict(sorted(excluded_by_reason.items())),
        "samples": {
            key: list(value) for key, value in sorted(excluded_samples.items())
        },
    }
    plan: Dict[str, Any] = {
        "plan_version": REPLAY_PLAN_VERSION,
        "inspection_run_id": int(run.id or run_id),
        "branch_key": normalized_branch,
        "branch_name": str(branch.branch_name or normalized_branch),
        "package_name": str(run.package_name or ""),
        "summary": {
            "state_count": len(states),
            "candidate_chain_count": len(candidates),
            "selected_chain_count": len(public_chains),
            "max_chains": int(max_chains),
            "evidence_counts": dict(sorted(evidence_counts.items())),
            "replay_eligibility_counts": dict(sorted(eligibility_counts.items())),
            "replay_scope_counts": dict(sorted(scope_counts.items())),
            "safe_prefix_count": int(eligibility_counts.get("SAFE_PREFIX", 0)),
            "diagnostic_only_count": len(diagnostic_candidates),
            "full_path_count": int(eligibility_counts.get("FULL", 0)),
            "terminal_boundary_count": int(boundary_count),
            "covered_roles": covered_roles,
            "covered_subtypes": covered_subtypes,
            "covered_family_ids": covered_family_ids,
            "covered_family_count": len(covered_family_ids),
            "excluded_state_count": excluded["total"],
        },
        "chains": public_chains,
        "prefix_tree": _prefix_tree(public_chains),
        "excluded": excluded,
    }
    plan["digest"] = _sha256_json(plan)
    # Keep the API-facing spelling available while retaining ``digest`` as
    # the canonical field used by older callers.
    plan["plan_digest"] = plan["digest"]
    return plan


def _multiset_similarity(left: Iterable[Any], right: Iterable[Any]) -> Optional[float]:
    left_values = [str(item) for item in left]
    right_values = [str(item) for item in right]
    if not left_values and not right_values:
        return None
    left_counts = Counter(left_values)
    right_counts = Counter(right_values)
    union = sum((left_counts | right_counts).values())
    return sum((left_counts & right_counts).values()) / union if union else None


def _action_from_step(step: Mapping[str, Any]) -> InspectionAction:
    return InspectionAction(
        action_type=str(step.get("action_type") or "click"),
        action_key=str(step.get("action_key") or ""),
        locator_candidates=[
            dict(item)
            for item in step.get("locator_candidates") or []
            if isinstance(item, Mapping)
        ],
        target_meta=dict(step.get("target_meta") or {}),
        coordinate_only=bool(step.get("coordinate_only")),
        replayable=bool(step.get("replayable", True)),
        risk_type=str(step.get("risk_type")) if step.get("risk_type") else None,
        blocked_reason=(
            str(step.get("blocked_reason")) if step.get("blocked_reason") else None
        ),
        input_rule_id=(
            str(step.get("input_rule_id")) if step.get("input_rule_id") else None
        ),
        input_variable_key=(
            str(step.get("input_variable_key"))
            if step.get("input_variable_key")
            else None
        ),
        action_role=str(step.get("action_role")) if step.get("action_role") else None,
        action_role_key=(
            str(step.get("action_role_key")) if step.get("action_role_key") else None
        ),
        action_anchor_key=(
            str(step.get("action_anchor_key"))
            if step.get("action_anchor_key")
            else None
        ),
        action_group_key=(
            str(step.get("action_group_key"))
            if step.get("action_group_key")
            else None
        ),
        action_instance_key=(
            str(step.get("action_instance_key"))
            if step.get("action_instance_key")
            else None
        ),
        sample_policy=str(step.get("sample_policy") or "ALL"),
    )


def evaluate_reachability(
    checkpoint: Mapping[str, Any],
    actual: PageModel,
    *,
    expected_package: Optional[str] = None,
    incoming_step: Optional[Mapping[str, Any]] = None,
    source_instance_anchor: Optional[str] = None,
) -> Dict[str, Any]:
    """Evaluate cross-version reachability without requiring semantic-key equality."""

    expectation = checkpoint.get("expectation") or checkpoint.get("expected_signature")
    expected = dict(expectation) if isinstance(expectation, Mapping) else {}
    package = str(
        expected_package or expected.get("package") or actual.package_name or ""
    ).casefold()
    actual_package = str(actual.package_name or "").casefold()
    expected_role = str(
        checkpoint.get("role")
        or checkpoint.get("expected_role")
        or expected.get("role")
        or "UNKNOWN"
    )
    expected_subtype = str(
        checkpoint.get("page_subtype")
        or checkpoint.get("expected_page_subtype")
        or expected.get("page_subtype")
        or "UNKNOWN"
    )
    expected_anchor = str(
        checkpoint.get("instance_anchor")
        or checkpoint.get("expected_instance_anchor")
        or expected.get("instance_anchor")
        or expected.get("content_anchor")
        or ""
    )
    incoming_action = _action_from_step(incoming_step) if incoming_step else None
    actual_anchor = str(
        derive_instance_anchor(
            actual,
            incoming_action=incoming_action,
            source_instance_anchor=source_instance_anchor,
        )
        or ""
    )
    expected_semantic = str(
        checkpoint.get("semantic_key")
        or checkpoint.get("expected_semantic_key")
        or expected.get("semantic_key")
        or ""
    )
    exact_semantic = bool(
        expected_semantic and expected_semantic == str(actual.semantic_key or "")
    )
    package_match = bool(package and package == actual_package)
    role_match = bool(expected_role != "UNKNOWN" and expected_role == str(actual.role or ""))
    subtype_match: Optional[bool] = None
    if expected_subtype != "UNKNOWN":
        subtype_match = expected_subtype == str(actual.page_subtype or "UNKNOWN")
    anchor_match = bool(expected_anchor and expected_anchor == actual_anchor)
    activity_family_match: Optional[bool] = None
    expected_activity_family = str(expected.get("activity_family") or "")
    if expected_activity_family:
        activity_family_match = expected_activity_family == str(actual.activity_family or "")
    structure_similarity = _multiset_similarity(
        expected.get("structure_tokens") or [], actual.template_tokens
    )
    action_similarity = _multiset_similarity(
        expected.get("action_tokens") or [], actual.action_tokens
    )
    control_similarity = _multiset_similarity(
        expected.get("control_tokens") or [], actual.control_tokens
    )
    risk_similarity = _multiset_similarity(
        expected.get("risk_tokens") or [], actual.risk_tokens
    )
    mismatches: List[str] = []
    warnings: List[str] = []
    if not package_match:
        mismatches.append("PACKAGE_MISMATCH")
    if expected_role != "UNKNOWN" and not role_match:
        mismatches.append("ROLE_MISMATCH")
    if subtype_match is False:
        mismatches.append("SUBTYPE_MISMATCH")
    if expected_anchor and not anchor_match and not exact_semantic:
        mismatches.append("INSTANCE_ANCHOR_MISMATCH")
    if expected_role == "UNKNOWN":
        warnings.append("WEAK_ROLE_EXPECTATION")
    if not expected_anchor and not exact_semantic:
        warnings.append("WEAK_ANCHOR_EXPECTATION")
    if activity_family_match is False:
        warnings.append("ACTIVITY_FAMILY_CHANGED")
    if structure_similarity is not None and structure_similarity < 0.97:
        warnings.append("STRUCTURE_CHANGED")
    if action_similarity is not None and action_similarity < 1.0:
        warnings.append("ACTION_SET_CHANGED")
    if control_similarity is not None and control_similarity < 1.0:
        warnings.append("CONTROL_STATE_CHANGED")
    if risk_similarity is not None and risk_similarity < 1.0:
        warnings.append("RISK_SIGNATURE_CHANGED")
    if mismatches:
        status = "MISMATCH"
        confidence = 0.0
    elif exact_semantic:
        status = "MATCH"
        confidence = 1.0
    elif role_match and anchor_match:
        status = "MATCH"
        confidence = 0.98
    else:
        status = "WEAK_MATCH"
        confidence = 0.75
    return {
        "status": status,
        "matched": status != "MISMATCH",
        "confidence": confidence,
        "mismatches": mismatches,
        "warnings": warnings,
        "expected": {
            "package": package,
            "role": expected_role,
            "page_subtype": expected_subtype,
            "instance_anchor": expected_anchor,
            "semantic_key": expected_semantic,
            "activity_family": expected_activity_family,
        },
        "actual": {
            "package": actual_package,
            "role": str(actual.role or "UNKNOWN"),
            "page_subtype": str(actual.page_subtype or "UNKNOWN"),
            "instance_anchor": actual_anchor,
            "semantic_key": str(actual.semantic_key or ""),
            "activity_family": str(actual.activity_family or ""),
        },
        "diagnostics": {
            "exact_semantic_key": exact_semantic,
            "package_match": package_match,
            "role_match": role_match,
            "page_subtype_match": subtype_match,
            "instance_anchor_match": anchor_match,
            "activity_family_match": activity_family_match,
            "structure_similarity": structure_similarity,
            "action_similarity": action_similarity,
            "control_similarity": control_similarity,
            "risk_similarity": risk_similarity,
        },
    }


def _screen_size_from_page(page: PageModel) -> Tuple[int, int]:
    bounds = [node.bounds for node in page.nodes if node.bounds is not None]
    if not bounds:
        return 1080, 1920
    return max(item[2] for item in bounds), max(item[3] for item in bounds)


def _normalized_center(meta: Mapping[str, Any]) -> Optional[Tuple[float, float]]:
    bounds = meta.get("bounds")
    size = meta.get("screen_size")
    if not (
        isinstance(bounds, (list, tuple))
        and len(bounds) == 4
        and isinstance(size, (list, tuple))
        and len(size) == 2
    ):
        return None
    try:
        width, height = max(1.0, float(size[0])), max(1.0, float(size[1]))
        return (
            (float(bounds[0]) + float(bounds[2])) / 2.0 / width,
            (float(bounds[1]) + float(bounds[3])) / 2.0 / height,
        )
    except (TypeError, ValueError):
        return None


def _navigation_identity(meta: Mapping[str, Any]) -> Tuple[str, str]:
    navigation = meta.get("navigation")
    if not isinstance(navigation, Mapping):
        return "", ""
    member = navigation.get("member")
    member_key = str(navigation.get("member_key") or "")
    if not member_key and isinstance(member, Mapping):
        member_key = str(member.get("member_key") or "")
    return member_key, str(navigation.get("group_region") or "")


def _binding_score(
    historical: InspectionAction,
    candidate: InspectionAction,
) -> Optional[Tuple[int, Dict[str, Any]]]:
    if historical.action_type != candidate.action_type:
        return None
    if not historical.action_role or historical.action_role != candidate.action_role:
        return None
    historical_meta = historical.target_meta or {}
    candidate_meta = candidate.target_meta or {}
    for key in ("enabled", "checked", "selected"):
        if historical_meta.get(key) != candidate_meta.get(key):
            return None
    direct_historical = str(
        historical_meta.get("content_desc") or historical_meta.get("text") or ""
    ).strip().casefold()
    direct_candidate = str(
        candidate_meta.get("content_desc") or candidate_meta.get("text") or ""
    ).strip().casefold()
    historical_ancestor = str(historical_meta.get("ancestor_semantic") or "").strip().casefold()
    candidate_ancestor = str(candidate_meta.get("ancestor_semantic") or "").strip().casefold()
    historical_navigation, historical_region = _navigation_identity(historical_meta)
    candidate_navigation, candidate_region = _navigation_identity(candidate_meta)
    anchor_key_match = bool(
        historical.action_anchor_key
        and historical.action_anchor_key == candidate.action_anchor_key
    )
    ancestor_match = bool(
        historical_ancestor and historical_ancestor == candidate_ancestor
    )
    direct_match = bool(direct_historical and direct_historical == direct_candidate)
    navigation_match = bool(
        historical_navigation and historical_navigation == candidate_navigation
    )
    anchor_match = anchor_key_match or ancestor_match or direct_match or navigation_match
    historical_bucket = str(historical_meta.get("relative_bucket") or "")
    candidate_bucket = str(candidate_meta.get("relative_bucket") or "")
    bucket_match = bool(
        historical_bucket and historical_bucket == candidate_bucket
    )
    navigation_region_match = bool(
        historical_region and historical_region == candidate_region
    )
    old_center = _normalized_center(historical_meta)
    new_center = _normalized_center(candidate_meta)
    center_distance: Optional[float] = None
    if old_center is not None and new_center is not None:
        center_distance = (
            (old_center[0] - new_center[0]) ** 2
            + (old_center[1] - new_center[1]) ** 2
        ) ** 0.5
    region_match = bool(
        bucket_match
        or navigation_region_match
        or (center_distance is not None and center_distance <= 0.18)
    )
    if not anchor_match or not region_match:
        return None
    role_key_match = bool(
        historical.action_role_key
        and historical.action_role_key == candidate.action_role_key
    )
    score = (
        8
        + (5 if anchor_key_match else 0)
        + (4 if ancestor_match else 0)
        + (4 if direct_match else 0)
        + (5 if navigation_match else 0)
        + (3 if bucket_match else 0)
        + (3 if navigation_region_match else 0)
        + (2 if role_key_match else 0)
        + (
            max(0, int(round((0.18 - center_distance) / 0.18 * 2)))
            if center_distance is not None
            else 0
        )
    )
    return score, {
        "anchor_key_match": anchor_key_match,
        "ancestor_match": ancestor_match,
        "direct_semantic_match": direct_match,
        "navigation_member_match": navigation_match,
        "relative_bucket_match": bucket_match,
        "navigation_region_match": navigation_region_match,
        "normalized_center_distance": (
            round(center_distance, 4) if center_distance is not None else None
        ),
        "action_role_key_match": role_key_match,
    }


def rebind_replay_action(
    step: Mapping[str, Any],
    page: PageModel,
    *,
    screen_size: Optional[Tuple[int, int]] = None,
    safety_rules: Sequence[Dict[str, Any]] = (),
    input_rules: Sequence[Dict[str, Any]] = (),
    validate_historical_risk: bool = False,
    probe_only: bool = False,
) -> ReplayActionBinding:
    """Rebind one historical action against freshly classified current XML."""

    historical = _action_from_step(step)
    if (
        (historical.risk_type or historical.blocked_reason)
        and not validate_historical_risk
        and not probe_only
    ):
        return ReplayActionBinding(
            status="BLOCKED",
            failure_type="BLOCKED",
            reason="historical action is classified as risky",
            risk_type=historical.risk_type or "HISTORICAL_RISK",
        )
    if (historical.coordinate_only or not historical.replayable) and not probe_only:
        return ReplayActionBinding(
            status="BLOCKED",
            failure_type="COORDINATE_UNSAFE",
            reason="coordinate-only or non-replayable action",
            risk_type="COORDINATE_UNSAFE",
        )
    if historical.action_type == "back":
        return ReplayActionBinding(
            status="BOUND",
            action=historical,
            candidate_count=1,
            evidence={"system_back": True},
        )
    resolved_size = screen_size or _screen_size_from_page(page)
    current_actions = enumerate_actions(
        page,
        screen_size=resolved_size,
        safety_rules=safety_rules,
        input_rules=input_rules,
        max_scrolls_per_direction=3,
        coverage_scheduler_v2=True,
    )
    ranked: List[Tuple[int, InspectionAction, Dict[str, Any]]] = []
    for candidate in current_actions:
        scored = _binding_score(historical, candidate)
        if scored is not None:
            ranked.append((scored[0], candidate, scored[1]))
    if not ranked:
        return ReplayActionBinding(
            status="NOT_FOUND",
            failure_type="LOCATOR_NOT_FOUND",
            reason="no current control matched action role, stable anchor, and region",
            candidate_count=0,
            evidence={"action_role": historical.action_role},
        )
    ranked.sort(key=lambda item: item[0], reverse=True)
    best_score, best, best_evidence = ranked[0]
    tied = [item for item in ranked if item[0] == best_score]
    if len(tied) != 1:
        return ReplayActionBinding(
            status="AMBIGUOUS",
            failure_type="LOCATOR_AMBIGUOUS",
            reason="multiple current controls have equal semantic binding evidence",
            candidate_count=len(tied),
            evidence={"action_role": historical.action_role, "score": best_score},
        )
    if probe_only:
        expected_risk = str(historical.risk_type or "").strip().upper()
        current_risk = str(best.risk_type or "").strip().upper()
        evidence = {
            **best_evidence,
            "score": best_score,
            "probe_only": True,
            "expected_risk": expected_risk,
            "current_risk": current_risk,
        }
        if expected_risk and current_risk == expected_risk:
            return ReplayActionBinding(
                status="PROBE_MATCH",
                failure_type=None,
                reason="safety boundary matched without invoking the control",
                risk_type=best.risk_type,
                candidate_count=len(ranked),
                evidence=evidence,
            )
        return ReplayActionBinding(
            status="CHANGED",
            failure_type="SAFETY_BOUNDARY_CHANGED",
            reason="current control no longer has the expected safety classification",
            risk_type=best.risk_type,
            candidate_count=len(ranked),
            evidence=evidence,
        )
    if best.risk_type:
        return ReplayActionBinding(
            status="BLOCKED",
            failure_type="BLOCKED",
            reason=best.blocked_reason or "current action is classified as risky",
            risk_type=best.risk_type,
            candidate_count=len(ranked),
            evidence={**best_evidence, "score": best_score},
        )
    if best.coordinate_only or not best.replayable:
        return ReplayActionBinding(
            status="BLOCKED",
            failure_type="COORDINATE_UNSAFE",
            reason="current action has no safe semantic locator",
            risk_type="COORDINATE_UNSAFE",
            candidate_count=len(ranked),
            evidence={**best_evidence, "score": best_score},
        )
    rebound = replace(
        best,
        action_key=historical.action_key,
        action_role=historical.action_role,
        action_role_key=historical.action_role_key,
        action_anchor_key=historical.action_anchor_key,
        action_group_key=historical.action_group_key,
        action_instance_key=historical.action_instance_key,
        sample_policy=historical.sample_policy,
        input_rule_id=historical.input_rule_id,
        input_variable_key=historical.input_variable_key,
    )
    return ReplayActionBinding(
        status="BOUND",
        action=rebound,
        candidate_count=len(ranked),
        evidence={**best_evidence, "score": best_score},
    )


def probe_replay_boundary(
    step: Mapping[str, Any],
    page: PageModel,
    *,
    screen_size: Optional[Tuple[int, int]] = None,
    safety_rules: Sequence[Dict[str, Any]] = (),
    input_rules: Sequence[Dict[str, Any]] = (),
) -> ReplayActionBinding:
    """Match a historical safety control without ever returning an executable action."""
    return rebind_replay_action(
        step,
        page,
        screen_size=screen_size,
        safety_rules=safety_rules,
        input_rules=input_rules,
        validate_historical_risk=True,
        probe_only=True,
    )


def build_replay_trace_step(
    *,
    step_index: int,
    step: Mapping[str, Any],
    status: str,
    duration_ms: float,
    source_evidence: Optional[Mapping[str, Any]] = None,
    target_evidence: Optional[Mapping[str, Any]] = None,
    binding: Optional[ReplayActionBinding] = None,
    failure_type: Optional[str] = None,
    reason: Optional[str] = None,
    boundary_evidence: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a browser-safe trace row without selectors or input values."""

    def compact_reachability(value: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
        if value is None:
            return None
        return {
            "status": value.get("status"),
            "confidence": value.get("confidence"),
            "mismatches": list(value.get("mismatches") or []),
            "warnings": list(value.get("warnings") or []),
            "expected": dict(value.get("expected") or {}),
            "actual": dict(value.get("actual") or {}),
            "diagnostics": dict(value.get("diagnostics") or {}),
        }

    return {
        "step_index": int(step_index),
        "action_type": str(step.get("action_type") or ""),
        "action_role": str(step.get("action_role") or ""),
        "action_role_key": str(step.get("action_role_key") or ""),
        "status": status,
        "failure_type": failure_type,
        "boundary_evidence": boundary_evidence,
        "reason": reason,
        "duration_ms": round(max(0.0, float(duration_ms)), 2),
        "source": compact_reachability(source_evidence),
        "target": compact_reachability(target_evidence),
        "binding": binding.to_dict() if binding else None,
    }


def _record_reachability_warnings(
    warning_codes: set[str],
    evidence: Mapping[str, Any],
) -> None:
    """Keep only outcome-affecting reachability warnings in the run result.

    Structure/action/control/activity changes remain visible in the trace
    diagnostics, but a compatible page should not fail merely because its
    implementation was refactored.
    """
    if str(evidence.get("status") or "") == "WEAK_MATCH":
        warning_codes.add("WEAK_MATCH")
    for warning in evidence.get("warnings") or ():
        value = str(warning)
        if value == "WEAK_MATCH" or value.startswith("RISK"):
            warning_codes.add(value)


def execute_replay_chain(
    device: Any,
    chain: Mapping[str, Any],
    *,
    package_name: str,
    abort_event: threading.Event,
    initial_capture: Optional[CapturedPage] = None,
    dynamic_patterns: Sequence[str] = (),
    safety_rules: Sequence[Dict[str, Any]] = (),
    input_rules: Sequence[Dict[str, Any]] = (),
    input_resolver: Optional[Callable[[Mapping[str, Any]], Optional[str]]] = None,
    stable_wait_seconds: float = 5.0,
    before_device_action: Optional[Callable[[str], None]] = None,
    stage_callback: Optional[Callable[[int, str], None]] = None,
) -> ReplayExecutionResult:
    """Replay one frozen chain after the caller has established its root page."""

    path = [dict(item) for item in chain.get("first_path") or []]
    checkpoints = [dict(item) for item in chain.get("checkpoints") or []]
    if len(checkpoints) != len(path) + 1:
        return ReplayExecutionResult(
            status="FAIL",
            reason="replay chain checkpoint count does not match its path",
            failure_type="INVALID_REPLAY_PLAN",
            trace=[],
            completed_checkpoints=0,
        )
    safety_failure = _path_safety_failure(path)
    if safety_failure:
        return ReplayExecutionResult(
            status="WARNING" if safety_failure in {"HISTORICAL_RISK", "COORDINATE_ONLY"} else "FAIL",
            reason="replay chain contains an unsafe or invalid action",
            failure_type=(
                "BLOCKED"
                if safety_failure == "HISTORICAL_RISK"
                else "COORDINATE_UNSAFE"
                if safety_failure == "COORDINATE_ONLY"
                else "INVALID_REPLAY_PLAN"
            ),
            trace=[],
            completed_checkpoints=0,
        )

    trace: List[Dict[str, Any]] = []
    warning_codes: set[str] = set()
    verified_boundaries: List[Dict[str, Any]] = []
    capture = initial_capture
    active_step_index: Optional[int] = None
    active_step: Optional[Mapping[str, Any]] = None
    active_source_evidence: Optional[Mapping[str, Any]] = None
    active_binding: Optional[ReplayActionBinding] = None
    active_started: Optional[float] = None

    def take_capture() -> CapturedPage:
        if abort_event.is_set():
            raise InspectionAborted("replay cancelled")
        return wait_for_stable_page(
            device,
            expected_package=package_name,
            abort_event=abort_event,
            max_wait_seconds=max(0.0, float(stable_wait_seconds)),
            dynamic_patterns=dynamic_patterns,
        )

    def confirm(
        checkpoint: Mapping[str, Any],
        current: CapturedPage,
        *,
        incoming_step: Optional[Mapping[str, Any]] = None,
        source_anchor: Optional[str] = None,
    ) -> Tuple[CapturedPage, Dict[str, Any]]:
        evidence = evaluate_reachability(
            checkpoint,
            current.model,
            expected_package=package_name,
            incoming_step=incoming_step,
            source_instance_anchor=source_anchor,
        )
        if evidence["status"] != "MISMATCH":
            return current, evidence
        second = take_capture()
        second_evidence = evaluate_reachability(
            checkpoint,
            second.model,
            expected_package=package_name,
            incoming_step=incoming_step,
            source_instance_anchor=source_anchor,
        )
        return second, second_evidence

    def verify_terminal_boundaries(current: Optional[CapturedPage]) -> None:
        """Verify safety controls in-place; never invoke a terminal action."""
        if current is None:
            return
        boundaries = [
            item for item in (chain.get("terminal_boundaries") or [])
            if isinstance(item, Mapping)
            and str(item.get("terminal_outcome") or "").upper() == "SAFETY_BLOCKED"
        ]
        for offset, boundary in enumerate(boundaries):
            step = dict(boundary.get("action") or {})
            step.setdefault("action_key", boundary.get("action_key") or "")
            step.setdefault("action_role", boundary.get("action_role") or "")
            step.setdefault("risk_type", boundary.get("risk_type"))
            step.setdefault("blocked_reason", boundary.get("reason"))
            binding = probe_replay_boundary(
                step,
                current.model,
                safety_rules=safety_rules,
                input_rules=input_rules,
            )
            if binding.status == "PROBE_MATCH":
                evidence = BOUNDARY_EVIDENCE_VERIFIED
                trace_status = "BOUNDARY_VERIFIED"
                failure_type = None
                reason = "安全拦截控件仍存在，未执行"
            elif binding.status == "CHANGED":
                evidence = BOUNDARY_EVIDENCE_CHANGED
                trace_status = "BOUNDARY_CHANGED"
                failure_type = "SAFETY_BOUNDARY_CHANGED"
                reason = "安全拦截控件的风险分类已变化"
            else:
                evidence = BOUNDARY_EVIDENCE_NOT_VERIFIABLE
                trace_status = "BOUNDARY_NOT_VERIFIABLE"
                failure_type = "SAFETY_BOUNDARY_NOT_VERIFIABLE"
                reason = "安全拦截控件无法唯一确认，未执行"
            trace.append(
                build_replay_trace_step(
                    step_index=len(path) + offset,
                    step=step,
                    status=trace_status,
                    duration_ms=0.0,
                    binding=binding,
                    failure_type=failure_type,
                    reason=reason,
                    boundary_evidence=evidence,
                )
            )
            verified_boundaries.append(
                {
                    "terminal_outcome": "SAFETY_BLOCKED",
                    "boundary_evidence": evidence,
                }
            )
            if evidence != BOUNDARY_EVIDENCE_VERIFIED:
                warning_codes.add(str(failure_type))

    try:
        capture = capture or take_capture()
        capture, root_evidence = confirm(checkpoints[0], capture)
        if root_evidence["status"] == "MISMATCH":
            return ReplayExecutionResult(
                status="FAIL",
                reason="business root page is not preserved",
                failure_type="STATE_NOT_PRESERVED",
                trace=[],
                completed_checkpoints=0,
                last_capture=capture,
            )
        _record_reachability_warnings(warning_codes, root_evidence)
        if not path:
            verify_terminal_boundaries(capture)
            return ReplayExecutionResult(
                status="WARNING" if warning_codes else "PASS",
                reason="weak or changed compatibility evidence" if warning_codes else None,
                failure_type=None,
                trace=trace,
                completed_checkpoints=1,
                boundary_evidence=aggregate_boundary_evidence(
                    verified_boundaries
                ),
                warning_codes=sorted(warning_codes),
                last_capture=capture,
            )

        for index, step in enumerate(path):
            started = time.monotonic()
            active_step_index = index
            active_step = step
            active_source_evidence = None
            active_binding = None
            active_started = started
            if abort_event.is_set():
                raise InspectionAborted("replay cancelled")
            if stage_callback:
                stage_callback(index, str(step.get("action_role") or ""))
            source_checkpoint = checkpoints[index]
            target_checkpoint = checkpoints[index + 1]
            previous_step = path[index - 1] if index > 0 else None
            previous_source_anchor = (
                str(checkpoints[index - 1].get("instance_anchor") or "")
                if index > 0
                else None
            )
            capture, source_evidence = confirm(
                source_checkpoint,
                capture,
                incoming_step=previous_step,
                source_anchor=previous_source_anchor,
            )
            if source_evidence["status"] == "MISMATCH":
                trace.append(
                    build_replay_trace_step(
                        step_index=index,
                        step=step,
                        status="FAIL",
                        duration_ms=(time.monotonic() - started) * 1000,
                        source_evidence=source_evidence,
                        failure_type="PATH_DIVERGED",
                        reason="source checkpoint diverged twice",
                    )
                )
                return ReplayExecutionResult(
                    status="FAIL",
                    reason="source checkpoint diverged twice",
                    failure_type="PATH_DIVERGED",
                    trace=trace,
                    completed_checkpoints=index + 1,
                    warning_codes=sorted(warning_codes),
                    failed_step_index=index,
                    last_capture=capture,
                )
            active_source_evidence = source_evidence
            _record_reachability_warnings(warning_codes, source_evidence)
            binding = rebind_replay_action(
                step,
                capture.model,
                safety_rules=safety_rules,
                input_rules=input_rules,
            )
            active_binding = binding
            if binding.status != "BOUND" or binding.action is None:
                blocked = binding.status == "BLOCKED"
                trace.append(
                    build_replay_trace_step(
                        step_index=index,
                        step=step,
                        status="BLOCKED" if blocked else "FAIL",
                        duration_ms=(time.monotonic() - started) * 1000,
                        source_evidence=source_evidence,
                        binding=binding,
                        failure_type=binding.failure_type,
                        reason=binding.reason,
                    )
                )
                return ReplayExecutionResult(
                    status="WARNING" if blocked else "FAIL",
                    reason=binding.reason,
                    failure_type=binding.failure_type,
                    trace=trace,
                    completed_checkpoints=index + 1,
                    warning_codes=sorted(
                        warning_codes | ({"BLOCKED"} if blocked else set())
                    ),
                    failed_step_index=index,
                    last_capture=capture,
                )
            input_value: Optional[str] = None
            if binding.action.action_type == "input":
                input_value = input_resolver(step) if input_resolver else None
                if input_value is None:
                    trace.append(
                        build_replay_trace_step(
                            step_index=index,
                            step=step,
                            status="FAIL",
                            duration_ms=(time.monotonic() - started) * 1000,
                            source_evidence=source_evidence,
                            binding=binding,
                            failure_type="INPUT_VALUE_MISSING",
                            reason="input rule value is unavailable",
                        )
                    )
                    return ReplayExecutionResult(
                        status="FAIL",
                        reason="input rule value is unavailable",
                        failure_type="INPUT_VALUE_MISSING",
                        trace=trace,
                        completed_checkpoints=index + 1,
                        warning_codes=sorted(warning_codes),
                        failed_step_index=index,
                        last_capture=capture,
                    )
            if before_device_action:
                before_device_action(str(binding.action.action_role or "replay_action"))
            perform_action(
                device,
                binding.action,
                current_xml=capture.xml,
                input_value=input_value,
            )
            target_capture = take_capture()
            capture = target_capture
            source_anchor = str(
                source_checkpoint.get("instance_anchor")
                or (source_evidence.get("actual") or {}).get("instance_anchor")
                or ""
            )
            target_capture, target_evidence = confirm(
                target_checkpoint,
                target_capture,
                incoming_step=step,
                source_anchor=source_anchor,
            )
            if target_evidence["status"] == "MISMATCH":
                trace.append(
                    build_replay_trace_step(
                        step_index=index,
                        step=step,
                        status="FAIL",
                        duration_ms=(time.monotonic() - started) * 1000,
                        source_evidence=source_evidence,
                        target_evidence=target_evidence,
                        binding=binding,
                        failure_type="PATH_DIVERGED",
                        reason="target checkpoint diverged twice",
                    )
                )
                return ReplayExecutionResult(
                    status="FAIL",
                    reason="target checkpoint diverged twice",
                    failure_type="PATH_DIVERGED",
                    trace=trace,
                    completed_checkpoints=index + 1,
                    warning_codes=sorted(warning_codes),
                    failed_step_index=index,
                    last_capture=target_capture,
                )
            _record_reachability_warnings(warning_codes, target_evidence)
            trace.append(
                build_replay_trace_step(
                    step_index=index,
                    step=step,
                    status="PASS",
                    duration_ms=(time.monotonic() - started) * 1000,
                    source_evidence=source_evidence,
                    target_evidence=target_evidence,
                    binding=binding,
                )
            )
            capture = target_capture
            active_step_index = None
            active_step = None
            active_source_evidence = None
            active_binding = None
            active_started = None
        verify_terminal_boundaries(capture)
    except InspectionAborted:
        if active_step_index is not None and active_step is not None:
            trace.append(
                build_replay_trace_step(
                    step_index=active_step_index,
                    step=active_step,
                    status="ABORTED",
                    duration_ms=(time.monotonic() - (active_started or time.monotonic())) * 1000,
                    source_evidence=active_source_evidence,
                    binding=active_binding,
                    failure_type="CANCELLED",
                    reason="replay cancelled",
                )
            )
        return ReplayExecutionResult(
            status="ABORTED",
            reason="replay cancelled",
            failure_type="CANCELLED",
            trace=trace,
            completed_checkpoints=(
                active_step_index + 1 if active_step_index is not None else 0
            ),
            warning_codes=sorted(warning_codes),
            failed_step_index=active_step_index,
            last_capture=capture,
        )
    except (LocatorAmbiguous, LocatorDrift) as exc:
        failure_type = (
            "LOCATOR_AMBIGUOUS"
            if isinstance(exc, LocatorAmbiguous)
            else "LOCATOR_NOT_FOUND"
        )
        if active_step_index is not None and active_step is not None:
            trace.append(
                build_replay_trace_step(
                    step_index=active_step_index,
                    step=active_step,
                    status="FAIL",
                    duration_ms=(time.monotonic() - (active_started or time.monotonic())) * 1000,
                    source_evidence=active_source_evidence,
                    binding=active_binding,
                    failure_type=failure_type,
                    reason="current semantic locator could not be executed",
                )
            )
        return ReplayExecutionResult(
            status="FAIL",
            reason="current semantic locator could not be executed",
            failure_type=failure_type,
            trace=trace,
            completed_checkpoints=(
                active_step_index + 1 if active_step_index is not None else 0
            ),
            warning_codes=sorted(warning_codes),
            failed_step_index=active_step_index,
            last_capture=capture,
        )
    except (DeviceDisconnected, PermissionError) as exc:
        is_device_error = isinstance(exc, DeviceDisconnected)
        failure_type = "DEVICE_ERROR" if is_device_error else "BLOCKED"
        safe_reason = "device became unavailable during replay" if is_device_error else "current action blocked by safety policy"
        if active_step_index is not None and active_step is not None:
            trace.append(
                build_replay_trace_step(
                    step_index=active_step_index,
                    step=active_step,
                    status="FAIL" if is_device_error else "BLOCKED",
                    duration_ms=(time.monotonic() - (active_started or time.monotonic())) * 1000,
                    source_evidence=active_source_evidence,
                    binding=active_binding,
                    failure_type=failure_type,
                    reason=safe_reason,
                )
            )
        return ReplayExecutionResult(
            status="FAIL" if is_device_error else "WARNING",
            reason=safe_reason,
            failure_type=failure_type,
            trace=trace,
            completed_checkpoints=(
                active_step_index + 1 if active_step_index is not None else 0
            ),
            warning_codes=sorted(warning_codes),
            failed_step_index=active_step_index,
            last_capture=capture,
        )
    except Exception:
        if active_step_index is not None and active_step is not None:
            trace.append(
                build_replay_trace_step(
                    step_index=active_step_index,
                    step=active_step,
                    status="FAIL",
                    duration_ms=(time.monotonic() - (active_started or time.monotonic())) * 1000,
                    source_evidence=active_source_evidence,
                    binding=active_binding,
                    failure_type="EXECUTION_ERROR",
                    reason="replay step execution failed",
                )
            )
        return ReplayExecutionResult(
            status="FAIL",
            reason="replay step execution failed",
            failure_type="EXECUTION_ERROR",
            trace=trace,
            completed_checkpoints=(
                active_step_index + 1 if active_step_index is not None else 0
            ),
            warning_codes=sorted(warning_codes),
            failed_step_index=active_step_index,
            last_capture=capture,
        )

    return ReplayExecutionResult(
        status="WARNING" if warning_codes else "PASS",
        reason="weak or changed compatibility evidence" if warning_codes else None,
        failure_type=None,
        trace=trace,
        completed_checkpoints=len(checkpoints),
        boundary_evidence=aggregate_boundary_evidence(verified_boundaries),
        warning_codes=sorted(warning_codes),
        last_capture=capture,
    )


__all__ = [
    "BOUNDARY_EVIDENCE_CHANGED",
    "BOUNDARY_EVIDENCE_NOT_VERIFIABLE",
    "BOUNDARY_EVIDENCE_VERIFIED",
    "MAX_REPLAY_CHAINS",
    "REACHABILITY_OBSERVED_ONCE",
    "REACHABILITY_UNKNOWN",
    "REACHABILITY_UNSTABLE",
    "REACHABILITY_VERIFIED_TWICE",
    "REPLAY_PLAN_VERSION",
    "REPLAY_SCOPE_DIAGNOSTIC_ONLY",
    "REPLAY_SCOPE_FULL_PATH",
    "REPLAY_SCOPE_NONE",
    "REPLAY_SCOPE_SAFETY_PREFIX",
    "ReplayActionBinding",
    "ReplayExecutionResult",
    "ReplayPlanError",
    "aggregate_boundary_evidence",
    "build_replay_plan",
    "build_replay_trace_step",
    "derive_replay_eligibility",
    "evaluate_reachability",
    "execute_replay_chain",
    "legacy_replay_eligibility",
    "normalise_terminal_outcome",
    "probe_replay_boundary",
    "rebind_replay_action",
    "state_reachability_evidence",
    "terminal_boundaries_for_state",
]
