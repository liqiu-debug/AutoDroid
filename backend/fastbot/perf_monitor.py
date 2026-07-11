"""性能采集：CPU/内存监控协程与 dumpsys gfxinfo 汇总解析。"""
import re
import asyncio
import logging
from datetime import datetime
from typing import Dict, List, Optional

from backend.fastbot.adb import ADB_MONITOR_COMMAND_TIMEOUT_SECONDS, _adb_shell

logger = logging.getLogger("FastbotRunner")

TOTAL_FRAMES_PATTERN = re.compile(r"Total frames rendered:\s*(\d+)", re.IGNORECASE)
JANKY_FRAMES_PATTERN = re.compile(r"Janky frames:\s*(\d+)\s*\(([\d.]+)%\)", re.IGNORECASE)
MISSED_VSYNC_PATTERN = re.compile(r"Number Missed Vsync:\s*(\d+)", re.IGNORECASE)
SLOW_UI_THREAD_PATTERN = re.compile(r"Number Slow UI thread:\s*(\d+)", re.IGNORECASE)
SLOW_BITMAP_PATTERN = re.compile(r"Number Slow bitmap uploads:\s*(\d+)", re.IGNORECASE)
SLOW_DRAW_PATTERN = re.compile(r"Number Slow issue draw commands:\s*(\d+)", re.IGNORECASE)
FRAME_DEADLINE_MISSED_PATTERN = re.compile(r"Number Frame deadline missed:\s*(\d+)", re.IGNORECASE)
FROZEN_FRAMES_PATTERN = re.compile(r"Number Frozen frames:\s*(\d+)", re.IGNORECASE)

JANK_SAMPLE_INTERVAL_SECONDS = 5
JANK_ACTIVE_FRAME_THRESHOLD = 20


async def _monitor_performance(
    device_serial: str,
    package_name: str,
    stop_event: asyncio.Event,
    perf_data: List[Dict],
    interval: int = 10,
):
    """子协程：定期采集 CPU/内存"""
    while not stop_event.is_set():
        try:
            cpu_info = await _adb_shell(
                device_serial,
                f"dumpsys cpuinfo | grep {package_name} | head -1",
                timeout=ADB_MONITOR_COMMAND_TIMEOUT_SECONDS,
            )
            mem_info = await _adb_shell(
                device_serial,
                f"dumpsys meminfo {package_name} | grep 'TOTAL PSS' | head -1",
                timeout=ADB_MONITOR_COMMAND_TIMEOUT_SECONDS,
            )

            cpu_val = 0.0
            mem_val = 0.0

            cpu_match = re.search(r"([\d.]+)%", cpu_info)
            if cpu_match:
                cpu_val = float(cpu_match.group(1))

            mem_match = re.search(r"TOTAL\s+PSS:\s+([\d,]+)", mem_info)
            if not mem_match:
                mem_match = re.search(r"([\d,]+)\s+K", mem_info)
            if mem_match:
                mem_val = int(mem_match.group(1).replace(",", "")) / 1024.0

            perf_data.append({
                "time": datetime.now().strftime("%H:%M:%S"),
                "cpu": round(cpu_val, 1),
                "mem": round(mem_val, 1),
            })
        except Exception as e:
            logger.warning(f"性能采集异常: {e}")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            break
        except asyncio.TimeoutError:
            pass


def _extract_int(pattern: re.Pattern, text: str, default: int = 0) -> int:
    match = pattern.search(text or "")
    if not match:
        return default
    return int(match.group(1))


def _parse_gfxinfo_output(
    output: str,
    interval_sec: int = JANK_SAMPLE_INTERVAL_SECONDS,
    timestamp: Optional[str] = None,
) -> Optional[Dict]:
    """解析 dumpsys gfxinfo 输出，提取当前窗口的卡顿指标。"""
    if not output:
        return None

    text = output.strip()
    if not text or "No process found" in text or "Graphics info for pid 0" in text:
        return None

    total_match = TOTAL_FRAMES_PATTERN.search(text)
    if not total_match:
        return None

    total_frames = int(total_match.group(1))
    jank_match = JANKY_FRAMES_PATTERN.search(text)
    jank_frames = int(jank_match.group(1)) if jank_match else 0
    jank_rate = (float(jank_match.group(2)) / 100.0) if jank_match else (
        (jank_frames / total_frames) if total_frames > 0 else 0.0
    )

    slow_ui = _extract_int(SLOW_UI_THREAD_PATTERN, text)
    slow_bitmap = _extract_int(SLOW_BITMAP_PATTERN, text)
    slow_draw = _extract_int(SLOW_DRAW_PATTERN, text)
    frozen_frames = _extract_int(FROZEN_FRAMES_PATTERN, text)
    deadline_missed = _extract_int(FRAME_DEADLINE_MISSED_PATTERN, text)

    render_throughput = round((total_frames / interval_sec) if interval_sec > 0 else 0.0, 1)
    is_idle = total_frames < JANK_ACTIVE_FRAME_THRESHOLD

    return {
        "time": timestamp or datetime.now().strftime("%H:%M:%S"),
        "window_sec": interval_sec,
        "fps": render_throughput,
        "render_throughput": render_throughput,
        "jank_rate": round(jank_rate, 4),
        "total_frames": total_frames,
        "jank_frames": jank_frames,
        "slow_frames": slow_ui + slow_bitmap + slow_draw,
        "frozen_frames": frozen_frames,
        "missed_vsync": _extract_int(MISSED_VSYNC_PATTERN, text),
        "frame_deadline_missed": deadline_missed,
        "is_idle": is_idle,
        "source": "gfxinfo",
    }
