"""
定时任务 API 路由

提供定时任务的 CRUD 和开关切换功能。
"""
import json
import logging
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select
from datetime import datetime

from backend.database import get_session
from backend.models import ScheduledTask, TestScenario, User
from backend.schemas import ScheduledTaskCreate, ScheduledTaskRead, ScheduledTaskUpdate
from backend.api import deps
from backend.scheduler_service import get_scheduler, SchedulerService

logger = logging.getLogger(__name__)

router = APIRouter()


def _run_scheduled_scenario(task_id: int):
    """调度器回调：根据任务类型执行 UI 场景或 Fastbot 探索"""
    from backend.api.scenarios import execute_scenario_batch_background
    from backend.models import TestExecution, TestResult
    from backend.notification_service import NotificationService
    from sqlmodel import Session as SQLSession
    from backend.database import engine

    with SQLSession(engine) as session:
        task = session.get(ScheduledTask, task_id)
        if not task:
            logger.error(f"[定时任务] 任务 #{task_id} 不存在")
            return
        scenario_id = task.scenario_id
        task_name = task.name
        
        device_serials = [s.strip() for s in task.device_serial.split(",")] if task.device_serial else []
        enable_notification = task.enable_notification
        config = {}
        if task.strategy_config:
            try:
                config = json.loads(task.strategy_config)
            except (json.JSONDecodeError, TypeError):
                config = {}

    task_type = config.get("_task_type", "ui")

    if task_type == "fastbot":
        fb_device_serial = device_serials[0] if device_serials else None
        _run_scheduled_fastbot(config, task_name, fb_device_serial, enable_notification)
    else:
        if scenario_id is None:
            logger.error(f"[定时任务] UI 任务 #{task_id} 缺少 scenario_id")
            return
            
        env_id = config.get("env_id")
        logger.info(f"[定时任务] 开始执行场景 #{scenario_id} (任务: {task_name}, env_id: {env_id}, device_serials: {device_serials})")
        
        batch_id = execute_scenario_batch_background(scenario_id, f"定时任务: {task_name}", env_id, device_serials)

        if enable_notification and batch_id:
            try:
                with SQLSession(engine) as session:
                    executions = session.exec(
                        select(TestExecution)
                        .where(TestExecution.batch_id == batch_id)
                    ).all()
                    
                    if executions:
                        all_passed = all(e.status == "PASS" for e in executions)
                        overall_status = "PASS" if all_passed else "FAIL"
                        max_duration = max((e.duration or 0 for e in executions), default=0)
                        
                        total_steps = 0
                        passed_steps = 0
                        errors = []
                        passed_devices = []
                        failed_devices = []
                        
                        for exec_record in executions:
                            device_name = exec_record.device_info or "Unknown"
                            if exec_record.status == "PASS":
                                passed_devices.append(device_name)
                            else:
                                failed_devices.append(device_name)
                                
                            results = session.exec(
                                select(TestResult)
                                .where(TestResult.execution_id == exec_record.id)
                            ).all()
                            total_steps += len(results)
                            passed_steps += sum(1 for r in results if r.status == "PASS")
                            errors.extend([r.error_message for r in results if r.error_message])
                            
                        failed_steps = total_steps - passed_steps
                        
                        first_exec_id = executions[0].id if executions else None

                        NotificationService.send_report_card(
                            task_name=task_name,
                            execution_id=first_exec_id,
                            status=overall_status,
                            total=total_steps,
                            passed=passed_steps,
                            failed=failed_steps,
                            duration_seconds=max_duration,
                            errors=errors,
                            device_count=len(executions),
                            passed_devices=passed_devices,
                            failed_devices=failed_devices
                        )
            except Exception as e:
                logger.error(f"[定时任务] 发送通知失败: {e}")


