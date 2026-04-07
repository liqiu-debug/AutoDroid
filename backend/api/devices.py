"""
设备管理 API - ADB 核心控制接口

提供设备同步、内存级截图、强制释放锁、重启等功能。
所有 ADB 命令通过 asyncio subprocess 异步执行。
"""
import asyncio
import base64
import logging
import os
import platform
import shlex
import signal
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from backend.database import get_session
from backend.feature_flags import get_setting_value
from backend.models import Device, TestExecution
from backend.schemas import DeviceRead, DeviceSyncResponse, DeviceRenameRequest
from backend.api.deps import get_current_user
from backend.wda_port_manager import wda_relay_manager

logger = logging.getLogger(__name__)

router = APIRouter()

DEFAULT_IOS_WDA_BUNDLE_ID = "com.facebook.WebDriverAgentRunner.xctrunner"
DEFAULT_IOS_WDA_SCHEME = "WebDriverAgentRunner"
DEFAULT_IOS_WDA_START_RETRY_ATTEMPTS = 8
DEFAULT_IOS_WDA_START_RETRY_INTERVAL_SECONDS = 1.0
_ios_wda_lock_guard = threading.Lock()
_ios_wda_locks: Dict[str, threading.Lock] = {}


def _ensure_android_device(device: Device, action: str) -> None:
    """ADB 操作仅支持 Android 设备。"""
    platform = str(device.platform or "android").strip().lower()
    if platform != "android":
        raise HTTPException(
            status_code=400,
            detail=f"P2002_ADB_ANDROID_ONLY: {action} 仅支持 Android 设备，iOS 设备仅支持执行。",
        )


def _resolve_ios_device_status(current_status: Optional[str], wda_healthy: bool) -> str:
    """
    解析 iOS 设备目标状态。

    约束：
    - BUSY 状态不被覆盖（避免执行中被同步流程改写）
    - WDA 健康时标记为 IDLE
    - WDA 不健康时标记为 WDA_DOWN
    """
    status = str(current_status or "").strip().upper()
    if status == "BUSY":
        return "BUSY"
    return "IDLE" if wda_healthy else "WDA_DOWN"


def _check_ios_wda_health(session: Session, udid: str) -> Dict[str, Any]:
    """
    使用执行链路同一套配置来源检查 iOS 设备 WDA 健康度，并返回详情。
    """
    wda_url: Optional[str] = None
    try:
        from backend.cross_platform_execution import check_wda_health, resolve_ios_wda_url

        wda_url = resolve_ios_wda_url(session, udid)
        check_wda_health(wda_url)
        return {
            "healthy": True,
            "wda_url": wda_url,
            "error": None,
        }
    except Exception as exc:
        logger.warning("iOS 设备 %s WDA 健康检查失败: %s", udid, exc)
        return {
            "healthy": False,
            "wda_url": wda_url,
            "error": str(exc),
        }


def _is_ios_wda_healthy(session: Session, udid: str) -> bool:
    """兼容旧调用点：仅返回健康状态。"""
    return bool(_check_ios_wda_health(session, udid).get("healthy"))


def _get_ios_wda_lock(udid: str) -> threading.Lock:
    device_id = str(udid or "").strip()
    if not device_id:
        raise RuntimeError("invalid udid")
    with _ios_wda_lock_guard:
        lock = _ios_wda_locks.get(device_id)
        if lock is None:
            lock = threading.Lock()
            _ios_wda_locks[device_id] = lock
        return lock


