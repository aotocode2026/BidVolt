"""成果与版本链模型（4.5/4.6）。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigInt, JSONType, TimestampMixin


class Deliverable(Base, TimestampMixin):
    __tablename__ = "deliverable"
    __table_args__ = (UniqueConstraint("project_id", "deliverable_type", name="uq_deliverable_type"),)

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(BigInt, nullable=False, index=True)
    project_id: Mapped[int] = mapped_column(BigInt, nullable=False)
    deliverable_type: Mapped[int] = mapped_column(SmallInteger, nullable=False)  # 1 商务 2 技术 3 报价
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    current_version_no: Mapped[int] = mapped_column(BigInt, nullable=False, default=0)
    stat: Mapped[dict | None] = mapped_column(JSONType)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=datetime.utcnow
    )


class DeliverableContent(Base, TimestampMixin):
    """内容去重：同 content_hash 只存一份 JSON。"""

    __tablename__ = "deliverable_content"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    content_json: Mapped[dict] = mapped_column(JSONType, nullable=False)


class DeliverableVersion(Base, TimestampMixin):
    __tablename__ = "deliverable_version"
    __table_args__ = (UniqueConstraint("deliverable_id", "version_no", name="uq_dv_no"),)

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(BigInt, nullable=False, index=True)
    deliverable_id: Mapped[int] = mapped_column(
        BigInt, ForeignKey("deliverable.id"), nullable=False, index=True
    )
    version_no: Mapped[int] = mapped_column(BigInt, nullable=False)
    version_type: Mapped[int] = mapped_column(SmallInteger, nullable=False)  # 1 原始 2 AI生成 3 AI校核 4 用户 5 报价应用
    milestone: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    content_id: Mapped[int] = mapped_column(
        BigInt, ForeignKey("deliverable_content.id"), nullable=False
    )
    created_by: Mapped[int | None] = mapped_column(BigInt)
    source_task_id: Mapped[int | None] = mapped_column(BigInt)
    idempotency_key: Mapped[str | None] = mapped_column(String(100))


class AIEditDiff(Base, TimestampMixin):
    __tablename__ = "ai_edit_diff"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    deliverable_id: Mapped[int] = mapped_column(
        BigInt, ForeignKey("deliverable.id"), nullable=False
    )
    base_version_no: Mapped[int] = mapped_column(BigInt, nullable=False)
    diff: Mapped[dict] = mapped_column(JSONType, nullable=False)
    applied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
