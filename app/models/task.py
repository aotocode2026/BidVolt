"""任务模型（4.4.3）。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, SmallInteger, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigInt, JSONType, TimestampMixin


class Task(Base, TimestampMixin):
    __tablename__ = "task"
    __table_args__ = (UniqueConstraint("idempotency_key", "enterprise_id", name="uq_task_idem"),)

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(BigInt, nullable=False, index=True)
    project_id: Mapped[int] = mapped_column(BigInt, nullable=False)
    task_type: Mapped[str] = mapped_column(String(50), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=5)
    status: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    payload: Mapped[dict] = mapped_column(JSONType, nullable=False)
    result: Mapped[dict | None] = mapped_column(JSONType)
    progress: Mapped[dict | None] = mapped_column(JSONType)
    retry_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    generation: Mapped[int] = mapped_column(BigInt, nullable=False, default=1)
    error: Mapped[dict | None] = mapped_column(JSONType)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 任务租约（Issue #3）：worker 领取后持租约 + 心跳续期；
    # 进程被强杀/卡死后租约过期，其他 worker 可回收，避免任务永久卡在 RUNNING。
    lease_owner: Mapped[str | None] = mapped_column(String(120))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
