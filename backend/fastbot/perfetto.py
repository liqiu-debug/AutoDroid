"""Perfetto trace 会话管理：探测、配置、环形缓冲录制、导出与分析。"""
import os
import re
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

from backend.paths import PROJECT_ROOT, project_path

from backend.fastbot.adb import (
    ADB_MONITOR_COMMAND_TIMEOUT_SECONDS,
    _adb_pull,
    _adb_push,
    _adb_shell,
    _adb_shell_result,
    _check_remote_file,
)

logger = logging.getLogger("FastbotRunner")

JANK_TRACE_EXPORT_COOLDOWN_SECONDS = 60
JANK_MAX_TRACE_EXPORTS = 6
JANK_DIAGNOSTIC_TRACE_DURATION_SECONDS = 12
PERFETTO_MIN_SDK_INT = 29
FRAME_TIMELINE_MIN_SDK_INT = 31
PERFETTO_REMOTE_CONFIG_DIR = "/data/misc/perfetto-configs"
PERFETTO_REMOTE_TRACE_DIR = "/data/misc/perfetto-traces"
PERFETTO_TRACE_BUFFER_KB = 32768
PERFETTO_META_BUFFER_KB = 8192
PERFETTO_CONTINUOUS_TRACE_BUFFER_KB = 12288
PERFETTO_CONTINUOUS_META_BUFFER_KB = 2048
PERFETTO_CONTINUOUS_FILE_WRITE_PERIOD_MS = 5000
PERFETTO_CONTINUOUS_MAX_FILE_SIZE_BYTES = 64 * 1024 * 1024


@dataclass
class PerfettoSessionState:
    report_dir: str
    capture_mode: str = "diagnostic"
    available: bool = False
    frame_timeline_supported: bool = False
    sdk_int: int = 0
    session_pid: Optional[int] = None
    remote_config_path: str = ""
    remote_trace_path: str = ""
    session_index: int = 0
    export_attempts: int = 0
    last_export_time: Optional[datetime] = None
    session_started_at: Optional[datetime] = None
    enabled: bool = False
    capture_in_progress: bool = False
    started_successfully: bool = False
    last_error: str = ""


async def _detect_perfetto_support(
    device_serial: str,
    report_dir: str,
) -> PerfettoSessionState:
    state = PerfettoSessionState(report_dir=report_dir)

    sdk_output = await _adb_shell(
        device_serial,
        "getprop ro.build.version.sdk",
        timeout=ADB_MONITOR_COMMAND_TIMEOUT_SECONDS,
    )
    sdk_int = int(sdk_output.strip()) if sdk_output.strip().isdigit() else 0
    perfetto_path = await _adb_shell(
        device_serial,
        "which perfetto 2>/dev/null",
        timeout=ADB_MONITOR_COMMAND_TIMEOUT_SECONDS,
    )

    state.sdk_int = sdk_int
    state.available = bool(perfetto_path.strip()) and sdk_int >= PERFETTO_MIN_SDK_INT
    state.frame_timeline_supported = state.available and sdk_int >= FRAME_TIMELINE_MIN_SDK_INT
    return state


