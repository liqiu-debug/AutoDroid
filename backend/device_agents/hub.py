"""TunnelHub - 远程设备接入点（Agent）会话与反向隧道管理。

拓扑（B→A 单向可达）：

    A 的 adb server ⇄ 127.0.0.1:<tunnel_port>（本模块 asyncio TCP 中继）
      ⇄ WebSocket（Agent 从 B 主动外连 A）
      ⇄ B 上 Agent ⇄ 127.0.0.1:<forward端口>（B 的 adb forward）⇄ USB ⇄ 手机 adbd

每个 Agent 一条 WebSocket：控制消息走 JSON text 帧，隧道数据走 binary 帧
（4 字节大端 conn_id + payload）。设备的隧道端口按 (agent, usb_serial)
持久化在 RemoteAgentDevice 表，保证 adb serial（127.0.0.1:<port>）跨重启稳定。
"""
import asyncio
import json
import logging
import socket
import time
from collections import deque
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

from sqlmodel import Session, select
from starlette.websockets import WebSocket, WebSocketDisconnect

from backend.database import engine
from backend.models import RemoteAgent, RemoteAgentDevice
from backend.device_agents.protocol import (
    AGENT_PROTOCOL_VERSION,
    MAX_DATA_PAYLOAD,
    TUNNEL_HOST,
    TUNNEL_PORT_RANGE_END,
    TUNNEL_PORT_RANGE_START,
    decode_data_frame,
    encode_data_frame,
    tunnel_serial,
)

logger = logging.getLogger(__name__)

# Agent 未发任何消息（含应用层 ping）超过该秒数即判定失联
# （Agent ping 间隔 20s，45s = 容忍丢 1 次 ping；失联越早判定，
#  keeper 越早清理陈旧 adb 条目、新会话越早接管）
SESSION_IDLE_TIMEOUT_SECONDS = 45
# 等待首条 register 消息的超时
REGISTER_TIMEOUT_SECONDS = 15
# RemoteAgent.last_seen_at 落库节流间隔
LAST_SEEN_WRITE_INTERVAL_SECONDS = 30
# 服务端 TCP 读块大小（单个 binary 帧 payload 上限之内）
TCP_READ_CHUNK = 64 * 1024
# 单次 WebSocket 发送上限。8KB 足以让小控制消息在一帧视频之间插队。
TUNNEL_SEND_CHUNK_BYTES = 8 * 1024
# 每条 ADB TCP 连接最多积压 256KB；达到上限后暂停读取该连接并让 TCP 反压上游。
TUNNEL_CONNECTION_MAX_PENDING_BYTES = 256 * 1024
# Agent → 平台侧写入同样有界，避免慢视频连接拖住 WebSocket receive loop。
TUNNEL_CONNECTION_MAX_INBOUND_CHUNKS = 32

# 带宽探测：Agent 回填的最大数据量与等待时长
BANDWIDTH_PROBE_DEFAULT_BYTES = 1_000_000
BANDWIDTH_PROBE_TIMEOUT_SECONDS = 20.0


class _LinkRateWindow:
    """滚动窗口字节速率统计（按秒分桶）。

    record() 只在事件循环内调用；rate_bps() 可能被 REST 线程读取，
    因此读取路径不做淘汰、仅按时间过滤快照，避免跨线程修改 deque。
    """

    def __init__(self, window_seconds: int = 10):
        self._window = window_seconds
        self._buckets: Deque[Tuple[int, int]] = deque()

    def record(self, nbytes: int) -> None:
        now_s = int(time.time())
        cutoff = now_s - self._window
        while self._buckets and self._buckets[0][0] <= cutoff:
            self._buckets.popleft()
        if self._buckets and self._buckets[-1][0] == now_s:
            second, value = self._buckets[-1]
            self._buckets[-1] = (second, value + nbytes)
        else:
            self._buckets.append((now_s, nbytes))

    def rate_bps(self) -> int:
        cutoff = int(time.time()) - self._window
        window_bytes = sum(value for second, value in list(self._buckets) if second > cutoff)
        return int(window_bytes * 8 / self._window)


