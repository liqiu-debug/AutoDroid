"""In-process execution registry for user-triggered runs."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence


RUNNING_STATUSES = {"RUNNING", "PENDING"}
TERMINAL_STATUSES = {"PASS", "WARNING", "FAIL", "ERROR", "ABORTED"}
ABORTED_STATUS = "ABORTED"


@dataclass
class RunRecord:
    run_id: str
    kind: str
    target_id: int
    batch_id: str
    device_serial: Optional[str]
    abort_event: threading.Event
    execution_id: Optional[int] = None
    status: str = "RUNNING"
    started_at: datetime = field(default_factory=datetime.now)
    cancel_requested_at: Optional[datetime] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "kind": self.kind,
            "target_id": self.target_id,
            "batch_id": self.batch_id,
            "device_serial": self.device_serial,
            "execution_id": self.execution_id,
            "status": self.status,
            "started_at": self.started_at.isoformat(),
            "cancel_requested_at": self.cancel_requested_at.isoformat()
            if self.cancel_requested_at
            else None,
            "metadata": dict(self.metadata or {}),
        }


class RunRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: Dict[str, RunRecord] = {}

    def register(
        self,
        *,
        kind: str,
        target_id: int,
        batch_id: Optional[str] = None,
        device_serial: Optional[str] = None,
        abort_event: Optional[threading.Event] = None,
        run_id: Optional[str] = None,
        execution_id: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> RunRecord:
        normalized_kind = str(kind or "").strip().lower()
        if normalized_kind not in {"case", "scenario"}:
            raise ValueError(f"unsupported run kind: {kind}")

        record = RunRecord(
            run_id=str(run_id or uuid.uuid4()),
            kind=normalized_kind,
            target_id=int(target_id),
            batch_id=str(batch_id or uuid.uuid4()),
            device_serial=str(device_serial).strip() if device_serial else None,
            abort_event=abort_event or threading.Event(),
            execution_id=execution_id,
            metadata=dict(metadata or {}),
        )
        with self._lock:
            self._runs[record.run_id] = record
        return record

    def complete(self, run_id: Optional[str], status: Optional[str] = None) -> None:
        if not run_id:
            return
        with self._lock:
            record = self._runs.get(str(run_id))
            if record and status:
                record.status = str(status).upper()
            self._runs.pop(str(run_id), None)

    def active(
        self,
        *,
        kind: Optional[str] = None,
        target_id: Optional[int] = None,
    ) -> List[RunRecord]:
        normalized_kind = str(kind or "").strip().lower() if kind else None
        with self._lock:
            records = list(self._runs.values())
        return [
            record
            for record in records
            if (not normalized_kind or record.kind == normalized_kind)
            and (target_id is None or record.target_id == int(target_id))
            and record.status in RUNNING_STATUSES
        ]

    def clear(self) -> None:
        with self._lock:
            self._runs.clear()

    def cancel(
        self,
        *,
        kind: Optional[str] = None,
        target_id: Optional[int] = None,
        batch_id: Optional[str] = None,
        run_ids: Optional[Sequence[str]] = None,
        execution_ids: Optional[Sequence[int]] = None,
        device_serials: Optional[Sequence[str]] = None,
    ) -> List[RunRecord]:
        records = self._match(
            kind=kind,
            target_id=target_id,
            batch_id=batch_id,
            run_ids=run_ids,
            execution_ids=execution_ids,
            device_serials=device_serials,
        )
        now = datetime.now()
        for record in records:
            record.status = ABORTED_STATUS
            record.cancel_requested_at = now
            record.abort_event.set()

        serials = sorted({record.device_serial for record in records if record.device_serial})
        if serials:
            try:
                from backend.runner import trigger_device_abort

                for serial in serials:
                    trigger_device_abort(serial)
            except Exception:
                pass

        return records

    def _match(
        self,
        *,
        kind: Optional[str],
        target_id: Optional[int],
        batch_id: Optional[str],
        run_ids: Optional[Sequence[str]],
        execution_ids: Optional[Sequence[int]],
        device_serials: Optional[Sequence[str]],
    ) -> List[RunRecord]:
        run_id_set = _string_set(run_ids)
        execution_id_set = _int_set(execution_ids)
        serial_set = _string_set(device_serials)
        normalized_kind = str(kind or "").strip().lower() if kind else None
        normalized_batch = str(batch_id or "").strip() if batch_id else None

        with self._lock:
            records = list(self._runs.values())

        matched: List[RunRecord] = []
        for record in records:
            if normalized_kind and record.kind != normalized_kind:
                continue
            if target_id is not None and record.target_id != int(target_id):
                continue
            if normalized_batch and record.batch_id != normalized_batch:
                continue
            if run_id_set and record.run_id not in run_id_set:
                continue
            if execution_id_set and record.execution_id not in execution_id_set:
                continue
            if serial_set and (record.device_serial or "") not in serial_set:
                continue
            if record.status not in RUNNING_STATUSES:
                continue
            matched.append(record)
        return matched


def _string_set(values: Optional[Iterable[Any]]) -> set:
    return {str(value).strip() for value in values or [] if str(value).strip()}


def _int_set(values: Optional[Iterable[Any]]) -> set:
    result = set()
    for value in values or []:
        try:
            result.add(int(value))
        except Exception:
            continue
    return result


registry = RunRegistry()
