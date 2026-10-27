"""
定时任务 API 路由

提供定时任务的 CRUD 和开关切换功能。
"""
import json
import logging
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_
from sqlmodel import Session, col, func, select
from datetime import datetime

from backend.database import get_session
from backend.feature_flags import (
    FLAG_MODEL_INSPECTION,
    is_flag_enabled,
)
from backend.models import (
    AppPackage,
    Device,
    InspectionProfile,
    ScheduledTask,
    TestExecution,
    TestScenario,
    User,
)
from backend.schemas import (
    PaginatedClientScheduledTaskRead,
    PaginatedScheduledTaskRead,
    ScheduledTaskCreate,
    ScheduledTaskRead,
    ScheduledTaskUpdate,
)
from backend.api import deps
from backend.scheduler_service import get_scheduler, SchedulerService

logger = logging.getLogger(__name__)

router = APIRouter()


def _task_type(config: dict) -> str:
    value = str((config or {}).get("_task_type") or "ui").strip().lower()
    if value not in {"ui", "fastbot", "inspection", "api"}:
        raise HTTPException(status_code=400, detail=f"不支持的定时任务类型: {value}")
    return value


def _validate_scheduled_target(
    *,
    session: Session,
    scenario_id: Optional[int],
    device_serials: List[str],
    config: dict,
) -> Optional[TestScenario]:
    """Validate the target-specific part of a scheduled task.

    Inspection schedules deliberately require one explicit Android device.  The
    device is not reserved at schedule creation time; the execution-time atomic
    lease remains the source of truth.
    """
    task_type = _task_type(config)
    if task_type == "api":
        from backend.models import ApiScenario
        from backend.api_testing.service import load_env, scenario_steps
        from backend.api_testing.values import validate_steps
        target = session.get(ApiScenario, config.get("api_scenario_id")) if config.get("api_scenario_id") else None
        if target is None:
            raise HTTPException(422, "请选择接口场景")
        env, _, _ = load_env(session, config.get("env_id", target.env_id))
        steps = scenario_steps(session, target.id)
        errors = validate_steps(steps, env, validation_mode="run")
        if errors:
            raise HTTPException(422, errors)
        if device_serials:
            raise HTTPException(422, "接口任务不需要执行设备")
        return None
    if task_type == "ui":
        if scenario_id is None:
            raise HTTPException(status_code=400, detail="UI 定时任务必须选择场景")
        scenario = session.get(TestScenario, scenario_id)
        if scenario is None:
            raise HTTPException(status_code=404, detail="场景不存在")
        return scenario

    if task_type == "inspection":
        if not is_flag_enabled(session, FLAG_MODEL_INSPECTION):
            raise HTTPException(
                status_code=404,
                detail="模型化智能巡检尚未启用（Feature Flag: model_inspection）",
            )
        if len(device_serials) != 1:
            raise HTTPException(status_code=400, detail="巡检定时任务必须显式选择 1 台设备")
        device = session.exec(
            select(Device).where(Device.serial == device_serials[0])
        ).first()
        if device is None:
            raise HTTPException(status_code=404, detail="巡检设备不存在")
        if str(device.platform or "android").lower() != "android":
            raise HTTPException(status_code=400, detail="智能巡检首期仅支持 Android")

        try:
            profile_id = int(config.get("inspection_profile_id") or 0)
        except (TypeError, ValueError):
            profile_id = 0
        profile = session.get(InspectionProfile, profile_id) if profile_id else None
        if profile is None:
            raise HTTPException(status_code=404, detail="巡检配置不存在")

        branches = list(config.get("inspection_branches") or ["guest", "authenticated"])
        normalized = []
        for item in branches:
            key = str(item or "").strip().lower()
            if key not in {"guest", "authenticated"}:
                raise HTTPException(status_code=400, detail=f"不支持的巡检业务线: {key}")
            if key not in normalized:
                normalized.append(key)
        if not normalized:
            raise HTTPException(status_code=400, detail="巡检定时任务至少选择一条业务线")
        missing = [key for key in normalized if key not in (profile.branches or {})]
        if missing:
            raise HTTPException(
                status_code=400,
                detail=f"巡检配置缺少业务线: {', '.join(missing)}",
            )
        config["inspection_branches"] = normalized

        package_id = config.get("inspection_package_id")
        if package_id not in (None, ""):
            try:
                package = session.get(AppPackage, int(package_id))
            except (TypeError, ValueError):
                package = None
            if package is None:
                raise HTTPException(status_code=404, detail="巡检安装包不存在")
            if str(package.platform or "android").lower() != "android":
                raise HTTPException(status_code=400, detail="巡检安装包必须为 Android")
            if package.package_name and package.package_name != profile.package_name:
                raise HTTPException(status_code=400, detail="安装包包名与巡检配置不一致")
        return None

    # Fastbot validation stays backward compatible, while new UI always supplies
    # an explicit device.
    return None