class TunnelConnection:
    """一条经隧道中继的 TCP 连接（A 的 adb server → 本机监听端口）。"""

    def __init__(self, conn_id: int, usb_serial: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.conn_id = conn_id
        self.usb_serial = usb_serial
        self.reader = reader
        self.writer = writer
        self.pump_task: Optional[asyncio.Task] = None
        self.writer_task: Optional[asyncio.Task] = None
        self.write_queue: "asyncio.Queue[bytes]" = asyncio.Queue(
            maxsize=TUNNEL_CONNECTION_MAX_INBOUND_CHUNKS
        )
        self.closed = False


class RemoteDeviceRuntime:
    """在线 Agent 上一台设备的运行时状态。"""

    def __init__(self, usb_serial: str, tunnel_port: int, meta: Dict[str, Any]):
        self.usb_serial = usb_serial
        self.tunnel_port = tunnel_port
        self.meta = dict(meta or {})
        self.listener: Optional[asyncio.base_events.Server] = None
        self.error: Optional[str] = None

    @property
    def ready(self) -> bool:
        return self.listener is not None and self.error is None


class AgentSession:
    """一个在线 Agent（一条 WebSocket）的运行时状态。"""

    def __init__(self, hub: "TunnelHub", websocket: WebSocket, name: str, agent_id: int):
        self.hub = hub
        self.websocket = websocket
        self.name = name
        self.agent_id = agent_id
        self.devices: Dict[str, RemoteDeviceRuntime] = {}
        self.conns: Dict[int, TunnelConnection] = {}
        self.closed = False
        self._next_conn_id = 1
        self._last_seen_written = 0.0
        self._send_condition = asyncio.Condition()
        self._control_queue: Deque[Tuple[str, asyncio.Future]] = deque()
        self._data_queues: Dict[int, Deque[bytes]] = {}
        self._data_pending_bytes: Dict[int, int] = {}
        self._round_robin_connections: Deque[int] = deque()
        self._closed_connections: Set[int] = set()
        self._sender_task: Optional[asyncio.Task] = None
        # 链路质量观测：RTT 由 Agent 随 ping 捎带上报；吞吐在数据转发处累计
        self.link_rtt_ms: Optional[float] = None
        self.link_rtt_samples: Deque[float] = deque(maxlen=20)
        self.inbound_rate = _LinkRateWindow()   # B→A（视频/截图/命令响应）
        self.outbound_rate = _LinkRateWindow()  # A→B（adb 命令/触控）
        self.bandwidth_probe: Optional[Dict[str, Any]] = None
        self._probe_futures: Dict[int, asyncio.Future] = {}
        self._next_probe_id = 1

    def allocate_conn_id(self) -> int:
        conn_id = self._next_conn_id
        self._next_conn_id += 1
        return conn_id

    async def send_control(self, message: Dict[str, Any]) -> None:
        """控制消息优先，且仅在真正写入 WebSocket 后返回。"""
        payload = json.dumps(message, ensure_ascii=False)
        future = asyncio.get_running_loop().create_future()
        async with self._send_condition:
            if self.closed:
                raise ConnectionError("Agent 会话已关闭")
            self._ensure_sender_locked()
            self._control_queue.append((payload, future))
            self._send_condition.notify_all()
        await future

    async def send_data(self, conn_id: int, payload: bytes) -> None:
        """按连接有界排队；轮询发送防止视频流长期独占 WebSocket。"""
        if not payload:
            return
        async with self._send_condition:
            self._ensure_sender_locked()
            while (
                not self.closed
                and conn_id not in self._closed_connections
                and self._data_pending_bytes.get(conn_id, 0) + len(payload)
                > TUNNEL_CONNECTION_MAX_PENDING_BYTES
            ):
                await self._send_condition.wait()
            if self.closed or conn_id in self._closed_connections:
                raise ConnectionError("Agent 隧道连接已关闭")
            queue_for_connection = self._data_queues.setdefault(conn_id, deque())
            was_empty = not queue_for_connection
            queue_for_connection.append(bytes(payload))
            self._data_pending_bytes[conn_id] = self._data_pending_bytes.get(conn_id, 0) + len(payload)
            if was_empty:
                self._round_robin_connections.append(conn_id)
            self._send_condition.notify_all()

    async def discard_connection_data(self, conn_id: int) -> None:
        """连接关闭时丢弃尚未发出的完整 TCP 数据，唤醒被反压的读取协程。"""
        async with self._send_condition:
            self._closed_connections.add(conn_id)
            self._data_queues.pop(conn_id, None)
            self._data_pending_bytes.pop(conn_id, None)
            self._round_robin_connections = deque(
                item for item in self._round_robin_connections if item != conn_id
            )
            self._send_condition.notify_all()

    async def shutdown_sender(self) -> None:
        """结束发送任务，并让所有等待控制发送/背压的协程立即退出。"""
        async with self._send_condition:
            self.closed = True
            error = ConnectionError("Agent 会话已关闭")
            while self._control_queue:
                _, future = self._control_queue.popleft()
                if not future.done():
                    future.set_exception(error)
            self._data_queues.clear()
            self._data_pending_bytes.clear()
            self._round_robin_connections.clear()
            self._send_condition.notify_all()
            sender_task = self._sender_task
            self._sender_task = None
        if sender_task is not None and sender_task is not asyncio.current_task():
            sender_task.cancel()
            try:
                await sender_task
            except asyncio.CancelledError:
                pass

    def _ensure_sender_locked(self) -> None:
        if self._sender_task is None or self._sender_task.done():
            self._sender_task = asyncio.create_task(
                self._sender_loop(), name=f"AgentTunnelSender-{self.name}"
            )

    async def _sender_loop(self) -> None:
        try:
            while True:
                control_future: Optional[asyncio.Future] = None
                text_payload: Optional[str] = None
                binary_payload: Optional[bytes] = None
                async with self._send_condition:
                    while (
                        not self.closed
                        and not self._control_queue
                        and not self._round_robin_connections
                    ):
                        await self._send_condition.wait()
                    if self.closed:
                        return

                    if self._control_queue:
                        text_payload, control_future = self._control_queue.popleft()
                    else:
                        conn_id = self._round_robin_connections.popleft()
                        queue_for_connection = self._data_queues.get(conn_id)
                        if not queue_for_connection:
                            continue
                        pending = queue_for_connection.popleft()
                        chunk = pending[:TUNNEL_SEND_CHUNK_BYTES]
                        remainder = pending[TUNNEL_SEND_CHUNK_BYTES:]
                        if remainder:
                            queue_for_connection.appendleft(remainder)
                        if queue_for_connection:
                            self._round_robin_connections.append(conn_id)
                        else:
                            self._data_queues.pop(conn_id, None)
                        self._data_pending_bytes[conn_id] = max(
                            0, self._data_pending_bytes.get(conn_id, 0) - len(chunk)
                        )
                        if self._data_pending_bytes.get(conn_id) == 0:
                            self._data_pending_bytes.pop(conn_id, None)
                        self._send_condition.notify_all()
                        binary_payload = encode_data_frame(conn_id, chunk)

                try:
                    if text_payload is not None:
                        await self.websocket.send_text(text_payload)
                        self.outbound_rate.record(len(text_payload))
                    elif binary_payload is not None:
                        await self.websocket.send_bytes(binary_payload)
                        self.outbound_rate.record(len(binary_payload))
                    if control_future is not None and not control_future.done():
                        control_future.set_result(None)
                except Exception as exc:
                    if control_future is not None and not control_future.done():
                        control_future.set_exception(exc)
                    logger.info("Agent 隧道发送失败: name=%s error=%s", self.name, exc)
                    await self.shutdown_sender()
                    try:
                        await self.websocket.close()
                    except Exception:
                        pass
                    return
        except asyncio.CancelledError:
            raise


class TunnelHub:
    """Agent 会话与隧道监听的全局管理器（单例 ``tunnel_hub``）。"""

    def __init__(self):
        self._sessions: Dict[str, AgentSession] = {}
        self._lock = asyncio.Lock()
        self._keeper = None  # backend.device_agents.adb_keeper.AdbKeeper

    # ==================== 生命周期 ====================

    def set_keeper(self, keeper) -> None:
        self._keeper = keeper

    def startup_reset(self) -> None:
        """进程启动时把库里所有接入点标记为 OFFLINE（在线状态以活跃会话为准）。"""
        try:
            with Session(engine) as session:
                agents = session.exec(select(RemoteAgent)).all()
                now = datetime.now()
                for agent in agents:
                    if agent.status != "OFFLINE":
                        agent.status = "OFFLINE"
                        agent.updated_at = now
                        session.add(agent)
                session.commit()
        except Exception:
            logger.exception("重置远程接入点状态失败")

    async def shutdown(self) -> None:
        async with self._lock:
            sessions = list(self._sessions.values())
        for agent_session in sessions:
            await self._teardown_session(agent_session, reason="server shutdown")

    # ==================== 对外查询 ====================

    def desired_ports(self) -> Set[int]:
        """当前应保持 adb connect 的隧道端口集合（在线 Agent 的就绪设备）。"""
        ports: Set[int] = set()
        for agent_session in self._sessions.values():
            for device in agent_session.devices.values():
                if device.ready:
                    ports.add(device.tunnel_port)
        return ports

    def get_remote_device_meta(self, serial: str) -> Optional[Dict[str, Any]]:
        """按 adb serial（127.0.0.1:<port>）查远程设备元数据；非隧道形态返回 None。

        优先取在线会话的运行时信息；离线时回落数据库映射（供展示历史归属）。
        """
        from backend.device_agents.protocol import parse_tunnel_serial

        port = parse_tunnel_serial(serial)
        if port is None:
            return None
        for agent_session in self._sessions.values():
            for device in agent_session.devices.values():
                if device.tunnel_port == port:
                    return {
                        "agent_name": agent_session.name,
                        "usb_serial": device.usb_serial,
                        "online": device.ready,
                    }
        try:
            with Session(engine) as session:
                mapping = session.exec(
                    select(RemoteAgentDevice).where(RemoteAgentDevice.tunnel_port == port)
                ).first()
                if not mapping:
                    return None
                agent = session.get(RemoteAgent, mapping.agent_id)
                return {
                    "agent_name": agent.name if agent else None,
                    "usb_serial": mapping.usb_serial,
                    "online": False,
                }
        except Exception:
            logger.exception("查询远程设备映射失败: serial=%s", serial)
            return None

    def list_agents(self) -> List[Dict[str, Any]]:
        """接入点列表（数据库记录 + 在线会话叠加），供 REST 展示。"""
        result: List[Dict[str, Any]] = []
        with Session(engine) as session:
            agents = session.exec(select(RemoteAgent)).all()
            for agent in agents:
                live = self._sessions.get(agent.name)
                devices: List[Dict[str, Any]] = []
                mappings = session.exec(
                    select(RemoteAgentDevice).where(RemoteAgentDevice.agent_id == agent.id)
                ).all()
                live_serials = set(live.devices.keys()) if live else set()
                for mapping in mappings:
                    runtime = live.devices.get(mapping.usb_serial) if live else None
                    devices.append(
                        {
                            "usb_serial": mapping.usb_serial,
                            "tunnel_port": mapping.tunnel_port,
                            "serial": tunnel_serial(mapping.tunnel_port),
                            "model": mapping.model,
                            "brand": mapping.brand,
                            "os_version": mapping.os_version,
                            "online": bool(runtime and runtime.ready),
                            "error": runtime.error if runtime else None,
                            "last_seen_at": mapping.last_seen_at.isoformat() if mapping.last_seen_at else None,
                        }
                    )
                # 在线但尚未落映射的设备（极短暂窗口），防御性补充
                if live:
                    known = {d["usb_serial"] for d in devices}
                    for usb_serial in live_serials - known:
                        runtime = live.devices[usb_serial]
                        devices.append(
                            {
                                "usb_serial": usb_serial,
                                "tunnel_port": runtime.tunnel_port,
                                "serial": tunnel_serial(runtime.tunnel_port),
                                "model": runtime.meta.get("model"),
                                "brand": runtime.meta.get("brand"),
                                "os_version": runtime.meta.get("os_version"),
                                "online": runtime.ready,
                                "error": runtime.error,
                                "last_seen_at": None,
                            }
                        )
                result.append(
                    {
                        "id": agent.id,
                        "name": agent.name,
                        "status": "ONLINE" if live else "OFFLINE",
                        "agent_version": agent.agent_version,
                        "os_info": agent.os_info,
                        "last_seen_at": agent.last_seen_at.isoformat() if agent.last_seen_at else None,
                        "created_at": agent.created_at.isoformat() if agent.created_at else None,
                        "device_count": len(devices),
                        "online_device_count": sum(1 for d in devices if d["online"]),
                        "devices": devices,
                        "link_quality": self._link_quality_snapshot(live),
                    }
                )
        return result

    def is_agent_online(self, name: str) -> bool:
        return name in self._sessions

    async def probe_agent_bandwidth(
        self,
        agent_id: int,
        size_bytes: int = BANDWIDTH_PROBE_DEFAULT_BYTES,
        timeout: float = BANDWIDTH_PROBE_TIMEOUT_SECONDS,
    ) -> Dict[str, Any]:
        """主动测量 B→A 上行吞吐：让 Agent 回填一块数据并计时。

        结果含约一个 RTT 的固定开销（按最近上报 RTT 扣除）；探测数据与
        视频流共享 WebSocket，探测期间会短暂挤占视频属预期——测的就是
        真实可用容量。旧版 Agent（<1.2.0）不识别该消息，等待超时后报错。
        """
        async with self._lock:
            agent_session = next(
                (s for s in self._sessions.values() if s.agent_id == agent_id), None
            )
        if agent_session is None or agent_session.closed:
            raise ValueError("接入点不在线，无法探测")

        probe_id = agent_session._next_probe_id
        agent_session._next_probe_id += 1
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        agent_session._probe_futures[probe_id] = future
        started = time.monotonic()
        await agent_session.send_control(
            {"type": "bandwidth_probe", "probe_id": probe_id, "size": int(size_bytes)}
        )
        try:
            payload_bytes = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            agent_session._probe_futures.pop(probe_id, None)
            raise TimeoutError(
                "Agent 未在时限内回传探测数据：请确认 Agent 已升级到 1.2.0+，"
                "若已升级则说明链路吞吐极低"
            )
        elapsed = max(time.monotonic() - started, 1e-6)
        rtt_seconds = (agent_session.link_rtt_ms or 0.0) / 1000.0
        transfer_seconds = max(elapsed - rtt_seconds, 1e-6)
        result = {
            "bps": int(payload_bytes * 8 / transfer_seconds),
            "bytes": int(payload_bytes),
            "elapsed_ms": int(elapsed * 1000),
            "rtt_ms_at_probe": agent_session.link_rtt_ms,
            "at": datetime.now().isoformat(timespec="seconds"),
        }
        agent_session.bandwidth_probe = result
        return result

    @staticmethod
    def _link_quality_snapshot(agent_session: Optional["AgentSession"]) -> Optional[Dict[str, Any]]:
        if agent_session is None or agent_session.closed:
            return None
        samples = list(agent_session.link_rtt_samples)
        return {
            "rtt_ms": agent_session.link_rtt_ms,
            "rtt_avg_ms": round(sum(samples) / len(samples), 1) if samples else None,
            "up_bps": agent_session.inbound_rate.rate_bps(),
            "down_bps": agent_session.outbound_rate.rate_bps(),
            "bandwidth_probe": agent_session.bandwidth_probe,
        }

    # ==================== WS 会话主流程 ====================

    async def handle_session(self, websocket: WebSocket, token_user: str) -> None:
        """处理一条已通过鉴权的 Agent WebSocket，直到断开。"""
        register = await self._wait_register(websocket)
        if register is None:
            return

        agent_info = register.get("agent") or {}
        name = str(agent_info.get("name") or "").strip()
        if not name or len(name) > 64:
            await self._reject(websocket, "register.agent.name 缺失或过长（≤64 字符）")
            return

        protocol = int(register.get("protocol") or 0)
        if protocol != AGENT_PROTOCOL_VERSION:
            await self._reject(
                websocket,
                f"协议版本不匹配：Agent={protocol} 服务端={AGENT_PROTOCOL_VERSION}，请重新下载 Agent 脚本",
            )
            return

        async with self._lock:
            stale = self._sessions.get(name)
        if stale is not None:
            logger.info("接入点 %s 重复注册，替换旧会话", name)
            await self._teardown_session(stale, reason="replaced by new session")

        agent_id = self._persist_agent_online(name, agent_info, token_user)
        agent_session = AgentSession(self, websocket, name, agent_id)
        async with self._lock:
            self._sessions[name] = agent_session

        logger.info(
            "远程接入点上线: name=%s version=%s os=%s user=%s",
            name,
            agent_info.get("version"),
            agent_info.get("os"),
            token_user,
        )

        try:
            accepted, rejected = await self._apply_device_list(
                agent_session, register.get("devices") or []
            )
            await agent_session.send_control(
                {
                    "type": "registered",
                    "protocol": AGENT_PROTOCOL_VERSION,
                    "devices": accepted,
                    "rejected": rejected,
                }
            )
            self._notify_keeper()
            await self._receive_loop(agent_session)
        except WebSocketDisconnect:
            pass
        except Exception:
            logger.exception("接入点 %s 会话异常", name)
        finally:
            await self._teardown_session(agent_session, reason="session ended")

    async def _wait_register(self, websocket: WebSocket) -> Optional[Dict[str, Any]]:
        try:
            message = await asyncio.wait_for(
                websocket.receive(), timeout=REGISTER_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            await self._reject(websocket, "等待 register 消息超时")
            return None
        except WebSocketDisconnect:
            return None

        if message.get("type") == "websocket.disconnect":
            return None
        text = message.get("text")
        if not text:
            await self._reject(websocket, "首条消息必须是 JSON register")
            return None
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            await self._reject(websocket, "register 消息不是合法 JSON")
            return None
        if payload.get("type") != "register":
            await self._reject(websocket, "首条消息必须是 register")
            return None
        return payload

    async def _reject(self, websocket: WebSocket, reason: str) -> None:
        logger.warning("拒绝 Agent 连接: %s", reason)
        try:
            await websocket.send_text(json.dumps({"type": "error", "message": reason}, ensure_ascii=False))
        except Exception:
            pass
        try:
            await websocket.close(code=4400)
        except Exception:
            pass

    async def _receive_loop(self, agent_session: AgentSession) -> None:
        websocket = agent_session.websocket
        while not agent_session.closed:
            try:
                message = await asyncio.wait_for(
                    websocket.receive(), timeout=SESSION_IDLE_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                logger.warning("接入点 %s 超时无消息，判定失联", agent_session.name)
                return

            if message.get("type") == "websocket.disconnect":
                return

            data = message.get("bytes")
            if data is not None:
                await self._on_data_frame(agent_session, data)
                continue

            text = message.get("text")
            if text is None:
                continue
            try:
                control = json.loads(text)
            except json.JSONDecodeError:
                logger.warning("接入点 %s 发来非法控制消息，忽略", agent_session.name)
                continue
            await self._on_control(agent_session, control)

    # ==================== 控制/数据帧处理 ====================

    async def _on_control(self, agent_session: AgentSession, control: Dict[str, Any]) -> None:
        msg_type = str(control.get("type") or "")
        if msg_type == "ping":
            self._touch_last_seen(agent_session)
            # Agent ≥1.2.0 会把上一次 ping→pong 实测 RTT 捎带上来
            rtt_raw = control.get("rtt_ms")
            if isinstance(rtt_raw, (int, float)) and 0 <= float(rtt_raw) < 60_000:
                agent_session.link_rtt_ms = float(rtt_raw)
                agent_session.link_rtt_samples.append(float(rtt_raw))
            try:
                await agent_session.send_control({"type": "pong"})
            except Exception:
                pass
        elif msg_type == "bandwidth_probe_result":
            probe_id = int(control.get("probe_id") or 0)
            future = agent_session._probe_futures.pop(probe_id, None)
            if future is not None and not future.done():
                future.set_result(len(str(control.get("payload") or "")))
        elif msg_type == "devices":
            accepted, rejected = await self._apply_device_list(
                agent_session, control.get("devices") or []
            )
            try:
                await agent_session.send_control(
                    {"type": "registered", "protocol": AGENT_PROTOCOL_VERSION, "devices": accepted, "rejected": rejected}
                )
            except Exception:
                pass
            self._notify_keeper()
        elif msg_type == "open_result":
            conn_id = int(control.get("conn_id") or 0)
            if not control.get("ok"):
                logger.info(
                    "接入点 %s 打开设备连接失败: conn_id=%s error=%s",
                    agent_session.name,
                    conn_id,
                    control.get("error"),
                )
                await self._close_conn(agent_session, conn_id, notify_agent=False)
        elif msg_type == "close":
            conn_id = int(control.get("conn_id") or 0)
            await self._close_conn(agent_session, conn_id, notify_agent=False)
        else:
            logger.debug("接入点 %s 未知控制消息: %s", agent_session.name, msg_type)

    async def _on_data_frame(self, agent_session: AgentSession, frame: bytes) -> None:
        agent_session.inbound_rate.record(len(frame))
        try:
            conn_id, payload = decode_data_frame(frame)
        except ValueError:
            logger.warning("接入点 %s 发来畸形数据帧，忽略", agent_session.name)
            return
        if len(payload) > MAX_DATA_PAYLOAD:
            logger.warning(
                "接入点 %s 数据帧过大(%s)，关闭连接 conn_id=%s",
                agent_session.name,
                len(payload),
                conn_id,
            )
            await self._close_conn(agent_session, conn_id, notify_agent=True)
            return
        conn = agent_session.conns.get(conn_id)
        if conn is None or conn.closed:
            # 连接已关闭：回发 close 让 Agent 清理
            try:
                await agent_session.send_control({"type": "close", "conn_id": conn_id})
            except Exception:
                pass
            return
        try:
            conn.write_queue.put_nowait(payload)
        except asyncio.QueueFull:
            # TCP 字节流不能跳过中间数据；单条连接阻塞时直接关闭它，避免把整个
            # Agent receive loop 一起卡住。Scrcpy 会自动在上层触发重连。
            logger.warning(
                "隧道连接入站队列已满，关闭慢连接: agent=%s conn_id=%s",
                agent_session.name,
                conn_id,
            )
            await self._close_conn(agent_session, conn_id, notify_agent=True)

    # ==================== 设备列表与监听管理 ====================

    async def _apply_device_list(
        self, agent_session: AgentSession, raw_devices: List[Dict[str, Any]]
    ):
        """全量应用 Agent 上报的设备列表：新增开监听、移除关监听、更新元数据。"""
        reported: Dict[str, Dict[str, Any]] = {}
        for item in raw_devices:
            usb_serial = str((item or {}).get("usb_serial") or "").strip()
            if usb_serial:
                reported[usb_serial] = dict(item)

        accepted: List[Dict[str, Any]] = []
        rejected: List[Dict[str, Any]] = []

        async with self._lock:
            port_map, alloc_errors = self._persist_device_mappings(
                agent_session.agent_id, reported
            )

        for usb_serial, error in alloc_errors.items():
            rejected.append({"usb_serial": usb_serial, "error": error})

        # 移除不再上报的设备
        for usb_serial in list(agent_session.devices.keys()):
            if usb_serial not in port_map:
                await self._remove_device_runtime(agent_session, usb_serial)

        # 新增/更新
        for usb_serial, port in port_map.items():
            meta = reported.get(usb_serial) or {}
            runtime = agent_session.devices.get(usb_serial)
            if runtime is None:
                runtime = RemoteDeviceRuntime(usb_serial, port, meta)
                agent_session.devices[usb_serial] = runtime
                await self._start_listener(agent_session, runtime)
            else:
                runtime.meta = dict(meta)
            entry = {"usb_serial": usb_serial, "tunnel_port": port, "serial": tunnel_serial(port)}
            if runtime.error:
                entry["error"] = runtime.error
            accepted.append(entry)

        logger.info(
            "接入点 %s 设备列表更新: %s 台在线, %s 台被拒",
            agent_session.name,
            len(accepted),
            len(rejected),
        )
        return accepted, rejected

    async def _start_listener(self, agent_session: AgentSession, runtime: RemoteDeviceRuntime) -> None:
        port = runtime.tunnel_port

        async def _on_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            await self._handle_tunnel_client(agent_session, runtime, reader, writer)

        try:
            server = await asyncio.start_server(_on_client, TUNNEL_HOST, port)
        except OSError as exc:
            runtime.error = f"隧道端口 {port} 监听失败: {exc}"
            logger.error("%s（agent=%s usb_serial=%s）", runtime.error, agent_session.name, runtime.usb_serial)
            return
        runtime.listener = server
        runtime.error = None
        logger.info(
            "隧道监听已就绪: agent=%s usb_serial=%s -> %s",
            agent_session.name,
            runtime.usb_serial,
            tunnel_serial(port),
        )

    async def _remove_device_runtime(self, agent_session: AgentSession, usb_serial: str) -> None:
        runtime = agent_session.devices.pop(usb_serial, None)
        if runtime is None:
            return
        if runtime.listener is not None:
            runtime.listener.close()
            try:
                await runtime.listener.wait_closed()
            except Exception:
                pass
            runtime.listener = None
        for conn_id, conn in list(agent_session.conns.items()):
            if conn.usb_serial == usb_serial:
                await self._close_conn(agent_session, conn_id, notify_agent=True)
        logger.info(
            "远程设备已移除: agent=%s usb_serial=%s port=%s",
            agent_session.name,
            usb_serial,
            runtime.tunnel_port,
        )

    # ==================== 隧道连接中继 ====================

    async def _handle_tunnel_client(
        self,
        agent_session: AgentSession,
        runtime: RemoteDeviceRuntime,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        if agent_session.closed:
            writer.close()
            return
        # adb 命令多为小包往返，关闭 Nagle 避免每次交互吃合并延迟
        try:
            sock = writer.get_extra_info("socket")
            if sock is not None:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass
        conn_id = agent_session.allocate_conn_id()
        conn = TunnelConnection(conn_id, runtime.usb_serial, reader, writer)
        agent_session.conns[conn_id] = conn
        conn.writer_task = asyncio.create_task(
            self._drain_tunnel_connection(agent_session, conn),
            name=f"AgentTunnelInbound-{agent_session.name}-{conn_id}",
        )
        try:
            await agent_session.send_control(
                {"type": "open", "conn_id": conn_id, "usb_serial": runtime.usb_serial}
            )
        except Exception:
            await self._close_conn(agent_session, conn_id, notify_agent=False)
            return

        conn.pump_task = asyncio.current_task()
        try:
            while not conn.closed:
                data = await reader.read(TCP_READ_CHUNK)
                if not data:
                    break
                await agent_session.send_data(conn_id, data)
        except Exception as exc:
            logger.debug(
                "隧道连接读取结束: agent=%s conn_id=%s error=%s",
                agent_session.name,
                conn_id,
                exc,
            )
        finally:
            if not conn.closed:
                await self._close_conn(agent_session, conn_id, notify_agent=True, cancel_pump=False)

    async def _drain_tunnel_connection(
        self,
        agent_session: AgentSession,
        conn: TunnelConnection,
    ) -> None:
        """单连接顺序写回本地 ADB；慢连接不会阻塞 Agent WebSocket 接收。"""
        try:
            while not conn.closed:
                payload = await conn.write_queue.get()
                conn.writer.write(payload)
                await conn.writer.drain()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not conn.closed:
                logger.info(
                    "隧道连接本地写入失败: agent=%s conn_id=%s error=%s",
                    agent_session.name,
                    conn.conn_id,
                    exc,
                )
                await self._close_conn(agent_session, conn.conn_id, notify_agent=True)

    async def _close_conn(
        self,
        agent_session: AgentSession,
        conn_id: int,
        *,
        notify_agent: bool,
        cancel_pump: bool = True,
    ) -> None:
        conn = agent_session.conns.pop(conn_id, None)
        if conn is None or conn.closed:
            return
        conn.closed = True
        await agent_session.discard_connection_data(conn_id)
        try:
            conn.writer.close()
        except Exception:
            pass
        if cancel_pump and conn.pump_task is not None and conn.pump_task is not asyncio.current_task():
            conn.pump_task.cancel()
        if conn.writer_task is not None and conn.writer_task is not asyncio.current_task():
            conn.writer_task.cancel()
        if notify_agent and not agent_session.closed:
            try:
                await agent_session.send_control({"type": "close", "conn_id": conn_id})
            except Exception:
                pass

    # ==================== 会话收尾 ====================

    async def _teardown_session(self, agent_session: AgentSession, *, reason: str) -> None:
        if agent_session.closed:
            return
        agent_session.closed = True
        async with self._lock:
            if self._sessions.get(agent_session.name) is agent_session:
                self._sessions.pop(agent_session.name, None)

        for usb_serial in list(agent_session.devices.keys()):
            await self._remove_device_runtime(agent_session, usb_serial)
        for conn_id in list(agent_session.conns.keys()):
            await self._close_conn(agent_session, conn_id, notify_agent=False)

        # 结束未决的带宽探测，避免 REST 端等待到超时
        for future in agent_session._probe_futures.values():
            if not future.done():
                future.set_exception(ConnectionError("Agent 会话已关闭"))
        agent_session._probe_futures.clear()

        # 会话已从运行时目录移除后立即反映离线状态；发送队列收尾不应延迟 UI 状态。
        self._persist_agent_offline(agent_session.agent_id)
        await agent_session.shutdown_sender()

        try:
            await agent_session.websocket.close()
        except Exception:
            pass

        self._notify_keeper()
        logger.info("远程接入点下线: name=%s reason=%s", agent_session.name, reason)

    # ==================== 持久化 ====================

    def _persist_agent_online(self, name: str, agent_info: Dict[str, Any], token_user: str) -> int:
        with Session(engine) as session:
            agent = session.exec(select(RemoteAgent).where(RemoteAgent.name == name)).first()
            now = datetime.now()
            if agent is None:
                agent = RemoteAgent(name=name)
            agent.status = "ONLINE"
            agent.agent_version = str(agent_info.get("version") or "") or None
            os_info = str(agent_info.get("os") or "") or None
            if os_info:
                agent.os_info = os_info[:120]
            agent.last_seen_at = now
            agent.updated_at = now
            session.add(agent)
            session.commit()
            session.refresh(agent)
            return int(agent.id)

    def _persist_agent_offline(self, agent_id: int) -> None:
        try:
            with Session(engine) as session:
                agent = session.get(RemoteAgent, agent_id)
                if agent is None:
                    return
                now = datetime.now()
                agent.status = "OFFLINE"
                agent.last_seen_at = now
                agent.updated_at = now
                session.add(agent)
                session.commit()
        except Exception:
            logger.exception("更新接入点离线状态失败: agent_id=%s", agent_id)

    def _persist_device_mappings(
        self, agent_id: int, reported: Dict[str, Dict[str, Any]]
    ):
        """为上报设备分配/复用固定隧道端口并更新元数据快照。

        返回 (usb_serial -> tunnel_port, usb_serial -> 错误信息)。
        必须在 hub._lock 内调用以串行化端口分配。
        """
        port_map: Dict[str, int] = {}
        errors: Dict[str, str] = {}
        with Session(engine) as session:
            used_ports = {
                int(row.tunnel_port)
                for row in session.exec(select(RemoteAgentDevice)).all()
            }
            now = datetime.now()
            for usb_serial, meta in reported.items():
                mapping = session.exec(
                    select(RemoteAgentDevice).where(
                        RemoteAgentDevice.agent_id == agent_id,
                        RemoteAgentDevice.usb_serial == usb_serial,
                    )
                ).first()
                if mapping is None:
                    port = self._pick_free_port(used_ports)
                    if port is None:
                        errors[usb_serial] = (
                            f"隧道端口耗尽（{TUNNEL_PORT_RANGE_START}-{TUNNEL_PORT_RANGE_END}），"
                            "请删除不再使用的接入点释放端口"
                        )
                        logger.error("%s: agent_id=%s usb_serial=%s", errors[usb_serial], agent_id, usb_serial)
                        continue
                    used_ports.add(port)
                    mapping = RemoteAgentDevice(
                        agent_id=agent_id,
                        usb_serial=usb_serial,
                        tunnel_port=port,
                    )
                mapping.model = str(meta.get("model") or "") or mapping.model
                mapping.brand = str(meta.get("brand") or "") or mapping.brand
                mapping.os_version = str(meta.get("os_version") or "") or mapping.os_version
                mapping.last_seen_at = now
                session.add(mapping)
                port_map[usb_serial] = int(mapping.tunnel_port)
            session.commit()
        return port_map, errors

    @staticmethod
    def _pick_free_port(used_ports: Set[int]) -> Optional[int]:
        for port in range(TUNNEL_PORT_RANGE_START, TUNNEL_PORT_RANGE_END + 1):
            if port not in used_ports:
                return port
        return None

    def _touch_last_seen(self, agent_session: AgentSession) -> None:
        now_monotonic = time.monotonic()
        if now_monotonic - agent_session._last_seen_written < LAST_SEEN_WRITE_INTERVAL_SECONDS:
            return
        agent_session._last_seen_written = now_monotonic
        try:
            with Session(engine) as session:
                agent = session.get(RemoteAgent, agent_session.agent_id)
                if agent is None:
                    return
                agent.last_seen_at = datetime.now()
                session.add(agent)
                session.commit()
        except Exception:
            logger.exception("更新接入点心跳失败: agent=%s", agent_session.name)

    def _notify_keeper(self) -> None:
        if self._keeper is not None:
            try:
                self._keeper.request_sync()
            except Exception:
                logger.exception("通知 adb keeper 失败")


# 全局单例
tunnel_hub = TunnelHub()
