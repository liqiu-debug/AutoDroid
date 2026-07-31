"""Cross-run application map for inspection coverage.

Every other inspection table is scoped to one run, so a run can only measure
itself: it discovers N screens and reports that it covered N of N.  Two runs of
the Haier mall with the same configuration shared only 47 of ~75 page templates,
so that self-referential number moved 38% between runs while claiming to be
complete both times.

This module owns the package-scoped alternative.  A run *updates* the map at the
end (:func:`sync_app_map`) and *reads* it while exploring
(:func:`load_app_map`, :func:`surface_priority`), which turns consecutive runs
from repetition into complementary passes and gives the report a denominator
that does not move with the sample.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from sqlalchemy import func
from sqlmodel import Session, select

from backend.inspection.semantics import SURFACE_FINGERPRINT_VERSION
from backend.models import (
    InspectionAppAction,
    InspectionAppSurface,
    InspectionRun,
    InspectionState,
    InspectionTransition,
)

logger = logging.getLogger(__name__)

# Transition statuses that prove an action slot was actually exercised.  A
# blocked or never-reached action must stay uncovered, otherwise the safety
# policy would quietly manufacture coverage.
COVERED_STATUSES = frozenset({"PASS", "SUCCESS", "SELF_LOOP", "NO_NEW_COVERAGE"})
FAILED_STATUSES = frozenset(
    {"LOCATOR_NOT_FOUND", "LOCATOR_AMBIGUOUS", "FAILED", "PATH_DIVERGED"}
)

# Frontier tiers.  Lower wins; the engine's own tiers (100 critical commerce,
# 550 unknown, 700 default) are interleaved deliberately so an unseen surface
# outranks a critical page that has already been covered.
PRIORITY_NEW_SURFACE = 90
PRIORITY_UNCOVERED_ACTION = 150
PRIORITY_STALE_ACTION = 300
PRIORITY_FAILED_RETRY = 400
# Everything on this surface was covered recently.  Demote below the engine's
# real work tiers (500 uncovered group, 600 home visual) so a fresh run spends
# its budget on what the map still owes rather than on a repeat pass.
PRIORITY_COVERED = 750

# An action slot older than this is worth revisiting before an already-fresh one.
STALE_AFTER = timedelta(days=3)

# Roles like ``COMMAND:<sha256-of-label>`` and ``NAV:<hash>`` are minted per
# record - every product name hashes differently - so as slot identities they
# make the action denominator grow forever: run 73 carried 488 slots of which
# 151 were "never covered" hashes that will never recur.  They collapse to one
# dynamic slot per prefix; named roles (``COMMAND:PAY``, ``FAVORITE``) survive.
_DYNAMIC_ROLE_RE = re.compile(r"^([A-Z_]+):[0-9a-f]{64}$")


def normalize_action_slot(action_role: object, action_role_key: object) -> str:
    """Map one executed action onto its stable cross-run slot identity."""
    role = str(action_role or "").strip()
    if role:
        match = _DYNAMIC_ROLE_RE.match(role)
        if match:
            return f"{match.group(1)}:DYNAMIC"
        return role
    return str(action_role_key or "").strip()


@dataclass(frozen=True)
class ActionRecord:
    action_role_key: str
    action_role: str
    last_covered_at: Optional[datetime]
    last_covered_run_id: Optional[int]
    coverage_count: int
    last_status: str
    failed_run_count: int


@dataclass
class SurfaceRecord:
    surface_key: str
    page_subtype: str
    role: str
    label: Optional[str]
    seen_run_count: int
    last_seen_run_id: Optional[int]
    last_seen_at: Optional[datetime]
    is_retired: bool
    actions: Dict[str, ActionRecord] = field(default_factory=dict)


@dataclass
class AppMapView:
    """An immutable in-memory snapshot, loaded once per branch."""

    package_name: str
    fingerprint_version: int
    surfaces: Dict[str, SurfaceRecord] = field(default_factory=dict)
    loaded_at: Optional[datetime] = None

    def known_surface_keys(self, *, include_retired: bool = False) -> Set[str]:
        return {
            key
            for key, record in self.surfaces.items()
            if include_retired or not record.is_retired
        }

    def get(self, surface_key: Optional[str]) -> Optional[SurfaceRecord]:
        if not surface_key:
            return None
        return self.surfaces.get(str(surface_key))


@dataclass
class AppMapSyncResult:
    package_name: str
    fingerprint_version: int
    surfaces_created: int = 0
    surfaces_updated: int = 0
    actions_created: int = 0
    actions_updated: int = 0
    states_without_surface: int = 0

    def as_dict(self) -> Dict[str, object]:
        return {
            "package_name": self.package_name,
            "fingerprint_version": self.fingerprint_version,
            "surfaces_created": self.surfaces_created,
            "surfaces_updated": self.surfaces_updated,
            "actions_created": self.actions_created,
            "actions_updated": self.actions_updated,
            "states_without_surface": self.states_without_surface,
        }


def load_app_map(
    session: Session,
    package_name: str,
    *,
    fingerprint_version: int = SURFACE_FINGERPRINT_VERSION,
    now: Optional[datetime] = None,
) -> AppMapView:
    """Load the accumulated map for one package at the current rule version."""
    surfaces = session.exec(
        select(InspectionAppSurface).where(
            InspectionAppSurface.package_name == package_name,
            InspectionAppSurface.surface_fingerprint_version == fingerprint_version,
        )
    ).all()
    view = AppMapView(
        package_name=package_name,
        fingerprint_version=fingerprint_version,
        loaded_at=now or datetime.now(),
    )
    if not surfaces:
        return view
    by_id: Dict[int, SurfaceRecord] = {}
    for row in surfaces:
        record = SurfaceRecord(
            surface_key=str(row.surface_key),
            page_subtype=str(row.page_subtype or "UNKNOWN"),
            role=str(row.role or "UNKNOWN"),
            label=row.label,
            seen_run_count=int(row.seen_run_count or 0),
            last_seen_run_id=row.last_seen_run_id,
            last_seen_at=row.last_seen_at,
            is_retired=bool(row.is_retired),
        )
        view.surfaces[record.surface_key] = record
        if row.id is not None:
            by_id[int(row.id)] = record
    actions = session.exec(
        select(InspectionAppAction).where(
            InspectionAppAction.surface_id.in_(list(by_id.keys()))  # type: ignore[union-attr]
        )
    ).all()
    for row in actions:
        record = by_id.get(int(row.surface_id))
        if record is None:
            continue
        record.actions[str(row.action_role_key)] = ActionRecord(
            action_role_key=str(row.action_role_key),
            action_role=str(row.action_role or ""),
            last_covered_at=row.last_covered_at,
            last_covered_run_id=row.last_covered_run_id,
            coverage_count=int(row.coverage_count or 0),
            last_status=str(row.last_status or "NEVER"),
            failed_run_count=int(row.failed_run_count or 0),
        )
    return view


def surface_priority(
    view: Optional[AppMapView],
    surface_key: Optional[str],
    action_role_keys: Sequence[str],
    *,
    now: Optional[datetime] = None,
) -> Tuple[Optional[int], Optional[str]]:
    """Rank a candidate state by what the map still owes coverage.

    Returns ``(None, None)`` when the map has nothing to say, so the caller keeps
    whatever tier its own heuristics produced.
    """
    if view is None or not surface_key:
        return None, None
    if not view.surfaces:
        # An empty map knows nothing: every surface would look new and the
        # engine's own tiers would collapse to one value.  Stay silent until a
        # run or the backfill has populated it.
        return None, None
    record = view.get(surface_key)
    if record is None:
        return PRIORITY_NEW_SURFACE, "APP_MAP_NEW_SURFACE"
    if record.is_retired:
        return None, None

    roles = [str(key) for key in action_role_keys if key]
    if not roles:
        return None, None

    reference = now or view.loaded_at or datetime.now()
    has_never = False
    has_failed = False
    oldest: Optional[datetime] = None
    for role_key in roles:
        action = record.actions.get(role_key)
        if action is None or action.last_status == "NEVER" or not action.coverage_count:
            has_never = True
            continue
        if action.last_status in FAILED_STATUSES:
            has_failed = True
        if action.last_covered_at is not None:
            oldest = (
                action.last_covered_at
                if oldest is None
                else min(oldest, action.last_covered_at)
            )
    if has_never:
        return PRIORITY_UNCOVERED_ACTION, "APP_MAP_UNCOVERED_ACTION"
    if oldest is not None and reference - oldest >= STALE_AFTER:
        return PRIORITY_STALE_ACTION, "APP_MAP_STALE_ACTION"
    if has_failed:
        return PRIORITY_FAILED_RETRY, "APP_MAP_RETRY_FAILED"
    return PRIORITY_COVERED, "APP_MAP_COVERED"


def _action_outcomes(
    transitions: Iterable[InspectionTransition],
) -> Dict[int, Dict[str, Tuple[str, str, str]]]:
    """Collapse a run's transitions into per-state action-slot verdicts.

    A slot exercised several times keeps its best outcome: one successful
    execution is what coverage claims, and a later locator failure on a stale
    replay must not erase it.
    """
    outcomes: Dict[int, Dict[str, Tuple[str, str, str]]] = {}
    for edge in transitions:
        slot = normalize_action_slot(edge.action_role, edge.action_role_key)
        if not slot:
            continue
        state_slots = outcomes.setdefault(int(edge.from_state_id), {})
        status = str(edge.status or "")
        existing = state_slots.get(slot)
        if existing is not None and existing[0] in COVERED_STATUSES:
            continue
        state_slots[slot] = (
            status,
            str(edge.action_role or ""),
            str(edge.action_type or ""),
        )
    return outcomes


def _surface_run_counts(
    session: Session,
    *,
    package_name: str,
    fingerprint_version: int,
) -> Dict[str, int]:
    """Distinct runs that have observed each surface, straight from the states.

    Counters that increment on every sync drift: re-folding a run would inflate
    them, and the denominator the report leans on would stop being reproducible.
    Deriving them keeps the map a pure projection of the run data.
    """
    rows = session.exec(
        select(
            InspectionState.surface_key,
            func.count(func.distinct(InspectionState.run_id)),
        )
        .join(InspectionRun, InspectionRun.id == InspectionState.run_id)
        .where(
            InspectionRun.package_name == package_name,
            InspectionState.surface_key.is_not(None),  # type: ignore[union-attr]
            InspectionState.surface_fingerprint_version == fingerprint_version,
        )
        .group_by(InspectionState.surface_key)
    ).all()
    return {str(key): int(count) for key, count in rows if key}


def _action_run_counts(
    session: Session,
    *,
    package_name: str,
    fingerprint_version: int,
    statuses: Iterable[str],
) -> Dict[Tuple[str, str], int]:
    """Distinct runs in which each (surface, action slot) hit one of ``statuses``.

    Aggregated in Python rather than SQL because the slot identity is the
    *normalized* role: two content-hashed commands from different runs must
    count as the same dynamic slot.
    """
    rows = session.exec(
        select(
            InspectionState.surface_key,
            InspectionTransition.action_role,
            InspectionTransition.action_role_key,
            InspectionTransition.run_id,
        )
        .join(
            InspectionState,
            InspectionState.id == InspectionTransition.from_state_id,
        )
        .join(InspectionRun, InspectionRun.id == InspectionTransition.run_id)
        .where(
            InspectionRun.package_name == package_name,
            InspectionState.surface_key.is_not(None),  # type: ignore[union-attr]
            InspectionState.surface_fingerprint_version == fingerprint_version,
            InspectionTransition.action_role_key.is_not(None),  # type: ignore[union-attr]
            InspectionTransition.status.in_(tuple(statuses)),  # type: ignore[union-attr]
        )
        .distinct()
    ).all()
    run_sets: Dict[Tuple[str, str], Set[int]] = {}
    for surface, action_role, action_role_key, run_id in rows:
        slot = normalize_action_slot(action_role, action_role_key)
        if not surface or not slot:
            continue
        run_sets.setdefault((str(surface), slot), set()).add(int(run_id))
    return {key: len(runs) for key, runs in run_sets.items()}


def sync_app_map(
    session: Session,
    run_id: int,
    *,
    package_name: str,
    fingerprint_version: int = SURFACE_FINGERPRINT_VERSION,
    now: Optional[datetime] = None,
) -> AppMapSyncResult:
    """Fold one finished run into the package-scoped map.

    Called once per run from a single write point, so nothing in the exploration
    hot loop has to keep the map consistent.  Idempotent: every counter is
    derived from the stored runs, so re-folding the same run is a no-op.
    """
    stamp = now or datetime.now()
    result = AppMapSyncResult(
        package_name=package_name, fingerprint_version=fingerprint_version
    )

    states = session.exec(
        select(InspectionState).where(InspectionState.run_id == run_id)
    ).all()
    if not states:
        return result
    transitions = session.exec(
        select(InspectionTransition).where(InspectionTransition.run_id == run_id)
    ).all()
    outcomes = _action_outcomes(transitions)

    existing_rows = session.exec(
        select(InspectionAppSurface).where(
            InspectionAppSurface.package_name == package_name,
            InspectionAppSurface.surface_fingerprint_version == fingerprint_version,
        )
    ).all()
    surface_rows: Dict[str, InspectionAppSurface] = {
        str(row.surface_key): row for row in existing_rows
    }
    surface_run_counts = _surface_run_counts(
        session, package_name=package_name, fingerprint_version=fingerprint_version
    )
    action_run_counts = _action_run_counts(
        session,
        package_name=package_name,
        fingerprint_version=fingerprint_version,
        statuses=COVERED_STATUSES,
    )
    failed_run_counts = _action_run_counts(
        session,
        package_name=package_name,
        fingerprint_version=fingerprint_version,
        statuses=FAILED_STATUSES,
    )

    # Group the run's states by surface so each surface is touched once.
    grouped: Dict[str, List[InspectionState]] = {}
    for state in states:
        surface_key = str(state.surface_key or "")
        if not surface_key:
            result.states_without_surface += 1
            continue
        if int(state.surface_fingerprint_version or 1) != fingerprint_version:
            continue
        grouped.setdefault(surface_key, []).append(state)

    for surface_key, members in grouped.items():
        representative = _representative_state(members)
        row = surface_rows.get(surface_key)
        if row is None:
            row = InspectionAppSurface(
                package_name=package_name,
                surface_key=surface_key,
                surface_fingerprint_version=fingerprint_version,
                page_subtype=str(representative.page_subtype or "UNKNOWN"),
                role=str(representative.page_subtype or "UNKNOWN"),
                first_seen_run_id=run_id,
                seen_run_count=0,
            )
            result.surfaces_created += 1
        else:
            result.surfaces_updated += 1
        row.seen_run_count = surface_run_counts.get(
            surface_key, max(1, int(row.seen_run_count or 0))
        )
        row.last_seen_run_id = run_id
        row.last_seen_at = stamp
        row.page_subtype = str(representative.page_subtype or "UNKNOWN")
        if representative.id is not None:
            row.representative_state_id = int(representative.id)
        if representative.screenshot_path:
            row.representative_screenshot_path = str(representative.screenshot_path)
        row.updated_at = stamp
        session.add(row)
        session.flush()
        surface_rows[surface_key] = row

        created, updated = _sync_surface_actions(
            session,
            surface_row=row,
            members=members,
            outcomes=outcomes,
            run_id=run_id,
            package_name=package_name,
            stamp=stamp,
            action_run_counts=action_run_counts,
            failed_run_counts=failed_run_counts,
        )
        result.actions_created += created
        result.actions_updated += updated

    session.commit()
    logger.info("inspection app map synced: %s", result.as_dict())
    return result


def _representative_state(members: Sequence[InspectionState]) -> InspectionState:
    """Prefer an expanded capture with a screenshot as the surface's face."""

    def rank(state: InspectionState) -> Tuple[int, int, int]:
        expanded = 0 if str(state.expansion_status or "") == "EXPANDED" else 1
        has_shot = 0 if state.screenshot_path else 1
        return (expanded, has_shot, int(state.id or 0))

    return sorted(members, key=rank)[0]


