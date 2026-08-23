"""认证与权限依赖。"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import Permission, TaskStatus
from app.db import get_session
from app.models.auth import AppUser, EnterprisePermission
from app.models.task import Task
from app.services.capability import CapabilityError, verify_capability
from app.services.permissions import effective_permissions
from app.services.security import decode_token

bearer = HTTPBearer(auto_error=False)

# MCP 工具 → JWT 回退所需权限点（Issue #2 安全回归：普通 JWT 不能绕过权限检查）
TOOL_PERMISSION: dict[str, str] = {
    "get_project_material_blocks": Permission.FILE_READ,
    "list_project_materials": Permission.FILE_READ,
    "get_deliverable_content": Permission.FILE_READ,
    "save_deliverable": Permission.DELIVERABLE_EDIT,
    "get_latest_score": Permission.SCORE_VIEW,
    "get_review_items": Permission.SCORE_VIEW,
    "submit_score_items": Permission.SCORE_CONFIRM,
    "confirm_review_items": Permission.SCORE_CONFIRM,
    "list_requirements": Permission.FILE_READ,
    "get_requirement": Permission.FILE_READ,
    "upsert_requirements": Permission.PROJECT_EDIT,
    "get_history_price": Permission.QUOTE_CALCULATE,
    "calculate_quote": Permission.QUOTE_CALCULATE,
    "search_web": Permission.FILE_READ,
    "search_web_minimax": Permission.FILE_READ,
    "vision_analyze_minimax": Permission.FILE_READ,
    "save_source": Permission.PROJECT_EDIT,
    "link_citation": Permission.DELIVERABLE_EDIT,
    "search_assets": Permission.FILE_READ,
    "get_asset": Permission.FILE_READ,
    "classify_enterprise_asset": Permission.PROJECT_EDIT,
    "upsert_enterprise_facts": Permission.PROJECT_EDIT,
    "search_knowledge": Permission.FILE_READ,
    "create_deliverable": Permission.DELIVERABLE_EDIT,
    # 成文工具链（新方案）：机制工具，读底稿/写产物
    "resolve_template_draft": Permission.FILE_READ,
    "get_template_outline": Permission.FILE_READ,
    "slice_template_item": Permission.DELIVERABLE_EXPORT,
    "fill_template_slice": Permission.DELIVERABLE_EXPORT,
    "append_template_slice": Permission.DELIVERABLE_EXPORT,
    "verify_template_slice": Permission.FILE_READ,
    "seal_template_item": Permission.DELIVERABLE_EXPORT,
    "build_quote_xlsx": Permission.DELIVERABLE_EXPORT,
    "package_response_zip": Permission.DELIVERABLE_EXPORT,
    "list_agent_artifacts": Permission.FILE_READ,
    "inspect_agent_artifact": Permission.FILE_READ,
}


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
            # 任务终态后授权上下文失效（A-2）：DONE/CANCELLED/FAILED_TERMINAL 拒绝
            task = await session.scalar(
                select(Task).where(
                    Task.id == int(payload["tid"]),
                    Task.enterprise_id == int(payload["eid"]),
                )
            )
            if task is None:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="任务不存在，授权上下文失效")
            if task.status in (int(TaskStatus.DONE), int(TaskStatus.CANCELLED), int(TaskStatus.FAILED_TERMINAL)):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="任务已结束，授权上下文失效")
            if task.project_id != int(payload.get("pid", 0)):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="capability token 与任务项目不一致")
            req_pid = request.path_params.get("project_id")
            if req_pid is not None and task.project_id != int(req_pid):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="capability token 不属于请求项目")
            await _set_rls_context(session, int(payload["eid"]))
            request.state.cap_payload = payload  # 端点可按需校验 body.project_id == payload.pid
            return UserContext(
                user_id=0,
                enterprise_id=int(payload["eid"]),
                email="mcp",
                permissions=set(),
            )
        # 普通用户 JWT 鉴权：按工具映射执行对应权限点，避免绕过权限检查
        permission = TOOL_PERMISSION.get(tool)
        if permission is None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="未知工具，拒绝 JWT 回退")
        user_ctx = await _auth_user(credentials, session)
        if permission not in user_ctx.permissions:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"缺少权限：{permission}")
        return user_ctx

    return checker
