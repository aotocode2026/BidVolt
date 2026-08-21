"""终检与导出接口（4.10.1）。"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import UserContext, require_permission
from app.constants import Permission
from app.db import get_session
from app.models.deliverable import Deliverable
from app.models.export import ExportJob, FinalCheck
from app.services import deliverable_service, export_service
from app.services.quota_service import QuotaExceeded, check_export_daily
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
    # Issue #12：终检必须覆盖“成果是否逐条响应招标要求 + 文档质量”，而不仅是三类成果齐全。
    from app.models.requirement import Requirement

    requirements = (
        await session.scalars(
            select(Requirement).where(
                Requirement.enterprise_id == user.enterprise_id,
                Requirement.project_id == project_id,
                Requirement.current.is_(True),
            )
        )
    ).all()
    contents: dict[int, dict] = {}
    for d in deliverables:
        if d.current_version_no == 0:
            continue
        try:
            _, model = await deliverable_service.get_version_content(
                session, d.id, d.current_version_no
            )
            contents[d.id] = {"version_no": d.current_version_no, "model": model}
        except Exception:  # noqa: BLE001 单个成果读取失败不阻塞终检
            contents[d.id] = {}
    structure = [
        {"role": (r.structured or {}).get("role"), "title": r.content}
        for r in requirements
        if r.req_type == "doc_structure" and (r.structured or {}).get("role") in ("business", "technical", "price")
    ]
    result = export_service.run_final_check(
        deliverables,
        requirements=[
            {"id": r.id, "content": r.content, "req_type": r.req_type}
            for r in requirements
            if r.req_type != "doc_structure"
        ],
        contents=contents,
        structure=structure,
    )
    row = FinalCheck(
        enterprise_id=user.enterprise_id,
        project_id=project_id,
        status=2,
        passed=result["passed"],
        result=result,
    )
    session.add(row)
    await session.commit()
    return {
        "check_id": row.id,
        "passed": result["passed"],
        "issues": result["issues"],
        "stats": {
            "requirements": sum(1 for r in requirements if r.req_type != "doc_structure"),
            "structure": len(structure),
            "deliverables": len(deliverables),
            "error_count": sum(1 for i in result["issues"] if i["severity"] == "error"),
            "warning_count": sum(1 for i in result["issues"] if i["severity"] == "warning"),
            "words": result.get("words", {}),
            "pending": result.get("pending", {}),
        },
    }


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
    try:
        await check_export_daily(session, user.enterprise_id)
    except QuotaExceeded as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
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
                try:
                    data = await export_service.docx_bytes_with_source(
                        session, user.enterprise_id, model, project_id=project_id
                    )
                except ValueError as exc:
                    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
                ext = "docx"
            elif fmt == "xlsx" and deliverable.deliverable_type == 3:
                data = export_service.xlsx_bytes(model)
                ext = "xlsx"
            else:
                continue
            label = export_service.DELIVERABLE_NAMES.get(deliverable.deliverable_type, "成果")
            if ext == "docx":
                # 产品要求：文件名标注底稿来源
                draft_name = await export_service.template_source_name(
                    session, user.enterprise_id, model, project_id=project_id
                )
                name = f"{export_service._draft_label(draft_name, label)}.docx"
            else:
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
