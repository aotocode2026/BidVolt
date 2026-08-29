"""项目模型（4.1.6）。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, SmallInteger, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigInt, TimestampMixin


class Project(Base, TimestampMixin):
    __tablename__ = "project"
    __table_args__ = (Index("idx_project_ent_status", "enterprise_id", "status"),)

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(BigInt, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    tender_no: Mapped[str | None] = mapped_column(String(100))
    buyer: Mapped[str | None] = mapped_column(String(300))  # 招标人（Issue #6 P1）
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    note: Mapped[str | None] = mapped_column(Text)
    # 任务前对话会话 id：客户在发起 agent-run 前与主会话聊天建立的会话，
    # 开跑时把任务 prompt 注入该会话（resume），任务前交代自动成为上下文
    pre_chat_session_id: Mapped[str | None] = mapped_column(String(64))
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=datetime.utcnow
    )
