"""文件服务接口（4.2）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import UserContext, require_capability, require_permission
from app.config import settings
from app.constants import Permission
from app.db import get_session
from app.models.doc import DocBlock
from app.models.enterprise_domain import EnterpriseAsset, EnterpriseFact
from app.models.file import FileObject
from app.models.project_material import ProjectMaterial
from app.schemas.project import Page
from app.services import file_service
from app.services.audit import write_audit
from app.services.quota_service import QuotaExceeded
from app.services.storage import StorageProvider

router = APIRouter(prefix="/files", tags=["files"])
storage = StorageProvider()


def _file_dict(f: FileObject) -> dict:
    return {
        "file_id": f.id,
        "name": f.original_name,
        "size": f.size_bytes,
        "mime": f.mime_type,
        "status": f.status,
        "sha256": f.sha256,
        "category": f.category,
        "project_id": f.project_id,
        "document_role": f.document_role,
        # 存量解析失败文件在资料列表红字标注原因（新上传已直接拒绝，不会产生 status=4）
        "parse_status": f.parse_status,
    }


@router.post("/upload")
async def upload_files(
    target: str = Form(...),
    project_id: int | None = Form(default=None),
    document_role: str | None = Form(default=None),
    files: list[UploadFile] = File(...),
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.FILE_UPLOAD)),
) -> dict:
    results: list[dict] = []
    for upload in files:
        data = await upload.read(settings.max_upload_bytes + 1)
        try:
            # SAVEPOINT：单文件解析失败 → 该文件整体回滚（不入库、不进资料列表），
            # 其余文件互不影响（Issue #8 复盘：失败文件必须红字拒绝，不留歧义记录）。
            async with session.begin_nested():
                fobj = await file_service.process_upload(
                    session, user, data, upload.filename or "unnamed", target, project_id, document_role
                )
                await write_audit(
                    session,
                    enterprise_id=user.enterprise_id,
                    user_id=user.user_id,
                    project_id=fobj.project_id,
                    action="file.upload",
                    object_type="file_object",
                    object_id=fobj.id,
                )
                item = {
                    "file_id": fobj.id,
                    "name": fobj.original_name,
                    "size": fobj.size_bytes,
                    "mime": fobj.mime_type,
                    "status": fobj.status,
                    "document_role": fobj.document_role,
                    # Issue #13：解析失败原因随上传响应返回，前端即时提示（此前原因只落库不展示）
                    "parse_status": fobj.parse_status,
                }
                if target == "enterprise":
                    # Issue #6 P0：企业上传明确返回 asset_id 与是否自动 ingest。
                    # 上传即自动入库（按文件名分类+抽取初始事实），facts_extracted 给出实际条数信号。
                    asset = await session.scalar(
                        select(EnterpriseAsset).where(EnterpriseAsset.source_file_id == fobj.id)
                    )
                    item["asset_id"] = asset.id if asset else None
                    item["auto_ingest"] = True
                    item["facts_extracted"] = (
                        await session.scalar(
                            select(func.count()).select_from(EnterpriseFact).where(
                                EnterpriseFact.asset_id == asset.id
                            )
                        )
                        or 0
                    ) if asset else 0
            results.append(item)
        except ValueError as exc:
            results.append({"name": upload.filename, "error": str(exc)})
        except QuotaExceeded as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    await session.commit()
    return {"files": results}


@router.get("", response_model=Page)
async def list_files(
    target: str | None = Query(default=None),
    project_id: int | None = Query(default=None),
    page: int = 1,
    size: int = 20,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.FILE_READ)),
) -> Page:
    query = select(FileObject).where(
        FileObject.enterprise_id == user.enterprise_id, FileObject.is_deleted.is_(False)
    )
    if target == "enterprise":
        query = query.where(FileObject.owner_type == 1)
    elif target == "project":
        query = query.where(FileObject.owner_type == 2)
    if project_id is not None:
        query = query.where(FileObject.project_id == project_id)
    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    rows = await session.scalars(query.order_by(FileObject.id.desc()).offset((page - 1) * size).limit(size))
    return Page(items=[_file_dict(f) for f in rows], total=total or 0, page=page, size=size)


async def _get_file(session: AsyncSession, user: UserContext, file_id: int) -> FileObject:
    f = await session.scalar(
        select(FileObject).where(
            FileObject.id == file_id,
            FileObject.enterprise_id == user.enterprise_id,
            FileObject.is_deleted.is_(False),
        )
    )
    if f is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文件不存在")
    return f


@router.get("/{file_id}/info")
async def file_info(
    file_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.FILE_READ)),
) -> dict:
    return _file_dict(await _get_file(session, user, file_id))


async def _serve_download(
    file_id: int,
    session: AsyncSession,
    user: UserContext,
) -> FileResponse:
    f = await _get_file(session, user, file_id)
    try:
        path = storage.open(f.bucket, f.object_key)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="存储对象缺失") from exc
    await write_audit(
        session,
        enterprise_id=user.enterprise_id,
        user_id=user.user_id,
        project_id=f.project_id,
        action="file.download",
        object_type="file_object",
        object_id=f.id,
    )
    await session.commit()
    return FileResponse(path, filename=f.original_name, media_type=f.mime_type)


@router.get("/{file_id}/download")
async def download_file(
    file_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.FILE_DOWNLOAD)),
) -> FileResponse:
    return await _serve_download(file_id, session, user)


@router.get("/{file_id}/signed")
async def signed_download(
    file_id: int,
    exp: int,
    sig: str,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.FILE_DOWNLOAD)),
) -> FileResponse:
    """签名 URL 下载：HMAC 校验 + 过期 401 + 租户/权限校验。"""
    if not storage.verify_signed(file_id, user.enterprise_id, exp, sig):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="签名无效或已过期")
    return await _serve_download(file_id, session, user)


@router.delete("/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_file(
    file_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.FILE_UPLOAD)),
) -> None:
    f = await _get_file(session, user, file_id)
    f.is_deleted = True
    await write_audit(
        session,
        enterprise_id=user.enterprise_id,
        user_id=user.user_id,
        project_id=f.project_id,
        action="file.delete",
        object_type="file_object",
        object_id=f.id,
    )
    await session.commit()


@router.post("/archive")
async def archive_upload(
    body: dict,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.FILE_UPLOAD)),
) -> dict:
    try:
        job = await file_service.process_archive(
            session,
            user,
            body["archive_file_id"],
            body["target"],
            body.get("project_id"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return {"job_id": job.id, "status": job.status, "result": job.result}


@router.get("/{file_id}/parse-status")
async def parse_status(
    file_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.FILE_READ)),
) -> dict:
    f = await _get_file(session, user, file_id)
    return {"status": f.status, "category": f.category, "parse_status": f.parse_status}


@router.get("/{file_id}/blocks")
async def file_blocks(
    file_id: int,
    page: int = 1,
    size: int = 100,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_capability("get_project_material_blocks")),
) -> Page:
    await _get_file(session, user, file_id)
    query = select(DocBlock).where(DocBlock.file_id == file_id)
    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    rows = await session.scalars(query.order_by(DocBlock.block_index).offset((page - 1) * size).limit(size))
    return Page(items=[{"block_id": b.id, "block_type": b.block_type, "page_no": b.page_no, "block_index": b.block_index, "text": b.text_content} for b in rows], total=total or 0, page=page, size=size)


@router.get("/projects/{project_id}/materials")
async def project_materials(
    project_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_capability("list_project_materials")),
) -> list[dict]:
    rows = await session.scalars(
        select(ProjectMaterial).where(
            ProjectMaterial.enterprise_id == user.enterprise_id,
            ProjectMaterial.project_id == project_id,
        )
    )
    return [
        {"material_id": m.id, "file_id": m.file_id, "status": m.status}
        for m in rows
    ]
