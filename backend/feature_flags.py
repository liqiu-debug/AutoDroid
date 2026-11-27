"""Feature flag helpers for staged rollout."""
from __future__ import annotations

from typing import Any, Dict, Optional

from sqlmodel import Session, select

from backend.models import SystemSetting

FLAG_NEW_STEP_MODEL = "new_step_model"
FLAG_IOS_EXECUTION = "ios_execution"
# WebSocket 客户端断开后立即中止对应执行（默认关闭，保持历史行为）
FLAG_WS_DISCONNECT_ABORT = "ws_disconnect_abort"
FLAG_CONTENT_ADDRESSED_ASSETS = "content_addressed_assets"
FLAG_TIERED_ASSET_RETENTION = "tiered_asset_retention"
FLAG_DRIVER_POOL = "driver_pool_enabled"
FLAG_RECORDING_ENABLE_RESOURCE_ID = "recording_enable_resource_id"
FLAG_RECORDING_ENABLE_CLASS_NAME = "recording_enable_class_name"
SETTING_RECORDING_LOCATOR_PRIORITY = "recording_locator_priority"
RECORDING_PRIORITY_SEMANTIC_FIRST = "semantic_first"
RECORDING_PRIORITY_ID_FIRST = "id_first"
VALID_RECORDING_LOCATOR_PRIORITIES = (
    RECORDING_PRIORITY_SEMANTIC_FIRST,
    RECORDING_PRIORITY_ID_FIRST,
)

# 各开关的默认值表：调用点不再各自硬编码 default。
# 标准步骤模型已默认启用；在 SystemSetting 中显式写入 false 仍可关闭。
# 跨端 Runner 已成为唯一执行链路，不再受开关控制。iOS 执行保持默认关闭。
_FLAG_DEFAULTS = {
    FLAG_NEW_STEP_MODEL: True,
    FLAG_IOS_EXECUTION: False,
    FLAG_WS_DISCONNECT_ABORT: False,
    FLAG_CONTENT_ADDRESSED_ASSETS: False,
    FLAG_TIERED_ASSET_RETENTION: False,
    FLAG_DRIVER_POOL: False,
    FLAG_RECORDING_ENABLE_RESOURCE_ID: False,
    FLAG_RECORDING_ENABLE_CLASS_NAME: False,
}

_UNSET = object()

_TRUE_VALUES = {"1", "true", "yes", "on", "enabled"}
_FALSE_VALUES = {"0", "false", "no", "off", "disabled"}


def parse_bool_setting(value: Optional[str], default: bool = False) -> bool:
    """Parse bool-like setting values safely."""
    if value is None:
        return default

    normalized = str(value).strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    return default


def get_setting_value(session: Session, key: str) -> Optional[str]:
    setting = session.exec(select(SystemSetting).where(SystemSetting.key == key)).first()
    return setting.value if setting else None


def set_setting_value(
    session: Session,
    key: str,
    value: str,
    description: Optional[str] = None,
) -> None:
    """Upsert 单条 SystemSetting。"""
    setting = session.exec(select(SystemSetting).where(SystemSetting.key == key)).first()
    if setting:
        setting.value = value
        if description:
            setting.description = description
        session.add(setting)
    else:
        session.add(SystemSetting(key=key, value=value, description=description))
    session.commit()


def delete_setting_value(session: Session, key: str) -> bool:
    """删除单条 SystemSetting；存在并删除返回 True，不存在返回 False（幂等）。"""
    setting = session.exec(select(SystemSetting).where(SystemSetting.key == key)).first()
    if not setting:
        return False
    session.delete(setting)
    session.commit()
    return True


def is_flag_enabled(session: Session, key: str, default=_UNSET) -> bool:
    """Read a feature flag with per-key defaults.

    未显式传入 default 时使用 `_FLAG_DEFAULTS` 表；DB 中显式配置的值
    （true/false）始终优先于默认值。
    """
    if default is _UNSET:
        default = _FLAG_DEFAULTS.get(key, False)
    return parse_bool_setting(get_setting_value(session, key), default=bool(default))


def _coerce_bool_value(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return parse_bool_setting(str(value), default=default)


def normalize_recording_locator_priority(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text == RECORDING_PRIORITY_ID_FIRST:
        return RECORDING_PRIORITY_ID_FIRST
    return RECORDING_PRIORITY_SEMANTIC_FIRST


def parse_recording_locator_config(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Normalize recording locator policy from SystemSetting dict or request payload."""
    source = raw if isinstance(raw, dict) else {}
    enable_id_raw = (
        source.get("enable_resource_id")
        if "enable_resource_id" in source
        else source.get(FLAG_RECORDING_ENABLE_RESOURCE_ID)
    )
    enable_class_raw = (
        source.get("enable_class_name")
        if "enable_class_name" in source
        else source.get(FLAG_RECORDING_ENABLE_CLASS_NAME)
    )
    priority_raw = (
        source.get("priority")
        if "priority" in source
        else source.get(SETTING_RECORDING_LOCATOR_PRIORITY)
    )
    return {
        "enable_resource_id": _coerce_bool_value(enable_id_raw, default=False),
        "enable_class_name": _coerce_bool_value(enable_class_raw, default=False),
        "priority": normalize_recording_locator_priority(priority_raw),
    }


def get_recording_locator_config(session: Session) -> Dict[str, Any]:
    """Read recording locator policy from SystemSetting."""
    return {
        "enable_resource_id": is_flag_enabled(session, FLAG_RECORDING_ENABLE_RESOURCE_ID),
        "enable_class_name": is_flag_enabled(session, FLAG_RECORDING_ENABLE_CLASS_NAME),
        "priority": normalize_recording_locator_priority(
            get_setting_value(session, SETTING_RECORDING_LOCATOR_PRIORITY)
        ),
    }
