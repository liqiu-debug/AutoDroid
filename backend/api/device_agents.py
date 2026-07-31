"""远程设备接入点（Device Agent）API 与 Agent WebSocket 端点。

- ``WS /ws/device-agent``：B 机 Agent 的反向隧道长连接，
  握手用 ``Authorization: Bearer <API Token>`` 头鉴权（机器凭证，见 backend/api/tokens.py）。
- ``GET /api/device-agents/``：接入点列表（前端设备中心展示）。
- ``DELETE /api/device-agents/{agent_id}``：删除离线接入点并释放隧道端口映射。
- ``GET /api/device-agents/agent-script``：下载 Agent 脚本。
"""
import json
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlmodel import Session, select
from starlette.websockets import WebSocket

from backend.api.deps import API_TOKEN_LAST_USED_THROTTLE_SECONDS, get_current_user
from backend.core.api_tokens import hash_api_token, is_api_token, verify_api_token
from backend.database import engine, get_session
from backend.device_agents import tunnel_hub
from backend.models import ApiToken, RemoteAgent, RemoteAgentDevice, User
from backend.paths import project_path

logger = logging.getLogger(__name__)

router = APIRouter()
ws_router = APIRouter()

AGENT_SCRIPT_PATH = project_path("scripts", "device_agent.py")


def _authenticate_ws_api_token(websocket: WebSocket) -> Optional[str]:
    """校验 WS 握手头中的 API Token，返回所属用户名；失败返回 None。

    仅接受机器凭证（adk_ 前缀），不接受 JWT：Agent 是长驻进程，
    不应依赖会过期的登录态。
    """
    auth_header = websocket.headers.get("authorization") or ""
    parts = auth_header.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    if not is_api_token(token):
        return None

    with Session(engine) as session:
        token_hash = hash_api_token(token)
        api_token = session.exec(
            select(ApiToken).where(ApiToken.token_hash == token_hash)
        ).first()
        if (
            api_token is None
            or not verify_api_token(token, api_token.token_hash)
            or not api_token.is_active
        ):
            return None
        user = session.get(User, api_token.user_id)
        if user is None or not user.is_active:
            return None

        now = datetime.now()
        if (
            api_token.last_used_at is None
            or (now - api_token.last_used_at).total_seconds() > API_TOKEN_LAST_USED_THROTTLE_SECONDS
        ):
            api_token.last_used_at = now
            session.add(api_token)
            session.commit()
        return user.username


@ws_router.websocket("/ws/device-agent")
async def device_agent_ws(websocket: WebSocket):
    """Agent 反向隧道长连接：鉴权通过后交由 TunnelHub 托管。"""
    username = _authenticate_ws_api_token(websocket)
    await websocket.accept()
    if username is None:
        try:
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "error",
                        "message": (
                            "鉴权失败：请在启动 Agent 时提供有效的 API Token"
                            "（账号设置页生成，Authorization: Bearer adk_...）"
                        ),
                    },
                    ensure_ascii=False,
                )
            )
        except Exception:
            pass
        await websocket.close(code=4401)
        return
    await tunnel_hub.handle_session(websocket, token_user=username)


@router.get("/")
def list_device_agents(current_user=Depends(get_current_user)):
    """接入点列表（数据库记录 + 在线会话状态叠加）。"""
    return {"items": tunnel_hub.list_agents()}


@router.get("/agent-script")
def download_agent_script(current_user=Depends(get_current_user)):
    """下载设备接入助手脚本（B 机运行）。"""
    if not AGENT_SCRIPT_PATH.exists():
        raise HTTPException(status_code=404, detail="Agent 脚本缺失，请检查服务端 scripts/device_agent.py")
    return FileResponse(
        str(AGENT_SCRIPT_PATH),
        media_type="text/x-python",
        filename="device_agent.py",
    )


@router.delete("/{agent_id}")
def delete_device_agent(
    agent_id: int,
    session: Session = Depends(get_session),
    current_user=Depends(get_current_user),
):
    """删除离线接入点及其设备端口映射（释放隧道端口）。"""
    agent = session.get(RemoteAgent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="接入点不存在")
    if tunnel_hub.is_agent_online(agent.name):
        raise HTTPException(
            status_code=400,
            detail="接入点当前在线，无法删除；请先停止 B 机上的 Agent 进程",
        )

    # commit 后 ORM 实例会过期/删除，提前取出用于响应与日志的字段
    agent_name = str(agent.name)
    operator = str(getattr(current_user, "username", "?") or "?")
    mappings = session.exec(
        select(RemoteAgentDevice).where(RemoteAgentDevice.agent_id == agent_id)
    ).all()
    released_ports = sorted(int(m.tunnel_port) for m in mappings)
    for mapping in mappings:
        session.delete(mapping)
    session.delete(agent)
    session.commit()

    logger.info(
        "删除远程接入点: id=%s name=%s released_ports=%s by=%s",
        agent_id,
        agent_name,
        released_ports,
        operator,
    )
    return {
        "message": f"接入点 {agent_name} 已删除",
        "released_ports": released_ports,
    }
