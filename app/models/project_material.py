"""项目材料域模型（4.2.6）。"""

from __future__ import annotations

from sqlalchemy import (
    ForeignKey,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigInt, JSONType, TimestampMixin


class ProjectMaterial(Base, TimestampMixin):
    __tablename__ = "project_material"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(BigInt, nullable=False, index=True)
    project_id: Mapped[int] = mapped_column(BigInt, nullable=False, index=True)
    file_id: Mapped[int] = mapped_column(BigInt, ForeignKey("file_object.id"), nullable=False)
    status: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)  # 1 待解析 2 已解析 3 需人工


class ProjectMaterialRevision(Base, TimestampMixin):
    __tablename__ = "project_material_revision"
    __table_args__ = (UniqueConstraint("material_id", "revision_no", name="uq_pmr_rev"),)

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    material_id: Mapped[int] = mapped_column(
        BigInt, ForeignKey("project_material.id"), nullable=False
    )
    revision_no: Mapped[int] = mapped_column(BigInt, nullable=False)
    supersedes: Mapped[int | None] = mapped_column(BigInt)
    conflict: Mapped[str | None] = mapped_column(Text)
    source_file_id: Mapped[int | None] = mapped_column(BigInt)


class ProjectEvent(Base, TimestampMixin):
    __tablename__ = "project_event"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(BigInt, nullable=False)
    project_id: Mapped[int] = mapped_column(BigInt, nullable=False)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    event_data: Mapped[dict | None] = mapped_column(JSONType)


class MaterialMatchResult(Base, TimestampMixin):
    __tablename__ = "material_match_result"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(BigInt, nullable=False)
    project_id: Mapped[int] = mapped_column(BigInt, nullable=False)
    requirement_id: Mapped[int | None] = mapped_column(BigInt)
    asset_id: Mapped[int | None] = mapped_column(BigInt)
    matched: Mapped[int] = mapped_column(SmallInteger, nullable=False)  # 1 已匹配 2 部分 3 缺失
    gap_desc: Mapped[str | None] = mapped_column(Text)
    affected_score_item: Mapped[str | None] = mapped_column(String(100))
    impact_score: Mapped[float | None] = mapped_column(Numeric(6, 2))
    suggestion: Mapped[str | None] = mapped_column(Text)
    source_task_id: Mapped[int | None] = mapped_column(BigInt)


class ProjectSnapshot(Base, TimestampMixin):
    __tablename__ = "project_snapshot"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(BigInt, nullable=False)
    project_id: Mapped[int] = mapped_column(BigInt, nullable=False)
    snapshot_type: Mapped[str] = mapped_column(String(30), nullable=False)
    input_refs: Mapped[dict] = mapped_column(JSONType, nullable=False)
    external_samples: Mapped[dict | None] = mapped_column(JSONType)
    rules_version: Mapped[dict | None] = mapped_column(JSONType)
    manifest: Mapped[dict | None] = mapped_column(JSONType)
