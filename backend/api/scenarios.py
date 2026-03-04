from typing import List, Optional
import os
import base64
import io
from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks
from sqlmodel import Session, select, func
from datetime import datetime

from backend.database import get_session, engine
from backend.models import TestScenario, ScenarioStep, User, TestCase, Step, TestExecution, TestResult, Device
from backend.schemas import TestScenarioCreate, TestScenarioRead, ScenarioStepCreate, ScenarioStepRead, PaginatedTestScenarioRead
from backend.api import deps
import uuid
import asyncio
from concurrent.futures import ThreadPoolExecutor
from backend.schemas import TestScenarioCreate, TestScenarioRead, ScenarioStepCreate, ScenarioStepRead, PaginatedTestScenarioRead, ScenarioRunRequest
from backend.runner import ScenarioRunner, register_device_abort, trigger_device_abort, unregister_device_abort

router = APIRouter()

@router.post("/", response_model=TestScenarioRead)
def create_scenario(
    scenario: TestScenarioCreate, 
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user)
):
    """Create a new scenario"""
    db_scenario = TestScenario.from_orm(scenario)
    db_scenario.user_id = current_user.id
    db_scenario.updater_id = current_user.id
    db_scenario.created_at = datetime.now()
    session.add(db_scenario)
    session.commit()
    session.refresh(db_scenario)
    return db_scenario

@router.get("/", response_model=PaginatedTestScenarioRead)
def list_scenarios(
    skip: int = 0,
    limit: int = 100,
    keyword: Optional[str] = None,
    session: Session = Depends(get_session)
):
    """List scenarios with pagination and filtering"""
    from sqlalchemy.orm import aliased
    Creator = aliased(User)
    Updater = aliased(User)
    
    query = session.query(TestScenario, Creator.full_name, Creator.username, Updater.full_name, Updater.username)\
        .outerjoin(Creator, TestScenario.user_id == Creator.id)\
        .outerjoin(Updater, TestScenario.updater_id == Updater.id)
    
    if keyword:
        query = query.filter(TestScenario.name.contains(keyword))
        
    count_query = session.query(func.count(TestScenario.id))
    if keyword:
        count_query = count_query.filter(TestScenario.name.contains(keyword))
    total = count_query.scalar()
        
    query = query.order_by(TestScenario.created_at.desc())
    query = query.offset(skip).limit(limit)
    
    results = query.all()
    
    scenario_list = []
    for scenario, c_full, c_user, u_full, u_user in results:
        read_obj = TestScenarioRead.from_orm(scenario)
        read_obj.creator_name = c_full or c_user or "Unknown"
        read_obj.updater_name = u_full or u_user or "Unknown"
        scenario_list.append(read_obj)
        
    return PaginatedTestScenarioRead(total=total, items=scenario_list)

@router.get("/{scenario_id}", response_model=TestScenarioRead)
def get_scenario(scenario_id: int, session: Session = Depends(get_session)):
    """Get a single scenario"""
    scenario = session.get(TestScenario, scenario_id)
    if not scenario:
        raise HTTPException(status_code=404, detail="Scenario not found")
    return scenario

@router.put("/{scenario_id}", response_model=TestScenarioRead)
def update_scenario(
    scenario_id: int, 
    scenario: TestScenarioCreate, 
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user)
):
    """Update scenario details"""
    db_scenario = session.get(TestScenario, scenario_id)
    if not db_scenario:
        raise HTTPException(status_code=404, detail="Scenario not found")
    
    db_scenario.name = scenario.name
    if scenario.description is not None:
        db_scenario.description = scenario.description
        
    db_scenario.updater_id = current_user.id
    db_scenario.updated_at = datetime.now()
        
    session.add(db_scenario)
    session.commit()
    session.refresh(db_scenario)
    return db_scenario

@router.delete("/{scenario_id}")
def delete_scenario(scenario_id: int, session: Session = Depends(get_session)):
    """Delete a scenario"""
    scenario = session.get(TestScenario, scenario_id)
    if not scenario:
        raise HTTPException(status_code=404, detail="Scenario not found")
    
    # Cascade delete steps
    steps = session.exec(select(ScenarioStep).where(ScenarioStep.scenario_id == scenario_id)).all()
    for s in steps:
        session.delete(s)
        
    session.delete(scenario)
    session.commit()
    return {"message": "Scenario deleted", "id": scenario_id}

