"""成文工具链接口（新方案）：主会话自主成文的机制工具，与旧成文端点隔离。

MCP 调用带 X-Bidvolt-Cap 能力令牌，逐工具校验白名单/租户/任务状态；
普通用户可经 JWT 下载成文产物。旧 /response-package 路径不受影响。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import UserContext, require_capability, require_permission
from app.constants import Permission
from app.db import get_session
from app.models.agent import AgentArtifact
from app.schemas.agent import AgentArtifactInspect, AgentArtifactListResponse
from app.services import assembly_service

router = APIRouter(prefix="/projects", tags=["agent-assembly"])


async def _ensure_project(session: AsyncSession, enterprise_id: int, project_id: int) -> None:
    """项目归属校验：跨企业/不存在项目一律 404（与其他端点一致，杜绝项目 id 探测）。"""
    from app.models.project import Project

    project = await session.scalar(
        select(Project).where(
            Project.id == project_id,
            Project.enterprise_id == enterprise_id,
        )
    )
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="项目不存在")


def _cap_task(request: Request) -> int:
    payload = getattr(request.state, "cap_payload", None)
    if payload is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="缺少 capability 授权上下文")
    return int(payload["tid"])


def _cap_task_or_none(request: Request) -> int | None:
    """MCP 调用返回 capability 中的 task_id；普通 JWT 返回 None。"""
    payload = getattr(request.state, "cap_payload", None)
    if payload is None:
        return None
    return int(payload["tid"])


@router.get("/{project_id}/assembly/drafts")
async def resolve_template_draft(
    project_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_capability("resolve_template_draft")),
) -> dict:
    await _ensure_project(session, user.enterprise_id, project_id)
    return await assembly_service.list_draft_candidates(session, user.enterprise_id, project_id)


@router.get("/{project_id}/assembly/outline")
async def get_template_outline(
    project_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_capability("get_template_outline")),
) -> dict:
    await _ensure_project(session, user.enterprise_id, project_id)
    return await assembly_service.get_template_outline(session, user.enterprise_id, project_id)


@router.post("/{project_id}/assembly/slices", status_code=status.HTTP_201_CREATED)
async def slice_template_item(
    project_id: int,
    body: dict,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_capability("slice_template_item")),
) -> dict:
    await _ensure_project(session, user.enterprise_id, project_id)
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
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_capability("fill_template_slice")),
) -> dict:
    await _ensure_project(session, user.enterprise_id, project_id)
    try:
        return assembly_service.fill_slice(
            slice_id, _cap_task(request), body.get("fields"), body.get("fills"),
            body.get("table_fills"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/{project_id}/assembly/slices/{slice_id}/append")
async def append_template_slice(
    project_id: int,
    slice_id: str,
    body: dict,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_capability("append_template_slice")),
) -> dict:
    await _ensure_project(session, user.enterprise_id, project_id)
    try:
        return assembly_service.append_slice(
            slice_id, _cap_task(request), body.get("nodes"), body.get("comment"), body.get("heading")
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("/{project_id}/assembly/slices/{slice_id}/verify")
async def verify_template_slice(
    project_id: int,
    slice_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_capability("verify_template_slice")),
) -> dict:
    await _ensure_project(session, user.enterprise_id, project_id)
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
    await _ensure_project(session, user.enterprise_id, project_id)
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
    await _ensure_project(session, user.enterprise_id, project_id)
    try:
        return await assembly_service.quote_xlsx(
            session,
            user.enterprise_id,
            project_id,
            _cap_task(request),
            body.get("sheets"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/{project_id}/assembly/upload-file", status_code=status.HTTP_201_CREATED)
async def upload_deliverable_file(
    project_id: int,
    request: Request,
    file: UploadFile = File(...),
    name: str = Form(...),
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_capability("upload_deliverable_file")),
) -> dict:
    """整文件交付通道：Hermes 直接写好的完整交付文件（docx/xlsx/pdf）落库为封存产物。

    与切片修订路径并列可选——Hermes 可自产整文件（python-docx 配图等），
    服务端不介入内容生成；打包/清单/审计与切片产物同一套机制。"""
    await _ensure_project(session, user.enterprise_id, project_id)
    data = await file.read(60 * 1024 * 1024 + 1)
    try:
        return await assembly_service.upload_artifact_file(
            session,
            user.enterprise_id,
            project_id,
            _cap_task(request),
            name,
            data,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.put("/{project_id}/assembly/artifacts/{artifact_id}")
async def replace_deliverable_file(
    project_id: int,
    artifact_id: int,
    request: Request,
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_capability("upload_deliverable_file")),
) -> dict:
    """覆盖修改已封存产物：Hermes 修完文件直接换内容，artifact_id 与包内路径名不变。
    随时可改——改完重新 package_response_zip 打包即可，交付包与最终文件保持一致。"""
    await _ensure_project(session, user.enterprise_id, project_id)
    data = await file.read(60 * 1024 * 1024 + 1)
    try:
        return await assembly_service.replace_artifact_file(
            session,
            user.enterprise_id,
            project_id,
            _cap_task(request),
            artifact_id,
            data,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/{project_id}/assembly/artifacts/{artifact_id}/save", status_code=status.HTTP_201_CREATED)
async def save_deliverable_file(
    project_id: int,
    artifact_id: int,
    request: Request,
    file: UploadFile = File(...),
    mode: str = Form("overwrite"),
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.DELIVERABLE_EDIT)),
) -> dict:
    """远端 Office 文件保存：
    mode=overwrite 覆盖当前文件（artifact_id 不变，version_no 递增）；
    mode=new 另存为新版本（旧版本保留）。"""
    if mode not in ("overwrite", "new"):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="mode 必须是 overwrite 或 new")
    await _ensure_project(session, user.enterprise_id, project_id)
    data = await file.read(60 * 1024 * 1024 + 1)
    try:
        result = await assembly_service.save_artifact_file(
            session,
            user.enterprise_id,
            project_id,
            _cap_task_or_none(request),
            artifact_id,
            data,
            mode,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    result["download_url"] = (
        f"/api/v1/projects/{project_id}/agent-artifact/{result['artifact_id']}/download"
    )
    return result


@router.post("/{project_id}/assembly/artifacts/{artifact_id}/render-qa")
async def render_qa_artifact(
    project_id: int,
    artifact_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_capability("render_qa_docx")),
) -> dict:
    """渲染质检：docx 产物 → LibreOffice headless 转 PDF → 逐页 PNG + 空白页/页数统计。
    Hermes 用返回的 PNG 路径配 vision 抽查版面（表格跨页/图片方向/断页）。"""
    await _ensure_project(session, user.enterprise_id, project_id)
    try:
        return await assembly_service.render_qa_artifact(
            session,
            user.enterprise_id,
            project_id,
            artifact_id,
            _cap_task(request),
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/{project_id}/assembly/package", status_code=status.HTTP_201_CREATED)
async def package_response_zip(
    project_id: int,
    body: dict,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_capability("package_response_zip")),
) -> dict:
    await _ensure_project(session, user.enterprise_id, project_id)
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


@router.get("/{project_id}/assembly/artifacts")
async def list_agent_artifacts(
    project_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_capability("list_agent_artifacts")),
    task_id: int | None = Query(default=None, description="按任务过滤；普通用户可留空查看项目全部产物"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=100, ge=1, le=500),
) -> AgentArtifactListResponse:
    await _ensure_project(session, user.enterprise_id, project_id)
    scope_task_id = _cap_task_or_none(request) or task_id
    return await assembly_service.list_artifacts(
        session, user.enterprise_id, project_id, scope_task_id, page, size
    )


@router.get("/{project_id}/assembly/artifacts/{artifact_id}/inspect")
async def inspect_agent_artifact(
    project_id: int,
    artifact_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_capability("inspect_agent_artifact")),
) -> AgentArtifactInspect:
    await _ensure_project(session, user.enterprise_id, project_id)
    try:
        return await assembly_service.inspect_artifact(
            session, user.enterprise_id, project_id, _cap_task_or_none(request), artifact_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get("/{project_id}/agent-artifact/{artifact_id}/download")
async def download_artifact(
    project_id: int,
    artifact_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.FILE_DOWNLOAD)),
) -> Response:
    await _ensure_project(session, user.enterprise_id, project_id)
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
