"""Lifecycle preparation is excluded from every timed startup sample."""
from __future__ import annotations

import asyncio
import copy
import inspect
import logging
import time
from datetime import datetime

from .contract import MEASUREMENT_VERSION, normalize_config
from .metrics import METRIC_METADATA, compute_startup_aggregate, metric_threshold, numeric_ms
from .runtime import StartupCancelled, StartupInvalid, check_cancel, interruptible_sleep, wait_with_cancel

logger = logging.getLogger(__name__)
READY_POLL_SECONDS = 0.2


async def _observe_ready(adapter, options, started, stop_event):
    if not options.get("enabled"):
        return {"ready_status": "DISABLED", "ready_ms": None, "ready_error": "", "ready_query_count": 0}
    deadline = started + float(options.get("timeout_sec", 10))
    queries = 0
    try:
        while time.monotonic() < deadline:
            check_cancel(stop_event)
            queries += 1
            # Home/old-page text cannot satisfy the target application's ready
            # condition. Every probe is a fresh, exact locator query.
            found = await adapter.is_foreground() and await adapter.ready_exists(options)
            observed = time.monotonic()
            if found and observed <= deadline:
                return {"ready_status": "FOUND", "ready_ms": int(round((observed - started) * 1000)),
                        "ready_error": "", "ready_query_count": queries}
            await interruptible_sleep(min(READY_POLL_SECONDS, max(0, deadline - observed)), stop_event)
        return {"ready_status": "TIMEOUT", "ready_ms": None, "ready_error": "首页就绪检查超时",
                "ready_query_count": queries}
    except StartupCancelled:
        raise
    except Exception as exc:
        return {"ready_status": "ERROR", "ready_ms": None, "ready_error": str(exc), "ready_query_count": queries}


def _blank_run(adapter, mode, iteration):
    return {
        "mode": mode, "requested_mode": mode, "actual_mode": "unknown", "iteration": iteration,
        "platform": adapter.platform, "measurement_version": MEASUREMENT_VERSION,
        "diagnostic": False, "completed": True, "sample_valid": False, "invalid_reason": "",
        "launch_success": False, "success": False, "status": "FAIL", "automation_ms": None,
        "this_time_ms": None, "total_time_ms": None, "wait_time_ms": None, "ready_ms": None,
        "ready_status": "DISABLED", "ready_error": "", "ready_query_count": 0,
        "performance_passed": None, "state_before": None, "state_after": None,
        "metric_sources": {key: value["source"] for key, value in METRIC_METADATA[adapter.platform].items()},
        "crash_events": [], "error": "", "time": datetime.now().isoformat(),
    }


async def _measure(adapter, config, run, stop_event):
    prepared = await adapter.prepare(run["mode"], config, stop_event)
    run.update(prepared or {})
    check_cancel(stop_event)
    if hasattr(adapter, "before_sample"):
        await adapter.before_sample(run, config, stop_event)
    if hasattr(adapter, "validate_before_launch"):
        await adapter.validate_before_launch(run, config, stop_event)
    started = time.monotonic()
    launch_task = asyncio.create_task(adapter.launch(run["mode"], config, stop_event))
    ready_task = asyncio.create_task(_observe_ready(adapter, config["ready_check"], started, stop_event))
    try:
        launched = await wait_with_cancel(launch_task, stop_event)
        finished_at = (launched or {}).pop("_automation_finished_at", time.monotonic())
        run.update(launched or {})
        run["automation_ms"] = int(round((finished_at - started) * 1000))
        if run.get("launch_success", True):
            run.update(await wait_with_cancel(ready_task, stop_event))
        else:
            ready_task.cancel()
            await asyncio.gather(ready_task, return_exceptions=True)
            if config["ready_check"]["enabled"]:
                run["ready_status"] = "NOT_CHECKED"
                run["ready_error"] = "启动失败，未完成首页就绪检查"
    finally:
        for pending in (launch_task, ready_task):
            if not pending.done():
                pending.cancel()
        await asyncio.gather(launch_task, ready_task, return_exceptions=True)

    ready_ok = not config["ready_check"]["enabled"] or run["ready_status"] == "FOUND"
    run["success"] = bool(run["launch_success"] and ready_ok and run["sample_valid"])
    if not run["launch_success"] or not ready_ok:
        run["status"] = "FAIL"
        run["error"] = run.get("error") or run.get("ready_error") or "应用启动失败"
    elif not run["sample_valid"]:
        run["status"] = "INVALID"
    else:
        run["status"] = "PASS"
    metric = config["thresholds"]["metric"]
    value = numeric_ms(run.get(metric))
    limit = metric_threshold(config["thresholds"], run["mode"], metric)
    if run["sample_valid"] and run["launch_success"] and value is not None and limit is not None:
        run["performance_passed"] = value <= limit
    run["metric"] = metric
    run["metric_value_ms"] = value


