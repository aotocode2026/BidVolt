"""认证与租户基础模型（4.1）。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, SmallInteger, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigInt, JSONType, TimestampMixin


class Enterprise(Base, TimestampMixin):
    __tablename__ = "enterprise"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    credit_code: Mapped[str | None] = mapped_column(String(50))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=datetime.utcnow
    )


class AppUser(Base, TimestampMixin):
    __tablename__ = "app_user"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(
        BigInt, ForeignKey("enterprise.id"), nullable=False, index=True
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    permissions: Mapped[dict | None] = mapped_column(JSONType, nullable=True)  # NULL = 继承企业默认


class RefreshToken(Base, TimestampMixin):
    __tablename__ = "refresh_token"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInt, ForeignKey("app_user.id"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class EnterprisePermission(Base):
    __tablename__ = "enterprise_permission"

    enterprise_id: Mapped[int] = mapped_column(
        BigInt, ForeignKey("enterprise.id"), primary_key=True
    )
    permissions: Mapped[list] = mapped_column(JSONType, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=datetime.utcnow
    )


class ProjectEditLock(Base):
    __tablename__ = "project_edit_lock"

    project_id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    lock_token: Mapped[str] = mapped_column(String(64), nullable=False)
    holder_user_id: Mapped[int] = mapped_column(BigInt, nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
