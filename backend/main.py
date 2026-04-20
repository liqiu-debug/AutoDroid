"""
AutoDroid-Pro 后端主入口

FastAPI 应用，提供：
- 测试用例 CRUD API
- 设备交互 API (截图/层级/点击)
- WebSocket 实时执行
- 测试报告 API
"""
import asyncio
import os
import io
import time
import base64
import hashlib
import logging
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote
from pydantic import BaseModel, Field
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, FastAPI, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session, select
from fastapi.middleware.cors import CORSMiddleware

from .models import Device, TestCase, GlobalVariable
from .schemas import InteractionRequest, Step
from .runner import TestRunner, register_device_abort, unregister_device_abort
from .socket_manager import manager
from .utils.pydantic_compat import dump_model
from .report_generator import report_generator
from .device_stream.router import rest_router as stream_rest_router
from .device_stream.router import ws_router as stream_ws_router
from .device_stream.manager import device_manager
from .wda_port_manager import wda_relay_manager

logger = logging.getLogger(__name__)

CLICK_IMAGE_REQUIRED_DETAIL = "添加步骤失败：当前点击区域无 Desc/Text，请使用图像点击步骤。"
CLICK_TARGET_NOT_FOUND_DETAIL = "添加步骤失败：当前点击区域未识别到可录制元素，请重试或使用图像点击步骤。"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = PROJECT_ROOT / "reports"
STATIC_DIR = PROJECT_ROOT / "static"
FRONTEND_DIST_DIR = PROJECT_ROOT / "frontend" / "dist"
FRONTEND_INDEX_FILE = FRONTEND_DIST_DIR / "index.html"
REPORT_ASSET_API_PREFIX = "/api/report-assets"
REPORT_API_RESERVED_SEGMENTS = {"executions", "dashboard"}
SPA_EXCLUDED_PREFIXES = (
    "api",
    "auth",
    "cases",
    "folders",
    "scenarios",
    "reports",
    "tasks",
    "fastbot",
    "devices",
    "device",
    "run",
    "ws",
    "docs",
    "redoc",
    "openapi.json",
    "static",
)
RECORDING_IOS_IDLE_TTL_SECONDS = 45.0


class _RecordingIOSSessionEntry:
    def __init__(self, serial: str, wda_url: str, driver: Any) -> None:
        self.serial = str(serial or "").strip()
        self.wda_url = str(wda_url or "").strip()
        self.driver = driver
        self.lock = threading.Lock()
        self.last_used_at = time.time()

    def touch(self) -> None:
        self.last_used_at = time.time()


class _RecordingIOSSessionPool:
    def __init__(self) -> None:
        self._entries: Dict[str, _RecordingIOSSessionEntry] = {}
        self._lock = threading.Lock()

    def acquire(self, serial: str, wda_url: str) -> Any:
        device_id = str(serial or "").strip()
        resolved_wda_url = str(wda_url or "").strip()
        if not device_id:
            raise RuntimeError("iOS 录制缺少设备序列号")

        stale_entries: List[_RecordingIOSSessionEntry] = []
        with self._lock:
            stale_entries = self._collect_stale_locked(exclude_serial=device_id)
            entry = self._entries.get(device_id)
            if entry and entry.wda_url != resolved_wda_url:
                self._entries.pop(device_id, None)
                stale_entries.append(entry)
                entry = None

            if entry is None:
                check_wda_health(resolved_wda_url)
                entry = _RecordingIOSSessionEntry(
                    serial=device_id,
                    wda_url=resolved_wda_url,
                    driver=IOSDriver(device_id=device_id, wda_url=resolved_wda_url),
                )
                self._entries[device_id] = entry

            entry.touch()

        for stale in stale_entries:
            self._close_entry(stale)

        entry.lock.acquire()
        entry.touch()
        return entry.driver

    def release(self, serial: str, driver: Optional[Any] = None) -> None:
        device_id = str(serial or "").strip()
        if not device_id:
            return
        with self._lock:
            entry = self._entries.get(device_id)
            if not entry:
                return
            if driver is not None and entry.driver is not driver:
                return
            entry.touch()
            lock = entry.lock
        if lock.locked():
            try:
                lock.release()
            except RuntimeError:
                pass

    def invalidate(self, serial: str, driver: Optional[Any] = None) -> None:
        device_id = str(serial or "").strip()
        if not device_id:
            return
        with self._lock:
            entry = self._entries.get(device_id)
            if not entry:
                return
            if driver is not None and entry.driver is not driver:
                return
            removed = self._entries.pop(device_id, None)
        if not removed:
            return
        if removed.lock.locked():
            try:
                removed.lock.release()
            except RuntimeError:
                pass
        self._close_entry(removed)

    def close_all(self) -> None:
        with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
        for entry in entries:
            if entry.lock.locked():
                try:
                    entry.lock.release()
                except RuntimeError:
                    pass
            self._close_entry(entry)

    def _collect_stale_locked(self, exclude_serial: Optional[str] = None) -> List[_RecordingIOSSessionEntry]:
        now = time.time()
        stale_entries: List[_RecordingIOSSessionEntry] = []
        excluded = str(exclude_serial or "").strip()
        for device_id, entry in list(self._entries.items()):
            if excluded and device_id == excluded:
                continue
            if entry.lock.locked():
                continue
            if (now - float(entry.last_used_at or 0.0)) < RECORDING_IOS_IDLE_TTL_SECONDS:
                continue
            stale_entries.append(self._entries.pop(device_id))
        return stale_entries

    @staticmethod
    def _close_entry(entry: _RecordingIOSSessionEntry) -> None:
        try:
            entry.driver.disconnect()
        except Exception:
            logger.exception("关闭 iOS 录制会话失败: serial=%s", entry.serial)