def _sync_surface_actions(
    session: Session,
    *,
    surface_row: InspectionAppSurface,
    members: Sequence[InspectionState],
    outcomes: Mapping[int, Mapping[str, Tuple[str, str, str]]],
    run_id: int,
    package_name: str,
    stamp: datetime,
    action_run_counts: Mapping[Tuple[str, str], int],
    failed_run_counts: Mapping[Tuple[str, str], int],
) -> Tuple[int, int]:
    surface_id = int(surface_row.id or 0)
    if not surface_id:
        return 0, 0
    surface_key = str(surface_row.surface_key or "")

    # Merge every member state's slots: the surface owes coverage for the union
    # of the action slots its instances exposed.
    merged: Dict[str, Tuple[str, str, str]] = {}
    for state in members:
        for role_key, payload in (outcomes.get(int(state.id or 0)) or {}).items():
            existing = merged.get(role_key)
            if existing is not None and existing[0] in COVERED_STATUSES:
                continue
            merged[role_key] = payload
    if not merged:
        return 0, 0

    action_rows: Dict[str, InspectionAppAction] = {}
    for row in session.exec(
        select(InspectionAppAction).where(
            InspectionAppAction.surface_id == surface_id
        )
    ).all():
        stored_key = str(row.action_role_key)
        if normalize_action_slot(row.action_role, stored_key) != stored_key:
            # A ledger row from before slot normalization: keyed by a per-record
            # hash that no future run can ever match, so it would sit in
            # "never covered" forever and keep manufacturing phantom debt.
            session.delete(row)
            continue
        action_rows[stored_key] = row
    created = 0
    updated = 0
    for role_key, (status, action_role, action_type) in merged.items():
        row = action_rows.get(role_key)
        if row is None:
            row = InspectionAppAction(
                surface_id=surface_id,
                package_name=package_name,
                action_role_key=role_key,
                # A dynamic slot's raw label is per-record noise; the slot name
                # itself is the readable identity.
                action_role=(
                    action_role
                    if action_role and action_role == role_key
                    else role_key
                ),
                action_type=action_type or None,
                first_seen_run_id=run_id,
            )
            created += 1
        else:
            updated += 1
        if action_type and not row.action_type:
            row.action_type = action_type
        if status in COVERED_STATUSES:
            row.coverage_count = action_run_counts.get(
                (surface_key, role_key), max(1, int(row.coverage_count or 0))
            )
            row.last_covered_run_id = run_id
            row.last_covered_at = stamp
            row.last_status = status
        elif status in FAILED_STATUSES:
            row.failed_run_count = failed_run_counts.get(
                (surface_key, role_key), max(1, int(row.failed_run_count or 0))
            )
            row.last_status = status
        elif status and row.last_status == "NEVER":
            # BLOCKED / SAMPLED_OUT / BUDGET_* leave the slot uncovered, but the
            # slot itself is now known to exist.
            row.last_status = status
        row.updated_at = stamp
        session.add(row)
    return created, updated


