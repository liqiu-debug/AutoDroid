"""Thread-safe live observation state for model inspection runs.

The inspection engine is synchronous and device-bound, while viewers are
asynchronous WebSocket clients.  This module deliberately keeps the bridge
small: publishers replace the latest complete snapshot and subscribers have a
single-slot queue, so a slow browser can never backpressure device execution.
"""
from __future__ import annotations

import copy
import hashlib
import math
import queue
import re
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Deque, Dict, List, Mapping, Optional, Set


TERMINAL_RUN_STATUSES = {"PASS", "WARNING", "FAIL", "ERROR", "ABORTED"}
_MAX_LABEL_LENGTH = 48
_MAX_STAGE_LENGTH = 160
_MAX_REASON_LENGTH = 500
_SENSITIVE_LABEL_RE = re.compile(
    r"密码|口令|验证码|身份证|银行卡|手机号|"
    r"password|passwd|secret|token|pin|otp|credit\s*card|bank\s*card",
    re.I,
)
_PRIVATE_DETAIL_RE = re.compile(
    r"(?:xpath|selector|resource[-_ ]?id)\s*[:=]|//[\w*]+|\[@[\w:-]+|"
    r"\b(?:password|passwd|token|secret|otp|pin)\b\s*[:=]\s*[^\s,;]+",
    re.I,
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _short_text(value: Any, limit: int) -> Optional[str]:
    if value is None:
        return None
    text = " ".join(str(value).replace("\x00", "").split())
    return text[:limit] if text else None


def _positive_int(value: Any) -> Optional[int]:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _safe_bounds(value: Any) -> Optional[List[int]]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    result: List[int] = []
    for item in value:
        try:
            number = float(item)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number):
            return None
        result.append(int(round(number)))
    if result[2] < result[0] or result[3] < result[1]:
        return None
    return result


def _safe_asset_url(
    value: Any,
    *,
    expected_run_id: Optional[int] = None,
) -> Optional[str]:
    text = _short_text(value, 1000)
    if not text or text.lower().startswith(("data:", "javascript:")):
        return None
    # Live snapshots may reference only same-origin report APIs.  In
    # particular, arbitrary remote URLs and local filesystem paths are not
    # accepted into the browser-facing snapshot.
    if not text.startswith("/api/inspections/"):
        return None
    if expected_run_id is not None and not text.startswith(
        f"/api/inspections/runs/{int(expected_run_id)}/"
    ):
        return None
    return text


def _safe_screenshot_path(
    value: Any,
    *,
    expected_run_id: Optional[int] = None,
) -> Optional[str]:
    text = _short_text(value, 1000)
    if not text or text.startswith(("/", "\\")):
        return None
    normalized = text.replace("\\", "/")
    parts = normalized.split("/")
    if not normalized.startswith("inspection/") or any(
        part in {"", ".", ".."} for part in parts
    ):
        return None
    if expected_run_id is not None:
        expected_prefix = f"inspection/{int(expected_run_id)}/"
        if not normalized.startswith(expected_prefix):
            return None
    return normalized


