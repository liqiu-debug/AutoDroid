"""
Scrcpy 视频流 API 路由

REST 与 WebSocket 分离，便于在主应用中分别挂载：
- REST canonical: /api/stream/devices/*
- REST legacy: /devices* /api/devices*（兼容保留）
- WebSocket: /ws/scrcpy/{serial}

iOS 实时画面（WDA MJPEG）：
- WebSocket: /ws/ios-mjpeg/{serial}（每条二进制消息为一帧完整 JPEG）
- REST: /api/stream/ios-mjpeg/{serial}（multipart/x-mixed-replace，可直接用于 <img>）
"""
import asyncio
import logging
from typing import Generator, Iterable, Set

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlmodel import Session, col, select

from backend.api.deps import (
    authenticate_token,
    get_current_user,
    get_current_user_allow_query_token,
)
from backend.database import get_session
from backend.models import Device

from . import ios_mjpeg
from .ios_mjpeg import IOSMjpegStreamError, MjpegClient
from .manager import (
    device_manager,
    get_stream_profile_state,
    set_stream_profile_override,
)

logger = logging.getLogger(__name__)

rest_router = APIRouter()
ws_router = APIRouter()
router = rest_router


def _visible_stream_serials(
    session: Session,
    current_user,
    serials: Iterable[str],
) -> Set[str]:
    """归属过滤（与 GET /api/devices/ 同口径）：admin 全量，普通用户仅本人设备。

    未登记到 device 表的 serial 对普通用户不可见（与列表接口的未认领设备口径一致）。
    """
    from backend.api.devices import filter_devices_for_user

    wanted = [str(s) for s in (serials or []) if str(s)]
    uid = getattr(current_user, "id", None)
    role = getattr(current_user, "role", None)
    if uid is None or role == "admin":
        return set(wanted)
    if not wanted:
        return set()
    devices = session.exec(select(Device).where(col(Device.serial).in_(wanted))).all()
    return {d.serial for d in filter_devices_for_user(devices, current_user)}


def _require_stream_visible(session: Session, current_user, serial: str) -> None:
    """REST 端点归属校验（与 GET /stream/devices/{serial} 同口径：不可见按 404）。"""
    if serial not in _visible_stream_serials(session, current_user, [serial]):
        raise HTTPException(status_code=404, detail=f"设备 {serial} 未找到")


def _extract_ws_token(websocket: WebSocket) -> str:
    """WS 握手 token：Authorization 头（网关/桌面 ws_proxy 转发）或 ?token=（浏览器）。"""
    raw = str(websocket.headers.get("authorization") or "")
    if raw.lower().startswith("bearer "):
        return raw[7:].strip()
    return str(websocket.query_params.get("token") or "").strip()


def _authenticate_stream_user(websocket: WebSocket, session: Session):
    """WS 鉴权（accept 之前执行）：失败返回 None（握手拒绝）。"""
    token = _extract_ws_token(websocket)
    if not token:
        return None
    try:
        return authenticate_token(token, session)
    except HTTPException:
        return None


async def _reject_ws(websocket: WebSocket, code: int, reason: str) -> None:
    """accept 之前拒绝握手（ASGI 约定：close 先于 accept = 握手失败）。"""
    try:
        await websocket.close(code=code, reason=reason)
    except Exception:
        pass


# ==================== REST API ====================


@rest_router.get("/devices")
def list_devices(
    session: Session = Depends(get_session),
    current_user=Depends(get_current_user),
):
    """获取所有已连接设备列表（按归属过滤：admin 全量，普通用户仅本人设备）"""
    items = device_manager.get_devices_list()
    visible = _visible_stream_serials(session, current_user, (item.get("serial") for item in items))
    return [item for item in items if str(item.get("serial") or "") in visible]


@rest_router.get("/devices/{serial}")
def get_device(
    serial: str,
    session: Session = Depends(get_session),
    current_user=Depends(get_current_user),
):
    """获取单个设备信息（按归属过滤）"""
    if serial not in _visible_stream_serials(session, current_user, [serial]):
        raise HTTPException(status_code=404, detail=f"设备 {serial} 未找到")
    info = device_manager.get_device(serial)
    if not info:
        raise HTTPException(status_code=404, detail=f"设备 {serial} 未找到")
    return info


