"""
WebSocket 连接管理器
用于实时推送测试执行状态
"""
from typing import Dict, List, Any, Union
from fastapi import WebSocket
import asyncio
import json
from datetime import datetime
import uuid


class ConnectionManager:
    """管理 WebSocket 连接"""
    
    def __init__(self):
        # key (int or str) -> list of websocket connections
        self.active_connections: Dict[Union[int, str], List[WebSocket]] = {}
    
    async def connect(self, websocket: WebSocket, key: Union[int, str]):
        """接受新连接"""
        await websocket.accept()
        if key not in self.active_connections:
            self.active_connections[key] = []
        self.active_connections[key].append(websocket)
    
    def disconnect(self, websocket: WebSocket, key: Union[int, str]):
        """断开连接"""
        if key in self.active_connections:
            if websocket in self.active_connections[key]:
                self.active_connections[key].remove(websocket)
            if not self.active_connections[key]:
                del self.active_connections[key]
    
    async def send_message(self, key: Union[int, str], message: dict):
        """向指定 key 的所有连接发送消息"""
        if key in self.active_connections:
            dead_connections = []
            for connection in self.active_connections[key]:
                try:
                    await connection.send_json(message)
                except Exception:
                    dead_connections.append(connection)
            
            # 清理断开的连接
            for conn in dead_connections:
                self.disconnect(conn, key)
    
    async def broadcast_log(
        self, 
        key: Union[int, str], 
        level: str, 
        message: str, 
        attachment: str = None, 
        attachment_type: str = None
    ):
        """广播普通日志消息"""
        payload = {
            "type": "log",
            "level": level, # info, success, warning, error
            "message": message,
            "timestamp": datetime.now().isoformat()
        }
        if attachment:
            payload["attachment"] = attachment
        if attachment_type:
            payload["attachment_type"] = attachment_type
            
        await self.send_message(key, payload)

    async def broadcast_step_update(
        self, 
        case_id: int, 
        step_index: int,
        status: str,  # "running", "success", "failed", "retry"
        log: str,
        duration: float = 0,
        screenshot_base64: str = None,
        error: str = None
    ):
        """广播步骤状态更新 (兼容旧 Case 执行)"""
        message = {
            "type": "step_update",
            "step_id": str(step_index),
            "step_index": step_index,
            "status": status,
            "log": log,
            "duration": round(duration, 2),
            "timestamp": datetime.now().isoformat()
        }
        
        if screenshot_base64:
            message["screenshot"] = screenshot_base64
        if error:
            message["error"] = error
            
        await self.send_message(case_id, message)
    
    async def broadcast_run_start(self, case_id: int, case_name: str, total_steps: int):
        """广播开始执行"""
        await self.send_message(case_id, {
            "type": "run_start",
            "case_id": case_id,
            "case_name": case_name,
            "total_steps": total_steps,
            "timestamp": datetime.now().isoformat()
        })
    
    async def broadcast_run_complete(
        self, 
        case_id: int, 
        success: bool, 
        total_duration: float,
        passed: int,
        failed: int,
        report_id: str = None
    ):
        """广播执行完成"""
        await self.send_message(case_id, {
            "type": "run_complete",
            "case_id": case_id,
            "success": success,
            "total_duration": round(total_duration, 2),
            "passed": passed,
            "failed": failed,
            "report_id": report_id,
            "timestamp": datetime.now().isoformat()
        })


# 全局连接管理器实例
manager = ConnectionManager()
