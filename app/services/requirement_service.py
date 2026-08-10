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
    content: str,
    structured: dict | None = None,
    coordinates: list | None = None,
    confidence: float | None = None,
    source_file_id: int | None = None,
    source_task_id: int | None = None,
) -> Requirement:
    """同类型同内容视为幂等；新内容覆盖旧条目（supersedes + revision 递增）。"""
    existing = await session.scalar(
        select(Requirement).where(
            Requirement.enterprise_id == enterprise_id,
            Requirement.project_id == project_id,
            Requirement.req_type == req_type,
            Requirement.content == content,
            Requirement.current.is_(True),
        )
    )
    if existing is not None:
        return existing

    previous = await session.scalar(
        select(Requirement)
        .where(
            Requirement.enterprise_id == enterprise_id,
            Requirement.project_id == project_id,
            Requirement.req_type == req_type,
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
