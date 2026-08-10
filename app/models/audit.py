"""操作审计模型（4.11.3）。"""

from __future__ import annotations

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigInt, JSONType, TimestampMixin


class AuditLog(Base, TimestampMixin):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(BigInt, nullable=False, index=True)
    project_id: Mapped[int | None] = mapped_column(BigInt)
    user_id: Mapped[int | None] = mapped_column(BigInt)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    object_type: Mapped[str | None] = mapped_column(String(50))
    object_id: Mapped[int | None] = mapped_column(BigInt)
    payload: Mapped[dict | None] = mapped_column(JSONType)
