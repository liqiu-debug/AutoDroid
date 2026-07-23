"""Sanitized, persistent action maps for inspection states."""

from __future__ import annotations

import json
import os
import re
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from backend.inspection.semantics import InspectionAction


_SENSITIVE_HINT_RE = re.compile(
    r"密码|口令|验证码|身份证|银行卡|手机号|token|secret|password|passwd|pin|otp|"
    r"credit\s*card|bank\s*card|phone",
    re.I,
)
_INVALID_LABEL_CHARS_RE = re.compile(r"[\uFFFC\uFFFD]")
_SPACE_RE = re.compile(r"\s+")
_SELECTOR_DETAIL_RE = re.compile(
    r"(?:xpath|selector|resource[-_ ]?id)\s*[:=]|//[\w*]+|\[@[\w:-]+",
    re.I,
)
_SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"\b(password|passwd|token|secret|otp|pin)\b\s*[:=]\s*([^\s,;]+)",
    re.I,
)
_LABEL_LIMIT = 48
_NONTERMINAL_STATUSES = {"", "PENDING", "QUEUED", "ACTIVE", "INVOKED"}
_EXECUTED_STATUSES = {"PASS", "SELF_LOOP", "NO_EFFECT"}
_FAILED_STATUSES = {
    "ACTION_ERROR",
    "ERROR",
    "APP_EXIT",
    "EXTERNAL_APP",
    "LOCATOR_AMBIGUOUS",
    "LOCATOR_NOT_FOUND",
}
_NOT_REACHED_STATUSES = {
    "NOT_REACHED",
    "CANCELLED",
    "BUDGET_NOT_REACHED",
    "BUDGET_LIMIT",
    "PARENT_RECOVERY_FAILED",
    "PARENT_RECOVERY_CASCADE",
    "PATH_DIVERGED",
    "QUEUE_TRUNCATED",
    "UNSTABLE_PARENT",
}
_SKIPPED_STATUSES = {
    "BLOCKED",
    "COORDINATE_ONLY",
    "COORDINATE_UNSAFE",
    "COORDINATE_STALE",
    "AMBIGUOUS",
    "SKIPPED",
    "VARIANT_LIMIT",
    "FILTERED_NON_ACTIONABLE",
    "COVERAGE_EXHAUSTED",
    "CYCLE_CONVERGED",
    "COVERED_BY_CONTRACT",
    "SAMPLED_OUT",
    "NAVIGATION_REUSED",
    "VISUAL_STALE",
    "NO_NEW_COVERAGE",
}


def _now_iso() -> str:
    return datetime.now().isoformat()


def _terminal_disposition(entry: Dict[str, Any]) -> str:
    status = str(entry.get("status") or "").strip().upper()
    if status == "COVERED_BY_FAMILY":
        return "FAMILY_REUSED"
    if status == "COVERED_BY_CONTRACT":
        return "CONTRACT_REUSED"
    if status == "NAVIGATION_REUSED":
        return "NAVIGATION_REUSED"
    if status in _EXECUTED_STATUSES:
        return "EXECUTED"
    if status in _FAILED_STATUSES:
        return "FAILED"
    if status in _NOT_REACHED_STATUSES:
        if status in {"CANCELLED", "BUDGET_LIMIT"} and bool(entry.get("invoked") or entry.get("invocation_unknown")):
            return "RESULT_UNKNOWN"
        return "NOT_REACHED"
    if status in _SKIPPED_STATUSES:
        return "SKIPPED"
    # A terminal extension status still represents a completed decision.  Do
    # not leave it looking pending in persisted reports.
    return "EXECUTED" if entry.get("invoked") else "SKIPPED"


