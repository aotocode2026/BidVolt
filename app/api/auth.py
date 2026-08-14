"""认证接口（4.1.1）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import UserContext, get_current_user
from app.constants import Permission
from app.db import get_session
from app.models.auth import AppUser, Enterprise, EnterprisePermission, RefreshToken
from app.models.enterprise_domain import EnterpriseAssetCategory
from app.models.quota import TenantQuota
from app.schemas.auth import (
    LoginRequest,
    MeResponse,
    RefreshRequest,
    RegisterRequest,
    TokenPair,
)
from app.services.audit import write_audit
from app.services.security import (
    create_access_token,
    create_refresh_token,
    hash_password,
    hash_refresh_token,
    validate_refresh_token,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def _issue_tokens(user: AppUser, permissions: list[str]) -> tuple[TokenPair, str, str]:
    raw_refresh, refresh_hash = create_refresh_token()
    return TokenPair(
        user_id=user.id,
        enterprise_id=user.enterprise_id,
        access_token=create_access_token(user.id, user.enterprise_id, permissions),
        refresh_token=raw_refresh,
    ), raw_refresh, refresh_hash


@router.post("/register", response_model=TokenPair, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest, session: AsyncSession = Depends(get_session)) -> TokenPair:
    exists = await session.scalar(select(AppUser).where(AppUser.email == body.email.lower()))
    if exists:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="邮箱已注册")
    if not any(c.isalpha() for c in body.password) or not any(c.isdigit() for c in body.password):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="密码需同时包含字母和数字")

    enterprise = Enterprise(name=body.enterprise_name)
    session.add(enterprise)
    await session.flush()

    session.add(
        EnterprisePermission(
            enterprise_id=enterprise.id,
            permissions=sorted(Permission.DEFAULT),
        )
    )
    session.add(
        TenantQuota(
            enterprise_id=enterprise.id,
        )
    )
    for category_name in ("证照", "资质", "业绩", "人员", "产品参数", "检测报告", "其他"):
        session.add(
            EnterpriseAssetCategory(
                enterprise_id=enterprise.id,
                name=category_name,
            )
        )

    user = AppUser(
        enterprise_id=enterprise.id,
        email=body.email.lower(),
        password_hash=hash_password(body.password),
        permissions=None,  # 继承企业默认集
    )
    session.add(user)
    await session.flush()

    pair, raw_refresh, refresh_hash = _issue_tokens(user, sorted(Permission.DEFAULT))
    session.add(
        RefreshToken(
            user_id=user.id,
            token_hash=refresh_hash,
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        )
    )
    await write_audit(
        session,
        enterprise_id=enterprise.id,
        user_id=user.id,
        action="auth.register",
        object_type="app_user",
        object_id=user.id,
    )
    await session.commit()
    return pair


@router.post("/login", response_model=TokenPair)
async def login(body: LoginRequest, session: AsyncSession = Depends(get_session)) -> TokenPair:
    user = await session.scalar(select(AppUser).where(AppUser.email == body.email.lower()))
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="邮箱或密码错误")
    if user.status != 1:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="账号已禁用")

    ent_perm = await session.get(EnterprisePermission, user.enterprise_id)
    perms = sorted(user.permissions or (ent_perm.permissions if ent_perm else []))
    pair, raw_refresh, refresh_hash = _issue_tokens(user, perms)
    session.add(
        RefreshToken(
            user_id=user.id,
            token_hash=refresh_hash,
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        )
    )
    await write_audit(
        session,
        enterprise_id=user.enterprise_id,
        user_id=user.id,
        action="auth.login",
    )
    await session.commit()
    return pair


@router.post("/refresh", response_model=TokenPair)
async def refresh(body: RefreshRequest, session: AsyncSession = Depends(get_session)) -> TokenPair:
    token_hash = hash_refresh_token(body.refresh_token)
    record = await session.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == token_hash, RefreshToken.revoked.is_(False))
    )
    if record is None or not validate_refresh_token(body.refresh_token, record.token_hash, record.expires_at):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="refresh token 无效或已过期")

    user = await session.get(AppUser, record.user_id)
    if user is None or user.status != 1:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不可用")

    record.revoked = True
    ent_perm = await session.get(EnterprisePermission, user.enterprise_id)
    perms = sorted(user.permissions or (ent_perm.permissions if ent_perm else []))
    pair, raw_refresh, refresh_hash = _issue_tokens(user, perms)
    session.add(
        RefreshToken(
            user_id=user.id,
            token_hash=refresh_hash,
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        )
    )
    await session.commit()
    return pair


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    body: RefreshRequest,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(get_current_user),
) -> None:
    token_hash = hash_refresh_token(body.refresh_token)
    record = await session.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == token_hash, RefreshToken.user_id == user.user_id)
    )
    if record is not None:
        record.revoked = True
    await write_audit(
        session,
        enterprise_id=user.enterprise_id,
        user_id=user.user_id,
        action="auth.logout",
    )
    await session.commit()


@router.get("/me", response_model=MeResponse)
async def me(user: UserContext = Depends(get_current_user)) -> MeResponse:
    return MeResponse(
        user_id=user.user_id,
        email=user.email,
        enterprise_id=user.enterprise_id,
        enterprise_name="",  # 由前端补充展示；如需企业名可在 UserContext 扩展
        permissions=sorted(user.permissions),
    )
