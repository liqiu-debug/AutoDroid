"""崩溃/ANR 检测：logcat 流式监控与快照抓取。"""
import re
import asyncio
import logging
from datetime import datetime
from typing import Awaitable, Callable, Dict, List, Optional

from backend.fastbot.adb import _terminate_subprocess

logger = logging.getLogger("FastbotRunner")

CRASH_PATTERN = re.compile(r"(FATAL EXCEPTION|ANR in)", re.IGNORECASE)
ANR_PATTERN = re.compile(r"ANR in", re.IGNORECASE)
PROC_LINE_PATTERN = re.compile(r"Process:\s*(\S+)")
ANR_PKG_PATTERN = re.compile(r"ANR in\s+(\S+)")

LOGCAT_SNAPSHOT_TIMEOUT_SECONDS = 8
DEDUP_COOLDOWN_SECONDS = 10
CRASH_SOURCE_TAG = re.compile(r"E/AndroidRuntime\s*\(")


async def _monitor_logcat(
    device_serial: str,
    package_name: str,
    stop_event: asyncio.Event,
    crash_events: List[Dict],
    capture_log: bool,
    abort_on_crash: bool = False,
    abort_event: Optional[asyncio.Event] = None,
    replay_callback: Optional[Callable[[str, str], Awaitable[Optional[Dict]]]] = None,
):
    """子协程：持续读取 logcat 流，只抓取目标包名相关的崩溃/ANR。

    策略：
    - 启动前调用方已清空 logcat 缓冲区，避免旧日志干扰
    - 只认 E/AndroidRuntime 标签的 FATAL EXCEPTION，忽略厂商重复条目
    - 同类事件在冷却期(10s)内不重复计数
    - abort_on_crash=True 时，检测到崩溃后触发 abort_event 通知主协程终止 Monkey
    """
    proc = await asyncio.create_subprocess_shell(
        f"adb -s {device_serial} logcat -v time *:E",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    pending_crash = False
    pending_lines: List[str] = []
    crash_timestamp = ""
    MAX_LOOK_AHEAD = 15
    last_crash_time: Optional[datetime] = None
    last_anr_time: Optional[datetime] = None

    try:
        while not stop_event.is_set():
            try:
                line_bytes = await asyncio.wait_for(
                    proc.stdout.readline(), timeout=1.0
                )
            except asyncio.TimeoutError:
                if pending_crash:
                    pending_crash = False
                    pending_lines.clear()
                continue
            if not line_bytes:
                break

            line = line_bytes.decode(errors="ignore")

            if ANR_PATTERN.search(line):
                anr_pkg = ANR_PKG_PATTERN.search(line)
                if anr_pkg and package_name in anr_pkg.group(1):
                    now = datetime.now()
                    if last_anr_time and (now - last_anr_time).total_seconds() < DEDUP_COOLDOWN_SECONDS:
                        logger.debug(f"ANR 去重冷却中，忽略: {line.strip()[:120]}")
                        continue
                    last_anr_time = now
                    full_log = ""
                    if capture_log:
                        full_log = await _capture_logcat_snapshot(device_serial)
                    event_time = now.strftime("%H:%M:%S")
                    event = {
                        "time": event_time,
                        "type": "ANR",
                        "full_log": full_log,
                    }
                    if replay_callback:
                        try:
                            replay = await replay_callback("ANR", event_time)
                            if replay:
                                event["replay"] = replay
                        except Exception as exc:
                            event["replay"] = {
                                "status": "FAILED",
                                "error": str(exc),
                            }
                    crash_events.append(event)
                    logger.warning(f"检测到 ANR ({package_name}): {line.strip()[:200]}")
                    if abort_on_crash and abort_event:
                        logger.warning(f"容错策略=立即停止，触发终止")
                        abort_event.set()
                        return
                continue

            if re.search(r"FATAL EXCEPTION", line, re.IGNORECASE):
                if not CRASH_SOURCE_TAG.search(line):
                    continue
                pending_crash = True
                pending_lines = [line]
                crash_timestamp = datetime.now().strftime("%H:%M:%S")
                continue

            if pending_crash:
                pending_lines.append(line)
                proc_match = PROC_LINE_PATTERN.search(line)
                if proc_match:
                    crash_pkg = proc_match.group(1).rstrip(",")
                    if package_name in crash_pkg:
                        now = datetime.now()
                        if last_crash_time and (now - last_crash_time).total_seconds() < DEDUP_COOLDOWN_SECONDS:
                            logger.debug(f"CRASH 去重冷却中，忽略: {pending_lines[0].strip()[:120]}")
                        else:
                            last_crash_time = now
                            full_log = ""
                            if capture_log:
                                full_log = await _capture_logcat_snapshot(device_serial)
                            event = {
                                "time": crash_timestamp,
                                "type": "CRASH",
                                "full_log": full_log,
                            }
                            if replay_callback:
                                try:
                                    replay = await replay_callback("CRASH", crash_timestamp)
                                    if replay:
                                        event["replay"] = replay
                                except Exception as exc:
                                    event["replay"] = {
                                        "status": "FAILED",
                                        "error": str(exc),
                                    }
                            crash_events.append(event)
                            logger.warning(f"检测到 CRASH ({package_name}): {pending_lines[0].strip()[:200]}")
                            if abort_on_crash and abort_event:
                                logger.warning(f"容错策略=立即停止，触发终止")
                                abort_event.set()
                                pending_crash = False
                                pending_lines.clear()
                                return
                    else:
                        logger.debug(f"忽略非目标包 CRASH: {crash_pkg}")
                    pending_crash = False
                    pending_lines.clear()
                elif len(pending_lines) >= MAX_LOOK_AHEAD:
                    logger.debug(f"FATAL EXCEPTION 后 {MAX_LOOK_AHEAD} 行内未找到 Process 行，忽略")
                    pending_crash = False
                    pending_lines.clear()
    finally:
        proc.terminate()
        try:
            await proc.wait()
        except Exception:
            pass


async def _capture_logcat_snapshot(device_serial: str) -> str:
    """截取最近 500 行 logcat"""
    proc = await asyncio.create_subprocess_shell(
        f"adb -s {device_serial} logcat -d -t 500",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await asyncio.wait_for(
            proc.communicate(),
            timeout=LOGCAT_SNAPSHOT_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        await _terminate_subprocess(proc)
        logger.warning("抓取 logcat 快照超时: serial=%s", device_serial)
        return ""
    return stdout.decode(errors="ignore")
