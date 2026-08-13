"""招标要求写入/查询（补遗覆盖 + revision/supersedes）。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.requirement import Requirement, RequirementRevision


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
