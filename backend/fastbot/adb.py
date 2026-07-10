"""ADB 协程工具：shell 执行、文件推送/拉取、进程终止等基础能力。"""
import asyncio
from typing import Dict, Optional

ADB_MONITOR_COMMAND_TIMEOUT_SECONDS = 8


async def _adb_shell(device_serial: str, cmd: str, timeout: Optional[float] = None) -> str:
    result = await _adb_shell_result(device_serial, cmd, timeout=timeout)
    return result["stdout"]


async def _terminate_subprocess(proc) -> None:
    if proc.returncode is not None:
        return
    try:
        proc.terminate()
        await asyncio.wait_for(proc.wait(), timeout=1)
        return
    except Exception:
        pass

    try:
        proc.kill()
        await asyncio.wait_for(proc.wait(), timeout=1)
    except Exception:
        pass


async def _adb_shell_result(
    device_serial: str,
    cmd: str,
    timeout: Optional[float] = None,
) -> Dict[str, object]:
    proc = await asyncio.create_subprocess_shell(
        f"adb -s {device_serial} shell {cmd}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        if timeout is None:
            stdout, stderr = await proc.communicate()
        else:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        await _terminate_subprocess(proc)
        return {
            "stdout": "",
            "stderr": f"adb shell timeout after {timeout}s: {cmd}",
            "returncode": -1,
        }
    return {
        "stdout": stdout.decode(errors="ignore").strip(),
        "stderr": stderr.decode(errors="ignore").strip(),
        "returncode": proc.returncode,
    }


async def _adb_push(device_serial: str, local: str, remote: str):
    proc = await asyncio.create_subprocess_shell(
        f"adb -s {device_serial} push \"{local}\" \"{remote}\"",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()


async def _adb_pull(device_serial: str, remote: str, local: str):
    proc = await asyncio.create_subprocess_shell(
        f"adb -s {device_serial} pull \"{remote}\" \"{local}\"",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        message = stderr.decode(errors="ignore").strip() or stdout.decode(errors="ignore").strip()
        raise RuntimeError(message or f"adb pull failed: {remote}")


async def _check_remote_file(device_serial: str, remote_path: str) -> bool:
    """检查设备上文件是否存在"""
    result = await _adb_shell(
        device_serial,
        f"ls {remote_path} 2>/dev/null",
        timeout=ADB_MONITOR_COMMAND_TIMEOUT_SECONDS,
    )
    return bool(result and "No such file" not in result)


async def _get_device_sdk_int(device_serial: str) -> int:
    sdk_output = await _adb_shell(
        device_serial,
        "getprop ro.build.version.sdk",
        timeout=ADB_MONITOR_COMMAND_TIMEOUT_SECONDS,
    )
    return int(sdk_output.strip()) if sdk_output.strip().isdigit() else 0
