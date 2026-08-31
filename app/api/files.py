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
        except file_service.DuplicateUploadError as dup:
            # 内容去重：同企业已入库相同文件——返回既有文件，不重复解析/入库
            asset_id = None
            if target == "enterprise":
                existing_asset = await session.scalar(
                    select(EnterpriseAsset).where(
                        EnterpriseAsset.enterprise_id == user.enterprise_id,
                        EnterpriseAsset.source_file_id == dup.existing.id,
                    )
                )
                asset_id = existing_asset.id if existing_asset else None
            results.append(
                {
                    "name": upload.filename,
                    "duplicate": True,
                    "file_id": dup.existing.id,
                    "asset_id": asset_id,
                    "size": dup.existing.size_bytes,
                    "message": "内容与已入库文件相同（sha256 一致），已跳过重复入库；如需另存请改名后重传",
                }
            )
        except ValueError as exc:
            results.append({"name": upload.filename, "error": str(exc)})
        except QuotaExceeded as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    # 压缩包自动解包：上传的 zip 立即展开成内部文件入库（项目侧=内部文件成为项目材料；
    # 企业侧=每个内部文件自动入库成资料资产）。原件保留，expanded 回执随响应返回。
    for item in results:
        if item.get("error") or item.get("duplicate"):
            continue
        _fid = item.get("file_id")
        if not _fid:
            continue
        _fobj = await session.get(FileObject, _fid)
        if _fobj is None or _fobj.ext != ".zip":
            continue
        try:
            _job = await file_service.process_archive(session, user, _fobj.id, target, project_id)
            _res = _job.result or {}
            item["expanded"] = {
                "imported": len(_res.get("imported") or []),
                "failed": len(_res.get("failed") or []),
                "duplicates": len(_res.get("duplicates") or []),
            }
        except ValueError as exc:
            item["expanded"] = {"error": str(exc)}
        # process_archive 内部 commit 会清掉事务级 RLS 上下文：重设后再处理下一个文件
        from app.services.task_service import _set_rls_context  # noqa: PLC0415

        await _set_rls_context(session, user.enterprise_id)
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
    user: UserContext = Depends(require_capability("download_project_material")),
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


@router.get("/image-describe-progress")
async def image_describe_progress(
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.FILE_READ)),
) -> dict:
    """后台识图任务进度：按状态统计本企业 image_describe 任务 + 全局已缓存描述张数。"""
    from app.constants import TaskType
    from app.models.file import ImageDescription
    from app.models.task import Task

    rows = await session.execute(
        select(Task.status, func.count())
        .where(
            Task.task_type == TaskType.IMAGE_DESCRIBE,
            Task.enterprise_id == user.enterprise_id,
        )
        .group_by(Task.status)
    )
    stats = {int(s): int(n) for s, n in rows}
    total_described = await session.scalar(
        select(func.count()).select_from(ImageDescription)
    )
    return {
        "queued": stats.get(1, 0),
        "running": stats.get(2, 0),
        "done": stats.get(3, 0),
        "failed_terminal": stats.get(4, 0) + stats.get(6, 0),
        "remaining": stats.get(1, 0) + stats.get(2, 0),
        "described_images": int(total_described or 0),
    }


@router.get("/{file_id}/image-descriptions")
async def file_image_descriptions(
    file_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_capability("get_image_descriptions")),
) -> dict:
    """文件内嵌图片的结构化描述清单（入库后台任务产出，sha256 全局缓存）。
    描述用于「找」该装的证据（编号/金额/日期/主体/印章/摘要）；
    装订关键字段时仍须 vision 复核原件图。"""
    from app.models.file import FileImage, ImageDescription

    await _get_file(session, user, file_id)
    rows = list(
        await session.scalars(
            select(FileImage).where(FileImage.file_id == file_id).order_by(FileImage.ordinal)
        )
    )
    hashes = [r.sha256 for r in rows]
    descs = {
        d.sha256: d
        for d in await session.scalars(select(ImageDescription).where(ImageDescription.sha256.in_(hashes)))
    } if hashes else {}
    return {
        "file_id": file_id,
        "image_count": len(rows),
        "described_count": sum(1 for h in hashes if h in descs),
        "items": [
            {
                "ordinal": r.ordinal,
                "page": r.page,
                "sha256": r.sha256,
                "described": r.sha256 in descs,
                "description": (descs[r.sha256].description if r.sha256 in descs else None),
            }
            for r in rows
        ],
    }


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
    return Page(items=[{"block_id": b.id, "block_type": b.block_type, "page_no": b.page_no, "block_index": b.block_index, "text": b.text_content, "extra": b.extra} for b in rows], total=total or 0, page=page, size=size)


