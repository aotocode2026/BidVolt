"""Agent 主会话事件模型（新方案）：会话控制台的消息/回复/工具/委派逐条记录。"""

from __future__ import annotations

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigInt, TimestampMixin


class AgentSessionEvent(Base, TimestampMixin):
    __tablename__ = "agent_session_event"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(BigInt, nullable=False, index=True)
    project_id: Mapped[int] = mapped_column(BigInt, nullable=False, index=True)
    task_id: Mapped[int] = mapped_column(BigInt, nullable=False, index=True)
    seq: Mapped[int] = mapped_column(BigInt, nullable=False)
    # service=服务发给主会话的消息 / hermes=主会话输出 / tool=工具调用与委派 / error=错误 / user=客户消息
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="hermes")
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