def _sanitize_page(
    value: Any,
    *,
    expected_run_id: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        return None
    page: Dict[str, Any] = {}
    for key in ("state_id", "page_visit_id", "screen_width", "screen_height"):
        parsed = _positive_int(value.get(key))
        if parsed is not None:
            page[key] = parsed
    for key, limit in (
        ("activity", 300),
        ("foreground_package", 300),
        ("captured_at", 80),
    ):
        cleaned = _short_text(value.get(key), limit)
        if cleaned is not None:
            page[key] = cleaned
    for key in ("screenshot_url", "thumbnail_url"):
        cleaned_url = _safe_asset_url(
            value.get(key),
            expected_run_id=expected_run_id,
        )
        if cleaned_url is not None:
            page[key] = cleaned_url
    screenshot_path = _safe_screenshot_path(
        value.get("screenshot_path"),
        expected_run_id=expected_run_id,
    )
    if screenshot_path is not None:
        page["screenshot_path"] = screenshot_path
    return page


def _sanitize_action(value: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(value, Mapping):
        return None
    action: Dict[str, Any] = {}
    for key, limit in (
        ("action_id", 160),
        ("action_key", 160),
        ("action_type", 40),
        ("locator_type", 40),
        ("locator_method", 40),
        ("risk_type", 80),
        ("status", 60),
        ("result", 60),
        ("direction", 20),
        ("failure_type", 80),
        ("execution_disposition", 80),
        ("action_role", 80),
        ("action_role_key", 160),
    ):
        cleaned = _short_text(value.get(key), limit)
        if cleaned is not None:
            action[key] = cleaned
    for key in ("blocked_reason", "reason"):
        cleaned = _short_text(value.get(key), 240)
        if cleaned is not None:
            action[key] = (
                "敏感详情已隐藏"
                if _PRIVATE_DETAIL_RE.search(cleaned)
                else cleaned
            )

    raw_label = _short_text(value.get("label"), _MAX_LABEL_LENGTH)
    sensitive_haystack = " ".join(
        str(value.get(key) or "")
        for key in ("label", "class_name", "action_type")
    )
    sensitive = bool(
        value.get("sensitive")
        or value.get("is_sensitive")
        or value.get("password")
        or _SENSITIVE_LABEL_RE.search(sensitive_haystack)
    )
    label = "敏感输入框" if sensitive else raw_label
    if label is not None:
        action["label"] = label
    if value.get("error"):
        # Runtime exceptions often embed a complete XPath/selector or an
        # input value. Live observation only needs the failure marker.
        action["error"] = "动作执行异常"

    bounds = _safe_bounds(value.get("bounds"))
    if bounds is not None:
        action["bounds"] = bounds
    for key in (
        "page_order",
        "display_order",
        "sequence",
        "attempts",
        "attempt_count",
        "global_sequence",
        "coverage_source_transition_id",
        "recovery_attempt_count",
    ):
        parsed = _positive_int(value.get(key))
        if parsed is not None:
            action[key] = parsed
    for key in (
        "invoked",
        "invocation_unknown",
        "coordinate_only",
        "replayable",
        "focused",
    ):
        if key in value:
            action[key] = bool(value.get(key))
    return action


def _sanitize_actions(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    result: List[Dict[str, Any]] = []
    # An inspection page is already budget-limited.  Keep an explicit cap as
    # defense against accidental unbounded publisher payloads.
    for item in value[:1000]:
        cleaned = _sanitize_action(item)
        if cleaned is not None:
            result.append(cleaned)
    return result


def sanitize_action_map_payload(
    value: Any,
    *,
    run_id: int,
    state_id: int,
    branch_key: str,
    activity: Optional[str] = None,
    screenshot_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Apply a second browser-facing whitelist to a persisted action map."""
    source = value if isinstance(value, Mapping) else {}
    result: Dict[str, Any] = {
        "schema_version": _positive_int(source.get("schema_version")) or 1,
        "run_id": int(run_id),
        "branch_key": _short_text(branch_key, 100) or "",
        "state_id": int(state_id),
        "activity": _short_text(activity, 300) or "",
        "actions": _sanitize_actions(source.get("actions")),
    }
    for key in ("screen_width", "screen_height"):
        parsed = _positive_int(source.get(key))
        if parsed is not None:
            result[key] = parsed
    for key in ("captured_at", "updated_at"):
        cleaned = _short_text(source.get(key), 80)
        if cleaned is not None:
            result[key] = cleaned
    safe_path = _safe_screenshot_path(
        screenshot_path if screenshot_path is not None else source.get("screenshot_path"),
        expected_run_id=run_id,
    )
    if safe_path is not None:
        result["screenshot_path"] = safe_path
    return result


def _sanitize_progress(value: Any) -> Dict[str, int]:
    if not isinstance(value, Mapping):
        return {}
    result: Dict[str, int] = {}
    for key in (
        "states",
        "transitions",
        "actions_total",
        "actions_finished",
        "blocked",
        "faults",
    ):
        parsed = _positive_int(value.get(key))
        if parsed is not None:
            result[key] = parsed
    return result


def _sanitize_frontier(value: Any) -> Dict[str, int]:
    """Keep bounded frontier counters for the live exploration phase."""
    if not isinstance(value, Mapping):
        return {}
    result: Dict[str, int] = {}
    for key in (
        "queued_count",
        "deferred_count",
        "pending_action_count",
        "expanding_count",
    ):
        parsed = _positive_int(value.get(key))
        if parsed is not None:
            result[key] = parsed
    return result


def _sanitize_device_context(
    value: Any,
    *,
    expected_run_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Keep physical-canvas state separate from the logical action panel."""
    if not isinstance(value, Mapping):
        return {}
    result: Dict[str, Any] = {}
    for key in ("state_id", "page_visit_id"):
        parsed = _positive_int(value.get(key))
        if parsed is not None:
            result[key] = parsed
    for key, limit in (
        ("activity", 300),
        ("foreground_package", 300),
        ("captured_at", 80),
        ("phase", 80),
    ):
        cleaned = _short_text(value.get(key), limit)
        if cleaned is not None:
            result[key] = cleaned
    if "canvas_matches_panel" in value:
        result["canvas_matches_panel"] = bool(value.get("canvas_matches_panel"))
    page = _sanitize_page(value.get("page"), expected_run_id=expected_run_id)
    if page is not None:
        result["page"] = page
    return result


def _sanitize_action_panel(
    value: Any,
    *,
    expected_run_id: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Sanitize the stable logical State whose actions are being expanded."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        return None
    result: Dict[str, Any] = {}
    state_id = _positive_int(
        value.get("state_id", value.get("expansion_owner_state_id"))
    )
    if state_id is not None:
        result["state_id"] = state_id
    epoch = _positive_int(value.get("expansion_epoch"))
    if epoch is not None:
        result["expansion_epoch"] = epoch
    status = _short_text(value.get("expansion_status"), 60)
    if status is not None:
        result["expansion_status"] = status
    page = _sanitize_page(value.get("page"), expected_run_id=expected_run_id)
    if page is not None:
        result["page"] = page
        result.setdefault("state_id", page.get("state_id"))
    result["actions"] = _sanitize_actions(value.get("actions"))
    result["current_action"] = _sanitize_action(value.get("current_action"))
    if "canvas_matches_panel" in value:
        result["canvas_matches_panel"] = bool(value.get("canvas_matches_panel"))
    return result


@dataclass(eq=False)
class LiveSubscription:
    """Single-slot subscriber used by an async WebSocket consumer."""

    _registry: "InspectionLiveRegistry"
    run_id: int
    _queue: "queue.Queue[Optional[Dict[str, Any]]]" = field(
        default_factory=lambda: queue.Queue(maxsize=1)
    )
    _closed: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def get(self, timeout: Optional[float] = None) -> Optional[Dict[str, Any]]:
        return self._queue.get(timeout=timeout)

    def close(self) -> None:
        self._registry.unsubscribe(self)

    def _offer(self, snapshot: Dict[str, Any]) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                self._queue.put_nowait(snapshot)
                return
            except queue.Full:
                pass
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(snapshot)
            except queue.Full:
                pass

    def _mark_closed(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                pass


@dataclass
class _RunChannel:
    snapshot: Dict[str, Any]
    recent_events: Deque[Dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=20)
    )
    subscribers: Set[LiveSubscription] = field(default_factory=set)
    terminal_expires_at: Optional[float] = None


@dataclass(frozen=True)
class TicketClaim:
    session_id: str
    run_id: int
    user_id: int
    kind: str


@dataclass
class _Ticket:
    session_id: str
    run_id: int
    user_id: int
    kind: str
    expires_at: float


@dataclass
class _ViewerSession:
    session_id: str
    run_id: int
    user_id: int
    expires_at: float
    pending_kinds: Set[str] = field(default_factory=lambda: {"event", "video"})
    active_kinds: Set[str] = field(default_factory=set)


class InspectionLiveRegistry:
    """In-memory inspection live state, subscriber, and ticket registry."""

    def __init__(
        self,
        *,
        ticket_ttl_seconds: float = 60.0,
        terminal_retention_seconds: float = 600.0,
        max_sessions_per_run: int = 3,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ticket_ttl_seconds = float(ticket_ttl_seconds)
        self.terminal_retention_seconds = float(terminal_retention_seconds)
        self.max_sessions_per_run = int(max_sessions_per_run)
        self._clock = clock
        self._lock = threading.RLock()
        self._runs: Dict[int, _RunChannel] = {}
        self._tickets: Dict[str, _Ticket] = {}
        self._sessions: Dict[str, _ViewerSession] = {}

    @staticmethod
    def _ticket_digest(token: str) -> str:
        return hashlib.sha256(str(token).encode("utf-8")).hexdigest()

    def _cleanup_locked(self) -> None:
        now = self._clock()
        expired_runs = [
            run_id
            for run_id, channel in self._runs.items()
            if channel.terminal_expires_at is not None
            and channel.terminal_expires_at <= now
        ]
        for run_id in expired_runs:
            channel = self._runs.pop(run_id)
            for subscriber in tuple(channel.subscribers):
                subscriber._mark_closed()

        expired_digests = [
            digest
            for digest, ticket in self._tickets.items()
            if ticket.expires_at <= now
        ]
        for digest in expired_digests:
            ticket = self._tickets.pop(digest)
            viewer = self._sessions.get(ticket.session_id)
            if viewer is not None:
                viewer.pending_kinds.discard(ticket.kind)

        expired_sessions = [
            session_id
            for session_id, viewer in self._sessions.items()
            if not viewer.active_kinds
            and (not viewer.pending_kinds or viewer.expires_at <= now)
        ]
        for session_id in expired_sessions:
            self._sessions.pop(session_id, None)

    def start_run(
        self,
        run_id: int,
        device_serial: str,
        run_status: str = "PENDING",
    ) -> Dict[str, Any]:
        """Create live state for a run, preserving it if already initialized."""
        normalized_id = int(run_id)
        with self._lock:
            self._cleanup_locked()
            existing = self._runs.get(normalized_id)
            if existing is not None:
                return copy.deepcopy(existing.snapshot)
            emitted_at = _utc_now_iso()
            snapshot = {
                "run_id": normalized_id,
                "stream_id": secrets.token_urlsafe(18),
                "stream_started_at": emitted_at,
                "revision": 0,
                "event_type": "RUN_STARTED",
                "emitted_at": emitted_at,
                "run_status": _short_text(run_status, 40) or "PENDING",
                "device_serial": _short_text(device_serial, 300),
                "branch_key": None,
                "phase": None,
                "current_stage": None,
                "page": None,
                "overlay_visible": False,
                "actions": [],
                "current_action": None,
                "action_panel": None,
                "expansion_owner_state_id": None,
                "expansion_epoch": 0,
                "device_context": {},
                "canvas_matches_panel": False,
                "progress": {},
                "frontier": {},
                "terminal": False,
                "reason": None,
                "recent_events": [],
            }
            self._runs[normalized_id] = _RunChannel(snapshot=snapshot)
            return copy.deepcopy(snapshot)

    def publish(self, run_id: int, event_type: str, **patch: Any) -> Dict[str, Any]:
        """Publish a safe complete snapshot without blocking slow subscribers."""
        normalized_id = int(run_id)
        normalized_event = _short_text(event_type, 60) or "UPDATE"
        with self._lock:
            self._cleanup_locked()
            channel = self._runs.get(normalized_id)
            if channel is None:
                self.start_run(
                    normalized_id,
                    str(patch.get("device_serial") or ""),
                    str(patch.get("run_status") or patch.get("status") or "RUNNING"),
                )
                channel = self._runs[normalized_id]

            snapshot = dict(channel.snapshot)
            snapshot["revision"] = int(snapshot.get("revision") or 0) + 1
            snapshot["event_type"] = normalized_event
            snapshot["emitted_at"] = _utc_now_iso()

            # Frontier events are deliberately display-neutral.  Enqueueing a
            # newly observed child must never replace the logical State whose
            # actions are currently being expanded, even if a future caller
            # accidentally supplies page/action fields here.
            display_neutral = normalized_event == "FRONTIER_UPDATED"

            for key, limit in (
                ("branch_key", 100),
                ("phase", 80),
                ("current_stage", _MAX_STAGE_LENGTH),
                ("run_status", 40),
                ("reason", _MAX_REASON_LENGTH),
            ):
                source_key = key
                if key == "run_status" and key not in patch and "status" in patch:
                    source_key = "status"
                if source_key in patch:
                    snapshot[key] = _short_text(patch.get(source_key), limit)
            if "expansion_owner_state_id" in patch and not display_neutral:
                snapshot["expansion_owner_state_id"] = _positive_int(
                    patch.get("expansion_owner_state_id")
                )
            if "expansion_epoch" in patch and not display_neutral:
                snapshot["expansion_epoch"] = (
                    _positive_int(patch.get("expansion_epoch")) or 0
                )
            if "page" in patch and not display_neutral:
                snapshot["page"] = _sanitize_page(
                    patch.get("page"),
                    expected_run_id=normalized_id,
                )
            if "actions" in patch and not display_neutral:
                snapshot["actions"] = _sanitize_actions(patch.get("actions"))
            if (
                ("action" in patch or "current_action" in patch)
                and not display_neutral
            ):
                snapshot["current_action"] = _sanitize_action(
                    patch.get("current_action", patch.get("action"))
                )
            if "action_panel" in patch and not display_neutral:
                snapshot["action_panel"] = _sanitize_action_panel(
                    patch.get("action_panel"),
                    expected_run_id=normalized_id,
                )
            if "device_context" in patch and not display_neutral:
                snapshot["device_context"] = _sanitize_device_context(
                    patch.get("device_context"),
                    expected_run_id=normalized_id,
                )
            if "canvas_matches_panel" in patch and not display_neutral:
                snapshot["canvas_matches_panel"] = bool(
                    patch.get("canvas_matches_panel")
                )
            if "progress" in patch:
                snapshot["progress"] = _sanitize_progress(patch.get("progress"))
            if "frontier" in patch:
                snapshot["frontier"] = _sanitize_frontier(patch.get("frontier"))
            if "overlay_visible" in patch and not display_neutral:
                snapshot["overlay_visible"] = bool(patch.get("overlay_visible"))
            if normalized_event == "OVERLAY_CLEAR":
                snapshot["overlay_visible"] = False
                snapshot["current_action"] = None
            if "terminal" in patch:
                snapshot["terminal"] = bool(patch.get("terminal"))

            panel = snapshot.get("action_panel")
            owner_state_id = snapshot.get("expansion_owner_state_id")
            page_state_id = (snapshot.get("page") or {}).get("state_id")
            panel_patch_present = any(
                key in patch for key in ("page", "actions", "action", "current_action")
            )
            if (
                not display_neutral
                and owner_state_id is not None
                and (page_state_id is None or page_state_id == owner_state_id)
                and (panel_patch_present or panel is None)
            ):
                mirrored = dict(panel or {})
                mirrored.update(
                    {
                        "state_id": owner_state_id,
                        "expansion_epoch": int(snapshot.get("expansion_epoch") or 0),
                        "page": copy.deepcopy(snapshot.get("page")),
                        "actions": copy.deepcopy(snapshot.get("actions") or []),
                        "current_action": copy.deepcopy(
                            snapshot.get("current_action")
                        ),
                        "canvas_matches_panel": bool(
                            snapshot.get("canvas_matches_panel")
                        ),
                    }
                )
                snapshot["action_panel"] = mirrored
            elif isinstance(panel, Mapping) and not display_neutral:
                mirrored = dict(panel)
                if "canvas_matches_panel" in patch:
                    mirrored["canvas_matches_panel"] = bool(
                        snapshot.get("canvas_matches_panel")
                    )
                snapshot["action_panel"] = mirrored

            event: Dict[str, Any] = {
                "revision": snapshot["revision"],
                "type": normalized_event,
                "at": snapshot["emitted_at"],
            }
            for key in ("branch_key", "phase", "current_stage", "run_status"):
                if snapshot.get(key) is not None:
                    event[key] = snapshot[key]
            page = snapshot.get("page") or {}
            for key in ("state_id", "page_visit_id"):
                if key in page:
                    event[key] = page[key]
            current_action = snapshot.get("current_action") or {}
            for key in (
                "action_id",
                "action_key",
                "action_type",
                "page_order",
                "display_order",
                "sequence",
                "global_sequence",
                "status",
                "label",
            ):
                if key in current_action:
                    event[key] = current_action[key]
            channel.recent_events.append(event)
            snapshot["recent_events"] = list(channel.recent_events)
            channel.snapshot = snapshot
            subscribers = tuple(channel.subscribers)
            result = copy.deepcopy(snapshot)

        for subscriber in subscribers:
            subscriber._offer(copy.deepcopy(result))
        return result

    def snapshot(self, run_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            self._cleanup_locked()
            channel = self._runs.get(int(run_id))
            return copy.deepcopy(channel.snapshot) if channel is not None else None

    def finish_run(
        self,
        run_id: int,
        status: str,
        stage: str,
        reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        result = self.publish(
            run_id,
            "TERMINAL",
            run_status=status,
            current_stage=stage,
            reason=reason,
            overlay_visible=False,
            canvas_matches_panel=False,
            device_context={
                "phase": "complete",
                "canvas_matches_panel": False,
            },
            terminal=True,
        )
        with self._lock:
            channel = self._runs.get(int(run_id))
            if channel is not None:
                channel.terminal_expires_at = (
                    self._clock() + self.terminal_retention_seconds
                )
        return result

    def subscribe(self, run_id: int) -> LiveSubscription:
        with self._lock:
            self._cleanup_locked()
            channel = self._runs.get(int(run_id))
            if channel is None:
                raise KeyError(f"inspection live run not found: {run_id}")
            subscriber = LiveSubscription(self, int(run_id))
            channel.subscribers.add(subscriber)
            # Seed while holding the registry lock. This closes the
            # snapshot-then-subscribe race; later publications still replace
            # this single queued snapshot for a slow client.
            subscriber._offer(copy.deepcopy(channel.snapshot))
            return subscriber

    def unsubscribe(self, subscriber: LiveSubscription) -> None:
        with self._lock:
            channel = self._runs.get(subscriber.run_id)
            if channel is not None:
                channel.subscribers.discard(subscriber)
            subscriber._mark_closed()

    def create_live_session(self, run_id: int, user_id: int) -> Dict[str, Any]:
        """Issue one-use event/video tickets for one viewer session."""
        normalized_run_id = int(run_id)
        normalized_user_id = int(user_id)
        with self._lock:
            self._cleanup_locked()
            active_count = sum(
                1
                for item in self._sessions.values()
                if item.run_id == normalized_run_id
            )
            if active_count >= self.max_sessions_per_run:
                raise RuntimeError("实时巡检观看人数已达上限")

            session_id = secrets.token_urlsafe(18)
            expires_at = self._clock() + self.ticket_ttl_seconds
            viewer = _ViewerSession(
                session_id=session_id,
                run_id=normalized_run_id,
                user_id=normalized_user_id,
                expires_at=expires_at,
            )
            self._sessions[session_id] = viewer
            tokens: Dict[str, str] = {}
            for kind in ("event", "video"):
                token = secrets.token_urlsafe(32)
                self._tickets[self._ticket_digest(token)] = _Ticket(
                    session_id=session_id,
                    run_id=normalized_run_id,
                    user_id=normalized_user_id,
                    kind=kind,
                    expires_at=expires_at,
                )
                tokens[kind] = token

        return {
            "session_id": session_id,
            "event_ticket": tokens["event"],
            "video_ticket": tokens["video"],
            "expires_in": int(self.ticket_ttl_seconds),
            "expires_at": (
                datetime.now(timezone.utc)
                + timedelta(seconds=self.ticket_ttl_seconds)
            ).isoformat(),
        }

    def consume_ticket(self, token: str, *, run_id: int, kind: str) -> TicketClaim:
        normalized_kind = str(kind).strip().lower()
        if normalized_kind not in {"event", "video"}:
            raise ValueError("invalid live ticket kind")
        digest = self._ticket_digest(token)
        with self._lock:
            self._cleanup_locked()
            # Pop first: a known ticket is one-use even when presented to the
            # wrong run or channel.
            ticket = self._tickets.pop(digest, None)
            if ticket is None:
                raise ValueError("live ticket is invalid, expired, or already used")
            viewer = self._sessions.get(ticket.session_id)
            if viewer is not None:
                viewer.pending_kinds.discard(ticket.kind)
            if ticket.run_id != int(run_id) or ticket.kind != normalized_kind:
                raise ValueError("live ticket does not match this run or channel")
            if ticket.expires_at <= self._clock() or viewer is None:
                raise ValueError("live ticket is invalid, expired, or already used")
            viewer.active_kinds.add(normalized_kind)
            return TicketClaim(
                session_id=ticket.session_id,
                run_id=ticket.run_id,
                user_id=ticket.user_id,
                kind=ticket.kind,
            )

    def release_channel(self, session_id: str, kind: str) -> None:
        with self._lock:
            self._cleanup_locked()
            viewer = self._sessions.get(str(session_id))
            if viewer is None:
                return
            viewer.active_kinds.discard(str(kind).strip().lower())
            if not viewer.active_kinds and not viewer.pending_kinds:
                self._sessions.pop(viewer.session_id, None)

    def active_session_count(self, run_id: int) -> int:
        with self._lock:
            self._cleanup_locked()
            return sum(
                1 for item in self._sessions.values() if item.run_id == int(run_id)
            )

    def clear(self) -> None:
        """Clear process-local state. Intended for orderly shutdown and tests."""
        with self._lock:
            for channel in self._runs.values():
                for subscriber in tuple(channel.subscribers):
                    subscriber._mark_closed()
            self._runs.clear()
            self._tickets.clear()
            self._sessions.clear()


inspection_live_registry = InspectionLiveRegistry()
