"""终检与导出接口（4.10.1）。"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timezone
from urllib.parse import quote

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


@router.get("/{project_id}/response-package")
async def response_package(
    project_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.DELIVERABLE_EXPORT)),
) -> StreamingResponse:
    """响应文件包：
    - 新方案（agent_pipeline 任务）：主会话经成文工具链自主成文并打包，
      本端点只取主会话打包好的 zip（尚未打包 → 409 指引重新发起 agent-run）；
    - 旧任务：维持原服务端成文逻辑（build_response_package），完全不变。"""
    from sqlalchemy import select as sa_select

    from app.constants import TaskType
    from app.models.agent import AgentArtifact
    from app.models.task import Task

    pipe_task = await session.scalar(
        sa_select(Task)
        .where(
            Task.enterprise_id == user.enterprise_id,
            Task.project_id == project_id,
            Task.task_type == TaskType.AGENT_PIPELINE,
        )
        .order_by(Task.id.desc())
        .limit(1)
    )
    if pipe_task is not None:
        # 取该项目主会话最近一次打包的 zip（续跑链上的旧包仍有效：
        # 续跑任务若只补评审/评分等非成文步骤，会沿用上一单的包）
        zip_art = await session.scalar(
            sa_select(AgentArtifact)
            .where(
                AgentArtifact.enterprise_id == user.enterprise_id,
                AgentArtifact.project_id == project_id,
                AgentArtifact.kind == "zip",
            )
            .order_by(AgentArtifact.id.desc())
            .limit(1)
        )
        if zip_art is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Agent 主会话尚未成文打包：请重新发起 agent-run，主会话将在交付阶段"
                        "经成文工具链（切片→填空→追加→校验→封存→打包）生成响应文件包。",
            )
        return StreamingResponse(
            io.BytesIO(zip_art.content),
            media_type="application/zip",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(zip_art.name)}"},
        )

    try:
        data = await export_service.build_response_package(session, user.enterprise_id, project_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    buffer = io.BytesIO(data)
    draft_name = "采购文件"
    try:
        from sqlalchemy import select as sa_select

        from app.models.file import FileObject

        rows = (
            await session.scalars(
                sa_select(FileObject).where(
                    FileObject.project_id == project_id,
                    FileObject.enterprise_id == user.enterprise_id,
                    FileObject.is_deleted.is_(False),
                    FileObject.owner_type == 2,
                )
            )
        ).all()
        docx_rows = [f for f in rows if (f.ext or "").strip(".").lower() == "docx"]
        fobj = next((f for f in docx_rows if f.document_role == "tender"), None) or (
            max(docx_rows, key=lambda f: f.size_bytes) if docx_rows else None
        )
        if fobj is not None:
            draft_name = str(fobj.original_name or "").strip() or draft_name
    except Exception:  # noqa: BLE001 文件名兜底
        pass
    fname = f"响应文件包(底稿：{draft_name}).zip"
    return StreamingResponse(
        buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"},
    )
