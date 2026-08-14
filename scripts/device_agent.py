#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AutoDroid 设备接入助手（Device Agent）

在使用者电脑（B 机）上运行，把插在本机的 Android USB 设备通过反向隧道
接入部署在服务器（A 机）上的 AutoDroid 平台：

    平台 adb server ⇄ 平台内隧道端口 ⇄ WebSocket(B→A 反向连接)
      ⇄ 本脚本 ⇄ 本机 adb forward ⇄ USB ⇄ 手机 adbd(tcpip 模式)

用法：
    python device_agent.py --server http://<平台地址>:8000 --token adk_xxx [--name 工位B]

要求：
- Python 3.8+（仅标准库，无需 pip 安装任何依赖）
- 本机已安装 adb（Android platform-tools）且手机已开启 USB 调试
- 手机需两次授权 USB 调试弹窗：一次是本机的密钥，一次是平台服务器的密钥

按 Ctrl+C 退出；退出时会清理本脚本创建的 adb forward。
"""
import argparse
import base64
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
from collections import deque
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

AGENT_VERSION = "1.1.0"
PROTOCOL_VERSION = 1
WS_PATH = "/ws/device-agent"

# 数据帧头：4 字节大端 conn_id（与服务端 backend/device_agents/protocol.py 一致）
DATA_FRAME_HEADER_SIZE = 4
TCP_READ_CHUNK = 64 * 1024
# 单个 WebSocket 帧的最大隧道数据块。小块轮转可让控制/自动化命令插队。
TUNNEL_SEND_CHUNK_BYTES = 8 * 1024
TUNNEL_CONNECTION_MAX_PENDING_BYTES = 256 * 1024

# 手机端 adbd TCP 端口（adb tcpip 5555）
DEVICE_ADBD_TCP_PORT = 5555

# 轮询与健康检查
DEFAULT_POLL_INTERVAL = 2.0
HEALTH_PROBE_INTERVAL = 10.0
PROBE_WAIT_SECONDS = 0.4
TCPIP_REAPPEAR_TIMEOUT = 15.0

# 应用层心跳（服务端 75s 无消息判定失联）
PING_INTERVAL = 20.0

# 重连退避（起步快、上限低：断网恢复后尽快回归）
RECONNECT_BACKOFF_START = 1.0
RECONNECT_BACKOFF_MAX = 15.0

_WS_ACCEPT_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

_NETWORK_SERIAL_RE = re.compile(r"^[\w.\-]+:\d+$")


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


# ==================== WebSocket 极简客户端（RFC6455） ====================


class WSClosed(Exception):
    def __init__(self, code: int, reason: str = ""):
        super().__init__(f"websocket closed: code={code} reason={reason}")
        self.code = code
        self.reason = reason


def mask_payload(mask: bytes, data: bytes) -> bytes:
    """按 RFC6455 对 payload 做掩码（大整数 XOR，避免逐字节循环开销）。"""
    if not data:
        return data
    n = len(data)
    repeated = (mask * ((n + 3) // 4))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(repeated, "big")).to_bytes(n, "big")


def build_ws_frame(opcode: int, payload: bytes, *, masked: bool = True) -> bytes:
    """构造单个 FIN=1 的 WebSocket 帧（客户端必须掩码）。"""
    header = bytearray([0x80 | (opcode & 0x0F)])
    n = len(payload)
    mask_bit = 0x80 if masked else 0x00
    if n < 126:
        header.append(mask_bit | n)
    elif n <= 0xFFFF:
        header.append(mask_bit | 126)
        header += struct.pack(">H", n)
    else:
        header.append(mask_bit | 127)
        header += struct.pack(">Q", n)
    if masked:
        mask = os.urandom(4)
        return bytes(header) + mask + mask_payload(mask, payload)
    return bytes(header) + payload


class MiniWebSocket:
    """仅覆盖本 Agent 所需能力的 WebSocket 客户端。

    - 线程模型：单读线程调用 recv_message()；多线程可并发调用 send_*（内部加锁）
    - 自动回应 ping；收到 close 帧抛 WSClosed
    """

    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._send_lock = threading.Lock()
        self._closed = False

    # ---------- 建连 ----------

    @classmethod
    def connect(cls, server_url: str, *, headers: Dict[str, str], timeout: float = 10.0) -> "MiniWebSocket":
        parsed = urlparse(server_url)
        scheme = (parsed.scheme or "http").lower()
        use_tls = scheme in ("https", "wss")
        host = parsed.hostname
        if not host:
            raise ValueError(f"无法解析服务器地址: {server_url}")
        port = parsed.port or (443 if use_tls else 80)

        raw = socket.create_connection((host, port), timeout=timeout)
        try:
            # 隧道上跑的是 adb 小包往返，关闭 Nagle 降低交互延迟
            try:
                raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            if use_tls:
                context = ssl.create_default_context()
                # 内网自签证书场景：不强制校验（与浏览器手动信任等价的取舍）
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
                raw = context.wrap_socket(raw, server_hostname=host)

            key = base64.b64encode(os.urandom(16)).decode("ascii")
            host_header = f"{host}:{port}"
            lines = [
                f"GET {WS_PATH} HTTP/1.1",
                f"Host: {host_header}",
                "Upgrade: websocket",
                "Connection: Upgrade",
                f"Sec-WebSocket-Key: {key}",
                "Sec-WebSocket-Version: 13",
                f"User-Agent: AutoDroid-DeviceAgent/{AGENT_VERSION}",
            ]
            for name, value in headers.items():
                lines.append(f"{name}: {value}")
            request = "\r\n".join(lines) + "\r\n\r\n"
            raw.sendall(request.encode("utf-8"))

            response = cls._read_http_response(raw)
            status_line = response.split("\r\n", 1)[0]
            if " 101 " not in f"{status_line} ":
                raise ConnectionError(f"WebSocket 握手失败: {status_line.strip()}")
            accept_expected = base64.b64encode(
                hashlib.sha1((key + _WS_ACCEPT_GUID).encode("ascii")).digest()
            ).decode("ascii")
            match = re.search(r"(?im)^sec-websocket-accept:\s*(\S+)\s*$", response)
            if not match or match.group(1) != accept_expected:
                raise ConnectionError("WebSocket 握手校验失败（Sec-WebSocket-Accept 不匹配）")

            raw.settimeout(None)
            return cls(raw)
        except Exception:
            try:
                raw.close()
            except Exception:
                pass
            raise

    @staticmethod
    def _read_http_response(sock: socket.socket) -> str:
        buffer = b""
        while b"\r\n\r\n" not in buffer:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("WebSocket 握手期间连接被关闭")
            buffer += chunk
            if len(buffer) > 64 * 1024:
                raise ConnectionError("WebSocket 握手响应过大")
        return buffer.split(b"\r\n\r\n", 1)[0].decode("utf-8", errors="replace")

    # ---------- 发送 ----------

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8"))

    def send_binary(self, payload: bytes) -> None:
        self._send_frame(0x2, payload)

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        frame = build_ws_frame(opcode, payload)
        with self._send_lock:
            self._sock.sendall(frame)

    def close(self, code: int = 1000) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._send_frame(0x8, struct.pack(">H", code))
        except Exception:
            pass
        try:
            self._sock.close()
        except Exception:
            pass

    # ---------- 接收 ----------

    def _recv_exact(self, n: int) -> bytes:
        data = b""
        while len(data) < n:
            chunk = self._sock.recv(n - len(data))
            if not chunk:
                raise ConnectionError("websocket connection lost")
            data += chunk
        return data

    def _recv_frame(self) -> Tuple[bool, int, bytes]:
        head = self._recv_exact(2)
        fin = bool(head[0] & 0x80)
        opcode = head[0] & 0x0F
        masked = bool(head[1] & 0x80)
        length = head[1] & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._recv_exact(8))[0]
        mask = self._recv_exact(4) if masked else b""
        payload = self._recv_exact(length) if length else b""
        if masked and payload:
            payload = mask_payload(mask, payload)
        return fin, opcode, payload

    def recv_message(self) -> Tuple[str, bytes]:
        """接收一条完整消息，返回 ("text"|"binary", payload)。"""
        buffered_opcode: Optional[int] = None
        buffered = bytearray()
        while True:
            fin, opcode, payload = self._recv_frame()
            if opcode == 0x9:  # ping
                try:
                    self._send_frame(0xA, payload)
                except Exception:
                    pass
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode == 0x8:  # close
                code = struct.unpack(">H", payload[:2])[0] if len(payload) >= 2 else 1005
                reason = payload[2:].decode("utf-8", errors="replace") if len(payload) > 2 else ""
                raise WSClosed(code, reason)
            if opcode in (0x1, 0x2):
                if fin:
                    return ("text" if opcode == 0x1 else "binary", bytes(payload))
                buffered_opcode = opcode
                buffered = bytearray(payload)
                continue
            if opcode == 0x0 and buffered_opcode is not None:  # continuation
                buffered += payload
                if fin:
                    kind = "text" if buffered_opcode == 0x1 else "binary"
                    buffered_opcode = None
                    return (kind, bytes(buffered))
                continue
            # 其他 opcode 忽略


class TunnelSendScheduler:
    """Agent → 平台侧的公平发送器。

    所有连接复用一个 WebSocket，但不能由一条 scrcpy 视频流连续持锁。数据按
    conn_id 分队列，每轮最多写一个 8KB 块；控制消息始终优先。单连接到达
    上限时其读取线程阻塞，让 TCP 窗口反压视频而不是挤占其他 ADB 命令。
    """

    def __init__(self, ws: MiniWebSocket):
        self._ws = ws
        self._condition = threading.Condition()
        self._controls = deque()
        self._data_queues: Dict[int, deque] = {}
        self._data_pending_bytes: Dict[int, int] = {}
        self._round_robin = deque()
        self._closed_connections = set()
        self._closed = False
        self._thread = threading.Thread(target=self._run, daemon=True, name="tunnel-ws-sender")
        self._thread.start()

    def send_text(self, text: str, *, wait: bool = False) -> bool:
        done = threading.Event() if wait else None
        result = []
        with self._condition:
            if self._closed:
                return False
            self._controls.append((str(text), done, result))
            self._condition.notify_all()
        if done is not None:
            done.wait()
            return bool(result and result[0] is None)
        return True

    def send_binary(self, conn_id: int, payload: bytes) -> bool:
        if not payload:
            return True
        with self._condition:
            while (
                not self._closed
                and conn_id not in self._closed_connections
                and self._data_pending_bytes.get(conn_id, 0) + len(payload)
                > TUNNEL_CONNECTION_MAX_PENDING_BYTES
            ):
                self._condition.wait()
            if self._closed or conn_id in self._closed_connections:
                return False
            queue_for_connection = self._data_queues.setdefault(conn_id, deque())
            was_empty = not queue_for_connection
            queue_for_connection.append(bytes(payload))
            self._data_pending_bytes[conn_id] = self._data_pending_bytes.get(conn_id, 0) + len(payload)
            if was_empty:
                self._round_robin.append(conn_id)
            self._condition.notify_all()
        return True

    def discard_connection(self, conn_id: int) -> None:
        with self._condition:
            self._closed_connections.add(conn_id)
            self._data_queues.pop(conn_id, None)
            self._data_pending_bytes.pop(conn_id, None)
            self._round_robin = deque(item for item in self._round_robin if item != conn_id)
            self._condition.notify_all()

    def stop(self) -> None:
        with self._condition:
            self._closed = True
            while self._controls:
                _, done, result = self._controls.popleft()
                if done is not None:
                    result.append(ConnectionError("tunnel sender stopped"))
                    done.set()
            self._data_queues.clear()
            self._data_pending_bytes.clear()
            self._round_robin.clear()
            self._condition.notify_all()
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)

    def _run(self) -> None:
        while True:
            control = None
            binary_payload = None
            with self._condition:
                while not self._closed and not self._controls and not self._round_robin:
                    self._condition.wait()
                if self._closed:
                    return
                if self._controls:
                    control = self._controls.popleft()
                else:
                    conn_id = self._round_robin.popleft()
                    queue_for_connection = self._data_queues.get(conn_id)
                    if not queue_for_connection:
                        continue
                    pending = queue_for_connection.popleft()
                    chunk = pending[:TUNNEL_SEND_CHUNK_BYTES]
                    remainder = pending[TUNNEL_SEND_CHUNK_BYTES:]
                    if remainder:
                        queue_for_connection.appendleft(remainder)
                    if queue_for_connection:
                        self._round_robin.append(conn_id)
                    else:
                        self._data_queues.pop(conn_id, None)
                    self._data_pending_bytes[conn_id] = max(
                        0, self._data_pending_bytes.get(conn_id, 0) - len(chunk)
                    )
                    if self._data_pending_bytes.get(conn_id) == 0:
                        self._data_pending_bytes.pop(conn_id, None)
                    self._condition.notify_all()
                    binary_payload = build_data_frame(conn_id, chunk)
            try:
                if control is not None:
                    text, done, result = control
                    self._ws.send_text(text)
                    if done is not None:
                        result.append(None)
                        done.set()
                elif binary_payload is not None:
                    self._ws.send_binary(binary_payload)
            except Exception as exc:
                if control is not None:
                    _, done, result = control
                    if done is not None:
                        result.append(exc)
                        done.set()
                self.stop()
                return


# ==================== 数据帧编解码（与服务端协议一致） ====================


def build_data_frame(conn_id: int, payload: bytes) -> bytes:
    return struct.pack(">I", int(conn_id)) + payload


def parse_data_frame(frame: bytes) -> Tuple[int, bytes]:
    if len(frame) < DATA_FRAME_HEADER_SIZE:
        raise ValueError(f"data frame too short: {len(frame)}")
    conn_id = struct.unpack(">I", frame[:DATA_FRAME_HEADER_SIZE])[0]
    return conn_id, frame[DATA_FRAME_HEADER_SIZE:]


# ==================== adb 管理 ====================


def find_adb_path(explicit: Optional[str] = None) -> Optional[str]:
    """定位 adb 可执行文件：--adb 参数 > PATH > 常见 SDK 安装路径。"""
    candidates: List[str] = []
    if explicit:
        candidates.append(explicit)
    found = shutil.which("adb")
    if found:
        candidates.append(found)
    home = os.path.expanduser("~")
    if os.name == "nt":
        local_appdata = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        candidates.extend(
            [
                os.path.join(local_appdata, "Android", "Sdk", "platform-tools", "adb.exe"),
                r"C:\platform-tools\adb.exe",
            ]
        )
    else:
        candidates.extend(
            [
                os.path.join(home, "Library", "Android", "sdk", "platform-tools", "adb"),
                os.path.join(home, "Android", "Sdk", "platform-tools", "adb"),
                "/opt/homebrew/bin/adb",
                "/usr/local/bin/adb",
            ]
        )
    for path in candidates:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def parse_adb_devices_output(output: str) -> List[Dict[str, str]]:
    """解析 ``adb devices -l``：返回 [{serial, state, model}]，跳过标题与空行。"""
    devices: List[Dict[str, str]] = []
    for line in str(output or "").strip().splitlines():
        line = line.strip()
        if not line or line.lower().startswith("list of devices"):
            continue
        if line.startswith("*"):  # daemon 启动提示
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        model = ""
        for token in parts[2:]:
            if token.startswith("model:"):
                model = token[len("model:"):].replace("_", " ")
                break
        devices.append({"serial": serial, "state": state, "model": model})
    return devices


def is_network_serial(serial: str) -> bool:
    """是否为 ip:port / host:port 形态（非本机 USB 直插设备）。"""
    return bool(_NETWORK_SERIAL_RE.match(str(serial or "")) and ":" in str(serial))


class AdbManager:
    def __init__(self, adb_path: str):
        self.adb_path = adb_path

    def run(self, *args: str, timeout: float = 20.0) -> str:
        cmd = [self.adb_path] + list(args)
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        output = (proc.stdout or b"").decode("utf-8", errors="replace")
        if proc.returncode != 0:
            raise RuntimeError(f"adb {' '.join(args)} 失败(rc={proc.returncode}): {output.strip()[:300]}")
        return output

    def list_usb_devices(self) -> List[Dict[str, str]]:
        output = self.run("devices", "-l")
        return [d for d in parse_adb_devices_output(output) if not is_network_serial(d["serial"])]

    def get_device_props(self, serial: str) -> Dict[str, str]:
        mapping = {
            "model": "ro.product.model",
            "brand": "ro.product.brand",
            "os_version": "ro.build.version.release",
        }
        keys = list(mapping.keys())
        separator = "___AUTODROID_SEP___"
        script = f"; echo {separator}; ".join(f"getprop {mapping[key]}" for key in keys)
        props = {key: "" for key in keys}
        try:
            output = self.run("-s", serial, "shell", script, timeout=8)
        except Exception:
            return props
        parts = output.split(separator)
        if len(parts) != len(keys):
            return props
        for key, value in zip(keys, parts):
            props[key] = value.strip()
        return props

    def probe_forward_port(self, local_port: int) -> bool:
        """探测 forward 隧道是否真正打通到手机 adbd。

        adb forward 的监听端口总是可以 accept；若设备侧 tcp:5555 不可达，
        adb 会在建立后立即关闭连接。因此：连接后短暂等待，
        - 立即收到 EOF → 隧道断（设备侧 tcpip 未开）
        - 等待超时仍保持打开 → 隧道通（adbd 在等客户端握手）
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1.5)
        try:
            sock.connect(("127.0.0.1", local_port))
            sock.settimeout(PROBE_WAIT_SECONDS)
            try:
                data = sock.recv(1)
                return bool(data)  # EOF(b"") = 断
            except socket.timeout:
                return True
        except OSError:
            return False
        finally:
            try:
                sock.close()
            except Exception:
                pass

    def ensure_tcp_bridge(self, serial: str, local_port: int) -> None:
        """确保 serial 设备经 127.0.0.1:local_port 可达其 adbd TCP 端口。

        先尝试直接建 forward（幂等、开销小）；探测失败再走
        tcpip 5555 → 等设备回归 → 重建 forward → 复测 的完整流程。
        """
        try:
            self.run("-s", serial, "forward", f"tcp:{local_port}", f"tcp:{DEVICE_ADBD_TCP_PORT}")
        except Exception as exc:
            raise RuntimeError(f"创建 adb forward 失败: {exc}")
        if self.probe_forward_port(local_port):
            return

        log(f"设备 {serial} adbd TCP 未就绪，执行 adb tcpip {DEVICE_ADBD_TCP_PORT}（adbd 将重启，等待设备回归）")
        self.run("-s", serial, "tcpip", str(DEVICE_ADBD_TCP_PORT), timeout=15)

        deadline = time.time() + TCPIP_REAPPEAR_TIMEOUT
        while time.time() < deadline:
            time.sleep(0.5)
            try:
                states = {d["serial"]: d["state"] for d in self.list_usb_devices()}
            except Exception:
                continue
            if states.get(serial) == "device":
                break
        else:
            raise RuntimeError(f"设备 {serial} 在 tcpip 后未回归 USB 列表")

        self.run("-s", serial, "forward", f"tcp:{local_port}", f"tcp:{DEVICE_ADBD_TCP_PORT}")
        time.sleep(0.3)
        if not self.probe_forward_port(local_port):
            raise RuntimeError(f"设备 {serial} 隧道探测失败（forward 已建但 adbd:{DEVICE_ADBD_TCP_PORT} 不可达）")

    def remove_forward(self, local_port: int) -> None:
        try:
            self.run("forward", "--remove", f"tcp:{local_port}")
        except Exception:
            pass


