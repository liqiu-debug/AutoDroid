"""
Fastbot 智能探索执行引擎

核心功能：
- 自动挂载 Fastbot 所需 jar 包到手机
- 拼接并执行 Monkey 命令
- 双协程并发：主进程执行 + 子协程监控 CPU/Mem/Crash
- 通过 asyncio.Event 协调协程退出
"""
import os
import re
import json
import shlex
import time
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, List, Dict, Optional, Callable, Awaitable

from backend.paths import PROJECT_ROOT, project_path

logger = logging.getLogger("FastbotRunner")

FASTBOT_ASSETS_DIR = str(project_path("resources", "fastbot"))

DEVICE_JARS = [
    "framework.jar",
    "monkeyq.jar",
    "fastbot-thirdpart.jar",
]
DEVICE_JAR_TARGET = "/sdcard/"
DEVICE_LIBS_TARGET = "/data/local/tmp/"

CRASH_PATTERN = re.compile(r"(FATAL EXCEPTION|ANR in)", re.IGNORECASE)
ANR_PATTERN = re.compile(r"ANR in", re.IGNORECASE)
PROC_LINE_PATTERN = re.compile(r"Process:\s*(\S+)")
ANR_PKG_PATTERN = re.compile(r"ANR in\s+(\S+)")
TOTAL_FRAMES_PATTERN = re.compile(r"Total frames rendered:\s*(\d+)", re.IGNORECASE)
JANKY_FRAMES_PATTERN = re.compile(r"Janky frames:\s*(\d+)\s*\(([\d.]+)%\)", re.IGNORECASE)
MISSED_VSYNC_PATTERN = re.compile(r"Number Missed Vsync:\s*(\d+)", re.IGNORECASE)
SLOW_UI_THREAD_PATTERN = re.compile(r"Number Slow UI thread:\s*(\d+)", re.IGNORECASE)
SLOW_BITMAP_PATTERN = re.compile(r"Number Slow bitmap uploads:\s*(\d+)", re.IGNORECASE)
SLOW_DRAW_PATTERN = re.compile(r"Number Slow issue draw commands:\s*(\d+)", re.IGNORECASE)
FRAME_DEADLINE_MISSED_PATTERN = re.compile(r"Number Frame deadline missed:\s*(\d+)", re.IGNORECASE)
FROZEN_FRAMES_PATTERN = re.compile(r"Number Frozen frames:\s*(\d+)", re.IGNORECASE)
AM_START_FIELD_PATTERN = re.compile(r"^\s*([A-Za-z]+):\s*(.*?)\s*$")
STARTUP_DISPLAYED_PATTERN = re.compile(
    r"\bDisplayed\s+([A-Za-z0-9_.$]+/[A-Za-z0-9_.$]+):\s*\+?([0-9sm.]+)",
    re.IGNORECASE,
)
STARTUP_FULLY_DRAWN_PATTERN = re.compile(
    r"\bFully drawn\s+([A-Za-z0-9_.$]+/[A-Za-z0-9_.$]+):\s*\+?([0-9sm.]+)",
    re.IGNORECASE,
)

JANK_SAMPLE_INTERVAL_SECONDS = 5
JANK_ACTIVE_FRAME_THRESHOLD = 20
JANK_WARNING_RATE_THRESHOLD = 0.08
JANK_CRITICAL_RATE_THRESHOLD = 0.15
JANK_WARNING_FPS_THRESHOLD = 45.0
JANK_CRITICAL_FPS_THRESHOLD = 30.0
JANK_CONSECUTIVE_WINDOWS_REQUIRED = 2
JANK_EVENT_DEDUP_SECONDS = 30
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
FASTBOT_REPORTS_DIR = str(project_path("reports", "fastbot"))
LOCAL_REPLAY_PRE_ROLL_SEC = 30
LOCAL_REPLAY_POST_ROLL_SEC = 5
LOCAL_REPLAY_SEGMENT_SEC = 5
ADB_MONITOR_COMMAND_TIMEOUT_SECONDS = 8
LOGCAT_SNAPSHOT_TIMEOUT_SECONDS = 8
MONITOR_TASK_SHUTDOWN_TIMEOUT_SECONDS = 12
TRACE_TASK_SHUTDOWN_TIMEOUT_SECONDS = 15

FRAMESTATS_MIN_SDK_INT = 23
FRAMESTATS_POLL_INTERVAL_SECONDS = 1.5
FRAMESTATS_HEADER_MARKER = "---PROFILEDATA---"
FRAMESTATS_COLUMN_COUNT_LEGACY = 14
FRAMESTATS_COLUMN_COUNT_DEADLINE = 15
VSYNC_PERIOD_NS = 16_666_667
JANK_FRAME_MULTIPLIER = 2.0
FRAMESTATS_IDLE_FRAME_THRESHOLD = 5


@dataclass
class FrameStatsSample:
    intended_vsync_ns: int
    vsync_ns: int
    draw_start_ns: int
    sync_start_ns: int
    issue_draw_commands_start_ns: int
    swap_buffers_ns: int
    frame_completed_ns: int
    deadline_ns: int = 0

    @property
    def total_duration_ns(self) -> int:
        return self.frame_completed_ns - self.intended_vsync_ns

    @property
    def total_duration_ms(self) -> float:
        return self.total_duration_ns / 1_000_000

    @property
    def missed_deadline(self) -> bool:
        if self.deadline_ns > 0:
            return self.frame_completed_ns > self.deadline_ns
        return self.total_duration_ns > VSYNC_PERIOD_NS

    @property
    def is_jank(self) -> bool:
        return self.total_duration_ns > int(VSYNC_PERIOD_NS * JANK_FRAME_MULTIPLIER)

    @property
    def is_frozen(self) -> bool:
        return self.total_duration_ns > 700_000_000


@dataclass
class FramestatsMonitorState:
    last_seen_vsync_ns: int = 0
    vsync_period_ns: int = VSYNC_PERIOD_NS


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


async def _adb_shell(device_serial: str, cmd: str, timeout: Optional[float] = None) -> str:
    result = await _adb_shell_result(device_serial, cmd, timeout=timeout)
    return result["stdout"]


async def _terminate_subprocess(proc) -> None:
    if proc.returncode is not None:
        return
    try:
        proc.terminate()
        await asyncio.wait_for(proc.wait(), timeout=1)
        return
    except Exception:
        pass

    try:
        proc.kill()
        await asyncio.wait_for(proc.wait(), timeout=1)
    except Exception:
        pass


