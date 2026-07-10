"""冷热启动专项测试：am start -W 计时、就绪检查、慢启动 Perfetto 取证与聚合。"""
import os
import re
import shlex
import time
import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from backend.fastbot.adb import (
    ADB_MONITOR_COMMAND_TIMEOUT_SECONDS,
    _adb_shell,
    _adb_shell_result,
)
from backend.fastbot.logcat import ANR_PATTERN, _capture_logcat_snapshot
from backend.fastbot.perfetto import (
    _analyze_exported_traces,
    _build_trace_artifact,
    _detect_perfetto_support,
    _pull_perfetto_trace_to_local,
    _start_perfetto_ring_buffer,
    _stop_perfetto_ring_buffer,
)
from backend.fastbot.reporting import _build_fastbot_report_dir

logger = logging.getLogger("FastbotRunner")

AM_START_FIELD_PATTERN = re.compile(r"^\s*([A-Za-z]+):\s*(.*?)\s*$")
STARTUP_DISPLAYED_PATTERN = re.compile(
    r"\bDisplayed\s+([A-Za-z0-9_.$]+/[A-Za-z0-9_.$]+):\s*\+?([0-9sm.]+)",
    re.IGNORECASE,
)
STARTUP_FULLY_DRAWN_PATTERN = re.compile(
    r"\bFully drawn\s+([A-Za-z0-9_.$]+/[A-Za-z0-9_.$]+):\s*\+?([0-9sm.]+)",
    re.IGNORECASE,
)


def _parse_duration_token_to_ms(value: str) -> Optional[int]:
    text = str(value or "").strip().lstrip("+")
    if not text:
        return None
    if text.isdigit():
        return int(text)

    total_ms = 0
    matched = False
    seconds_match = re.search(r"(\d+(?:\.\d+)?)s", text)
    millis_match = re.search(r"(\d+)ms", text)
    if seconds_match:
        total_ms += int(float(seconds_match.group(1)) * 1000)
        matched = True
    if millis_match:
        total_ms += int(millis_match.group(1))
        matched = True
    return total_ms if matched else None


def _normalize_startup_component(package_name: str, activity_name: str) -> str:
    pkg = str(package_name or "").strip()
    activity = str(activity_name or "").strip()
    if not pkg:
        raise ValueError("package_name is required")
    if not activity:
        raise ValueError("activity_name is required")
    if "/" in activity:
        return activity
    return f"{pkg}/{activity}"


def _parse_resolved_activity(output: str, package_name: str) -> str:
    text = str(output or "")
    if not text.strip() or "No activity found" in text or "unable to resolve" in text.lower():
        return ""

    for raw_line in reversed(text.splitlines()):
        line = raw_line.strip()
        if not line or "/" not in line:
            continue
        candidate = line.split()[-1]
        if "/" not in candidate:
            continue
        if candidate.startswith(package_name) or candidate.startswith(".") or candidate.startswith("/"):
            return candidate
        if candidate.split("/", 1)[0] == package_name:
            return candidate
    return ""


async def _resolve_launcher_activity(device_serial: str, package_name: str) -> str:
    quoted_pkg = shlex.quote(package_name)
    output = await _adb_shell(
        device_serial,
        (
            "cmd package resolve-activity --brief "
            "-a android.intent.action.MAIN "
            "-c android.intent.category.LAUNCHER "
            f"{quoted_pkg}"
        ),
        timeout=ADB_MONITOR_COMMAND_TIMEOUT_SECONDS,
    )
    component = _parse_resolved_activity(output, package_name)
    if not component:
        raise RuntimeError(f"无法解析 {package_name} 的 Launcher Activity")
    return _normalize_startup_component(package_name, component)


