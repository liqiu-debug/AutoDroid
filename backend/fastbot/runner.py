"""顶层编排：Fastbot 智能探索任务与手动流畅度录制会话。

核心功能：
- 自动挂载 Fastbot 所需 jar 包到手机
- 拼接并执行 Monkey 命令
- 双协程并发：主进程执行 + 子协程监控 CPU/Mem/Crash
- 通过 asyncio.Event 协调协程退出
"""
import asyncio
import logging
import os
from typing import Dict, List, Optional

from backend.fastbot.adb import ADB_MONITOR_COMMAND_TIMEOUT_SECONDS, _adb_shell
from backend.fastbot.framestats import _monitor_jank
from backend.fastbot.logcat import _monitor_logcat
from backend.fastbot.monkey import _build_monkey_command, push_fastbot_assets
from backend.fastbot.perf_monitor import _monitor_performance
from backend.fastbot.perfetto import (
    PerfettoSessionState,
    _analyze_exported_traces,
    _collect_continuous_perfetto_trace,
    _detect_perfetto_support,
    _start_perfetto_ring_buffer,
    _stop_perfetto_ring_buffer,
)
from backend.fastbot.reporting import _build_fastbot_report_dir
from backend.fastbot.summary import _compute_summary

logger = logging.getLogger("FastbotRunner")

LOCAL_REPLAY_PRE_ROLL_SEC = 30
LOCAL_REPLAY_POST_ROLL_SEC = 5
LOCAL_REPLAY_SEGMENT_SEC = 5
MONITOR_TASK_SHUTDOWN_TIMEOUT_SECONDS = 12
TRACE_TASK_SHUTDOWN_TIMEOUT_SECONDS = 15


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
    report_dir_override: Optional[str] = None,
    report_run_id: Optional[str] = None,
    crash_event_sink=None,
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
    report_dir = ""
    if enable_jank_frame_monitor or enable_local_replay:
        report_dir = (
            str(report_dir_override)
            if report_dir_override
            else _build_fastbot_report_dir(task_id, run_id=report_run_id)
        )
        os.makedirs(report_dir, exist_ok=True)
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
    logcat_kwargs = {
        "abort_on_crash": should_abort,
        "abort_event": abort_event,
        "replay_callback": _capture_local_replay if local_replay_started else None,
    }
    # Keep the historical call contract byte-for-byte compatible when no
    # external sink is requested.  Existing integrations commonly replace the
    # monitor with a callable that predates the optional event_sink keyword.
    if crash_event_sink is not None:
        logcat_kwargs["event_sink"] = crash_event_sink
    logcat_task = asyncio.create_task(
        _monitor_logcat(
            device_serial,
            package_name,
            stop_event,
            crash_events,
            capture_log,
            **logcat_kwargs,
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
