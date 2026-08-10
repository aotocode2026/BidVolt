"""租户配额模型（4.11.1）。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigInt


class TenantQuota(Base):
    __tablename__ = "tenant_quota"

    enterprise_id: Mapped[int] = mapped_column(
        BigInt, ForeignKey("enterprise.id"), primary_key=True
    )
    storage_bytes: Mapped[int] = mapped_column(BigInt, nullable=False, default=10 * 1024**3)
    concurrent_tasks: Mapped[int] = mapped_column(BigInt, nullable=False, default=3)
    model_tokens_daily: Mapped[int] = mapped_column(BigInt, nullable=False, default=2_000_000)
    search_daily: Mapped[int] = mapped_column(BigInt, nullable=False, default=1000)
    export_daily: Mapped[int] = mapped_column(BigInt, nullable=False, default=50)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=datetime.utcnow
    )
