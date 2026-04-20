"""
Scrcpy 视频流 API 路由

REST 与 WebSocket 分离，便于在主应用中分别挂载：
- REST canonical: /api/stream/devices/*
- REST legacy: /devices* /api/devices*（兼容保留）
- WebSocket: /ws/scrcpy/{serial}
"""
import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException
from pydantic import BaseModel

from .manager import device_manager

logger = logging.getLogger(__name__)

rest_router = APIRouter()
ws_router = APIRouter()
router = rest_router


# ==================== REST API ====================


@rest_router.get("/devices")
def list_devices():
    """获取所有已连接设备列表"""
    return device_manager.get_devices_list()


@rest_router.get("/devices/{serial}")
def get_device(serial: str):
    """获取单个设备信息"""
    info = device_manager.get_device(serial)
    if not info:
        raise HTTPException(status_code=404, detail=f"设备 {serial} 未找到")
    return info


class TouchEvent(BaseModel):
    """触控事件请求体"""
    action: int = 0  # 兼容语义：0=tap, 1=抬起, 2=移动
    x: int
    y: int


@rest_router.post("/devices/{serial}/touch")
def send_touch(serial: str, event: TouchEvent):
    """向设备发送触控事件"""
    try:
        device_manager.send_touch_event(serial, event.action, event.x, event.y)
        return {"status": "ok"}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@rest_router.post("/devices/{serial}/reconnect")
def reconnect_device(serial: str):
    """重新连接设备（清理旧连接 + 重新初始化 scrcpy）"""
    device_manager.reconnect_device(serial)
    return {"status": "reconnecting", "serial": serial}



# ==================== WebSocket 视频流 ====================


@ws_router.websocket("/ws/scrcpy/{serial}")
async def video_stream(websocket: WebSocket, serial: str):
    """
    WebSocket 端点：推送 H.264 原始视频流。
    
    前端使用 jmuxer 解码播放。
    """
    await websocket.accept()
    logger.info(f"视频流 WebSocket 已连接: {serial}")

    try:
        # 获取视频流生成器（阻塞IO，需要在线程池中运行）
        generator = device_manager.get_video_generator(serial)

        loop = asyncio.get_event_loop()

        # 将阻塞的 socket.recv 包装为异步
        def _next_chunk():
            try:
                return next(generator)
            except StopIteration:
                return None

        while True:
            # 在线程池中执行阻塞的 recv 调用
            chunk = await loop.run_in_executor(None, _next_chunk)
            if chunk is None:
                break
            await websocket.send_bytes(chunk)

    except WebSocketDisconnect:
        logger.info(f"视频流 WebSocket 断开: {serial}")
    except ValueError as e:
        logger.warning(f"视频流请求失败: {e}")
        try:
            await websocket.close(code=4004, reason=str(e))
        except Exception:
            pass
    except Exception as e:
        logger.error(f"视频流异常: {e}")
        try:
            await websocket.close(code=4000, reason="内部错误")
        except Exception:
            pass