def pick_free_local_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


# ==================== 设备注册表（USB 设备状态机） ====================


class LocalDevice:
    def __init__(self, serial: str, forward_port: int, props: Dict[str, str]):
        self.serial = serial
        self.forward_port = forward_port
        self.props = props
        self.last_probe_at = time.time()

    def to_report(self) -> Dict[str, str]:
        return {
            "usb_serial": self.serial,
            "model": self.props.get("model") or "",
            "brand": (self.props.get("brand") or "").upper(),
            "os_version": self.props.get("os_version") or "",
        }


class DeviceRegistry:
    """轮询本机 USB 设备：接入建桥、拔出清理、掉桥自愈；变化时回调上报。"""

    def __init__(self, adb: AdbManager, on_change, poll_interval: float = DEFAULT_POLL_INTERVAL):
        self.adb = adb
        self.on_change = on_change  # Callable[[List[dict]], None]
        self.poll_interval = poll_interval
        self.devices: Dict[str, LocalDevice] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._warned: Dict[str, str] = {}

    def snapshot_reports(self) -> List[Dict[str, str]]:
        with self._lock:
            return [d.to_report() for d in self.devices.values()]

    def forward_port_of(self, usb_serial: str) -> Optional[int]:
        with self._lock:
            device = self.devices.get(usb_serial)
            return device.forward_port if device else None

    def stop(self) -> None:
        self._stop.set()

    def cleanup_forwards(self) -> None:
        with self._lock:
            devices = list(self.devices.values())
        for device in devices:
            self.adb.remove_forward(device.forward_port)

    def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                self._poll_once()
            except Exception as exc:
                log(f"设备轮询异常（将重试）: {exc}")
            self._stop.wait(self.poll_interval)

    def _warn_once(self, serial: str, state: str, message: str) -> None:
        if self._warned.get(serial) != state:
            self._warned[serial] = state
            log(message)

    def _poll_once(self) -> None:
        listed = self.adb.list_usb_devices()
        usable: Dict[str, Dict[str, str]] = {}
        for item in listed:
            serial, state = item["serial"], item["state"]
            if state == "device":
                usable[serial] = item
                self._warned.pop(serial, None)
            elif state == "unauthorized":
                self._warn_once(serial, state, f"设备 {serial} 未授权：请在手机上允许本机的 USB 调试请求")
            elif state == "offline":
                self._warn_once(serial, state, f"设备 {serial} 状态 offline：请重新插拔或检查数据线")

        changed = False

        # 拔出（或不可用）的设备
        with self._lock:
            known_serials = list(self.devices.keys())
        for serial in known_serials:
            if serial not in usable:
                with self._lock:
                    device = self.devices.pop(serial, None)
                if device:
                    self.adb.remove_forward(device.forward_port)
                    log(f"设备已移除: {serial}")
                    changed = True

        # 新接入的设备
        for serial, item in usable.items():
            with self._lock:
                exists = serial in self.devices
            if exists:
                continue
            try:
                forward_port = pick_free_local_port()
                self.adb.ensure_tcp_bridge(serial, forward_port)
                props = self.adb.get_device_props(serial)
                if not props.get("model"):
                    props["model"] = item.get("model") or ""
                device = LocalDevice(serial, forward_port, props)
                with self._lock:
                    self.devices[serial] = device
                log(f"设备已接入: {serial} ({props.get('model') or '未知型号'}) forward=tcp:{forward_port}")
                changed = True
            except Exception as exc:
                self._warn_once(serial, f"bridge_fail:{exc}", f"设备 {serial} 建立隧道失败: {exc}")

        # 存量设备健康检查（周期性探测 forward，掉桥自愈）
        now = time.time()
        with self._lock:
            to_check = [d for d in self.devices.values() if now - d.last_probe_at >= HEALTH_PROBE_INTERVAL]
        for device in to_check:
            device.last_probe_at = now
            if self.adb.probe_forward_port(device.forward_port):
                continue
            log(f"设备 {device.serial} 隧道断开（可能重启过），尝试自愈…")
            try:
                self.adb.ensure_tcp_bridge(device.serial, device.forward_port)
                log(f"设备 {device.serial} 隧道已恢复")
            except Exception as exc:
                log(f"设备 {device.serial} 自愈失败，暂时下线: {exc}")
                with self._lock:
                    self.devices.pop(device.serial, None)
                self.adb.remove_forward(device.forward_port)
                changed = True

        if changed:
            self.on_change(self.snapshot_reports())


