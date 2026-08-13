"""模型基类与通用类型。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, Integer, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# JSONB（PG）→ JSON（SQLite 测试/开发）兼容
JSONType = JSON().with_variant(JSONB(), "postgresql")

# BIGINT（PG）→ INTEGER（SQLite，保证主键自增）
BigInt = BigInteger().with_variant(Integer(), "sqlite")


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
