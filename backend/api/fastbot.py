"""
Fastbot 智能探索 API 路由

提供：
- 任务 CRUD
- 设备排他锁
- 异步执行引擎调度
- 报告查询
"""
import json
import asyncio
import logging
import os
import re
import shutil
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse
from sqlmodel import Session, select

from backend.database import get_session, engine
from backend.models import FastbotTask, FastbotReport, User, Device
from backend.schemas import (
    FastbotTaskCreate,
    FastbotTaskRead,
    FastbotReportRead,
    FluencySessionRead,
    FluencySessionStartRequest,
    FluencyMarkerCreate,
    DeviceRead,
    DeviceStatusRead,
    JankAiSummaryRequest,
    JankAiSummaryResponse,
)
from backend.api import deps
from backend.device_stream.manager import device_manager
from backend.device_stream.recorder import transcode_h264_to_mp4
from backend.jank_ai_service import summarize_jank_analysis
from backend.utils.pydantic_compat import dump_model

logger = logging.getLogger(__name__)

router = APIRouter()
PROJECT_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
FASTBOT_REPORTS_DIR = os.path.join(PROJECT_ROOT, "reports", "fastbot")

# 内存级设备锁，记录哪些设备正在跑 Fastbot
_device_locks: Dict[str, int] = {}  # serial -> task_id


@dataclass
class _FluencyRuntime:
    task_id: int
    package_name: str
    device_serial: str
    capture_log: bool
    enable_performance_monitor: bool
    enable_jank_frame_monitor: bool
    auto_launch_app: bool
    loop: Optional[asyncio.AbstractEventLoop] = None
    stop_event: Optional[asyncio.Event] = None
    ready_event: threading.Event = field(default_factory=threading.Event)
    done_event: threading.Event = field(default_factory=threading.Event)
    markers: List[Dict[str, Any]] = field(default_factory=list)
    markers_lock: threading.Lock = field(default_factory=threading.Lock)
    error: str = ""

    def add_marker(self, marker: Dict[str, Any]) -> None:
        with self.markers_lock:
            self.markers.append(marker)

    def snapshot_markers(self) -> List[Dict[str, Any]]:
        with self.markers_lock:
            return [dict(item) for item in self.markers]


_fluency_runtimes: Dict[int, _FluencyRuntime] = {}


def _ensure_fastbot_android_device(device: Device) -> None:
    platform = str(device.platform or "android").strip().lower()
    if platform != "android":
        raise HTTPException(
            status_code=400,
            detail="P3001_FASTBOT_ANDROID_ONLY: Fastbot 仅支持 Android 设备。",
        )


def _artifact_needs_trace_analysis(artifact: Dict) -> bool:
    if not artifact.get("path"):
        return False
    if not artifact.get("analysis_status"):
        return True

    analysis = artifact.get("analysis") or {}
    frame_stats = analysis.get("frame_stats") or {}
    frame_timeline_series = analysis.get("frame_timeline_series") or []
    return (
        analysis.get("analysis_level") == "full"
        and (
            not frame_stats.get("effective_fps")
            or not isinstance(frame_timeline_series, list)
            or len(frame_timeline_series) == 0
        )
    )


def _ensure_report_trace_analysis(
    task_id: int,
    report: FastbotReport,
    session: Session,
):
    jank_events = json.loads(report.jank_events) if report.jank_events else []
    trace_artifacts = json.loads(report.trace_artifacts) if report.trace_artifacts else []
    summary = json.loads(report.summary) if report.summary else {}

    needs_trace_analysis = any(_artifact_needs_trace_analysis(artifact) for artifact in trace_artifacts)
    if needs_trace_analysis:
        task = session.get(FastbotTask, task_id)
        if task and task.package_name:
            try:
                from backend.fastbot_runner import _analyze_exported_traces

                _analyze_exported_traces(task.package_name, trace_artifacts, jank_events)
                summary["analyzed_trace_count"] = sum(
                    1 for artifact in trace_artifacts if artifact.get("analysis_status") == "ANALYZED"
                )
                report.jank_events = json.dumps(jank_events)
                report.trace_artifacts = json.dumps(trace_artifacts)
                report.summary = json.dumps(summary)
                session.add(report)
                session.commit()
                session.refresh(report)
            except Exception:
                session.rollback()

    return jank_events, trace_artifacts, summary