_recording_ios_session_pool = _RecordingIOSSessionPool()

from .database import engine, create_db_and_tables, get_session
from backend.core.security import get_password_hash
from backend.models import User
from backend.cross_platform_execution import (
    check_wda_health,
    prepare_case_steps_for_platform,
    resolve_device_platform,
    resolve_ios_wda_url,
    restore_device_status_after_execution,
)
from backend.drivers.ios_driver import IOSDriver
from backend.drivers.cross_platform_runner import TestCaseRunner as CrossPlatformRunner
from backend.feature_flags import (
    FLAG_CROSS_PLATFORM_RUNNER,
    FLAG_IOS_EXECUTION,
    is_flag_enabled,
)
from backend.step_contract import (
    legacy_step_to_standard,
    normalize_error_strategy,
    normalize_execute_on,
    normalize_platform_overrides,
    standard_step_to_legacy,
)
from backend.utils import calculate_element_from_coordinates

# ==================== FastAPI 应用 ====================

app = FastAPI(title="AutoDroid", description="Android UI 自动化低代码平台")
api_router = APIRouter(prefix="/api")

# Mount reports directory for canonical static asset access
REPORTS_DIR.mkdir(exist_ok=True)
app.mount(REPORT_ASSET_API_PREFIX, StaticFiles(directory=str(REPORTS_DIR)), name="report_assets")

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

from backend.api import auth, cases

from backend.api import folders
from backend.api import scenarios
from backend.api import reports
from backend.api import tasks
from backend.api import settings
from backend.api import fastbot
from backend.api import log_analysis
from backend.api import devices
from backend.api import packages
from backend.api import environments
from backend.api import ai


def _register_http_routers(
    target,
    *,
    include_in_schema: bool,
    ai_prefix: Optional[str] = "/ai",
    include_settings_alias: bool = True,
    reports_prefix: str = "/reports",
) -> None:
    target.include_router(auth.router, prefix="/auth", tags=["auth"], include_in_schema=include_in_schema)
    target.include_router(cases.router, prefix="/cases", tags=["cases"], include_in_schema=include_in_schema)
    target.include_router(folders.router, prefix="/folders", tags=["folders"], include_in_schema=include_in_schema)
    target.include_router(scenarios.router, prefix="/scenarios", tags=["scenarios"], include_in_schema=include_in_schema)
    target.include_router(reports.router, prefix=reports_prefix, tags=["reports"], include_in_schema=include_in_schema)
    target.include_router(tasks.router, prefix="/tasks", tags=["tasks"], include_in_schema=include_in_schema)
    if include_settings_alias:
        target.include_router(settings.router, prefix="/settings", tags=["settings"], include_in_schema=include_in_schema)
    target.include_router(fastbot.router, prefix="/fastbot", tags=["fastbot"], include_in_schema=include_in_schema)
    target.include_router(log_analysis.router, prefix="/fastbot", tags=["log_analysis"], include_in_schema=include_in_schema)
    target.include_router(devices.router, prefix="/devices", tags=["devices"], include_in_schema=include_in_schema)
    target.include_router(packages.router, prefix="/packages", tags=["packages"], include_in_schema=include_in_schema)
    target.include_router(environments.router, prefix="/environments", tags=["environments"], include_in_schema=include_in_schema)
    if ai_prefix:
        target.include_router(ai.router, prefix=ai_prefix, tags=["ai"], include_in_schema=include_in_schema)


_register_http_routers(api_router, include_in_schema=True, ai_prefix="/ai", reports_prefix="/reports")
_register_http_routers(app, include_in_schema=False, ai_prefix=None, include_settings_alias=False, reports_prefix="")

api_router.include_router(
    stream_rest_router,
    prefix="/stream",
    tags=["device_stream"],
    include_in_schema=True,
)
app.include_router(stream_rest_router, prefix="/api", include_in_schema=False)
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
        wda_relay_manager.stop_all()
    except Exception:
        logger.exception("关闭时停止 WDA relay 失败")


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

# ==================== 设备交互 API ====================


