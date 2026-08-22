"""成文工具链接口（新方案）：主会话自主成文的机制工具，与旧成文端点隔离。

MCP 调用带 X-Bidvolt-Cap 能力令牌，逐工具校验白名单/租户/任务状态；
普通用户可经 JWT 下载成文产物。旧 /response-package 路径不受影响。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import UserContext, require_capability, require_permission
from app.constants import Permission
from app.db import get_session
from app.models.agent import AgentArtifact
from app.services import assembly_service

router = APIRouter(prefix="/projects", tags=["agent-assembly"])


def _cap_task(request: Request) -> int:
    payload = getattr(request.state, "cap_payload", None)
    if payload is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="缺少 capability 授权上下文")
    return int(payload["tid"])


@router.get("/{project_id}/assembly/drafts")
async def resolve_template_draft(
    project_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_capability("resolve_template_draft")),
) -> dict:
    return await assembly_service.list_draft_candidates(session, user.enterprise_id, project_id)


@router.get("/{project_id}/assembly/outline")
async def get_template_outline(
    project_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_capability("get_template_outline")),
) -> dict:
    return await assembly_service.get_template_outline(session, user.enterprise_id, project_id)


@router.post("/{project_id}/assembly/slices", status_code=status.HTTP_201_CREATED)
async def slice_template_item(
    project_id: int,
    body: dict,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_capability("slice_template_item")),
) -> dict:
    try:
        return await assembly_service.create_slice(
            session,
            user.enterprise_id,
            project_id,
            _cap_task(request),
            int(body.get("file_id") or 0),
            int(body.get("req_id") or 0),
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/{project_id}/assembly/slices/{slice_id}/fill")
async def fill_template_slice(
    project_id: int,
    slice_id: str,
    body: dict,
    request: Request,
    user: UserContext = Depends(require_capability("fill_template_slice")),
) -> dict:
    try:
        return assembly_service.fill_slice(slice_id, _cap_task(request), body.get("fields"), body.get("fills"))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/{project_id}/assembly/slices/{slice_id}/append")
async def append_template_slice(
    project_id: int,
    slice_id: str,
    body: dict,
    request: Request,
    user: UserContext = Depends(require_capability("append_template_slice")),
) -> dict:
    try:
        return assembly_service.append_slice(slice_id, _cap_task(request), body.get("nodes"), body.get("comment"))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("/{project_id}/assembly/slices/{slice_id}/verify")
async def verify_template_slice(
    project_id: int,
    slice_id: str,
    request: Request,
    user: UserContext = Depends(require_capability("verify_template_slice")),
) -> dict:
    try:
        return assembly_service.verify_slice(slice_id, _cap_task(request))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/{project_id}/assembly/slices/{slice_id}/seal", status_code=status.HTTP_201_CREATED)
async def seal_template_item(
    project_id: int,
    slice_id: str,
    body: dict,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_capability("seal_template_item")),
) -> dict:
    try:
        return await assembly_service.seal_slice(
            session,
            slice_id,
            _cap_task(request),
            str(body.get("dir") or "商务文件"),
            str(body.get("filename") or f"{slice_id}.docx"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/{project_id}/assembly/xlsx", status_code=status.HTTP_201_CREATED)
async def build_quote_xlsx(
    project_id: int,
    body: dict,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_capability("build_quote_xlsx")),
) -> dict:
    return await assembly_service.quote_xlsx(
        session,
        user.enterprise_id,
        project_id,
        _cap_task(request),
        body.get("sheets"),
    )


@router.post("/{project_id}/assembly/package", status_code=status.HTTP_201_CREATED)
async def package_response_zip(
    project_id: int,
    body: dict,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_capability("package_response_zip")),
) -> dict:
    try:
        return await assembly_service.package_zip(
            session,
            user.enterprise_id,
            project_id,
            _cap_task(request),
            body.get("artifact_ids"),
            body.get("draft_file_id"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("/{project_id}/agent-artifact/{artifact_id}/download")
async def download_artifact(
    project_id: int,
    artifact_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.FILE_DOWNLOAD)),
) -> Response:
    art = await session.scalar(
        select(AgentArtifact).where(
            AgentArtifact.id == artifact_id,
            AgentArtifact.enterprise_id == user.enterprise_id,
            AgentArtifact.project_id == project_id,
        )
    )
    if art is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="成文产物不存在")
    return Response(
        content=art.content,
        media_type=art.mime,
        headers={"Content-Disposition": f'attachment; filename="{art.name.rsplit("/", 1)[-1]}"'},
    )
