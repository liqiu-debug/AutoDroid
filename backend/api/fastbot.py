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
from typing import Dict, List
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlmodel import Session, select

from backend.database import get_session, engine
from backend.models import FastbotTask, FastbotReport, User, Device
from backend.schemas import FastbotTaskCreate, FastbotTaskRead, FastbotReportRead, DeviceStatusRead
from backend.api import deps
from backend.device_stream.manager import device_manager

logger = logging.getLogger(__name__)

router = APIRouter()

# 内存级设备锁，记录哪些设备正在跑 Fastbot
_device_locks: Dict[str, int] = {}  # serial -> task_id


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
    return {"success": True}


@router.get("/reports/{task_id}", response_model=FastbotReportRead)
def get_report(task_id: int, session: Session = Depends(get_session)):
    """获取指定任务的性能报告"""
    report = session.exec(
        select(FastbotReport).where(FastbotReport.task_id == task_id)
    ).first()
    if not report:
        raise HTTPException(status_code=404, detail="报告不存在")

    result = {
        "id": report.id,
        "task_id": report.task_id,
        "performance_data": json.loads(report.performance_data) if report.performance_data else [],
        "crash_events": json.loads(report.crash_events) if report.crash_events else [],
        "summary": json.loads(report.summary) if report.summary else {},
        "created_at": report.created_at,
    }
    return result


from sqlmodel import Session, select
from backend.database import get_session
from backend.models import Device
from backend.schemas import DeviceRead

@router.get("/devices", response_model=List[DeviceRead])
async def list_devices_with_status(session: Session = Depends(get_session)):
    """获取所有设备，包含下线设备并动态附加 Fastbot 占用状态，实时检查设备在线状态"""
    devices = session.exec(select(Device).order_by(Device.status, Device.model)).all()
    
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
    except Exception as e:
        logger.warning(f"检查设备在线状态失败: {e}")
    
    result = []
    for d in devices:
        # Convert to dict to override status if necessary without persisting
        d_dict = d.dict()
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

    background_tasks.add_task(_execute_fastbot_background, task.id, event_weights)

    return task


def _execute_fastbot_background(task_id: int, event_weights: Dict = None):
    """后台线程入口：跑 asyncio 事件循环执行 Fastbot"""
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_execute_fastbot_async(task_id, event_weights))
    finally:
        loop.close()


async def _execute_fastbot_async(task_id: int, event_weights: Dict = None):
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
        result = await run_fastbot_task(
            device_serial=serial,
            package_name=pkg,
            duration=dur,
            throttle=throt,
            ignore_crashes=ign_crash,
            capture_log=cap_log,
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