class TouchEvent(BaseModel):
    """触控事件请求体"""
    action: int = 0  # 兼容语义：0=tap, 1=抬起, 2=移动
    x: int
    y: int
    method: str = "scrcpy"  # scrcpy=control socket, adb=adb shell input tap


@rest_router.post("/devices/{serial}/touch")
def send_touch(
    serial: str,
    event: TouchEvent,
    session: Session = Depends(get_session),
    current_user=Depends(get_current_user),
):
    """向设备发送触控事件（鉴权 + 归属过滤，与 GET /stream/devices 同口径）"""
    _require_stream_visible(session, current_user, serial)
    try:
        device_manager.send_touch_event(serial, event.action, event.x, event.y, method=event.method)
        return {"status": "ok"}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@rest_router.post("/devices/{serial}/reconnect")
def reconnect_device(
    serial: str,
    session: Session = Depends(get_session),
    current_user=Depends(get_current_user),
):
    """重新连接设备（清理旧连接 + 重新初始化 scrcpy）"""
    _require_stream_visible(session, current_user, serial)
    device_manager.reconnect_device(serial)
    return {"status": "reconnecting", "serial": serial}


class StreamProfileRequest(BaseModel):
    """投屏清晰度档位请求体；auto/default/空串 = 恢复按 serial 形态的默认档"""
    profile: str


@rest_router.get("/devices/{serial}/stream-profile")
def get_stream_profile(
    serial: str,
    session: Session = Depends(get_session),
    current_user=Depends(get_current_user),
):
    """查询设备当前生效的投屏档位（profile/source/params）"""
    _require_stream_visible(session, current_user, serial)
    return {"serial": serial, **get_stream_profile_state(serial)}


@rest_router.post("/devices/{serial}/stream-profile")
def set_stream_profile(
    serial: str,
    req: StreamProfileRequest,
    session: Session = Depends(get_session),
    current_user=Depends(get_current_user),
):
    """
    设置设备投屏清晰度档位（hd/standard/smooth），设备流在管理中时立即重启生效。

    档位为内存态覆盖，平台重启后回到默认档；前端按设备记忆用户选择并在
    需要时重新下发。
    """
    _require_stream_visible(session, current_user, serial)
    profile = str(req.profile or "").strip().lower()
    try:
        set_stream_profile_override(serial, None if profile in ("", "auto", "default") else profile)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    restarting = bool(device_manager.get_device(serial))
    if restarting:
        device_manager.reconnect_device(serial)
    return {
        "serial": serial,
        "status": "reconnecting" if restarting else "saved",
        **get_stream_profile_state(serial),
    }



# ==================== WebSocket 视频流 ====================


@ws_router.websocket("/ws/scrcpy/{serial}")
async def video_stream(websocket: WebSocket, serial: str, session: Session = Depends(get_session)):
    """
    WebSocket 端点：推送 H.264 原始视频流。

    前端使用 jmuxer 解码播放。
    鉴权在 accept 之前完成（token 走 Authorization 头或 ?token=），归属过滤同 REST。
    """
    user = _authenticate_stream_user(websocket, session)
    if user is None:
        await _reject_ws(websocket, 4401, "登录状态无效或已过期，请重新登录")
        return
    if serial not in _visible_stream_serials(session, user, [serial]):
        await _reject_ws(websocket, 4403, f"设备 {serial} 未找到")
        return
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


# ==================== iOS MJPEG 实时画面 ====================


def _open_ios_mjpeg_client(serial: str) -> MjpegClient:
    """
    解析上游地址、按需建立 relay 并注册一个 MJPEG 客户端。

    阻塞调用（可能启动 tidevice relay / 连接上游 / 调用 WDA settings），
    异步上下文中需放入线程池执行。
    """
    from sqlmodel import Session

    from backend.database import engine

    with Session(engine) as session:
        upstream = ios_mjpeg.resolve_ios_mjpeg_upstream(session, serial)
        stream_settings = ios_mjpeg.resolve_ios_mjpeg_stream_settings(session, serial)

    def _configure() -> None:
        # 尽力设置帧率/质量，失败不阻断推流
        ios_mjpeg.apply_wda_mjpeg_settings(upstream.wda_url, stream_settings)

    return ios_mjpeg.ios_mjpeg_stream_manager.open_client(upstream, configure=_configure)


