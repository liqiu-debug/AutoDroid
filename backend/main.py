"""
AutoDroid-Pro 后端主入口

FastAPI 应用装配：
- 路由挂载（/api 前缀 + legacy 无前缀别名双挂载）
- CORS / 静态资源 / 报告资源
- startup/shutdown 生命周期
- 定时任务恢复

设备录制与单步执行见 backend.api.recording，
WebSocket 实时执行见 backend.api.ws_run，
SPA 前端托管见 backend.spa。
"""
import logging
import os
from pathlib import Path
from typing import List, Optional
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, FastAPI, Depends, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session, select
from fastapi.middleware.cors import CORSMiddleware

from .database import backfill_legacy_asset_owners, engine, create_db_and_tables, get_session
from .device_stream.router import rest_router as stream_rest_router
from .device_stream.router import ws_router as stream_ws_router
from .device_stream.manager import device_manager
from .wda_port_manager import wda_relay_manager
from backend.core.security import get_password_hash
from backend.models import User

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = PROJECT_ROOT / "reports"
STATIC_DIR = PROJECT_ROOT / "static"
REPORT_ASSET_API_PREFIX = "/api/report-assets"
REPORT_ASSET_DEV_PREFIX = "/report-assets"
REPORT_API_RESERVED_SEGMENTS = {"executions", "dashboard"}

# ==================== FastAPI 应用 ====================

app = FastAPI(title="AutoDroid", description="Android UI 自动化低代码平台")
api_router = APIRouter(prefix="/api")

# Mount reports directory for canonical static asset access
REPORTS_DIR.mkdir(exist_ok=True)
app.mount(REPORT_ASSET_API_PREFIX, StaticFiles(directory=str(REPORTS_DIR)), name="report_assets")
app.mount(REPORT_ASSET_DEV_PREFIX, StaticFiles(directory=str(REPORTS_DIR)), name="report_assets_dev")

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.mount("/api/static", StaticFiles(directory=str(STATIC_DIR)), name="api_static")

from backend import spa
from backend.api import auth, cases
from backend.api import admin
from backend.api import tokens

from backend.api import folders
from backend.api import scenarios
from backend.api import reports
from backend.api import runs
from backend.api import tasks
from backend.api import settings
from backend.api import fastbot
from backend.api import log_analysis
from backend.api import devices
from backend.api import packages
from backend.api import compatibility
from backend.api import environments
from backend.api import ai
from backend.api import limiter
from backend.api import deps
from backend.api import recording
from backend.api import ws_run
from backend.api.recording import _recording_ios_session_pool


def _register_http_routers(
    target,
    *,
    include_in_schema: bool,
    ai_prefix: Optional[str] = "/ai",
    include_settings_alias: bool = True,
    reports_prefix: str = "/reports",
) -> None:
    target.include_router(auth.router, prefix="/auth", tags=["auth"], include_in_schema=include_in_schema)
    target.include_router(admin.router, prefix="/admin", tags=["admin"], include_in_schema=include_in_schema)
    target.include_router(tokens.router, prefix="/tokens", tags=["tokens"], include_in_schema=include_in_schema)
    target.include_router(cases.router, prefix="/cases", tags=["cases"], include_in_schema=include_in_schema)
    target.include_router(folders.router, prefix="/folders", tags=["folders"], include_in_schema=include_in_schema)
    target.include_router(folders.scenario_router, prefix="/scenario-folders", tags=["scenario-folders"], include_in_schema=include_in_schema)
    target.include_router(scenarios.router, prefix="/scenarios", tags=["scenarios"], include_in_schema=include_in_schema)
    target.include_router(reports.router, prefix=reports_prefix, tags=["reports"], include_in_schema=include_in_schema)
    target.include_router(runs.router, prefix="/runs", tags=["runs"], include_in_schema=include_in_schema)
    target.include_router(tasks.router, prefix="/tasks", tags=["tasks"], include_in_schema=include_in_schema)
    if include_settings_alias:
        target.include_router(settings.router, prefix="/settings", tags=["settings"], include_in_schema=include_in_schema)
    target.include_router(fastbot.router, prefix="/fastbot", tags=["fastbot"], include_in_schema=include_in_schema)
    target.include_router(log_analysis.router, prefix="/fastbot", tags=["log_analysis"], include_in_schema=include_in_schema)
    target.include_router(devices.router, prefix="/devices", tags=["devices"], include_in_schema=include_in_schema)
    target.include_router(packages.router, prefix="/packages", tags=["packages"], include_in_schema=include_in_schema)
    target.include_router(compatibility.router, prefix="/compatibility", tags=["compatibility"], include_in_schema=include_in_schema)
    target.include_router(environments.router, prefix="/environments", tags=["environments"], include_in_schema=include_in_schema)
    target.include_router(limiter.router, prefix="/limiter", tags=["limiter"], include_in_schema=include_in_schema)
    if ai_prefix:
        target.include_router(ai.router, prefix=ai_prefix, tags=["ai"], include_in_schema=include_in_schema)


_register_http_routers(api_router, include_in_schema=True, ai_prefix="/ai", reports_prefix="/reports")
_register_http_routers(app, include_in_schema=False, ai_prefix=None, include_settings_alias=True, reports_prefix="/reports")

