"""卡顿检测：framestats 逐帧解析、卡顿分级与监控协程（含 gfxinfo 降级路径）。"""
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

from backend.fastbot.adb import (
    ADB_MONITOR_COMMAND_TIMEOUT_SECONDS,
    _adb_shell,
    _get_device_sdk_int,
)
from backend.fastbot.perf_monitor import (
    JANK_ACTIVE_FRAME_THRESHOLD,
    JANK_SAMPLE_INTERVAL_SECONDS,
    _parse_gfxinfo_output,
)
from backend.fastbot.perfetto import (
    PerfettoSessionState,
    JANK_MAX_TRACE_EXPORTS,
    _export_perfetto_trace,
    _should_export_perfetto_trace,
)

logger = logging.getLogger("FastbotRunner")

JANK_WARNING_RATE_THRESHOLD = 0.08
JANK_CRITICAL_RATE_THRESHOLD = 0.15
JANK_WARNING_FPS_THRESHOLD = 45.0
JANK_CRITICAL_FPS_THRESHOLD = 30.0
JANK_CONSECUTIVE_WINDOWS_REQUIRED = 2
JANK_EVENT_DEDUP_SECONDS = 30

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