# ==================== 隧道客户端（WS 会话 + 连接转发） ====================


class TunnelAgent:
    def __init__(self, server: str, token: str, name: str, registry: DeviceRegistry):
        self.server = server.rstrip("/")
        self.token = token
        self.name = name
        self.registry = registry
        self.ws: Optional[MiniWebSocket] = None
        self._sender: Optional[TunnelSendScheduler] = None
        self.conns: Dict[int, socket.socket] = {}
        self._conn_lock = threading.Lock()
        self._stop = threading.Event()
        self._registered = threading.Event()
        self._fatal: Optional[str] = None

    # ---------- 对外 ----------

    def stop(self) -> None:
        self._stop.set()
        sender = self._sender
        if sender is not None:
            sender.stop()
        ws = self.ws
        if ws is not None:
            ws.close()

    def notify_devices_changed(self, reports: List[Dict[str, str]]) -> None:
        sender = self._sender
        if sender is None or not self._registered.is_set():
            return
        try:
            sender.send_text(json.dumps({"type": "devices", "devices": reports}, ensure_ascii=False))
        except Exception as exc:
            log(f"设备列表上报失败（等待重连）: {exc}")

    def run_forever(self) -> int:
        backoff = RECONNECT_BACKOFF_START
        while not self._stop.is_set():
            try:
                self._run_session()
                backoff = RECONNECT_BACKOFF_START
            except WSClosed as exc:
                if self._fatal:
                    log(f"服务端拒绝: {self._fatal}")
                    return 2
                log(f"连接被服务端关闭 (code={exc.code})")
            except Exception as exc:
                log(f"连接失败: {exc}")
            if self._stop.is_set():
                break
            if self._fatal:
                log(f"致命错误，退出: {self._fatal}")
                return 2
            log(f"{backoff:.0f} 秒后重连…")
            if self._stop.wait(backoff):
                break
            backoff = min(backoff * 2, RECONNECT_BACKOFF_MAX)
        return 0

    # ---------- 会话 ----------

    def _run_session(self) -> None:
        self._registered.clear()
        log(f"连接平台 {self.server}{WS_PATH} …")
        ws = MiniWebSocket.connect(
            self.server,
            headers={"Authorization": f"Bearer {self.token}"},
        )
        self.ws = ws
        sender = TunnelSendScheduler(ws)
        self._sender = sender
        try:
            register = {
                "type": "register",
                "protocol": PROTOCOL_VERSION,
                "agent": {
                    "name": self.name,
                    "version": AGENT_VERSION,
                    "os": f"{platform.system()} {platform.release()}",
                },
                "devices": self.registry.snapshot_reports(),
            }
            if not sender.send_text(json.dumps(register, ensure_ascii=False), wait=True):
                raise ConnectionError("注册消息发送失败")

            ping_thread = threading.Thread(target=self._ping_loop, args=(ws,), daemon=True)
            ping_thread.start()

            while not self._stop.is_set():
                kind, payload = ws.recv_message()
                if kind == "binary":
                    self._on_data_frame(payload)
                else:
                    self._on_control(json.loads(payload.decode("utf-8")))
        finally:
            self.ws = None
            self._registered.clear()
            self._close_all_conns()
            self._sender = None
            sender.stop()
            ws.close()

    def _ping_loop(self, ws: MiniWebSocket) -> None:
        while not self._stop.is_set() and self.ws is ws:
            time.sleep(PING_INTERVAL)
            if self.ws is not ws:
                return
            sender = self._sender
            if sender is None or not sender.send_text(json.dumps({"type": "ping"})):
                return

    # ---------- 控制消息 ----------

    def _on_control(self, control: Dict) -> None:
        msg_type = str(control.get("type") or "")
        if msg_type == "registered":
            self._registered.set()
            devices = control.get("devices") or []
            rejected = control.get("rejected") or []
            summary = ", ".join(
                f"{d.get('usb_serial')} → 平台侧 {d.get('serial')}" for d in devices
            ) or "（暂无设备）"
            log(f"注册成功：{summary}")
            for item in rejected:
                log(f"设备被平台拒绝: {item.get('usb_serial')}: {item.get('error')}")
        elif msg_type == "open":
            self._on_open(int(control.get("conn_id") or 0), str(control.get("usb_serial") or ""))
        elif msg_type == "close":
            self._close_conn(int(control.get("conn_id") or 0), notify=False)
        elif msg_type == "pong":
            pass
        elif msg_type == "error":
            message = str(control.get("message") or "服务端返回错误")
            if "鉴权" in message or "Token" in message or "协议版本" in message:
                self._fatal = message
            log(f"服务端消息: {message}")
        else:
            log(f"未知控制消息: {msg_type}")

    def _on_open(self, conn_id: int, usb_serial: str) -> None:
        ws = self.ws
        if ws is None:
            return
        forward_port = self.registry.forward_port_of(usb_serial)
        if forward_port is None:
            self._send_open_result(conn_id, False, f"设备 {usb_serial} 不在本机")
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(3.0)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        try:
            sock.connect(("127.0.0.1", forward_port))
            sock.settimeout(None)
        except OSError as exc:
            try:
                sock.close()
            except Exception:
                pass
            self._send_open_result(conn_id, False, f"本机 forward 端口连接失败: {exc}")
            return
        with self._conn_lock:
            self.conns[conn_id] = sock
        self._send_open_result(conn_id, True, None)
        pump = threading.Thread(
            target=self._pump_device_to_ws, args=(conn_id, sock), daemon=True
        )
        pump.start()

    def _send_open_result(self, conn_id: int, ok: bool, error: Optional[str]) -> None:
        message = {"type": "open_result", "conn_id": conn_id, "ok": ok}
        if error:
            message["error"] = error
        sender = self._sender
        if sender is not None:
            sender.send_text(json.dumps(message, ensure_ascii=False), wait=True)

    # ---------- 数据转发 ----------

    def _on_data_frame(self, frame: bytes) -> None:
        try:
            conn_id, payload = parse_data_frame(frame)
        except ValueError:
            return
        with self._conn_lock:
            sock = self.conns.get(conn_id)
        if sock is None:
            return
        try:
            sock.sendall(payload)
        except OSError:
            self._close_conn(conn_id, notify=True)

    def _pump_device_to_ws(self, conn_id: int, sock: socket.socket) -> None:
        try:
            while True:
                data = sock.recv(TCP_READ_CHUNK)
                if not data:
                    break
                sender = self._sender
                if sender is None or not sender.send_binary(conn_id, data):
                    break
        except OSError:
            pass
        except Exception:
            pass
        finally:
            self._close_conn(conn_id, notify=True)

    def _close_conn(self, conn_id: int, *, notify: bool) -> None:
        with self._conn_lock:
            sock = self.conns.pop(conn_id, None)
        if sock is None:
            return
        try:
            sock.close()
        except Exception:
            pass
        if notify:
            sender = self._sender
            if sender is not None:
                sender.discard_connection(conn_id)
                sender.send_text(json.dumps({"type": "close", "conn_id": conn_id}))
        else:
            sender = self._sender
            if sender is not None:
                sender.discard_connection(conn_id)

    def _close_all_conns(self) -> None:
        with self._conn_lock:
            socks = list(self.conns.values())
            self.conns.clear()
        for sock in socks:
            try:
                sock.close()
            except Exception:
                pass