# ADB 异步工具函数
async def _run_adb_command(*args: str, timeout: int = 15) -> bytes:
    """异步执行 ADB 命令并返回 stdout 字节流"""
    import subprocess
    cmd = ["adb"] + list(args)
    logger.info(f"执行 ADB 命令: {' '.join(cmd)}")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
        if proc.returncode != 0:
            err_msg = stderr.decode("utf-8", errors="replace").strip()
            logger.warning(f"ADB 命令失败 (rc={proc.returncode}): {err_msg}")
            return b""
        return stdout
    except asyncio.TimeoutError:
        logger.error(f"ADB 命令超时: {' '.join(cmd)}")
        return b""
    except Exception as e:
        logger.warning(f"ADB 命令执行异常: {e}")
        return b""

async def _get_online_devices() -> set:
    """获取当前在线设备 serial 列表"""
    try:
        raw = await _run_adb_command("devices")
        if not raw:
            return set()
        lines = raw.decode("utf-8", errors="replace").strip().splitlines()
        online_serials = set()
        for line in lines[1:]:
            parts = line.strip().split()
            if len(parts) >= 2 and parts[1] == "device":
                online_serials.add(parts[0])
        return online_serials
    except Exception as e:
        logger.warning(f"获取在线设备失败: {e}")
        return set()


def _is_device_busy(serial: str) -> bool:
    return serial in _device_locks


def _lock_device(serial: str, task_id: int):
    _device_locks[serial] = task_id


def _unlock_device(serial: str):
    _device_locks.pop(serial, None)


def _extract_marker_payload(summary: Dict[str, Any]) -> List[Dict[str, Any]]:
    markers = summary.get("manual_markers") or []
    if not isinstance(markers, list):
        return []
    result = []
    for item in markers:
        if isinstance(item, dict) and item.get("label") and item.get("time"):
            result.append({
                "label": str(item.get("label")),
                "time": str(item.get("time")),
                "activity": str(item.get("activity") or ""),
            })
    return result


def _build_marker_segments(
    markers: List[Dict[str, Any]],
    started_at: Optional[datetime],
    finished_at: Optional[datetime],
) -> List[Dict[str, Any]]:
    if not started_at or not finished_at or not markers:
        return []

    timeline = []
    for marker in markers:
        time_text = str(marker.get("time") or "")
        try:
            hour, minute, second = [int(part) for part in time_text.split(":")]
        except Exception:
            continue
        timestamp = started_at.replace(hour=hour, minute=minute, second=second, microsecond=0)
        if timestamp < started_at:
            timestamp = timestamp + timedelta(days=1)
        timeline.append((timestamp, marker))

    timeline.sort(key=lambda item: item[0])
    segments = []
    for index, (segment_start, marker) in enumerate(timeline):
        segment_end = timeline[index + 1][0] if index + 1 < len(timeline) else finished_at
        if segment_end <= segment_start:
            continue
        segments.append({
            "label": marker.get("label"),
            "activity": marker.get("activity") or "",
            "start_time": segment_start.strftime("%H:%M:%S"),
            "end_time": segment_end.strftime("%H:%M:%S"),
            "duration_sec": int((segment_end - segment_start).total_seconds()),
        })
    return segments


def _delete_fastbot_artifacts_dir(task_id: int) -> None:
    report_dir = os.path.join(FASTBOT_REPORTS_DIR, str(task_id))
    if not os.path.isdir(report_dir):
        return
    shutil.rmtree(report_dir, ignore_errors=True)


