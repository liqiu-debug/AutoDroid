from typing import List, Optional
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select, func, desc
from ..database import get_session
from ..models import TestExecution, TestResult, TestScenario, User
from pydantic import BaseModel

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


# --- API Endpoints ---

@router.get("/executions", response_model=PaginatedTestExecutionRead)
def get_reports(
    skip: int = 0,
    limit: int = 20,
    scenario_id: Optional[int] = None,
    status: Optional[str] = None,
    session: Session = Depends(get_session)
):
    query = select(TestExecution, User.full_name, User.username).outerjoin(User, TestExecution.executor_id == User.id)
    
    if scenario_id:
        query = query.where(TestExecution.scenario_id == scenario_id)
    if status and status != 'all':
        query = query.where(TestExecution.status == status)
        
    count_query = select(func.count(TestExecution.id))
    if scenario_id:
        count_query = count_query.where(TestExecution.scenario_id == scenario_id)
    if status and status != 'all':
        count_query = count_query.where(TestExecution.status == status)
    
    total = session.exec(count_query).one()
    
    query = query.order_by(desc(TestExecution.start_time))
    results = session.exec(query.offset(skip).limit(limit)).all()
    
    response = []
    for execution, f_name, u_name in results:
        data = execution.dict()
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
    
    steps_read = [TestResultRead(**s.dict()) for s in steps]
    
    detail = TestExecutionDetail(**execution.dict())
    detail.steps = steps_read
    return detail


@router.get("/executions/dashboard/stats", response_model=DashboardStats)
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
