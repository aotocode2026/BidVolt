"""终检与导出模型（4.10）。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, SmallInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigInt, JSONType, TimestampMixin


class FinalCheck(Base, TimestampMixin):
    __tablename__ = "final_check"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(BigInt, nullable=False)
    project_id: Mapped[int] = mapped_column(BigInt, nullable=False)
    status: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=2)  # 2 完成
    passed: Mapped[bool | None] = mapped_column(Boolean)
    result: Mapped[dict | None] = mapped_column(JSONType)


class ExportJob(Base, TimestampMixin):
    __tablename__ = "export_job"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(BigInt, nullable=False)
    project_id: Mapped[int] = mapped_column(BigInt, nullable=False)
    status: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)  # 1 生成中 2 完成 3 失败
    formats: Mapped[list] = mapped_column(JSONType, nullable=False)
    options: Mapped[dict | None] = mapped_column(JSONType)
    files: Mapped[list | None] = mapped_column(JSONType)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
