"""Agent 主会话事件/成文产物模型（新方案）：会话控制台事件 + 成文工具链产物。"""

from __future__ import annotations

from sqlalchemy import LargeBinary, String, Text
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


class AgentArtifact(Base, TimestampMixin):
    """成文工具链产物：主会话经 slice/fill/append/seal/package 产出的文件与 zip。"""

    __tablename__ = "agent_artifact"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(BigInt, nullable=False, index=True)
    project_id: Mapped[int] = mapped_column(BigInt, nullable=False, index=True)
    task_id: Mapped[int] = mapped_column(BigInt, nullable=False, index=True)
    # item_docx=条目文件 / xlsx=报价单 / zip=响应文件包 / other
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="item_docx")
    # 包内路径名（如 "价格文件/（一）响应函及报价汇总表.docx"）
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    mime: Mapped[str] = mapped_column(String(120), nullable=False, default="application/octet-stream")
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, default=b"")