def _run_scheduled_fastbot(config: dict, task_name: str, device_serial: str, enable_notification: bool):
    """执行智能探索（Fastbot）定时任务"""
    from backend.api.fastbot import _execute_fastbot_background
    from backend.models import FastbotTask
    from backend.database import engine
    from sqlmodel import Session as SQLSession

    package_name = config.get("fb_package_name", "")
    duration = config.get("fb_duration", 600)
    throttle = config.get("fb_throttle", 500)
    device_serial = device_serial or config.get("fb_device_serial", "")

    if not device_serial:
        from backend.device_stream.manager import device_manager
        available = [d["serial"] for d in device_manager.get_devices_list() if d.get("ready")]
        if available:
            device_serial = available[0]
            logger.info(f"[定时任务] Fastbot 自动选择设备: {device_serial}")

    if not package_name or not device_serial:
        logger.error(f"[定时任务] Fastbot 任务缺少必要参数: package={package_name}, device={device_serial}")
        return

    ignore_crashes = config.get("fb_ignore_crashes", False)

    with SQLSession(engine) as session:
        fb_task = FastbotTask(
            package_name=package_name,
            duration=duration,
            throttle=throttle,
            ignore_crashes=ignore_crashes,
            device_serial=device_serial,
            status="RUNNING",
            executor_name=f"定时任务: {task_name}",
            started_at=datetime.now(),
        )
        session.add(fb_task)
        session.commit()
        session.refresh(fb_task)
        fb_task_id = fb_task.id

    logger.info(f"[定时任务] 开始智能探索 (任务: {task_name}, Fastbot #{fb_task_id})")
    _execute_fastbot_background(fb_task_id)

    if enable_notification:
        try:
            from backend.notification_service import NotificationService
            from backend.models import FastbotTask, FastbotReport

            with SQLSession(engine) as session:
                fb = session.get(FastbotTask, fb_task_id)
                if fb:
                    duration_secs = 0.0
                    if fb.started_at and fb.finished_at:
                        duration_secs = (fb.finished_at - fb.started_at).total_seconds()

                    summary = {}
                    report = session.exec(
                        select(FastbotReport).where(FastbotReport.task_id == fb_task_id)
                    ).first()
                    if report and report.summary:
                        import json as _json
                        try:
                            summary = _json.loads(report.summary)
                        except Exception:
                            pass

                    from backend.models import Device
                    device_display_name = fb.device_serial or "Unknown"
                    if fb.device_serial:
                        dev = session.exec(select(Device).where(Device.serial == fb.device_serial)).first()
                        if dev:
                            name_part = dev.custom_name or dev.market_name or dev.model
                            if name_part:
                                device_display_name = name_part
                    
                    NotificationService.send_fastbot_report_card(
                        task_name=task_name,
                        fastbot_task_id=fb_task_id,
                        package_name=fb.package_name,
                        device_serial=device_display_name,
                        status=fb.status,
                        duration_seconds=duration_secs,
                        total_crashes=fb.total_crashes,
                        total_anrs=fb.total_anrs,
                        avg_cpu=summary.get("avg_cpu", 0),
                        max_cpu=summary.get("max_cpu", 0),
                        avg_mem=summary.get("avg_mem", 0),
                        max_mem=summary.get("max_mem", 0),
                    )
        except Exception as e:
            logger.error(f"[定时任务] 智能探索通知发送失败: {e}")


def _task_to_read(task: ScheduledTask, scenario_name: str = "") -> dict:
    """将 ScheduledTask 转为 ScheduledTaskRead 兼容的字典"""
    config = {}
    if task.strategy_config:
        try:
            config = json.loads(task.strategy_config)
        except (json.JSONDecodeError, TypeError):
            config = {}

    scheduler = get_scheduler()
    next_run = scheduler.get_next_run_time(task.id) or task.next_run_time

    formatted = SchedulerService.format_schedule(task.strategy, config)

    # ONCE 任务过期检测
    if task.strategy == "ONCE" and not next_run:
        formatted = "⏹ 已过期"
    elif task.strategy == "ONCE" and next_run:
        if isinstance(next_run, datetime) and next_run < datetime.now(next_run.tzinfo):
            formatted = "⏹ 已过期"

    return {
        "id": task.id,
        "name": task.name,
        "scenario_id": task.scenario_id,
        "device_serials": [s.strip() for s in task.device_serial.split(",")] if task.device_serial else [],
        "strategy": task.strategy,
        "strategy_config": config,
        "is_active": task.is_active,
        "enable_notification": task.enable_notification,
        "next_run_time": next_run,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "formatted_schedule": formatted,
        "scenario_name": scenario_name,
    }