@router.get("/projects/{project_id}/materials")
async def project_materials(
    project_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_capability("list_project_materials")),
) -> list[dict]:
    rows = list(await session.scalars(
        select(ProjectMaterial).where(
            ProjectMaterial.enterprise_id == user.enterprise_id,
            ProjectMaterial.project_id == project_id,
        )
    ))
    file_ids = [m.file_id for m in rows]
    block_counts: dict[int, int] = {}
    if file_ids:
        cnt_rows = await session.execute(
            select(DocBlock.file_id, func.count())
            .where(DocBlock.file_id.in_(file_ids))
            .group_by(DocBlock.file_id)
        )
        block_counts = {int(fid): int(n) for fid, n in cnt_rows}
    files = {
        f.id: f
        for f in await session.scalars(select(FileObject).where(FileObject.id.in_(file_ids)))
    } if file_ids else {}
    # 解包溯源：展开来的文件带出「来源压缩包名 + 包内路径层次」
    src_ids = {f.source_archive_id for f in files.values() if f.source_archive_id}
    src_files = {
        f.id: f
        for f in await session.scalars(select(FileObject).where(FileObject.id.in_(src_ids)))
    } if src_ids else {}
    # 已解包统计：zip 行标注「已解包 N 个文件」（列表降噪，原件字节保留）
    expanded_counts: dict[int, int] = {}
    if file_ids:
        exp_rows = await session.execute(
            select(FileObject.source_archive_id, func.count())
            .where(FileObject.source_archive_id.in_(file_ids))
            .group_by(FileObject.source_archive_id)
        )
        expanded_counts = {int(sid): int(n) for sid, n in exp_rows if sid is not None}
    # 内容结构信号：块类型统计（段落/表格/图片/页眉页脚）+ 内嵌图片张数——
    # 让主会话一眼知道「这文件里有几张图、几张表」，决定要不要下载原件+vision 读图取证
    block_stats: dict[int, dict[str, int]] = {}
    media_counts: dict[int, int] = {}
    if file_ids:
        stats_rows = await session.execute(
            select(DocBlock.file_id, DocBlock.block_type, func.count())
            .where(DocBlock.file_id.in_(file_ids))
            .group_by(DocBlock.file_id, DocBlock.block_type)
        )
        for _fid, _btype, _n in stats_rows:
            block_stats.setdefault(int(_fid), {})[_btype] = int(_n)
        img_rows = await session.execute(
            select(DocBlock.file_id, DocBlock.extra).where(
                DocBlock.file_id.in_(file_ids),
                DocBlock.block_type == "image",
            )
        )
        for _fid, _extra in img_rows:
            _cnt = _extra.get("count") if isinstance(_extra, dict) else 0
            media_counts[int(_fid)] = media_counts.get(int(_fid), 0) + int(_cnt or 0)
    # 图片描述进度：file_image 登记数 vs 已描述数（入库后台任务逐步填满）
    image_counts: dict[int, int] = {}
    image_described: dict[int, int] = {}
    if file_ids:
        from app.models.file import FileImage, ImageDescription

        desc_rows = await session.execute(
            select(FileImage.file_id, func.count(), func.count(ImageDescription.sha256))
            .outerjoin(ImageDescription, FileImage.sha256 == ImageDescription.sha256)
            .where(FileImage.file_id.in_(file_ids))
            .group_by(FileImage.file_id)
        )
        for _fid, _total, _done in desc_rows:
            image_counts[int(_fid)] = int(_total)
            image_described[int(_fid)] = int(_done or 0)
    return [
        {
            "material_id": m.id,
            "file_id": m.file_id,
            "file_name": (files.get(m.file_id).original_name if m.file_id in files else None),
            "ext": (files.get(m.file_id).ext if m.file_id in files else None),
            "status": m.status,
            # 解析完整性信号（服务端只给信号不裁决）：
            # - status=3 已解析，但 block_count 只统计"可提取文字块"；扫描件/图表可能无文字层或块不完整，
            #   解析索引永远只是导航辅助，内容以原件为准（读块工具 + 必要时 vision 读原图）。
            # - status=4 解析失败，必须走原件。
            "parse_status": (files.get(m.file_id).parse_status if m.file_id in files else None),
            "block_count": block_counts.get(m.file_id, 0),
            # 解包溯源（压缩包展开来的文件才非空）
            "source_archive_id": (files.get(m.file_id).source_archive_id if m.file_id in files else None),
            "source_archive_name": (
                src_files.get(files[m.file_id].source_archive_id).original_name
                if m.file_id in files and files[m.file_id].source_archive_id in src_files else None
            ),
            "archive_path": (files.get(m.file_id).archive_path if m.file_id in files else None),
            # 该 zip 已解包展开的文件数（仅 zip 行有值，0=未解包）
            "expanded_count": expanded_counts.get(m.file_id, 0),
            # 内容结构信号：段落/表格/图片/页眉页脚块统计 + 内嵌图片张数
            "block_stats": block_stats.get(m.file_id, {}),
            "media_count": media_counts.get(m.file_id, 0),
            # 图片描述进度（入库后台任务）：已提取图数 / 已有结构化描述的图数
            "image_count": image_counts.get(m.file_id, 0),
            "image_described_count": image_described.get(m.file_id, 0),
        }
        for m in rows
    ]
