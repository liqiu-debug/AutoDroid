from datetime import datetime
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
from sqlmodel import Session, select

from backend.database import get_session
from backend.models import ApiToken, User
from backend.core.security import SECRET_KEY, ALGORITHM
from backend.core.api_tokens import hash_api_token, is_api_token, verify_api_token

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/token")

# last_used_at 写库节流：距上次更新超过该秒数才写，避免每个请求都写库
API_TOKEN_LAST_USED_THROTTLE_SECONDS = 60


def _authenticate_api_token(
    request: Optional[Request],
    token: str,
    session: Session,
    credentials_exception: HTTPException,
) -> User:
    """校验 API Token（adk_ 前缀）并返回属主用户。"""
    token_hash = hash_api_token(token)
    api_token = session.exec(
        select(ApiToken).where(ApiToken.token_hash == token_hash)
    ).first()
    # 命中后再做一次恒时比较，避免依赖数据库比较行为
    if (
        api_token is None
        or not verify_api_token(token, api_token.token_hash)
        or not api_token.is_active
    ):
        raise credentials_exception

    user = session.get(User, api_token.user_id)
    if user is None or not user.is_active:
        raise credentials_exception

    if request is not None:
        request.state.auth_via_api_token = True
        request.state.api_token_id = api_token.id

    now = datetime.now()
    if (
        api_token.last_used_at is None
        or (now - api_token.last_used_at).total_seconds() > API_TOKEN_LAST_USED_THROTTLE_SECONDS
    ):
        api_token.last_used_at = now
        session.add(api_token)
        session.commit()

    return user


async def get_current_user(
    request: Request = None,
    token: str = Depends(oauth2_scheme),
    session: Session = Depends(get_session),
) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    # API Token（机器凭证）路径：Bearer adk_...
    if is_api_token(token):
        return _authenticate_api_token(request, token, session, credentials_exception)

    # 原有 JWT 路径（行为不变）
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    statement = select(User).where(User.username == username)
    user = session.exec(statement).first()
    if user is None:
        raise credentials_exception
    return user


def is_api_token_auth(request: Optional[Request]) -> bool:
    """当前请求是否通过 API Token 认证。"""
    return bool(request is not None and getattr(request.state, "auth_via_api_token", False))


async def get_current_active_user(current_user: User = Depends(get_current_user)) -> User:
    if not current_user.is_active:
        raise HTTPException(status_code=400, detail="Inactive user")
    return current_user


async def get_current_user_no_token(
    request: Request = None,
    current_user: User = Depends(get_current_active_user),
) -> User:
    """仅允许 JWT 登录用户的依赖：API Token（机器凭证）访问返回 403。

    用于受限接口：admin 全部、改密、settings 写、Token 管理等。
    """
    if is_api_token_auth(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API tokens are not allowed to access this endpoint",
        )
    return current_user


async def get_current_admin_user(current_user: User = Depends(get_current_user_no_token)) -> User:
    if current_user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not enough permissions")
    return current_user


def ensure_owner_or_admin(
    owner_id: Optional[int],
    current_user: User,
    detail: str = "仅创建人或管理员可以删除",
) -> None:
    if current_user.role == "admin":
        return
    if owner_id is not None and owner_id == current_user.id:
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)
