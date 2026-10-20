"""App presence is independent of the host's USB/WDA transport and task leases."""
from datetime import datetime, timedelta

from fastapi import HTTPException
from sqlmodel import Session, select

from backend.models import Device

HEARTBEAT_TTL = timedelta(minutes=3)


def is_app_device(device: Device) -> bool:
    return device.connection_type == "autojs6" or (
        device.platform == "ios" and device.connection_type == "wda" and device.serial.startswith("ios:")
    )


def app_presence(device: Device, now=None) -> dict:
    now = now or datetime.now()
    seen = device.app_last_seen_at or device.updated_at or device.created_at
    reason = "LOGGED_OUT" if device.app_logged_out_at else "ONLINE" if seen and now - seen < HEARTBEAT_TTL else "TIMEOUT"
    return {"serial": device.serial, "status": reason, "last_seen_at": seen.isoformat() if seen else None,
            "executor_status": device.app_executor_status if reason == "ONLINE" else "UNKNOWN",
            "bound_device_serial": device.bound_device_serial}


def refresh_app_presence(session: Session) -> None:
    changed = False
    for device in session.exec(select(Device)).all():
        if not is_app_device(device):
            continue
        presence = app_presence(device)
        status = "OFFLINE" if presence["status"] != "ONLINE" else "BUSY" if presence["executor_status"] == "RUNNING" else "IDLE"
        if device.status != status:
            device.status = status
            session.add(device)
            changed = True
    if changed:
        session.commit()


def require_host_device(device: Device | None) -> None:
    if device and is_app_device(device):
        raise HTTPException(409, "此设备通过 App 接入，请在手机操作；Web 控制需要独立的 USB 或 WDA 无线连接。")
