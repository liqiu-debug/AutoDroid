from typing import Dict, List, Optional
from datetime import datetime, timedelta
from collections import defaultdict
import json
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select, func, desc
from ..database import get_session
from ..models import Device, ScheduledTask, TestExecution, TestResult, TestScenario, User
from pydantic import BaseModel
from backend.scheduler_service import SchedulerService

from backend.utils.pydantic_compat import dump_model

router = APIRouter()

# --- Schemas for Response ---

class TestResultRead(BaseModel):
    id: int
    step_name: str
    step_order: int
    status: str
    error_message: Optional[str] = None
    screenshot_path: Optional[str] = None
    ui_hierarchy: Optional[str] = None
    duration: float

class TestExecutionRead(BaseModel):
    id: int
    scenario_id: int
    scenario_name: str
    executor_id: Optional[int] = None
    executor_name: Optional[str] = None
    start_time: datetime
    end_time: Optional[datetime] = None
    status: str
    device_serial: Optional[str] = None
    platform: Optional[str] = None
    device_info: Optional[str] = None
    duration: Optional[float] = None # Calculated duration in seconds
    batch_id: Optional[str] = None
    batch_name: Optional[str] = None

    def __init__(self, **data):
        super().__init__(**data)
        if self.start_time and self.end_time:
            self.duration = (self.end_time - self.start_time).total_seconds()

class TestExecutionDetail(TestExecutionRead):
    steps: List[TestResultRead] = []

class PaginatedTestExecutionRead(BaseModel):
    total: int
    items: List[TestExecutionRead]


from sqlalchemy import case

class FailingScenario(BaseModel):
    id: int
    name: str
    fail_count: int
    fail_rate: float

class DashboardStats(BaseModel):
    total_executions: int
    pass_rate: float
    avg_duration: float # seconds
    top_failed_scenarios: List[FailingScenario] = []


class DashboardKpis(BaseModel):
    total_executions: int
    pass_rate: float
    failed_scenarios: int
    avg_duration: float
    running_executions: int
    idle_devices: int
    active_tasks: int


class DashboardTrendPoint(BaseModel):
    date: str
    total: int
    pass_count: int
    fail_count: int
    warning_count: int
    running_count: int


class DashboardStatusDistributionItem(BaseModel):
    status: str
    count: int


class DashboardAlert(BaseModel):
    type: str
    level: str
    title: str
    message: str


class DashboardTaskItem(BaseModel):
    id: int
    name: str
    scenario_name: str
    next_run_time: datetime
    formatted_schedule: str


class DashboardOverview(BaseModel):
    range: str
    platform: str
    generated_at: datetime
    kpis: DashboardKpis
    trend: List[DashboardTrendPoint] = []
    status_distribution: List[DashboardStatusDistributionItem] = []
    top_failed_scenarios: List[FailingScenario] = []
    alerts: List[DashboardAlert] = []
    recent_executions: List[TestExecutionRead] = []
    upcoming_tasks: List[DashboardTaskItem] = []


# --- API Endpoints ---

ALERT_FAIL_STATUSES = {"FAIL", "WARNING", "ERROR"}
COMPLETED_STATUSES = {"PASS", "FAIL", "WARNING", "ERROR"}


def _dashboard_window_start(range_key: str, now: datetime) -> datetime:
    if range_key == "24h":
        return now - timedelta(hours=24)
    if range_key == "30d":
        return now - timedelta(days=30)
    # default / 7d
    return now - timedelta(days=7)


def _normalize_platform(platform: str) -> str:
    value = (platform or "all").strip().lower()
    return value if value in {"all", "android", "ios"} else "all"


