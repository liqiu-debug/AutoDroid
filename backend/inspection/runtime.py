"""Per-run abort registry for inspection tasks."""
from __future__ import annotations

import threading
from typing import Dict, Optional


_RUN_ABORT_EVENTS: Dict[int, threading.Event] = {}
_RUN_ABORT_LOCK = threading.Lock()


def abort_event_for_run(run_id: int) -> threading.Event:
    with _RUN_ABORT_LOCK:
        event = _RUN_ABORT_EVENTS.get(int(run_id))
        if event is None:
            event = threading.Event()
            _RUN_ABORT_EVENTS[int(run_id)] = event
        return event


def get_abort_event(run_id: int) -> Optional[threading.Event]:
    with _RUN_ABORT_LOCK:
        return _RUN_ABORT_EVENTS.get(int(run_id))


def discard_abort_event(run_id: int) -> None:
    with _RUN_ABORT_LOCK:
        _RUN_ABORT_EVENTS.pop(int(run_id), None)


def request_abort(run_id: int) -> bool:
    event = abort_event_for_run(run_id)
    already_set = event.is_set()
    event.set()
    return not already_set
