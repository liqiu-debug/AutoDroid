"""
WebSocket 实时执行端点

从 backend.main 拆出的 `WS /ws/run/{case_id}` 执行链路：
连接管理、断开监听（可选断开即中止）、跨端 Runner 步骤执行、
报告生成与设备状态恢复。
"""
import asyncio
import base64
import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from functools import partial
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlmodel import Session, select

from backend.api.recording import _disconnect_runner_if_supported
from backend.cross_platform_execution import (
    check_wda_health,
    prepare_case_steps_for_platform,
    resolve_device_platform,
    resolve_ios_wda_url,
    restore_device_status_after_execution,
)
from backend.database import engine
from backend.drivers.cross_platform_runner import TestCaseRunner as CrossPlatformRunner
from backend.feature_flags import (
    FLAG_IOS_EXECUTION,
    FLAG_WS_DISCONNECT_ABORT,
    is_flag_enabled,
)
from backend.models import Device, TestCase
from backend.report_generator import report_generator
from backend.run_control import (
    ABORTED_STATUS,
    register_device_abort,
    registry,
    unregister_device_abort,
)
from backend.socket_manager import manager
from backend.step_contract import normalize_error_strategy, standard_step_to_legacy

logger = logging.getLogger(__name__)

router = APIRouter()


async def _run_in_blocking_executor(executor: ThreadPoolExecutor, func, *args, **kwargs):
    """Run thread-bound blocking work without stalling the event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, partial(func, *args, **kwargs))


def _capture_cross_platform_runner_screenshot(runner) -> bytes:
    return runner.driver.screenshot()


async def _watch_ws_disconnect(
    websocket: WebSocket,
    abort_event: Optional[threading.Event],
    abort_on_disconnect: bool,
    case_id: int,
) -> None:
    """监听执行 WebSocket 的断开：开关开启时断开即触发中止事件。

    执行主流程只发送不接收，且 socket_manager 会吞掉发送异常，
    因此必须用独立的 receive 协程才能及时感知客户端断开。
    """
    try:
        while True:
            # 忽略客户端消息内容，只关心连接是否存活
            await websocket.receive_text()
    except asyncio.CancelledError:
        raise
    except (WebSocketDisconnect, RuntimeError):
        if abort_on_disconnect and abort_event and not abort_event.is_set():
            abort_event.set()
            logger.info(
                "Case WebSocket disconnected, aborting run: case_id=%s", case_id
            )
    except Exception:
        logger.exception("WebSocket disconnect watcher failed: case_id=%s", case_id)


@router.websocket("/ws/run/{case_id}")
async def websocket_run_case(websocket: WebSocket, case_id: int, env_id: Optional[int] = None, device_serial: Optional[str] = None):
    """WebSocket 端点：实时执行测试用例并推送步骤状态"""
    await manager.connect(websocket, case_id)

    blocking_executor = ThreadPoolExecutor(max_workers=1)
    managed_device_serial = None
    run_id = None
    run_batch_id = None
    abort_event = None
    disconnect_watcher = None
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
            if not device_serial:
                # 跨端链路必须显式指定设备。
                await websocket.send_json({"type": "error", "message": "请选择执行设备"})
                return
            abort_on_disconnect = is_flag_enabled(session, FLAG_WS_DISCONNECT_ABORT)
            run_batch_id = str(uuid.uuid4())
            abort_event = register_device_abort(device_serial)
            disconnect_watcher = asyncio.create_task(
                _watch_ws_disconnect(websocket, abort_event, abort_on_disconnect, case_id)
            )
            managed_device_serial = device_serial
            run_record = registry.register(
                kind="case",
                target_id=case_id,
                batch_id=run_batch_id,
                device_serial=device_serial,
                abort_event=abort_event,
            )
            run_id = run_record.run_id
            case.last_run_status = "RUNNING"
            case.last_run_time = datetime.now()
            session.add(case)
            session.commit()

            platform = resolve_device_platform(session, device_serial)
            driver_kwargs = {}
            if platform == "ios":
                if not is_flag_enabled(session, FLAG_IOS_EXECUTION):
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

            await manager.broadcast_run_start(
                case_id,
                case.name,
                len(steps),
                batch_id=run_batch_id,
                run_id=run_id,
                device_serial=device_serial,
            )

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
                if abort_event and abort_event.is_set():
                    await manager.broadcast_step_update(
                        case_id,
                        i,
                        "warning",
                        "执行已被用户终止",
                    )
                    break
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
                if abort_event and abort_event.is_set():
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
                    steps_results.append(
                        {
                            **legacy_step,
                            "status": "warning",
                            "duration": round(float(step_result.get("duration") or 0), 2),
                            "log": "执行已被用户终止",
                            "error": "执行已被用户终止",
                        }
                    )
                    await manager.broadcast_step_update(
                        case_id,
                        i,
                        "warning",
                        "执行已被用户终止",
                    )
                    break
                status = str(step_result.get("status") or "FAIL").upper()
                strategy = normalize_error_strategy(step_result.get("error_strategy", "ABORT"))
                error_msg = step_result.get("error")
                # 结构化错误信息（纯增量字段，见 cross_platform_runner._result）
                error_code = str(step_result.get("error_code") or "").strip() or None
                error_suggestion = str(step_result.get("suggestion") or "").strip() or None
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
                    success_entry = {
                        **legacy_step,
                        "status": "success",
                        "duration": round(duration, 2),
                        "log": f"✓ 步骤成功 ({round(duration, 2)}s)",
                    }
                    if isinstance(step_result.get("output"), dict):
                        success_entry["output"] = step_result.get("output")
                    steps_results.append(success_entry)
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
                            "error_code": error_code,
                            "suggestion": error_suggestion,
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
                        error_code=error_code,
                        suggestion=error_suggestion,
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
                            "error_code": error_code,
                            "suggestion": error_suggestion,
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
                        error_code=error_code,
                        suggestion=error_suggestion,
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
                        "error_code": error_code,
                        "suggestion": error_suggestion,
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
                    error_code=error_code,
                    suggestion=error_suggestion,
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

        final_status = ABORTED_STATUS if abort_event and abort_event.is_set() else ("PASS" if failed == 0 else "FAIL")
        try:
            with Session(engine) as status_session:
                db_case = status_session.get(TestCase, case_id)
                if db_case:
                    db_case.last_run_status = final_status
                    db_case.last_run_time = end_time
                    status_session.add(db_case)
                    status_session.commit()
        except Exception:
            logger.exception("failed to update websocket case status: case_id=%s", case_id)

        # 广播执行完成
        await manager.broadcast_run_complete(
            case_id,
            success=(final_status == "PASS"),
            total_duration=total_duration,
            passed=passed,
            failed=failed,
            report_id=report_id,
            status=final_status,
            batch_id=run_batch_id,
            run_id=run_id,
            device_serial=device_serial,
        )

    except WebSocketDisconnect:
        logger.info("Case WebSocket disconnected: case_id=%s", case_id)
    except Exception as e:
        logger.exception("Case WebSocket execution failed: case_id=%s", case_id)
        await websocket.send_json({"type": "error", "message": str(e)})
    finally:
        if disconnect_watcher:
            disconnect_watcher.cancel()
            try:
                await disconnect_watcher
            except (asyncio.CancelledError, Exception):
                pass
        try:
            if run_id:
                with Session(engine) as status_session:
                    db_case = status_session.get(TestCase, case_id)
                    if db_case and str(db_case.last_run_status or "").upper() == "RUNNING":
                        db_case.last_run_status = (
                            ABORTED_STATUS if abort_event and abort_event.is_set() else "ERROR"
                        )
                        db_case.last_run_time = datetime.now()
                        status_session.add(db_case)
                        status_session.commit()
        except Exception:
            logger.exception("failed to finalize websocket case status: case_id=%s", case_id)
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
        registry.complete(run_id, ABORTED_STATUS if abort_event and abort_event.is_set() else None)
        blocking_executor.shutdown(wait=True)
        manager.disconnect(websocket, case_id)
