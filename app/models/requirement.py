"""招标解析结果模型（4.3.3）：requirement + requirement_revision。"""

from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, Numeric, SmallInteger, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigInt, JSONType, TimestampMixin


class Requirement(Base, TimestampMixin):
    __tablename__ = "requirement"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(BigInt, nullable=False, index=True)
    project_id: Mapped[int] = mapped_column(BigInt, nullable=False, index=True)
    req_type: Mapped[str] = mapped_column(String(50), nullable=False)
    req_key: Mapped[str | None] = mapped_column(String(100))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    structured: Mapped[dict | None] = mapped_column(JSONType)
    source_file_id: Mapped[int | None] = mapped_column(BigInt)
    coordinates: Mapped[list | None] = mapped_column(JSONType)
    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3))
    revision: Mapped[int] = mapped_column(BigInt, nullable=False, default=1)
    supersedes: Mapped[int | None] = mapped_column(BigInt)
    current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class RequirementRevision(Base, TimestampMixin):
    __tablename__ = "requirement_revision"
    __table_args__ = (UniqueConstraint("requirement_id", "revision_no", name="uq_req_rev"),)

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(BigInt, nullable=False, index=True)
    requirement_id: Mapped[int] = mapped_column(
        BigInt, ForeignKey("requirement.id"), nullable=False
    )
    revision_no: Mapped[int] = mapped_column(BigInt, nullable=False)
    req_key: Mapped[str | None] = mapped_column(String(100))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    structured: Mapped[dict | None] = mapped_column(JSONType)
    coordinates: Mapped[list | None] = mapped_column(JSONType)
    supersedes: Mapped[int | None] = mapped_column(BigInt)
    source_file_id: Mapped[int | None] = mapped_column(BigInt)
    source_task_id: Mapped[int | None] = mapped_column(BigInt)