def _upgrade_replay_payload_to_mp4(task_id: int, replay: Dict[str, Any]) -> bool:
    def _mark_upgrade_failed(error: str) -> bool:
        replay["status"] = "FAILED"
        replay["error"] = error
        replay["filename"] = ""
        replay["path"] = ""
        return True

    filename = str(replay.get("filename") or "")
    path = str(replay.get("path") or "")
    current_name = filename or (path.split("/")[-1] if path else "")
    if not current_name or not current_name.lower().endswith(".h264"):
        return False

    replay_dir = os.path.join(FASTBOT_REPORTS_DIR, str(task_id), "replays")
    source_path = os.path.join(replay_dir, current_name)
    if not os.path.isfile(source_path):
        return _mark_upgrade_failed("历史回放文件不存在，无法升级为 MP4")

    mp4_name = f"{os.path.splitext(current_name)[0]}.mp4"
    mp4_path = os.path.join(replay_dir, mp4_name)
    if not os.path.isfile(mp4_path):
        try:
            transcode_h264_to_mp4(source_path, mp4_path)
        except Exception:
            logger.exception("升级历史回放为 mp4 失败: task_id=%s file=%s", task_id, current_name)
            return _mark_upgrade_failed("历史回放升级为 MP4 失败")

    replay["status"] = "READY"
    replay["error"] = ""
    replay["filename"] = mp4_name
    replay["path"] = f"reports/fastbot/{task_id}/replays/{mp4_name}"
    return True


def _normalize_report_replays(task_id: int, crash_events: List[Dict[str, Any]]) -> bool:
    changed = False
    for event in crash_events:
        replay = event.get("replay")
        if not isinstance(replay, dict):
            continue
        if _upgrade_replay_payload_to_mp4(task_id, replay):
            changed = True
    return changed


async def _get_current_activity(device_serial: str) -> str:
    raw = await _run_adb_command("-s", device_serial, "shell", "dumpsys", "activity", "activities")
    if not raw:
        return ""
    text = raw.decode("utf-8", errors="replace")
    match = None
    for pattern in [
        r"mResumedActivity:.*?\s([A-Za-z0-9_.$]+/[A-Za-z0-9_.$]+)",
        r"topResumedActivity=.*?\s([A-Za-z0-9_.$]+/[A-Za-z0-9_.$]+)",
    ]:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return ""


def _build_fluency_session_read(
    task: FastbotTask,
    report: Optional[FastbotReport] = None,
    runtime: Optional[_FluencyRuntime] = None,
) -> Dict[str, Any]:
    summary = json.loads(report.summary) if report and report.summary else {}
    markers = _extract_marker_payload(summary)
    if runtime is not None:
        markers = runtime.snapshot_markers()

    return {
        "task_id": task.id,
        "package_name": task.package_name,
        "device_serial": task.device_serial,
        "status": task.status,
        "executor_name": task.executor_name,
        "created_at": task.created_at,
        "started_at": task.started_at,
        "finished_at": task.finished_at,
        "report_ready": bool(report),
        "marker_count": len(markers),
        "markers": markers,
        "summary": summary if summary else None,
    }


@router.get("/tasks", response_model=List[FastbotTaskRead])
def list_tasks(
    skip: int = 0,
    limit: int = 50,
    session: Session = Depends(get_session),
):
    """获取任务历史列表"""
    tasks = session.exec(
        select(FastbotTask).order_by(FastbotTask.id.desc()).offset(skip).limit(limit)
    ).all()
    return tasks


