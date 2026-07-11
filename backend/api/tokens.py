"""API Token 管理接口。

供外部 CI 使用的长效机器凭证的创建、列表与吊销。
仅 JWT 登录用户可用（API Token 自身不能管理 Token）。
"""
from datetime import datetime
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import Session, select

from backend.api import deps
from backend.core.api_tokens import (
    generate_api_token,
    hash_api_token,
    token_display_prefix,
)
from backend.database import get_session
from backend.models import ApiToken, User

router = APIRouter()


class ApiTokenCreate(BaseModel):
    name: str


class ApiTokenRead(BaseModel):
    id: int
    name: str
    token_prefix: str
    created_at: datetime
    last_used_at: Optional[datetime] = None
    is_active: bool
    user_id: int
    username: Optional[str] = None


class ApiTokenCreated(ApiTokenRead):
    # 明文 token，仅创建时返回一次
    token: str


def _to_read(api_token: ApiToken, username: Optional[str] = None) -> ApiTokenRead:
    return ApiTokenRead(
        id=api_token.id,
        name=api_token.name,
        token_prefix=api_token.token_prefix,
        created_at=api_token.created_at,
        last_used_at=api_token.last_used_at,
        is_active=api_token.is_active,
        user_id=api_token.user_id,
        username=username,
    )


@router.post("/", response_model=ApiTokenCreated)
def create_token(
    *,
    token_in: ApiTokenCreate,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user_no_token),
) -> Any:
    """创建 API Token，明文仅本次响应返回，之后无法再次查看。"""
    name = (token_in.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Token 名称不能为空")

    plaintext = generate_api_token()
    api_token = ApiToken(
        name=name,
        token_hash=hash_api_token(plaintext),
        token_prefix=token_display_prefix(plaintext),
        user_id=current_user.id,
    )
    session.add(api_token)
    session.commit()
    session.refresh(api_token)

    read = _to_read(api_token, username=current_user.username)
    return ApiTokenCreated(**read.model_dump(), token=plaintext)


@router.get("/", response_model=List[ApiTokenRead])
def list_tokens(
    *,
    all: int = Query(default=0, description="admin 传 1 查看所有人的 token"),
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user_no_token),
) -> Any:
    """列出 API Token（不含明文）。默认仅本人；admin 可加 ?all=1 查看全部。"""
    statement = select(ApiToken)
    if all:
        if current_user.role != "admin":
            raise HTTPException(status_code=403, detail="Not enough permissions")
    else:
        statement = statement.where(ApiToken.user_id == current_user.id)
    statement = statement.order_by(ApiToken.created_at.desc())
    tokens = session.exec(statement).all()

    user_ids = {t.user_id for t in tokens}
    usernames = {
        u.id: u.username
        for u in session.exec(select(User).where(User.id.in_(user_ids))).all()
    } if user_ids else {}
    return [_to_read(t, username=usernames.get(t.user_id)) for t in tokens]


@router.delete("/{token_id}")
def revoke_token(
    *,
    token_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(deps.get_current_user_no_token),
) -> Any:
    """吊销 API Token（软删：is_active=False，立即失效）。"""
    api_token = session.get(ApiToken, token_id)
    if api_token is None:
        raise HTTPException(status_code=404, detail="Token 不存在")

    deps.ensure_owner_or_admin(
        api_token.user_id, current_user, detail="仅创建人或管理员可以吊销"
    )

    api_token.is_active = False
    session.add(api_token)
    session.commit()
    return {"message": "Token 已吊销", "id": token_id}
