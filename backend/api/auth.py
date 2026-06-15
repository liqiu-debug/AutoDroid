from datetime import timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlmodel import Session, select

from backend.database import get_session
from backend.models import User
from backend.core.security import verify_password, create_access_token, get_password_hash, ACCESS_TOKEN_EXPIRE_MINUTES
from backend.api import deps
from backend.api.settings_helpers import is_registration_allowed
from backend.schemas import CurrentUserRead, PasswordChange, RegistrationStatus, UserRead, UserRegister

router = APIRouter()


@router.post("/token")
def login_access_token(
    form_data: OAuth2PasswordRequestForm = Depends(),
    session: Session = Depends(get_session)
) -> Any:
    """
    OAuth2 compatible token login, get an access token for future requests
    """
    statement = select(User).where(User.username == form_data.username)
    user = session.exec(statement).first()
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(status_code=400, detail="Incorrect username or password")
    
    if not user.is_active:
        raise HTTPException(status_code=400, detail="Inactive user")
        
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user.username}, expires_delta=access_token_expires
    )
    return {
        "access_token": access_token,
        "token_type": "bearer",
    }


@router.get("/registration-status", response_model=RegistrationStatus)
def read_registration_status(
    session: Session = Depends(get_session),
) -> Any:
    """
    Public registration switch used by login/register pages.
    """
    return RegistrationStatus(allow_registration=is_registration_allowed(session))


@router.get("/users/me", response_model=CurrentUserRead)
def read_users_me(
    current_user: User = Depends(deps.get_current_active_user),
) -> Any:
    """
    Get current user.
    """
    return current_user


@router.put("/password")
def change_password(
    *,
    password_in: PasswordChange,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_active_user),
) -> Any:
    """
    Change password for the current user after verifying the current password.
    """
    if not verify_password(password_in.current_password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    current_user.hashed_password = get_password_hash(password_in.new_password)
    session.add(current_user)
    session.commit()
    return {"message": "Password updated successfully"}


# Legacy admin-create endpoint kept for compatibility. New UI uses /admin/users.
@router.post("/users", response_model=UserRead)
def create_user(
    *,
    session: Session = Depends(get_session),
    username: str,
    password: str,
    full_name: Optional[str] = None,
    email: Optional[str] = None,
    current_user: User = Depends(deps.get_current_admin_user),
) -> Any:
    """
    Create new user (only for admin or open registration).
    For now, let's limit to existing users (e.g. admin) creating new users, 
    or just open it up if needed. The Requirement said "admin only or open".
    Let's check current_user role.
    """
    statement = select(User).where(User.username == username)
    user = session.exec(statement).first()
    if user:
        raise HTTPException(
            status_code=400,
            detail="The user with this username already exists in the system",
        )
    
    user = User(
        username=username,
        hashed_password=get_password_hash(password),
        full_name=full_name,
        email=email,
        role="user",
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


@router.post("/register", response_model=UserRead)
def register_user(
    *,
    session: Session = Depends(get_session),
    user_in: UserRegister,
) -> Any:
    """
    Open registration for new users.
    """
    if not is_registration_allowed(session):
        raise HTTPException(status_code=403, detail="Registration is disabled. Please contact administrator.")

    user = session.exec(select(User).where(User.username == user_in.username)).first()
    if user:
        raise HTTPException(
            status_code=400,
            detail="The user with this username already exists",
        )
    user = User(
        username=user_in.username,
        hashed_password=get_password_hash(user_in.password),
        full_name=user_in.name,
        role="user",
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user
