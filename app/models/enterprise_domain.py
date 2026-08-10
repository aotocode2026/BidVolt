"""企业资料域模型（4.2.5）。"""

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


class EnterpriseAssetCategory(Base, TimestampMixin):
    __tablename__ = "enterprise_asset_category"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(BigInt, nullable=False, index=True)
    parent_id: Mapped[int | None] = mapped_column(BigInt)
    name: Mapped[str] = mapped_column(String(200), nullable=False)


class EnterpriseAsset(Base, TimestampMixin):
    __tablename__ = "enterprise_asset"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(BigInt, nullable=False, index=True)
    category_id: Mapped[int | None] = mapped_column(
        BigInt, ForeignKey("enterprise_asset_category.id")
    )
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    asset_type: Mapped[str] = mapped_column(String(50), nullable=False, default="other")
    source_file_id: Mapped[int | None] = mapped_column(
        BigInt, ForeignKey("file_object.id")
    )
    status: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)  # 1 待分类 2 待确认 3 已确认
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=datetime.utcnow
    )


class EnterpriseAssetRevision(Base, TimestampMixin):
    __tablename__ = "enterprise_asset_revision"
    __table_args__ = (UniqueConstraint("asset_id", "revision_no", name="uq_ear_rev"),)

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    asset_id: Mapped[int] = mapped_column(
        BigInt, ForeignKey("enterprise_asset.id"), nullable=False
    )
    revision_no: Mapped[int] = mapped_column(BigInt, nullable=False)
    file_id: Mapped[int] = mapped_column(BigInt, ForeignKey("file_object.id"), nullable=False)
    source_location: Mapped[list | None] = mapped_column(JSONType)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[int | None] = mapped_column(BigInt)


class EnterpriseFact(Base, TimestampMixin):
    __tablename__ = "enterprise_fact"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(BigInt, nullable=False, index=True)
    asset_id: Mapped[int] = mapped_column(
        BigInt, ForeignKey("enterprise_asset.id"), nullable=False
    )
    fact_key: Mapped[str] = mapped_column(String(100), nullable=False)
    fact_value: Mapped[dict] = mapped_column(JSONType, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3))
    status: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)  # 1 待确认 2 已确认 3 已纠正
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=datetime.utcnow
    )


class EnterpriseFactEvidence(Base, TimestampMixin):
    __tablename__ = "enterprise_fact_evidence"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    fact_id: Mapped[int] = mapped_column(
        BigInt, ForeignKey("enterprise_fact.id"), nullable=False
    )
    asset_revision_id: Mapped[int] = mapped_column(
        BigInt, ForeignKey("enterprise_asset_revision.id"), nullable=False
    )
    source_range: Mapped[dict | None] = mapped_column(JSONType)


class EnterpriseIngestionTask(Base, TimestampMixin):
    __tablename__ = "enterprise_ingestion_task"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(BigInt, nullable=False)
    task_id: Mapped[int] = mapped_column(BigInt, ForeignKey("task.id"), nullable=False)
    asset_ids: Mapped[list] = mapped_column(JSONType, nullable=False)
    status: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)  # 1 分类中 2 待确认 3 完成