@router.get("/", response_model=List[ScheduledTaskRead])
def list_tasks(session: Session = Depends(get_session)):
    """获取所有定时任务"""
    tasks = session.exec(select(ScheduledTask).order_by(ScheduledTask.id.desc())).all()
    result = []
    for task in tasks:
        scenario_name = "智能探索"
        if task.scenario_id is not None:
            scenario = session.get(TestScenario, task.scenario_id)
            scenario_name = scenario.name if scenario else "未知场景"
        result.append(_task_to_read(task, scenario_name))
    return result


@router.post("/", response_model=ScheduledTaskRead)
def create_task(
    data: ScheduledTaskCreate,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    """创建定时任务"""
    scenario = None
    if data.scenario_id is not None:
        scenario = session.get(TestScenario, data.scenario_id)
        if not scenario:
            raise HTTPException(status_code=404, detail="场景不存在")

    task = ScheduledTask(
        name=data.name,
        scenario_id=data.scenario_id,
        device_serial=",".join(data.device_serials) if data.device_serials else None,
        strategy=data.strategy.value,
        strategy_config=json.dumps(data.strategy_config),
        is_active=True,
        enable_notification=data.enable_notification,
        user_id=current_user.id,
        created_at=datetime.now(),
    )
    session.add(task)
    session.commit()
    session.refresh(task)

    # 注册到调度器
    scheduler = get_scheduler()
    next_run = scheduler.add_task(
        task_id=task.id,
        strategy=task.strategy,
        config=data.strategy_config,
        job_func=_run_scheduled_scenario,
    )
    task.next_run_time = next_run
    session.add(task)
    session.commit()

    return _task_to_read(task, scenario.name if scenario else "智能探索")


@router.put("/{task_id}", response_model=ScheduledTaskRead)
def update_task(
    task_id: int,
    data: ScheduledTaskUpdate,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    """更新定时任务"""
    task = session.get(ScheduledTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    if data.name is not None:
        task.name = data.name
    if data.scenario_id is not None:
        scenario = session.get(TestScenario, data.scenario_id)
        if not scenario:
            raise HTTPException(status_code=404, detail="场景不存在")
        task.scenario_id = data.scenario_id
    if data.device_serials is not None:
        task.device_serial = ",".join(data.device_serials) if data.device_serials else None
    if data.strategy is not None:
        task.strategy = data.strategy.value
    if data.strategy_config is not None:
        task.strategy_config = json.dumps(data.strategy_config)
    if data.enable_notification is not None:
        task.enable_notification = data.enable_notification

    task.updated_at = datetime.now()
    session.add(task)
    session.commit()
    session.refresh(task)

    # 重新注册调度
    config = json.loads(task.strategy_config) if task.strategy_config else {}
    scheduler = get_scheduler()
    if task.is_active:
        next_run = scheduler.add_task(
            task_id=task.id,
            strategy=task.strategy,
            config=config,
            job_func=_run_scheduled_scenario,
        )
        task.next_run_time = next_run
        session.add(task)
        session.commit()

    scenario_name = "智能探索"
    if task.scenario_id is not None:
        scenario = session.get(TestScenario, task.scenario_id)
        scenario_name = scenario.name if scenario else "未知场景"
    return _task_to_read(task, scenario_name)


@router.patch("/{task_id}/toggle", response_model=ScheduledTaskRead)
def toggle_task(
    task_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    """切换任务开关"""
    task = session.get(ScheduledTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    task.is_active = not task.is_active
    task.updated_at = datetime.now()

    scheduler = get_scheduler()
    if task.is_active:
        config = json.loads(task.strategy_config) if task.strategy_config else {}
        next_run = scheduler.add_task(
            task_id=task.id,
            strategy=task.strategy,
            config=config,
            job_func=_run_scheduled_scenario,
        )
        task.next_run_time = next_run
    else:
        scheduler.remove_task(task.id)
        task.next_run_time = None

    session.add(task)
    session.commit()
    session.refresh(task)

    scenario_name = "智能探索"
    if task.scenario_id is not None:
        scenario = session.get(TestScenario, task.scenario_id)
        scenario_name = scenario.name if scenario else "未知场景"
    return _task_to_read(task, scenario_name)


@router.delete("/{task_id}")
def delete_task(
    task_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    """删除定时任务"""
    task = session.get(ScheduledTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    # 从调度器移除
    scheduler = get_scheduler()
    scheduler.remove_task(task.id)

    session.delete(task)
    session.commit()
    return {"success": True}
