"""项目助手会话模型（Issue #2 #47-#49）：会话 + 消息历史。"""

from __future__ import annotations

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigInt, TimestampMixin


class Conversation(Base, TimestampMixin):
    __tablename__ = "conversation"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(BigInt, nullable=False, index=True)
    project_id: Mapped[int] = mapped_column(BigInt, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="项目助手")
    created_by: Mapped[int | None] = mapped_column(BigInt)


class ConversationMessage(Base, TimestampMixin):
    __tablename__ = "conversation_message"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(BigInt, nullable=False, index=True)
    project_id: Mapped[int] = mapped_column(BigInt, nullable=False, index=True)
    conversation_id: Mapped[int] = mapped_column(
        BigInt, ForeignKey("conversation.id"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # user / assistant
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source_task_id: Mapped[int | None] = mapped_column(BigInt)