api_router.include_router(
    stream_rest_router,
    prefix="/stream",
    tags=["device_stream"],
    include_in_schema=True,
)
app.include_router(stream_rest_router, prefix="/api", include_in_schema=False)
app.include_router(stream_rest_router, prefix="/stream", include_in_schema=False)
app.include_router(stream_rest_router, include_in_schema=False)
app.include_router(stream_ws_router)
app.include_router(api_router)


def _build_report_asset_url(report_path: str) -> str:
    normalized = str(report_path or "").strip().lstrip("/")
    if not normalized:
        raise HTTPException(status_code=404, detail="Report asset not found")
    return f"{REPORT_ASSET_API_PREFIX}/{quote(normalized, safe='/')}"


@app.get("/reports/{report_path:path}", include_in_schema=False)
def redirect_legacy_report_asset(report_path: str):
    return RedirectResponse(url=_build_report_asset_url(report_path))


@app.get("/api/reports/{report_path:path}", include_in_schema=False)
def redirect_legacy_api_report_asset(report_path: str):
    normalized = str(report_path or "").strip().lstrip("/")
    if not normalized:
        raise HTTPException(status_code=404, detail="Report asset not found")

    head = normalized.split("/", 1)[0]
    if head in REPORT_API_RESERVED_SEGMENTS:
        raise HTTPException(status_code=404, detail="Not found")

    return RedirectResponse(url=_build_report_asset_url(normalized))


def _cors_allow_origins() -> List[str]:
    """允许来源通过 AUTODROID_CORS_ORIGINS 配置（逗号分隔），默认放开。"""
    raw = (os.environ.get("AUTODROID_CORS_ORIGINS") or "").strip()
    origins = [origin.strip() for origin in raw.split(",") if origin.strip()]
    return origins or ["*"]


_cors_origins = _cors_allow_origins()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    # 通配 origin 与 credentials 组合不符合 CORS 规范；前端走同源 Bearer header，无 cookie 依赖。
    allow_credentials="*" not in _cors_origins,
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
            user = User(
                username="admin",
                hashed_password=get_password_hash("123456"),
                role="admin",
                full_name="Administrator"
            )
            session.add(user)
            session.commit()
            session.refresh(user)
        backfill_legacy_asset_owners(session, user.id)

    # 初始化定时任务调度器并恢复活跃任务
    _restore_scheduled_tasks()

    # 注册报告保留策略每日清理（保留天数由系统配置 report_retention_days 控制，默认关闭）
    try:
        from backend.retention_service import register_retention_job

        register_retention_job()
    except Exception:
        logger.exception("注册报告保留清理任务失败")


@app.on_event("shutdown")
def on_shutdown():
    try:
        device_manager.stop_tracking()
    except Exception:
        logger.exception("关闭时停止设备监听失败")
    try:
        _recording_ios_session_pool.close_all()
    except Exception:
        logger.exception("关闭时停止 iOS 录制会话池失败")
    try:
        from backend.drivers.driver_pool import reset_execution_driver_pool

        reset_execution_driver_pool()
    except Exception:
        logger.exception("关闭时停止执行驱动连接池失败")
    try:
        wda_relay_manager.stop_all()
    except Exception:
        logger.exception("关闭时停止 WDA relay 失败")
    try:
        from backend.device_stream.ios_mjpeg import ios_mjpeg_stream_manager

        ios_mjpeg_stream_manager.shutdown()
    except Exception:
        logger.exception("关闭时停止 iOS MJPEG 流失败")
    try:
        from backend.wda_port_manager import ios_mjpeg_relay_manager

        ios_mjpeg_relay_manager.stop_all()
    except Exception:
        logger.exception("关闭时停止 iOS MJPEG relay 失败")


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
                logger.error("恢复定时任务失败: task_id=%s error=%s", task.id, e)
        session.commit()
    logger.info("已恢复 %s 个定时任务", len(active_tasks))


@app.post("/api/run/{case_id}", include_in_schema=False)
@app.post("/run/{case_id}", include_in_schema=False)
def run_test_case_legacy_alias(
    case_id: int,
    background_tasks: BackgroundTasks,
    env_id: Optional[int] = None,
    device_serial: Optional[str] = None,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    """
    Legacy compatibility endpoint.

    Deprecated: use `/cases/{case_id}/run` instead.
    """
    response = cases.run_test_case(
        case_id=case_id,
        background_tasks=background_tasks,
        env_id=env_id,
        device_serial=device_serial,
        session=session,
        current_user=current_user,
    )
    if isinstance(response, dict):
        payload = dict(response)
        payload["deprecated"] = True
        payload["deprecated_endpoint"] = "/run/{case_id}"
        payload["replacement_endpoint"] = "/cases/{case_id}/run"
        msg = str(payload.get("message") or "").strip()
        migration_msg = "兼容入口 /run/{case_id} 将下线，请改用 /cases/{case_id}/run"
        payload["message"] = f"{msg}（{migration_msg}）" if msg else migration_msg
        return payload
    return response


# ==================== 设备录制 / WebSocket 执行 / SPA ====================

# 设备录制与单步执行（/device/*，含 /api 前缀双注册）
app.include_router(recording.router)

# WebSocket 实时执行（/ws/run/{case_id}）
app.include_router(ws_run.router)

# SPA 兜底路由必须最后挂载（含 /{full_path:path} 通配）
app.include_router(spa.router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