def _build_perfetto_trace_config(
    package_name: str,
    frame_timeline_supported: bool,
    capture_mode: str = "diagnostic",
) -> str:
    if capture_mode == "continuous":
        if not frame_timeline_supported:
            return ""
        return "\n".join([
            "write_into_file: true",
            f"file_write_period_ms: {PERFETTO_CONTINUOUS_FILE_WRITE_PERIOD_MS}",
            f"max_file_size_bytes: {PERFETTO_CONTINUOUS_MAX_FILE_SIZE_BYTES}",
            f"buffers {{ size_kb: {PERFETTO_CONTINUOUS_TRACE_BUFFER_KB} fill_policy: RING_BUFFER }}",
            f"buffers {{ size_kb: {PERFETTO_CONTINUOUS_META_BUFFER_KB} fill_policy: RING_BUFFER }}",
            """
data_sources {
  config {
    name: "android.surfaceflinger.frametimeline"
    target_buffer: 0
  }
}
""".strip(),
            """
data_sources {
  config {
    name: "linux.process_stats"
    target_buffer: 1
    process_stats_config {
      scan_all_processes_on_start: true
    }
  }
}
""".strip(),
        ]) + "\n"

    data_sources = [
        f"""
data_sources {{
  config {{
    name: "linux.ftrace"
    target_buffer: 0
    ftrace_config {{
      ftrace_events: "sched/sched_switch"
      ftrace_events: "sched/sched_wakeup"
      ftrace_events: "sched/sched_waking"
      atrace_categories: "am"
      atrace_categories: "gfx"
      atrace_categories: "input"
      atrace_categories: "view"
      atrace_categories: "wm"
      atrace_apps: "{package_name}"
    }}
  }}
}}
""".strip(),
        """
data_sources {
  config {
    name: "linux.process_stats"
    target_buffer: 1
    process_stats_config {
      scan_all_processes_on_start: true
    }
  }
}
""".strip(),
    ]

    if frame_timeline_supported:
        data_sources.append(
            """
data_sources {
  config {
    name: "android.surfaceflinger.frametimeline"
    target_buffer: 1
  }
}
""".strip()
        )

    return "\n".join([
        f"buffers {{ size_kb: {PERFETTO_TRACE_BUFFER_KB} fill_policy: RING_BUFFER }}",
        f"buffers {{ size_kb: {PERFETTO_META_BUFFER_KB} fill_policy: RING_BUFFER }}",
        *data_sources,
    ]) + "\n"


async def _cleanup_perfetto_remote_files(
    device_serial: str,
    remote_config_path: str = "",
    remote_trace_path: str = "",
):
    paths = [path for path in [remote_config_path, remote_trace_path] if path]
    if not paths:
        return
    await _adb_shell(
        device_serial,
        f"rm -f {' '.join(paths)}",
        timeout=ADB_MONITOR_COMMAND_TIMEOUT_SECONDS,
    )


async def _wait_for_perfetto_trace_finalize(device_serial: str, remote_trace_path: str):
    if not remote_trace_path:
        return
    try:
        await asyncio.wait_for(
            _adb_shell_result(
                device_serial,
                f"inotifyd - {remote_trace_path}:w | head -n0 2>/dev/null",
                timeout=ADB_MONITOR_COMMAND_TIMEOUT_SECONDS,
            ),
            timeout=10,
        )
    except Exception:
        await asyncio.sleep(2)