def _task_serials(task: ScheduledTask) -> List[str]:
    return [item.strip() for item in (task.device_serial or "").split(",") if item.strip()]


def _client_managed_serials(session: Session, serials: List[str]) -> set:
    """serials 中「认领/心跳」客户端设备集合（devices.is_client_managed_device 口径）。"""
    from backend.api.devices import is_client_managed_device

    if not serials:
        return set()
    rows = session.exec(select(Device).where(col(Device.serial).in_(list(serials)))).all()
    return {row.serial for row in rows if is_client_managed_device(row)}


def client_scheduled_serials(session: Session, task: ScheduledTask) -> List[str]:
    """统一谓词（T1，P4 终验必修缺陷 2）：任务绑定中「应由客户端本地调度」的 serial。

    云端触发过滤（_run_scheduled_scenario）与 GET /api/client-tasks 下发过滤共用
    同一实现，消除两侧口径漂移：
    - 仅 ui 类型 + scenario_id（桌面 LOCAL_TASK_TYPES={"ui"} 且 execute_task 需要
      场景）；fastbot/inspection/api 桌面不接管；
    - serial 命中「认领/心跳」客户端设备（client_instance_id / last_seen_at，见
      devices.is_client_managed_device）；
    - 空列表 = 该任务没有任何 serial 归客户端调度（桌面不调度，云端执行）。
    """
    try:
        config = json.loads(task.strategy_config or "{}")
    except (json.JSONDecodeError, TypeError):
        config = {}
    try:
        task_type = _task_type(config)
    except HTTPException:
        return []
    if task_type != "ui" or task.scenario_id is None:
        return []
    serials = _task_serials(task)
    if not serials:
        return []
    managed = _client_managed_serials(session, serials)
    return [item for item in serials if item in managed]


def is_client_scheduled_task(session: Session, task: ScheduledTask) -> bool:
    """T1 双触发协调：云端是否完全让位（该任务全部 serial 由客户端本地调度）。

    判定与 GET /api/client-tasks ∩ 桌面 should_schedule_locally 口径一致（统一
    谓词 client_scheduled_serials 同源）：
    - 仅 ui 类型 + scenario_id（桌面 LOCAL_TASK_TYPES={"ui"}；fastbot/inspection/api
      桌面不接管，云端必须保留注册）；
    - device_serials 非空且全部命中「认领/心跳」客户端设备（client_instance_id /
      last_seen_at，见 devices.is_client_managed_device）。混合绑定时云端保留 job，
      触发时只跑非客户端设备（_run_scheduled_scenario 过滤），客户端设备按同一
      谓词交桌面（GET /api/client-tasks 下发），不双跑不漏跑；
    - 无绑定设备任务桌面 execute_task 不触发，保留云端注册。
    """
    serials = _task_serials(task)
    return bool(serials) and set(serials) <= set(client_scheduled_serials(session, task))