def _build_dashboard_trend(
    executions: List[TestExecution],
    range_key: str,
    now: datetime,
) -> List[DashboardTrendPoint]:
    if range_key == "24h":
        total_buckets = 24
        cursor = (now - timedelta(hours=23)).replace(minute=0, second=0, microsecond=0)
        delta = timedelta(hours=1)
        label_fmt = "%m-%d %H:00"
        round_time = lambda dt: dt.replace(minute=0, second=0, microsecond=0)
    else:
        total_days = 30 if range_key == "30d" else 7
        total_buckets = total_days
        cursor = (now - timedelta(days=total_days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
        delta = timedelta(days=1)
        label_fmt = "%m-%d"
        round_time = lambda dt: dt.replace(hour=0, minute=0, second=0, microsecond=0)

    buckets: Dict[str, Dict[str, int]] = {}
    labels: Dict[str, str] = {}
    for i in range(total_buckets):
        bucket_dt = cursor + i * delta
        key = bucket_dt.isoformat()
        labels[key] = bucket_dt.strftime(label_fmt)
        buckets[key] = {
            "total": 0,
            "pass_count": 0,
            "fail_count": 0,
            "warning_count": 0,
            "running_count": 0,
        }

    for execution in executions:
        if not execution.start_time:
            continue
        rounded = round_time(execution.start_time)
        key = rounded.isoformat()
        if key not in buckets:
            continue
        bucket = buckets[key]
        bucket["total"] += 1
        status = str(execution.status or "").upper()
        if status == "PASS":
            bucket["pass_count"] += 1
        elif status in {"FAIL", "ERROR"}:
            bucket["fail_count"] += 1
        elif status == "WARNING":
            bucket["warning_count"] += 1
        elif status == "RUNNING":
            bucket["running_count"] += 1

    points: List[DashboardTrendPoint] = []
    for key in labels:
        item = buckets[key]
        points.append(
            DashboardTrendPoint(
                date=labels[key],
                total=item["total"],
                pass_count=item["pass_count"],
                fail_count=item["fail_count"],
                warning_count=item["warning_count"],
                running_count=item["running_count"],
            )
        )
    return points


def _build_top_failed_scenarios(executions: List[TestExecution], limit: int = 5) -> List[FailingScenario]:
    stats: Dict[int, Dict[str, object]] = {}
    for execution in executions:
        scenario_id = int(execution.scenario_id)
        item = stats.setdefault(
            scenario_id,
            {
                "name": execution.scenario_name or f"Scenario#{scenario_id}",
                "total": 0,
                "fail_count": 0,
            },
        )
        status = str(execution.status or "").upper()
        if status in COMPLETED_STATUSES:
            item["total"] = int(item["total"]) + 1
        if status in ALERT_FAIL_STATUSES:
            item["fail_count"] = int(item["fail_count"]) + 1

    failed_list: List[FailingScenario] = []
    for scenario_id, item in stats.items():
        fail_count = int(item["fail_count"])
        total = int(item["total"])
        if fail_count <= 0:
            continue
        fail_rate = round((fail_count / total) * 100, 1) if total > 0 else 100.0
        failed_list.append(
            FailingScenario(
                id=scenario_id,
                name=str(item["name"]),
                fail_count=fail_count,
                fail_rate=fail_rate,
            )
        )

    failed_list.sort(key=lambda x: (x.fail_count, x.fail_rate), reverse=True)
    return failed_list[:limit]


def _build_dashboard_alerts(
    session: Session,
    executions: List[TestExecution],
    platform: str,
    now: datetime,
) -> List[DashboardAlert]:
    alerts: List[DashboardAlert] = []

    # 连续失败场景告警
    per_scenario = defaultdict(list)
    for execution in sorted(executions, key=lambda e: e.start_time or datetime.min, reverse=True):
        per_scenario[execution.scenario_id].append(execution)

    consecutive_items = []
    for scenario_id, items in per_scenario.items():
        streak = 0
        for execution in items:
            status = str(execution.status or "").upper()
            if status in ALERT_FAIL_STATUSES:
                streak += 1
            elif status in COMPLETED_STATUSES:
                break
        if streak >= 3:
            scenario_name = items[0].scenario_name if items else f"Scenario#{scenario_id}"
            consecutive_items.append((streak, scenario_name))

    consecutive_items.sort(key=lambda x: x[0], reverse=True)
    for streak, scenario_name in consecutive_items[:3]:
        alerts.append(
            DashboardAlert(
                type="scenario_consecutive_fail",
                level="danger",
                title=f"{scenario_name} 连续失败",
                message=f"近窗口内连续 {streak} 次失败/告警，请优先排查。",
            )
        )

    # 设备状态告警
    device_query = select(Device)
    if platform != "all":
        device_query = device_query.where(Device.platform == platform)
    devices = session.exec(device_query).all()

    offline_devices = [d for d in devices if str(d.status or "").upper() == "OFFLINE"]
    if offline_devices:
        sample = "、".join([(d.custom_name or d.market_name or d.model or d.serial) for d in offline_devices[:3]])
        alerts.append(
            DashboardAlert(
                type="device_offline",
                level="warning",
                title=f"离线设备 {len(offline_devices)} 台",
                message=f"示例：{sample}",
            )
        )

    wda_down_devices = [d for d in devices if str(d.status or "").upper() == "WDA_DOWN"]
    if wda_down_devices:
        sample = "、".join([(d.custom_name or d.market_name or d.model or d.serial) for d in wda_down_devices[:3]])
        alerts.append(
            DashboardAlert(
                type="device_wda_down",
                level="warning",
                title=f"WDA 异常设备 {len(wda_down_devices)} 台",
                message=f"示例：{sample}",
            )
        )

    # 任务告警：启用但缺少 next_run_time；启用且 next_run_time 超时未触发
    active_tasks = session.exec(
        select(ScheduledTask).where(ScheduledTask.is_active == True)
    ).all()
    missing_next_run = [t for t in active_tasks if t.next_run_time is None]
    if missing_next_run:
        sample = "、".join([t.name for t in missing_next_run[:3]])
        alerts.append(
            DashboardAlert(
                type="task_missing_next_run",
                level="danger",
                title=f"异常任务 {len(missing_next_run)} 个",
                message=f"已启用但无下次触发时间，示例：{sample}",
            )
        )

    overdue_tasks = []
    for task in active_tasks:
        if not task.next_run_time:
            continue
        reference_now = datetime.now(task.next_run_time.tzinfo) if task.next_run_time.tzinfo else now
        overdue_threshold = reference_now - timedelta(minutes=5)
        if task.next_run_time < overdue_threshold:
            overdue_tasks.append(task)
    if overdue_tasks:
        sample = "、".join([t.name for t in overdue_tasks[:3]])
        alerts.append(
            DashboardAlert(
                type="task_overdue",
                level="warning",
                title=f"超时未触发任务 {len(overdue_tasks)} 个",
                message=f"下次触发时间已过 5 分钟以上，示例：{sample}",
            )
        )

    return alerts


def _build_upcoming_tasks(
    session: Session,
    now: datetime,
    limit: int,
) -> List[DashboardTaskItem]:
    tasks = session.exec(
        select(ScheduledTask).where(
            ScheduledTask.is_active == True,
            ScheduledTask.next_run_time != None,
        )
    ).all()
    filtered = []
    for task in tasks:
        next_run = task.next_run_time
        if not next_run:
            continue
        reference_now = datetime.now(next_run.tzinfo) if next_run.tzinfo else now
        if next_run >= reference_now:
            filtered.append(task)
    filtered.sort(key=lambda t: t.next_run_time)
    tasks = filtered[:limit]
    if not tasks:
        return []

    scenario_ids = [task.scenario_id for task in tasks if task.scenario_id is not None]
    scenario_map = {}
    if scenario_ids:
        scenario_rows = session.exec(select(TestScenario).where(TestScenario.id.in_(scenario_ids))).all()
        scenario_map = {scenario.id: scenario.name for scenario in scenario_rows}

    result: List[DashboardTaskItem] = []
    for task in tasks:
        strategy_config = {}
        if task.strategy_config:
            try:
                strategy_config = json.loads(task.strategy_config)
            except (json.JSONDecodeError, TypeError):
                strategy_config = {}

        result.append(
            DashboardTaskItem(
                id=task.id,
                name=task.name,
                scenario_name="智能探索" if task.scenario_id is None else scenario_map.get(task.scenario_id, "未知场景"),
                next_run_time=task.next_run_time,
                formatted_schedule=SchedulerService.format_schedule(task.strategy, strategy_config),
            )
        )
    return result


@router.get("/dashboard/overview", response_model=DashboardOverview)
def get_dashboard_overview(
    range_key: str = Query(default="7d", alias="range", pattern="^(24h|7d|30d)$"),
    platform: str = Query(default="all", pattern="^(all|android|ios|ALL|ANDROID|IOS)$"),
    limit_recent: int = Query(default=10, ge=1, le=50),
    limit_tasks: int = Query(default=10, ge=1, le=50),
    session: Session = Depends(get_session),
):
    # Keep function directly callable in unit tests where FastAPI Query defaults
    # may be passed as objects instead of validated primitives.
    range_key = range_key if isinstance(range_key, str) else "7d"
    platform = platform if isinstance(platform, str) else "all"
    limit_recent = limit_recent if isinstance(limit_recent, int) else 10
    limit_tasks = limit_tasks if isinstance(limit_tasks, int) else 10

    now = datetime.now()
    normalized_platform = _normalize_platform(platform)
    window_start = _dashboard_window_start(range_key, now)

    execution_query = select(TestExecution).where(TestExecution.start_time >= window_start)
    if normalized_platform != "all":
        execution_query = execution_query.where(TestExecution.platform == normalized_platform)
    executions = session.exec(execution_query).all()

    completed_executions = [e for e in executions if str(e.status or "").upper() in COMPLETED_STATUSES]
    passed_count = sum(1 for e in completed_executions if str(e.status or "").upper() == "PASS")
    pass_rate = round((passed_count / len(completed_executions)) * 100, 1) if completed_executions else 0.0

    failed_scenarios = {
        e.scenario_id
        for e in executions
        if str(e.status or "").upper() in ALERT_FAIL_STATUSES
    }

    durations: List[float] = []
    for execution in completed_executions:
        if execution.start_time and execution.end_time:
            durations.append((execution.end_time - execution.start_time).total_seconds())
        elif execution.duration and execution.duration > 0:
            durations.append(float(execution.duration))
    avg_duration = round(sum(durations) / len(durations), 1) if durations else 0.0

    running_query = select(func.count(TestExecution.id)).where(TestExecution.status == "RUNNING")
    if normalized_platform != "all":
        running_query = running_query.where(TestExecution.platform == normalized_platform)
    running_executions = int(session.exec(running_query).one() or 0)

    idle_devices_query = select(func.count(Device.id)).where(Device.status == "IDLE")
    if normalized_platform != "all":
        idle_devices_query = idle_devices_query.where(Device.platform == normalized_platform)
    idle_devices = int(session.exec(idle_devices_query).one() or 0)

    active_tasks = int(session.exec(select(func.count(ScheduledTask.id)).where(ScheduledTask.is_active == True)).one() or 0)

    status_order = ["PASS", "WARNING", "FAIL", "ERROR", "RUNNING"]
    status_counter = {status: 0 for status in status_order}
    for execution in executions:
        status = str(execution.status or "").upper()
        if status in status_counter:
            status_counter[status] += 1

    status_distribution = [
        DashboardStatusDistributionItem(status=status, count=status_counter[status])
        for status in status_order
    ]

    trend = _build_dashboard_trend(executions, range_key, now)
    top_failed_scenarios = _build_top_failed_scenarios(executions, limit=5)
    alerts = _build_dashboard_alerts(session, executions, normalized_platform, now)

    recent_query = (
        select(TestExecution, User.full_name, User.username)
        .outerjoin(User, TestExecution.executor_id == User.id)
        .where(TestExecution.start_time >= window_start)
    )
    if normalized_platform != "all":
        recent_query = recent_query.where(TestExecution.platform == normalized_platform)
    recent_rows = session.exec(
        recent_query.order_by(desc(TestExecution.start_time)).limit(limit_recent)
    ).all()

    recent_executions: List[TestExecutionRead] = []
    for execution, full_name, username in recent_rows:
        payload = dump_model(execution)
        payload["executor_name"] = full_name or username or execution.executor_name or "System"
        recent_executions.append(TestExecutionRead(**payload))

    upcoming_tasks = _build_upcoming_tasks(session, now, limit_tasks)

    return DashboardOverview(
        range=range_key,
        platform=normalized_platform,
        generated_at=now,
        kpis=DashboardKpis(
            total_executions=len(executions),
            pass_rate=pass_rate,
            failed_scenarios=len(failed_scenarios),
            avg_duration=avg_duration,
            running_executions=running_executions,
            idle_devices=idle_devices,
            active_tasks=active_tasks,
        ),
        trend=trend,
        status_distribution=status_distribution,
        top_failed_scenarios=top_failed_scenarios,
        alerts=alerts,
        recent_executions=recent_executions,
        upcoming_tasks=upcoming_tasks,
    )

@router.get("/executions", response_model=PaginatedTestExecutionRead)
def get_reports(
    skip: int = 0,
    limit: int = 20,
    scenario_id: Optional[int] = None,
    status: Optional[str] = None,
    platform: Optional[str] = None,
    device_serial: Optional[str] = None,
    session: Session = Depends(get_session)
):
    query = select(TestExecution, User.full_name, User.username).outerjoin(User, TestExecution.executor_id == User.id)
    
    if scenario_id:
        query = query.where(TestExecution.scenario_id == scenario_id)
    if status and status != 'all':
        query = query.where(TestExecution.status == status)
    if platform and platform != "all":
        query = query.where(TestExecution.platform == platform.lower())
    if device_serial:
        query = query.where(TestExecution.device_serial == device_serial)
        
    count_query = select(func.count(TestExecution.id))
    if scenario_id:
        count_query = count_query.where(TestExecution.scenario_id == scenario_id)
    if status and status != 'all':
        count_query = count_query.where(TestExecution.status == status)
    if platform and platform != "all":
        count_query = count_query.where(TestExecution.platform == platform.lower())
    if device_serial:
        count_query = count_query.where(TestExecution.device_serial == device_serial)
    
    total = session.exec(count_query).one()
    
    query = query.order_by(desc(TestExecution.start_time))
    results = session.exec(query.offset(skip).limit(limit)).all()
    
    response = []
    for execution, f_name, u_name in results:
        data = dump_model(execution)
        data['executor_name'] = f_name or u_name or "System"
        response.append(TestExecutionRead(**data))
        
    return PaginatedTestExecutionRead(total=total, items=response)


@router.get("/executions/{execution_id}", response_model=TestExecutionDetail)
def get_report_detail(execution_id: int, session: Session = Depends(get_session)):
    execution = session.get(TestExecution, execution_id)
    if not execution:
        raise HTTPException(status_code=404, detail="Execution not found")
        
    # Get steps
    steps = session.exec(select(TestResult).where(TestResult.execution_id == execution_id).order_by(TestResult.step_order)).all()
    
    steps_read = [TestResultRead(**dump_model(s)) for s in steps]
    
    detail = TestExecutionDetail(**dump_model(execution))
    detail.steps = steps_read
    return detail


@router.get("/dashboard/stats", response_model=DashboardStats)
@router.get("/executions/dashboard/stats", response_model=DashboardStats, include_in_schema=False)
def get_dashboard_stats(session: Session = Depends(get_session)):
    # Total Executions
    total = session.exec(select(func.count(TestExecution.id))).one()
    
    if total == 0:
        return DashboardStats(total_executions=0, pass_rate=0.0, avg_duration=0.0, top_failed_scenarios=[])

    # Pass Rate
    passed = session.exec(select(func.count(TestExecution.id)).where(TestExecution.status == 'PASS')).one()
    pass_rate = round((passed / total) * 100, 1) if total > 0 else 0.0
    
    # Avg Duration (only for completed ones)
    completed_executions = session.exec(select(TestExecution).where(TestExecution.end_time != None)).all()
    total_duration = 0
    count_duration = 0
    
    for e in completed_executions:
        if e.start_time and e.end_time:
            total_duration += (e.end_time - e.start_time).total_seconds()
            count_duration += 1
            
    avg_duration = round(total_duration / count_duration, 1) if count_duration > 0 else 0.0
    
    # Top Failing Scenarios
    # Group by scenario_id, count total, count failures
    # Output: (scenario_id, name, total_count, fail_count)
    stats_query = (
        select(
            TestExecution.scenario_id,
            TestScenario.name,
            func.count(TestExecution.id).label("total"),
            func.sum(case((TestExecution.status != "PASS", 1), else_=0)).label("fail_count")
        )
        .join(TestScenario, TestExecution.scenario_id == TestScenario.id)
        .group_by(TestExecution.scenario_id, TestScenario.name)
        .order_by(desc("fail_count"))
        .limit(3)
    )
    
    top_stats = session.exec(stats_query).all()
    
    top_failed = []
    for row in top_stats:
        # row is (scenario_id, name, total, fail_count)
        s_id, s_name, s_total, s_fail = row
        s_fail = s_fail or 0
        if s_fail > 0:
            rate = round((s_fail / s_total) * 100, 1)
            top_failed.append(FailingScenario(
                id=s_id,
                name=s_name,
                fail_count=s_fail,
                fail_rate=rate
            ))
            
    return DashboardStats(
        total_executions=total,
        pass_rate=pass_rate,
        avg_duration=avg_duration,
        top_failed_scenarios=top_failed
    )


from fastapi.responses import FileResponse
from backend.report_generator import report_generator
import os
import base64

@router.get("/executions/{execution_id}/download", response_class=FileResponse)
def download_report(execution_id: int, session: Session = Depends(get_session)):
    execution = session.get(TestExecution, execution_id)
    if not execution:
        raise HTTPException(status_code=404, detail="Execution not found")

    # 1. Try to serve existing report if linked
    if execution.report_id:
        report_path = report_generator.get_report_path(execution.report_id)
        if report_path:
             return FileResponse(
                path=report_path, 
                filename=execution.report_id, 
                media_type="text/html"
            )

    # 2. Fallback: Generate report from DB records
    steps = session.exec(select(TestResult).where(TestResult.execution_id == execution_id).order_by(TestResult.step_order)).all()

    # Use absolute path based on this file's location to find reports dir
    # backend/api/reports.py -> backend/api -> backend -> project_root
    current_file = os.path.abspath(__file__)
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(current_file)))
    reports_dir = os.path.join(project_root, "reports")
    
    steps_results = []

    for step in steps:
        # Load screenshot as base64 if exists
        b64_img = None
        if step.screenshot_path:
            # step.screenshot_path e.g. "screenshots/exec_37_step_9.png"
            full_path = os.path.join(reports_dir, step.screenshot_path)
            
            if os.path.exists(full_path):
                 try:
                    with open(full_path, "rb") as image_file:
                        encoded_string = base64.b64encode(image_file.read()).decode('utf-8')
                        b64_img = encoded_string
                 except Exception:
                     pass

        steps_results.append({
            "step_name": step.step_name,
            "action": "step",
            "description": step.step_name,
            "status": "success" if step.status == "PASS" else "failed",
            "duration": (step.duration or 0) / 1000.0,
            "log": step.error_message or "",
            "error": step.error_message,
            "screenshot": b64_img, # Pass base64 image
            "step_order": step.step_order
        })

    # Since we can't easily modify template now, and previously we saw logic for 'screenshot' (base64)
    # The 'generate_report' function calls 'template.render(steps=steps_results...)'.
    # If the template matches what I saw in similar contexts, it uses 'step.screenshot' for base64.
    # But usually improved templates check for 'screenshot_path'. 
    # For now, let's just generate it. Use start_time/end_time.
    
    start_time = execution.start_time
    end_time = execution.end_time or datetime.now()
    
    report_id = report_generator.generate_report(
        case_id=execution.scenario_id, # Use scenario_id as case_id
        case_name=execution.scenario_name,
        steps_results=steps_results,
        start_time=start_time,
        end_time=end_time,
        variables=[] # No variables stored
    )
    
    # Update execution with new report_id
    execution.report_id = report_id
    session.add(execution)
    session.commit()
    
    # Return the new file
    report_path = report_generator.get_report_path(report_id)
    return FileResponse(
        path=report_path, 
        filename=report_id, 
        media_type="text/html"
    )
