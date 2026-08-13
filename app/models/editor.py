"""在线编辑会话模型（Issue #2 #41-#43）：会话租约 + 服务端检查点 + 完成回调。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, SmallInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigInt, JSONType, TimestampMixin


class EditorSession(Base, TimestampMixin):
    __tablename__ = "editor_session"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(BigInt, nullable=False, index=True)
    project_id: Mapped[int] = mapped_column(BigInt, nullable=False)
    deliverable_id: Mapped[int] = mapped_column(
        BigInt, ForeignKey("deliverable.id"), nullable=False, index=True
    )
    base_version_no: Mapped[int] = mapped_column(BigInt, nullable=False)
    status: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)  # 1 编辑中 2 已完成 3 已取消 4 已过期
    lease_token: Mapped[str] = mapped_column(String(128), nullable=False)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checkpoint: Mapped[dict | None] = mapped_column(JSONType)
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_version_no: Mapped[int | None] = mapped_column(BigInt)
    created_by: Mapped[int | None] = mapped_column(BigInt)