def _sync_cloud_schedule(session: Session, task: ScheduledTask, config: dict):
    """统一的云端调度注册入口（create / update / toggle / _restore 共用，T1）。

    P4 终验修正（必修缺陷 2）：「应由客户端调度」的任务同样注册云端 job——触发时
    走 _run_scheduled_scenario 的客户端过滤 + 心跳观测（不执行）：桌面心跳在线 →
    静默交本地引擎；无客户端心跳 → warning + SKIPPED 执行记录，杜绝「云端跳过
    注册 + 桌面缺席 = 静默漏跑无记录」。next_run_time 与物理任务同源（add_task
    返回值），列表「下次运行」展示不失真。
    """
    scheduler = get_scheduler()
    if not task.is_active:
        scheduler.remove_task(task.id)
        task.next_run_time = None
        return None
    if is_client_scheduled_task(session, task):
        logger.info(
            "[定时任务] 任务 #%s 应由客户端本地调度，云端注册观测 job（触发时不执行）",
            task.id,
        )
    next_run = scheduler.add_task(
        task_id=task.id,
        strategy=task.strategy,
        config=config,
        job_func=_run_scheduled_scenario,
    )
    task.next_run_time = next_run
    return next_run


def _observe_client_scheduled_serials(
    session: Session,
    task: ScheduledTask,
    serials: List[str],
) -> None:
    """「应由客户端调度」serial 的触发时刻可观测（T1，P4 终验必修缺陷 2）。

    桌面心跳在线（client_presence_expired 未过期）→ 本地引擎会跑：静默跳过，
    不重复执行、不重复记账；无客户端心跳（引擎缺席）→ warning 日志 + 落一条
    SKIPPED 执行记录，杜绝「云端让位 + 桌面缺席 = 静默漏跑无记录」。
    """
    from backend.api.devices import client_presence_expired

    absent: List[str] = []
    for serial in serials:
        device = session.exec(select(Device).where(Device.serial == serial)).first()
        if device is None or client_presence_expired(device):
            absent.append(serial)

    if not absent:
        logger.info(
            "[定时任务] 任务 #%s 客户端设备 %s 心跳在线，由桌面本地引擎执行",
            task.id,
            serials,
        )
        return

    logger.warning(
        "[定时任务] 任务 #%s 应由客户端调度的设备无心跳（桌面引擎缺席），落 SKIPPED 记录: %s",
        task.id,
        absent,
    )
    if task.scenario_id is None:
        return

    scenario = session.get(TestScenario, task.scenario_id)
    scenario_name = scenario.name if scenario else task.name
    now = datetime.now()
    for serial in absent:
        session.add(
            TestExecution(
                scenario_id=task.scenario_id,
                scenario_name=scenario_name,
                status="SKIPPED",
                executor_id=task.user_id,
                executor_name=f"定时任务: {task.name}",
                device_serial=serial,
                start_time=now,
                end_time=now,
                duration=0.0,
                batch_name=f"定时任务: {task.name}",
                run_source="platform",
            )
        )
    session.commit()


