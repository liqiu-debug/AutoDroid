"""远程设备接入（B 机 USB 设备经反向隧道接入本机 adb）。

- protocol: 协议常量与帧编解码
- hub: Agent WebSocket 会话与隧道中继（TunnelHub 单例）
- adb_keeper: 本机 adb connect/disconnect 状态维护
"""
from backend.device_agents.adb_keeper import adb_keeper
from backend.device_agents.hub import tunnel_hub

__all__ = ["adb_keeper", "tunnel_hub"]
