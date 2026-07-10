"""
Fastbot 智能探索执行引擎 — 兼容入口（re-export shim）。

实现已按职责拆分到 backend/fastbot/ 包：
- backend.fastbot.adb          ADB 协程工具
- backend.fastbot.reporting    报告目录管理
- backend.fastbot.perfetto     Perfetto trace 会话与分析
- backend.fastbot.perf_monitor CPU/内存采集与 gfxinfo 解析
- backend.fastbot.framestats   逐帧解析与卡顿监控
- backend.fastbot.logcat       崩溃/ANR logcat 监控
- backend.fastbot.monkey       Fastbot 资源与 Monkey 命令
- backend.fastbot.startup      冷热启动专项
- backend.fastbot.summary      报告汇总
- backend.fastbot.runner       顶层任务编排

本模块仅保留旧导入路径 `backend.fastbot_runner` 的名字兼容。注意：对本模块
属性打 patch 不会影响新模块内部的相互调用，测试请直接 patch 新模块路径
（例如 patch("backend.fastbot.adb._adb_shell")，且以被测函数所在模块为准）。
"""
import logging

from backend.fastbot.adb import (  # noqa: F401
    ADB_MONITOR_COMMAND_TIMEOUT_SECONDS,
    _adb_pull,
    _adb_push,
    _adb_shell,
    _adb_shell_result,
    _check_remote_file,
    _get_device_sdk_int,
    _terminate_subprocess,
)
from backend.fastbot.reporting import (  # noqa: F401
    FASTBOT_REPORTS_DIR,
    _build_fastbot_report_dir,
)
from backend.fastbot.perfetto import (  # noqa: F401
    FRAME_TIMELINE_MIN_SDK_INT,
    JANK_DIAGNOSTIC_TRACE_DURATION_SECONDS,
    JANK_MAX_TRACE_EXPORTS,
    JANK_TRACE_EXPORT_COOLDOWN_SECONDS,
    PERFETTO_CONTINUOUS_FILE_WRITE_PERIOD_MS,
    PERFETTO_CONTINUOUS_MAX_FILE_SIZE_BYTES,
    PERFETTO_CONTINUOUS_META_BUFFER_KB,
    PERFETTO_CONTINUOUS_TRACE_BUFFER_KB,
    PERFETTO_META_BUFFER_KB,
    PERFETTO_MIN_SDK_INT,
    PERFETTO_REMOTE_CONFIG_DIR,
    PERFETTO_REMOTE_TRACE_DIR,
    PERFETTO_TRACE_BUFFER_KB,
    PerfettoSessionState,
    _analysis_status_to_event_status,
    _analyze_exported_traces,
    _build_perfetto_trace_config,
    _build_trace_artifact,
    _cleanup_perfetto_remote_files,
    _collect_continuous_perfetto_trace,
    _detect_perfetto_support,
    _export_perfetto_trace,
    _primary_trace_cause,
    _pull_perfetto_trace_to_local,
    _should_export_perfetto_trace,
    _start_perfetto_ring_buffer,
    _stop_perfetto_ring_buffer,
    _wait_for_perfetto_trace_finalize,
)
from backend.fastbot.perf_monitor import (  # noqa: F401
    FRAME_DEADLINE_MISSED_PATTERN,
    FROZEN_FRAMES_PATTERN,
    JANK_ACTIVE_FRAME_THRESHOLD,
    JANK_SAMPLE_INTERVAL_SECONDS,
    JANKY_FRAMES_PATTERN,
    MISSED_VSYNC_PATTERN,
    SLOW_BITMAP_PATTERN,
    SLOW_DRAW_PATTERN,
    SLOW_UI_THREAD_PATTERN,
    TOTAL_FRAMES_PATTERN,
    _extract_int,
    _monitor_performance,
    _parse_gfxinfo_output,
)
from backend.fastbot.framestats import (  # noqa: F401
    FRAMESTATS_COLUMN_COUNT_DEADLINE,
    FRAMESTATS_COLUMN_COUNT_LEGACY,
    FRAMESTATS_HEADER_MARKER,
    FRAMESTATS_IDLE_FRAME_THRESHOLD,
    FRAMESTATS_MIN_SDK_INT,
    FRAMESTATS_POLL_INTERVAL_SECONDS,
    JANK_CONSECUTIVE_WINDOWS_REQUIRED,
    JANK_CRITICAL_FPS_THRESHOLD,
    JANK_CRITICAL_RATE_THRESHOLD,
    JANK_EVENT_DEDUP_SECONDS,
    JANK_FRAME_MULTIPLIER,
    JANK_WARNING_FPS_THRESHOLD,
    JANK_WARNING_RATE_THRESHOLD,
    VSYNC_PERIOD_NS,
    FrameStatsSample,
    FramestatsMonitorState,
    _build_jank_event,
    _classify_jank_sample,
    _compute_framestats_sample,
    _default_framestats_col_map,
    _detect_vsync_period,
    _emit_jank_event,
    _extract_profiledata_section,
    _find_closest_perf_sample,
    _monitor_jank,
    _monitor_jank_framestats,
    _monitor_jank_legacy,
    _parse_framestats_output,
    _time_text_to_seconds,
)
from backend.fastbot.logcat import (  # noqa: F401
    ANR_PATTERN,
    ANR_PKG_PATTERN,
    CRASH_PATTERN,
    CRASH_SOURCE_TAG,
    DEDUP_COOLDOWN_SECONDS,
    LOGCAT_SNAPSHOT_TIMEOUT_SECONDS,
    PROC_LINE_PATTERN,
    _capture_logcat_snapshot,
    _monitor_logcat,
)
from backend.fastbot.monkey import (  # noqa: F401
    DEVICE_JAR_TARGET,
    DEVICE_JARS,
    DEVICE_LIBS_TARGET,
    FASTBOT_ASSETS_DIR,
    _build_monkey_command,
    push_fastbot_assets,
)
from backend.fastbot.startup import (  # noqa: F401
    AM_START_FIELD_PATTERN,
    STARTUP_DISPLAYED_PATTERN,
    STARTUP_FULLY_DRAWN_PATTERN,
    _capture_startup_perfetto_trace,
    _compute_startup_aggregate,
    _extract_startup_crash_events,
    _extract_startup_logcat_timings,
    _normalize_startup_component,
    _parse_am_start_output,
    _parse_duration_token_to_ms,
    _parse_resolved_activity,
    _resolve_launcher_activity,
    _round_optional_ms,
    _run_single_startup_iteration,
    _startup_percentile,
    _wait_for_startup_ready,
    _wait_for_startup_ready_sync,
    run_startup_task,
)
from backend.fastbot.summary import (  # noqa: F401
    _build_jank_verdict,
    _compute_jank_summary,
    _compute_summary,
    _pick_trace_effective_fps,
    _resolve_jank_monitoring_mode,
)
from backend.fastbot.runner import (  # noqa: F401
    LOCAL_REPLAY_POST_ROLL_SEC,
    LOCAL_REPLAY_PRE_ROLL_SEC,
    LOCAL_REPLAY_SEGMENT_SEC,
    MONITOR_TASK_SHUTDOWN_TIMEOUT_SECONDS,
    TRACE_TASK_SHUTDOWN_TIMEOUT_SECONDS,
    _await_task_group,
    run_fastbot_task,
    run_manual_fluency_session,
)

logger = logging.getLogger("FastbotRunner")