# ---- Steps Management ----

@router.get("/{scenario_id}/steps", response_model=List[ScenarioStepRead])
def get_scenario_steps(scenario_id: int, session: Session = Depends(get_session)):
    """Get steps for a scenario"""
    steps = session.exec(select(ScenarioStep).where(ScenarioStep.scenario_id == scenario_id).order_by(ScenarioStep.order)).all()
    return steps

@router.post("/{scenario_id}/steps")
def update_scenario_steps(
    scenario_id: int, 
    steps: List[ScenarioStepCreate], 
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user)
):
    """Replace all steps in a scenario"""
    scenario = session.get(TestScenario, scenario_id)
    if not scenario:
        raise HTTPException(status_code=404, detail="Scenario not found")
    
    # 1. Delete old steps
    old_steps = session.exec(select(ScenarioStep).where(ScenarioStep.scenario_id == scenario_id)).all()
    for s in old_steps:
        session.delete(s)
    
    # 2. Add new steps
    for s_in in steps:
        new_step = ScenarioStep(
            scenario_id=scenario_id,
            case_id=s_in.case_id,
            order=s_in.order,
            alias=s_in.alias
        )
        session.add(new_step)
        
    # 3. Update scenario stats
    scenario.step_count = len(steps)
    scenario.updated_at = datetime.now()
    scenario.updater_id = current_user.id
    session.add(scenario)
    
    session.commit()
    return {"success": True, "count": len(steps)}

# ---- Execution ----