def _parse_am_start_output(stdout: str, stderr: str = "", returncode: int = 0) -> Dict[str, Any]:
    raw_text = "\n".join(part for part in [stdout, stderr] if part).strip()
    result: Dict[str, Any] = {
        "raw_output": raw_text,
        "status": "",
        "launch_state": "",
        "activity": "",
        "this_time_ms": None,
        "total_time_ms": None,
        "wait_time_ms": None,
        "error": "",
    }

    for line in raw_text.splitlines():
        match = AM_START_FIELD_PATTERN.match(line)
        if not match:
            continue
        key = match.group(1).lower()
        value = match.group(2).strip()
        if key == "status":
            result["status"] = value
        elif key == "launchstate":
            result["launch_state"] = value
        elif key == "activity":
            result["activity"] = value
        elif key == "thistime":
            result["this_time_ms"] = _parse_duration_token_to_ms(value)
        elif key == "totaltime":
            result["total_time_ms"] = _parse_duration_token_to_ms(value)
        elif key == "waittime":
            result["wait_time_ms"] = _parse_duration_token_to_ms(value)

    lower_text = raw_text.lower()
    if returncode != 0:
        result["error"] = raw_text or f"am start failed with returncode={returncode}"
    elif "error:" in lower_text or "exception" in lower_text:
        result["error"] = raw_text
    elif result["total_time_ms"] is None:
        result["error"] = "am start -W 未返回 TotalTime"
    return result


def _extract_startup_logcat_timings(log_text: str, package_name: str) -> Dict[str, Any]:
    displayed = None
    fully_drawn = None
    for line in str(log_text or "").splitlines():
        displayed_match = STARTUP_DISPLAYED_PATTERN.search(line)
        if displayed_match and displayed_match.group(1).startswith(package_name):
            displayed = {
                "component": displayed_match.group(1),
                "time_ms": _parse_duration_token_to_ms(displayed_match.group(2)),
                "line": line.strip(),
            }
        fully_drawn_match = STARTUP_FULLY_DRAWN_PATTERN.search(line)
        if fully_drawn_match and fully_drawn_match.group(1).startswith(package_name):
            fully_drawn = {
                "component": fully_drawn_match.group(1),
                "time_ms": _parse_duration_token_to_ms(fully_drawn_match.group(2)),
                "line": line.strip(),
            }
    return {
        "displayed": displayed,
        "fully_drawn": fully_drawn,
    }


def _extract_startup_crash_events(log_text: str, package_name: str, capture_log: bool) -> List[Dict]:
    text = str(log_text or "")
    events: List[Dict] = []
    now = datetime.now().strftime("%H:%M:%S")

    if ANR_PATTERN.search(text) and package_name in text:
        events.append({
            "time": now,
            "type": "ANR",
            "full_log": text if capture_log else "",
        })

    if re.search(r"FATAL EXCEPTION", text, re.IGNORECASE) and package_name in text:
        events.append({
            "time": now,
            "type": "CRASH",
            "full_log": text if capture_log else "",
        })
    return events


def _wait_for_startup_ready_sync(
    device_serial: str,
    ready_check: Dict[str, Any],
) -> Dict[str, Any]:
    if not ready_check or not ready_check.get("enabled"):
        return {"status": "DISABLED", "error": ""}

    locator_type = str(ready_check.get("locator_type") or "text").strip().lower()
    locator_value = str(ready_check.get("locator_value") or "").strip()
    timeout_sec = max(1, int(ready_check.get("timeout_sec") or 10))
    if not locator_value:
        return {"status": "SKIPPED", "error": "未配置首页就绪 locator"}

    try:
        import uiautomator2 as u2

        device = u2.connect(device_serial)
        if locator_type in ("resource_id", "resourceid", "id"):
            exists = bool(device(resourceId=locator_value).exists(timeout=timeout_sec))
        elif locator_type == "description":
            exists = bool(device(description=locator_value).exists(timeout=timeout_sec))
        elif locator_type == "xpath":
            exists = bool(device.xpath(locator_value).wait(timeout=timeout_sec))
        else:
            element = device(text=locator_value)
            exists = bool(element.exists(timeout=timeout_sec))
            if not exists:
                exists = bool(device(textContains=locator_value).exists(timeout=1))
        return {"status": "FOUND" if exists else "TIMEOUT", "error": ""}
    except Exception as exc:
        return {"status": "ERROR", "error": str(exc)}


async def _wait_for_startup_ready(
    device_serial: str,
    ready_check: Dict[str, Any],
) -> Dict[str, Any]:
    return await asyncio.to_thread(_wait_for_startup_ready_sync, device_serial, ready_check)


def _startup_percentile(values: List[int], percentile: float) -> Optional[float]:
    ordered = sorted(int(value) for value in values if value is not None)
    if not ordered:
        return None
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (len(ordered) - 1) * percentile
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    weight = rank - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _round_optional_ms(value: Optional[float]) -> Optional[int]:
    if value is None:
        return None
    return int(round(value))


