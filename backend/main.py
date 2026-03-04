"""
AutoDroid-Pro 后端主入口

FastAPI 应用，提供：
- 测试用例 CRUD API
- 设备交互 API (截图/层级/点击)
- WebSocket 实时执行
- 测试报告 API
"""
import os
import io
import time
import base64
import logging
from typing import List, Optional
from pydantic import BaseModel
from datetime import datetime

from fastapi import FastAPI, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session, SQLModel, create_engine, select
from fastapi.middleware.cors import CORSMiddleware

from .models import TestCase, GlobalVariable
from .schemas import TestCaseCreate, TestCaseRead, InteractionRequest, Step
from .runner import TestRunner
from .socket_manager import manager
from .report_generator import report_generator
from .device_stream.router import router as stream_router
from .device_stream.manager import device_manager

logger = logging.getLogger(__name__)

from .database import engine, create_db_and_tables, get_session
from backend.api import auth, deps
from backend.core.security import get_password_hash
from backend.models import User

# ==================== FastAPI 应用 ====================

app = FastAPI(title="AutoDroid", description="Android UI 自动化低代码平台")

# Mount reports directory for static access (screenshots, html)
# Ensure reports directory exists
if not os.path.exists("reports"):
    os.makedirs("reports")
app.mount("/reports", StaticFiles(directory="reports"), name="reports")

from backend.api import auth, deps, cases

# Register Auth Router
app.include_router(auth.router, prefix="/auth", tags=["auth"])
# Register Case Router
app.include_router(cases.router, prefix="/cases", tags=["cases"])
# Register Folder Router
from backend.api import folders
app.include_router(folders.router, prefix="/folders", tags=["folders"])
# Register Scenario Router
from backend.api import scenarios
app.include_router(scenarios.router, prefix="/scenarios", tags=["scenarios"])
# Register Report Router
from backend.api import reports
app.include_router(reports.router, tags=["reports"])
# Register Tasks Router
from backend.api import tasks
app.include_router(tasks.router, prefix="/tasks", tags=["tasks"])
# Register Settings Router
from backend.api import settings
app.include_router(settings.router, prefix="/settings", tags=["settings"])
# Register Fastbot Router
from backend.api import fastbot
app.include_router(fastbot.router, prefix="/fastbot", tags=["fastbot"])
# Register Log Analysis Router
from backend.api import log_analysis
app.include_router(log_analysis.router, prefix="/fastbot", tags=["log_analysis"])
# Register Device Management Router
from backend.api import devices
app.include_router(devices.router, prefix="/devices", tags=["devices"])
# Register Packages Router
from backend.api import packages
app.include_router(packages.router, prefix="/packages", tags=["packages"])
# Register Environments (Global Variable Library) Router
from backend.api import environments
app.include_router(environments.router, prefix="/environments", tags=["environments"])
# Register AI (NL2Script) Router
from backend.api import ai
app.include_router(ai.router, prefix="/api/ai", tags=["ai"])


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup():
    create_db_and_tables()
    # 启动 Scrcpy 设备监听（独立守护线程，不阻塞主线程）
    device_manager.start_tracking()

    # Create default admin user
    with Session(engine) as session:
        statement = select(User).where(User.username == "admin")
        user = session.exec(statement).first()
        if not user:
            admin_user = User(
                username="admin",
                hashed_password=get_password_hash("123456"),
                role="admin",
                full_name="Administrator"
            )
            session.add(admin_user)
            session.commit()

    # 初始化定时任务调度器并恢复活跃任务
    _restore_scheduled_tasks()


def _restore_scheduled_tasks():
    """从数据库恢复所有活跃的定时任务到调度器"""
    import json
    from backend.scheduler_service import get_scheduler
    from backend.models import ScheduledTask
    from backend.api.tasks import _run_scheduled_scenario

    scheduler = get_scheduler()
    with Session(engine) as session:
        active_tasks = session.exec(
            select(ScheduledTask).where(ScheduledTask.is_active == True)
        ).all()
        for task in active_tasks:
            try:
                config = json.loads(task.strategy_config) if task.strategy_config else {}
                next_run = scheduler.add_task(
                    task_id=task.id,
                    strategy=task.strategy,
                    config=config,
                    job_func=_run_scheduled_scenario,
                )
                task.next_run_time = next_run
                session.add(task)
            except Exception as e:
                logging.error(f"恢复定时任务 #{task.id} 失败: {e}")
        session.commit()
    logging.info(f"已恢复 {len(active_tasks)} 个定时任务")

