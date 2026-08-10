"""终检与导出接口（4.10.1）。"""

from __future__ import annotations

from datetime import datetime, timezone
import io
import json
import zipfile

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_permission, UserContext
from app.constants import Permission
from app.db import get_session
from app.models.deliverable import Deliverable
from app.models.export import ExportJob, FinalCheck
from app.services import deliverable_service, export_service
from app.services.storage import StorageProvider

router = APIRouter(prefix="/projects", tags=["export"])
storage = StorageProvider()


@router.post("/{project_id}/check")
async def final_check(
    project_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.DELIVERABLE_EXPORT)),
) -> dict:
    deliverables = (
        await session.scalars(
            select(Deliverable).where(
                Deliverable.enterprise_id == user.enterprise_id,
                Deliverable.project_id == project_id,
            )
        )
    ).all()
    result = export_service.run_final_check(deliverables)
    row = FinalCheck(
        enterprise_id=user.enterprise_id,
        project_id=project_id,
        status=2,
        passed=result["passed"],
        result=result,
    )
    session.add(row)
    await session.commit()
    return {"check_id": row.id, "passed": result["passed"], "issues": result["issues"]}


@router.get("/{project_id}/check/{check_id}")
async def get_check(
    project_id: int,
    check_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.FILE_READ)),
) -> dict:
    row = await session.scalar(
        select(FinalCheck).where(
            FinalCheck.id == check_id,
            FinalCheck.enterprise_id == user.enterprise_id,
            FinalCheck.project_id == project_id,
        )
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="检查不存在")
    return {"check_id": row.id, "passed": row.passed, "issues": row.result.get("issues") if row.result else []}


@router.post("/{project_id}/export")
async def export_project(
    project_id: int,
    body: dict,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.DELIVERABLE_EXPORT)),
) -> dict:
    formats = body.get("formats") or ["docx", "xlsx"]
    deliverables = (
        await session.scalars(
            select(Deliverable).where(
                Deliverable.enterprise_id == user.enterprise_id,
                Deliverable.project_id == project_id,
            )
        )
    ).all()
    job = ExportJob(
        enterprise_id=user.enterprise_id,
        project_id=project_id,
        status=1,
        formats=formats,
        options={"with_manifest": body.get("with_manifest", True)},
    )
    session.add(job)
    await session.flush()

    files: list[dict] = []
    for deliverable in deliverables:
        if deliverable.current_version_no == 0:
            continue
        _, model = await deliverable_service.get_version_content(
            session, deliverable.id, deliverable.current_version_no
        )
        for fmt in formats:
            if fmt == "docx" and deliverable.deliverable_type in (1, 2):
                data = export_service.docx_bytes(model)
                ext = "docx"
            elif fmt == "xlsx" and deliverable.deliverable_type == 3:
                data = export_service.xlsx_bytes(model)
                ext = "xlsx"
            else:
                continue
            label = export_service.DELIVERABLE_NAMES.get(deliverable.deliverable_type, "成果")
            name = f"{label}_v{deliverable.current_version_no}.{ext}"
            saved = storage.save(data, user.enterprise_id, name)
            files.append(
                {
                    "name": name,
                    "bucket": saved["bucket"],
                    "object_key": saved["object_key"],
                    "sha256": saved["sha256"],
                    "size": saved["size_bytes"],
                }
            )

    checks = export_service.run_final_check(deliverables)
    manifest = export_service.build_manifest(project_id, files, checks)
    if body.get("with_manifest", True):
        manifest_bytes = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
        saved = storage.save(manifest_bytes, user.enterprise_id, "manifest.json")
        files.append(
            {
                "name": "manifest.json",
                "bucket": saved["bucket"],
                "object_key": saved["object_key"],
                "sha256": saved["sha256"],
                "size": saved["size_bytes"],
            }
        )
    job.status = 2
    job.files = files
    job.finished_at = datetime.now(timezone.utc)
    await session.commit()
    return {"job_id": job.id, "status": job.status, "files": files}


@router.get("/{project_id}/export/{job_id}")
async def export_status(
    project_id: int,
    job_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.FILE_READ)),
) -> dict:
    job = await session.scalar(
        select(ExportJob).where(
            ExportJob.id == job_id,
            ExportJob.enterprise_id == user.enterprise_id,
            ExportJob.project_id == project_id,
        )
    )
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="导出任务不存在")
    return {"job_id": job.id, "status": job.status, "files": job.files}


@router.get("/{project_id}/delivery-package")
async def delivery_package(
    project_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.DELIVERABLE_EXPORT)),
) -> StreamingResponse:
    job = await session.scalar(
        select(ExportJob)
        .where(
            ExportJob.enterprise_id == user.enterprise_id,
            ExportJob.project_id == project_id,
            ExportJob.status == 2,
        )
        .order_by(ExportJob.id.desc())
        .limit(1)
    )
    if job is None or not job.files:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="无已完成的导出任务")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in job.files:
            path = storage.open(item["bucket"], item["object_key"])
            zf.write(path, arcname=item["name"])
    buffer.seek(0)
    return StreamingResponse(
        buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="delivery_{project_id}.zip"'},
    )
