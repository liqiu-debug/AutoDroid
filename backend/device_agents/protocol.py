"""远程设备接入协议常量与帧编解码。

Agent（B 机脚本）与后端通过一条 WebSocket 通信：
- 控制消息走 JSON text 帧（register / devices / open / open_result / close / ping / pong / error）
- 隧道数据走 binary 帧：4 字节大端 conn_id + 原始 TCP payload

纯函数集中在本模块，便于后端与测试复用；Agent 脚本内嵌同构实现。
"""
import struct
from typing import Optional, Tuple

# Agent 协议版本（不兼容变更时递增，双方在 register/registered 中交换）
AGENT_PROTOCOL_VERSION = 1

# 每台远程设备在后端 127.0.0.1 上的隧道监听端口范围（含端点）。
# 避开 scrcpy 本地转发端口段 27183-27283。
TUNNEL_PORT_RANGE_START = 28100
TUNNEL_PORT_RANGE_END = 28199

TUNNEL_HOST = "127.0.0.1"

# 单个 binary 数据帧 payload 上限（发送侧按 64KB 读，接收侧防御性放宽）
DATA_FRAME_HEADER_SIZE = 4
MAX_DATA_PAYLOAD = 1024 * 1024

# Android 远程 USB 设备的 connection_type 取值
REMOTE_USB_CONNECTION_TYPE = "remote_usb"


def encode_data_frame(conn_id: int, payload: bytes) -> bytes:
    """编码隧道数据帧：4 字节大端 conn_id + payload。"""
    return struct.pack(">I", int(conn_id)) + payload


def decode_data_frame(frame: bytes) -> Tuple[int, bytes]:
    """解码隧道数据帧，返回 (conn_id, payload)；帧过短时抛 ValueError。"""
    if len(frame) < DATA_FRAME_HEADER_SIZE:
        raise ValueError(f"data frame too short: {len(frame)} bytes")
    conn_id = struct.unpack(">I", frame[:DATA_FRAME_HEADER_SIZE])[0]
    return conn_id, frame[DATA_FRAME_HEADER_SIZE:]


def tunnel_serial(port: int) -> str:
    """隧道端口对应的 adb serial（adb connect 后的设备形态）。"""
    return f"{TUNNEL_HOST}:{int(port)}"


def parse_tunnel_serial(serial: str) -> Optional[int]:
    """解析 serial 是否为本机隧道形态（127.0.0.1:<范围内端口>），返回端口或 None。"""
    text = str(serial or "").strip()
    prefix = f"{TUNNEL_HOST}:"
    if not text.startswith(prefix):
        return None
    try:
        port = int(text[len(prefix):])
    except ValueError:
        return None
    if TUNNEL_PORT_RANGE_START <= port <= TUNNEL_PORT_RANGE_END:
        return port
    return None
