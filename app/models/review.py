"""评审域模型（4.8.3）：review_provider / review_run / score_record / review_item / review_material_link。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigInt, JSONType, TimestampMixin


class ReviewProvider(Base, TimestampMixin):
    __tablename__ = "review_provider"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(BigInt, nullable=False)
    provider_type: Mapped[str] = mapped_column(String(20), nullable=False)  # document / code / api
    provider_code: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    provider_version: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str | None] = mapped_column(String(50))
    severity: Mapped[int | None] = mapped_column(SmallInteger)
    config: Mapped[dict | None] = mapped_column(JSONType)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class ReviewRun(Base, TimestampMixin):
    __tablename__ = "review_run"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(BigInt, nullable=False)
    project_id: Mapped[int] = mapped_column(BigInt, nullable=False)
    snapshot_id: Mapped[int] = mapped_column(BigInt, nullable=False)
    provider_id: Mapped[int] = mapped_column(
        BigInt, ForeignKey("review_provider.id"), nullable=False
    )
    review_run_id: Mapped[str | None] = mapped_column(String(100))
    provider_raw_hash: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)  # 1 运行中 2 完成 3 失败


class ScoreRecord(Base, TimestampMixin):
    __tablename__ = "score_record"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(BigInt, nullable=False)
    project_id: Mapped[int] = mapped_column(BigInt, nullable=False)
    review_run_id: Mapped[int | None] = mapped_column(
        BigInt, ForeignKey("review_run.id")
    )
    total_score: Mapped[float | None] = mapped_column(Numeric(6, 2))
    biz_score: Mapped[float | None] = mapped_column(Numeric(6, 2))
    tech_score: Mapped[float | None] = mapped_column(Numeric(6, 2))
    quote_score: Mapped[float | None] = mapped_column(Numeric(6, 2))
    reject_count: Mapped[int] = mapped_column(BigInt, nullable=False, default=0)
    missing_count: Mapped[int] = mapped_column(BigInt, nullable=False, default=0)
    improvable: Mapped[float | None] = mapped_column(Numeric(6, 2))
    detail: Mapped[dict | None] = mapped_column(JSONType)


class ReviewItem(Base, TimestampMixin):
    __tablename__ = "review_item"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(BigInt, nullable=False, index=True)
    project_id: Mapped[int] = mapped_column(BigInt, nullable=False)
    review_run_id: Mapped[int] = mapped_column(
        BigInt, ForeignKey("review_run.id"), nullable=False
    )
    score_id: Mapped[int | None] = mapped_column(
        BigInt, ForeignKey("score_record.id")
    )
    requirement_id: Mapped[int | None] = mapped_column(BigInt)
    criterion_id: Mapped[str | None] = mapped_column(String(100))
    ruleset_version: Mapped[str] = mapped_column(String(50), nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False)
    problem_description: Mapped[str] = mapped_column(Text, nullable=False)
    got: Mapped[float | None] = mapped_column(Numeric(6, 2))
    full: Mapped[float | None] = mapped_column(Numeric(6, 2))
    improvable: Mapped[float | None] = mapped_column(Numeric(6, 2))
    risk_level: Mapped[int | None] = mapped_column(SmallInteger)
    suggestion: Mapped[str | None] = mapped_column(Text)
    action_type: Mapped[str | None] = mapped_column(String(30))  # upload_material / edit_deliverable / manual_review
    evidence: Mapped[dict] = mapped_column(JSONType, nullable=False)
    missing_material_types: Mapped[str | None] = mapped_column(String(200))
    related_deliverable_node: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)  # 1 pending 2 confirmed 3 rejected 4 re_reviewed
    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3))
    expected_version: Mapped[str | None] = mapped_column(String(100))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=datetime.utcnow
    )


class ReviewMaterialLink(Base, TimestampMixin):
    __tablename__ = "review_material_link"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(BigInt, nullable=False)
    project_id: Mapped[int] = mapped_column(BigInt, nullable=False)
    review_item_id: Mapped[int] = mapped_column(
        BigInt, ForeignKey("review_item.id"), nullable=False
    )
    material_id: Mapped[int] = mapped_column(BigInt, nullable=False)
    material_type: Mapped[str] = mapped_column(String(20), nullable=False, default="project")
    match_basis: Mapped[str | None] = mapped_column(Text)
    source_location: Mapped[str | None] = mapped_column(String(500))
    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3))