# Default reporting window.  The acceptance gate is cumulative, not per-run: one
# device-hour cannot walk a commerce app, so a single run's number is context and
# the window's number is the verdict.
DEFAULT_WINDOW_DAYS = 7


def build_surface_coverage(
    session: Session,
    run_id: int,
    *,
    package_name: str,
    fingerprint_version: int = SURFACE_FINGERPRINT_VERSION,
    window_days: int = DEFAULT_WINDOW_DAYS,
    now: Optional[datetime] = None,
) -> Dict[str, object]:
    """Describe coverage against the accumulated map, with a real denominator.

    The manifest verdict answers "did the business journeys pass".  This answers
    the question the old report could not: *out of how many screens*, and which
    ones were missed.  Both are needed, and they must not be merged into one
    percentage - that pairing is what made the previous report unreadable.
    """
    reference = now or datetime.now()
    horizon = reference - timedelta(days=max(1, int(window_days)))

    surfaces = session.exec(
        select(InspectionAppSurface).where(
            InspectionAppSurface.package_name == package_name,
            InspectionAppSurface.surface_fingerprint_version == fingerprint_version,
            InspectionAppSurface.is_retired == False,  # noqa: E712 - SQL predicate
        )
    ).all()
    if not surfaces:
        return {
            "surface_fingerprint_version": fingerprint_version,
            "available": False,
            "reason": "APP_MAP_EMPTY",
        }

    surface_ids = [int(row.id) for row in surfaces if row.id is not None]
    actions = session.exec(
        select(InspectionAppAction).where(
            InspectionAppAction.surface_id.in_(surface_ids)  # type: ignore[union-attr]
        )
    ).all()
    slots_by_surface: Dict[int, List[InspectionAppAction]] = {}
    for row in actions:
        slots_by_surface.setdefault(int(row.surface_id), []).append(row)

    visited_this_run = {
        str(key)
        for key in session.exec(
            select(InspectionState.surface_key).where(
                InspectionState.run_id == run_id,
                InspectionState.surface_key.is_not(None),  # type: ignore[union-attr]
            )
        ).all()
        if key
    }

    never_covered: List[Dict[str, object]] = []
    stale: List[Dict[str, object]] = []
    run_fully_covered = 0
    cumulative_covered = 0
    unclassified = 0
    slot_total = 0
    slot_covered_ever = 0
    slot_covered_this_run = 0
    slot_never = 0

    for row in surfaces:
        slots = slots_by_surface.get(int(row.id or 0), [])
        slot_total += len(slots)
        descriptor = {
            "surface_key": str(row.surface_key),
            "page_subtype": str(row.page_subtype or "UNKNOWN"),
            "label": row.label or None,
            "first_seen_run_id": row.first_seen_run_id,
            "last_seen_run_id": row.last_seen_run_id,
            "representative_state_id": row.representative_state_id,
            "representative_screenshot_path": row.representative_screenshot_path,
            "action_slot_count": len(slots),
        }
        if str(row.page_subtype or "UNKNOWN").upper() in {"UNKNOWN", "OPAQUE"}:
            unclassified += 1

        covered_ever = [slot for slot in slots if int(slot.coverage_count or 0) > 0]
        slot_covered_ever += len(covered_ever)
        slot_never += len(slots) - len(covered_ever)
        slot_covered_this_run += sum(
            1 for slot in slots if slot.last_covered_run_id == run_id
        )

        if not slots or not covered_ever:
            never_covered.append(descriptor)
            continue
        if len(covered_ever) == len(slots) and all(
            slot.last_covered_run_id == run_id for slot in slots
        ):
            run_fully_covered += 1
        fresh = [
            slot
            for slot in slots
            if slot.last_covered_at is not None and slot.last_covered_at >= horizon
        ]
        if len(fresh) == len(slots):
            cumulative_covered += 1
        else:
            stale.append(
                {
                    **descriptor,
                    "stale_slot_count": len(slots) - len(fresh),
                    "oldest_covered_at": min(
                        (
                            slot.last_covered_at.isoformat()
                            for slot in slots
                            if slot.last_covered_at is not None
                        ),
                        default=None,
                    ),
                }
            )

    known = len(surfaces)
    verdict = (
        "COMPLETE"
        if cumulative_covered >= known and not never_covered
        else "INCOMPLETE"
    )

    def sort_key(item: Dict[str, object]) -> Tuple[str, str]:
        return (str(item.get("page_subtype") or ""), str(item.get("surface_key") or ""))

    return {
        "surface_fingerprint_version": fingerprint_version,
        "available": True,
        "package_known_surfaces": known,
        "run_visited_surfaces": len(visited_this_run & {
            str(row.surface_key) for row in surfaces
        }),
        "run_fully_covered_surfaces": run_fully_covered,
        "cumulative_window_days": int(window_days),
        "cumulative_covered_surfaces": cumulative_covered,
        # The acceptance gate, per the agreed reporting contract.
        "cumulative_verdict": verdict,
        "unclassified_surfaces": unclassified,
        "never_covered_surfaces": sorted(never_covered, key=sort_key),
        "stale_surfaces": sorted(stale, key=sort_key),
        "action_slots": {
            "total": slot_total,
            "covered_ever": slot_covered_ever,
            "covered_this_run": slot_covered_this_run,
            "never_covered": slot_never,
        },
    }
