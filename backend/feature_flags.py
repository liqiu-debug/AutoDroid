"""Feature flag helpers for staged rollout."""
from __future__ import annotations

from typing import Optional

from sqlmodel import Session, select

from backend.models import SystemSetting

FLAG_NEW_STEP_MODEL = "new_step_model"
FLAG_IOS_EXECUTION = "ios_execution"
# WebSocket 客户端断开后立即中止对应执行（默认关闭，保持历史行为）
FLAG_WS_DISCONNECT_ABORT = "ws_disconnect_abort"
# Android 模型化智能巡检（默认关闭，真机验收后由系统设置开启）
FLAG_MODEL_INSPECTION = "model_inspection"
# Inspection graph/storage v2 rollout switches. These remain independently
# reversible while the legacy state/path fields are still supported.
FLAG_INSPECTION_IDENTITY_V2 = "inspection_identity_v2"
# Temporary source compatibility for code written during the initial rollout.
FLAG_INSPECTION_TSO_V2 = FLAG_INSPECTION_IDENTITY_V2
FLAG_CONTENT_ADDRESSED_ASSETS = "content_addressed_assets"
FLAG_TIERED_ASSET_RETENTION = "tiered_asset_retention"
FLAG_INSPECTION_SIMILARITY_CONVERGENCE = "inspection_similarity_convergence"
FLAG_INSPECTION_EXPLORATION_FAMILY_CONVERGENCE = (
    "inspection_exploration_family_convergence"
)
FLAG_INSPECTION_COVERAGE_SCHEDULER_V2 = "inspection_coverage_scheduler_v2"
# Versioned Haier Mall business-journey assessment.  Enabling this flag alone
# runs shadow evaluation; the existing scheduler flag additionally enables
# goal-directed action ordering.
FLAG_INSPECTION_BUSINESS_COVERAGE_V2 = "inspection_business_coverage_v2"
FLAG_INSPECTION_VISUAL_HOME_ACTIONS = "inspection_visual_home_actions"
FLAG_COMPATIBILITY_INSTALLED_REPLAY = "compatibility_installed_replay"
FLAG_COMPATIBILITY_LEGACY_COMPARE_CREATION = (
    "compatibility_legacy_compare_creation"
)

# 各开关的默认值表：调用点不再各自硬编码 default。
# 标准步骤模型已默认启用；在 SystemSetting 中显式写入 false 仍可关闭。
# 跨端 Runner 已成为唯一执行链路，不再受开关控制。iOS 执行保持默认关闭。
_FLAG_DEFAULTS = {
    FLAG_NEW_STEP_MODEL: True,
    FLAG_IOS_EXECUTION: False,
    FLAG_WS_DISCONNECT_ABORT: False,
    FLAG_MODEL_INSPECTION: False,
    # New inspections use instance-aware State identity and exploration
    # families by default.  Operators can still write an explicit false value
    # to roll either layer back independently.
    FLAG_INSPECTION_IDENTITY_V2: True,
    FLAG_CONTENT_ADDRESSED_ASSETS: False,
    FLAG_TIERED_ASSET_RETENTION: False,
    FLAG_INSPECTION_SIMILARITY_CONVERGENCE: False,
    FLAG_INSPECTION_EXPLORATION_FAMILY_CONVERGENCE: True,
    FLAG_INSPECTION_COVERAGE_SCHEDULER_V2: False,
    FLAG_INSPECTION_BUSINESS_COVERAGE_V2: False,
    FLAG_INSPECTION_VISUAL_HOME_ACTIONS: False,
    FLAG_COMPATIBILITY_INSTALLED_REPLAY: True,
    FLAG_COMPATIBILITY_LEGACY_COMPARE_CREATION: False,
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