async def run_startup(adapter, config, *, stop_event=None, progress=None):
    """Return the existing Fastbot six-column report, including partial results.

    progress receives a detached, complete report snapshot. The caller owns
    device leasing, persistence and transport retries. The adapter may already
    have been preflighted by the submitting route; preflight is idempotent.
    """
    config = normalize_config(config, platform=adapter.platform, device_serial=adapter.device_serial)
    report = {"performance_data": [], "jank_data": [], "jank_events": [],
              "trace_artifacts": [], "crash_events": [], "summary": {}}
    runs = []
    requested = len(config["startup_modes"]) * config["iterations"]
    metadata = {}
    status = "RUNNING"
    error = ""
    phase = "preflight"
    current_mode = None
    current_iteration = None

    async def emit():
        completed = [run for run in runs if run.get("completed")]
        aggregate = compute_startup_aggregate(runs, config["thresholds"], platform=adapter.platform)
        success_count = sum(bool(run.get("success")) for run in completed)
        valid_count = sum(bool(run.get("sample_valid")) for run in completed)
        progress_data = {
            "requested_count": requested, "completed_count": len(completed), "valid_count": valid_count,
            "invalid_count": sum(run.get("status") == "INVALID" for run in completed),
            "phase": phase, "current_mode": current_mode, "current_iteration": current_iteration,
        }
        summary = {
            "session_type": "startup", "session_label": "冷热启动测试", "platform": adapter.platform,
            "measurement_version": MEASUREMENT_VERSION, "status": status,
            "startup_config": config, "startup_runs": runs, "startup_aggregate": aggregate,
            "metrics_metadata": METRIC_METADATA[adapter.platform], "measurement_environment": metadata,
            **progress_data, "progress": progress_data,
            "success_count": success_count,
            "fail_count": sum(run.get("status") == "FAIL" for run in completed),
            "aborted_count": sum(run.get("status") == "ABORTED" for run in runs),
            "launch_success_count": sum(bool(run.get("launch_success")) for run in completed),
            "ready_success_count": sum(run.get("ready_status") == "FOUND" for run in completed),
            "ready_failure_count": sum(run.get("ready_status") in {"TIMEOUT", "ERROR"} for run in completed),
            "success_rate": round(success_count / len(completed), 4) if completed else 0,
            "slow_count": sum(item["slow_count"] for item in aggregate.values()),
            "slow_events": [dict(run, threshold_ms=metric_threshold(config["thresholds"], run["mode"], config["thresholds"]["metric"]))
                            for run in completed if run.get("performance_passed") is False],
            "trace_artifact_count": len(report["trace_artifacts"]),
            "total_crashes": sum(event.get("type") == "CRASH" for event in report["crash_events"]),
            "total_anrs": sum(event.get("type") == "ANR" for event in report["crash_events"]),
            "performance_monitor_enabled": False, "jank_frame_monitor_enabled": False,
            "local_replay_enabled": False, "error": error,
        }
        report["summary"] = summary
        if progress:
            try:
                returned = progress(copy.deepcopy(report))
                if inspect.isawaitable(returned):
                    await returned
            except StartupCancelled:
                raise
            except Exception as exc:
                # Progress transport failures must not erase a measured sample.
                # Execution-node callbacks persist locally before attempting I/O.
                logger.warning("启动测试进度回调失败: %s", exc)

    try:
        check_cancel(stop_event)
        metadata = await adapter.preflight(config) or {}
        if metadata.get("resolved_component"):
            config["resolved_component"] = metadata["resolved_component"]
        await emit()
        for mode in config["startup_modes"]:
            for iteration in range(1, config["iterations"] + 1):
                check_cancel(stop_event)
                current_mode, current_iteration, phase = mode, iteration, "preparing"
                await emit()
                run = _blank_run(adapter, mode, iteration)
                if config["ready_check"]["enabled"]:
                    run["ready_status"] = "NOT_CHECKED"
                try:
                    phase = "measuring"
                    await _measure(adapter, config, run, stop_event)
                except StartupInvalid as exc:
                    run.update(status="INVALID", sample_valid=False, invalid_reason=str(exc), error=str(exc))
                except (StartupCancelled, asyncio.CancelledError):
                    run.update(status="ABORTED", completed=False, error="本轮采样被中断，不纳入统计")
                    runs.append(run)
                    if hasattr(adapter, "collect"):
                        try:
                            evidence = await adapter.collect(run, config) or {}
                            report["trace_artifacts"].extend(evidence.get("trace_artifacts") or [])
                        except Exception as exc:
                            run["evidence_error"] = str(exc)
                    raise StartupCancelled("启动测试已取消")
                except Exception as exc:
                    run.update(status="FAIL", error=str(exc))
                if hasattr(adapter, "collect"):
                    try:
                        evidence = await adapter.collect(run, config) or {}
                        report["trace_artifacts"].extend(evidence.get("trace_artifacts") or [])
                        run.update({key: value for key, value in evidence.items() if key != "trace_artifacts"})
                    except Exception as exc:
                        run["evidence_error"] = str(exc)
                if run.get("crash_events"):
                    run.update(success=False, status="FAIL", error=run.get("error") or "采样期间应用发生崩溃或 ANR")
                report["crash_events"].extend(run.get("crash_events") or [])
                runs.append(run)
                phase = "sampling"
                await emit()
        status = "FAILED" if any(run["status"] in {"FAIL", "INVALID"} for run in runs) else "COMPLETED"
    except (StartupCancelled, asyncio.CancelledError):
        status, error = "ABORTED", "启动测试已取消，已完成轮次保留"
    except Exception as exc:
        status, error = "FAILED", str(exc)
    finally:
        if hasattr(adapter, "close"):
            try:
                await adapter.close()
            except Exception as exc:
                metadata["cleanup_error"] = str(exc)
        phase = "finished"
        await emit()
    return report
