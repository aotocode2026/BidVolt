"""认证与权限依赖。"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models.auth import AppUser, EnterprisePermission
from app.services.permissions import effective_permissions
from app.services.security import decode_token

bearer = HTTPBearer(auto_error=False)


@dataclass
class UserContext:
    user_id: int
    enterprise_id: int
    email: str
    permissions: set[str]


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    session: AsyncSession = Depends(get_session),
) -> UserContext:
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未认证")
    try:
        payload = decode_token(credentials.credentials)
    except JWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="令牌无效或已过期") from exc
    if payload.get("type") != "access":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="令牌类型错误")

    user = await session.get(AppUser, int(payload["sub"]))
    if user is None or user.status != 1:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不存在或已禁用")

    ent_perm = await session.get(EnterprisePermission, user.enterprise_id)
    base_perms = ent_perm.permissions if ent_perm else []
    perms = effective_permissions(user.permissions, base_perms)

    # RLS：PG 下事务内注入租户上下文（SQLite 测试环境跳过）
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        await session.execute(
            text("SELECT set_config('app.enterprise_id', :eid, true)"), {"eid": str(user.enterprise_id)}
        )

    return UserContext(
        user_id=user.id,
        enterprise_id=user.enterprise_id,
        email=user.email,
        permissions=perms,
    )


def require_permission(permission: str):
    """权限门禁依赖工厂。"""

    async def checker(user: UserContext = Depends(get_current_user)) -> UserContext:
        if permission not in user.permissions:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"缺少权限：{permission}")
        return user

    return checker
