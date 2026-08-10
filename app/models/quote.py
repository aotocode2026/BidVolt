"""报价域模型（4.7.3）。"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Numeric, SmallInteger, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigInt, JSONType, TimestampMixin


class HistoryPriceSnapshot(Base, TimestampMixin):
    """外部历史报价的本地不可变快照（审计/复算用，不回写外部库）。"""

    __tablename__ = "history_price_snapshot"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(BigInt, nullable=False, index=True)
    provider_id: Mapped[str] = mapped_column(String(50), nullable=False)
    material_name: Mapped[str] = mapped_column(String(200), nullable=False)
    material_code: Mapped[str | None] = mapped_column(String(100))
    spec: Mapped[str | None] = mapped_column(String(200))
    region: Mapped[str | None] = mapped_column(String(100))
    win_price: Mapped[float] = mapped_column(Numeric(18, 2), nullable=False)
    win_date: Mapped[date] = mapped_column(Date, nullable=False)
    source_hash: Mapped[str | None] = mapped_column(String(64))
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class QuoteCalc(Base, TimestampMixin):
    __tablename__ = "quote_calc"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(BigInt, nullable=False, index=True)
    project_id: Mapped[int] = mapped_column(BigInt, nullable=False)
    params: Mapped[dict] = mapped_column(JSONType, nullable=False)
    result: Mapped[dict] = mapped_column(JSONType, nullable=False)
    strategy_results: Mapped[dict | None] = mapped_column(JSONType)
    ai_suggest: Mapped[dict | None] = mapped_column(JSONType)
    snapshot_refs: Mapped[list | None] = mapped_column(JSONType)
    status: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)  # 1 待确认 2 已应用 3 已放弃
    applied_version_no: Mapped[int | None] = mapped_column(BigInt)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