def _run_scheduled_scenario(task_id: int):
    """调度器回调：根据任务类型执行 UI 场景、Fastbot 或模型化巡检。"""
    from backend.database import engine
    # Dispatch before importing device execution code: HTTP runs need no driver.
    with Session(engine) as api_session:
        scheduled = api_session.get(ScheduledTask, task_id)
        try:
            scheduled_config = json.loads(scheduled.strategy_config or "{}") if scheduled else {}
        except ValueError:
            scheduled_config = {}
        if scheduled_config.get("_task_type") == "api":
            from backend.api_testing.service import run_scheduled
            run_scheduled(task_id, db_engine=engine)
            return
    from backend.api.scenarios import (
        _summarize_precheck_failure,
        execute_scenario_batch_background,
        precheck_scenario_execution,
    )
    from backend.models import TestExecution, TestResult
    from backend.notification_service import NotificationService
    from sqlmodel import Session as SQLSession

    runnable_device_serials: List[str] = []
    blocked_prechecks: List[dict] = []
    env_id = None

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
        task_type = str(config.get("_task_type") or "ui").strip().lower()
        env_id = config.get("env_id")

        # T1 双触发防御：认领/心跳客户端设备由桌面本地调度执行，云端跳过
        # （覆盖「注册后设备才被桌面认领」的残留 job 场景，不双跑）。
        # P4 终验修正（必修缺陷 2）：跳过前做心跳观测——桌面缺席不得静默漏跑。
        if task_type == "ui" and device_serials:
            client_serials = _client_managed_serials(session, device_serials)
            if client_serials:
                remaining = [s for s in device_serials if s not in client_serials]
                logger.info(
                    "[定时任务] 任务 #%s 跳过客户端本地调度设备: %s",
                    task_id,
                    sorted(client_serials),
                )
                _observe_client_scheduled_serials(session, task, sorted(client_serials))
                if not remaining:
                    logger.info(
                        "[定时任务] 任务 #%s 全部设备由客户端本地调度，云端取消执行",
                        task_id,
                    )
                    return
                device_serials = remaining

        # UI 场景任务：执行前按设备做预检，过滤明显不可执行设备。
        runnable_device_serials = list(device_serials)
        if (
            task_type == "ui"
            and scenario_id is not None
            and device_serials
        ):
            runnable_device_serials = []
            for serial in device_serials:
                try:
                    precheck = precheck_scenario_execution(
                        session=session,
                        scenario_id=scenario_id,
                        device_serial=serial,
                        env_id=env_id,
                    )
                except Exception as exc:
                    blocked_prechecks.append(
                        {
                            "device_serial": serial,
                            "reason": str(exc),
                        }
                    )
                    continue

                if precheck.get("ok"):
                    runnable_device_serials.append(serial)
                else:
                    blocked_prechecks.append(
                        {
                            "device_serial": serial,
                            "reason": _summarize_precheck_failure(precheck),
                        }
                    )

    task_type = str(config.get("_task_type") or "ui").strip().lower()

    if task_type == "fastbot":
        fb_device_serial = device_serials[0] if device_serials else None
        _run_scheduled_fastbot(config, task_name, fb_device_serial, enable_notification)
    elif task_type == "inspection":
        if len(device_serials) != 1:
            logger.error(
                "[定时任务] 巡检任务 #%s 必须显式配置且只能配置 1 台设备",
                task_id,
            )
            return
        _run_scheduled_inspection(
            config=config,
            task_name=task_name,
            device_serial=device_serials[0],
            executor_id=task.user_id,
        )
    else:
        if scenario_id is None:
            logger.error(f"[定时任务] UI 任务 #{task_id} 缺少 scenario_id")
            return
            
        if blocked_prechecks:
            first = blocked_prechecks[0]
            logger.warning(
                "[定时任务] 场景预检拦截 %d 台设备，示例: serial=%s reason=%s",
                len(blocked_prechecks),
                first.get("device_serial"),
                first.get("reason"),
            )

        if device_serials and not runnable_device_serials:
            logger.error(
                "[定时任务] 场景预检全部失败，取消执行: scenario=%s task=%s",
                scenario_id,
                task_name,
            )
            return

        target_serials = runnable_device_serials if device_serials else device_serials
        logger.info(
            f"[定时任务] 开始执行场景 #{scenario_id} (任务: {task_name}, env_id: {env_id}, device_serials: {target_serials})"
        )
        
        batch_id = execute_scenario_batch_background(
            scenario_id,
            f"定时任务: {task_name}",
            env_id,
            target_serials,
        )

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