def _compute_startup_aggregate(
    runs: List[Dict[str, Any]],
    thresholds: Dict[str, int],
) -> Dict[str, Dict[str, Any]]:
    aggregate: Dict[str, Dict[str, Any]] = {}
    for mode in ("cold", "hot"):
        mode_runs = [run for run in runs if run.get("mode") == mode]
        success_runs = [run for run in mode_runs if run.get("success")]
        total_values = [
            int(run["total_time_ms"])
            for run in success_runs
            if run.get("total_time_ms") is not None
        ]
        ready_values = [
            int(run["ready_ms"])
            for run in success_runs
            if run.get("ready_ms") is not None
        ]
        threshold = int(thresholds.get(mode) or 0)
        aggregate[mode] = {
            "count": len(mode_runs),
            "success_count": len(success_runs),
            "fail_count": len(mode_runs) - len(success_runs),
            "min_ms": min(total_values) if total_values else None,
            "median_ms": _round_optional_ms(_startup_percentile(total_values, 0.5)),
            "p90_ms": _round_optional_ms(_startup_percentile(total_values, 0.9)),
            "p95_ms": _round_optional_ms(_startup_percentile(total_values, 0.95)),
            "max_ms": max(total_values) if total_values else None,
            "avg_ms": _round_optional_ms((sum(total_values) / len(total_values)) if total_values else None),
            "slow_count": sum(1 for value in total_values if threshold > 0 and value >= threshold),
            "ready_min_ms": min(ready_values) if ready_values else None,
            "ready_median_ms": _round_optional_ms(_startup_percentile(ready_values, 0.5)),
            "ready_p90_ms": _round_optional_ms(_startup_percentile(ready_values, 0.9)),
            "ready_max_ms": max(ready_values) if ready_values else None,
        }
    return aggregate


async def _run_single_startup_iteration(
    device_serial: str,
    package_name: str,
    component: str,
    mode: str,
    iteration: int,
    capture_log: bool,
    ready_check: Dict[str, Any],
    cooldown_sec: int,
    diagnostic: bool = False,
) -> Dict[str, Any]:
    quoted_pkg = shlex.quote(package_name)
    quoted_component = shlex.quote(component)

    if mode == "cold":
        await _adb_shell(
            device_serial,
            f"am force-stop {quoted_pkg}",
            timeout=ADB_MONITOR_COMMAND_TIMEOUT_SECONDS,
        )
    else:
        await _adb_shell(
            device_serial,
            (
                "am start "
                "-a android.intent.action.MAIN "
                "-c android.intent.category.LAUNCHER "
                f"-n {quoted_component} >/dev/null 2>&1"
            ),
            timeout=ADB_MONITOR_COMMAND_TIMEOUT_SECONDS,
        )
        await asyncio.sleep(1)
        await _adb_shell(
            device_serial,
            "input keyevent HOME",
            timeout=ADB_MONITOR_COMMAND_TIMEOUT_SECONDS,
        )

    if cooldown_sec > 0:
        await asyncio.sleep(cooldown_sec)

    await _adb_shell(
        device_serial,
        "logcat -c",
        timeout=ADB_MONITOR_COMMAND_TIMEOUT_SECONDS,
    )

    start_perf = time.perf_counter()
    result = await _adb_shell_result(
        device_serial,
        (
            "am start -W "
            "-a android.intent.action.MAIN "
            "-c android.intent.category.LAUNCHER "
            f"-n {quoted_component}"
        ),
        timeout=60,
    )
    parsed = _parse_am_start_output(
        str(result.get("stdout") or ""),
        str(result.get("stderr") or ""),
        int(result.get("returncode") or 0),
    )

    ready_result = {"status": "DISABLED", "error": ""}
    ready_ms = None
    if ready_check and ready_check.get("enabled"):
        ready_result = await _wait_for_startup_ready(device_serial, ready_check)
        if ready_result.get("status") == "FOUND":
            ready_ms = int(round((time.perf_counter() - start_perf) * 1000))

    log_snapshot = await _capture_logcat_snapshot(device_serial)
    log_timings = _extract_startup_logcat_timings(log_snapshot, package_name)
    crash_events = _extract_startup_crash_events(log_snapshot, package_name, capture_log)

    success = not parsed.get("error")
    error = str(parsed.get("error") or "")
    if success and ready_result.get("status") == "ERROR":
        logger.warning("启动首页就绪检查异常: %s", ready_result.get("error"))

    return {
        "mode": mode,
        "iteration": iteration,
        "diagnostic": diagnostic,
        "success": success,
        "status": "PASS" if success else "FAIL",
        "activity": parsed.get("activity") or component,
        "launch_state": parsed.get("launch_state") or "",
        "this_time_ms": parsed.get("this_time_ms"),
        "total_time_ms": parsed.get("total_time_ms"),
        "wait_time_ms": parsed.get("wait_time_ms"),
        "ready_status": ready_result.get("status"),
        "ready_ms": ready_ms,
        "ready_error": ready_result.get("error") or "",
        "displayed": log_timings.get("displayed"),
        "fully_drawn": log_timings.get("fully_drawn"),
        "crash_events": crash_events,
        "error": error,
        "raw_output": parsed.get("raw_output") or "",
        "time": datetime.now().strftime("%H:%M:%S"),
    }