@app.post("/api/run/{case_id}", include_in_schema=False)
@app.post("/run/{case_id}", include_in_schema=False)
def run_test_case_legacy_alias(
    case_id: int,
    background_tasks: BackgroundTasks,
    env_id: Optional[int] = None,
    device_serial: Optional[str] = None,
    session: Session = Depends(get_session),
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


@app.get("/api/device/dump")
@app.get("/device/dump", include_in_schema=False)
def dump_device_info(
    serial: Optional[str] = None,
    include_device_info: bool = True,
    include_hierarchy: bool = True,
    include_screenshot: bool = True,
    session: Session = Depends(get_session),
):
    """获取设备信息：截图(base64) + 层级XML + 设备信息"""
    cleanup = None
    platform = None
    device = None
    try:
        platform, device, cleanup = _connect_recording_device(session, serial)
        return _build_device_dump_payload(
            device,
            platform=platform,
            serial=serial,
            include_device_info=include_device_info,
            include_hierarchy=include_hierarchy,
            include_screenshot=include_screenshot,
        )
    except HTTPException:
        raise
    except Exception as e:
        _invalidate_recording_device(platform, serial, device)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        _cleanup_recording_device(cleanup)


def _take_screenshot_base64(device) -> str:
    """工具函数：截取设备屏幕并返回 base64 字符串"""
    image = device.screenshot()
    if isinstance(image, (bytes, bytearray)):
        return base64.b64encode(bytes(image)).decode("utf-8")
    buffered = io.BytesIO()
    image.save(buffered, format="PNG")
    return base64.b64encode(buffered.getvalue()).decode("utf-8")


def _resolve_recording_platform(session: Session, serial: Optional[str]) -> str:
    if not serial:
        return "android"
    try:
        return resolve_device_platform(session, serial)
    except Exception:
        return "android"


def _connect_recording_device(
    session: Session,
    serial: Optional[str],
) -> Tuple[str, Any, Optional[Any]]:
    platform = _resolve_recording_platform(session, serial)
    if platform == "ios":
        if not serial:
            raise HTTPException(status_code=400, detail="iOS 录制必须选择一台设备。")
        try:
            wda_url = resolve_ios_wda_url(session, serial)
            driver = _recording_ios_session_pool.acquire(serial, wda_url)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return platform, driver, partial(_recording_ios_session_pool.release, serial, driver)

    runner = TestRunner(device_serial=serial)
    runner.connect()
    return platform, runner.d, getattr(runner, "disconnect", None)


def _cleanup_recording_device(cleanup) -> None:
    if not callable(cleanup):
        return
    try:
        cleanup()
    except Exception:
        logger.exception("录制设备连接释放失败")


def _invalidate_recording_device(platform: Optional[str], serial: Optional[str], device: Optional[Any]) -> None:
    if platform != "ios" or not serial:
        return
    try:
        _recording_ios_session_pool.invalidate(serial, device if isinstance(device, IOSDriver) else None)
    except Exception:
        logger.exception("iOS 录制会话失效处理失败: serial=%s", serial)


def _get_ios_source_xml(driver: IOSDriver) -> str:
    session = driver.client.session()
    source_candidates = [
        lambda: session.source(),
        lambda: getattr(session, "source", None),
        lambda: driver.client.source(),
        lambda: getattr(driver.client, "source", None),
    ]

    for getter in source_candidates:
        try:
            raw_value = getter()
            if callable(raw_value):
                raw_value = raw_value()
            source_text = str(raw_value or "").strip()
            if source_text:
                return source_text
        except Exception:
            continue

    raise RuntimeError("iOS 页面层级获取失败")


def _get_device_hierarchy_xml(device, platform: str) -> str:
    if platform == "ios":
        return _get_ios_source_xml(device)
    return device.dump_hierarchy()


def _get_ios_window_size(driver: IOSDriver) -> Tuple[int, int]:
    session = driver.client.session()
    size = None
    for getter in (
        lambda: session.window_size(),
        lambda: driver.client.window_size(),
    ):
        try:
            payload = getter()
            if payload:
                size = payload
                break
        except Exception:
            continue

    width = 0
    height = 0
    if isinstance(size, dict):
        width = int(size.get("width") or size.get("w") or 0)
        height = int(size.get("height") or size.get("h") or 0)
    elif isinstance(size, (tuple, list)) and len(size) >= 2:
        width = int(size[0] or 0)
        height = int(size[1] or 0)
    elif size is not None:
        width = int(getattr(size, "width", 0) or 0)
        height = int(getattr(size, "height", 0) or 0)
    return width, height


def _build_ios_device_info(driver: IOSDriver, serial: Optional[str]) -> Dict[str, Any]:
    width_points, height_points = _get_ios_window_size(driver)
    scale = float(getattr(driver, "scale", 1.0) or 1.0)
    width_pixels = int(round(width_points * scale)) if width_points else 0
    height_pixels = int(round(height_points * scale)) if height_points else 0
    return {
        "platform": "ios",
        "serial": serial or getattr(driver, "device_id", ""),
        "udid": getattr(driver, "device_id", serial or ""),
        "wda_url": getattr(driver, "wda_url", ""),
        "scale": scale,
        "resolution": f"{width_pixels}x{height_pixels}" if width_pixels and height_pixels else "",
        "window_size": {
            "width": width_points,
            "height": height_points,
        },
    }


def _get_device_info_payload(device, platform: str, serial: Optional[str]) -> Dict[str, Any]:
    if platform == "ios":
        return _build_ios_device_info(device, serial)
    return device.info


def _build_hierarchy_payload(device, platform: str) -> Dict[str, str]:
    hierarchy_xml = _get_device_hierarchy_xml(device, platform=platform)
    payload = {"hierarchy_xml": hierarchy_xml}
    if hierarchy_xml:
        payload["hierarchy_hash"] = hashlib.sha1(hierarchy_xml.encode("utf-8")).hexdigest()
    return payload


def _build_device_dump_payload(
    device,
    platform: str,
    serial: Optional[str],
    include_device_info: bool = True,
    include_hierarchy: bool = True,
    include_screenshot: bool = True,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    if include_device_info:
        payload["device_info"] = _get_device_info_payload(device, platform=platform, serial=serial)
    if include_hierarchy:
        payload.update(_build_hierarchy_payload(device, platform=platform))
    if include_screenshot:
        payload["screenshot"] = _take_screenshot_base64(device)
    return payload


def _get_recording_coordinate_scale(device, platform: str) -> float:
    if platform != "ios":
        return 1.0
    scale = float(getattr(device, "scale", 1.0) or 1.0)
    return scale if scale > 0 else 1.0


def _get_recording_post_action_delay(platform: str, operation: str) -> float:
    operation_text = str(operation or "").strip().lower()
    if platform == "ios" and operation_text == "click":
        return 0.35
    if platform == "ios":
        return 0.6
    return 1.0


def _perform_device_operation(device, platform: str, req: InteractionRequest) -> None:
    operation = str(req.operation or "").strip().lower()
    if operation == "click":
        if platform == "ios":
            device.click_by_coordinates(req.x, req.y)
        else:
            device.click(req.x, req.y)
        return
    if operation == "start_app":
        if platform == "ios":
            device.start_app(req.action_data)
        else:
            device.app_start(req.action_data)
        return
    if operation == "stop_app":
        if platform == "ios":
            device.stop_app(req.action_data)
        else:
            device.app_stop(req.action_data)
        return
    if operation == "back":
        if platform == "ios":
            device.back()
        else:
            device.press("back")
        return
    if operation == "home":
        if platform == "ios":
            device.home()
        else:
            device.press("home")
        return
    if operation == "swipe":
        if platform == "ios":
            device.swipe(req.action_data or "up")
        else:
            device.swipe_ext(req.action_data or "up", scale=0.8)
        return

    raise HTTPException(status_code=400, detail=f"不支持的设备操作: {req.operation}")


def _ensure_android_recording_device(session: Session, serial: Optional[str]) -> None:
    """
    录制链路仅支持 Android。

    - serial 为空：保持历史行为（使用默认 Android 设备）
    - serial 指向已登记 iOS 设备：返回明确 400
    - serial 未登记：不阻断，交由既有连接逻辑处理
    """
    if not serial:
        return

    db_device = session.exec(select(Device).where(Device.serial == serial)).first()
    if db_device and str(db_device.platform or "android").strip().lower() == "ios":
        raise HTTPException(
            status_code=400,
            detail="P2001_RECORDING_ANDROID_ONLY: iOS 设备仅支持执行，不支持录制。请切换到 Android 设备。",
        )


def _build_step_from_inspect(inspect_res: dict, operation: str = "click") -> dict:
    """
    工具函数：根据元素检查结果构建步骤数据。
    
    统一 /device/inspect 和 /device/interact 的步骤生成逻辑。
    注: 图像模板匹配(click_image)已改为用户手动框选截取，不再自动生成。
    """
    element = inspect_res.get("element", {})
    strategy = inspect_res["strategy"]

    if operation == "click":
        has_semantic_locator = bool(
            str(element.get("text") or "").strip()
            or str(element.get("description") or "").strip()
        )
        if strategy not in {"text", "description"} or not has_semantic_locator:
            raise HTTPException(status_code=400, detail=CLICK_IMAGE_REQUIRED_DETAIL)

    desc = element.get("text") or element.get("description") or "element"
    return {
        "action": "click" if operation == "click" else operation,
        "selector": inspect_res["selector"],
        "selector_type": strategy,
        "value": "",
        "description": f"Click [{desc}]",
        "error_strategy": "ABORT"
    }


def _build_click_step_from_inspect_result(inspect_res: dict) -> dict:
    if "error" in inspect_res:
        raise HTTPException(status_code=400, detail=CLICK_TARGET_NOT_FOUND_DETAIL)
    return _build_step_from_inspect(inspect_res, operation="click")


class CropTemplateRequest(BaseModel):
    screenshot_base64: str = Field(..., description="当前设备截图的 base64 编码")
    x1: int = Field(..., description="裁剪区域左上角 X 坐标（像素）")
    y1: int = Field(..., description="裁剪区域左上角 Y 坐标（像素）")
    x2: int = Field(..., description="裁剪区域右下角 X 坐标（像素）")
    y2: int = Field(..., description="裁剪区域右下角 Y 坐标（像素）")


@app.post("/api/device/crop_template")
@app.post("/device/crop_template", include_in_schema=False)
def crop_template(req: CropTemplateRequest):
    """
    手动截取图像模板：裁剪截图中的指定区域并保存为模板图。

    用于 click_image 步骤的手动录制，用户在前端框选目标区域后调用此接口。
    """
    import uuid as _uuid
    from PIL import Image as _Image

    try:
        img = _Image.open(io.BytesIO(base64.b64decode(req.screenshot_base64)))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"无法解析截图: {e}")

    width_s, height_s = img.size
    # 坐标边界校验
    rx1 = max(0, min(req.x1, width_s))
    ry1 = max(0, min(req.y1, height_s))
    rx2 = max(0, min(req.x2, width_s))
    ry2 = max(0, min(req.y2, height_s))

    if rx2 <= rx1 or ry2 <= ry1:
        raise HTTPException(status_code=400, detail=f"裁剪区域无效: [{rx1},{ry1},{rx2},{ry2}]")

    cropped = img.crop((rx1, ry1, rx2, ry2))

    image_filename = f"element_{_uuid.uuid4().hex[:8]}.png"
    image_dir = os.path.join(os.path.dirname(__file__), "..", "static", "images")
    os.makedirs(image_dir, exist_ok=True)
    cropped.save(os.path.join(image_dir, image_filename))

    image_path = f"static/images/{image_filename}"
    logger.info(f"手动截取模板图已保存: {image_path} ({rx2-rx1}x{ry2-ry1})")

    return {"image_path": image_path}


@app.post("/api/device/inspect")
@app.post("/device/inspect", include_in_schema=False)
def inspect_device(
    x: int,
    y: int,
    serial: Optional[str] = None,
    session: Session = Depends(get_session),
):
    """
    审查模式：返回指定坐标处的最佳元素和定位策略。
    不执行点击操作，仅分析元素。
    """
    cleanup = None
    platform = None
    device = None
    try:
        platform, device, cleanup = _connect_recording_device(session, serial)
        xml_dump = _get_device_hierarchy_xml(device, platform=platform)
        inspect_res = calculate_element_from_coordinates(
            xml_dump,
            x,
            y,
            coordinate_scale=_get_recording_coordinate_scale(device, platform),
        )

        step = _build_click_step_from_inspect_result(inspect_res)

        return {
            "step": step,
            "element": inspect_res.get("element", {}),
            "selector": inspect_res["selector"],
            "strategy": inspect_res["strategy"]
        }
    except HTTPException:
        raise
    except Exception as e:
        _invalidate_recording_device(platform, serial, device)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        _cleanup_recording_device(cleanup)


@app.post("/api/device/interact")
@app.post("/device/interact", include_in_schema=False)
def interact_with_device(req: InteractionRequest, session: Session = Depends(get_session)):
    """
    交互模式：分析元素 → 执行点击 → 返回新状态。
    
    流程: 截图分析当前UI → 生成步骤 → 执行操作 → 等待UI稳定 → 返回新截图
    """
    cleanup = None
    platform = None
    device = None
    try:
        platform, device, cleanup = _connect_recording_device(session, req.device_serial)

        # 2. 如果是坐标点击，分析点击坐标处的元素
        inspect_res = {}
        step_info = None
        if req.operation == "click" and req.record_step:
            # 1. 获取当前 UI 层级 (仅点击时需要)
            xml_dump = req.xml_dump or _get_device_hierarchy_xml(device, platform=platform)
            coordinate_scale = _get_recording_coordinate_scale(device, platform)
            inspect_res = calculate_element_from_coordinates(
                xml_dump,
                req.x,
                req.y,
                coordinate_scale=coordinate_scale,
            )

            # 如果前端传入的 XML 过期，用新的重试
            if "error" in inspect_res:
                logger.info(f"使用缓存XML分析失败，重新获取...")
                xml_dump = _get_device_hierarchy_xml(device, platform=platform)
                inspect_res = calculate_element_from_coordinates(
                    xml_dump,
                    req.x,
                    req.y,
                    coordinate_scale=coordinate_scale,
                )

        # 3. 构建步骤
        if req.operation == "click" and req.record_step:
            step_info = _build_click_step_from_inspect_result(inspect_res)
        elif req.operation != "click":
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
        _perform_device_operation(device, platform=platform, req=req)
        
        # 5. 等待 UI 稳定后返回新状态
        time.sleep(_get_recording_post_action_delay(platform, req.operation))

        return {
            "step": step_info,
            "dump": _build_device_dump_payload(device, platform=platform, serial=req.device_serial),
        }
    except HTTPException:
        raise
    except Exception as e:
        _invalidate_recording_device(platform, req.device_serial, device)
        logger.exception("设备交互失败: operation=%s device_serial=%s", req.operation, req.device_serial)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        _cleanup_recording_device(cleanup)


class SingleStepPayload(BaseModel):
    step: Dict[str, Any]
    case_id: Optional[int] = None
    env_id: Optional[int] = None
    variables: Optional[List[dict]] = Field(default_factory=list)
    device_serial: Optional[str] = None


async def _run_in_blocking_executor(executor: ThreadPoolExecutor, func, *args, **kwargs):
    """Run thread-bound blocking work without stalling the event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, partial(func, *args, **kwargs))


def _capture_cross_platform_runner_screenshot(runner) -> bytes:
    return runner.driver.screenshot()


def _capture_legacy_runner_screenshot(runner) -> Optional[str]:
    if not runner or not getattr(runner, "d", None):
        return None
    return _take_screenshot_base64(runner.d)


def _disconnect_runner_if_supported(runner) -> None:
    if runner and hasattr(runner, "disconnect"):
        runner.disconnect()


def _merge_execution_variables(
    session: Session,
    env_id: Optional[int],
    variables: Optional[List[dict]],
) -> Dict[str, Any]:
    variables_map: Dict[str, Any] = {}
    if env_id:
        global_vars = session.exec(
            select(GlobalVariable).where(GlobalVariable.env_id == env_id)
        ).all()
        for gv in global_vars:
            variables_map[gv.key] = gv.value

    for v in variables or []:
        if not isinstance(v, dict):
            continue
        key = v.get("key")
        if key:
            variables_map[key] = v.get("value")
    return variables_map


def _normalize_single_step_for_runner(
    raw_step: Dict[str, Any],
    *,
    case_id: Optional[int],
    default_platform: str,
) -> Dict[str, Any]:
    step_data = dict(raw_step or {})
    standard_step = legacy_step_to_standard(
        step_data,
        case_id=int(case_id or 0),
        order=1,
    )

    args = step_data.get("args")
    if isinstance(args, dict):
        standard_step["args"] = dict(args)

    try:
        standard_step["execute_on"] = normalize_execute_on(
            step_data.get("execute_on") or [default_platform]
        )
    except Exception:
        standard_step["execute_on"] = [default_platform]

    try:
        standard_step["platform_overrides"] = normalize_platform_overrides(
            step_data.get("platform_overrides")
        )
    except Exception:
        pass

    if step_data.get("timeout") is not None:
        try:
            timeout_value = int(step_data.get("timeout") or 10)
            if timeout_value > 0:
                standard_step["timeout"] = timeout_value
        except Exception:
            pass
    if step_data.get("error_strategy") is not None:
        standard_step["error_strategy"] = normalize_error_strategy(step_data.get("error_strategy"))
    if step_data.get("description") is not None:
        standard_step["description"] = step_data.get("description")
    if step_data.get("value") is not None:
        standard_step["value"] = step_data.get("value")

    return standard_step


def _cross_platform_result_to_legacy_payload(result: Dict[str, Any]) -> Dict[str, Any]:
    status = str(result.get("status") or "").upper()
    return {
        "step": standard_step_to_legacy(result.get("step") or {}),
        "success": status == "PASS",
        "error": result.get("error"),
        "duration": float(result.get("duration") or 0),
        "status": status,
        "platform": result.get("platform"),
        "device_id": result.get("device_id"),
        "output": result.get("output"),
    }

@app.post("/api/device/execute_step")
@app.post("/device/execute_step", include_in_schema=False)
def execute_single_step(payload: SingleStepPayload, session: Session = Depends(get_session)):
    """
    执行单个步骤并返回最新 UI 快照。
    """
    platform = _resolve_recording_platform(session, payload.device_serial)
    variables_map = _merge_execution_variables(session, payload.env_id, payload.variables)

    if platform == "ios":
        if not payload.device_serial:
            raise HTTPException(status_code=400, detail="iOS 单步执行必须选择一台设备。")
        if not is_flag_enabled(session, FLAG_IOS_EXECUTION, default=False):
            raise HTTPException(status_code=400, detail="iOS 执行开关未开启")

        wda_url = resolve_ios_wda_url(session, payload.device_serial)
        check_wda_health(wda_url)
        runner = None
        try:
            standard_step = _normalize_single_step_for_runner(
                payload.step,
                case_id=payload.case_id,
                default_platform=platform,
            )
            runner = CrossPlatformRunner(
                platform=platform,
                device_id=payload.device_serial,
                wda_url=wda_url,
            )
            runner.runtime_variables.update(
                {
                    str(key): "" if value is None else str(value)
                    for key, value in variables_map.items()
                    if str(key).strip()
                }
            )
            step_result = runner.run_step(standard_step)
            time.sleep(0.5)
            return {
                "result": _cross_platform_result_to_legacy_payload(step_result),
                "dump": _build_device_dump_payload(
                    runner.driver,
                    platform=platform,
                    serial=payload.device_serial,
                ),
            }
        except HTTPException:
            raise
        except Exception as e:
            logger.exception("iOS 单步执行失败: device_serial=%s", payload.device_serial)
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            _disconnect_runner_if_supported(runner)

    runner = TestRunner(device_serial=payload.device_serial)
    try:
        runner.connect()
        d = runner.d

        # 执行步骤 
        step_model = Step.model_validate(payload.step)
        result = runner.execute_step(step_model, variables_map)
        
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
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("单步执行失败: device_serial=%s", payload.device_serial)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        _disconnect_runner_if_supported(runner)


# ==================== WebSocket 实时执行 ====================


@app.websocket("/ws/run/{case_id}")
async def websocket_run_case(websocket: WebSocket, case_id: int, env_id: Optional[int] = None, device_serial: Optional[str] = None):
    """WebSocket 端点：实时执行测试用例并推送步骤状态"""
    await manager.connect(websocket, case_id)

    blocking_executor = ThreadPoolExecutor(max_workers=1)
    managed_device_serial = None
    try:
        runner = None
        case_name_for_report = f"case-{case_id}"
        report_variables = []
        with Session(engine) as session:
            case = session.get(TestCase, case_id)
            if not case:
                await websocket.send_json({"type": "error", "message": "用例不存在"})
                return
            case_name_for_report = str(case.name or case_name_for_report)
            case_variables = list(case.variables or [])
            report_variables = list(case_variables)
            use_cross_platform_runner = (
                is_flag_enabled(session, FLAG_CROSS_PLATFORM_RUNNER, default=False)
                and bool(device_serial)
            )

            if use_cross_platform_runner:
                if not device_serial:
                    await websocket.send_json({"type": "error", "message": "跨端执行必须指定设备"})
                    return

                managed_device_serial = device_serial
                abort_event = register_device_abort(device_serial)

                platform = resolve_device_platform(session, device_serial)
                driver_kwargs = {}
                if platform == "ios":
                    if not is_flag_enabled(session, FLAG_IOS_EXECUTION, default=False):
                        await websocket.send_json({"type": "error", "message": "iOS 执行开关未开启"})
                        return
                    wda_url = resolve_ios_wda_url(session, device_serial)
                    await _run_in_blocking_executor(blocking_executor, check_wda_health, wda_url)
                    driver_kwargs["wda_url"] = wda_url

                steps, variables_map = prepare_case_steps_for_platform(
                    session=session,
                    case=case,
                    platform=platform,
                    env_id=env_id,
                )

                await manager.broadcast_run_start(case_id, case.name, len(steps))

                device = session.exec(select(Device).where(Device.serial == device_serial)).first()
                if device:
                    device.status = "BUSY"
                    device.updated_at = datetime.now()
                    session.add(device)
                    session.commit()

                runner = await _run_in_blocking_executor(
                    blocking_executor,
                    CrossPlatformRunner,
                    platform=platform,
                    device_id=device_serial,
                    abort_event=abort_event,
                    **driver_kwargs,
                )

                start_time = datetime.now()
                steps_results = []
                passed = 0
                failed = 0

                for i, step in enumerate(steps):
                    desc = step.get("description", "") if isinstance(step, dict) else ""
                    action = step.get("action") if isinstance(step, dict) else ""
                    await manager.broadcast_step_update(
                        case_id,
                        i,
                        "running",
                        f"[{i+1}/{len(steps)}] 执行 {action}: {desc}",
                    )

                    step_result = await _run_in_blocking_executor(
                        blocking_executor,
                        runner.run_step,
                        step,
                    )
                    status = str(step_result.get("status") or "FAIL").upper()
                    strategy = normalize_error_strategy(step_result.get("error_strategy", "ABORT"))
                    error_msg = step_result.get("error")
                    duration = float(step_result.get("duration") or 0)

                    try:
                        legacy_step = standard_step_to_legacy(step)
                    except Exception:
                        legacy_step = {
                            "action": step.get("action"),
                            "selector": None,
                            "selector_type": None,
                            "value": step.get("value"),
                            "options": {},
                            "description": step.get("description"),
                            "timeout": step.get("timeout", 10),
                            "error_strategy": step.get("error_strategy", "ABORT"),
                        }

                    screenshot_base64 = None
                    artifacts = step_result.get("artifacts") if isinstance(step_result, dict) else None
                    if isinstance(artifacts, dict):
                        cached_screenshot = artifacts.get("screenshot_base64")
                        if cached_screenshot:
                            screenshot_base64 = str(cached_screenshot)
                    if status in ("FAIL", "WARNING") and not screenshot_base64:
                        try:
                            raw_png = await _run_in_blocking_executor(
                                blocking_executor,
                                _capture_cross_platform_runner_screenshot,
                                runner,
                            )
                            screenshot_base64 = base64.b64encode(raw_png).decode("utf-8")
                        except Exception:
                            screenshot_base64 = None

                    if status == "PASS":
                        passed += 1
                        steps_results.append(
                            {
                                **legacy_step,
                                "status": "success",
                                "duration": round(duration, 2),
                                "log": f"✓ 步骤成功 ({round(duration, 2)}s)",
                            }
                        )
                        await manager.broadcast_step_update(
                            case_id, i, "success", f"✓ 步骤 {i+1} 成功", duration
                        )
                        continue

                    if status == "SKIP":
                        steps_results.append(
                            {
                                **legacy_step,
                                "status": "skipped",
                                "duration": round(duration, 2),
                                "log": f"↷ 步骤跳过: {error_msg or '平台不匹配'}",
                                "error": error_msg,
                            }
                        )
                        await manager.broadcast_step_update(
                            case_id,
                            i,
                            "skipped",
                            f"↷ 步骤 {i+1} 跳过: {error_msg or '平台不匹配'}",
                            duration,
                            None,
                            error_msg,
                        )
                        continue

                    if status == "WARNING" or (status == "FAIL" and strategy == "IGNORE"):
                        steps_results.append(
                            {
                                **legacy_step,
                                "status": "warning",
                                "duration": round(duration, 2),
                                "log": f"⚠ 步骤失败(IGNORE): {error_msg}",
                                "error": error_msg,
                                "screenshot": screenshot_base64,
                            }
                        )
                        await manager.broadcast_step_update(
                            case_id,
                            i,
                            "warning",
                            f"⚠ 步骤 {i+1} 失败(已忽略): {error_msg}",
                            duration,
                            screenshot_base64,
                            error_msg,
                        )
                        continue

                    failed += 1
                    steps_results.append(
                        {
                            **legacy_step,
                            "status": "failed",
                            "duration": round(duration, 2),
                            "log": f"✗ 步骤失败: {error_msg}",
                            "error": error_msg,
                            "screenshot": screenshot_base64,
                        }
                    )
                    await manager.broadcast_step_update(
                        case_id,
                        i,
                        "failed",
                        f"✗ 步骤 {i+1} 失败: {error_msg}",
                        duration,
                        screenshot_base64,
                        error_msg,
                    )

                    if strategy == "ABORT":
                        break

            else:
                steps = case.steps or []
                variables = case_variables or []

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
                        variables_map[v.get("key")] = v.get("value")
                    else:
                        variables_map[v.key] = v.value

                # 连接设备
                runner = await _run_in_blocking_executor(
                    blocking_executor,
                    TestRunner,
                    device_serial=device_serial,
                )
                try:
                    await _run_in_blocking_executor(blocking_executor, runner.connect)
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
                        desc = step.get("description", "") if isinstance(step, dict) else step.description
                        action = step.get("action") if isinstance(step, dict) else step.action
                        await manager.broadcast_step_update(
                            case_id, i, "running",
                            f"[{i+1}/{len(steps)}] 执行 {action}: {desc}"
                        )
                    except Exception as e:
                        # 广播失败不影响实际执行流程。
                        logger.debug("广播步骤开始状态失败: case_id=%s step_index=%s error=%s", case_id, i, e)

                    try:
                        # 将 dict 转换为 Step 对象
                        step_obj = Step(**step) if isinstance(step, dict) else step
                        result = await _run_in_blocking_executor(
                            blocking_executor,
                            runner.execute_step,
                            step_obj,
                            variables_map,
                        )

                        if not result.get("success"):
                            raise Exception(result.get("error", "未知错误"))

                        duration = time.time() - step_start
                        passed += 1

                        step_data = step if isinstance(step, dict) else dump_model(step)
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
                            screenshot_base64 = await _run_in_blocking_executor(
                                blocking_executor,
                                _capture_legacy_runner_screenshot,
                                runner,
                            )
                        except Exception as e:
                            # 截图失败不影响步骤结果判断，仅缺失失败截图。
                            logger.debug("失败截图采集异常: case_id=%s step_index=%s error=%s", case_id, i, e)

                        step_data = step if isinstance(step, dict) else dump_model(step)

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

        report_id = await _run_in_blocking_executor(
            blocking_executor,
            report_generator.generate_report,
            case_id=case_id,
            case_name=case_name_for_report,
            steps_results=steps_results,
            start_time=start_time,
            end_time=end_time,
            variables=report_variables,
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
        logger.info("Case WebSocket disconnected: case_id=%s", case_id)
    except Exception as e:
        logger.exception("Case WebSocket execution failed: case_id=%s", case_id)
        await websocket.send_json({"type": "error", "message": str(e)})
    finally:
        try:
            await _run_in_blocking_executor(
                blocking_executor,
                _disconnect_runner_if_supported,
                runner,
            )
        except Exception as e:
            logger.debug("WebSocket 结束时断开设备失败: case_id=%s error=%s", case_id, e)
        try:
            if managed_device_serial:
                with Session(engine) as session:
                    restore_device_status_after_execution(session, managed_device_serial)
        except Exception:
            logger.exception(
                "failed to restore device status after websocket case execution: device=%s",
                managed_device_serial,
            )
        if managed_device_serial:
            unregister_device_abort(managed_device_serial)
        blocking_executor.shutdown(wait=True)
        manager.disconnect(websocket, case_id)


# ==================== 报告 API ====================


# Old report file APIs removed in favor of DB-based reports


def _frontend_build_missing_response():
    return PlainTextResponse(
        "前端构建产物不存在。请先执行 `cd frontend && npm run build`，或直接运行 `./scripts/start_lan.sh`。",
        status_code=503,
    )


@app.get("/", include_in_schema=False)
async def serve_frontend_index():
    if FRONTEND_INDEX_FILE.is_file():
        return FileResponse(str(FRONTEND_INDEX_FILE))
    return _frontend_build_missing_response()


@app.get("/{full_path:path}", include_in_schema=False)
async def serve_frontend_app(full_path: str):
    normalized_path = str(full_path or "").strip("/")
    if not normalized_path:
        return await serve_frontend_index()

    if any(
        normalized_path == prefix or normalized_path.startswith(f"{prefix}/")
        for prefix in SPA_EXCLUDED_PREFIXES
    ):
        raise HTTPException(status_code=404, detail="Not found")

    if FRONTEND_DIST_DIR.is_dir():
        dist_root = FRONTEND_DIST_DIR.resolve()
        candidate = (dist_root / normalized_path).resolve()
        try:
            candidate.relative_to(dist_root)
        except ValueError:
            raise HTTPException(status_code=404, detail="Not found")

        if candidate.is_file():
            return FileResponse(str(candidate))

        if Path(normalized_path).suffix:
            raise HTTPException(status_code=404, detail="Not found")

    if FRONTEND_INDEX_FILE.is_file():
        return FileResponse(str(FRONTEND_INDEX_FILE))

    return _frontend_build_missing_response()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