def _run_scheduled_inspection(
    *,
    config: dict,
    task_name: str,
    device_serial: str,
    executor_id: Optional[int],
) -> Optional[int]:
    """Create an immutable inspection run snapshot and execute it synchronously."""
    from backend.database import engine
    from backend.inspection.engine import execute_inspection_run
    from backend.inspection.runtime import abort_event_for_run
    from backend.models import InspectionBranchRun, InspectionRun
    from sqlmodel import Session as SQLSession

    with SQLSession(engine) as session:
        if not is_flag_enabled(session, FLAG_MODEL_INSPECTION):
            logger.error("[定时任务] 模型化智能巡检 Feature Flag 未开启")
            return None
        try:
            profile_id = int(config.get("inspection_profile_id") or 0)
        except (TypeError, ValueError):
            profile_id = 0
        profile = session.get(InspectionProfile, profile_id) if profile_id else None
        device = session.exec(select(Device).where(Device.serial == device_serial)).first()
        if profile is None or device is None:
            logger.error(
                "[定时任务] 巡检目标无效: profile=%s device=%s",
                profile_id,
                device_serial,
            )
            return None
        if str(device.platform or "android").lower() != "android":
            logger.error("[定时任务] 巡检设备不是 Android: %s", device_serial)
            return None

        selected_branches = []
        for item in list(
            config.get("inspection_branches") or ["guest", "authenticated"]
        ):
            key = str(item or "").strip().lower()
            if key in {"guest", "authenticated"} and key not in selected_branches:
                selected_branches.append(key)
        selected_branches = [
            key for key in selected_branches if key in (profile.branches or {})
        ]
        if not selected_branches:
            logger.error("[定时任务] 巡检任务没有可执行业务线: profile=%s", profile_id)
            return None

        package_id = config.get("inspection_package_id")
        try:
            package_id = int(package_id) if package_id not in (None, "") else None
        except (TypeError, ValueError):
            logger.error("[定时任务] 巡检安装包 ID 非法: %r", package_id)
            return None
        if package_id is not None:
            package = session.get(AppPackage, package_id)
            if (
                package is None
                or str(package.platform or "android").lower() != "android"
                or (
                    package.package_name
                    and package.package_name != profile.package_name
                )
            ):
                logger.error("[定时任务] 巡检安装包与配置不匹配: %s", package_id)
                return None

        snapshot = {
            "name": profile.name,
            "package_name": profile.package_name,
            "branches": profile.branches or {},
            "input_rules": profile.input_rules or [],
            "safety_rules": profile.safety_rules or [],
            "sanitizer_rules": profile.sanitizer_rules or [],
            "dynamic_text_patterns": profile.dynamic_text_patterns or [],
            "budgets": profile.budgets or {},
            "monitor_options": profile.monitor_options or {},
            "selected_branches": selected_branches,
            "graph_hierarchy_version": 2,
        }
        run = InspectionRun(
            name=f"定时任务: {task_name}",
            profile_id=profile.id,
            package_name=profile.package_name,
            package_id=package_id,
            package_source="package" if package_id is not None else "installed",
            profile_snapshot=snapshot,
            device_serial=device_serial,
            selected_branches=selected_branches,
            status="PENDING",
            current_stage="等待设备租约",
            total_branches=len(selected_branches),
            executor_id=executor_id,
            executor_name=f"定时任务: {task_name}",
        )
        session.add(run)
        session.flush()
        for branch_key in selected_branches:
            branch = dict((profile.branches or {}).get(branch_key) or {})
            session.add(
                InspectionBranchRun(
                    run_id=run.id,
                    branch_key=branch_key,
                    branch_name=str(branch.get("name") or branch_key),
                )
            )
        session.commit()
        session.refresh(run)
        run_id = int(run.id)

    logger.info("[定时任务] 开始模型化巡检: task=%s run=%s", task_name, run_id)
    execute_inspection_run(run_id, abort_event_for_run(run_id))
    return run_id


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


def _scheduled_target_name(
    session: Session,
    task: ScheduledTask,
    config: Optional[dict] = None,
) -> str:
    if config is None:
        try:
            config = json.loads(task.strategy_config or "{}")
        except (json.JSONDecodeError, TypeError):
            config = {}
    task_type = str((config or {}).get("_task_type") or "ui").strip().lower()
    if task_type == "api":
        from backend.models import ApiScenario
        api_id = (config or {}).get("api_scenario_id")
        target = session.get(ApiScenario, api_id) if api_id else None
        return target.name if target else "接口场景（已删除）"
    if task_type == "inspection":
        profile_id = (config or {}).get("inspection_profile_id")
        try:
            profile = session.get(InspectionProfile, int(profile_id))
        except (TypeError, ValueError):
            profile = None
        return f"模型化巡检 · {profile.name}" if profile else "模型化巡检"
    if task_type == "fastbot":
        return "智能探索"
    if task.scenario_id is None:
        return "未知场景"
    scenario = session.get(TestScenario, task.scenario_id)
    return scenario.name if scenario else "未知场景"


