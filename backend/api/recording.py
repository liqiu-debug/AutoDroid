"""
录制与单步执行 API

从 backend.main 拆出的设备录制链路：
- /device/dump | /device/inspect | /device/interact | /device/execute_step
- /device/crop_template（手动截取图像模板）
- iOS 录制会话池与录制辅助函数

路由同时注册 `/api` 前缀（进 OpenAPI）与 legacy 无前缀别名（隐藏），
与原先挂载在 app 上的行为保持一致。
"""
import base64
import hashlib
import io
import logging
import os
import threading
import time
from functools import partial
from typing import Any, Dict, List, Optional, Tuple

import uiautomator2 as u2
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from backend.cross_platform_execution import (
    check_wda_health,
    resolve_device_platform,
    resolve_ios_wda_url,
)
from backend.database import get_session
from backend.drivers.cross_platform_runner import TestCaseRunner as CrossPlatformRunner
from backend.drivers.ios_driver import IOSDriver
from backend.feature_flags import FLAG_IOS_EXECUTION, is_flag_enabled
from backend.models import Device, GlobalVariable
from backend.schemas import InteractionRequest
from backend.step_contract import (
    legacy_step_to_standard,
    normalize_error_strategy,
    normalize_execute_on,
    normalize_platform_overrides,
    standard_step_to_legacy,
)
from backend.utils import calculate_element_from_coordinates

logger = logging.getLogger(__name__)

router = APIRouter()

CLICK_IMAGE_REQUIRED_DETAIL = "添加步骤失败：当前点击区域无 Desc/Text，请使用图像点击步骤。"
CLICK_TARGET_NOT_FOUND_DETAIL = "添加步骤失败：当前点击区域未识别到可录制元素，请重试或使用图像点击步骤。"

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


@router.get("/api/device/dump")
@router.get("/device/dump", include_in_schema=False)
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

    device = u2.connect(serial) if serial else u2.connect()
    return platform, device, None


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
    """返回操作后的最小等待时间（秒），用于给页面一个起始响应窗口。"""
    operation_text = str(operation or "").strip().lower()
    if operation_text in ("start_app", "stop_app"):
        return 0.6
    if platform == "ios" and operation_text == "click":
        return 0.15
    if platform == "ios":
        return 0.3
    return 0.2


def _screenshot_hash(device) -> str:
    """快速获取当前屏幕截图的哈希值，用于对比 UI 是否稳定。"""
    image = device.screenshot()
    if isinstance(image, (bytes, bytearray)):
        raw = bytes(image)
    else:
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        raw = buf.getvalue()
    return hashlib.md5(raw).hexdigest()


def _wait_ui_stable(device, platform: str, operation: str, timeout: float = 3.0) -> None:
    """
    等待设备 UI 稳定：先等最小间隔，再通过截图对比轮询检测。
    两次连续截图哈希一致即认为页面已稳定。
    """
    min_delay = _get_recording_post_action_delay(platform, operation)
    time.sleep(min_delay)

    poll_interval = 0.25
    deadline = time.monotonic() + (timeout - min_delay)

    prev_hash = _screenshot_hash(device)
    while time.monotonic() < deadline:
        time.sleep(poll_interval)
        curr_hash = _screenshot_hash(device)
        if curr_hash == prev_hash:
            return
        prev_hash = curr_hash


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

    return {
        "action": "click" if operation == "click" else operation,
        "selector": inspect_res["selector"],
        "selector_type": strategy,
        "value": "",
        "description": "",
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


@router.post("/api/device/crop_template")
@router.post("/device/crop_template", include_in_schema=False)
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
    image_dir = os.path.join(os.path.dirname(__file__), "..", "..", "static", "images")
    os.makedirs(image_dir, exist_ok=True)
    cropped.save(os.path.join(image_dir, image_filename))

    image_path = f"static/images/{image_filename}"
    logger.info(f"手动截取模板图已保存: {image_path} ({rx2-rx1}x{ry2-ry1})")

    return {"image_path": image_path}


@router.post("/api/device/inspect")
@router.post("/device/inspect", include_in_schema=False)
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


@router.post("/api/device/interact")
@router.post("/device/interact", include_in_schema=False)
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
                "description": "",
                "error_strategy": "ABORT"
            }

        # 4. 在设备上执行操作
        _perform_device_operation(device, platform=platform, req=req)

        # 5. 等待 UI 稳定后返回新状态
        _wait_ui_stable(device, platform=platform, operation=req.operation)

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

def _unwrap_runner_dump_device(driver, platform: str):
    """跨端 Runner 的 Android 驱动内部持有 u2 设备，dump/截图需使用原始设备对象。"""
    if platform == "android":
        return getattr(driver, "_device", driver)
    return driver


@router.post("/api/device/execute_step")
@router.post("/device/execute_step", include_in_schema=False)
def execute_single_step(payload: SingleStepPayload, session: Session = Depends(get_session)):
    """
    执行单个步骤并返回最新 UI 快照（统一走跨端 Runner）。
    """
    if not payload.device_serial:
        raise HTTPException(status_code=400, detail="请选择执行设备")

    platform = _resolve_recording_platform(session, payload.device_serial)
    variables_map = _merge_execution_variables(session, payload.env_id, payload.variables)

    driver_kwargs = {}
    if platform == "ios":
        if not is_flag_enabled(session, FLAG_IOS_EXECUTION):
            raise HTTPException(status_code=400, detail="iOS 执行开关未开启")
        wda_url = resolve_ios_wda_url(session, payload.device_serial)
        check_wda_health(wda_url)
        driver_kwargs["wda_url"] = wda_url

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
            **driver_kwargs,
        )
        runner.runtime_variables.update(
            {
                str(key): "" if value is None else str(value)
                for key, value in variables_map.items()
                if str(key).strip()
            }
        )
        step_result = runner.run_step(standard_step)
        dump_device = _unwrap_runner_dump_device(runner.driver, platform)
        _wait_ui_stable(dump_device, platform=platform, operation=standard_step.get("action", ""))
        return {
            "result": _cross_platform_result_to_legacy_payload(step_result),
            "dump": _build_device_dump_payload(
                dump_device,
                platform=platform,
                serial=payload.device_serial,
            ),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("单步执行失败: device_serial=%s", payload.device_serial)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        _disconnect_runner_if_supported(runner)