def _run_single_device_sync(execution_id: int, scenario_id: int, device_serial: Optional[str] = None, env_id: Optional[int] = None):
    """核心：每个子线程内独立的执行逻辑。必须使用独立的数据库 Session 防止并发冲突"""
    from sqlmodel import Session as SQLSession
    
    abort_event = None
    with SQLSession(engine) as session:
        execution = session.get(TestExecution, execution_id)
        if not execution:
            return
            
        execution.status = "RUNNING"
        execution.start_time = datetime.now()
        session.add(execution)
        session.commit()
        
        scenario = session.get(TestScenario, scenario_id)
        if not scenario:
            execution.status = "ERROR"
            execution.log = "Scenario not found"
            session.add(execution)
            session.commit()
            return
            
        runner = ScenarioRunner(device_serial=device_serial)
        try:
            start_time = execution.start_time
            
            # ★ 先连接设备，获取 serial 并标记为 BUSY
            try:
                runner.runner.connect()
                if runner.runner.d:
                    device_serial = runner.runner.d.serial
                    # ★ 注册中止事件并传给 runner
                    abort_event = register_device_abort(device_serial)
                    runner.abort_event = abort_event
                    runner.runner.abort_event = abort_event

                    dev = session.exec(select(Device).where(Device.serial == device_serial)).first()
                    if dev:
                        dev.status = "BUSY"
                        dev.updated_at = datetime.now()
                        session.add(dev)
                        session.commit()
            except Exception as e:
                print(f"设备连接失败: {e}")

            result = runner.run_scenario(scenario_id, session, env_id=env_id)

            end_time = datetime.now()
            
            duration = (end_time - start_time).total_seconds()
            
            # --- Result Processing & DB Saving ---
            
            raw_results = result.get("results", [])
            cases_results = []
            
            global_step_order = 1
            
            for item in raw_results:
                case_res = item.get("result", {})
                success = case_res.get("success", False)
                steps = case_res.get("steps", [])
                
                # Check for screenshot in case result
                error_screenshot = case_res.get("last_error_screenshot")
                
                formatted_steps = []
                for s in steps:
                    s_step = s.get("step", {})
                    
                    # 2. Save TestResult to DB
                    step_status = "PASS" if s.get("success") else ("WARNING" if s.get("is_warning") else "FAIL")
                    step_desc = s_step.get("description") or f"{s_step.get('action')} {s_step.get('selector') or ''}"
                    
                    screenshot_path = None
                    screenshot_b64 = None
                    if not s.get("success") and error_screenshot:
                        try:
                            filename = f"exec_{execution.id}_step_{global_step_order}.png"
                            reports_dir = os.path.join(os.getcwd(), "reports", "screenshots")
                            os.makedirs(reports_dir, exist_ok=True)
                            full_path = os.path.join(reports_dir, filename)
                            
                            # Save to file
                            error_screenshot.save(full_path)
                            screenshot_path = f"screenshots/{filename}"
                            
                            # Encode to base64 for report
                            buffered = io.BytesIO()
                            error_screenshot.save(buffered, format="PNG")
                            screenshot_b64 = base64.b64encode(buffered.getvalue()).decode("utf-8")
                            
                            # Consume screenshot
                            error_screenshot = None
                        except Exception as e:
                             print(f"Failed to save background screenshot: {e}")

                    test_result = TestResult(
                        execution_id=execution.id,
                        step_name=f"[{item.get('alias') or item.get('case_name')}] {step_desc}",
                        step_order=global_step_order,
                        status=step_status,
                        duration=s.get("duration", 0) * 1000, # ms
                        error_message=s.get("error"),
                        screenshot_path=screenshot_path
                    )
                    session.add(test_result)
                    global_step_order += 1
                    
                    formatted_steps.append({
                        "status": "success" if s.get("success") else ("warning" if s.get("is_warning") else "failed"),
                        "action": s_step.get("action"),
                        "description": s_step.get("description"),
                        "selector": s_step.get("selector"),
                        "selector_type": s_step.get("selector_type"),
                        "duration": round(s.get("duration", 0), 2),
                        "error": s.get("error"),
                        "screenshot": screenshot_b64
                    })
                
                cases_results.append({
                    "case_id": case_res.get("case_id"),
                    "case_name": item.get("case_name"),
                    "alias": item.get("alias"),
                    "status": "failed" if not success else ("warning" if case_res.get("is_warning") else "success"),
                    "steps": formatted_steps
                })
            
            session.commit() # Save results
            
            # Generate Report (HTML)
            from backend.report_generator import report_generator
            
            report_id = None
            try:
                report_id = report_generator.generate_scenario_report(
                    scenario_id=scenario_id,
                    scenario_name=scenario.name,
                    cases_results=cases_results,
                    start_time=start_time,
                    end_time=end_time
                )
            except Exception as e:
                print(f"Background report generation failed: {e}")

            # 3. Update TestExecution
            execution.status = "PASS" if result.get("success") else "FAIL"
            execution.end_time = end_time
            execution.duration = duration
            if report_id:
                execution.report_id = report_id
            session.add(execution)

            # Update Scenario Status
            scenario.last_run_status = "PASS" if result.get("success") else "FAIL"
            scenario.last_run_time = end_time
            scenario.last_run_duration = int(duration)
            scenario.last_execution_id = execution.id
            scenario.last_executor = execution.executor_name
            
            last_failed_step_name = None
            if not result.get("success"):
                for item in raw_results:
                    if not item.get("result", {}).get("success"):
                        for step in item.get("result", {}).get("steps", []):
                            if not step.get("success") and not step.get("is_warning"):
                                s_step = step.get("step", {})
                                step_desc = s_step.get("description") or s_step.get("selector") or s_step.get("action") or "未知操作"
                                case_name = item.get("alias") or item.get("case_name", "未知用例")
                                last_failed_step_name = f"[{case_name}] {step_desc}"
                                break
                        if last_failed_step_name:
                            break
            
            scenario.last_failed_step = last_failed_step_name

            if report_id:
                scenario.last_report_id = report_id
                
            session.add(scenario)
            session.commit()
        except Exception as e:
            print(f"Error running scenario {scenario_id}: {e}")
            scenario.last_run_status = "FAIL"
            scenario.last_execution_id = execution.id if 'execution' in locals() else None
            if 'start_time' in locals():
                scenario.last_run_duration = int((datetime.now() - start_time).total_seconds())
            session.add(scenario)
            
            # Fail the execution record if exists
            if 'execution' in locals():
                execution.status = "ERROR"
                execution.end_time = datetime.now()
                session.add(execution)
                
            session.commit()
        finally:
            # ★ 恢复设备状态
            if device_serial:
                try:
                    dev = session.exec(select(Device).where(Device.serial == device_serial)).first()
                    if dev and dev.status == "BUSY":
                        dev.status = "IDLE"
                        dev.updated_at = datetime.now()
                        session.add(dev)
                        session.commit()
                except Exception:
                    pass
                # ★ 清除中止事件注册
                unregister_device_abort(device_serial)