def normalize_terminal_action_entries(
    action_map: Dict[str, Any],
    *,
    phase: Optional[str] = None,
) -> None:
    """Backfill terminal metadata without changing an action's result."""
    finalized_at = _now_iso()
    changed = False
    for entry in action_map.get("actions") or []:
        if not isinstance(entry, dict):
            continue
        status = str(entry.get("status") or "").strip().upper()
        if status in _NONTERMINAL_STATUSES:
            continue
        disposition = str(entry.get("execution_disposition") or "").strip().upper()
        if disposition in {"", "PENDING"}:
            entry["execution_disposition"] = _terminal_disposition(entry)
            changed = True
        if not entry.get("phase_at_finalize"):
            entry["phase_at_finalize"] = phase or action_map.get("phase") or "finalize"
            changed = True
        if not entry.get("finalized_at"):
            entry["finalized_at"] = finalized_at
            changed = True
    if changed:
        action_map["updated_at"] = finalized_at


def _matches_rule(meta: Dict[str, Any], rule: Dict[str, Any]) -> bool:
    values = {
        "content_desc_regex": str(meta.get("content_desc") or ""),
        "text_regex": str(meta.get("text") or ""),
        "class_regex": str(meta.get("class") or ""),
    }
    matched = False
    for key, value in values.items():
        pattern = str(rule.get(key) or "").strip()
        if not pattern:
            continue
        matched = True
        try:
            if re.search(pattern, value, re.I) is None:
                return False
        except re.error:
            return False
    return matched


def sanitize_action_label(
    action: InspectionAction,
    *,
    sanitizer_rules: Optional[Sequence[Dict[str, Any]]] = None,
    secret_values: Optional[Sequence[str]] = None,
) -> str:
    """Return a short label without selectors, secrets or sensitive values."""
    meta = dict(action.target_meta or {})
    raw = str(
        meta.get("product_title")
        or meta.get("content_desc")
        or meta.get("text")
        or ""
    ).strip()
    sensitive_haystack = " ".join(
        str(meta.get(key) or "") for key in ("content_desc", "text", "class", "ancestor_semantic")
    )
    if (
        bool(meta.get("password"))
        or _SENSITIVE_HINT_RE.search(sensitive_haystack)
        or any(_matches_rule(meta, rule) for rule in sanitizer_rules or ())
    ):
        return "敏感输入框"

    label = _INVALID_LABEL_CHARS_RE.sub("", raw)
    for secret in secret_values or ():
        value = str(secret or "")
        if value:
            label = label.replace(value, "***")
    label = _SPACE_RE.sub(" ", label).strip()
    if not label:
        label = str(meta.get("class") or action.action_type or "控件")
    return label[:_LABEL_LIMIT]


def _locator_method(action: InspectionAction) -> str:
    candidates = [item for item in action.locator_candidates or () if isinstance(item, dict)]
    if candidates:
        return str(candidates[0].get("by") or "unknown").lower()
    if action.action_type == "back":
        return "back"
    return "coordinate" if action.coordinate_only else "none"


def _sanitize_runtime_message(
    value: Any,
    *,
    secret_values: Optional[Sequence[str]] = None,
    limit: int = 300,
) -> Optional[str]:
    text = _SPACE_RE.sub(" ", str(value or "").replace("\x00", " ")).strip()
    if not text:
        return None
    for secret in secret_values or ():
        secret_text = str(secret or "")
        if secret_text:
            text = text.replace(secret_text, "***")
    text = _SENSITIVE_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=***", text)
    if _SELECTOR_DETAIL_RE.search(text):
        return "定位详情已隐藏"
    return text[:limit]