async def _adb_shell_result(
    device_serial: str,
    cmd: str,
    timeout: Optional[float] = None,
) -> Dict[str, object]:
    proc = await asyncio.create_subprocess_shell(
        f"adb -s {device_serial} shell {cmd}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        if timeout is None:
            stdout, stderr = await proc.communicate()
        else:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        await _terminate_subprocess(proc)
        return {
            "stdout": "",
            "stderr": f"adb shell timeout after {timeout}s: {cmd}",
            "returncode": -1,
        }
    return {
        "stdout": stdout.decode(errors="ignore").strip(),
        "stderr": stderr.decode(errors="ignore").strip(),
        "returncode": proc.returncode,
    }


async def _adb_push(device_serial: str, local: str, remote: str):
    proc = await asyncio.create_subprocess_shell(
        f"adb -s {device_serial} push \"{local}\" \"{remote}\"",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()


async def _adb_pull(device_serial: str, remote: str, local: str):
    proc = await asyncio.create_subprocess_shell(
        f"adb -s {device_serial} pull \"{remote}\" \"{local}\"",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        message = stderr.decode(errors="ignore").strip() or stdout.decode(errors="ignore").strip()
        raise RuntimeError(message or f"adb pull failed: {remote}")


async def _check_remote_file(device_serial: str, remote_path: str) -> bool:
    """检查设备上文件是否存在"""
    result = await _adb_shell(
        device_serial,
        f"ls {remote_path} 2>/dev/null",
        timeout=ADB_MONITOR_COMMAND_TIMEOUT_SECONDS,
    )
    return bool(result and "No such file" not in result)


def _build_fastbot_report_dir(task_id: Optional[int] = None) -> str:
    task_segment = str(task_id) if task_id is not None else datetime.now().strftime("adhoc_%Y%m%d_%H%M%S")
    report_dir = os.path.join(FASTBOT_REPORTS_DIR, task_segment)
    os.makedirs(report_dir, exist_ok=True)
    return report_dir


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


def _resolve_jank_monitoring_mode(
    enable_jank_frame_monitor: bool,
    perfetto_state: Optional[PerfettoSessionState] = None,
    use_framestats: bool = False,
) -> str:
    if not enable_jank_frame_monitor:
        return "disabled"
    base = "framestats" if use_framestats else "gfxinfo"
    if perfetto_state and (perfetto_state.started_successfully or perfetto_state.available):
        return f"{base}+perfetto"
    return base


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


async def push_fastbot_assets(device_serial: str):
    """将 Fastbot 所需的 jar/so 推送至手机（已存在则跳过）"""
    marker = f"{DEVICE_JAR_TARGET}{DEVICE_JARS[0]}"
    if await _check_remote_file(device_serial, marker):
        logger.info(f"设备 {device_serial} 已部署 Fastbot 资源，跳过推送")
        return

    logger.info(f"首次部署 Fastbot 资源到设备 {device_serial}")
    tasks = []
    for jar_name in DEVICE_JARS:
        local_path = os.path.join(FASTBOT_ASSETS_DIR, jar_name)
        if os.path.exists(local_path):
            tasks.append(_adb_push(device_serial, local_path, DEVICE_JAR_TARGET))
            logger.info(f"推送 {jar_name} -> {DEVICE_JAR_TARGET}")
        else:
            logger.warning(f"Fastbot 资源缺失: {local_path}")

    libs_dir = os.path.join(FASTBOT_ASSETS_DIR, "libs")
    if os.path.isdir(libs_dir):
        tasks.append(_adb_push(device_serial, libs_dir, DEVICE_LIBS_TARGET))
        logger.info(f"推送 libs/ -> {DEVICE_LIBS_TARGET}")

    if tasks:
        await asyncio.gather(*tasks)
        logger.info(f"设备 {device_serial} Fastbot 资源部署完成")


def _build_monkey_command(
    package_name: str,
    duration: int,
    throttle: int,
    ignore_crashes: bool,
    enable_custom_event_weights: bool = False,
    pct_touch: int = 40,
    pct_motion: int = 30,
    pct_syskeys: int = 5,
    pct_majornav: int = 15,
) -> str:
    """拼接 Fastbot Monkey 命令"""
    classpath_parts = [f"{DEVICE_JAR_TARGET}{j}" for j in DEVICE_JARS]
    classpath = ":".join(classpath_parts)

    cmd = (
        f"CLASSPATH={classpath} "
        f"exec app_process /system/bin "
        f"com.android.commands.monkey.Monkey "
        f"-p {package_name} "
        f"--throttle {throttle} "
        f"--running-minutes {duration // 60 or 1} "
        f"-v -v "
    )

    if ignore_crashes:
        cmd += "--ignore-crashes --ignore-timeouts --ignore-security-exceptions "

    if enable_custom_event_weights:
        cmd += f"--pct-touch {pct_touch} "
        cmd += f"--pct-motion {pct_motion} "
        cmd += f"--pct-syskeys {pct_syskeys} "
        cmd += f"--pct-majornav {pct_majornav} "
        remainder = max(0, 100 - pct_touch - pct_motion - pct_syskeys - pct_majornav)
        if remainder > 0:
            cmd += f"--pct-anyevent {remainder} "

    cmd += "999999"
    return cmd


async def _monitor_performance(
    device_serial: str,
    package_name: str,
    stop_event: asyncio.Event,
    perf_data: List[Dict],
    interval: int = 10,
):
    """子协程：定期采集 CPU/内存"""
    while not stop_event.is_set():
        try:
            cpu_info = await _adb_shell(
                device_serial,
                f"dumpsys cpuinfo | grep {package_name} | head -1",
                timeout=ADB_MONITOR_COMMAND_TIMEOUT_SECONDS,
            )
            mem_info = await _adb_shell(
                device_serial,
                f"dumpsys meminfo {package_name} | grep 'TOTAL PSS' | head -1",
                timeout=ADB_MONITOR_COMMAND_TIMEOUT_SECONDS,
            )

            cpu_val = 0.0
            mem_val = 0.0

            cpu_match = re.search(r"([\d.]+)%", cpu_info)
            if cpu_match:
                cpu_val = float(cpu_match.group(1))

            mem_match = re.search(r"TOTAL\s+PSS:\s+([\d,]+)", mem_info)
            if not mem_match:
                mem_match = re.search(r"([\d,]+)\s+K", mem_info)
            if mem_match:
                mem_val = int(mem_match.group(1).replace(",", "")) / 1024.0

            perf_data.append({
                "time": datetime.now().strftime("%H:%M:%S"),
                "cpu": round(cpu_val, 1),
                "mem": round(mem_val, 1),
            })
        except Exception as e:
            logger.warning(f"性能采集异常: {e}")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            break
        except asyncio.TimeoutError:
            pass


def _extract_int(pattern: re.Pattern, text: str, default: int = 0) -> int:
    match = pattern.search(text or "")
    if not match:
        return default
    return int(match.group(1))


def _parse_gfxinfo_output(
    output: str,
    interval_sec: int = JANK_SAMPLE_INTERVAL_SECONDS,
    timestamp: Optional[str] = None,
) -> Optional[Dict]:
    """解析 dumpsys gfxinfo 输出，提取当前窗口的卡顿指标。"""
    if not output:
        return None

    text = output.strip()
    if not text or "No process found" in text or "Graphics info for pid 0" in text:
        return None

    total_match = TOTAL_FRAMES_PATTERN.search(text)
    if not total_match:
        return None

    total_frames = int(total_match.group(1))
    jank_match = JANKY_FRAMES_PATTERN.search(text)
    jank_frames = int(jank_match.group(1)) if jank_match else 0
    jank_rate = (float(jank_match.group(2)) / 100.0) if jank_match else (
        (jank_frames / total_frames) if total_frames > 0 else 0.0
    )

    slow_ui = _extract_int(SLOW_UI_THREAD_PATTERN, text)
    slow_bitmap = _extract_int(SLOW_BITMAP_PATTERN, text)
    slow_draw = _extract_int(SLOW_DRAW_PATTERN, text)
    frozen_frames = _extract_int(FROZEN_FRAMES_PATTERN, text)
    deadline_missed = _extract_int(FRAME_DEADLINE_MISSED_PATTERN, text)

    render_throughput = round((total_frames / interval_sec) if interval_sec > 0 else 0.0, 1)
    is_idle = total_frames < JANK_ACTIVE_FRAME_THRESHOLD

    return {
        "time": timestamp or datetime.now().strftime("%H:%M:%S"),
        "window_sec": interval_sec,
        "fps": render_throughput,
        "render_throughput": render_throughput,
        "jank_rate": round(jank_rate, 4),
        "total_frames": total_frames,
        "jank_frames": jank_frames,
        "slow_frames": slow_ui + slow_bitmap + slow_draw,
        "frozen_frames": frozen_frames,
        "missed_vsync": _extract_int(MISSED_VSYNC_PATTERN, text),
        "frame_deadline_missed": deadline_missed,
        "is_idle": is_idle,
        "source": "gfxinfo",
    }


def _extract_profiledata_section(output: str) -> str:
    """提取 PROFILEDATA 区段的前几行用于诊断日志。"""
    lines = []
    in_section = False
    for line in (output or "").splitlines():
        if line.strip() == FRAMESTATS_HEADER_MARKER:
            if in_section:
                break
            in_section = True
            continue
        if in_section:
            lines.append(line.strip())
            if len(lines) >= 5:
                break
    return " | ".join(lines) if lines else "(empty)"


def _default_framestats_col_map(num_columns: int) -> Dict[str, int]:
    """当没有 header 行时，根据列数推断默认列映射。"""
    if num_columns >= 20:
        return {
            "Flags": 0, "FrameTimelineVsyncId": 1,
            "IntendedVsync": 2, "Vsync": 3,
            "DrawStart": 8, "FrameDeadline": 9,
            "SyncQueued": 13, "SyncStart": 14,
            "IssueDrawCommandsStart": 15, "SwapBuffers": 16,
            "FrameCompleted": 17,
        }
    if num_columns >= 15:
        return {
            "Flags": 0, "IntendedVsync": 1, "Vsync": 2,
            "DrawStart": 8, "SyncQueued": 9, "SyncStart": 10,
            "IssueDrawCommandsStart": 11, "SwapBuffers": 12,
            "FrameCompleted": 13, "DeadlineNs": 14,
        }
    return {
        "Flags": 0, "IntendedVsync": 1, "Vsync": 2,
        "DrawStart": 8, "SyncQueued": 9, "SyncStart": 10,
        "IssueDrawCommandsStart": 11, "SwapBuffers": 12,
        "FrameCompleted": 13,
    }


def _parse_framestats_output(output: str) -> List[FrameStatsSample]:
    """解析 dumpsys gfxinfo <pkg> framestats 输出，提取逐帧时间戳。

    支持多种格式：
    - API 23-25: 14 列 (Flags,IntendedVsync,...,FrameCompleted)
    - API 26-30: 15 列 (增加 DeadlineNs)
    - API 31+:   24 列 (增加 FrameTimelineVsyncId, InputEventId, FrameDeadline 等)
    通过解析 header 行动态确定列位置。
    """
    if not output:
        return []

    frames: List[FrameStatsSample] = []
    in_profile_section = False
    col_map: Optional[Dict[str, int]] = None

    for line in output.splitlines():
        stripped = line.strip()
        if stripped == FRAMESTATS_HEADER_MARKER:
            in_profile_section = not in_profile_section
            col_map = None
            continue
        if not in_profile_section:
            continue
        if not stripped:
            continue

        parts = [p.strip() for p in stripped.split(",")]
        parts = [p for p in parts if p]

        if col_map is None:
            if any(c.isalpha() for c in stripped):
                col_map = {name: idx for idx, name in enumerate(parts)}
                continue
            else:
                col_map = _default_framestats_col_map(len(parts))

        if len(parts) < 14:
            continue

        try:
            values = [int(p) for p in parts]
        except ValueError:
            continue

        flags = values[0]
        if flags != 0:
            continue

        intended_vsync_idx = col_map.get("IntendedVsync", 1)
        vsync_idx = col_map.get("Vsync", 2)
        draw_start_idx = col_map.get("DrawStart", 8)
        sync_start_idx = col_map.get("SyncStart", col_map.get("SyncQueued", 10))
        issue_draw_idx = col_map.get("IssueDrawCommandsStart", 11)
        swap_buffers_idx = col_map.get("SwapBuffers", 12)
        frame_completed_idx = col_map.get("FrameCompleted", 13)
        deadline_idx = col_map.get("FrameDeadline", col_map.get("DeadlineNs", -1))

        if frame_completed_idx >= len(values) or intended_vsync_idx >= len(values):
            continue

        deadline_ns = values[deadline_idx] if 0 <= deadline_idx < len(values) else 0

        frames.append(FrameStatsSample(
            intended_vsync_ns=values[intended_vsync_idx],
            vsync_ns=values[vsync_idx] if vsync_idx < len(values) else values[intended_vsync_idx],
            draw_start_ns=values[draw_start_idx] if draw_start_idx < len(values) else 0,
            sync_start_ns=values[sync_start_idx] if sync_start_idx < len(values) else 0,
            issue_draw_commands_start_ns=values[issue_draw_idx] if issue_draw_idx < len(values) else 0,
            swap_buffers_ns=values[swap_buffers_idx] if swap_buffers_idx < len(values) else 0,
            frame_completed_ns=values[frame_completed_idx],
            deadline_ns=deadline_ns,
        ))

    frames.sort(key=lambda f: f.intended_vsync_ns)
    return frames


def _detect_vsync_period(frames: List[FrameStatsSample], default_ns: int = VSYNC_PERIOD_NS) -> int:
    """从帧间隔中位数自动检测 vsync 周期，适配高刷新率设备。"""
    if len(frames) < 4:
        return default_ns
    intervals = [
        frames[i + 1].intended_vsync_ns - frames[i].intended_vsync_ns
        for i in range(len(frames) - 1)
        if frames[i + 1].intended_vsync_ns > frames[i].intended_vsync_ns
    ]
    if not intervals:
        return default_ns
    intervals.sort()
    median = intervals[len(intervals) // 2]
    if median <= 0:
        return default_ns
    return median


def _compute_framestats_sample(
    frames: List[FrameStatsSample],
    vsync_period_ns: int = VSYNC_PERIOD_NS,
    timestamp: Optional[str] = None,
) -> Optional[Dict]:
    """将一批新帧转换为兼容现有 jank_data 格式的样本字典。"""
    if not frames:
        return None

    total_frames = len(frames)
    is_idle = total_frames < FRAMESTATS_IDLE_FRAME_THRESHOLD

    durations_ns = [f.total_duration_ns for f in frames]
    durations_ms = [ns / 1_000_000 for ns in durations_ns]
    durations_ms.sort()

    jank_threshold_ns = int(vsync_period_ns * JANK_FRAME_MULTIPLIER)
    jank_frames = sum(1 for f in frames if f.total_duration_ns > jank_threshold_ns)
    frozen_frames = sum(1 for f in frames if f.is_frozen)
    deadline_missed = sum(1 for f in frames if f.missed_deadline)

    jank_rate = jank_frames / total_frames if total_frames > 0 else 0.0

    target_fps = round(1_000_000_000 / vsync_period_ns, 1)
    vsync_span_ns = 0
    if total_frames >= 2:
        vsync_span_ns = frames[-1].intended_vsync_ns - frames[0].intended_vsync_ns
        if vsync_span_ns > 0:
            real_fps = round((total_frames - 1) / (vsync_span_ns / 1_000_000_000), 1)
            real_fps = min(real_fps, target_fps * 1.5)
        else:
            real_fps = target_fps
    else:
        real_fps = target_fps

    def percentile(sorted_values: List[float], pct: float) -> float:
        if not sorted_values:
            return 0.0
        rank = (pct / 100) * (len(sorted_values) - 1)
        low = int(rank)
        high = min(low + 1, len(sorted_values) - 1)
        weight = rank - low
        return round(sorted_values[low] * (1 - weight) + sorted_values[high] * weight, 2)

    return {
        "time": timestamp or datetime.now().strftime("%H:%M:%S"),
        "window_sec": round(vsync_span_ns / 1_000_000_000, 2) if total_frames >= 2 and vsync_span_ns > 0 else round(vsync_period_ns / 1_000_000_000, 3),
        "fps": real_fps,
        "render_throughput": real_fps,
        "jank_rate": round(jank_rate, 4),
        "total_frames": total_frames,
        "jank_frames": jank_frames,
        "slow_frames": jank_frames,
        "frozen_frames": frozen_frames,
        "missed_vsync": 0,
        "frame_deadline_missed": deadline_missed,
        "is_idle": is_idle,
        "source": "framestats",
        "frame_time_p50_ms": percentile(durations_ms, 50),
        "frame_time_p90_ms": percentile(durations_ms, 90),
        "frame_time_p95_ms": percentile(durations_ms, 95),
        "frame_time_p99_ms": percentile(durations_ms, 99),
        "frame_time_max_ms": round(durations_ms[-1], 2) if durations_ms else 0,
        "frame_time_avg_ms": round(sum(durations_ms) / len(durations_ms), 2) if durations_ms else 0,
    }


def _classify_jank_sample(sample: Dict) -> Dict[str, object]:
    """根据单个采样窗口判断卡顿等级。"""
    total_frames = sample.get("total_frames", 0) or 0
    render_throughput = float(sample.get("render_throughput", sample.get("fps", 0.0)) or 0.0)
    jank_rate = float(sample.get("jank_rate", 0.0) or 0.0)
    frozen_frames = int(sample.get("frozen_frames", 0) or 0)
    is_idle = bool(sample.get("is_idle"))
    has_render_stall_evidence = any([
        jank_rate > 0,
        frozen_frames > 0,
        int(sample.get("jank_frames", 0) or 0) > 0,
        int(sample.get("slow_frames", 0) or 0) > 0,
        int(sample.get("frame_deadline_missed", 0) or 0) > 0,
        int(sample.get("missed_vsync", 0) or 0) > 0,
    ])

    if frozen_frames > 0:
        return {"severity": "CRITICAL", "reason": "FROZEN_FRAME", "immediate": True}

    if sample.get("source") == "framestats":
        max_ms = float(sample.get("frame_time_max_ms", 0) or 0)
        p99_ms = float(sample.get("frame_time_p99_ms", 0) or 0)
        if max_ms > 700:
            return {"severity": "CRITICAL", "reason": "FROZEN_FRAME", "immediate": True}
        if p99_ms > 100 and jank_rate >= JANK_CRITICAL_RATE_THRESHOLD:
            return {"severity": "CRITICAL", "reason": "HIGH_FRAME_TIME_P99", "immediate": False}

    if total_frames > 0 and jank_rate >= JANK_CRITICAL_RATE_THRESHOLD:
        return {"severity": "CRITICAL", "reason": "HIGH_JANK_RATE", "immediate": False}
    if not is_idle and has_render_stall_evidence and total_frames >= JANK_ACTIVE_FRAME_THRESHOLD and render_throughput < JANK_CRITICAL_FPS_THRESHOLD:
        return {"severity": "CRITICAL", "reason": "LOW_FPS", "immediate": False}
    if total_frames > 0 and jank_rate >= JANK_WARNING_RATE_THRESHOLD:
        return {"severity": "WARNING", "reason": "HIGH_JANK_RATE", "immediate": False}
    if not is_idle and has_render_stall_evidence and total_frames >= JANK_ACTIVE_FRAME_THRESHOLD and render_throughput < JANK_WARNING_FPS_THRESHOLD:
        return {"severity": "WARNING", "reason": "LOW_FPS", "immediate": False}
    return {"severity": None, "reason": None, "immediate": False}


def _build_jank_event(sample: Dict, severity: str, reason: str) -> Dict:
    return {
        "time": sample.get("time"),
        "severity": severity,
        "reason": reason,
        "fps": sample.get("fps", 0),
        "render_throughput": sample.get("render_throughput", sample.get("fps", 0)),
        "jank_rate": sample.get("jank_rate", 0),
        "window_sec": sample.get("window_sec", JANK_SAMPLE_INTERVAL_SECONDS),
        "total_frames": sample.get("total_frames", 0),
        "jank_frames": sample.get("jank_frames", 0),
        "is_idle": bool(sample.get("is_idle")),
        "trace_exported": False,
        "trace_path": "",
        "diagnosis_status": "UNAVAILABLE",
        "trace_error": "",
        "source": sample.get("source", "gfxinfo"),
        "cpu": None,
        "mem": None,
    }


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


async def _get_device_sdk_int(device_serial: str) -> int:
    sdk_output = await _adb_shell(
        device_serial,
        "getprop ro.build.version.sdk",
        timeout=ADB_MONITOR_COMMAND_TIMEOUT_SECONDS,
    )
    return int(sdk_output.strip()) if sdk_output.strip().isdigit() else 0


async def _emit_jank_event(
    sample: Dict,
    severity: str,
    reason: str,
    jank_events: List[Dict],
    perf_data: Optional[List[Dict]],
    perfetto_state: Optional[PerfettoSessionState],
    trace_artifacts: Optional[List[Dict]],
    trace_capture_tasks: Optional[List[asyncio.Task]],
    device_serial: str,
    package_name: str,
    stop_event: asyncio.Event,
):
    """构建并发射卡顿事件，处理 Perfetto trace 导出。"""
    now = datetime.now()
    event = _build_jank_event(sample, severity, reason)
    if perf_data:
        perf_sample = _find_closest_perf_sample(perf_data, str(sample.get("time") or ""))
        if perf_sample:
            event["cpu"] = perf_sample.get("cpu")
            event["mem"] = perf_sample.get("mem")

    if severity == "CRITICAL":
        if _should_export_perfetto_trace(perfetto_state, now):
            event["diagnosis_status"] = "EXPORT_IN_PROGRESS"
            if trace_capture_tasks is not None:
                trace_capture_tasks.append(asyncio.create_task(
                    _export_perfetto_trace(
                        device_serial,
                        package_name,
                        stop_event,
                        perfetto_state,
                        trace_artifacts if trace_artifacts is not None else [],
                        trigger_time=str(sample.get("time") or now.strftime("%H:%M:%S")),
                        trigger_reason=reason,
                        event=event,
                    )
                ))
            else:
                artifact = await _export_perfetto_trace(
                    device_serial,
                    package_name,
                    stop_event,
                    perfetto_state,
                    trace_artifacts if trace_artifacts is not None else [],
                    trigger_time=str(sample.get("time") or now.strftime("%H:%M:%S")),
                    trigger_reason=reason,
                    event=event,
                )
                if artifact:
                    event["trace_exported"] = True
                    event["trace_path"] = artifact["path"]
                    event["diagnosis_status"] = "PENDING"
        elif perfetto_state and perfetto_state.available:
            if perfetto_state.export_attempts >= JANK_MAX_TRACE_EXPORTS:
                event["diagnosis_status"] = "EXPORT_LIMIT_REACHED"
            elif perfetto_state.capture_in_progress or perfetto_state.enabled:
                event["diagnosis_status"] = "EXPORT_IN_PROGRESS"
            else:
                event["diagnosis_status"] = "EXPORT_COOLDOWN"

    jank_events.append(event)


async def _monitor_jank_framestats(
    device_serial: str,
    package_name: str,
    stop_event: asyncio.Event,
    jank_data: List[Dict],
    jank_events: List[Dict],
    perf_data: Optional[List[Dict]] = None,
    trace_artifacts: Optional[List[Dict]] = None,
    trace_capture_tasks: Optional[List[asyncio.Task]] = None,
    perfetto_state: Optional[PerfettoSessionState] = None,
) -> bool:
    """子协程：通过 framestats 逐帧采集。返回 True 表示成功采集过数据，False 表示需要降级。"""
    probe_output = await _adb_shell(
        device_serial,
        f"dumpsys gfxinfo {package_name} framestats",
        timeout=ADB_MONITOR_COMMAND_TIMEOUT_SECONDS,
    )
    if FRAMESTATS_HEADER_MARKER not in (probe_output or ""):
        logger.warning(
            f"framestats 探测失败：输出中未找到 {FRAMESTATS_HEADER_MARKER} 标记，降级到 gfxinfo reset。"
            f" 输出前300字符: {(probe_output or '')[:300]}"
        )
        return False

    probe_frames = _parse_framestats_output(probe_output)
    if not probe_frames:
        logger.warning(
            f"framestats 探测失败：找到标记但无法解析出帧数据，降级到 gfxinfo reset。"
            f" PROFILEDATA 区段内容: {_extract_profiledata_section(probe_output)}"
        )
        return False

    state = FramestatsMonitorState()
    warning_streak = 0
    critical_streak = 0
    last_event_by_key: Dict[str, datetime] = {}
    poll_interval = FRAMESTATS_POLL_INTERVAL_SECONDS
    empty_polls = 0
    FRAMESTATS_FALLBACK_THRESHOLD = 5

    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=poll_interval)
            break
        except asyncio.TimeoutError:
            pass

        try:
            output = await _adb_shell(
                device_serial,
                f"dumpsys gfxinfo {package_name} framestats",
                timeout=ADB_MONITOR_COMMAND_TIMEOUT_SECONDS,
            )
            all_frames = _parse_framestats_output(output)
            new_frames = [f for f in all_frames if f.intended_vsync_ns > state.last_seen_vsync_ns]

            if not new_frames:
                empty_polls += 1
                if empty_polls >= FRAMESTATS_FALLBACK_THRESHOLD and not jank_data:
                    logger.warning(
                        f"framestats 连续 {empty_polls} 次无数据，降级到 gfxinfo reset 模式。"
                        f" 原始输出前200字符: {(output or '')[:200]}"
                    )
                    return False
                continue

            empty_polls = 0

            state.last_seen_vsync_ns = new_frames[-1].intended_vsync_ns

            if len(new_frames) >= 4:
                state.vsync_period_ns = _detect_vsync_period(new_frames, state.vsync_period_ns)

            sample = _compute_framestats_sample(
                new_frames,
                vsync_period_ns=state.vsync_period_ns,
                timestamp=datetime.now().strftime("%H:%M:%S"),
            )
            if not sample:
                continue

            jank_data.append(sample)

            verdict = _classify_jank_sample(sample)
            severity = verdict["severity"]
            reason = verdict["reason"]
            immediate = bool(verdict["immediate"])

            if severity == "CRITICAL":
                critical_streak += 1
                warning_streak = 0
                threshold_met = immediate or critical_streak >= JANK_CONSECUTIVE_WINDOWS_REQUIRED
            elif severity == "WARNING":
                warning_streak += 1
                critical_streak = 0
                threshold_met = warning_streak >= JANK_CONSECUTIVE_WINDOWS_REQUIRED
            else:
                warning_streak = 0
                critical_streak = 0
                continue

            now = datetime.now()
            event_key = f"{severity}:{reason}"
            last_same_event_time = last_event_by_key.get(event_key)
            is_duplicate = (
                last_same_event_time is not None and
                (now - last_same_event_time).total_seconds() < JANK_EVENT_DEDUP_SECONDS
            )
            if threshold_met and not is_duplicate:
                await _emit_jank_event(
                    sample, str(severity), str(reason),
                    jank_events, perf_data, perfetto_state,
                    trace_artifacts, trace_capture_tasks,
                    device_serial, package_name, stop_event,
                )
                last_event_by_key[event_key] = now
                warning_streak = 0
                critical_streak = 0
        except Exception as e:
            logger.warning(f"framestats 采集异常: {e}")

    return True


async def _monitor_jank(
    device_serial: str,
    package_name: str,
    stop_event: asyncio.Event,
    jank_data: List[Dict],
    jank_events: List[Dict],
    perf_data: Optional[List[Dict]] = None,
    trace_artifacts: Optional[List[Dict]] = None,
    trace_capture_tasks: Optional[List[asyncio.Task]] = None,
    perfetto_state: Optional[PerfettoSessionState] = None,
    interval: int = JANK_SAMPLE_INTERVAL_SECONDS,
):
    """子协程：定期采集帧数据。SDK >= 23 使用 framestats 逐帧采集，失败时自动降级到 gfxinfo reset。"""
    sdk_int = await _get_device_sdk_int(device_serial)

    if sdk_int >= FRAMESTATS_MIN_SDK_INT:
        success = await _monitor_jank_framestats(
            device_serial, package_name, stop_event,
            jank_data, jank_events, perf_data,
            trace_artifacts, trace_capture_tasks, perfetto_state,
        )
        if not success and not stop_event.is_set():
            logger.info("framestats 采集失败，自动降级到 gfxinfo reset 模式")
            await _monitor_jank_legacy(
                device_serial, package_name, stop_event,
                jank_data, jank_events, perf_data,
                trace_artifacts, trace_capture_tasks, perfetto_state,
                interval=interval,
            )
    else:
        await _monitor_jank_legacy(
            device_serial, package_name, stop_event,
            jank_data, jank_events, perf_data,
            trace_artifacts, trace_capture_tasks, perfetto_state,
            interval=interval,
        )


async def _monitor_jank_legacy(
    device_serial: str,
    package_name: str,
    stop_event: asyncio.Event,
    jank_data: List[Dict],
    jank_events: List[Dict],
    perf_data: Optional[List[Dict]] = None,
    trace_artifacts: Optional[List[Dict]] = None,
    trace_capture_tasks: Optional[List[asyncio.Task]] = None,
    perfetto_state: Optional[PerfettoSessionState] = None,
    interval: int = JANK_SAMPLE_INTERVAL_SECONDS,
):
    """子协程：定期采集 gfxinfo，输出 FPS / 卡顿率趋势与异常事件。"""
    warning_streak = 0
    critical_streak = 0
    last_event_by_key: Dict[str, datetime] = {}

    try:
        await _adb_shell(
            device_serial,
            f"dumpsys gfxinfo {package_name} reset",
            timeout=ADB_MONITOR_COMMAND_TIMEOUT_SECONDS,
        )
    except Exception as e:
        logger.warning(f"初始化卡顿监控失败: {e}")

    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            break
        except asyncio.TimeoutError:
            pass

        try:
            output = await _adb_shell(
                device_serial,
                f"dumpsys gfxinfo {package_name} reset",
                timeout=ADB_MONITOR_COMMAND_TIMEOUT_SECONDS,
            )
            sample = _parse_gfxinfo_output(
                output,
                interval_sec=interval,
                timestamp=datetime.now().strftime("%H:%M:%S"),
            )
            if not sample:
                continue

            jank_data.append(sample)

            verdict = _classify_jank_sample(sample)
            severity = verdict["severity"]
            reason = verdict["reason"]
            immediate = bool(verdict["immediate"])

            if severity == "CRITICAL":
                critical_streak += 1
                warning_streak = 0
                threshold_met = immediate or critical_streak >= JANK_CONSECUTIVE_WINDOWS_REQUIRED
            elif severity == "WARNING":
                warning_streak += 1
                critical_streak = 0
                threshold_met = warning_streak >= JANK_CONSECUTIVE_WINDOWS_REQUIRED
            else:
                warning_streak = 0
                critical_streak = 0
                continue

            now = datetime.now()
            event_key = f"{severity}:{reason}"
            last_same_event_time = last_event_by_key.get(event_key)
            is_duplicate = (
                last_same_event_time is not None and
                (now - last_same_event_time).total_seconds() < JANK_EVENT_DEDUP_SECONDS
            )
            if threshold_met and not is_duplicate:
                event = _build_jank_event(sample, str(severity), str(reason))
                if perf_data:
                    perf_sample = _find_closest_perf_sample(perf_data, str(sample.get("time") or ""))
                    if perf_sample:
                        event["cpu"] = perf_sample.get("cpu")
                        event["mem"] = perf_sample.get("mem")

                if severity == "CRITICAL":
                    if _should_export_perfetto_trace(perfetto_state, now):
                        event["diagnosis_status"] = "EXPORT_IN_PROGRESS"
                        if trace_capture_tasks is not None:
                            trace_capture_tasks.append(asyncio.create_task(
                                _export_perfetto_trace(
                                    device_serial,
                                    package_name,
                                    stop_event,
                                    perfetto_state,
                                    trace_artifacts if trace_artifacts is not None else [],
                                    trigger_time=str(sample.get("time") or now.strftime("%H:%M:%S")),
                                    trigger_reason=str(reason),
                                    event=event,
                                )
                            ))
                        else:
                            artifact = await _export_perfetto_trace(
                                device_serial,
                                package_name,
                                stop_event,
                                perfetto_state,
                                trace_artifacts if trace_artifacts is not None else [],
                                trigger_time=str(sample.get("time") or now.strftime("%H:%M:%S")),
                                trigger_reason=str(reason),
                                event=event,
                            )
                            if artifact:
                                event["trace_exported"] = True
                                event["trace_path"] = artifact["path"]
                                event["diagnosis_status"] = "PENDING"
                    elif perfetto_state and perfetto_state.available:
                        if perfetto_state.export_attempts >= JANK_MAX_TRACE_EXPORTS:
                            event["diagnosis_status"] = "EXPORT_LIMIT_REACHED"
                        elif perfetto_state.capture_in_progress or perfetto_state.enabled:
                            event["diagnosis_status"] = "EXPORT_IN_PROGRESS"
                        else:
                            event["diagnosis_status"] = "EXPORT_COOLDOWN"

                jank_events.append(event)
                last_event_by_key[event_key] = now
                warning_streak = 0
                critical_streak = 0
        except Exception as e:
            logger.warning(f"卡顿采集异常: {e}")


def _time_text_to_seconds(value: str) -> Optional[int]:
    parts = str(value or "").split(":")
    if len(parts) != 3:
        return None
    try:
        hour, minute, second = [int(part) for part in parts]
    except ValueError:
        return None
    return hour * 3600 + minute * 60 + second


def _find_closest_perf_sample(perf_data: List[Dict], sample_time: str) -> Optional[Dict]:
    sample_seconds = _time_text_to_seconds(sample_time)
    if sample_seconds is None:
        return perf_data[-1] if perf_data else None

    best_sample = None
    best_delta = None
    for perf_sample in perf_data:
        perf_seconds = _time_text_to_seconds(str(perf_sample.get("time") or ""))
        if perf_seconds is None:
            continue
        delta = abs(perf_seconds - sample_seconds)
        if best_delta is None or delta < best_delta:
            best_sample = perf_sample
            best_delta = delta
    return best_sample or (perf_data[-1] if perf_data else None)


DEDUP_COOLDOWN_SECONDS = 10
CRASH_SOURCE_TAG = re.compile(r"E/AndroidRuntime\s*\(")


async def _monitor_logcat(
    device_serial: str,
    package_name: str,
    stop_event: asyncio.Event,
    crash_events: List[Dict],
    capture_log: bool,
    abort_on_crash: bool = False,
    abort_event: Optional[asyncio.Event] = None,
    replay_callback: Optional[Callable[[str, str], Awaitable[Optional[Dict]]]] = None,
):
    """子协程：持续读取 logcat 流，只抓取目标包名相关的崩溃/ANR。

    策略：
    - 启动前调用方已清空 logcat 缓冲区，避免旧日志干扰
    - 只认 E/AndroidRuntime 标签的 FATAL EXCEPTION，忽略厂商重复条目
    - 同类事件在冷却期(10s)内不重复计数
    - abort_on_crash=True 时，检测到崩溃后触发 abort_event 通知主协程终止 Monkey
    """
    proc = await asyncio.create_subprocess_shell(
        f"adb -s {device_serial} logcat -v time *:E",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    pending_crash = False
    pending_lines: List[str] = []
    crash_timestamp = ""
    MAX_LOOK_AHEAD = 15
    last_crash_time: Optional[datetime] = None
    last_anr_time: Optional[datetime] = None

    try:
        while not stop_event.is_set():
            try:
                line_bytes = await asyncio.wait_for(
                    proc.stdout.readline(), timeout=1.0
                )
            except asyncio.TimeoutError:
                if pending_crash:
                    pending_crash = False
                    pending_lines.clear()
                continue
            if not line_bytes:
                break

            line = line_bytes.decode(errors="ignore")

            if ANR_PATTERN.search(line):
                anr_pkg = ANR_PKG_PATTERN.search(line)
                if anr_pkg and package_name in anr_pkg.group(1):
                    now = datetime.now()
                    if last_anr_time and (now - last_anr_time).total_seconds() < DEDUP_COOLDOWN_SECONDS:
                        logger.debug(f"ANR 去重冷却中，忽略: {line.strip()[:120]}")
                        continue
                    last_anr_time = now
                    full_log = ""
                    if capture_log:
                        full_log = await _capture_logcat_snapshot(device_serial)
                    event_time = now.strftime("%H:%M:%S")
                    event = {
                        "time": event_time,
                        "type": "ANR",
                        "full_log": full_log,
                    }
                    if replay_callback:
                        try:
                            replay = await replay_callback("ANR", event_time)
                            if replay:
                                event["replay"] = replay
                        except Exception as exc:
                            event["replay"] = {
                                "status": "FAILED",
                                "error": str(exc),
                            }
                    crash_events.append(event)
                    logger.warning(f"检测到 ANR ({package_name}): {line.strip()[:200]}")
                    if abort_on_crash and abort_event:
                        logger.warning(f"容错策略=立即停止，触发终止")
                        abort_event.set()
                        return
                continue

            if re.search(r"FATAL EXCEPTION", line, re.IGNORECASE):
                if not CRASH_SOURCE_TAG.search(line):
                    continue
                pending_crash = True
                pending_lines = [line]
                crash_timestamp = datetime.now().strftime("%H:%M:%S")
                continue

            if pending_crash:
                pending_lines.append(line)
                proc_match = PROC_LINE_PATTERN.search(line)
                if proc_match:
                    crash_pkg = proc_match.group(1).rstrip(",")
                    if package_name in crash_pkg:
                        now = datetime.now()
                        if last_crash_time and (now - last_crash_time).total_seconds() < DEDUP_COOLDOWN_SECONDS:
                            logger.debug(f"CRASH 去重冷却中，忽略: {pending_lines[0].strip()[:120]}")
                        else:
                            last_crash_time = now
                            full_log = ""
                            if capture_log:
                                full_log = await _capture_logcat_snapshot(device_serial)
                            event = {
                                "time": crash_timestamp,
                                "type": "CRASH",
                                "full_log": full_log,
                            }
                            if replay_callback:
                                try:
                                    replay = await replay_callback("CRASH", crash_timestamp)
                                    if replay:
                                        event["replay"] = replay
                                except Exception as exc:
                                    event["replay"] = {
                                        "status": "FAILED",
                                        "error": str(exc),
                                    }
                            crash_events.append(event)
                            logger.warning(f"检测到 CRASH ({package_name}): {pending_lines[0].strip()[:200]}")
                            if abort_on_crash and abort_event:
                                logger.warning(f"容错策略=立即停止，触发终止")
                                abort_event.set()
                                pending_crash = False
                                pending_lines.clear()
                                return
                    else:
                        logger.debug(f"忽略非目标包 CRASH: {crash_pkg}")
                    pending_crash = False
                    pending_lines.clear()
                elif len(pending_lines) >= MAX_LOOK_AHEAD:
                    logger.debug(f"FATAL EXCEPTION 后 {MAX_LOOK_AHEAD} 行内未找到 Process 行，忽略")
                    pending_crash = False
                    pending_lines.clear()
    finally:
        proc.terminate()
        try:
            await proc.wait()
        except Exception:
            pass


async def _capture_logcat_snapshot(device_serial: str) -> str:
    """截取最近 500 行 logcat"""
    proc = await asyncio.create_subprocess_shell(
        f"adb -s {device_serial} logcat -d -t 500",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await asyncio.wait_for(
            proc.communicate(),
            timeout=LOGCAT_SNAPSHOT_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        await _terminate_subprocess(proc)
        logger.warning("抓取 logcat 快照超时: serial=%s", device_serial)
        return ""
    return stdout.decode(errors="ignore")


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


async def _await_task_group(tasks: List[asyncio.Task], timeout: float, label: str) -> None:
    if not tasks:
        return
    try:
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.warning("%s 退出超时，已强制取消剩余任务", label)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def run_fastbot_task(
    device_serial: str,
    package_name: str,
    duration: int,
    throttle: int,
    ignore_crashes: bool,
    capture_log: bool,
    task_id: Optional[int] = None,
    enable_performance_monitor: bool = True,
    enable_jank_frame_monitor: bool = False,
    enable_local_replay: bool = True,
    enable_custom_event_weights: bool = False,
    pct_touch: int = 40,
    pct_motion: int = 30,
    pct_syskeys: int = 5,
    pct_majornav: int = 15,
) -> Dict:
    """
    主执行函数：启动 Monkey 主进程 + 性能/崩溃监控子协程。
    
    返回 {performance_data, jank_data, jank_events, crash_events, summary}
    """
    await push_fastbot_assets(device_serial)

    monkey_cmd = _build_monkey_command(
        package_name, duration, throttle, ignore_crashes,
        enable_custom_event_weights, pct_touch, pct_motion, pct_syskeys, pct_majornav,
    )

    perf_data: List[Dict] = []
    jank_data: List[Dict] = []
    jank_events: List[Dict] = []
    trace_artifacts: List[Dict] = []
    crash_events: List[Dict] = []
    stop_event = asyncio.Event()
    abort_event = asyncio.Event()
    should_abort = not ignore_crashes
    perfetto_state: Optional[PerfettoSessionState] = None
    continuous_perfetto_state: Optional[PerfettoSessionState] = None
    trace_capture_tasks: List[asyncio.Task] = []
    report_dir = _build_fastbot_report_dir(task_id) if (enable_jank_frame_monitor or enable_local_replay) else ""
    local_replay_started = False

    if enable_local_replay and report_dir:
        try:
            from backend.device_stream.manager import device_manager

            await asyncio.to_thread(
                device_manager.start_recording,
                device_serial,
                task_id or 0,
                report_dir,
                LOCAL_REPLAY_PRE_ROLL_SEC,
                LOCAL_REPLAY_POST_ROLL_SEC,
                LOCAL_REPLAY_SEGMENT_SEC,
            )
            local_replay_started = True
        except Exception as exc:
            logger.warning(f"初始化本地复现录制失败，已降级为无视频回放: {exc}")

    async def _capture_local_replay(event_type: str, event_time: str) -> Optional[Dict]:
        if not local_replay_started:
            return None
        from backend.device_stream.manager import device_manager

        result = await asyncio.to_thread(
            device_manager.capture_replay,
            device_serial,
            event_type,
            event_time,
        )
        return result.to_dict() if result else None

    await _adb_shell(
        device_serial,
        "logcat -c",
        timeout=ADB_MONITOR_COMMAND_TIMEOUT_SECONDS,
    )
    logger.info("已清空 logcat 缓冲区")

    monitor_tasks = []
    if enable_performance_monitor:
        monitor_tasks.append(asyncio.create_task(
            _monitor_performance(device_serial, package_name, stop_event, perf_data)
        ))
    if enable_jank_frame_monitor:
        perfetto_state = PerfettoSessionState(report_dir=report_dir, capture_mode="diagnostic")
        try:
            perfetto_state = await _detect_perfetto_support(device_serial, report_dir)
            perfetto_state.capture_mode = "diagnostic"
            if perfetto_state.frame_timeline_supported:
                continuous_perfetto_state = await _detect_perfetto_support(device_serial, report_dir)
                continuous_perfetto_state.capture_mode = "continuous"
                await _start_perfetto_ring_buffer(device_serial, package_name, continuous_perfetto_state)
        except Exception as exc:
            logger.warning(f"初始化 Perfetto 取证失败，已降级为 gfxinfo-only: {exc}")
        monitor_tasks.append(asyncio.create_task(
            _monitor_jank(
                device_serial,
                package_name,
                stop_event,
                jank_data,
                jank_events,
                perf_data=perf_data,
                trace_artifacts=trace_artifacts,
                trace_capture_tasks=trace_capture_tasks,
                perfetto_state=perfetto_state,
            )
        ))
    logcat_task = asyncio.create_task(
        _monitor_logcat(
            device_serial, package_name, stop_event, crash_events, capture_log,
            abort_on_crash=should_abort, abort_event=abort_event,
            replay_callback=_capture_local_replay if local_replay_started else None,
        )
    )
    monitor_tasks.append(logcat_task)

    try:
        monkey_proc = await asyncio.create_subprocess_shell(
            f"adb -s {device_serial} shell \"{monkey_cmd}\"",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        monkey_comm = asyncio.create_task(monkey_proc.communicate())
        abort_wait = asyncio.create_task(abort_event.wait())

        done, pending = await asyncio.wait(
            {monkey_comm, abort_wait},
            timeout=duration + 60,
            return_when=asyncio.FIRST_COMPLETED,
        )

        if abort_wait in done:
            logger.warning("检测到崩溃且容错策略为立即停止，正在终止 Monkey 进程")
            monkey_proc.terminate()
            try:
                await asyncio.wait_for(monkey_proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                monkey_proc.kill()
            monkey_comm.cancel()
        elif monkey_comm not in done:
            monkey_proc.terminate()
            try:
                await monkey_proc.wait()
            except Exception:
                pass
            logger.warning("Monkey 进程超时，已强制终止")
            monkey_comm.cancel()

        for t in pending:
            t.cancel()

    finally:
        stop_event.set()
        await _await_task_group(
            monitor_tasks,
            timeout=MONITOR_TASK_SHUTDOWN_TIMEOUT_SECONDS,
            label="Fastbot 监控协程",
        )
        if trace_capture_tasks:
            await _await_task_group(
                trace_capture_tasks,
                timeout=TRACE_TASK_SHUTDOWN_TIMEOUT_SECONDS,
                label="Perfetto Trace 导出协程",
            )
        if local_replay_started:
            try:
                from backend.device_stream.manager import device_manager

                await asyncio.to_thread(device_manager.stop_recording, device_serial)
            except Exception as exc:
                logger.warning(f"停止本地复现录制失败，已忽略: {exc}")
        if perfetto_state and (perfetto_state.session_pid or perfetto_state.remote_config_path or perfetto_state.remote_trace_path):
            await _stop_perfetto_ring_buffer(device_serial, perfetto_state, preserve_trace=False)
        if continuous_perfetto_state:
            try:
                await _collect_continuous_perfetto_trace(
                    device_serial,
                    continuous_perfetto_state,
                    trace_artifacts,
                    trigger_reason="TASK_COMPLETED",
                )
            except Exception as exc:
                logger.warning(f"收集 Perfetto continuous trace 失败，已跳过: {exc}")
                await _stop_perfetto_ring_buffer(device_serial, continuous_perfetto_state, preserve_trace=False)

    if trace_artifacts:
        try:
            _analyze_exported_traces(package_name, trace_artifacts, jank_events)
        except Exception as exc:
            logger.warning(f"Perfetto trace 分析失败，已跳过: {exc}")

    summary = _compute_summary(
        perf_data,
        crash_events,
        jank_data=jank_data,
        jank_events=jank_events,
        trace_artifacts=trace_artifacts,
        enable_performance_monitor=enable_performance_monitor,
        enable_jank_frame_monitor=enable_jank_frame_monitor,
        perfetto_state=continuous_perfetto_state or perfetto_state,
    )
    summary["local_replay_enabled"] = bool(enable_local_replay)

    return {
        "performance_data": perf_data,
        "jank_data": jank_data,
        "jank_events": jank_events,
        "trace_artifacts": trace_artifacts,
        "crash_events": crash_events,
        "summary": summary,
    }


async def run_manual_fluency_session(
    device_serial: str,
    package_name: str,
    stop_event: asyncio.Event,
    task_id: Optional[int] = None,
    enable_performance_monitor: bool = True,
    enable_jank_frame_monitor: bool = True,
    capture_log: bool = True,
    auto_launch_app: bool = True,
) -> Dict:
    """
    手动流畅度录制会话：
    - 不注入 Monkey/Fastbot 随机事件
    - 仅在用户手动操作期间持续采集性能、gfxinfo 和 Perfetto
    - stop_event 被外部置位后完成收尾并输出标准报告数据
    """
    perf_data: List[Dict] = []
    jank_data: List[Dict] = []
    jank_events: List[Dict] = []
    trace_artifacts: List[Dict] = []
    crash_events: List[Dict] = []
    perfetto_state: Optional[PerfettoSessionState] = None
    continuous_perfetto_state: Optional[PerfettoSessionState] = None
    trace_capture_tasks: List[asyncio.Task] = []

    if auto_launch_app:
        try:
            await _adb_shell(
                device_serial,
                f"monkey -p {package_name} -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1",
                timeout=ADB_MONITOR_COMMAND_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.warning(f"手动流畅度录制自动拉起应用失败: {exc}")

    await _adb_shell(
        device_serial,
        "logcat -c",
        timeout=ADB_MONITOR_COMMAND_TIMEOUT_SECONDS,
    )
    logger.info("手动流畅度录制已清空 logcat 缓冲区")

    monitor_tasks = []
    if enable_performance_monitor:
        monitor_tasks.append(asyncio.create_task(
            _monitor_performance(device_serial, package_name, stop_event, perf_data)
        ))
    if enable_jank_frame_monitor:
        report_dir = _build_fastbot_report_dir(task_id)
        perfetto_state = PerfettoSessionState(report_dir=report_dir, capture_mode="diagnostic")
        try:
            perfetto_state = await _detect_perfetto_support(device_serial, report_dir)
            perfetto_state.capture_mode = "diagnostic"
            if perfetto_state.frame_timeline_supported:
                continuous_perfetto_state = await _detect_perfetto_support(device_serial, report_dir)
                continuous_perfetto_state.capture_mode = "continuous"
                await _start_perfetto_ring_buffer(device_serial, package_name, continuous_perfetto_state)
        except Exception as exc:
            logger.warning(f"初始化手动流畅度录制 Perfetto 失败，已降级为 gfxinfo-only: {exc}")
        monitor_tasks.append(asyncio.create_task(
            _monitor_jank(
                device_serial,
                package_name,
                stop_event,
                jank_data,
                jank_events,
                perf_data=perf_data,
                trace_artifacts=trace_artifacts,
                trace_capture_tasks=trace_capture_tasks,
                perfetto_state=perfetto_state,
            )
        ))
    monitor_tasks.append(asyncio.create_task(
        _monitor_logcat(
            device_serial,
            package_name,
            stop_event,
            crash_events,
            capture_log,
            abort_on_crash=False,
            abort_event=None,
        )
    ))

    try:
        await stop_event.wait()
    finally:
        await _await_task_group(
            monitor_tasks,
            timeout=MONITOR_TASK_SHUTDOWN_TIMEOUT_SECONDS,
            label="手动流畅度监控协程",
        )
        if trace_capture_tasks:
            await _await_task_group(
                trace_capture_tasks,
                timeout=TRACE_TASK_SHUTDOWN_TIMEOUT_SECONDS,
                label="手动流畅度 Trace 导出协程",
            )
        if perfetto_state and (perfetto_state.session_pid or perfetto_state.remote_config_path or perfetto_state.remote_trace_path):
            await _stop_perfetto_ring_buffer(device_serial, perfetto_state, preserve_trace=False)
        if continuous_perfetto_state:
            try:
                await _collect_continuous_perfetto_trace(
                    device_serial,
                    continuous_perfetto_state,
                    trace_artifacts,
                    trigger_reason="MANUAL_SESSION_COMPLETED",
                )
            except Exception as exc:
                logger.warning(f"收集手动流畅度 continuous trace 失败，已跳过: {exc}")
                await _stop_perfetto_ring_buffer(device_serial, continuous_perfetto_state, preserve_trace=False)

    if trace_artifacts:
        try:
            _analyze_exported_traces(package_name, trace_artifacts, jank_events)
        except Exception as exc:
            logger.warning(f"手动流畅度 trace 分析失败，已跳过: {exc}")

    summary = _compute_summary(
        perf_data,
        crash_events,
        jank_data=jank_data,
        jank_events=jank_events,
        trace_artifacts=trace_artifacts,
        enable_performance_monitor=enable_performance_monitor,
        enable_jank_frame_monitor=enable_jank_frame_monitor,
        perfetto_state=continuous_perfetto_state or perfetto_state,
    )

    return {
        "performance_data": perf_data,
        "jank_data": jank_data,
        "jank_events": jank_events,
        "trace_artifacts": trace_artifacts,
        "crash_events": crash_events,
        "summary": summary,
    }


def _compute_jank_summary(
    jank_data: List[Dict],
    jank_events: List[Dict],
    trace_artifacts: Optional[List[Dict]] = None,
    enable_jank_frame_monitor: bool = False,
    frame_timeline_supported: bool = False,
    jank_monitoring_mode: str = "disabled",
) -> Dict:
    trace_count = len(trace_artifacts or [])
    analyzed_trace_count = sum(1 for artifact in (trace_artifacts or []) if artifact.get("analysis_status") == "ANALYZED")
    active_samples = [sample for sample in jank_data if not bool(sample.get("is_idle"))]
    active_throughputs = [
        float(sample.get("render_throughput", sample.get("fps", 0)) or 0)
        for sample in active_samples
    ]
    active_jank_rates = [float(sample.get("jank_rate", 0) or 0) for sample in active_samples]
    all_jank_rates = [float(sample.get("jank_rate", 0) or 0) for sample in jank_data]

    peak_window = {}
    if active_samples:
        peak_sample = max(active_samples, key=lambda sample: float(sample.get("jank_rate", 0) or 0))
        peak_window = {
            "time": peak_sample.get("time"),
            "jank_rate": round(float(peak_sample.get("jank_rate", 0) or 0), 4),
            "render_throughput": round(float(peak_sample.get("render_throughput", peak_sample.get("fps", 0)) or 0), 1),
            "total_frames": int(peak_sample.get("total_frames", 0) or 0),
        }

    if not jank_data:
        return {
            "avg_fps": 0,
            "min_fps": 0,
            "avg_render_throughput": 0,
            "min_render_throughput": 0,
            "avg_jank_rate": 0,
            "active_avg_jank_rate": 0,
            "max_jank_rate": 0,
            "peak_jank_rate_window": {},
            "total_jank_events": len(jank_events),
            "severe_jank_events": sum(1 for e in jank_events if e.get("severity") == "CRITICAL"),
            "trace_artifact_count": trace_count,
            "analyzed_trace_count": analyzed_trace_count,
            "frame_timeline_supported": frame_timeline_supported,
            "jank_monitoring_mode": jank_monitoring_mode if enable_jank_frame_monitor else "disabled",
            "active_sample_count": 0,
        }

    fps_values = [float(p.get("fps", 0) or 0) for p in jank_data]

    result = {
        "avg_fps": round(sum(fps_values) / len(fps_values), 1),
        "min_fps": round(min(fps_values), 1),
        "avg_render_throughput": round(sum(active_throughputs) / len(active_throughputs), 1) if active_throughputs else 0,
        "min_render_throughput": round(min(active_throughputs), 1) if active_throughputs else 0,
        "avg_jank_rate": round(sum(all_jank_rates) / len(all_jank_rates), 4),
        "active_avg_jank_rate": round(sum(active_jank_rates) / len(active_jank_rates), 4) if active_jank_rates else 0,
        "max_jank_rate": round(max(active_jank_rates), 4) if active_jank_rates else round(max(all_jank_rates), 4),
        "peak_jank_rate_window": peak_window,
        "total_jank_events": len(jank_events),
        "severe_jank_events": sum(1 for e in jank_events if e.get("severity") == "CRITICAL"),
        "trace_artifact_count": trace_count,
        "analyzed_trace_count": analyzed_trace_count,
        "frame_timeline_supported": frame_timeline_supported,
        "jank_monitoring_mode": jank_monitoring_mode,
        "active_sample_count": len(active_samples),
    }

    framestats_samples = [s for s in active_samples if s.get("source") == "framestats"]
    if framestats_samples:
        all_p50 = [s["frame_time_p50_ms"] for s in framestats_samples if "frame_time_p50_ms" in s]
        all_p95 = [s["frame_time_p95_ms"] for s in framestats_samples if "frame_time_p95_ms" in s]
        all_p99 = [s["frame_time_p99_ms"] for s in framestats_samples if "frame_time_p99_ms" in s]
        all_max = [s["frame_time_max_ms"] for s in framestats_samples if "frame_time_max_ms" in s]
        result["frame_time_p50_ms"] = round(sum(all_p50) / len(all_p50), 2) if all_p50 else None
        result["frame_time_p95_ms"] = round(max(all_p95), 2) if all_p95 else None
        result["frame_time_p99_ms"] = round(max(all_p99), 2) if all_p99 else None
        result["frame_time_max_ms"] = round(max(all_max), 2) if all_max else None

    return result


def _pick_trace_effective_fps(trace_artifacts: Optional[List[Dict]]) -> float:
    analyzed_artifacts = [
        artifact for artifact in (trace_artifacts or [])
        if artifact.get("analysis_status") == "ANALYZED"
        and isinstance(artifact.get("analysis"), dict)
        and isinstance((artifact.get("analysis") or {}).get("frame_stats"), dict)
    ]
    if not analyzed_artifacts:
        return 0.0

    preferred_artifacts = [
        artifact for artifact in analyzed_artifacts
        if str(artifact.get("capture_mode") or "") == "continuous"
    ] or analyzed_artifacts

    fps_values = [
        float(((artifact.get("analysis") or {}).get("frame_stats") or {}).get("effective_fps", 0) or 0)
        for artifact in preferred_artifacts
    ]
    fps_values = [value for value in fps_values if value > 0]
    if not fps_values:
        return 0.0
    return round(sum(fps_values) / len(fps_values), 1)


def _build_jank_verdict(
    jank_summary: Dict,
    trace_artifacts: Optional[List[Dict]] = None,
) -> Dict:
    active_avg_jank_rate = float(jank_summary.get("active_avg_jank_rate", 0) or 0)
    severe_jank_events = int(jank_summary.get("severe_jank_events", 0) or 0)
    effective_fps = _pick_trace_effective_fps(trace_artifacts)

    if severe_jank_events >= 3 or active_avg_jank_rate >= 0.2 or (effective_fps > 0 and effective_fps < 40):
        return {
            "level": "POOR",
            "label": "较差",
            "reason": "活跃渲染窗口内卡顿明显，已达到需要重点排查的程度。",
            "suggestion": "优先查看严重卡顿事件和 Perfetto Trace 的首要怀疑点。",
        }
    if severe_jank_events > 0 or active_avg_jank_rate >= 0.08 or (effective_fps > 0 and effective_fps < 55):
        return {
            "level": "FAIR",
            "label": "一般",
            "reason": "存在可感知卡顿，建议结合 Trace 进一步确认瓶颈窗口。",
            "suggestion": "重点关注活跃窗口平均卡顿率和最差窗口时间点。",
        }
    return {
        "level": "GOOD",
        "label": "良好",
        "reason": "活跃渲染窗口整体平稳，未发现明显严重卡顿。",
        "suggestion": "如需进一步优化，可继续关注偶发峰值窗口。",
    }


def _compute_summary(
    perf_data: List[Dict],
    crash_events: List[Dict],
    jank_data: Optional[List[Dict]] = None,
    jank_events: Optional[List[Dict]] = None,
    trace_artifacts: Optional[List[Dict]] = None,
    enable_performance_monitor: bool = True,
    enable_jank_frame_monitor: bool = False,
    perfetto_state: Optional[PerfettoSessionState] = None,
) -> Dict:
    """汇总性能与异常统计"""
    use_framestats = any(s.get("source") == "framestats" for s in (jank_data or []))
    jank_summary = _compute_jank_summary(
        jank_data or [],
        jank_events or [],
        trace_artifacts=trace_artifacts or [],
        enable_jank_frame_monitor=enable_jank_frame_monitor,
        frame_timeline_supported=bool(perfetto_state and perfetto_state.frame_timeline_supported),
        jank_monitoring_mode=_resolve_jank_monitoring_mode(
            enable_jank_frame_monitor,
            perfetto_state=perfetto_state,
            use_framestats=use_framestats,
        ),
    )
    crashes = sum(1 for e in crash_events if e["type"] == "CRASH")
    anrs = sum(1 for e in crash_events if e["type"] == "ANR")

    if not perf_data:
        summary = {
            "avg_cpu": 0, "max_cpu": 0,
            "avg_mem": 0, "max_mem": 0,
            "total_crashes": crashes, "total_anrs": anrs,
            "performance_monitor_enabled": enable_performance_monitor,
            "jank_frame_monitor_enabled": enable_jank_frame_monitor,
        }
        summary.update(jank_summary)
        summary["verdict"] = _build_jank_verdict(jank_summary, trace_artifacts=trace_artifacts or [])
        return summary

    cpus = [p["cpu"] for p in perf_data]
    mems = [p["mem"] for p in perf_data]

    summary = {
        "avg_cpu": round(sum(cpus) / len(cpus), 1),
        "max_cpu": round(max(cpus), 1),
        "avg_mem": round(sum(mems) / len(mems), 1),
        "max_mem": round(max(mems), 1),
        "total_crashes": crashes,
        "total_anrs": anrs,
        "performance_monitor_enabled": enable_performance_monitor,
        "jank_frame_monitor_enabled": enable_jank_frame_monitor,
    }
    summary.update(jank_summary)
    summary["verdict"] = _build_jank_verdict(jank_summary, trace_artifacts=trace_artifacts or [])
    return summary
