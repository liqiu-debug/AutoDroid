"""
系统设置 API

提供全局配置的读写接口和通知测试接口。
"""
from typing import List, Dict, Any
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select
from pydantic import BaseModel

from backend.database import get_session
from backend.models import SystemSetting, User
from backend.api import deps
from backend.notification_service import NotificationService

router = APIRouter()


class SettingItem(BaseModel):
    key: str
    value: str
    description: str = ""


class SettingResponse(BaseModel):
    key: str
    value: str
    description: str = ""


class TestNotificationRequest(BaseModel):
    webhook_url: str


@router.get("/", response_model=List[SettingResponse])
def get_settings(
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user)
):
    """获取所有系统配置"""
    settings = session.exec(select(SystemSetting)).all()
    return [
        SettingResponse(
            key=s.key,
            value=s.value,
            description=s.description or ""
        )
        for s in settings
    ]


@router.post("/")
def save_settings(
    items: List[SettingItem],
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user)
):
    """批量保存系统配置 (Upsert)"""
    for item in items:
        existing = session.exec(
            select(SystemSetting).where(SystemSetting.key == item.key)
        ).first()
        if existing:
            existing.value = item.value
            if item.description:
                existing.description = item.description
            session.add(existing)
        else:
            new_setting = SystemSetting(
                key=item.key,
                value=item.value,
                description=item.description
            )
            session.add(new_setting)
    session.commit()
    return {"message": "配置已保存", "count": len(items)}


@router.post("/test-notification")
def test_notification(
    req: TestNotificationRequest,
    current_user: User = Depends(deps.get_current_user)
):
    """发送测试通知到飞书群"""
    if not req.webhook_url:
        raise HTTPException(status_code=400, detail="Webhook 地址不能为空")

    result = NotificationService.send_test_message(req.webhook_url)
    if result["success"]:
        return result
    else:
        raise HTTPException(status_code=400, detail=result["message"])
