"""招标要求写入/查询（补遗覆盖 + revision/supersedes + 用户确认/修正闭环）。"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.requirement import Requirement, RequirementRevision


class RevisionConflict(Exception):
    """expected_revision 与实际 revision 不一致（CAS 冲突，HTTP 409）。"""

    def __init__(self, req_id: int, expected: int, actual: int) -> None:
        super().__init__(f"requirement {req_id} revision 冲突：expected={expected} actual={actual}")
        self.req_id = req_id
        self.expected = expected
        self.actual = actual


async def get_owned_requirement(
    session: AsyncSession,
    *,
    enterprise_id: int,
    project_id: int,
    req_id: int,
) -> Requirement | None:
    return await session.scalar(
        select(Requirement).where(
            Requirement.id == req_id,
            Requirement.enterprise_id == enterprise_id,
            Requirement.project_id == project_id,
        )
    )


async def confirm_requirement(
    session: AsyncSession,
    *,
    enterprise_id: int,
    project_id: int,
    req_id: int,
    expected_revision: int,
    confirmed: bool,
) -> Requirement:
    """用户确认/拒绝一条要求（Issue #6 P0）：revision CAS + 审计由调用方写。"""
    req = await get_owned_requirement(
        session, enterprise_id=enterprise_id, project_id=project_id, req_id=req_id
    )
    if req is None:
        from fastapi import HTTPException, status

        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="要求不存在")
    if req.revision != expected_revision:
        raise RevisionConflict(req_id, expected_revision, req.revision)
    req.confirm_status = "confirmed" if confirmed else "rejected"
    req.confirmed_at = datetime.now(timezone.utc)
    return req


async def correct_requirement(
    session: AsyncSession,
    *,
    enterprise_id: int,
    project_id: int,
    req_id: int,
    expected_revision: int,
    content: str,
    coordinates: list | None = None,
    confidence: float | None = None,
    structured: dict | None = None,
    source_file_id: int | None = None,
) -> Requirement:
    """用户修正一条要求（Issue #6 P0）：生成新 revision（supersede），CAS 保护。"""
    req = await get_owned_requirement(
        session, enterprise_id=enterprise_id, project_id=project_id, req_id=req_id
    )
    if req is None:
        from fastapi import HTTPException, status

        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="要求不存在")
    if not req.current:
        raise RevisionConflict(req_id, expected_revision, req.revision)
    if req.revision != expected_revision:
        raise RevisionConflict(req_id, expected_revision, req.revision)

    new_revision = req.revision + 1
    corrected = Requirement(
        enterprise_id=enterprise_id,
        project_id=project_id,
        req_type=req.req_type,
        req_key=req.req_key,
        content=content,
        structured=structured,
        coordinates=coordinates,
        confidence=confidence,
        source_file_id=source_file_id or req.source_file_id,
        revision=new_revision,
        supersedes=req.id,
        current=True,
        confirm_status="unconfirmed",
    )
    session.add(corrected)
    await session.flush()
    session.add(
        RequirementRevision(
            enterprise_id=enterprise_id,
            requirement_id=corrected.id,
            revision_no=new_revision,
            req_key=req.req_key,
            content=content,
            structured=structured,
            coordinates=coordinates,
            supersedes=req.id,
            source_file_id=corrected.source_file_id,
        )
    )
    req.current = False
    return corrected


async def upsert_requirement(
    session: AsyncSession,
    *,
    enterprise_id: int,
    project_id: int,
    req_type: str,
    req_key: str | None = None,
    content: str,
    structured: dict | None = None,
    coordinates: list | None = None,
    confidence: float | None = None,
    source_file_id: int | None = None,
    source_task_id: int | None = None,
) -> Requirement:
    """写入/更新招标要求。

    - 有 req_key：以 key 为稳定身份做幂等与 supersede（补遗/修订递增 revision）。
    - 无 req_key：同 req_type 允许存在多条（Issue #2 #11），仅按 (req_type, content, req_key IS NULL)
      做精确重复幂等，不再按 req_type 覆盖。
    """
    idempotency_clauses = [
        Requirement.enterprise_id == enterprise_id,
        Requirement.project_id == project_id,
        Requirement.req_type == req_type,
        Requirement.content == content,
        Requirement.current.is_(True),
    ]
    if req_key:
        idempotency_clauses.append(Requirement.req_key == req_key)
    else:
        idempotency_clauses.append(Requirement.req_key.is_(None))
    existing = await session.scalar(select(Requirement).where(*idempotency_clauses))
    if existing is not None:
        return existing

    previous = None
    if req_key:
        previous = await session.scalar(
            select(Requirement)
            .where(
                Requirement.enterprise_id == enterprise_id,
                Requirement.project_id == project_id,
                Requirement.req_key == req_key,
                Requirement.current.is_(True),
            )
            .order_by(Requirement.revision.desc())
            .limit(1)
        )
    revision_no = (previous.revision + 1) if previous else 1
    requirement = Requirement(
        enterprise_id=enterprise_id,
        project_id=project_id,
        req_type=req_type,
        req_key=req_key,
        content=content,
        structured=structured,
        coordinates=coordinates,
        confidence=confidence,
        source_file_id=source_file_id,
        revision=revision_no,
        supersedes=previous.id if previous else None,
        current=True,
    )
    session.add(requirement)
    await session.flush()
    session.add(
        RequirementRevision(
            enterprise_id=enterprise_id,
            requirement_id=requirement.id,
            revision_no=revision_no,
            req_key=req_key,
            content=content,
            structured=structured,
            coordinates=coordinates,
            supersedes=requirement.supersedes,
            source_file_id=source_file_id,
            source_task_id=source_task_id,
        )
    )
    if previous is not None:
        previous.current = False
    return requirement


async def list_requirements(session: AsyncSession, enterprise_id: int, project_id: int) -> list[Requirement]:
    return (
        await session.scalars(
            select(Requirement).where(
                Requirement.enterprise_id == enterprise_id,
                Requirement.project_id == project_id,
                Requirement.current.is_(True),
            )
        )
    ).all()