# 注册 Scrcpy 视频流路由
app.include_router(stream_router)



# ==================== 设备交互 API ====================


@app.post("/run/{case_id}")
def run_test_case(case_id: int, env_id: Optional[int] = None, session: Session = Depends(get_session)):
    """同步执行测试用例（简单模式，不推荐用于长时间用例）"""
    case = session.get(TestCase, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="用例不存在")

    # 获取并合并全局变量
    if env_id:
        from backend.models import GlobalVariable
        global_vars = session.exec(
            select(GlobalVariable).where(GlobalVariable.env_id == env_id)
        ).all()
        # 将全局变量和局部变量合并，优先使用局部变量
        local_vars = {v.key: v.value for v in case.variables}
        for gv in global_vars:
            if gv.key not in local_vars:
                from backend.schemas import Variable
                # 追加到用例的 variables 列表中 (仅在内存中，不保存)
                case.variables.append(Variable(key=gv.key, value=gv.value, description=gv.description))

    runner = TestRunner()
    try:
        return runner.run_case(case)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/device/dump")
def dump_device_info(serial: Optional[str] = None):
    """获取设备信息：截图(base64) + 层级XML + 设备信息"""
    runner = TestRunner(device_serial=serial)
    try:
        runner.connect()
        d = runner.d

        info = d.info
        xml_dump = d.dump_hierarchy()

        image = d.screenshot()
        buffered = io.BytesIO()
        image.save(buffered, format="PNG")
        img_str = base64.b64encode(buffered.getvalue()).decode("utf-8")

        return {
            "device_info": info,
            "hierarchy_xml": xml_dump,
            "screenshot": img_str
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/reports/{report_id}")
def download_report(report_id: str):
    """下载测试报告"""
    # Reports dir is at project root/reports. main.py is in project root/backend
    # So .. from main.py is project root
    reports_dir = os.path.join(os.path.dirname(__file__), "..", "reports")
    report_path = os.path.join(reports_dir, report_id)
    
    if not os.path.exists(report_path):
        raise HTTPException(status_code=404, detail="报告不存在")
    
    return FileResponse(
        path=report_path, 
        filename=report_id, 
        media_type="text/html"
    )


def _take_screenshot_base64(device) -> str:
    """工具函数：截取设备屏幕并返回 base64 字符串"""
    image = device.screenshot()
    buffered = io.BytesIO()
    image.save(buffered, format="PNG")
    return base64.b64encode(buffered.getvalue()).decode("utf-8")


def _build_step_from_inspect(inspect_res: dict, operation: str = "click") -> dict:
    """
    工具函数：根据元素检查结果构建步骤数据。
    
    统一 /device/inspect 和 /device/interact 的步骤生成逻辑。
    """
    element = inspect_res.get("element", {})
    strategy = inspect_res["strategy"]

    if strategy == "image":
        return {
            "action": "click_image",
            "selector": inspect_res["selector"],
            "selector_type": "image",
            "value": "",
            "description": f"图像匹配点击 [{inspect_res.get('fallback_reason', '无属性元素')}]",
            "error_strategy": "ABORT"
        }
    else:
        desc = element.get("text") or element.get("description") or element.get("resourceId") or "element"
        return {
            "action": "click" if operation == "click" else operation,
            "selector": inspect_res["selector"],
            "selector_type": strategy,
            "value": "",
            "description": f"Click [{desc}]",
            "error_strategy": "ABORT"
        }


@app.post("/device/inspect")
def inspect_device(x: int, y: int, serial: Optional[str] = None):
    """
    审查模式：返回指定坐标处的最佳元素和定位策略。
    不执行点击操作，仅分析元素。
    """
    runner = TestRunner(device_serial=serial)
    try:
        runner.connect()
        d = runner.d

        xml_dump = d.dump_hierarchy()
        screenshot_base64 = _take_screenshot_base64(d)

        from .utils import calculate_element_from_coordinates
        inspect_res = calculate_element_from_coordinates(xml_dump, x, y, screenshot_base64)

        if "error" in inspect_res:
            raise HTTPException(status_code=404, detail=inspect_res["error"])

        step = _build_step_from_inspect(inspect_res)

        return {
            "step": step,
            "element": inspect_res.get("element", {}),
            "selector": inspect_res["selector"],
            "strategy": inspect_res["strategy"]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/device/interact")
def interact_with_device(req: InteractionRequest):
    """
    交互模式：分析元素 → 执行点击 → 返回新状态。
    
    流程: 截图分析当前UI → 生成步骤 → 执行操作 → 等待UI稳定 → 返回新截图
    """
    runner = TestRunner(device_serial=req.device_serial)
    try:
        runner.connect()
        d = runner.d

        # 2. 如果是坐标点击，分析点击坐标处的元素
        inspect_res = {}
        if req.operation == "click":
            # 1. 获取当前 UI 层级 (仅点击时需要)
            xml_dump = req.xml_dump or d.dump_hierarchy()
            screenshot_base64 = _take_screenshot_base64(d)
            
            from .utils import calculate_element_from_coordinates
            inspect_res = calculate_element_from_coordinates(xml_dump, req.x, req.y, screenshot_base64)

            # 如果前端传入的 XML 过期，用新的重试
            if "error" in inspect_res:
                logger.info(f"使用缓存XML分析失败，重新获取...")
                xml_dump = d.dump_hierarchy()
                inspect_res = calculate_element_from_coordinates(xml_dump, req.x, req.y, screenshot_base64)

        # 3. 构建步骤
        if req.operation == "click":
             if "error" not in inspect_res:
                step_info = _build_step_from_inspect(inspect_res, req.operation)
             else:
                # 兜底：坐标点击
                logger.warning(f"无法识别元素 ({req.x}, {req.y})，使用坐标点击")
                step_info = {
                    "action": req.operation,
                    "selector": f"{req.x},{req.y}",
                    "selector_type": "xpath",
                    "value": "",
                    "description": f"Click at ({req.x}, {req.y})",
                    "error_strategy": "ABORT"
                }
        else:
            # 全局动作/通用步骤
            step_info = {
                "action": req.operation,
                "selector": req.action_data or "",
                "selector_type": "text" if req.operation in ["start_app", "stop_app", "swipe"] else "resourceId",
                "value": "",
                "description": f"Execute {req.operation} {req.action_data or ''}",
                "error_strategy": "ABORT"
            }

        # 4. 在设备上执行操作
        if req.operation == "click":
            d.click(req.x, req.y)
        elif req.operation == "start_app":
            d.app_start(req.action_data)
        elif req.operation == "stop_app":
            d.app_stop(req.action_data)
        elif req.operation == "back":
            d.press("back")
        elif req.operation == "home":
             d.press("home")
        elif req.operation == "swipe":
             d.swipe_ext(req.action_data or "up", scale=0.8)
        
        # 5. 等待 UI 稳定后返回新状态
        time.sleep(1.0)

        return {
            "step": step_info,
            "dump": {
                "device_info": d.info,
                "hierarchy_xml": d.dump_hierarchy(),
                "screenshot": _take_screenshot_base64(d)
            }
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


class SingleStepPayload(BaseModel):
    step: Step
    env_id: Optional[int] = None
    variables: Optional[List[dict]] = []
    device_serial: Optional[str] = None

@app.post("/device/execute_step")
def execute_single_step(payload: SingleStepPayload, session: Session = Depends(get_session)):
    """
    执行单个步骤 (步骤编排调试用)
    """
    logger.info(f"Received execute_step payload: {payload.dict()}")
    runner = TestRunner(device_serial=payload.device_serial)
    try:
        runner.connect()
        d = runner.d
        
        # Merge global and local variables
        variables_map = {}
        if payload.env_id:
            global_vars = session.exec(
                select(GlobalVariable).where(GlobalVariable.env_id == payload.env_id)
            ).all()
            for gv in global_vars:
                variables_map[gv.key] = gv.value
                
        # Override with local variables
        for v in payload.variables:
            if isinstance(v, dict):
                variables_map[v.get('key')] = v.get('value')
        
        # 执行步骤 
        result = runner.execute_step(payload.step, variables_map)
        
        # 等待 UI 稳定
        time.sleep(0.5)
        
        return {
            "result": result,
            "dump": {
                "device_info": d.info,
                "hierarchy_xml": d.dump_hierarchy(),
                "screenshot": _take_screenshot_base64(d)
            }
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


# ==================== WebSocket 实时执行 ====================


@app.websocket("/ws/run/{case_id}")
async def websocket_run_case(websocket: WebSocket, case_id: int, env_id: Optional[int] = None, device_serial: Optional[str] = None):
    """WebSocket 端点：实时执行测试用例并推送步骤状态"""
    await manager.connect(websocket, case_id)

    try:
        # 获取用例数据
        from backend.models import Environment, GlobalVariable
        with Session(engine) as session:
            case = session.get(TestCase, case_id)
            if not case:
                await websocket.send_json({"type": "error", "message": "用例不存在"})
                return
            steps = case.steps or []
            variables = case.variables or []

            # 准备变量映射表 (全局变量优先级低于用例局部变量)
            variables_map = {}
            if env_id:
                global_vars = session.exec(
                    select(GlobalVariable).where(GlobalVariable.env_id == env_id)
                ).all()
                for gv in global_vars:
                    variables_map[gv.key] = gv.value

        # 广播执行开始
        await manager.broadcast_run_start(case_id, case.name, len(steps))

        # 覆盖局部变量
        for v in (variables if isinstance(variables, list) else []):
            if isinstance(v, dict):
                variables_map[v.get('key')] = v.get('value')
            else:
                variables_map[v.key] = v.value

        # 连接设备
        runner = TestRunner(device_serial=device_serial)
        try:
            runner.connect()
        except Exception as e:
            await websocket.send_json({"type": "error", "message": f"设备连接失败: {e}"})
            return

        # 逐步执行
        start_time = datetime.now()
        steps_results = []
        passed = 0
        failed = 0

        for i, step in enumerate(steps):
            step_start = time.time()

            # 广播步骤开始
            try:
                desc = step.get('description', '') if isinstance(step, dict) else step.description
                action = step.get('action') if isinstance(step, dict) else step.action
                await manager.broadcast_step_update(
                    case_id, i, "running",
                    f"[{i+1}/{len(steps)}] 执行 {action}: {desc}"
                )
            except Exception:
                pass

            try:
                # 确保设备连接
                if not runner.d:
                    runner.connect()

                # 将 dict 转换为 Step 对象
                step_obj = Step(**step) if isinstance(step, dict) else step
                result = runner.execute_step(step_obj, variables_map)

                if not result.get("success"):
                    raise Exception(result.get("error", "未知错误"))

                duration = time.time() - step_start
                passed += 1

                step_data = step if isinstance(step, dict) else step.dict()
                steps_results.append({
                    **step_data,
                    "status": "success",
                    "duration": round(duration, 2),
                    "log": f"✓ 步骤成功 ({round(duration, 2)}s)"
                })

                await manager.broadcast_step_update(
                    case_id, i, "success",
                    f"✓ 步骤 {i+1} 成功",
                    duration
                )

            except Exception as e:
                duration = time.time() - step_start

                strategy = step.get("error_strategy", "ABORT") if isinstance(step, dict) else getattr(step, "error_strategy", "ABORT")

                # 失败时尝试截图
                screenshot_base64 = None
                try:
                    screenshot_base64 = _take_screenshot_base64(runner.d)
                except Exception:
                    pass

                step_data = step if isinstance(step, dict) else step.dict()

                if strategy == "IGNORE":
                    steps_results.append({
                        **step_data,
                        "status": "warning",
                        "duration": round(duration, 2),
                        "log": f"⚠ 步骤失败(IGNORE): {str(e)}",
                        "error": str(e),
                        "screenshot": screenshot_base64
                    })
                    await manager.broadcast_step_update(
                        case_id, i, "warning",
                        f"⚠ 步骤 {i+1} 失败(已忽略): {str(e)}",
                        duration,
                        screenshot_base64,
                        str(e)
                    )
                else:
                    failed += 1
                    steps_results.append({
                        **step_data,
                        "status": "failed",
                        "duration": round(duration, 2),
                        "log": f"✗ 步骤失败: {str(e)}",
                        "error": str(e),
                        "screenshot": screenshot_base64
                    })
                    await manager.broadcast_step_update(
                        case_id, i, "failed",
                        f"✗ 步骤 {i+1} 失败: {str(e)}",
                        duration,
                        screenshot_base64,
                        str(e)
                    )

                    if strategy == "ABORT":
                        break

        # 生成测试报告
        end_time = datetime.now()
        total_duration = (end_time - start_time).total_seconds()

        report_id = report_generator.generate_report(
            case_id=case_id,
            case_name=case.name,
            steps_results=steps_results,
            start_time=start_time,
            end_time=end_time,
            variables=variables
        )

        # 广播执行完成
        await manager.broadcast_run_complete(
            case_id,
            success=(failed == 0),
            total_duration=total_duration,
            passed=passed,
            failed=failed,
            report_id=report_id
        )

    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(websocket, case_id)


# ==================== 报告 API ====================


# Old report file APIs removed in favor of DB-based reports


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)