def _find_tidevice_pids_for_udid(udid: str) -> List[int]:
    device_id = str(udid or "").strip()
    if not device_id:
        return []
    try:
        proc = subprocess.run(
            ["ps", "-ax", "-o", "pid=,command="],
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception as exc:
        logger.warning("列出 tidevice 进程失败: udid=%s error=%s", device_id, exc)
        return []

    matched: List[int] = []
    marker = f"-u {device_id}"
    for line in proc.stdout.splitlines():
        raw = line.strip()
        if not raw:
            continue
        try:
            pid_text, command = raw.split(None, 1)
        except ValueError:
            continue
        if "tidevice" not in command:
            continue
        if marker not in command and device_id not in command:
            continue
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if pid == os.getpid():
            continue
        matched.append(pid)

    return sorted(set(matched))


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def _terminate_processes(pids: List[int]) -> List[int]:
    if not pids:
        return []
    terminated: List[int] = []
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            terminated.append(pid)
        except ProcessLookupError:
            continue
        except Exception as exc:
            logger.warning("终止 tidevice 进程失败: pid=%s error=%s", pid, exc)

    if not terminated:
        return []

    time.sleep(0.3)
    for pid in terminated:
        if not _pid_exists(pid):
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            continue
        except Exception as exc:
            logger.warning("强制杀死 tidevice 进程失败: pid=%s error=%s", pid, exc)
    return terminated


def _cleanup_ios_tidevice_processes(udid: str) -> Dict[str, Any]:
    device_id = str(udid or "").strip()
    if not device_id:
        return {"killed_pids": [], "killed_count": 0}
    # 先回收本进程托管的 relay，再兜底清理系统级残留 tidevice 进程。
    wda_relay_manager.stop_relay(device_id)
    stale_pids = _find_tidevice_pids_for_udid(device_id)
    killed_pids = _terminate_processes(stale_pids)
    if killed_pids:
        logger.info(
            "清理 iOS tidevice 残留进程: udid=%s pids=%s",
            device_id,
            killed_pids,
        )
    return {
        "killed_pids": killed_pids,
        "killed_count": len(killed_pids),
    }


def _resolve_ios_wda_bundle_id(session: Session, udid: str) -> str:
    scoped_key = f"ios_wda_bundle_id.{udid}"
    configured = get_setting_value(session, scoped_key) or get_setting_value(session, "ios_wda_bundle_id")
    if configured:
        return configured

    discovered = _discover_ios_wda_bundle_id(udid)
    if discovered:
        return discovered

    return DEFAULT_IOS_WDA_BUNDLE_ID


def _discover_ios_wda_bundle_id(udid: str) -> Optional[str]:
    """
    自动发现设备上已安装的 WDA runner 包名，避免默认包名与实际签名不一致。
    """
    device_id = str(udid or "").strip()
    if not device_id:
        return None

    try:
        proc = subprocess.run(
            ["tidevice", "-u", device_id, "applist"],
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception as exc:
        logger.warning("自动发现 WDA bundle id 失败: udid=%s error=%s", device_id, exc)
        return None

    exact_candidates: List[str] = []
    fuzzy_candidates: List[str] = []
    for raw in proc.stdout.splitlines():
        line = str(raw or "").strip()
        if not line:
            continue
        try:
            bundle_id = line.split(None, 1)[0].strip()
        except Exception:
            continue
        if not bundle_id:
            continue
        lowered = bundle_id.lower()
        if "webdriveragentrunner" in lowered and lowered.endswith(".xctrunner"):
            exact_candidates.append(bundle_id)
            continue
        if lowered.endswith(".xctrunner"):
            fuzzy_candidates.append(bundle_id)

    if exact_candidates:
        selected = exact_candidates[0]
        logger.info("自动发现 WDA runner bundle id: udid=%s bundle_id=%s", device_id, selected)
        return selected
    if fuzzy_candidates:
        selected = fuzzy_candidates[0]
        logger.info("自动发现 xctrunner bundle id: udid=%s bundle_id=%s", device_id, selected)
        return selected
    return None


def _discover_wda_xcodeproj_path() -> Optional[str]:
    """
    尝试发现本机 WebDriverAgent.xcodeproj。
    """
    candidates = [
        os.path.expanduser("~/Desktop/x/Apple/WebDriverAgent/WebDriverAgent.xcodeproj"),
        os.path.expanduser("~/WebDriverAgent/WebDriverAgent.xcodeproj"),
        os.path.expanduser("~/.appium/node_modules/appium-webdriveragent/WebDriverAgent.xcodeproj"),
        os.path.expanduser(
            "~/.appium/node_modules/appium-xcuitest-driver/node_modules/appium-webdriveragent/WebDriverAgent.xcodeproj"
        ),
        "/opt/homebrew/lib/node_modules/appium-webdriveragent/WebDriverAgent.xcodeproj",
        "/usr/local/lib/node_modules/appium-webdriveragent/WebDriverAgent.xcodeproj",
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return None


def _resolve_ios_wda_xcodebuild_target(session: Session, udid: str) -> Dict[str, Optional[str]]:
    """
    解析 xcodebuild 所需 target：优先 workspace，其次 project。
    """
    scoped_workspace = get_setting_value(session, f"ios_wda_xcworkspace_path.{udid}")
    global_workspace = get_setting_value(session, "ios_wda_xcworkspace_path")
    workspace_path = scoped_workspace or global_workspace
    if workspace_path and not os.path.exists(workspace_path):
        raise RuntimeError(f"ios_wda_xcworkspace_path 不存在: {workspace_path}")

    scoped_project = get_setting_value(session, f"ios_wda_xcodeproj_path.{udid}")
    global_project = get_setting_value(session, "ios_wda_xcodeproj_path")
    project_path = scoped_project or global_project or _discover_wda_xcodeproj_path()
    if project_path and not os.path.exists(project_path):
        raise RuntimeError(f"ios_wda_xcodeproj_path 不存在: {project_path}")

    if workspace_path:
        return {
            "workspace_path": workspace_path,
            "project_path": None,
        }
    if project_path:
        return {
            "workspace_path": None,
            "project_path": project_path,
        }
    raise RuntimeError(
        "未找到 WebDriverAgent 工程，请配置 ios_wda_xcodeproj_path 或 ios_wda_xcworkspace_path"
    )


def _build_ios_wda_xcodebuild_command(session: Session, udid: str) -> Dict[str, Any]:
    target = _resolve_ios_wda_xcodebuild_target(session, udid)
    scheme = (
        get_setting_value(session, f"ios_wda_scheme.{udid}")
        or get_setting_value(session, "ios_wda_scheme")
        or DEFAULT_IOS_WDA_SCHEME
    )
    derived_data_path = (
        get_setting_value(session, f"ios_wda_derived_data_path.{udid}")
        or get_setting_value(session, "ios_wda_derived_data_path")
    )
    extra_args = (
        get_setting_value(session, f"ios_wda_xcodebuild_args.{udid}")
        or get_setting_value(session, "ios_wda_xcodebuild_args")
    )

    cmd: List[str] = ["xcodebuild"]
    if target.get("workspace_path"):
        cmd.extend(["-workspace", str(target["workspace_path"])])
    else:
        cmd.extend(["-project", str(target["project_path"])])
    cmd.extend(
        [
            "-scheme",
            scheme,
            "-destination",
            f"id={udid}",
            "test",
        ]
    )
    if derived_data_path:
        cmd.extend(["-derivedDataPath", derived_data_path])
    if extra_args:
        try:
            rendered = str(extra_args).format(udid=udid, scheme=scheme)
            cmd.extend(shlex.split(rendered))
        except Exception as exc:
            raise RuntimeError(f"ios_wda_xcodebuild_args 渲染失败: {exc}") from exc

    return {
        "command": cmd,
        "command_source": "xcodebuild",
        "bundle_id": None,
        "bundle_source": None,
        "scheme": scheme,
        "workspace_path": target.get("workspace_path"),
        "project_path": target.get("project_path"),
        "derived_data_path": derived_data_path,
    }


def _build_ios_wda_launch_command(session: Session, udid: str) -> Dict[str, Any]:
    raw_cmd = (
        get_setting_value(session, f"ios_wda_launch_cmd.{udid}")
        or get_setting_value(session, "ios_wda_launch_cmd")
    )
    if raw_cmd:
        bundle_id = _resolve_ios_wda_bundle_id(session, udid)
        try:
            rendered_cmd = str(raw_cmd).format(udid=udid, bundle_id=bundle_id)
        except Exception as exc:
            raise RuntimeError(f"ios_wda_launch_cmd 渲染失败: {exc}") from exc
        cmd = shlex.split(rendered_cmd)
        if not cmd:
            raise RuntimeError("ios_wda_launch_cmd 解析后为空")
        return {
            "command": cmd,
            "command_source": "setting",
            "bundle_id": bundle_id,
            "bundle_source": "resolved",
        }

    if platform.system().lower() == "darwin":
        return _build_ios_wda_xcodebuild_command(session, udid)

    bundle_id = _resolve_ios_wda_bundle_id(session, udid)
    return {
        "command": ["tidevice", "-u", udid, "xctest", "--bundle_id", bundle_id],
        "command_source": "tidevice",
        "bundle_id": bundle_id,
        "bundle_source": "resolved",
    }


def _read_log_tail(path: Optional[str], *, max_lines: int = 20, max_chars: int = 2000) -> str:
    if not path:
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception:
        return ""

    tail = "".join(lines[-max_lines:]).strip()
    if len(tail) > max_chars:
        return tail[-max_chars:]
    return tail


def _is_no_app_matches_error(message: str) -> bool:
    text = str(message or "").lower()
    return "no app matches" in text or "bundle id" in text and "not found" in text


def _launch_ios_wda_process(session: Session, udid: str) -> Dict[str, Any]:
    launch_meta = _build_ios_wda_launch_command(session, udid)
    command = list(launch_meta["command"])
    logger.info(
        "启动 iOS WDA: udid=%s source=%s cmd=%s",
        udid,
        launch_meta.get("command_source"),
        " ".join(command),
    )
    max_attempts = 2
    for launch_attempt in range(1, max_attempts + 1):
        log_path = tempfile.NamedTemporaryFile(
            prefix=f"ios_wda_start_{udid}_",
            suffix=".log",
            delete=False,
        ).name
        try:
            with open(log_path, "ab") as log_file:
                process = subprocess.Popen(
                    command,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
        except FileNotFoundError as exc:
            binary = command[0] if command else "command"
            raise RuntimeError(f"{binary} command not found") from exc
        except Exception as exc:
            raise RuntimeError(f"启动 WDA 命令失败: {exc}") from exc

        time.sleep(0.5)
        exit_code = process.poll()
        if exit_code is None:
            launch_meta["pid"] = process.pid
            launch_meta["command_str"] = " ".join(command)
            launch_meta["log_path"] = log_path
            return launch_meta

        log_tail = _read_log_tail(log_path)
        reason = f"启动 WDA 命令提前退出 (exit_code={exit_code})"
        if log_tail:
            reason = f"{reason}: {log_tail}"

        can_retry_with_discovered_bundle = (
            launch_attempt == 1
            and bool(command)
            and str(command[0]) == "tidevice"
            and str(launch_meta.get("command_source")) != "setting"
            and _is_no_app_matches_error(reason)
        )
        if can_retry_with_discovered_bundle:
            discovered = _discover_ios_wda_bundle_id(udid)
            current_bundle = str(launch_meta.get("bundle_id") or "").strip()
            if discovered and discovered != current_bundle:
                logger.info(
                    "WDA runner bundle id 自动回退重试: udid=%s from=%s to=%s",
                    udid,
                    current_bundle,
                    discovered,
                )
                command = ["tidevice", "-u", udid, "xctest", "--bundle_id", discovered]
                launch_meta["bundle_id"] = discovered
                launch_meta["command_source"] = "tidevice.discovered"
                continue

        raise RuntimeError(reason)

    raise RuntimeError("启动 WDA 失败：未知错误")


def _coerce_positive_int(value: Optional[str], default: int) -> int:
    try:
        parsed = int(str(value).strip())
        return parsed if parsed > 0 else default
    except Exception:
        return default


def _coerce_positive_float(value: Optional[str], default: float) -> float:
    try:
        parsed = float(str(value).strip())
        return parsed if parsed > 0 else default
    except Exception:
        return default


def _resolve_ios_wda_start_policy(session: Session, udid: str) -> Dict[str, Any]:
    attempts = _coerce_positive_int(
        get_setting_value(session, f"ios_wda_start_retry_attempts.{udid}")
        or get_setting_value(session, "ios_wda_start_retry_attempts"),
        DEFAULT_IOS_WDA_START_RETRY_ATTEMPTS,
    )
    interval_seconds = _coerce_positive_float(
        get_setting_value(session, f"ios_wda_start_retry_interval_seconds.{udid}")
        or get_setting_value(session, "ios_wda_start_retry_interval_seconds"),
        DEFAULT_IOS_WDA_START_RETRY_INTERVAL_SECONDS,
    )
    return {
        "retry_attempts": attempts,
        "retry_interval_seconds": interval_seconds,
    }


def _ensure_ios_wda_ready(
    session: Session,
    udid: str,
    *,
    retry_attempts: Optional[int] = None,
    retry_interval_seconds: Optional[float] = None,
) -> Dict[str, Any]:
    device_id = str(udid or "").strip()
    if not device_id:
        return {
            "healthy": False,
            "wda_url": None,
            "error": "invalid udid",
            "attempted_start": False,
            "recovered_by_cleanup": False,
            "cleanup": {"killed_pids": [], "killed_count": 0},
            "start_command": None,
            "start_pid": None,
            "start_command_source": None,
            "start_bundle_id": None,
            "start_log_path": None,
            "startup_checks": 0,
        }

    lock = _get_ios_wda_lock(device_id)
    with lock:
        policy = _resolve_ios_wda_start_policy(session, device_id)
        effective_attempts = max(1, retry_attempts or policy["retry_attempts"])
        effective_interval = (
            retry_interval_seconds
            if retry_interval_seconds is not None
            else policy["retry_interval_seconds"]
        )

        first_check = _check_ios_wda_health(session, device_id)
        if bool(first_check.get("healthy")):
            return {
                "healthy": True,
                "wda_url": first_check.get("wda_url"),
                "error": None,
                "attempted_start": False,
                "recovered_by_cleanup": False,
                "cleanup": {"killed_pids": [], "killed_count": 0},
                "start_command": None,
                "start_pid": None,
                "start_command_source": None,
                "start_bundle_id": None,
                "start_log_path": None,
                "startup_checks": 1,
            }

        cleanup_result = _cleanup_ios_tidevice_processes(device_id)
        second_check = _check_ios_wda_health(session, device_id)
        if bool(second_check.get("healthy")):
            return {
                "healthy": True,
                "wda_url": second_check.get("wda_url"),
                "error": None,
                "attempted_start": False,
                "recovered_by_cleanup": True,
                "cleanup": cleanup_result,
                "start_command": None,
                "start_pid": None,
                "start_command_source": None,
                "start_bundle_id": None,
                "start_log_path": None,
                "startup_checks": 2,
            }

        launch_error: Optional[str] = None
        launch_meta: Dict[str, Any] = {}
        try:
            launch_meta = _launch_ios_wda_process(session, device_id)
        except Exception as exc:
            launch_error = str(exc)
            logger.warning("iOS WDA 启动失败: udid=%s error=%s", device_id, launch_error)

        startup_checks = 2
        last_check = second_check
        if launch_error is None:
            for attempt in range(1, effective_attempts + 1):
                if attempt > 1 and effective_interval > 0:
                    time.sleep(effective_interval)
                startup_checks += 1
                current = _check_ios_wda_health(session, device_id)
                if bool(current.get("healthy")):
                    return {
                        "healthy": True,
                        "wda_url": current.get("wda_url"),
                        "error": None,
                        "attempted_start": True,
                        "recovered_by_cleanup": False,
                        "cleanup": cleanup_result,
                        "start_command": launch_meta.get("command_str"),
                        "start_pid": launch_meta.get("pid"),
                        "start_command_source": launch_meta.get("command_source"),
                        "start_bundle_id": launch_meta.get("bundle_id"),
                        "start_log_path": launch_meta.get("log_path"),
                        "startup_checks": startup_checks,
                    }
                last_check = current

        error_message = launch_error or last_check.get("error") or "WDA 启动后健康检查失败"
        launch_log_tail = _read_log_tail(launch_meta.get("log_path")) if launch_meta else ""
        if launch_log_tail and launch_log_tail not in str(error_message):
            error_message = f"{error_message}; launch_log={launch_log_tail}"
        return {
            "healthy": False,
            "wda_url": last_check.get("wda_url"),
            "error": error_message,
            "attempted_start": True,
            "recovered_by_cleanup": False,
            "cleanup": cleanup_result,
            "start_command": launch_meta.get("command_str"),
            "start_pid": launch_meta.get("pid"),
            "start_command_source": launch_meta.get("command_source"),
            "start_bundle_id": launch_meta.get("bundle_id"),
            "start_log_path": launch_meta.get("log_path"),
            "startup_checks": startup_checks,
        }


def _mark_running_executions_aborted(session: Session, serial: str) -> int:
    """回收指定设备仍处于 RUNNING 的执行记录。"""
    now = datetime.now()
    running_executions = session.exec(
        select(TestExecution).where(
            TestExecution.device_serial == serial,
            TestExecution.status == "RUNNING",
        )
    ).all()

    for execution in running_executions:
        execution.status = "ERROR"
        execution.end_time = now
        if execution.start_time:
            execution.duration = max((now - execution.start_time).total_seconds(), 0.0)
        session.add(execution)

    return len(running_executions)


# ==================== ADB 异步工具函数 ====================

async def _run_adb_command(*args: str, timeout: int = 15) -> bytes:
    """异步执行 ADB 命令并返回 stdout 字节流"""
    cmd = ["adb"] + list(args)
    logger.info(f"执行 ADB 命令: {' '.join(cmd)}")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
        if proc.returncode != 0:
            err_msg = stderr.decode("utf-8", errors="replace").strip()
            logger.warning(f"ADB 命令失败 (rc={proc.returncode}): {err_msg}")
            raise RuntimeError(f"ADB error: {err_msg}")
        return stdout
    except asyncio.TimeoutError:
        logger.error(f"ADB 命令超时: {' '.join(cmd)}")
        raise RuntimeError("ADB command timed out")


async def _get_device_prop(serial: str, prop: str) -> str:
    """获取设备单个属性值"""
    try:
        raw = await _run_adb_command("-s", serial, "shell", "getprop", prop)
        return raw.decode("utf-8", errors="replace").strip()
    except Exception as e:
        logger.warning(f"获取 {serial} 属性 {prop} 失败: {e}")
        return ""


async def _get_device_resolution(serial: str) -> str:
    """获取设备分辨率"""
    try:
        raw = await _run_adb_command("-s", serial, "shell", "wm", "size")
        # 输出格式: "Physical size: 1080x2340"
        text = raw.decode("utf-8", errors="replace").strip()
        if ":" in text:
            return text.split(":")[-1].strip()
        return text
    except Exception:
        return ""


async def _run_blocking_call(func, *args):
    """兼容 Python 3.8：在线程池执行阻塞调用。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: func(*args))


def _capture_ios_screenshot_bytes(serial: str, wda_url: str) -> bytes:
    """通过现有 IOSDriver + WDA 链路获取 iOS 屏幕快照。"""
    from backend.drivers.ios_driver import IOSDriver

    driver = IOSDriver(device_id=serial, wda_url=wda_url)
    try:
        return driver.screenshot()
    finally:
        try:
            driver.disconnect()
        except Exception as exc:
            logger.warning("iOS 截图后断开驱动失败: serial=%s error=%s", serial, exc)


# ==================== API 端点 ====================


@router.get("/", response_model=List[DeviceRead])
async def list_devices(session: Session = Depends(get_session)):
    """获取所有设备列表,并实时检查在线状态"""
    devices = session.exec(select(Device).order_by(Device.status, Device.model)).all()
    
    # 实时检查哪些 Android 设备在线（iOS 设备保持上次同步状态）
    try:
        raw = await _run_adb_command("devices")
        lines = raw.decode("utf-8", errors="replace").strip().splitlines()
        online_serials = set()
        for line in lines[1:]:
            parts = line.strip().split()
            if len(parts) >= 2 and parts[1] == "device":
                online_serials.add(parts[0])
        
        # 更新设备状态（仅 Android）
        status_updated = False
        for device in devices:
            if getattr(device, "platform", "android") == "ios":
                continue  # iOS 设备跳过 ADB 检测
            if device.serial in online_serials:
                if device.status == "OFFLINE":
                    device.status = "IDLE"
                    device.updated_at = datetime.now()
                    session.add(device)
                    status_updated = True
            else:
                if device.status != "OFFLINE":
                    device.status = "OFFLINE"
                    device.updated_at = datetime.now()
                    session.add(device)
                    status_updated = True
        
        if status_updated:
            session.commit()
            # 重新查询以获取最新状态
            devices = session.exec(select(Device).order_by(Device.status, Device.model)).all()
    except Exception as e:
        logger.warning(f"检查设备在线状态失败: {e}")
    
    return devices


@router.put("/{serial}/name", response_model=DeviceRead)
def rename_device(
    serial: str,
    req: DeviceRenameRequest,
    session: Session = Depends(get_session),
    current_user=Depends(get_current_user),
):
    """修改设备自定义名称"""
    device = session.exec(select(Device).where(Device.serial == serial)).first()
    if not device:
        raise HTTPException(status_code=404, detail="设备不存在")
    device.custom_name = req.custom_name
    device.updated_at = datetime.now()
    session.add(device)
    session.commit()
    session.refresh(device)
    return device


@router.post("/{serial}/wda/check")
def check_ios_wda(serial: str, session: Session = Depends(get_session)):
    """
    手动启动/修复指定 iOS 设备 WDA，并同步回写设备状态。
    """
    device = session.exec(select(Device).where(Device.serial == serial)).first()
    if not device:
        raise HTTPException(status_code=404, detail="设备不存在")

    platform = str(device.platform or "android").strip().lower()
    if platform != "ios":
        raise HTTPException(
            status_code=400,
            detail="P3002_WDA_IOS_ONLY: WDA 启动仅适用于 iOS 设备。",
        )

    result = _ensure_ios_wda_ready(session, serial)
    healthy = bool(result.get("healthy"))
    status_after_check = _resolve_ios_device_status(device.status, healthy)
    device.status = status_after_check
    device.updated_at = datetime.now()
    session.add(device)
    session.commit()

    return {
        "serial": serial,
        "platform": "ios",
        "wda_healthy": healthy,
        "wda_url": result.get("wda_url"),
        "status": status_after_check,
        "error": result.get("error"),
        "attempted_start": bool(result.get("attempted_start")),
        "recovered_by_cleanup": bool(result.get("recovered_by_cleanup")),
        "cleanup_killed_pids": (result.get("cleanup") or {}).get("killed_pids", []),
        "cleanup_killed_count": int((result.get("cleanup") or {}).get("killed_count", 0)),
        "startup_checks": int(result.get("startup_checks") or 0),
        "start_command": result.get("start_command"),
        "start_pid": result.get("start_pid"),
        "start_command_source": result.get("start_command_source"),
        "start_bundle_id": result.get("start_bundle_id"),
        "start_log_path": result.get("start_log_path"),
    }


@router.get("/wda/relays")
def list_wda_relays():
    """查看当前 WDA relay 端口映射状态（运维诊断接口）。"""
    return {"items": wda_relay_manager.list_relays()}


@router.post("/sync", response_model=DeviceSyncResponse)
async def sync_devices(
    session: Session = Depends(get_session),
    current_user=Depends(get_current_user),
):
    """
    一键同步物理设备

    1. 执行 adb devices 获取在线 serial 列表
    2. 并发获取每个设备的型号、版本、分辨率
    3. 与数据库 Merge（新增/更新/标记离线）
    """
    # Step 1: 解析 adb devices 输出
    try:
        raw = await _run_adb_command("devices")
        lines = raw.decode("utf-8", errors="replace").strip().splitlines()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"执行 adb devices 失败: {e}")

    online_serials = []
    for line in lines[1:]:  # 跳过 "List of devices attached" 标题行
        parts = line.strip().split()
        if len(parts) >= 2 and parts[1] == "device":
            online_serials.append(parts[0])

    # Step 2: 并发获取设备属性
    async def _fetch_device_info(serial: str) -> dict:
        # 1. 获取基础属性
        model, brand, version, resolution = await asyncio.gather(
            _get_device_prop(serial, "ro.product.model"),
            _get_device_prop(serial, "ro.product.brand"),
            _get_device_prop(serial, "ro.build.version.release"),
            _get_device_resolution(serial),
        )
        
        # 2. 根据厂商特性尝试获取市场名称
        market_name_props = [
            "ro.product.marketname",  # 通用
            "ro.vendor.oplus.market.name",  # OPPO / OnePlus
            "ro.vivo.market.name",  # vivo
            "ro.product.model", # 作为最低限度的 fallback，但我们会优先检查前面的
        ]
        
        market_name = ""
        for prop in market_name_props:
            val = await _get_device_prop(serial, prop)
            if val and val.lower() not in ["null", "unknown"]:
                market_name = val
                break
                
        # 如果 market_name 提取出来和 model 相同，或者提取失败，统一交给前端去 fallback 展示
        if not market_name or market_name == model:
            market_name = None

        return {
            "serial": serial,
            "platform": "android",
            "model": model or "Unknown",
            "brand": (brand or "").upper(),
            "android_version": version,
            "os_version": version,  # 同步写入跨平台版本号
            "resolution": resolution,
            "market_name": market_name,
        }

    device_infos = await asyncio.gather(
        *[_fetch_device_info(s) for s in online_serials]
    )

    # Step 3: Merge 到数据库
    synced_count = 0
    for info in device_infos:
        existing = session.exec(
            select(Device).where(Device.serial == info["serial"])
        ).first()
        if existing:
            existing.platform = info.get("platform", "android")
            existing.model = info["model"]
            existing.brand = info["brand"]
            existing.android_version = info["android_version"]
            existing.os_version = info.get("os_version", info["android_version"])
            existing.resolution = info["resolution"]
            existing.market_name = info["market_name"]
            existing.status = "IDLE" if existing.status == "OFFLINE" else existing.status
            existing.updated_at = datetime.now()
            session.add(existing)
        else:
            device = Device(
                serial=info["serial"],
                platform=info.get("platform", "android"),
                model=info["model"],
                brand=info["brand"],
                android_version=info["android_version"],
                os_version=info.get("os_version", info["android_version"]),
                resolution=info["resolution"],
                market_name=info["market_name"],
                status="IDLE",
            )
            session.add(device)
            synced_count += 1

    # ==================== iOS 设备扫描 ====================
    ios_online_serials: list[str] = []
    try:
        from backend.ios_scanner import get_ios_devices
        ios_devices = get_ios_devices()
        for ios_dev in ios_devices:
            udid = ios_dev["device_id"]
            ios_online_serials.append(udid)
            wda_healthy = _is_ios_wda_healthy(session, udid)
            existing = session.exec(
                select(Device).where(Device.serial == udid)
            ).first()
            if existing:
                existing.platform = "ios"
                existing.model = ios_dev["model"]
                existing.brand = "APPLE"
                existing.os_version = ios_dev["os_version"]
                existing.market_name = ios_dev["name"]
                existing.status = _resolve_ios_device_status(existing.status, wda_healthy)
                existing.updated_at = datetime.now()
                session.add(existing)
            else:
                device = Device(
                    serial=udid,
                    platform="ios",
                    model=ios_dev["model"],
                    brand="APPLE",
                    os_version=ios_dev["os_version"],
                    market_name=ios_dev["name"],
                    status=_resolve_ios_device_status(None, wda_healthy),
                )
                session.add(device)
                synced_count += 1
    except ImportError:
        logger.warning("tidevice 未安装，跳过 iOS 设备扫描")
    except Exception as e:
        logger.warning(f"iOS 设备扫描失败（不影响 Android 结果）: {e}")

    # 将不在物理列表中的设备标记为 OFFLINE
    all_online = set(online_serials) | set(ios_online_serials)
    all_db_devices = session.exec(select(Device)).all()
    for db_dev in all_db_devices:
        if db_dev.serial not in all_online and db_dev.status != "OFFLINE":
            db_dev.status = "OFFLINE"
            db_dev.updated_at = datetime.now()
            session.add(db_dev)

    session.commit()

    # 查询最终结果
    all_devices = session.exec(select(Device).order_by(Device.status, Device.model)).all()
    total_offline = sum(1 for d in all_devices if d.status == "OFFLINE")

    return DeviceSyncResponse(
        synced=synced_count,
        online=len(all_online),
        offline=total_offline,
        devices=all_devices,
    )


@router.get("/{serial}/screenshot")
async def get_screenshot(serial: str, session: Session = Depends(get_session)):
    """
    内存级屏幕快照

    执行 adb exec-out screencap -p，直接将 stdout 二进制流转为 Base64，
    全程不写磁盘，速度极快。
    """
    db_device = session.exec(select(Device).where(Device.serial == serial)).first()
    is_ios_device = bool(
        db_device and str(db_device.platform or "android").strip().lower() == "ios"
    )

    try:
        if is_ios_device:
            from backend.cross_platform_execution import check_wda_health, resolve_ios_wda_url

            wda_url = resolve_ios_wda_url(session, serial)
            try:
                await _run_blocking_call(check_wda_health, wda_url)
            except Exception as exc:
                db_device.status = _resolve_ios_device_status(db_device.status, False)
                db_device.updated_at = datetime.now()
                session.add(db_device)
                session.commit()
                raise RuntimeError(f"WDA 不可用，请先启动WDA：{exc}") from exc

            raw_bytes = await _run_blocking_call(_capture_ios_screenshot_bytes, serial, wda_url)
            db_device.status = _resolve_ios_device_status(db_device.status, True)
            db_device.updated_at = datetime.now()
            session.add(db_device)
            session.commit()
        else:
            if db_device:
                _ensure_android_device(db_device, action="截图")

            raw_bytes = await _run_adb_command(
                "-s", serial, "exec-out", "screencap", "-p",
                timeout=10,
            )

        if not raw_bytes or len(raw_bytes) < 100:
            raise RuntimeError("截图数据为空或异常")
        base64_img = base64.b64encode(raw_bytes).decode("utf-8")
        return {"base64_img": base64_img}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"截图失败: {e}")


@router.post("/{serial}/unlock")
async def unlock_device(
    serial: str,
    session: Session = Depends(get_session),
    current_user=Depends(get_current_user),
):
    """
    强制释放设备锁

    - Android: 杀掉残留自动化进程（Fastbot/Monkey/uiautomator2）
    - Android/iOS: 触发 Python 侧中止信号，回收运行中执行记录
    - iOS: 根据 WDA 健康度恢复为 IDLE 或 WDA_DOWN
    """
    device = session.exec(select(Device).where(Device.serial == serial)).first()
    if not device:
        raise HTTPException(status_code=404, detail="设备不存在")
    platform = str(device.platform or "android").strip().lower()
    wda_result: Optional[Dict[str, Any]] = None

    if platform == "android":
        # 杀掉设备上可能残留的自动化进程
        # 注意: monkey 由 app_process 启动，am force-stop 无效，必须用 pkill
        kill_commands = [
            # Fastbot: 杀 monkey 主进程 + app_process 子进程
            f"adb -s {serial} shell pkill -f 'com.android.commands.monkey'",
            f"adb -s {serial} shell pkill -f 'app_process.*monkey'",
            # uiautomator2: 停止 ATX agent 服务
            f"adb -s {serial} shell am force-stop com.github.uiautomator",
            f"adb -s {serial} shell am force-stop com.github.uiautomator.test",
            f"adb -s {serial} shell pkill -f 'uiautomator'",
        ]
        for cmd in kill_commands:
            try:
                proc = await asyncio.create_subprocess_shell(
                    cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(proc.communicate(), timeout=5)
                logger.info(f"执行清理命令: {cmd.split('shell ')[-1]}")
            except Exception:
                pass  # 进程不存在时命令会失败，忽略

        # 同步清除 Fastbot 内存锁
        try:
            from backend.api.fastbot import _unlock_device as fastbot_unlock

            fastbot_unlock(serial)
        except Exception:
            pass

    # ★ 触发 Python 端中止信号（中断正在执行的 runner 线程）
    try:
        from backend.runner import trigger_device_abort

        trigger_device_abort(serial)
    except Exception:
        pass

    recovered_executions = _mark_running_executions_aborted(session, serial)

    if platform == "ios":
        wda_result = _check_ios_wda_health(session, serial)
        device.status = "IDLE" if bool(wda_result.get("healthy")) else "WDA_DOWN"
    else:
        device.status = "IDLE"

    device.updated_at = datetime.now()
    session.add(device)
    session.commit()
    session.refresh(device)

    message = f"设备 {serial} 已释放"
    if platform == "android":
        message += "，已终止设备端残留进程"
    elif device.status == "WDA_DOWN":
        message += "，但当前 WDA 不可用"

    return {
        "message": message,
        "platform": platform,
        "recovered_executions": recovered_executions,
        "wda_healthy": None if wda_result is None else bool(wda_result.get("healthy")),
        "wda_url": None if wda_result is None else wda_result.get("wda_url"),
        "wda_error": None if wda_result is None else wda_result.get("error"),
        "device": DeviceRead.model_validate(device),
    }


@router.post("/{serial}/reboot")
async def reboot_device(
    serial: str,
    session: Session = Depends(get_session),
    current_user=Depends(get_current_user),
):
    """重启设备 - 执行 adb reboot 并标记为 OFFLINE"""
    device = session.exec(select(Device).where(Device.serial == serial)).first()
    if not device:
        raise HTTPException(status_code=404, detail="设备不存在")
    _ensure_android_device(device, action="重启")

    try:
        await _run_adb_command("-s", serial, "reboot", timeout=10)
    except Exception as e:
        logger.warning(f"重启命令执行异常（可能正常）: {e}")

    device.status = "OFFLINE"
    device.updated_at = datetime.now()
    session.add(device)
    session.commit()
    session.refresh(device)
    return {"message": f"设备 {serial} 正在重启", "device": DeviceRead.model_validate(device)}