async def _capture_startup_perfetto_trace(
    device_serial: str,
    package_name: str,
    component: str,
    mode: str,
    ready_check: Dict[str, Any],
    cooldown_sec: int,
    report_dir: str,
    trace_artifacts: List[Dict],
) -> Dict[str, Any]:
    event: Dict[str, Any] = {
        "mode": mode,
        "trigger_time": datetime.now().strftime("%H:%M:%S"),
        "trace_exported": False,
        "trace_path": "",
        "diagnosis_status": "UNAVAILABLE",
    }

    perfetto_state = await _detect_perfetto_support(device_serial, report_dir)
    perfetto_state.capture_mode = "diagnostic"
    if not perfetto_state.available:
        event["trace_error"] = "设备不支持 Perfetto 或 Android 版本过低"
        return event

    started = await _start_perfetto_ring_buffer(device_serial, package_name, perfetto_state)
    if not started:
        event["trace_error"] = perfetto_state.last_error or "Perfetto 启动失败"
        return event

    try:
        diagnostic_run = await _run_single_startup_iteration(
            device_serial=device_serial,
            package_name=package_name,
            component=component,
            mode=mode,
            iteration=0,
            capture_log=False,
            ready_check=ready_check,
            cooldown_sec=cooldown_sec,
            diagnostic=True,
        )
        event["diagnostic_run"] = diagnostic_run
        await asyncio.sleep(2)
        await _stop_perfetto_ring_buffer(device_serial, perfetto_state, preserve_trace=True)
        local_trace_path = os.path.join(
            report_dir,
            f"startup_trace_{mode}_{perfetto_state.session_index:03d}.perfetto-trace",
        )
        pulled = await _pull_perfetto_trace_to_local(
            device_serial,
            perfetto_state,
            local_trace_path,
            "拉取冷热启动 Perfetto trace ",
        )
        if pulled:
            artifact = _build_trace_artifact(
                local_trace_path,
                perfetto_state,
                trigger_time=str(event["trigger_time"]),
                trigger_reason=f"STARTUP_SLOW_{mode.upper()}",
            )
            artifact["startup_mode"] = mode
            artifact["startup_total_time_ms"] = diagnostic_run.get("total_time_ms")
            artifact["capture_window_sec"] = 0
            trace_artifacts.append(artifact)
            event["trace_exported"] = True
            event["trace_path"] = artifact["path"]
            event["diagnosis_status"] = "PENDING"
        else:
            event["trace_error"] = perfetto_state.last_error or "Trace 拉取失败"
            event["diagnosis_status"] = "EXPORT_FAILED"
    except Exception as exc:
        event["trace_error"] = str(exc)
        event["diagnosis_status"] = "EXPORT_FAILED"
        try:
            await _stop_perfetto_ring_buffer(device_serial, perfetto_state, preserve_trace=False)
        except Exception:
            pass
    return event


