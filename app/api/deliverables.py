"""成果与版本接口（4.5.2/4.6.2）。"""

from __future__ import annotations

import json
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import UserContext, require_capability, require_permission
from app.constants import Permission
from app.db import get_session
from app.models.deliverable import AIEditDiff, Deliverable, DeliverableVersion
from app.models.project import Project
from app.services import deliverable_service
from app.services.audit import write_audit
from app.services.deliverable_service import VersionConflict

router = APIRouter(prefix="/deliverables", tags=["deliverables"])


async def _get_deliverable(session: AsyncSession, user: UserContext, deliverable_id: int) -> Deliverable:
    d = await session.scalar(
        select(Deliverable).where(
            Deliverable.id == deliverable_id,
            Deliverable.enterprise_id == user.enterprise_id,
        )
    )
    if d is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="成果不存在")
    return d


def _handle_version_conflict(exc: VersionConflict) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_deliverable(
    body: dict,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_capability("create_deliverable")),
) -> dict:
    # MCP（capability）路径：project_id 强制等于任务绑定项目，防止越权写其他项目
    cap_payload = getattr(request.state, "cap_payload", None)
    if cap_payload is not None:
        if int(body.get("project_id") or 0) != int(cap_payload["pid"]):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="capability token 不允许写该项目")
        body["project_id"] = int(cap_payload["pid"])
    project = await session.scalar(
        select(Project).where(
            Project.id == body["project_id"],
            Project.enterprise_id == user.enterprise_id,
            Project.is_deleted.is_(False),
        )
    )
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="项目不存在")
    d = await deliverable_service.create_deliverable(
        session,
        enterprise_id=user.enterprise_id,
        project_id=body["project_id"],
        deliverable_type=body["deliverable_type"],
        title=body["title"],
    )
    await session.commit()
    return {"deliverable_id": d.id, "deliverable_type": d.deliverable_type, "title": d.title}


@router.get("")
async def list_deliverables(
    project_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.FILE_READ)),
) -> list[dict]:
    rows = await session.scalars(
        select(Deliverable).where(
            Deliverable.enterprise_id == user.enterprise_id,
            Deliverable.project_id == project_id,
        )
    )
    return [
        {
            "deliverable_id": d.id,
            "deliverable_type": d.deliverable_type,
            "title": d.title,
            "current_version_no": d.current_version_no,
            "stat": d.stat,
        }
        for d in rows
    ]


@router.get("/{deliverable_id}")
async def deliverable_info(
    deliverable_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.FILE_READ)),
) -> dict:
    d = await _get_deliverable(session, user, deliverable_id)
    return {
        "deliverable_id": d.id,
        "project_id": d.project_id,
        "deliverable_type": d.deliverable_type,
        "title": d.title,
        "current_version_no": d.current_version_no,
    }


@router.get("/{deliverable_id}/versions")
async def version_list(
    deliverable_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.FILE_READ)),
) -> list[dict]:
    await _get_deliverable(session, user, deliverable_id)
    rows = await session.scalars(
        select(DeliverableVersion)
        .where(DeliverableVersion.deliverable_id == deliverable_id)
        .order_by(DeliverableVersion.version_no.desc())
    )
    return [
        {
            "version_no": v.version_no,
            "version_type": v.version_type,
            "milestone": v.milestone,
            "created_by": v.created_by,
            "source_task_id": v.source_task_id,
            "created_at": v.created_at.isoformat() if v.created_at else None,
        }
        for v in rows
    ]


@router.get("/{deliverable_id}/versions/{version_no}")
async def version_content(
    deliverable_id: int,
    version_no: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.FILE_READ)),
) -> dict:
    await _get_deliverable(session, user, deliverable_id)
    try:
        version, content = await deliverable_service.get_version_content(
            session, deliverable_id, version_no
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return {"version_no": version.version_no, "version_type": version.version_type, "model": content}


@router.get("/{deliverable_id}/versions/{version_no}/download")
async def download_version(
    deliverable_id: int,
    version_no: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.FILE_DOWNLOAD)),
) -> Response:
    """下载指定成果版本（Issue #2 #40）：商务/技术标→DOCX，报价单→XLSX。"""
    d = await _get_deliverable(session, user, deliverable_id)
    try:
        version, content = await deliverable_service.get_version_content(
            session, deliverable_id, version_no
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    from app.services import export_service

    if d.deliverable_type == 3:
        data = export_service.xlsx_bytes(content)
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ext = "xlsx"
    elif d.deliverable_type in (1, 2):
        from app.models.requirement import Requirement

        structure_rows = (
            await session.scalars(
                select(Requirement).where(
                    Requirement.enterprise_id == user.enterprise_id,
                    Requirement.project_id == d.project_id,
                    Requirement.current.is_(True),
                    Requirement.req_type == "doc_structure",
                )
            )
        ).all()
        format_spec = next(
            ((r.structured or {}).get("spec") for r in structure_rows if (r.structured or {}).get("role") == "format"),
            None,
        )
        data = await export_service.docx_bytes_with_source(
            session, user.enterprise_id, content, format_spec=format_spec
        )
        media = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ext = "docx"
    else:
        data = json.dumps(content, ensure_ascii=False).encode("utf-8")
        media = "application/json"
        ext = "json"
    fname = f"{d.title}_v{version_no}.{ext}"
    return Response(
        content=data,
        media_type=media,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"},
    )


@router.post("/{deliverable_id}/versions", status_code=status.HTTP_201_CREATED)
async def save_version(
    deliverable_id: int,
    body: dict,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_capability("save_deliverable")),
) -> dict:
    d = await _get_deliverable(session, user, deliverable_id)
    try:
        version = await deliverable_service.save_version(
            session,
            d,
            body["content"],
            version_type=body.get("version_type", 4),
            created_by=user.user_id,
            expected_version_no=body.get("expected_version_no"),
            idempotency_key=body.get("idempotency_key"),
            source_task_id=body.get("source_task_id"),
        )
    except VersionConflict as exc:
        raise _handle_version_conflict(exc) from exc
    await write_audit(
        session,
        enterprise_id=user.enterprise_id,
        user_id=user.user_id,
        project_id=d.project_id,
        action="version_save",
        object_type="deliverable_version",
        object_id=version.id,
        payload={"version_no": version.version_no, "version_type": version.version_type},
    )
    await session.commit()
    return {
        "version_no": version.version_no,
        "version_id": version.id,
        "milestone": version.milestone,
    }