@router.get("/", response_model=PaginatedScheduledTaskRead)
def list_tasks(
    skip: int = 0,
    limit: int = 20,
    keyword: Optional[str] = None,
    session: Session = Depends(get_session),
):
    """分页获取定时任务，keyword 匹配任务名称或场景名称"""
    query = select(ScheduledTask).outerjoin(
        TestScenario, ScheduledTask.scenario_id == TestScenario.id
    )
    count_query = select(func.count(ScheduledTask.id)).outerjoin(
        TestScenario, ScheduledTask.scenario_id == TestScenario.id
    )
    if keyword:
        condition = or_(
            col(ScheduledTask.name).contains(keyword),
            col(TestScenario.name).contains(keyword),
        )
        query = query.where(condition)
        count_query = count_query.where(condition)

    total = session.exec(count_query).one()
    tasks = session.exec(
        query.order_by(ScheduledTask.id.desc()).offset(skip).limit(limit)
    ).all()

    result = []
    for task in tasks:
        result.append(_task_to_read(task, _scheduled_target_name(session, task)))
    return PaginatedScheduledTaskRead(total=total, items=result)


@router.post("/", response_model=ScheduledTaskRead)
def create_task(
    data: ScheduledTaskCreate,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    """创建定时任务"""
    config = dict(data.strategy_config or {})
    device_serials = list(dict.fromkeys(
        str(item or "").strip() for item in data.device_serials if str(item or "").strip()
    ))
    task_type = _task_type(config)
    scenario = _validate_scheduled_target(
        session=session,
        scenario_id=data.scenario_id,
        device_serials=device_serials,
        config=config,
    )
    scenario_id = data.scenario_id if task_type == "ui" else None

    task = ScheduledTask(
        name=data.name,
        scenario_id=scenario_id,
        device_serial=",".join(device_serials) if device_serials else None,
        strategy=data.strategy.value,
        strategy_config=json.dumps(config),
        is_active=True,
        enable_notification=data.enable_notification,
        user_id=current_user.id,
        created_at=datetime.now(),
    )
    session.add(task)
    session.commit()
    session.refresh(task)

    # 注册到调度器（应由客户端调度的任务跳过云端注册，T1）
    _sync_cloud_schedule(session, task, config)
    session.add(task)
    session.commit()

    return _task_to_read(
        task,
        scenario.name if scenario else _scheduled_target_name(session, task, config),
    )


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

    try:
        current_config = json.loads(task.strategy_config or "{}")
    except (json.JSONDecodeError, TypeError):
        current_config = {}
    config = (
        dict(data.strategy_config)
        if data.strategy_config is not None
        else dict(current_config)
    )
    if data.device_serials is not None:
        device_serials = list(dict.fromkeys(
            str(item or "").strip()
            for item in data.device_serials
            if str(item or "").strip()
        ))
    else:
        device_serials = [
            item.strip()
            for item in (task.device_serial or "").split(",")
            if item.strip()
        ]
    task_type = _task_type(config)
    scenario_id = data.scenario_id if data.scenario_id is not None else task.scenario_id
    if task_type != "ui":
        scenario_id = None
    scenario = _validate_scheduled_target(
        session=session,
        scenario_id=scenario_id,
        device_serials=device_serials,
        config=config,
    )

    if data.name is not None:
        task.name = data.name
    task.scenario_id = scenario_id
    if data.device_serials is not None:
        task.device_serial = ",".join(device_serials) if device_serials else None
    if data.strategy is not None:
        task.strategy = data.strategy.value
    task.strategy_config = json.dumps(config)
    if data.enable_notification is not None:
        task.enable_notification = data.enable_notification

    task.updated_at = datetime.now()
    session.add(task)
    session.commit()
    session.refresh(task)

    # 重新注册调度（应由客户端调度的任务跳过云端注册并清残留 job，T1）
    config = json.loads(task.strategy_config) if task.strategy_config else {}
    _sync_cloud_schedule(session, task, config)
    session.add(task)
    session.commit()

    return _task_to_read(
        task,
        scenario.name if scenario else _scheduled_target_name(session, task, config),
    )


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

    # 注册/移除云端调度（应由客户端调度的任务不注册云端 job，T1）
    config = json.loads(task.strategy_config) if task.strategy_config else {}
    _sync_cloud_schedule(session, task, config)

    session.add(task)
    session.commit()
    session.refresh(task)

    return _task_to_read(task, _scheduled_target_name(session, task))


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


# ==================== 客户端任务拉取（桌面引擎本地调度） ====================

client_router = APIRouter()


def _client_task_item(task: ScheduledTask) -> dict:
    """ScheduledTask 全字段条目：strategy_config 解析为字典、device_serials 为列表。"""
    try:
        config = json.loads(task.strategy_config or "{}")
    except (json.JSONDecodeError, TypeError):
        config = {}
    device_serials = [
        item.strip() for item in (task.device_serial or "").split(",") if item.strip()
    ]
    return {
        "id": task.id,
        "name": task.name,
        "scenario_id": task.scenario_id,
        "device_serial": task.device_serial,
        "device_serials": device_serials,
        "strategy": task.strategy,
        "strategy_config": config,
        "is_active": task.is_active,
        "enable_notification": task.enable_notification,
        "next_run_time": task.next_run_time,
        "user_id": task.user_id,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }


def _client_owned_serials(session: Session, user_id: int) -> set:
    """本人设备 serial 集合（owner_id / app_owner_id，与 filter_devices_for_user 同口径）。"""
    devices = session.exec(
        select(Device).where(or_(Device.owner_id == user_id, Device.app_owner_id == user_id))
    ).all()
    return {d.serial for d in devices if d.serial}


@client_router.get("", response_model=PaginatedClientScheduledTaskRead)
@client_router.get("/", response_model=PaginatedClientScheduledTaskRead)
def list_client_tasks(
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    """拉取「应由客户端调度」的定时任务（桌面引擎本地 APScheduler 消费）。

    统一谓词（P4 终验必修缺陷 2，与云端 is_client_scheduled_task / 触发过滤
    同源 client_scheduled_serials）：仅下发 ui + scenario_id + 绑定中存在
    认领/心跳客户端设备的任务——「本人创建 + 未认领 serial」不下发（云端执行），
    否则两端都调度（真双跑窗口）；无绑定设备 / fastbot/inspection/api 不下发
    （桌面 LOCAL_TASK_TYPES={'ui'} 且 execute_task 需要绑定设备）。

    可见性过滤：创建者=本人 或 device_serials 命中本人设备（owner_id /
    app_owner_id == 本人）；仅返回 is_active 任务（暂停任务不进本地调度，对齐
    _restore_scheduled_tasks 仅恢复活跃任务的语义）。

    admin 语义按 tasks.py 现状对齐：任务侧无 admin 全量可见语义（列表不做归属
    放大、创建仅打 user_id），admin 与普通用户同规则。

    任务 CRUD 照旧走 /api/tasks/（云端透传）；本地只在引擎存活时触发，重启后
    重新拉取本接口重排。返回 {total, items}，条目含 ScheduledTask 全字段
    （user_id / device_serial 原始 CSV 也在，strategy_config 解析为字典）。
    """
    uid = current_user.id
    mine_serials = _client_owned_serials(session, uid)
    rows = session.exec(
        select(ScheduledTask)
        .where(ScheduledTask.is_active == True)  # noqa: E712 — 与 _restore_scheduled_tasks 同口径
        .order_by(ScheduledTask.id.desc())
    ).all()

    items = []
    for task in rows:
        bound_serials = {
            item.strip() for item in (task.device_serial or "").split(",") if item.strip()
        }
        if not (task.user_id == uid or (bound_serials & mine_serials)):
            continue
        # 统一谓词：无客户端应调度 serial 的任务桌面不接管（云端执行），避免双跑
        if not client_scheduled_serials(session, task):
            continue
        items.append(_client_task_item(task))
    return PaginatedClientScheduledTaskRead(total=len(items), items=items)