async def run_startup_task(
    device_serial: str,
    package_name: str,
    activity_name: Optional[str] = None,
    startup_modes: Optional[List[str]] = None,
    iterations: int = 3,
    cooldown_sec: int = 3,
    capture_log: bool = True,
    ready_check: Optional[Dict[str, Any]] = None,
    perfetto_slow_trace: Optional[Dict[str, Any]] = None,
    task_id: Optional[int] = None,
) -> Dict[str, Any]:
    modes = [mode for mode in (startup_modes or ["cold", "hot"]) if mode in ("cold", "hot")]
    if not modes:
        modes = ["cold", "hot"]
    iterations = max(1, int(iterations or 1))
    cooldown_sec = max(0, int(cooldown_sec or 0))
    ready_options = dict(ready_check or {})
    slow_trace_options = dict(perfetto_slow_trace or {})
    thresholds = {
        "cold": int(slow_trace_options.get("cold_threshold_ms") or 5000),
        "hot": int(slow_trace_options.get("hot_threshold_ms") or 1500),
    }
    enable_slow_trace = bool(slow_trace_options.get("enabled", True))

    component = _normalize_startup_component(package_name, activity_name) if activity_name else await _resolve_launcher_activity(device_serial, package_name)
    report_dir = _build_fastbot_report_dir(task_id)
    startup_runs: List[Dict[str, Any]] = []
    slow_events: List[Dict[str, Any]] = []
    trace_artifacts: List[Dict] = []
    crash_events: List[Dict] = []
    traced_modes = set()

    for mode in modes:
        for iteration in range(1, iterations + 1):
            run = await _run_single_startup_iteration(
                device_serial=device_serial,
                package_name=package_name,
                component=component,
                mode=mode,
                iteration=iteration,
                capture_log=capture_log,
                ready_check=ready_options,
                cooldown_sec=cooldown_sec,
            )
            startup_runs.append(run)
            crash_events.extend(run.get("crash_events") or [])

            total_time_ms = run.get("total_time_ms")
            threshold_ms = thresholds.get(mode, 0)
            if run.get("success") and total_time_ms is not None and threshold_ms > 0 and int(total_time_ms) >= threshold_ms:
                slow_event = {
                    "mode": mode,
                    "iteration": iteration,
                    "time": run.get("time"),
                    "total_time_ms": total_time_ms,
                    "threshold_ms": threshold_ms,
                    "trace_exported": False,
                    "trace_path": "",
                }
                if enable_slow_trace and mode not in traced_modes:
                    traced_modes.add(mode)
                    trace_event = await _capture_startup_perfetto_trace(
                        device_serial=device_serial,
                        package_name=package_name,
                        component=component,
                        mode=mode,
                        ready_check=ready_options,
                        cooldown_sec=cooldown_sec,
                        report_dir=report_dir,
                        trace_artifacts=trace_artifacts,
                    )
                    slow_event.update(trace_event)
                slow_events.append(slow_event)

    if trace_artifacts:
        try:
            _analyze_exported_traces(package_name, trace_artifacts, [])
        except Exception as exc:
            logger.warning("冷热启动 Perfetto trace 分析失败，已跳过: %s", exc)

    aggregate = _compute_startup_aggregate(startup_runs, thresholds)
    success_count = sum(1 for run in startup_runs if run.get("success"))
    total_count = len(startup_runs)
    summary = {
        "session_type": "startup",
        "session_label": "冷热启动测试",
        "performance_monitor_enabled": False,
        "jank_frame_monitor_enabled": False,
        "local_replay_enabled": False,
        "startup_config": {
            "package_name": package_name,
            "activity_name": activity_name or "",
            "resolved_component": component,
            "startup_modes": modes,
            "iterations": iterations,
            "cooldown_sec": cooldown_sec,
            "capture_log": capture_log,
            "ready_check": ready_options,
            "perfetto_slow_trace": {
                "enabled": enable_slow_trace,
                "cold_threshold_ms": thresholds["cold"],
                "hot_threshold_ms": thresholds["hot"],
            },
        },
        "startup_runs": startup_runs,
        "startup_aggregate": aggregate,
        "slow_events": slow_events,
        "success_count": success_count,
        "fail_count": total_count - success_count,
        "success_rate": round(success_count / total_count, 4) if total_count else 0,
        "slow_count": sum(item.get("slow_count", 0) for item in aggregate.values()),
        "trace_artifact_count": len(trace_artifacts),
        "analyzed_trace_count": sum(1 for artifact in trace_artifacts if artifact.get("analysis_status") == "ANALYZED"),
        "total_crashes": sum(1 for event in crash_events if event.get("type") == "CRASH"),
        "total_anrs": sum(1 for event in crash_events if event.get("type") == "ANR"),
    }

    return {
        "performance_data": [],
        "jank_data": [],
        "jank_events": [],
        "trace_artifacts": trace_artifacts,
        "crash_events": crash_events,
        "summary": summary,
    }
