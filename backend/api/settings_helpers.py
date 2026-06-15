from sqlmodel import Session, select

from backend.models import SystemSetting


ALLOW_REGISTRATION_KEY = "allow_registration"


def is_registration_allowed(session: Session) -> bool:
    setting = session.exec(
        select(SystemSetting).where(SystemSetting.key == ALLOW_REGISTRATION_KEY)
    ).first()
    if setting is None:
        return True
    return str(setting.value).strip().lower() not in {"false", "0", "off", "no"}


def set_registration_allowed(session: Session, allowed: bool) -> None:
    setting = session.exec(
        select(SystemSetting).where(SystemSetting.key == ALLOW_REGISTRATION_KEY)
    ).first()
    value = "true" if allowed else "false"
    if setting:
        setting.value = value
        setting.description = "是否允许公开注册新用户"
        session.add(setting)
    else:
        session.add(
            SystemSetting(
                key=ALLOW_REGISTRATION_KEY,
                value=value,
                description="是否允许公开注册新用户",
            )
        )
    session.commit()