def build_action_map(
    *,
    run_id: int,
    branch_key: str,
    state_id: int,
    activity: str,
    screen_size: Tuple[int, int],
    actions: Sequence[InspectionAction],
    screenshot_path: Optional[str] = None,
    sanitizer_rules: Optional[Sequence[Dict[str, Any]]] = None,
    secret_values: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Build the initial complete action board for one captured state."""
    click_order = 0
    payload_actions: List[Dict[str, Any]] = []
    width, height = (int(screen_size[0]), int(screen_size[1]))
    now = _now_iso()
    for index, action in enumerate(actions):
        meta = dict(action.target_meta or {})
        can_invoke_numbered = bool(
            action.action_type in {"click", "input"}
            and not action.risk_type
            and (not action.coordinate_only or action.action_type == "click")
        )
        display_order = None
        if can_invoke_numbered:
            click_order += 1
            display_order = click_order
        initial_status = (
            "BLOCKED"
            if action.risk_type
            else "COORDINATE_ONLY"
            if action.coordinate_only and action.action_type not in {"click", "scroll"}
            else "PENDING"
        )
        bounds = meta.get("bounds")
        if not isinstance(bounds, (list, tuple)) or len(bounds) != 4:
            bounds = None
        payload_actions.append(
            {
                "action_id": f"{state_id}:{index}:{action.action_key}",
                "action_key": action.action_key,
                "control_key": action.action_key,
                "page_order": index + 1,
                "display_order": display_order,
                "action_type": action.action_type,
                "label": sanitize_action_label(
                    action,
                    sanitizer_rules=sanitizer_rules,
                    secret_values=secret_values,
                ),
                "class_name": str(meta.get("class") or ""),
                "bounds": list(bounds) if bounds else None,
                "direction": meta.get("direction"),
                "locator_method": _locator_method(action),
                "action_role": action.action_role,
                "action_role_key": action.action_role_key,
                "action_anchor_key": action.action_anchor_key,
                "action_group_key": action.action_group_key,
                "action_instance_key": action.action_instance_key,
                "sample_policy": action.sample_policy,
                "risk_type": action.risk_type,
                "coordinate_only": bool(action.coordinate_only),
                "replayable": bool(action.replayable),
                "status": initial_status,
                "invoked": False,
                "invocation_unknown": False,
                "attempt_count": 0,
                "global_sequence": None,
                "execution_disposition": ("SKIPPED" if initial_status in {"BLOCKED", "COORDINATE_ONLY"} else "PENDING"),
                "failure_type": None,
                "coverage_source_transition_id": None,
                "coverage_contract_id": None,
                "sampling_disposition": None,
                "phase_at_finalize": ("discovery" if initial_status in {"BLOCKED", "COORDINATE_ONLY"} else None),
                "finalized_at": (now if initial_status in {"BLOCKED", "COORDINATE_ONLY"} else None),
                "reason": (
                    _sanitize_runtime_message(
                        action.blocked_reason,
                        secret_values=secret_values,
                    )
                    if action.risk_type
                    else None
                ),
                "error": None,
            }
        )
    return {
        "schema_version": 3,
        "run_id": int(run_id),
        "branch_key": str(branch_key),
        "state_id": int(state_id),
        "activity": str(activity or ""),
        "screen_width": width,
        "screen_height": height,
        "screenshot_path": screenshot_path,
        "captured_at": now,
        "updated_at": now,
        "actions": payload_actions,
    }


def update_action_map(
    action_map: Dict[str, Any],
    action: InspectionAction,
    *,
    status: str,
    sequence: Optional[int] = None,
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
    phase: Optional[str] = None,
    secret_values: Optional[Sequence[str]] = None,
) -> Optional[Dict[str, Any]]:
    """Update one action in-place and return its sanitized public payload."""
    entries = list(action_map.get("actions") or [])
    entry = next(
        (item for item in entries if item.get("action_key") == action.action_key),
        None,
    )
    if entry is None:
        return None
    entry["status"] = str(status or entry.get("status") or "PENDING")
    normalized_status = str(entry["status"]).strip().upper()
    if sequence is not None:
        entry["global_sequence"] = int(sequence)
    if invoked is not None:
        entry["invoked"] = bool(invoked)
    if invocation_unknown is not None:
        entry["invocation_unknown"] = bool(invocation_unknown)
    if reason is not None:
        entry["reason"] = _sanitize_runtime_message(
            reason,
            secret_values=secret_values,
        )
    if error is not None:
        # Runtime exceptions frequently embed a full XPath/selector.  The
        # action map is a browser-facing, sanitized artifact, so keep only a
        # generic failure marker; detailed diagnostics remain in the protected
        # Transition/fault records.
        entry["error"] = "动作执行异常" if str(error).strip() else None
    if increment_attempt:
        entry["attempt_count"] = int(entry.get("attempt_count") or 0) + 1
    if execution_disposition is not None:
        entry["execution_disposition"] = str(execution_disposition)
    if failure_type is not None:
        entry["failure_type"] = str(failure_type)
    if coverage_source_transition_id is not None:
        entry["coverage_source_transition_id"] = int(coverage_source_transition_id)
    if coverage_contract_id is not None:
        entry["coverage_contract_id"] = int(coverage_contract_id)
    if sampling_disposition is not None:
        entry["sampling_disposition"] = str(sampling_disposition)
    updated_at = _now_iso()
    if normalized_status in _NONTERMINAL_STATUSES:
        if execution_disposition is None:
            entry["execution_disposition"] = "PENDING"
        entry["phase_at_finalize"] = None
        entry["finalized_at"] = None
    else:
        if str(entry.get("execution_disposition") or "").strip().upper() in {
            "",
            "PENDING",
        }:
            entry["execution_disposition"] = _terminal_disposition(entry)
        entry["phase_at_finalize"] = phase or action_map.get("phase") or "explore"
        entry["finalized_at"] = updated_at
    action_map["updated_at"] = updated_at
    return deepcopy(entry)


def finalize_action_map(
    action_map: Dict[str, Any],
    *,
    pending_status: str = "NOT_REACHED",
    reason: Optional[str] = None,
    phase: Optional[str] = None,
) -> None:
    finalized_at = _now_iso()
    for entry in action_map.get("actions") or []:
        if str(entry.get("status") or "") in {"PENDING", "ACTIVE"}:
            entry["status"] = str(pending_status or "NOT_REACHED")
            entry["execution_disposition"] = _terminal_disposition(entry)
            entry["reason"] = entry.get("reason") or _sanitize_runtime_message(reason)
            entry["phase_at_finalize"] = phase
            entry["finalized_at"] = finalized_at
        elif str(entry.get("status") or "") == "INVOKED":
            entry["status"] = "ACTION_ERROR"
            entry["execution_disposition"] = "FAILED"
            entry["failure_type"] = "ACTION_ERROR"
            entry["invocation_unknown"] = True
            entry["reason"] = entry.get("reason") or "设备调用已返回，但未能确认最终结果"
            entry["phase_at_finalize"] = phase
            entry["finalized_at"] = finalized_at
    normalize_terminal_action_entries(
        action_map,
        phase=phase or "finalize",
    )
    action_map["updated_at"] = finalized_at


def write_action_map(path: Path, action_map: Dict[str, Any]) -> None:
    """Atomically persist a sanitized action map."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading_id()}.tmp")
    try:
        temporary.write_text(
            json.dumps(action_map, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(path)
        # Only terminal maps are mirrored.  Intermediate updates continue to
        # overwrite the legacy JSON without creating immutable blob churn.
        from backend.artifact_store import mirror_final_json

        mirror_final_json(path, action_map)
    finally:
        if temporary.exists():
            temporary.unlink()


def threading_id() -> int:
    # Imported lazily to keep the serialized module surface small.
    import threading

    return threading.get_ident()


def read_action_map(path: Path) -> Dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return dict(data) if isinstance(data, dict) else {"actions": []}


def public_action_map(action_map: Dict[str, Any]) -> Dict[str, Any]:
    return deepcopy(action_map)


def finalize_all(action_maps: Iterable[Dict[str, Any]]) -> None:
    for action_map in action_maps:
        finalize_action_map(action_map)
