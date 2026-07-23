"""
系统设置 API

提供全局配置的读写接口和通知测试接口。
"""
from typing import List
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select
from pydantic import BaseModel

from backend.database import get_session
from backend.models import SystemSetting, User
from backend.api import deps
from backend.notification_service import NotificationService
from backend.feature_flags import (
    FLAG_COMPATIBILITY_INSTALLED_REPLAY,
    FLAG_COMPATIBILITY_LEGACY_COMPARE_CREATION,
    FLAG_CONTENT_ADDRESSED_ASSETS,
    FLAG_INSPECTION_EXPLORATION_FAMILY_CONVERGENCE,
    FLAG_INSPECTION_COVERAGE_SCHEDULER_V2,
    FLAG_INSPECTION_IDENTITY_V2,
    FLAG_INSPECTION_SIMILARITY_CONVERGENCE,
    FLAG_INSPECTION_VISUAL_HOME_ACTIONS,
    FLAG_IOS_EXECUTION,
    FLAG_MODEL_INSPECTION,
    FLAG_NEW_STEP_MODEL,
    FLAG_TIERED_ASSET_RETENTION,
    FLAG_WS_DISCONNECT_ABORT,
    is_flag_enabled,
    parse_bool_setting,
)

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


@router.get("/feature-flags")
def get_feature_flags(
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user),
):
    keys = (
        FLAG_COMPATIBILITY_INSTALLED_REPLAY,
        FLAG_COMPATIBILITY_LEGACY_COMPARE_CREATION,
        FLAG_NEW_STEP_MODEL,
        FLAG_IOS_EXECUTION,
        FLAG_WS_DISCONNECT_ABORT,
        FLAG_MODEL_INSPECTION,
        FLAG_INSPECTION_IDENTITY_V2,
        FLAG_INSPECTION_SIMILARITY_CONVERGENCE,
        FLAG_INSPECTION_EXPLORATION_FAMILY_CONVERGENCE,
        FLAG_INSPECTION_COVERAGE_SCHEDULER_V2,
        FLAG_INSPECTION_VISUAL_HOME_ACTIONS,
        FLAG_CONTENT_ADDRESSED_ASSETS,
        FLAG_TIERED_ASSET_RETENTION,
    )
    values = {key: is_flag_enabled(session, key) for key in keys}
    if not values[FLAG_MODEL_INSPECTION]:
        values[FLAG_INSPECTION_IDENTITY_V2] = False
    if not values[FLAG_INSPECTION_IDENTITY_V2]:
        values[FLAG_INSPECTION_SIMILARITY_CONVERGENCE] = False
        values[FLAG_INSPECTION_EXPLORATION_FAMILY_CONVERGENCE] = False
        values[FLAG_INSPECTION_COVERAGE_SCHEDULER_V2] = False
    if not values[FLAG_INSPECTION_COVERAGE_SCHEDULER_V2]:
        values[FLAG_INSPECTION_VISUAL_HOME_ACTIONS] = False
    if not values[FLAG_CONTENT_ADDRESSED_ASSETS]:
        values[FLAG_TIERED_ASSET_RETENTION] = False
    return values


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
    current_user: User = Depends(deps.get_current_user_no_token)
):
    """批量保存系统配置 (Upsert)"""
    items_by_key = {item.key: item for item in items}
    dependencies = {
        FLAG_MODEL_INSPECTION: (FLAG_INSPECTION_IDENTITY_V2,),
        FLAG_INSPECTION_IDENTITY_V2: (
            FLAG_INSPECTION_SIMILARITY_CONVERGENCE,
            FLAG_INSPECTION_EXPLORATION_FAMILY_CONVERGENCE,
            FLAG_INSPECTION_COVERAGE_SCHEDULER_V2,
        ),
        FLAG_INSPECTION_COVERAGE_SCHEDULER_V2: (
            FLAG_INSPECTION_VISUAL_HOME_ACTIONS,
        ),
        FLAG_CONTENT_ADDRESSED_ASSETS: (FLAG_TIERED_ASSET_RETENTION,),
    }

    def requested_flag(key: str) -> bool:
        item = items_by_key.get(key)
        if item is not None:
            return parse_bool_setting(item.value, default=False)
        return is_flag_enabled(session, key)

    for parent, children in dependencies.items():
        parent_enabled = requested_flag(parent)
        explicit_parent_disable = (
            parent in items_by_key and not parent_enabled
        )
        for child in children:
            child_item = items_by_key.get(child)
            child_enabled = requested_flag(child)
            if explicit_parent_disable:
                if child_item is not None or child_enabled:
                    items_by_key[child] = SettingItem(
                        key=child,
                        value="false",
                        description=(
                            child_item.description
                            if child_item is not None
                            else f"随 {parent} 关闭"
                        ),
                    )
                continue
            if child_item is not None and child_enabled and not parent_enabled:
                raise HTTPException(
                    status_code=422,
                    detail=f"{child} 依赖 {parent}，请先启用父功能",
                )

    for item in items_by_key.values():
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
    return {"message": "配置已保存", "count": len(items_by_key)}


@router.post("/test-notification")
def test_notification(
    req: TestNotificationRequest,
    current_user: User = Depends(deps.get_current_user_no_token)
):
    """发送测试通知到飞书群"""
    if not req.webhook_url:
        raise HTTPException(status_code=400, detail="Webhook 地址不能为空")

    result = NotificationService.send_test_message(req.webhook_url)
    if result["success"]:
        return result
    else:
        raise HTTPException(status_code=400, detail=result["message"])