async def _schedule_concurrent_runs(execution_ids: List[int], scenario_id: int, device_serials: List[str], env_id: Optional[int] = None):
    """使用 ThreadPoolExecutor 并发执行每个设备的测试"""
    loop = asyncio.get_running_loop()
    
    # 创建线程池，最大 worker 数量与设备数量一致，保证并发
    max_workers = len(device_serials) if device_serials else 1
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        tasks = []
        for exec_id, serial in zip(execution_ids, device_serials):
            # run_in_executor 将同步阻塞的 Runner 任务放入线程池调度
            task = loop.run_in_executor(
                executor, 
                _run_single_device_sync,  # 传入同步目标函数
                exec_id, 
                scenario_id, 
                serial, 
                env_id
            )
            tasks.append(task)
            
        # 等待所有设备上的执行任务全部返回
        if tasks:
            await asyncio.gather(*tasks)

def execute_scenario_batch_background(scenario_id: int, executor_name: str, env_id: Optional[int], device_serials: List[str]):
    """Background task used by tasks.py to execute tests concurrently on multiple devices."""
    from sqlmodel import Session as SQLSession
    from backend.database import engine
    import uuid
    import asyncio
    
    with SQLSession(engine) as session:
        scenario = session.get(TestScenario, scenario_id)
        if not scenario: return None
        
        batch_id = str(uuid.uuid4())
        execution_ids = []
        
        if not device_serials:
            device_serials = [None]
            
        for serial in device_serials:
            device_display_name = serial or "Scheduled Runner"
            if serial:
                from backend.models import Device
                dev = session.exec(select(Device).where(Device.serial == serial)).first()
                if dev:
                    name_part = dev.custom_name or dev.market_name or dev.model
                    device_display_name = name_part

            execution = TestExecution(
                scenario_id=scenario_id,
                scenario_name=scenario.name,
                status="PENDING",
                executor_id=None,
                executor_name=executor_name,
                device_info=device_display_name,
                batch_id=batch_id,
                batch_name=f"{scenario.name} 定时执行"
            )
            session.add(execution)
            session.commit()
            session.refresh(execution)
            execution_ids.append(execution.id)
            
    # Run concurrently and block the APScheduler thread until all finish
    asyncio.run(_schedule_concurrent_runs(execution_ids, scenario_id, device_serials, env_id))
    return batch_id

@router.post("/{scenario_id}/run")
async def run_scenario_api(
    scenario_id: int,
    request: ScenarioRunRequest,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user)
):
    """触发场景在多个设备上的并发执行"""
    scenario = session.get(TestScenario, scenario_id)
    if not scenario:
        raise HTTPException(status_code=404, detail="Scenario not found")
        
    executor_name = current_user.full_name or current_user.username
    
    batch_id = str(uuid.uuid4())
    execution_ids = []
    
    device_serials = request.device_serials
    if not device_serials:
        device_serials = [None] # If empty, run default once
    
    for serial in device_serials:
        device_display_name = serial or "Default Runner"
        if serial:
            from backend.models import Device
            dev = session.exec(select(Device).where(Device.serial == serial)).first()
            if dev:
                name_part = dev.custom_name or dev.market_name or dev.model
                if name_part:
                    device_display_name = name_part

        execution = TestExecution(
            scenario_id=scenario_id,
            scenario_name=scenario.name,
            status="PENDING", 
            executor_id=current_user.id,
            executor_name=executor_name,
            device_info=device_display_name,
            batch_id=batch_id,
            batch_name=f"{scenario.name} 并发运行"
        )
        session.add(execution)
        session.commit()
        session.refresh(execution)
        execution_ids.append(execution.id)
        
    asyncio.create_task(_schedule_concurrent_runs(
        execution_ids=execution_ids,
        scenario_id=scenario_id,
        device_serials=device_serials,
        env_id=request.env_id
    ))
    
    return {
        "message": "Batch execution started", 
        "batch_id": batch_id,
        "execution_ids": execution_ids
    }

# ---- WebSocket Execution ----

from fastapi import WebSocket, WebSocketDisconnect
from backend.socket_manager import manager
from backend.report_generator import report_generator
from backend.runner import TestRunner
import time
import logging

logger = logging.getLogger(__name__)

