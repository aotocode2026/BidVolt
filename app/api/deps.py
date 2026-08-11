"""认证与权限依赖。"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models.auth import AppUser, EnterprisePermission
from app.services.capability import CapabilityError, verify_capability
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
    return await _auth_user(credentials, session)


async def _auth_user(
    credentials: HTTPAuthorizationCredentials | None,
    session: AsyncSession,
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

    await _set_rls_context(session, user.enterprise_id)

    return UserContext(
        user_id=user.id,
        enterprise_id=user.enterprise_id,
        email=user.email,
        permissions=perms,
    )


async def _set_rls_context(session: AsyncSession, enterprise_id: int) -> None:
    """RLS：PG 下事务内注入租户上下文（SQLite 测试环境跳过）。"""
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        await session.execute(
            text("SELECT set_config('app.enterprise_id', :eid, true)"),
            {"eid": str(enterprise_id)},
        )


def require_permission(permission: str):
    """权限门禁依赖工厂。"""

    async def checker(user: UserContext = Depends(get_current_user)) -> UserContext:
        if permission not in user.permissions:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"缺少权限：{permission}")
        return user

    return checker


def require_capability(tool: str):
    """MCP 工具调用门禁：携带 X-Bidvolt-Cap 时强制校验任务级授权上下文。

    MCP 调用以 capability token 独立鉴权（不依赖用户 JWT），校验签名、有效期、
    工具白名单与租户绑定；普通用户调用未携带 cap token 时回退到 JWT 鉴权。
    """

    async def checker(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
        session: AsyncSession = Depends(get_session),
    ) -> UserContext:
        cap = request.headers.get("X-Bidvolt-Cap")
        if cap:
            try:
                payload = verify_capability(cap, tool=tool)
            except CapabilityError as exc:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
            await _set_rls_context(session, int(payload["eid"]))
            return UserContext(
                user_id=0,
                enterprise_id=int(payload["eid"]),
                email="mcp",
                permissions=set(),
            )
        # 普通用户 JWT 鉴权
        return await _auth_user(credentials, session)

    return checker
