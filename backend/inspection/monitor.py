"""Parameterised reuse of Fastbot monitoring for inspection runs."""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.fastbot.adb import ADB_MONITOR_COMMAND_TIMEOUT_SECONDS, _adb_shell
from backend.fastbot.framestats import _monitor_jank
from backend.fastbot.logcat import _monitor_logcat
from backend.fastbot.perf_monitor import _monitor_performance
from backend.fastbot.perfetto import (
    PerfettoSessionState,
    _analyze_exported_traces,
    _collect_continuous_perfetto_trace,
    _detect_perfetto_support,
    _start_perfetto_ring_buffer,
    _stop_perfetto_ring_buffer,
)
from backend.fastbot.runner import (
    LOCAL_REPLAY_POST_ROLL_SEC,
    LOCAL_REPLAY_PRE_ROLL_SEC,
    LOCAL_REPLAY_SEGMENT_SEC,
)

logger = logging.getLogger(__name__)


class InspectionMonitorSession:
    """Run existing Crash/ANR and CPU/memory monitors in a side event loop."""

    def __init__(
        self,
        *,
        device_serial: str,
        package_name: str,
        run_id: int,
        report_dir: Path,
        capture_log: bool = True,
        enable_performance_monitor: bool = True,
        enable_jank_frame_monitor: bool = False,
        enable_perfetto_trace: bool = False,
        enable_local_replay: bool = True,
    ) -> None:
        self.device_serial = device_serial
        self.package_name = package_name
        self.run_id = run_id
        self.report_dir = Path(report_dir)
        self.capture_log = capture_log
        self.enable_performance_monitor = enable_performance_monitor
        self.enable_jank_frame_monitor = enable_jank_frame_monitor
        self.enable_perfetto_trace = (
            enable_perfetto_trace and enable_jank_frame_monitor
        )
        self.enable_local_replay = enable_local_replay
        self.crash_events: List[Dict[str, Any]] = []
        self.performance_data: List[Dict[str, Any]] = []
        self.jank_data: List[Dict[str, Any]] = []
        self.jank_events: List[Dict[str, Any]] = []
        self.trace_artifacts: List[Dict[str, Any]] = []
        self._thread_stop = threading.Event()
        self._ready = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._startup_error: Optional[BaseException] = None
        self._local_replay_started = False

    def start(self, timeout: float = 10.0) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(
            target=self._thread_main,
            name=f"inspection-monitor-{self.run_id}",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait(timeout=max(timeout, 0.1))
        if self._startup_error:
            raise RuntimeError(f"巡检监控启动失败: {self._startup_error}") from self._startup_error

    def stop(self, timeout: float = 15.0) -> None:
        self._thread_stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=max(timeout, 0.1))
        if thread and thread.is_alive():
            logger.warning("inspection monitor did not stop within timeout: run=%s", self.run_id)

    def snapshot_events(self, start_index: int = 0) -> List[Dict[str, Any]]:
        return [dict(item) for item in self.crash_events[max(0, start_index) :]]

    def capture_replay(self, event_type: str) -> Optional[Dict[str, Any]]:
        """Capture the configured pre/post-roll for a non-logcat fault."""
        if not self._local_replay_started:
            return None
        try:
            from backend.device_stream.manager import device_manager

            result = device_manager.capture_replay(
                self.device_serial,
                str(event_type or "FAULT").upper(),
                datetime.now().strftime("%H:%M:%S"),
            )
            return result.to_dict() if result else None
        except Exception as exc:
            logger.warning(
                "inspection replay capture failed: run=%s type=%s error=%s",
                self.run_id,
                event_type,
                exc,
            )
            return {"status": "FAILED", "error": str(exc)}

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except BaseException as exc:
            self._startup_error = exc
            logger.exception("inspection monitor crashed: run=%s", self.run_id)
        finally:
            self._ready.set()

    async def _run(self) -> None:
        stop_event = asyncio.Event()
        local_replay_started = False
        trace_capture_tasks: List[asyncio.Task] = []
        perfetto_state: Optional[PerfettoSessionState] = None
        continuous_perfetto_state: Optional[PerfettoSessionState] = None

        if self.enable_local_replay:
            try:
                from backend.device_stream.manager import device_manager

                await asyncio.to_thread(
                    device_manager.start_recording,
                    self.device_serial,
                    self.run_id,
                    str(self.report_dir),
                    LOCAL_REPLAY_PRE_ROLL_SEC,
                    LOCAL_REPLAY_POST_ROLL_SEC,
                    LOCAL_REPLAY_SEGMENT_SEC,
                )
                local_replay_started = True
                self._local_replay_started = True
            except Exception as exc:
                logger.warning(
                    "inspection local replay unavailable: run=%s error=%s",
                    self.run_id,
                    exc,
                )

        async def capture_replay(event_type: str, event_time: str) -> Optional[Dict[str, Any]]:
            if not local_replay_started:
                return None
            from backend.device_stream.manager import device_manager

            result = await asyncio.to_thread(
                device_manager.capture_replay,
                self.device_serial,
                event_type,
                event_time,
            )
            return result.to_dict() if result else None

        try:
            await _adb_shell(
                self.device_serial,
                "logcat -c",
                timeout=ADB_MONITOR_COMMAND_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.warning("inspection logcat clear failed: %s", exc)

        tasks = [
            asyncio.create_task(
                _monitor_logcat(
                    self.device_serial,
                    self.package_name,
                    stop_event,
                    self.crash_events,
                    self.capture_log,
                    abort_on_crash=False,
                    replay_callback=capture_replay if local_replay_started else None,
                )
            )
        ]
        if self.enable_performance_monitor:
            tasks.append(
                asyncio.create_task(
                    _monitor_performance(
                        self.device_serial,
                        self.package_name,
                        stop_event,
                        self.performance_data,
                    )
                )
            )
        if self.enable_jank_frame_monitor:
            if self.enable_perfetto_trace:
                perfetto_state = PerfettoSessionState(
                    report_dir=str(self.report_dir),
                    capture_mode="diagnostic",
                )
                try:
                    perfetto_state = await _detect_perfetto_support(
                        self.device_serial,
                        str(self.report_dir),
                    )
                    perfetto_state.capture_mode = "diagnostic"
                    if perfetto_state.frame_timeline_supported:
                        continuous_perfetto_state = await _detect_perfetto_support(
                            self.device_serial,
                            str(self.report_dir),
                        )
                        continuous_perfetto_state.capture_mode = "continuous"
                        await _start_perfetto_ring_buffer(
                            self.device_serial,
                            self.package_name,
                            continuous_perfetto_state,
                        )
                except Exception as exc:
                    logger.warning(
                        "inspection Perfetto unavailable; using gfxinfo only: %s",
                        exc,
                    )
            tasks.append(
                asyncio.create_task(
                    _monitor_jank(
                        self.device_serial,
                        self.package_name,
                        stop_event,
                        self.jank_data,
                        self.jank_events,
                        perf_data=self.performance_data,
                        trace_artifacts=self.trace_artifacts,
                        trace_capture_tasks=trace_capture_tasks,
                        perfetto_state=perfetto_state,
                    )
                )
            )
        self._ready.set()
        try:
            await asyncio.to_thread(self._thread_stop.wait)
        finally:
            stop_event.set()
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=12.0,
                )
            except asyncio.TimeoutError:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            if trace_capture_tasks:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(
                            *trace_capture_tasks,
                            return_exceptions=True,
                        ),
                        timeout=15.0,
                    )
                except asyncio.TimeoutError:
                    for task in trace_capture_tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(
                        *trace_capture_tasks,
                        return_exceptions=True,
                    )
            if perfetto_state and (
                perfetto_state.session_pid
                or perfetto_state.remote_config_path
                or perfetto_state.remote_trace_path
            ):
                try:
                    await _stop_perfetto_ring_buffer(
                        self.device_serial,
                        perfetto_state,
                        preserve_trace=False,
                    )
                except Exception:
                    logger.exception(
                        "inspection diagnostic Perfetto cleanup failed: run=%s",
                        self.run_id,
                    )
            if continuous_perfetto_state:
                try:
                    await _collect_continuous_perfetto_trace(
                        self.device_serial,
                        continuous_perfetto_state,
                        self.trace_artifacts,
                        trigger_reason="INSPECTION_COMPLETED",
                    )
                except Exception:
                    logger.exception(
                        "inspection continuous Perfetto export failed: run=%s",
                        self.run_id,
                    )
                    try:
                        await _stop_perfetto_ring_buffer(
                            self.device_serial,
                            continuous_perfetto_state,
                            preserve_trace=False,
                        )
                    except Exception:
                        pass
            if self.trace_artifacts:
                try:
                    await asyncio.to_thread(
                        _analyze_exported_traces,
                        self.package_name,
                        self.trace_artifacts,
                        self.jank_events,
                    )
                except Exception:
                    logger.exception(
                        "inspection Perfetto analysis failed: run=%s",
                        self.run_id,
                    )
            if local_replay_started:
                try:
                    from backend.device_stream.manager import device_manager

                    await asyncio.to_thread(
                        device_manager.stop_recording,
                        self.device_serial,
                    )
                except Exception:
                    logger.exception(
                        "inspection local replay stop failed: run=%s", self.run_id
                    )
            metrics = {
                "performance_data": self.performance_data,
                "jank_data": self.jank_data,
                "jank_events": self.jank_events,
                "trace_artifacts": self.trace_artifacts,
            }
            try:
                (self.report_dir / "metrics.json").write_text(
                    json.dumps(metrics, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8",
                )
            except Exception:
                logger.exception(
                    "inspection monitor metrics persistence failed: run=%s",
                    self.run_id,
                )