@ws_router.websocket("/ws/ios-mjpeg/{serial}")
async def ios_mjpeg_stream_ws(
    websocket: WebSocket, serial: str, session: Session = Depends(get_session)
):
    """
    WebSocket 端点：推送 iOS 实时画面（WDA MJPEG）。

    每条二进制消息为一帧完整 JPEG（前端 blob -> img 渲染）。
    关闭码：4401/4403=鉴权/归属拒绝（握手阶段），4005=WDA/MJPEG 上游不可用
    （reason 含 P1005_WDA_UNAVAILABLE），4000=内部错误，1000=上游正常结束。
    """
    user = _authenticate_stream_user(websocket, session)
    if user is None:
        await _reject_ws(websocket, 4401, "登录状态无效或已过期，请重新登录")
        return
    if serial not in _visible_stream_serials(session, user, [serial]):
        await _reject_ws(websocket, 4403, f"设备 {serial} 未找到")
        return
    await websocket.accept()
    logger.info(f"iOS MJPEG WebSocket 已连接: {serial}")

    loop = asyncio.get_event_loop()
    try:
        client = await loop.run_in_executor(None, _open_ios_mjpeg_client, serial)
    except IOSMjpegStreamError as e:
        logger.warning(f"iOS MJPEG 流建立失败: serial={serial} error={e}")
        try:
            await websocket.close(code=4005, reason=str(e)[:120])
        except Exception:
            pass
        return
    except Exception as e:
        logger.exception(f"iOS MJPEG 流建立异常: serial={serial}")
        try:
            await websocket.close(code=4000, reason=f"内部错误: {e}"[:120])
        except Exception:
            pass
        return

    try:
        while True:
            frame = await loop.run_in_executor(None, client.next_frame, 30.0)
            if frame is None:
                break
            await websocket.send_bytes(frame)
    except WebSocketDisconnect:
        logger.info(f"iOS MJPEG WebSocket 断开: {serial}")
    except Exception as e:
        logger.error(f"iOS MJPEG 流异常: serial={serial} error={e}")
    finally:
        client.close()
        try:
            await websocket.close(code=1000, reason="stream ended")
        except Exception:
            pass


@rest_router.get("/ios-mjpeg/{serial}")
def ios_mjpeg_stream_http(
    serial: str,
    session: Session = Depends(get_session),
    current_user=Depends(get_current_user_allow_query_token),
):
    """
    HTTP 端点：以 multipart/x-mixed-replace 透传 iOS 实时画面，
    浏览器可直接 <img src="/api/stream/ios-mjpeg/{serial}"> 渲染。

    鉴权同 GET /stream/devices（<img src> 无法带 Authorization 头，兼容 ?token=）。
    错误：503=WDA/MJPEG 上游不可用（detail 含 P1005_WDA_UNAVAILABLE），500=内部错误。
    """
    _require_stream_visible(session, current_user, serial)
    try:
        client = _open_ios_mjpeg_client(serial)
    except IOSMjpegStreamError as e:
        logger.warning(f"iOS MJPEG 流建立失败: serial={serial} error={e}")
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.exception(f"iOS MJPEG 流建立异常: serial={serial}")
        raise HTTPException(status_code=500, detail=f"iOS MJPEG 流建立失败: {e}")

    boundary = "autodroid-mjpeg"

    def _multipart() -> Generator[bytes, None, None]:
        try:
            for frame in client.frames():
                yield (
                    f"--{boundary}\r\n"
                    f"Content-Type: image/jpeg\r\n"
                    f"Content-Length: {len(frame)}\r\n\r\n"
                ).encode("ascii") + frame + b"\r\n"
        finally:
            # 客户端断开（GeneratorExit）或上游结束时释放资源
            client.close()

    return StreamingResponse(
        _multipart(),
        media_type=f"multipart/x-mixed-replace; boundary={boundary}",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