@router.get("/tasks/{task_id}", response_model=FastbotTaskRead)
def get_task(task_id: int, session: Session = Depends(get_session)):
    task = session.get(FastbotTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


@router.delete("/tasks/{task_id}")
def delete_task(
    task_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    task = session.get(FastbotTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.status == "RUNNING":
        raise HTTPException(status_code=400, detail="运行中的任务无法删除")

    report = session.exec(
        select(FastbotReport).where(FastbotReport.task_id == task_id)
    ).first()
    if report:
        session.delete(report)

    session.delete(task)
    session.commit()
    _delete_fastbot_artifacts_dir(task_id)
    return {"success": True}


@router.get("/reports/{task_id}", response_model=FastbotReportRead)
def get_report(task_id: int, session: Session = Depends(get_session)):
    """获取指定任务的性能报告"""
    report = session.exec(
        select(FastbotReport).where(FastbotReport.task_id == task_id)
    ).first()
    if not report:
        raise HTTPException(status_code=404, detail="报告不存在")

    _ensure_report_trace_analysis(task_id, report, session)

    crash_events = json.loads(report.crash_events) if report.crash_events else []
    if _normalize_report_replays(task_id, crash_events):
        report.crash_events = json.dumps(crash_events)
        session.add(report)
        session.commit()
        session.refresh(report)

    result = {
        "id": report.id,
        "task_id": report.task_id,
        "performance_data": json.loads(report.performance_data) if report.performance_data else [],
        "jank_data": json.loads(report.jank_data) if report.jank_data else [],
        "jank_events": json.loads(report.jank_events) if report.jank_events else [],
        "trace_artifacts": json.loads(report.trace_artifacts) if report.trace_artifacts else [],
        "crash_events": crash_events,
        "summary": json.loads(report.summary) if report.summary else {},
        "created_at": report.created_at,
    }
    return result


@router.get("/fluency/sessions", response_model=List[FluencySessionRead])
def list_fluency_sessions(
    limit: int = 20,
    session: Session = Depends(get_session),
):
    tasks = session.exec(
        select(FastbotTask).order_by(FastbotTask.id.desc()).limit(max(limit * 4, 20))
    ).all()
    reports = session.exec(
        select(FastbotReport).order_by(FastbotReport.id.desc()).limit(max(limit * 4, 20))
    ).all()
    report_by_task = {report.task_id: report for report in reports}

    results: List[Dict[str, Any]] = []
    for task in tasks:
        report = report_by_task.get(task.id)
        runtime = _fluency_runtimes.get(task.id)
        summary = json.loads(report.summary) if report and report.summary else {}
        is_manual = bool(runtime) or str(summary.get("session_type") or "") == "fluency_manual"
        if not is_manual:
            continue
        results.append(_build_fluency_session_read(task, report=report, runtime=runtime))
        if len(results) >= limit:
            break
    return results


@router.post("/fluency/start", response_model=FluencySessionRead)
async def start_fluency_session(
    data: FluencySessionStartRequest,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    device = session.exec(select(Device).where(Device.serial == data.device_serial)).first()
    if not device:
        raise HTTPException(status_code=404, detail="设备不存在")

    _ensure_fastbot_android_device(device)

    if _is_device_busy(data.device_serial):
        raise HTTPException(status_code=400, detail="设备正忙，请稍后再试")

    online_serials = await _get_online_devices()
    if data.device_serial not in online_serials:
        raise HTTPException(status_code=400, detail="设备不在线或未就绪")

    task = FastbotTask(
        package_name=data.package_name,
        duration=0,
        throttle=0,
        ignore_crashes=True,
        capture_log=data.capture_log,
        device_serial=data.device_serial,
        status="RUNNING",
        executor_id=current_user.id,
        executor_name=current_user.full_name or current_user.username,
        started_at=datetime.now(),
    )
    session.add(task)
    session.commit()
    session.refresh(task)

    runtime = _FluencyRuntime(
        task_id=task.id,
        package_name=data.package_name,
        device_serial=data.device_serial,
        capture_log=data.capture_log,
        enable_performance_monitor=data.enable_performance_monitor,
        enable_jank_frame_monitor=data.enable_jank_frame_monitor,
        auto_launch_app=data.auto_launch_app,
    )
    _fluency_runtimes[task.id] = runtime
    _lock_device(data.device_serial, task.id)

    thread = threading.Thread(
        target=_execute_fluency_background,
        args=(task.id,),
        daemon=True,
    )
    thread.start()
    await asyncio.to_thread(runtime.ready_event.wait, 3)

    return _build_fluency_session_read(task, runtime=runtime)


@router.post("/fluency/{task_id}/markers", response_model=FluencySessionRead)
async def add_fluency_marker(
    task_id: int,
    data: FluencyMarkerCreate,
    session: Session = Depends(get_session),
):
    runtime = _fluency_runtimes.get(task_id)
    if not runtime or not runtime.loop:
        raise HTTPException(status_code=404, detail="当前录制会话不存在或已结束")

    task = session.get(FastbotTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    label = (data.label or "").strip()
    if not label:
        raise HTTPException(status_code=400, detail="打点标签不能为空")

    activity = await _get_current_activity(runtime.device_serial)
    runtime.add_marker({
        "label": label,
        "time": datetime.now().strftime("%H:%M:%S"),
        "activity": activity,
    })
    return _build_fluency_session_read(task, runtime=runtime)


@router.post("/fluency/{task_id}/stop", response_model=FluencySessionRead)
async def stop_fluency_session(
    task_id: int,
    session: Session = Depends(get_session),
):
    runtime = _fluency_runtimes.get(task_id)
    task = session.get(FastbotTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not runtime or not runtime.loop or not runtime.stop_event:
        report = session.exec(select(FastbotReport).where(FastbotReport.task_id == task_id)).first()
        return _build_fluency_session_read(task, report=report)

    runtime.loop.call_soon_threadsafe(runtime.stop_event.set)
    completed = await asyncio.to_thread(runtime.done_event.wait, 30)
    if not completed:
        raise HTTPException(status_code=504, detail="录制正在收尾，请稍后刷新会话列表查看结果")

    session.refresh(task)
    report = session.exec(select(FastbotReport).where(FastbotReport.task_id == task_id)).first()
    if runtime.error:
        raise HTTPException(status_code=500, detail=f"录制结束失败: {runtime.error}")
    return _build_fluency_session_read(task, report=report)


@router.post("/reports/{task_id}/trace_ai_summary", response_model=JankAiSummaryResponse)
async def analyze_trace_ai_summary(
    task_id: int,
    req: JankAiSummaryRequest,
    session: Session = Depends(get_session),
):
    report = session.exec(
        select(FastbotReport).where(FastbotReport.task_id == task_id)
    ).first()
    if not report:
        raise HTTPException(status_code=404, detail="报告不存在")

    task = session.get(FastbotTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    _ensure_report_trace_analysis(task_id, report, session)

    trace_artifacts = json.loads(report.trace_artifacts) if report.trace_artifacts else []
    trace_path = (req.trace_path or "").strip()
    artifact = next((item for item in trace_artifacts if str(item.get("path") or "") == trace_path), None)
    if not artifact:
        raise HTTPException(status_code=404, detail="指定 trace 不存在")

    if not artifact.get("analysis"):
        raise HTTPException(status_code=400, detail="当前 trace 还没有可用的结构化分析结果。")

    result = await summarize_jank_analysis(
        artifact=artifact,
        package_name=task.package_name,
        device_info=task.device_serial,
        session=session,
        force_refresh=req.force_refresh,
    )

    artifact["ai_summary"] = result["analysis_result"]
    artifact["ai_summary_cached"] = bool(result.get("cached"))
    report.trace_artifacts = json.dumps(trace_artifacts)
    session.add(report)
    session.commit()

    return JankAiSummaryResponse(
        success=True,
        analysis_result=result["analysis_result"],
        token_usage=int(result.get("token_usage") or 0),
        cached=bool(result.get("cached")),
    )


@router.get("/replays/{task_id}/{filename}")
def get_fastbot_replay_file(task_id: int, filename: str):
    if not filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="非法文件名")

    replay_path = os.path.join(FASTBOT_REPORTS_DIR, str(task_id), "replays", filename)
    replay_path = os.path.abspath(replay_path)
    expected_prefix = os.path.abspath(os.path.join(FASTBOT_REPORTS_DIR, str(task_id), "replays"))
    if not replay_path.startswith(expected_prefix):
        raise HTTPException(status_code=400, detail="非法文件路径")
    if not os.path.isfile(replay_path):
        raise HTTPException(status_code=404, detail="回放文件不存在")

    media_type = "video/mp4" if filename.lower().endswith(".mp4") else "application/octet-stream"
    return FileResponse(replay_path, media_type=media_type, filename=filename)

@router.get("/devices", response_model=List[DeviceRead])
async def list_devices_with_status(session: Session = Depends(get_session)):
    """获取所有设备，包含下线设备并动态附加 Fastbot 占用状态，实时检查设备在线状态"""
    devices = session.exec(select(Device).order_by(Device.status, Device.model)).all()
    devices = [
        d for d in devices if str(d.platform or "android").strip().lower() == "android"
    ]
    
    # 实时检查哪些设备在线
    try:
        online_serials = await _get_online_devices()
        
        # 更新设备状态
        status_updated = False
        for device in devices:
            if device.serial in online_serials:
                if device.status == "OFFLINE":
                    device.status = "IDLE"
                    device.updated_at = datetime.now()
                    session.add(device)
                    status_updated = True
            else:
                if device.status != "OFFLINE":
                    device.status = "OFFLINE"
                    device.updated_at = datetime.now()
                    session.add(device)
                    status_updated = True
        
        if status_updated:
            session.commit()
            # 重新查询以获取最新状态
            devices = session.exec(select(Device).order_by(Device.status, Device.model)).all()
            devices = [
                d for d in devices if str(d.platform or "android").strip().lower() == "android"
            ]
    except Exception as e:
        logger.warning(f"检查设备在线状态失败: {e}")
    
    result = []
    for d in devices:
        # Convert to dict to override status if necessary without persisting
        d_dict = dump_model(d)
        d_dict["ready"] = (d.status != "OFFLINE")
        
        if _is_device_busy(d.serial):
            d_dict["status"] = "FASTBOT_RUNNING"
        
        # Keep native FASTBOT_RUNNING status if it got stuck from a previous session
        if d.status == "FASTBOT_RUNNING" and not _is_device_busy(d.serial):
             d_dict["status"] = "IDLE"

        result.append(d_dict)
    return result


@router.post("/run", response_model=FastbotTaskRead)
async def run_fastbot(
    data: FastbotTaskCreate,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    """
    提交 Fastbot 运行任务。
    
    排他锁逻辑：若设备非空闲则拒绝。
    """
    device = session.exec(select(Device).where(Device.serial == data.device_serial)).first()
    if not device:
        raise HTTPException(status_code=404, detail="设备不存在")

    _ensure_fastbot_android_device(device)

    if _is_device_busy(data.device_serial):
        raise HTTPException(status_code=400, detail="设备正忙，请稍后再试")

    devices = device_manager.get_devices_list()
    device_found = any(d["serial"] == data.device_serial and d.get("ready") for d in devices)
    if not device_found:
        raise HTTPException(status_code=400, detail="设备不在线或未就绪")

    task = FastbotTask(
        package_name=data.package_name,
        duration=data.duration,
        throttle=data.throttle,
        ignore_crashes=data.ignore_crashes,
        capture_log=data.capture_log,
        device_serial=data.device_serial,
        status="RUNNING",
        executor_id=current_user.id,
        executor_name=current_user.full_name or current_user.username,
        started_at=datetime.now(),
    )
    event_weights = {
        "enable": data.enable_custom_event_weights,
        "pct_touch": data.pct_touch,
        "pct_motion": data.pct_motion,
        "pct_syskeys": data.pct_syskeys,
        "pct_majornav": data.pct_majornav,
    }
    session.add(task)
    session.commit()
    session.refresh(task)

    _lock_device(data.device_serial, task.id)

    monitor_options = {
        "enable_performance_monitor": data.enable_performance_monitor,
        "enable_jank_frame_monitor": data.enable_jank_frame_monitor,
        "enable_local_replay": data.enable_local_replay,
    }

    background_tasks.add_task(_execute_fastbot_background, task.id, event_weights, monitor_options)

    return task


def _execute_fastbot_background(
    task_id: int,
    event_weights: Dict = None,
    monitor_options: Dict = None,
):
    """后台线程入口：跑 asyncio 事件循环执行 Fastbot"""
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_execute_fastbot_async(task_id, event_weights, monitor_options))
    finally:
        loop.close()


async def _execute_fastbot_async(
    task_id: int,
    event_weights: Dict = None,
    monitor_options: Dict = None,
):
    """异步执行 Fastbot，使用 try...finally 确保设备锁释放"""
    from backend.fastbot_runner import run_fastbot_task
    from sqlmodel import Session as SQLSession

    with SQLSession(engine) as session:
        task = session.get(FastbotTask, task_id)
        if not task:
            return
        serial = task.device_serial
        pkg = task.package_name
        dur = task.duration
        throt = task.throttle
        ign_crash = task.ignore_crashes
        cap_log = task.capture_log
        if not task.started_at:
            task.started_at = datetime.now()
        if task.status != "RUNNING":
            task.status = "RUNNING"
        session.add(task)
        session.commit()

        # ★ 更新 Device 表状态为 BUSY
        device = session.exec(select(Device).where(Device.serial == serial)).first()
        if device:
            device.status = "BUSY"
            device.updated_at = datetime.now()
            session.add(device)
            session.commit()

    try:
        ew = event_weights or {}
        mo = monitor_options or {}
        result = await run_fastbot_task(
            device_serial=serial,
            package_name=pkg,
            duration=dur,
            throttle=throt,
            ignore_crashes=ign_crash,
            capture_log=cap_log,
            task_id=task_id,
            enable_performance_monitor=mo.get("enable_performance_monitor", True),
            enable_jank_frame_monitor=mo.get("enable_jank_frame_monitor", False),
            enable_local_replay=mo.get("enable_local_replay", True),
            enable_custom_event_weights=ew.get("enable", False),
            pct_touch=ew.get("pct_touch", 40),
            pct_motion=ew.get("pct_motion", 30),
            pct_syskeys=ew.get("pct_syskeys", 5),
            pct_majornav=ew.get("pct_majornav", 15),
        )

        with SQLSession(engine) as session:
            task = session.get(FastbotTask, task_id)
            if not task:
                return

            summary = result.get("summary", {})
            task.status = "COMPLETED"
            task.finished_at = datetime.now()
            task.total_crashes = summary.get("total_crashes", 0)
            task.total_anrs = summary.get("total_anrs", 0)
            session.add(task)

            report = FastbotReport(
                task_id=task_id,
                performance_data=json.dumps(result.get("performance_data", [])),
                jank_data=json.dumps(result.get("jank_data", [])),
                jank_events=json.dumps(result.get("jank_events", [])),
                trace_artifacts=json.dumps(result.get("trace_artifacts", [])),
                crash_events=json.dumps(result.get("crash_events", [])),
                summary=json.dumps(summary),
            )
            session.add(report)
            session.commit()

        logger.info(f"Fastbot 任务 #{task_id} 执行完成")

    except Exception as e:
        logger.error(f"Fastbot 任务 #{task_id} 执行失败: {e}")
        with SQLSession(engine) as session:
            task = session.get(FastbotTask, task_id)
            if task:
                task.status = "FAILED"
                task.finished_at = datetime.now()
                session.add(task)
                session.commit()
    finally:
        _unlock_device(serial)
        # ★ 恢复 Device 表状态为 IDLE
        try:
            with SQLSession(engine) as session:
                device = session.exec(select(Device).where(Device.serial == serial)).first()
                if device and device.status == "BUSY":
                    device.status = "IDLE"
                    device.updated_at = datetime.now()
                    session.add(device)
                    session.commit()
        except Exception:
            pass


def _execute_fluency_background(task_id: int):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_execute_fluency_async(task_id))
    finally:
        loop.close()


async def _execute_fluency_async(task_id: int):
    from backend.fastbot_runner import run_manual_fluency_session
    from sqlmodel import Session as SQLSession

    runtime = _fluency_runtimes.get(task_id)
    if not runtime:
        return

    runtime.loop = asyncio.get_running_loop()
    runtime.stop_event = asyncio.Event()
    runtime.ready_event.set()

    serial = runtime.device_serial
    try:
        with SQLSession(engine) as session:
            task = session.get(FastbotTask, task_id)
            if not task:
                return

            if not task.started_at:
                task.started_at = datetime.now()
            if task.status != "RUNNING":
                task.status = "RUNNING"
            session.add(task)

            device = session.exec(select(Device).where(Device.serial == serial)).first()
            if device:
                device.status = "BUSY"
                device.updated_at = datetime.now()
                session.add(device)
            session.commit()

        result = await run_manual_fluency_session(
            device_serial=runtime.device_serial,
            package_name=runtime.package_name,
            stop_event=runtime.stop_event,
            task_id=task_id,
            enable_performance_monitor=runtime.enable_performance_monitor,
            enable_jank_frame_monitor=runtime.enable_jank_frame_monitor,
            capture_log=runtime.capture_log,
            auto_launch_app=runtime.auto_launch_app,
        )

        with SQLSession(engine) as session:
            task = session.get(FastbotTask, task_id)
            if not task:
                return

            finished_at = datetime.now()
            summary = result.get("summary", {})
            markers = runtime.snapshot_markers()
            summary["session_type"] = "fluency_manual"
            summary["session_label"] = "手动流畅度录制"
            summary["manual_markers"] = markers
            summary["marker_count"] = len(markers)
            summary["marker_segments"] = _build_marker_segments(markers, task.started_at, finished_at)

            task.status = "COMPLETED"
            task.finished_at = finished_at
            task.total_crashes = summary.get("total_crashes", 0)
            task.total_anrs = summary.get("total_anrs", 0)
            session.add(task)

            report = FastbotReport(
                task_id=task_id,
                performance_data=json.dumps(result.get("performance_data", [])),
                jank_data=json.dumps(result.get("jank_data", [])),
                jank_events=json.dumps(result.get("jank_events", [])),
                trace_artifacts=json.dumps(result.get("trace_artifacts", [])),
                crash_events=json.dumps(result.get("crash_events", [])),
                summary=json.dumps(summary),
            )
            session.add(report)
            session.commit()
    except Exception as exc:
        runtime.error = str(exc)
        logger.error("手动流畅度会话 #%s 执行失败: %s", task_id, exc)
        with SQLSession(engine) as session:
            task = session.get(FastbotTask, task_id)
            if task:
                task.status = "FAILED"
                task.finished_at = datetime.now()
                session.add(task)
                session.commit()
    finally:
        _unlock_device(serial)
        try:
            with SQLSession(engine) as session:
                device = session.exec(select(Device).where(Device.serial == serial)).first()
                if device and device.status == "BUSY":
                    device.status = "IDLE"
                    device.updated_at = datetime.now()
                    session.add(device)
                    session.commit()
        except Exception:
            pass
        runtime.done_event.set()
        _fluency_runtimes.pop(task_id, None)
