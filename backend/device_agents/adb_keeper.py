"""AdbKeeper - 维护本机 adb server 与远程设备隧道端口的连接状态。

职责：
- 隧道就绪（Agent 在线且设备已开监听）→ ``adb connect 127.0.0.1:<port>``（幂等）
- ``adb devices`` 中隧道设备状态为 offline → 先 disconnect 再 connect 修复
- 隧道不再存在（Agent 失联/设备拔出）→ ``adb disconnect`` 清理条目
- 周期兜底巡检 + Hub 事件触发即时同步

只管理本模块端口范围（28100-28199）内的 127.0.0.1 条目，不触碰其他设备。
"""
import asyncio
import logging
from typing import Callable, Dict, Optional, Set

from backend.device_agents.protocol import parse_tunnel_serial, tunnel_serial

logger = logging.getLogger(__name__)

# 周期兜底巡检间隔（秒）
SYNC_INTERVAL_SECONDS = 30
ADB_COMMAND_TIMEOUT_SECONDS = 10

# 这些 adb 状态不做干预：unauthorized 需要用户在手机上确认 RSA 授权，
# authorizing/connecting 是瞬态，重连只会打断握手。
_LEAVE_ALONE_STATES = {"unauthorized", "authorizing", "connecting"}


async def _run_adb_command(*args: str, timeout: int = ADB_COMMAND_TIMEOUT_SECONDS) -> str:
    """异步执行 adb 命令，返回 stdout 文本；失败抛 RuntimeError。"""
    cmd = ["adb"] + list(args)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        raise RuntimeError(f"adb command timed out: {' '.join(cmd)}")
    if proc.returncode != 0:
        err_msg = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"adb error (rc={proc.returncode}): {err_msg}")
    return stdout.decode("utf-8", errors="replace")


def parse_adb_devices_tunnel_states(output: str) -> Dict[int, str]:
    """从 ``adb devices`` 输出提取隧道端口范围内条目：port -> 状态。"""
    states: Dict[int, str] = {}
    for line in str(output or "").strip().splitlines()[1:]:
        parts = line.strip().split()
        if len(parts) < 2:
            continue
        port = parse_tunnel_serial(parts[0])
        if port is not None:
            states[port] = parts[1]
    return states


class AdbKeeper:
    """后台协程：把 adb server 的隧道连接收敛到期望状态。"""

    def __init__(self):
        self._desired_provider: Optional[Callable[[], Set[int]]] = None
        self._wake: Optional[asyncio.Event] = None
        self._task: Optional[asyncio.Task] = None
        self._running = False

    def configure(self, desired_provider: Callable[[], Set[int]]) -> None:
        self._desired_provider = desired_provider

    def start_in_loop(self) -> None:
        """在当前事件循环中启动巡检任务（FastAPI startup 中调用）。"""
        if self._running:
            return
        loop = asyncio.get_running_loop()
        self._running = True
        self._wake = asyncio.Event()
        self._wake.set()  # 启动后立即执行一次（清理重启前的陈旧条目）
        self._task = loop.create_task(self._loop(), name="device-agent-adb-keeper")
        logger.info("adb keeper 已启动")

    def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            self._task = None

    def request_sync(self) -> None:
        """请求尽快执行一次同步（Hub 在设备/会话变化时调用）。"""
        if self._wake is not None:
            self._wake.set()

    async def _loop(self) -> None:
        while self._running:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=SYNC_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass
            if not self._running:
                return
            if self._wake is not None:
                self._wake.clear()
            try:
                await self._sync_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("adb keeper 同步失败，等待下一轮")

    async def _sync_once(self) -> None:
        desired: Set[int] = set()
        if self._desired_provider is not None:
            try:
                desired = set(self._desired_provider() or set())
            except Exception:
                logger.exception("获取期望隧道端口失败")

        try:
            output = await _run_adb_command("devices")
        except Exception as exc:
            logger.warning("adb devices 执行失败，跳过本轮同步: %s", exc)
            return
        states = parse_adb_devices_tunnel_states(output)

        # 清理不再期望的隧道条目
        for port, state in states.items():
            if port in desired:
                continue
            await self._disconnect(port, reason=f"tunnel gone (state={state})")

        # 建立/修复期望的隧道条目
        for port in sorted(desired):
            state = states.get(port)
            if state == "device":
                continue
            if state in _LEAVE_ALONE_STATES:
                logger.info(
                    "隧道设备 %s 状态 %s，等待授权/握手完成", tunnel_serial(port), state
                )
                continue
            if state is not None:
                # offline 等异常状态：先断开再重连
                await self._disconnect(port, reason=f"repair (state={state})")
            await self._connect(port)

    async def _connect(self, port: int) -> None:
        serial = tunnel_serial(port)
        try:
            output = await _run_adb_command("connect", serial)
            text = output.strip()
            if "connected" in text:
                logger.info("adb connect %s: %s", serial, text)
            else:
                logger.warning("adb connect %s 结果异常: %s", serial, text)
        except Exception as exc:
            logger.warning("adb connect %s 失败: %s", serial, exc)

    async def _disconnect(self, port: int, *, reason: str) -> None:
        serial = tunnel_serial(port)
        try:
            await _run_adb_command("disconnect", serial)
            logger.info("adb disconnect %s (%s)", serial, reason)
        except Exception as exc:
            logger.debug("adb disconnect %s 失败（忽略）: %s", serial, exc)


# 全局单例
adb_keeper = AdbKeeper()
