from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_
from sqlmodel import Session, select

from backend.api import deps
from backend.api.settings_helpers import is_registration_allowed, set_registration_allowed
from backend.core.security import get_password_hash
from backend.database import get_session
from backend.models import User
from backend.schemas import RegistrationStatus, UserCreateByAdmin, UserRead, UserStatusUpdate

router = APIRouter()


@router.get("/registration-settings", response_model=RegistrationStatus)
def get_registration_settings(
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_admin_user),
) -> Any:
    return RegistrationStatus(allow_registration=is_registration_allowed(session))


@router.put("/registration-settings", response_model=RegistrationStatus)
def update_registration_settings(
    *,
    settings_in: RegistrationStatus,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_admin_user),
) -> Any:
    set_registration_allowed(session, settings_in.allow_registration)
    return RegistrationStatus(allow_registration=settings_in.allow_registration)


@router.get("/users", response_model=List[UserRead])
def list_users(
    *,
    search: Optional[str] = None,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_admin_user),
) -> Any:
    statement = select(User)
    query = (search or "").strip()
    if query:
        statement = statement.where(
            or_(
                User.username.contains(query),
                User.full_name.contains(query),
            )
        )
    statement = statement.order_by(User.created_at.desc())
    return session.exec(statement).all()


@router.post("/users", response_model=UserRead)
def create_user(
    *,
    user_in: UserCreateByAdmin,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_admin_user),
) -> Any:
    username = user_in.username.strip()
    if not username:
        raise HTTPException(status_code=400, detail="Username is required")

    existing = session.exec(select(User).where(User.username == username)).first()
    if existing:
        raise HTTPException(status_code=400, detail="The user with this username already exists")

    user = User(
        username=username,
        hashed_password=get_password_hash(user_in.initial_password),
        full_name=(user_in.full_name or "").strip() or None,
        email=(user_in.email or "").strip() or None,
        role="user",
        is_active=True,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


@router.patch("/users/{user_id}/status", response_model=UserRead)
def update_user_status(
    *,
    user_id: int,
    status_in: UserStatusUpdate,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_admin_user),
) -> Any:
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    if user.id == current_user.id and not status_in.is_active:
        raise HTTPException(status_code=400, detail="You cannot deactivate your own account")

    user.is_active = status_in.is_active
    session.add(user)
    session.commit()
    session.refresh(user)
    return user
