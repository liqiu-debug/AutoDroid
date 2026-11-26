"""Metric-specific valid samples and versioned startup report aggregation."""
from __future__ import annotations

import math
from typing import Any, Dict, List

from .contract import MEASUREMENT_VERSION, METRICS, MIN_PERCENTILE_SAMPLES

METRIC_METADATA = {
    "android": {
        "total_time_ms": {"label": "Android 启动 TotalTime", "source": "android.am_start_w", "unit": "ms",
                          "description": "ActivityManager am start -W 的 TotalTime；不是业务首页就绪或 TTFD。"},
        "automation_ms": {"label": "启动/恢复控制耗时", "source": "host.monotonic.adb", "unit": "ms",
                          "description": "发起 ADB 启动至命令返回并确认前台，包含控制及状态查询开销。"},
        "ready_ms": {"label": "业务就绪耗时", "source": "host.monotonic.android_locator", "unit": "ms",
                     "description": "发起启动至首次观察到目标 App 前台且就绪元素命中，包含查询与采样开销。"},
    },
    "ios": {
        "automation_ms": {"label": "启动/恢复控制耗时", "source": "host.monotonic.wda", "unit": "ms",
                          "description": "发起 WDA launch/activate 至返回并确认前台，包含 WDA 控制和状态查询开销；不是系统首帧。"},
        "ready_ms": {"label": "业务就绪耗时", "source": "host.monotonic.ios_locator", "unit": "ms",
                     "description": "发起启动/恢复至首次观察到前台且就绪元素命中，包含 WDA 查询与采样开销；不是 XCTest 启动指标。"},
    },
}


def numeric_ms(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def percentile(values: List[float], fraction: float):
    if not values:
        return None
    ordered = sorted(values)
    rank = (len(ordered) - 1) * fraction
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)


def _rounded(value):
    return None if value is None else int(round(value))


def metric_threshold(thresholds: Dict[str, Any], mode: str, metric: str):
    if metric == thresholds.get("metric"):
        return thresholds.get(f"{mode}_ms")
    if metric == "ready_ms":
        return thresholds.get(f"ready_{mode}_ms")
    return None


def metric_stats(values: List[float], threshold=None):
    return {
        "count": len(values),
        "min_ms": _rounded(min(values)) if values else None,
        "avg_ms": _rounded(sum(values) / len(values)) if values else None,
        "p50_ms": _rounded(percentile(values, 0.5)),
        "p90_ms": _rounded(percentile(values, 0.9)),
        "p95_ms": _rounded(percentile(values, 0.95)),
        "max_ms": _rounded(max(values)) if values else None,
        "threshold_ms": threshold,
        "slow_count": sum(value > threshold for value in values) if threshold is not None else 0,
        "samples_sufficient": len(values) >= MIN_PERCENTILE_SAMPLES,
        "recommended_samples": MIN_PERCENTILE_SAMPLES,
    }


def compute_startup_aggregate(runs: List[Dict[str, Any]], thresholds: Dict[str, Any], *, platform: str):
    eligible = [run for run in runs if not run.get("diagnostic")
                and run.get("completed", True)
                and run.get("platform", platform) == platform
                and run.get("measurement_version", MEASUREMENT_VERSION) == MEASUREMENT_VERSION]
    aggregate = {}
    for mode in ("cold", "hot"):
        mode_runs = [run for run in eligible if run.get("requested_mode", run.get("mode")) == mode]
        valid = [run for run in mode_runs if run.get("sample_valid")]
        measured = [run for run in valid if run.get("launch_success")]
        metrics = {}
        for metric in METRICS[platform]:
            candidates = [run for run in measured if metric != "ready_ms" or run.get("ready_status") == "FOUND"]
            values = [value for run in candidates if (value := numeric_ms(run.get(metric))) is not None]
            metrics[metric] = metric_stats(values, metric_threshold(thresholds, mode, metric))
        native = metrics.get("total_time_ms", metric_stats([]))
        ready = metrics.get("ready_ms", metric_stats([]))
        primary = metrics.get(thresholds.get("metric"), metric_stats([]))
        aggregate[mode] = {
            "count": len(mode_runs), "completed_count": len(mode_runs),
            "valid_count": len(valid), "invalid_count": sum(run.get("status") == "INVALID" for run in mode_runs),
            "success_count": sum(bool(run.get("success")) for run in mode_runs),
            "fail_count": sum(run.get("status") == "FAIL" for run in mode_runs),
            "metrics": metrics, "metric": thresholds.get("metric"),
            "threshold_ms": primary.get("threshold_ms"), "slow_count": primary["slow_count"],
            "performance_passed": None if not primary["count"] else primary["slow_count"] == 0,
            # Keep existing report fields as Android-native aliases. iOS never
            # fills these aliases with WDA request times.
            "min_ms": native["min_ms"], "avg_ms": native["avg_ms"], "median_ms": native["p50_ms"],
            "p90_ms": native["p90_ms"], "p95_ms": native["p95_ms"], "max_ms": native["max_ms"],
            "ready_min_ms": ready["min_ms"], "ready_median_ms": ready["p50_ms"],
            "ready_p90_ms": ready["p90_ms"], "ready_p95_ms": ready["p95_ms"], "ready_max_ms": ready["max_ms"],
        }
    return aggregate