async def _start_perfetto_ring_buffer(
    device_serial: str,
    package_name: str,
    perfetto_state: PerfettoSessionState,
) -> bool:
    if not perfetto_state.available:
        return False

    perfetto_state.session_index += 1
    session_token = f"{os.getpid()}_{perfetto_state.session_index:03d}"
    config_mode = perfetto_state.capture_mode or "diagnostic"
    local_config_path = os.path.join(
        perfetto_state.report_dir,
        f"perfetto_{config_mode}_session_{perfetto_state.session_index:03d}.pbtxt",
    )
    remote_config_path = f"{PERFETTO_REMOTE_CONFIG_DIR}/autodroid_fastbot_{config_mode}_{session_token}.pbtxt"
    remote_trace_path = f"{PERFETTO_REMOTE_TRACE_DIR}/autodroid_fastbot_{config_mode}_{session_token}.perfetto-trace"

    config_text = _build_perfetto_trace_config(
        package_name,
        frame_timeline_supported=perfetto_state.frame_timeline_supported,
        capture_mode=config_mode,
    )
    if not config_text:
        perfetto_state.enabled = False
        perfetto_state.last_error = f"perfetto config unavailable for capture_mode={config_mode}"
        return False
    with open(local_config_path, "w", encoding="utf-8") as handle:
        handle.write(config_text)

    await _cleanup_perfetto_remote_files(
        device_serial,
        remote_config_path=remote_config_path,
        remote_trace_path=remote_trace_path,
    )
    await _adb_push(device_serial, local_config_path, remote_config_path)

    result = await _adb_shell_result(
        device_serial,
        f"perfetto --txt -c {remote_config_path} -o {remote_trace_path} --background-wait",
        timeout=ADB_MONITOR_COMMAND_TIMEOUT_SECONDS,
    )
    output = "\n".join(part for part in [result["stdout"], result["stderr"]] if part).strip()
    pid_match = re.search(r"\b(\d+)\b", output)
    if result["returncode"] != 0 or not pid_match:
        perfetto_state.enabled = False
        perfetto_state.last_error = output or "perfetto session start failed"
        logger.warning(f"启动 Perfetto ring buffer 失败: {perfetto_state.last_error}")
        await _cleanup_perfetto_remote_files(
            device_serial,
            remote_config_path=remote_config_path,
            remote_trace_path=remote_trace_path,
        )
        return False

    perfetto_state.remote_config_path = remote_config_path
    perfetto_state.remote_trace_path = remote_trace_path
    perfetto_state.session_pid = int(pid_match.group(1))
    perfetto_state.session_started_at = datetime.now()
    perfetto_state.enabled = True
    perfetto_state.started_successfully = True
    perfetto_state.last_error = ""
    logger.info(
        "已启动 Perfetto %s 会话: pid=%s, frameTimeline=%s",
        config_mode,
        perfetto_state.session_pid,
        perfetto_state.frame_timeline_supported,
    )
    return True


async def _stop_perfetto_ring_buffer(
    device_serial: str,
    perfetto_state: PerfettoSessionState,
    preserve_trace: bool = True,
):
    if perfetto_state.session_pid:
        result = await _adb_shell_result(
            device_serial,
            f"kill {perfetto_state.session_pid} >/dev/null 2>&1",
            timeout=ADB_MONITOR_COMMAND_TIMEOUT_SECONDS,
        )
        if int(result.get("returncode", 1)) == 0 and preserve_trace:
            await _wait_for_perfetto_trace_finalize(device_serial, perfetto_state.remote_trace_path)
        else:
            await asyncio.sleep(2)

    perfetto_state.session_pid = None
    perfetto_state.enabled = False
    if not preserve_trace:
        await _cleanup_perfetto_remote_files(
            device_serial,
            remote_config_path=perfetto_state.remote_config_path,
            remote_trace_path=perfetto_state.remote_trace_path,
        )
        perfetto_state.remote_config_path = ""
        perfetto_state.remote_trace_path = ""


async def _pull_perfetto_trace_to_local(
    device_serial: str,
    perfetto_state: PerfettoSessionState,
    local_trace_path: str,
    log_prefix: str,
) -> bool:
    remote_config_path = perfetto_state.remote_config_path
    remote_trace_path = perfetto_state.remote_trace_path
    if not remote_trace_path:
        perfetto_state.last_error = "trace path missing"
        return False

    if not await _check_remote_file(device_serial, remote_trace_path):
        perfetto_state.last_error = f"trace file missing: {remote_trace_path}"
        logger.warning(f"{log_prefix}失败: {perfetto_state.last_error}")
        await _cleanup_perfetto_remote_files(
            device_serial,
            remote_config_path=remote_config_path,
            remote_trace_path=remote_trace_path,
        )
        perfetto_state.remote_config_path = ""
        perfetto_state.remote_trace_path = ""
        return False

    try:
        await _adb_pull(device_serial, remote_trace_path, local_trace_path)
    except Exception as exc:
        perfetto_state.last_error = str(exc)
        logger.warning(f"{log_prefix}失败: {perfetto_state.last_error}")
        await _cleanup_perfetto_remote_files(
            device_serial,
            remote_config_path=remote_config_path,
            remote_trace_path=remote_trace_path,
        )
        perfetto_state.remote_config_path = ""
        perfetto_state.remote_trace_path = ""
        return False

    await _cleanup_perfetto_remote_files(
        device_serial,
        remote_config_path=remote_config_path,
        remote_trace_path=remote_trace_path,
    )
    perfetto_state.remote_config_path = ""
    perfetto_state.remote_trace_path = ""
    perfetto_state.last_error = ""
    return True


