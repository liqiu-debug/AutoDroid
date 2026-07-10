"""Fastbot/Monkey：资源部署与 Monkey 命令拼接。"""
import os
import asyncio
import logging

from backend.paths import project_path

from backend.fastbot.adb import _adb_push, _check_remote_file

logger = logging.getLogger("FastbotRunner")

FASTBOT_ASSETS_DIR = str(project_path("resources", "fastbot"))

DEVICE_JARS = [
    "framework.jar",
    "monkeyq.jar",
    "fastbot-thirdpart.jar",
]
DEVICE_JAR_TARGET = "/sdcard/"
DEVICE_LIBS_TARGET = "/data/local/tmp/"


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
