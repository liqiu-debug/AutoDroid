"""
Fastbot 智能探索执行引擎

核心功能：
- 自动挂载 Fastbot 所需 jar 包到手机
- 拼接并执行 Monkey 命令
- 双协程并发：主进程执行 + 子协程监控 CPU/Mem/Crash
- 通过 asyncio.Event 协调协程退出
"""
import os
import re
import json
import asyncio
import logging
from datetime import datetime
from typing import List, Dict, Optional

logger = logging.getLogger("FastbotRunner")

PROJECT_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
FASTBOT_ASSETS_DIR = os.path.join(PROJECT_ROOT, "resources", "fastbot")

DEVICE_JARS = [
    "framework.jar",
    "monkeyq.jar",
    "fastbot-thirdpart.jar",
]
DEVICE_JAR_TARGET = "/sdcard/"
DEVICE_LIBS_TARGET = "/data/local/tmp/"

CRASH_PATTERN = re.compile(r"(FATAL EXCEPTION|ANR in)", re.IGNORECASE)
ANR_PATTERN = re.compile(r"ANR in", re.IGNORECASE)
PROC_LINE_PATTERN = re.compile(r"Process:\s*(\S+)")
ANR_PKG_PATTERN = re.compile(r"ANR in\s+(\S+)")


async def _adb_shell(device_serial: str, cmd: str) -> str:
    proc = await asyncio.create_subprocess_shell(
        f"adb -s {device_serial} shell {cmd}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return stdout.decode(errors="ignore").strip()


async def _adb_push(device_serial: str, local: str, remote: str):
    proc = await asyncio.create_subprocess_shell(
        f"adb -s {device_serial} push \"{local}\" \"{remote}\"",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()


async def _check_remote_file(device_serial: str, remote_path: str) -> bool:
    """检查设备上文件是否存在"""
    result = await _adb_shell(device_serial, f"ls {remote_path} 2>/dev/null")
    return bool(result and "No such file" not in result)


async def push_fastbot_assets(device_serial: str):
    """将 Fastbot 所需的 jar/so 推送至手机（已存在则跳过）"""
    marker = f"{DEVICE_JAR_TARGET}{DEVICE_JARS[0]}"
    if await _check_remote_file(device_serial, marker):
        logger.info(f"设备 {device_serial} 已部署 Fastbot 资源，跳过推送")
        return

    logger.info(f"首次部署 Fastbot 资源到设备 {device_serial}")
    tasks = []
    for jar_name in DEVICE_JARS:
        local_path = os.path.join(FASTBOT_ASSETS_DIR, jar_name)
        if os.path.exists(local_path):
            tasks.append(_adb_push(device_serial, local_path, DEVICE_JAR_TARGET))
            logger.info(f"推送 {jar_name} -> {DEVICE_JAR_TARGET}")
        else:
            logger.warning(f"Fastbot 资源缺失: {local_path}")

    libs_dir = os.path.join(FASTBOT_ASSETS_DIR, "libs")
    if os.path.isdir(libs_dir):
        tasks.append(_adb_push(device_serial, libs_dir, DEVICE_LIBS_TARGET))
        logger.info(f"推送 libs/ -> {DEVICE_LIBS_TARGET}")

    if tasks:
        await asyncio.gather(*tasks)
        logger.info(f"设备 {device_serial} Fastbot 资源部署完成")


def _build_monkey_command(
    package_name: str,
    duration: int,
    throttle: int,
    ignore_crashes: bool,
    enable_custom_event_weights: bool = False,
    pct_touch: int = 40,
    pct_motion: int = 30,
    pct_syskeys: int = 5,
    pct_majornav: int = 15,
) -> str:
    """拼接 Fastbot Monkey 命令"""
    classpath_parts = [f"{DEVICE_JAR_TARGET}{j}" for j in DEVICE_JARS]
    classpath = ":".join(classpath_parts)

    cmd = (
        f"CLASSPATH={classpath} "
        f"exec app_process /system/bin "
        f"com.android.commands.monkey.Monkey "
        f"-p {package_name} "
        f"--throttle {throttle} "
        f"--running-minutes {duration // 60 or 1} "
        f"-v -v "
    )

    if ignore_crashes:
        cmd += "--ignore-crashes --ignore-timeouts --ignore-security-exceptions "

    if enable_custom_event_weights:
        cmd += f"--pct-touch {pct_touch} "
        cmd += f"--pct-motion {pct_motion} "
        cmd += f"--pct-syskeys {pct_syskeys} "
        cmd += f"--pct-majornav {pct_majornav} "
        remainder = max(0, 100 - pct_touch - pct_motion - pct_syskeys - pct_majornav)
        if remainder > 0:
            cmd += f"--pct-anyevent {remainder} "

    cmd += "999999"
    return cmd


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
                f"dumpsys cpuinfo | grep {package_name} | head -1"
            )
            mem_info = await _adb_shell(
                device_serial,
                f"dumpsys meminfo {package_name} | grep 'TOTAL PSS' | head -1"
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
                    crash_events.append({
                        "time": now.strftime("%H:%M:%S"),
                        "type": "ANR",
                        "full_log": full_log,
                    })
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
                            crash_events.append({
                                "time": crash_timestamp,
                                "type": "CRASH",
                                "full_log": full_log,
                            })
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
    stdout, _ = await proc.communicate()
    return stdout.decode(errors="ignore")


async def run_fastbot_task(
    device_serial: str,
    package_name: str,
    duration: int,
    throttle: int,
    ignore_crashes: bool,
    capture_log: bool,
    enable_custom_event_weights: bool = False,
    pct_touch: int = 40,
    pct_motion: int = 30,
    pct_syskeys: int = 5,
    pct_majornav: int = 15,
) -> Dict:
    """
    主执行函数：启动 Monkey 主进程 + 性能/崩溃监控子协程。
    
    返回 {performance_data, crash_events, summary}
    """
    await push_fastbot_assets(device_serial)

    monkey_cmd = _build_monkey_command(
        package_name, duration, throttle, ignore_crashes,
        enable_custom_event_weights, pct_touch, pct_motion, pct_syskeys, pct_majornav,
    )

    perf_data: List[Dict] = []
    crash_events: List[Dict] = []
    stop_event = asyncio.Event()
    abort_event = asyncio.Event()
    should_abort = not ignore_crashes

    await _adb_shell(device_serial, "logcat -c")
    logger.info("已清空 logcat 缓冲区")

    perf_task = asyncio.create_task(
        _monitor_performance(device_serial, package_name, stop_event, perf_data)
    )
    logcat_task = asyncio.create_task(
        _monitor_logcat(
            device_serial, package_name, stop_event, crash_events, capture_log,
            abort_on_crash=should_abort, abort_event=abort_event,
        )
    )

    try:
        monkey_proc = await asyncio.create_subprocess_shell(
            f"adb -s {device_serial} shell \"{monkey_cmd}\"",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        monkey_comm = asyncio.create_task(monkey_proc.communicate())
        abort_wait = asyncio.create_task(abort_event.wait())

        done, pending = await asyncio.wait(
            {monkey_comm, abort_wait},
            timeout=duration + 60,
            return_when=asyncio.FIRST_COMPLETED,
        )

        if abort_wait in done:
            logger.warning("检测到崩溃且容错策略为立即停止，正在终止 Monkey 进程")
            monkey_proc.terminate()
            try:
                await asyncio.wait_for(monkey_proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                monkey_proc.kill()
            monkey_comm.cancel()
        elif monkey_comm not in done:
            monkey_proc.terminate()
            try:
                await monkey_proc.wait()
            except Exception:
                pass
            logger.warning("Monkey 进程超时，已强制终止")
            monkey_comm.cancel()

        for t in pending:
            t.cancel()

    finally:
        stop_event.set()
        await asyncio.gather(perf_task, logcat_task, return_exceptions=True)

    summary = _compute_summary(perf_data, crash_events)

    return {
        "performance_data": perf_data,
        "crash_events": crash_events,
        "summary": summary,
    }


def _compute_summary(perf_data: List[Dict], crash_events: List[Dict]) -> Dict:
    """汇总性能与异常统计"""
    if not perf_data:
        return {
            "avg_cpu": 0, "max_cpu": 0,
            "avg_mem": 0, "max_mem": 0,
            "total_crashes": 0, "total_anrs": 0,
        }

    cpus = [p["cpu"] for p in perf_data]
    mems = [p["mem"] for p in perf_data]
    crashes = sum(1 for e in crash_events if e["type"] == "CRASH")
    anrs = sum(1 for e in crash_events if e["type"] == "ANR")

    return {
        "avg_cpu": round(sum(cpus) / len(cpus), 1),
        "max_cpu": round(max(cpus), 1),
        "avg_mem": round(sum(mems) / len(mems), 1),
        "max_mem": round(max(mems), 1),
        "total_crashes": crashes,
        "total_anrs": anrs,
    }
