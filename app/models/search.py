"""搜索域模型（4.9.3）：search_source / citation。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigInt, JSONType, TimestampMixin


class SearchSource(Base, TimestampMixin):
    __tablename__ = "search_source"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(BigInt, nullable=False, index=True)
    project_id: Mapped[int | None] = mapped_column(BigInt)
    query: Mapped[str | None] = mapped_column(String(500))
    url: Mapped[str] = mapped_column(String(1000), nullable=False)
    title: Mapped[str | None] = mapped_column(String(500))
    snippet: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    trust_level: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    domain: Mapped[str | None] = mapped_column(String(200))
    extra: Mapped[dict | None] = mapped_column(JSONType)


class Citation(Base, TimestampMixin):
    __tablename__ = "citation"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(BigInt, nullable=False, index=True)
    project_id: Mapped[int] = mapped_column(BigInt, nullable=False)
    deliverable_id: Mapped[int] = mapped_column(BigInt, nullable=False, index=True)
    version_no: Mapped[int] = mapped_column(BigInt, nullable=False)
    node_id: Mapped[str | None] = mapped_column(String(100))
    source_id: Mapped[int] = mapped_column(
        BigInt, ForeignKey("search_source.id"), nullable=False
    )
    quote_text: Mapped[str | None] = mapped_column(Text)