@router.websocket("/ws/run/{scenario_id}")
async def websocket_run_scenario(websocket: WebSocket, scenario_id: int, env_id: Optional[int] = None, device_serial: Optional[str] = None):
    """WebSocket endpoint: Run scenario with real-time logs"""
    ws_key = f"scenario:{scenario_id}"
    await manager.connect(websocket, ws_key)

    try:
        # 1. Get Scenario Data
        from sqlmodel import Session as SQLSession
        
        with SQLSession(engine) as session:
            scenario = session.get(TestScenario, scenario_id)
            if not scenario:
                await manager.broadcast_log(ws_key, "error", "场景不存在")
                return
            
            # Figure out executor_name for WebSocket
            executor_name = "System"
            if scenario.updater_id:
                updater = session.get(User, scenario.updater_id)
                if updater:
                    executor_name = updater.full_name or updater.username
                    
            # Create Execution Record
            start_time = datetime.now()
            execution = TestExecution(
                scenario_id=scenario_id,
                scenario_name=scenario.name,
                status="RUNNING",
                start_time=start_time,
                executor_id=scenario.updater_id, # Approximate
                executor_name=executor_name,
                device_info="WebSocket Runner"
            )
            session.add(execution)
            session.commit()
            session.refresh(execution)
            
            # Get steps (ordered)
            steps_db = session.exec(select(ScenarioStep).where(ScenarioStep.scenario_id == scenario_id).order_by(ScenarioStep.order)).all()
            total_steps = len(steps_db)
            
            await manager.broadcast_log(ws_key, "info", f"🎬 开始执行场景: {scenario.name} (共 {total_steps} 个步骤)")

            # 2. Prepare Runner
            runner = TestRunner(device_serial=device_serial)
            try:
                runner.connect()
                await manager.broadcast_log(ws_key, "info", "✅ 设备连接成功")
                
                # Update Device Info & Status
                device_serial_ws = getattr(runner.d, 'serial', None)
                if device_serial_ws:
                    abort_event_ws = register_device_abort(device_serial_ws)
                    runner.abort_event = abort_event_ws
                    dev = session.exec(select(Device).where(Device.serial == device_serial_ws)).first()
                    if dev:
                        dev.status = "BUSY"
                        dev.updated_at = datetime.now()
                        name_part = dev.custom_name or dev.market_name or dev.model
                        if name_part:
                            execution.device_info = name_part
                        session.add(dev)
                    else:
                        if runner.d and runner.d.info:
                            info = runner.d.info
                            execution.device_info = f"{info.get('manufacturer')} {info.get('model')} (Android {info.get('version')})"
                            
                    session.add(execution)
                    session.commit()
                    
            except Exception as e:
                await manager.broadcast_log(ws_key, "error", f"❌ 设备连接失败: {e}")
                execution.status = "ERROR"
                execution.end_time = datetime.now()
                session.add(execution)
                session.commit()
                return

            cases_results = [] # Collect results for report
            success_count = 0
            fail_count = 0
            global_step_order = 1
            scenario_context = {}
            from backend.models import GlobalVariable
            if env_id:
                global_vars = session.exec(
                    select(GlobalVariable).where(GlobalVariable.env_id == env_id)
                ).all()
                for gv in global_vars:
                    scenario_context[gv.key] = gv.value
            
            # 3. Execute Steps Sequentially
            for idx, step_db in enumerate(steps_db):
                # ★ 检查中止信号
                if 'abort_event_ws' in dir() and abort_event_ws and abort_event_ws.is_set():
                    await manager.broadcast_log(ws_key, "warning", "⚠️ 收到中止信号，停止执行")
                    fail_count += 1
                    break

                step_name = step_db.alias or f"Step {idx+1}"
                
                # Get TestCase
                case = session.get(TestCase, step_db.case_id)
                if not case:
                    await manager.broadcast_log(ws_key, "warning", f"⚠️ 步骤 {idx+1} ({step_name}): 用例不存在 (ID: {step_db.case_id})，跳过")
                    fail_count += 1
                    continue
                
                await manager.broadcast_log(ws_key, "info", f"👉 [{idx+1}/{total_steps}] 执行: {step_name} ({case.name})")

                # Prepare variables
                variables_map = dict(scenario_context)
                
                for v in case.variables:
                    variables_map[v.key] = v.value
                
                # Execute all steps in the case
                case_success = True
                case_start_time = time.time()
                current_case_steps = []
                
                # Loop through case steps
                for case_step in case.steps:
                    step_start = time.time()
                    step_log_entry = {
                        "action": case_step.action,
                        "description": case_step.description,
                        "selector": case_step.selector,
                        "selector_type": case_step.selector_type
                    }
                    
                    screenshot_base64 = None
                    screenshot_path = None
                    
                    try:
                        action_desc = f"{case_step.action} {case_step.selector or ''}"
                        step_res = runner.execute_step(case_step, variables_map)
                        
                        step_duration = time.time() - step_start
                        step_log_entry["duration"] = round(step_duration, 2)
                        
                        if not step_res["success"]:
                            error_msg = step_res.get("error")
                            strategy = getattr(case_step, "error_strategy", "ABORT")
                            
                            if strategy == "IGNORE":
                                await manager.broadcast_log(ws_key, "warning", f"    🟡 忽略错误: {error_msg} ({action_desc})")
                                step_log_entry["status"] = "warning"
                                step_log_entry["error"] = error_msg
                                step_log_entry["strategy"] = "IGNORE"
                                # Important: do NOT set case_success = False for IGNORE
                            else:
                                await manager.broadcast_log(ws_key, "error", f"    ❌ 失败: {error_msg} ({action_desc}) [策略: {strategy}]")
                                case_success = False
                                step_log_entry["status"] = "failed"
                                step_log_entry["error"] = error_msg
                                step_log_entry["strategy"] = strategy
                            
                            # Take Screenshot on Failure or Warning
                            try:
                                image = runner.d.screenshot()
                                
                                # 1. For WebSocket
                                buffered = io.BytesIO()
                                image.save(buffered, format="PNG")
                                screenshot_base64 = base64.b64encode(buffered.getvalue()).decode("utf-8")
                                
                                # 2. For DB / File
                                filename = f"exec_{execution.id}_step_{global_step_order}.png"
                                # Save to reports/screenshots
                                reports_dir = os.path.join(os.getcwd(), "reports", "screenshots")
                                os.makedirs(reports_dir, exist_ok=True)
                                full_path = os.path.join(reports_dir, filename)
                                image.save(full_path)
                                screenshot_path = f"screenshots/{filename}"
                                
                            except Exception as scr_err:
                                print(f"Screenshot failed: {scr_err}")

                            current_case_steps.append(step_log_entry)
                        else:
                             step_log_entry["status"] = "success"
                             current_case_steps.append(step_log_entry)
                        
                        # Add screenshot to step log entry if exists (for Report)
                        if screenshot_base64:
                            step_log_entry["screenshot"] = screenshot_base64
                             
                        # Save TestResult
                        step_db_status = "PASS" if step_res["success"] else ("WARNING" if strategy == "IGNORE" else "FAIL")
                        
                        test_result = TestResult(
                            execution_id=execution.id,
                            step_name=f"[{step_name}] {case_step.description or case_step.action}",
                            step_order=global_step_order,
                            status=step_db_status,
                            duration=step_duration * 1000,
                            error_message=step_res.get("error"),
                            screenshot_path=screenshot_path
                        )
                        session.add(test_result)
                        session.commit()
                        global_step_order += 1
                        
                        if not step_res["success"]:
                            if strategy == "ABORT":
                                # Stop case execution on failure
                                break 
                            # If CONTINUE or IGNORE, do not break

                    except Exception as e:
                        await manager.broadcast_log(ws_key, "error", f"    ❌ 异常: {e}")
                        case_success = False
                        step_log_entry["status"] = "failed"
                        step_log_entry["error"] = str(e)
                        step_log_entry["duration"] = round(time.time() - step_start, 2)
                        current_case_steps.append(step_log_entry)
                        
                        # Save TestResult for Exception
                        test_result = TestResult(
                            execution_id=execution.id,
                            step_name=f"[{step_name}] {case_step.description or case_step.action}",
                            step_order=global_step_order,
                            status="FAIL",
                            duration=(time.time() - step_start) * 1000,
                            error_message=str(e)
                        )
                        session.add(test_result)
                        session.commit()
                        global_step_order += 1
                        break
                
                duration = time.time() - case_start_time
                if case_success:
                    success_count += 1
                    await manager.broadcast_log(ws_key, "success", f"  ✓ 通过 (耗时 {duration:.2f}s)")
                else:
                    fail_count += 1
                    # Send error log with screenshot to WebSocket
                    await manager.broadcast_log(
                        ws_key, 
                        "error", 
                        f"  ✗ 失败 (耗时 {duration:.2f}s)",
                        attachment=screenshot_base64,
                        attachment_type="image"
                    )
                
                # Record Case Result for HTML Report
                has_step_warnings = any(s.get("status") == "warning" for s in current_case_steps)
                case_status = "failed" if not case_success else ("warning" if has_step_warnings else "success")
                    
                cases_results.append({
                    "case_id": case.id,
                    "case_name": case.name,
                    "alias": step_db.alias,
                    "status": case_status,
                    "duration": round(duration, 2),
                    "steps": current_case_steps
                })
                scenario_context.update(variables_map)
                
                # If case failed, we should only stop scenario if NO steps had CONTINUE strategy?
                # Actually, the requirement says CONTINUE means "fails but continues downwards (scenario continues)".
                # ABORT means "immediately aborts scenario execution".
                # Let's check if the case failure was caused by an ABORT step.
                last_failed_strategy = "ABORT"
                for s in current_case_steps[::-1]:
                    if s.get("status") == "failed":
                        last_failed_strategy = s.get("strategy", "ABORT")
                        break
                        
                if not case_success and last_failed_strategy == "ABORT":
                    break

            # 4. Generate Report
            end_time = datetime.now()
            report_id = None
            try:
                report_id = report_generator.generate_scenario_report(
                    scenario_id=scenario_id,
                    scenario_name=scenario.name,
                    cases_results=cases_results,
                    start_time=start_time,
                    end_time=end_time
                )
                await manager.broadcast_log(ws_key, "success", f"📊 报告已生成: {report_id}")
            except Exception as e:
                logger.error(f"报告生成失败: {e}")
                await manager.broadcast_log(ws_key, "error", f"报告生成失败: {e}")

            # 5. Summary & Update DB
            total_duration = (end_time - start_time).total_seconds()
            final_status = "success" if fail_count == 0 else "warning"
            summary_msg = f"🏁 执行结束: 总耗时 {total_duration:.2f}s | 成功 {success_count} | 失败 {fail_count}"
            
            # Update Scenario
            scenario.last_run_status = "PASS" if fail_count == 0 else "FAIL"
            scenario.last_run_time = end_time
            scenario.last_run_duration = int(total_duration)
            scenario.last_execution_id = execution.id
            scenario.last_executor = execution.executor_name
            
            last_failed_step_name = None
            if fail_count > 0:
                for item in cases_results:
                    if item.get("status") == "failed":
                        for step in item.get("steps", []):
                            if step.get("status") == "failed":
                                step_desc = step.get("description") or step.get("selector") or step.get("action") or "未知操作"
                                case_name = item.get("alias") or item.get("case_name", "未知用例")
                                last_failed_step_name = f"[{case_name}] {step_desc}"
                                break
                        if last_failed_step_name:
                            break
            
            scenario.last_failed_step = last_failed_step_name

            if report_id:
                scenario.last_report_id = report_id
            session.add(scenario)
            
            # Update Execution
            execution.status = "PASS" if fail_count == 0 else "FAIL"
            execution.end_time = end_time
            execution.duration = int(total_duration)
            if report_id:
                execution.report_id = report_id
            session.add(execution)
            
            session.commit()

            await manager.broadcast_log(ws_key, final_status, summary_msg)
            
            # Send completion message
            await manager.send_message(ws_key, {
                "type": "run_complete",
                "success": fail_count == 0,
                "summary": summary_msg,
                "report_id": report_id,
                "execution_id": execution.id
            })

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected: {ws_key}")
    except Exception as e:
        import traceback
        traceback.print_exc()
        await manager.broadcast_log(ws_key, "error", f"❌ 系统异常: {str(e)}")
    finally:
        # ★ 恢复设备状态 并 清除中止事件
        try:
            from sqlmodel import Session as SQLSession
            with SQLSession(engine) as s:
                if 'device_serial_ws' in dir() and device_serial_ws:
                    dev = s.exec(select(Device).where(Device.serial == device_serial_ws)).first()
                    if dev and dev.status == "BUSY":
                        dev.status = "IDLE"
                        dev.updated_at = datetime.now()
                        s.add(dev)
                        s.commit()
                    unregister_device_abort(device_serial_ws)
        except Exception:
            pass
        manager.disconnect(websocket, ws_key)