@router.post("/{deliverable_id}/restore/{version_no}", status_code=status.HTTP_201_CREATED)
async def restore(
    deliverable_id: int,
    version_no: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.DELIVERABLE_EDIT)),
) -> dict:
    d = await _get_deliverable(session, user, deliverable_id)
    try:
        version = await deliverable_service.restore_version(session, d, version_no, user.user_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    await write_audit(
        session,
        enterprise_id=user.enterprise_id,
        user_id=user.user_id,
        project_id=d.project_id,
        action="version_restore",
        object_type="deliverable_version",
        object_id=version.id,
        payload={"restored_from": version_no, "new_version_no": version.version_no},
    )
    await session.commit()
    return {"version_no": version.version_no, "version_id": version.id}


@router.get("/{deliverable_id}/content")
async def current_content(
    deliverable_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_capability("get_deliverable_content")),
) -> dict:
    d = await _get_deliverable(session, user, deliverable_id)
    if d.current_version_no == 0:
        return {"deliverable_id": d.id, "version_no": 0, "model": {"nodes": []}}
    _, content = await deliverable_service.get_version_content(session, d.id, d.current_version_no)
    return {"deliverable_id": d.id, "deliverable_type": d.deliverable_type, "version_no": d.current_version_no, "model": content}


@router.put("/{deliverable_id}/content", status_code=status.HTTP_201_CREATED)
async def put_content(
    deliverable_id: int,
    body: dict,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.DELIVERABLE_EDIT)),
) -> dict:
    d = await _get_deliverable(session, user, deliverable_id)
    try:
        version = await deliverable_service.save_version(
            session,
            d,
            body["content"],
            version_type=4,
            created_by=user.user_id,
            expected_version_no=body.get("expected_version_no"),
            idempotency_key=body.get("idempotency_key"),
        )
    except VersionConflict as exc:
        raise _handle_version_conflict(exc) from exc
    await session.commit()
    return {"version_no": version.version_no, "version_id": version.id, "milestone": version.milestone}


@router.get("/{deliverable_id}/diff")
async def diff_versions(
    deliverable_id: int,
    from_version: int,
    to_version: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.FILE_READ)),
) -> dict:
    await _get_deliverable(session, user, deliverable_id)
    try:
        _, prev = await deliverable_service.get_version_content(session, deliverable_id, from_version)
        _, curr = await deliverable_service.get_version_content(session, deliverable_id, to_version)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return {"from": from_version, "to": to_version, "operations": deliverable_service.compute_diff(prev, curr)}


@router.post("/{deliverable_id}/ai-edit")
async def ai_edit(
    deliverable_id: int,
    body: dict,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.DELIVERABLE_EDIT)),
) -> dict:
    d = await _get_deliverable(session, user, deliverable_id)
    base_version_no = body.get("base_version_no", d.current_version_no)
    if base_version_no != d.current_version_no:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="版本已更新，请基于最新版本重新选择")
    _, content = await deliverable_service.get_version_content(session, d.id, base_version_no)
    try:
        diff = await deliverable_service.generate_edit_diff(content, body["selection"], body["instruction"])
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    row = AIEditDiff(deliverable_id=d.id, base_version_no=base_version_no, diff=diff)
    session.add(row)
    await session.commit()
    return {"diff_id": row.id, "operations": diff["operations"]}


@router.get("/{deliverable_id}/ai-edit/{diff_id}")
async def get_ai_edit(
    deliverable_id: int,
    diff_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.FILE_READ)),
) -> dict:
    await _get_deliverable(session, user, deliverable_id)
    row = await session.get(AIEditDiff, diff_id)
    if row is None or row.deliverable_id != deliverable_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="diff 不存在")
    return {"diff_id": row.id, "base_version_no": row.base_version_no, "diff": row.diff, "applied": row.applied}


@router.post("/{deliverable_id}/ai-edit/{diff_id}/apply", status_code=status.HTTP_201_CREATED)
async def apply_ai_edit(
    deliverable_id: int,
    diff_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.DELIVERABLE_EDIT)),
) -> dict:
    d = await _get_deliverable(session, user, deliverable_id)
    row = await session.get(AIEditDiff, diff_id)
    if row is None or row.deliverable_id != deliverable_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="diff 不存在")
    if row.applied:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="diff 已应用")
    if row.base_version_no != d.current_version_no:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="基准版本已过期，请重新生成")
    _, content = await deliverable_service.get_version_content(session, d.id, d.current_version_no)
    try:
        new_content = deliverable_service.apply_diff(content, row.diff)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    version = await deliverable_service.save_version(
        session, d, new_content, version_type=4, created_by=user.user_id
    )
    row.applied = True
    await write_audit(
        session,
        enterprise_id=user.enterprise_id,
        user_id=user.user_id,
        project_id=d.project_id,
        action="ai_edit_apply",
        object_type="deliverable_version",
        object_id=version.id,
        payload={"diff_id": diff_id, "version_no": version.version_no},
    )
    await session.commit()
    return {"version_no": version.version_no, "version_id": version.id}