# ==================== 入口 ====================


def _default_agent_name() -> str:
    hostname = socket.gethostname() or "agent"
    return hostname.split(".")[0][:64]


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="AutoDroid 设备接入助手：把本机 USB Android 设备接入远端平台",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--server", required=True, help="平台地址，例如 http://192.168.1.10:8000")
    parser.add_argument("--token", required=True, help="平台 API Token（账号设置页生成，adk_ 开头）")
    parser.add_argument("--name", default=_default_agent_name(), help="接入点名称（默认取本机主机名）")
    parser.add_argument("--adb", default=None, help="adb 可执行文件路径（默认自动查找）")
    parser.add_argument("--poll", type=float, default=DEFAULT_POLL_INTERVAL, help="USB 设备轮询间隔（秒）")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    # Windows 控制台尽量切 UTF-8，避免中文日志乱码
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass

    args = build_arg_parser().parse_args(argv)

    parsed = urlparse(args.server)
    if parsed.scheme not in ("http", "https", "ws", "wss") or not parsed.hostname:
        print(f"--server 地址不合法: {args.server}（示例: http://192.168.1.10:8000）")
        return 2
    if not str(args.token).strip().startswith("adk_"):
        print("--token 应为平台 API Token（adk_ 开头），请在平台「账号设置 → API Token」生成")
        return 2

    adb_path = find_adb_path(args.adb)
    if not adb_path:
        print(
            "未找到 adb。请安装 Android platform-tools 并加入 PATH，"
            "或用 --adb 指定路径。下载: https://developer.android.com/tools/releases/platform-tools"
        )
        return 2

    adb = AdbManager(adb_path)
    try:
        adb.run("start-server", timeout=30)
    except Exception as exc:
        print(f"启动本机 adb server 失败: {exc}")
        return 2

    log(f"AutoDroid 设备接入助手 v{AGENT_VERSION}")
    log(f"接入点名称: {args.name} | adb: {adb_path}")
    log("提示：手机首次接入需确认两次「允许 USB 调试」弹窗（本机密钥 + 平台服务器密钥）")

    registry = DeviceRegistry(adb, on_change=lambda reports: agent.notify_devices_changed(reports), poll_interval=args.poll)
    agent = TunnelAgent(args.server, str(args.token).strip(), args.name, registry)

    def _handle_signal(signum, frame):
        log("收到退出信号，清理中…")
        agent.stop()
        registry.stop()

    signal.signal(signal.SIGINT, _handle_signal)
    try:
        signal.signal(signal.SIGTERM, _handle_signal)
    except Exception:
        pass

    poll_thread = threading.Thread(target=registry.run_forever, daemon=True, name="device-poll")
    poll_thread.start()

    try:
        return agent.run_forever()
    finally:
        registry.stop()
        registry.cleanup_forwards()
        log("已退出（本脚本创建的 adb forward 已清理；手机 tcpip 模式保留，重启手机或执行 adb usb 可关闭）")


if __name__ == "__main__":
    sys.exit(main())
