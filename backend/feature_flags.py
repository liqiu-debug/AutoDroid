"""Feature flag helpers for staged rollout."""
from __future__ import annotations

from typing import Optional

from sqlmodel import Session, select

from backend.models import SystemSetting

FLAG_NEW_STEP_MODEL = "new_step_model"
FLAG_CROSS_PLATFORM_RUNNER = "cross_platform_runner"
FLAG_IOS_EXECUTION = "ios_execution"

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


def is_flag_enabled(session: Session, key: str, default: bool = False) -> bool:
    return parse_bool_setting(get_setting_value(session, key), default=default)
