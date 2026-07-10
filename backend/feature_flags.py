"""Feature flag helpers for staged rollout."""
from __future__ import annotations

from typing import Optional

from sqlmodel import Session, select

from backend.models import SystemSetting

FLAG_NEW_STEP_MODEL = "new_step_model"
FLAG_CROSS_PLATFORM_RUNNER = "cross_platform_runner"
FLAG_IOS_EXECUTION = "ios_execution"
# WebSocket 客户端断开后立即中止对应执行（默认关闭，保持历史行为）
FLAG_WS_DISCONNECT_ABORT = "ws_disconnect_abort"

# 各开关的默认值表：调用点不再各自硬编码 default。
# 标准步骤模型与跨端 Runner 已默认启用；在 SystemSetting 中显式写入
# false 仍可关闭（保留回滚通道）。iOS 执行保持默认关闭。
_FLAG_DEFAULTS = {
    FLAG_NEW_STEP_MODEL: True,
    FLAG_CROSS_PLATFORM_RUNNER: True,
    FLAG_IOS_EXECUTION: False,
    FLAG_WS_DISCONNECT_ABORT: False,
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


def is_flag_enabled(session: Session, key: str, default=_UNSET) -> bool:
    """Read a feature flag with per-key defaults.

    未显式传入 default 时使用 `_FLAG_DEFAULTS` 表；DB 中显式配置的值
    （true/false）始终优先于默认值。
    """
    if default is _UNSET:
        default = _FLAG_DEFAULTS.get(key, False)
    return parse_bool_setting(get_setting_value(session, key), default=bool(default))