def _build_trace_artifact(
    local_trace_path: str,
    perfetto_state: PerfettoSessionState,
    trigger_time: str,
    trigger_reason: str,
) -> Dict:
    return {
        "path": os.path.relpath(local_trace_path, str(PROJECT_ROOT)).replace(os.sep, "/"),
        "trigger_time": trigger_time,
        "trigger_reason": trigger_reason,
        "analyzed": False,
        "source": "perfetto",
        "capture_mode": perfetto_state.capture_mode or "diagnostic",
        "capture_started_at": perfetto_state.session_started_at.isoformat() if perfetto_state.session_started_at else "",
        "capture_finished_at": datetime.now().isoformat(),
        "frame_timeline_supported": perfetto_state.frame_timeline_supported,
    }


async def _collect_continuous_perfetto_trace(
    device_serial: str,
    perfetto_state: PerfettoSessionState,
    trace_artifacts: List[Dict],
    trigger_time: Optional[str] = None,
    trigger_reason: str = "TASK_COMPLETED",
) -> Optional[Dict]:
    if not perfetto_state.enabled or not perfetto_state.remote_trace_path:
        return None

    await _stop_perfetto_ring_buffer(device_serial, perfetto_state, preserve_trace=True)

    local_trace_path = os.path.join(
        perfetto_state.report_dir,
        f"continuous_trace_{perfetto_state.session_index:03d}.perfetto-trace",
    )
    if not await _pull_perfetto_trace_to_local(
        device_serial,
        perfetto_state,
        local_trace_path,
        "拉取 Perfetto continuous trace ",
    ):
        return None

    artifact = _build_trace_artifact(
        local_trace_path,
        perfetto_state,
        trigger_time=trigger_time or datetime.now().strftime("%H:%M:%S"),
        trigger_reason=trigger_reason,
    )
    trace_artifacts.append(artifact)
    return artifact


async def _export_perfetto_trace(
    device_serial: str,
    package_name: str,
    stop_event: asyncio.Event,
    perfetto_state: PerfettoSessionState,
    trace_artifacts: List[Dict],
    trigger_time: str,
    trigger_reason: str,
    event: Optional[Dict] = None,
    duration_sec: int = JANK_DIAGNOSTIC_TRACE_DURATION_SECONDS,
) -> Optional[Dict]:
    if not perfetto_state.available or perfetto_state.capture_in_progress:
        return None

    next_trace_index = perfetto_state.export_attempts + 1
    perfetto_state.export_attempts = next_trace_index
    try:
        perfetto_state.capture_in_progress = True
        logger.info(
            "开始按需录制 Perfetto 异常诊断 trace: reason=%s, duration=%ss",
            trigger_reason,
            duration_sec,
        )
        started = await _start_perfetto_ring_buffer(device_serial, package_name, perfetto_state)
        if not started:
            if event is not None:
                event["diagnosis_status"] = "EXPORT_FAILED"
                if perfetto_state.last_error:
                    event["trace_error"] = perfetto_state.last_error
            return None

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=max(0, duration_sec))
        except asyncio.TimeoutError:
            pass

        await _stop_perfetto_ring_buffer(device_serial, perfetto_state, preserve_trace=True)
        local_trace_path = os.path.join(
            perfetto_state.report_dir,
            f"jank_trace_{next_trace_index:03d}.perfetto-trace",
        )
        if not await _pull_perfetto_trace_to_local(
            device_serial,
            perfetto_state,
            local_trace_path,
            "拉取 Perfetto 诊断 trace ",
        ):
            if event is not None:
                event["diagnosis_status"] = "EXPORT_FAILED"
                if perfetto_state.last_error:
                    event["trace_error"] = perfetto_state.last_error
            return None

        artifact = _build_trace_artifact(
            local_trace_path,
            perfetto_state,
            trigger_time=trigger_time,
            trigger_reason=trigger_reason,
        )
        artifact["capture_window_sec"] = max(0, duration_sec)
        trace_artifacts.append(artifact)
        perfetto_state.last_export_time = datetime.now()
        perfetto_state.last_error = ""

        if event is not None:
            event["trace_exported"] = True
            event["trace_path"] = artifact["path"]
            event["diagnosis_status"] = "PENDING"

        return artifact
    except Exception as exc:
        perfetto_state.last_error = str(exc)
        logger.warning(f"按需录制 Perfetto 诊断 trace 失败: {perfetto_state.last_error}")
        if event is not None:
            event["diagnosis_status"] = "EXPORT_FAILED"
            event["trace_error"] = perfetto_state.last_error
        return None
    finally:
        perfetto_state.capture_in_progress = False
        if perfetto_state.session_pid:
            await _stop_perfetto_ring_buffer(device_serial, perfetto_state, preserve_trace=False)
        elif perfetto_state.remote_config_path or perfetto_state.remote_trace_path:
            await _cleanup_perfetto_remote_files(
                device_serial,
                remote_config_path=perfetto_state.remote_config_path,
                remote_trace_path=perfetto_state.remote_trace_path,
            )
            perfetto_state.remote_config_path = ""
            perfetto_state.remote_trace_path = ""


