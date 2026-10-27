"""桌面客户端设备归属 API（客户端模式本地引擎上报）。

- POST /register：按 serial upsert Device 并认领归属（owner_id）；
  已被他人认领的设备返回 409。
- POST /heartbeat：批量刷新本人设备 last_seen_at。
- POST /logout：本人设备（或指定子集）status=OFFLINE。

设备归属：普通用户仅可见/可操作 owner_id 或 app_owner_id 为自己的设备，
admin 可见全部；存量无主设备由 backfill_legacy_device_owners 回填给 admin。
"""
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, col, select

from backend.api.deps import get_current_user
from backend.api.devices import _build_device_reads
from backend.database import get_session
from backend.models import Device, User
from backend.schemas import DeviceRead

router = APIRouter()


class ClientDeviceRegisterRequest(BaseModel):
    platform: str
    serial: str
    model: str
    brand: str
    android_version: str
    os_version: str
    resolution: str
    custom_name: Optional[str] = None
    connection_type: Optional[str] = None
    client_instance_id: Optional[str] = None


class ClientDeviceHeartbeatRequest(BaseModel):
    serials: List[str]


class ClientDeviceLogoutRequest(BaseModel):
    serials: Optional[List[str]] = None


@router.post("/register", response_model=DeviceRead)
def register_client_device(
    payload: ClientDeviceRegisterRequest,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """按 serial 认领/刷新一台客户端设备，返回 DeviceRead（与列表口径一致）。"""
    serial = str(payload.serial or "").strip()
    if not serial:
        raise HTTPException(status_code=400, detail="设备序列号不能为空")
    platform = str(payload.platform or "").strip().lower() or "android"

    now = datetime.now()
    device = session.exec(select(Device).where(Device.serial == serial)).first()
    if device and device.owner_id is not None and device.owner_id != current_user.id:
        raise HTTPException(status_code=409, detail="设备已被其他成员注册")

    if not device:
        device = Device(serial=serial, status="IDLE")

    # 归属 + 元数据刷新：必填字段直接覆盖，可选字段仅在显式提供时写入
    device.owner_id = current_user.id
    device.platform = platform
    device.model = str(payload.model or "Unknown")
    device.brand = str(payload.brand or "")
    device.android_version = str(payload.android_version or "")
    device.os_version = str(payload.os_version or "")
    device.resolution = str(payload.resolution or "")
    if payload.custom_name is not None:
        device.custom_name = str(payload.custom_name)
    if payload.connection_type is not None:
        device.connection_type = str(payload.connection_type)
    if payload.client_instance_id is not None:
        device.client_instance_id = str(payload.client_instance_id)
    device.last_seen_at = now
    device.updated_at = now
    # 注册即存活：把超时/登出的 OFFLINE 拉回 IDLE（客户端重启重新上报场景）
    if str(device.status or "").strip().upper() == "OFFLINE":
        device.status = "IDLE"

    session.add(device)
    session.commit()
    session.refresh(device)
    reads = _build_device_reads(session, [device])
    return reads[0] if reads else DeviceRead.model_validate(device)


@router.post("/heartbeat")
def heartbeat_client_devices(
    payload: ClientDeviceHeartbeatRequest,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """仅更新本人设备的 last_seen_at，返回 {updated: n}。"""
    serials = [str(s).strip() for s in (payload.serials or []) if str(s).strip()]
    if not serials:
        return {"updated": 0}

    now = datetime.now()
    rows = session.exec(
        select(Device).where(
            col(Device.serial).in_(serials), Device.owner_id == current_user.id
        )
    ).all()

    updated = 0
    for device in rows:
        device.last_seen_at = now
        device.updated_at = now
        # 心跳即存活：插拔回来/超时恢复后由心跳拉回 IDLE
        if str(device.status or "").strip().upper() == "OFFLINE":
            device.status = "IDLE"
        session.add(device)
        updated += 1

    if updated:
        session.commit()
    return {"updated": updated}


@router.post("/logout")
def logout_client_devices(
    payload: ClientDeviceLogoutRequest,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """本人设备（serials 缺省=全部，指定则为子集）置 OFFLINE，返回 {updated: n}。"""
    serials: Optional[List[str]] = None
    if payload.serials is not None:
        serials = [str(s).strip() for s in payload.serials if str(s).strip()]
        if not serials:
            return {"updated": 0}

    now = datetime.now()
    statement = select(Device).where(Device.owner_id == current_user.id)
    if serials is not None:
        statement = statement.where(col(Device.serial).in_(serials))
    devices = session.exec(statement).all()
    for device in devices:
        device.status = "OFFLINE"
        device.updated_at = now
        session.add(device)
    if devices:
        session.commit()
    return {"updated": len(devices)}
