"""In-memory device transport metrics for wireless ADB and Agent tunnel diagnostics."""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Deque, Dict, Optional


_DURATION_SAMPLES = 120


class DeviceTransportMetrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._devices: Dict[str, Dict[str, Dict[str, Any]]] = {}

    def record(
        self,
        serial: str,
        operation: str,
        duration_seconds: float = 0.0,
        *,
        success: bool = True,
        timed_out: bool = False,
        byte_count: int = 0,
        dropped: int = 0,
        queue_depth: Optional[int] = None,
    ) -> None:
        key = str(serial or "").strip()
        if not key:
            return
        name = str(operation or "other").strip() or "other"
        duration_ms = max(0.0, float(duration_seconds or 0.0) * 1000.0)
        with self._lock:
            operations = self._devices.setdefault(key, {})
            metric = operations.get(name)
            if metric is None:
                metric = {
                    "count": 0,
                    "failures": 0,
                    "timeouts": 0,
                    "bytes": 0,
                    "dropped": 0,
                    "max_queue_depth": 0,
                    "durations_ms": deque(maxlen=_DURATION_SAMPLES),
                    "updated_at": 0.0,
                }
                operations[name] = metric
            metric["count"] += 1
            if not success:
                metric["failures"] += 1
            if timed_out:
                metric["timeouts"] += 1
            metric["bytes"] += max(0, int(byte_count or 0))
            metric["dropped"] += max(0, int(dropped or 0))
            if queue_depth is not None:
                metric["max_queue_depth"] = max(metric["max_queue_depth"], max(0, int(queue_depth)))
            if duration_ms > 0:
                metric["durations_ms"].append(duration_ms)
            metric["updated_at"] = time.time()

    def snapshot(self, serial: str) -> Dict[str, Any]:
        key = str(serial or "").strip()
        with self._lock:
            operations = self._devices.get(key, {})
            result: Dict[str, Any] = {}
            for name, metric in operations.items():
                samples: Deque[float] = metric["durations_ms"]
                ordered = sorted(samples)
                result[name] = {
                    "count": metric["count"],
                    "failures": metric["failures"],
                    "timeouts": metric["timeouts"],
                    "bytes": metric["bytes"],
                    "dropped": metric["dropped"],
                    "max_queue_depth": metric["max_queue_depth"],
                    "sample_count": len(ordered),
                    "p50_ms": self._percentile(ordered, 0.50),
                    "p95_ms": self._percentile(ordered, 0.95),
                    "updated_at": metric["updated_at"],
                }
        return {"serial": key, "operations": result}

    @staticmethod
    def _percentile(samples: list[float], percentile: float) -> Optional[float]:
        if not samples:
            return None
        index = min(len(samples) - 1, max(0, int((len(samples) - 1) * percentile)))
        return round(samples[index], 2)


device_transport_metrics = DeviceTransportMetrics()