def _should_export_perfetto_trace(
    perfetto_state: Optional[PerfettoSessionState],
    now: datetime,
) -> bool:
    if not perfetto_state or not perfetto_state.available:
        return False
    if perfetto_state.capture_in_progress or perfetto_state.enabled:
        return False
    if perfetto_state.export_attempts >= JANK_MAX_TRACE_EXPORTS:
        return False
    if perfetto_state.last_export_time is None:
        return True
    return (now - perfetto_state.last_export_time).total_seconds() >= JANK_TRACE_EXPORT_COOLDOWN_SECONDS


def _analysis_status_to_event_status(status: str) -> str:
    if status == "ANALYZED":
        return "ANALYZED"
    if status in {"TRACE_MISSING", "TOOL_MISSING", "FAILED"}:
        return "ANALYSIS_FAILED"
    return "PENDING"


def _primary_trace_cause(artifact: Dict) -> str:
    analysis = artifact.get("analysis")
    if not isinstance(analysis, dict):
        return ""
    causes = analysis.get("suspected_causes")
    if isinstance(causes, list) and causes:
        first = causes[0]
        if isinstance(first, dict):
            return str(first.get("title") or "")
    return ""


def _analyze_exported_traces(
    package_name: str,
    trace_artifacts: List[Dict],
    jank_events: List[Dict],
):
    if not trace_artifacts:
        return

    from backend.jank_analyzer import analyze_perfetto_trace

    events_by_trace = {}
    for event in jank_events:
        trace_path = str(event.get("trace_path") or "")
        if trace_path:
            events_by_trace.setdefault(trace_path, []).append(event)

    for artifact in trace_artifacts:
        relative_path = str(artifact.get("path") or "")
        if not relative_path:
            continue

        local_trace_path = str(project_path(relative_path))
        result = analyze_perfetto_trace(
            local_trace_path,
            package_name,
            capture_mode=str(artifact.get("capture_mode") or "diagnostic"),
        )
        status = str(result.get("status") or "FAILED")
        analysis = result.get("analysis")
        error = str(result.get("error") or "")

        artifact["analysis_status"] = status
        artifact["analysis_error"] = error
        artifact["analysis"] = analysis
        artifact["analyzed"] = status == "ANALYZED"

        diagnosis_status = _analysis_status_to_event_status(status)
        summary = _primary_trace_cause(artifact)
        for event in events_by_trace.get(relative_path, []):
            event["diagnosis_status"] = diagnosis_status
            if summary:
                event["diagnosis_summary"] = summary
            if error:
                event["trace_error"] = error
