"""Agent 主会话事件/成文产物模型（新方案）：会话控制台事件 + 成文工具链产物 + 客户交互。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, LargeBinary, SmallInteger, String, Text, func
from sqlalchemy import UniqueConstraint
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
    # 客户端消息幂等标识；同一 client_message_id 只产生一条 user 事件
    client_message_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    # 回复关联：hermes 事件指向其回应的 user 事件 seq
    reply_to_seq: Mapped[int | None] = mapped_column(BigInt, nullable=True)


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
    # 覆盖修改时递增；初次封存为 1。artifact_id 保持稳定，版本号用于前端识别内容变化。
    version_no: Mapped[int] = mapped_column(BigInt, nullable=False, default=1)
    # 逻辑文件身份（issue #21）：同一逻辑文件在“另存为新版本”链上保持同一 lineage
    logical_file_id: Mapped[int | None] = mapped_column(BigInt, nullable=True, index=True)
    parent_artifact_id: Mapped[int | None] = mapped_column(BigInt, nullable=True)
    logical_version_no: Mapped[int | None] = mapped_column(BigInt, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class AgentArtifactContentVersion(Base, TimestampMixin):
    """产物覆盖前内容归档（issue #21）：覆盖保存前把当前版本内容落历史，覆盖后可回读。"""

    __tablename__ = "agent_artifact_content_version"
    __table_args__ = (
        UniqueConstraint("artifact_id", "version_no", name="uq_artifact_content_version"),
    )

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(BigInt, nullable=False, index=True)
    project_id: Mapped[int] = mapped_column(BigInt, nullable=False, index=True)
    artifact_id: Mapped[int] = mapped_column(BigInt, nullable=False, index=True)
    version_no: Mapped[int] = mapped_column(BigInt, nullable=False)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, default=b"")


class AgentCustomerAsk(Base, TimestampMixin):
    """主会话↔客户交互：ask_customer 工具的提问（question）与提交前动作清单（action）。"""

    __tablename__ = "agent_customer_ask"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(BigInt, nullable=False, index=True)
    project_id: Mapped[int] = mapped_column(BigInt, nullable=False, index=True)
    task_id: Mapped[int] = mapped_column(BigInt, nullable=False, index=True)
    # question=向客户提问（客户可在页面回答）/ action=提交前客户动作清单（只读呈现）
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="question")
    # question: [{q, need, checked}]；action: [str]
    items: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # 0=待回答 1=已回答（action 恒为 1=已呈现）
    answered: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    # 问答窗口（分钟）：超时后服务端注入「由你自行决定」信号；问卡仍可补答
    window_minutes: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=20)
    # 超时信号是否已注入（幂等标记）
    timeout_notified: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    # 客户回答（question 用）：[str]（与 items 逐条对应；客户未逐条时整体一条）
    answer: Mapped[list | None] = mapped_column(JSON, nullable=True)


class PreChatMessage(Base, TimestampMixin):
    """任务前对话（pre-chat）消息记录：项目尚无主会话任务时的对话，刷新后可恢复。"""

    __tablename__ = "pre_chat_message"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(BigInt, nullable=False, index=True)
    project_id: Mapped[int] = mapped_column(BigInt, nullable=False, index=True)
    seq: Mapped[int] = mapped_column(BigInt, nullable=False)
    # user=客户消息 / hermes=主会话回复 / error=处理失败
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="user")
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Hermes 会话 id：首轮建立后写入，用于后续 --resume 与排查关联
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 客户端幂等标识；同一 client_message_id 只产生一条 user 记录
    client_message_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    # 回复关联：hermes/error 事件指向其回应的 user 事件 seq
    reply_to_seq: Mapped[int | None] = mapped_column(BigInt, nullable=True)
