"""文件对象模型（4.2）。"""

from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, SmallInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigInt, JSONType, TimestampMixin


class FileObject(Base, TimestampMixin):
    __tablename__ = "file_object"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(BigInt, nullable=False, index=True)
    project_id: Mapped[int | None] = mapped_column(BigInt)
    owner_type: Mapped[int] = mapped_column(SmallInteger, nullable=False)  # 1 企业资料 2 项目材料
    bucket: Mapped[str] = mapped_column(String(100), nullable=False)
    object_key: Mapped[str] = mapped_column(String(500), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    original_name: Mapped[str] = mapped_column(String(500), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInt, nullable=False, default=0)
    mime_type: Mapped[str | None] = mapped_column(String(100))
    ext: Mapped[str | None] = mapped_column(String(20))
    category: Mapped[str | None] = mapped_column(String(50))
    status: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    parse_status: Mapped[dict | None] = mapped_column(JSONType)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class ArchiveJob(Base, TimestampMixin):
    __tablename__ = "archive_job"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(BigInt, nullable=False)
    archive_file_id: Mapped[int] = mapped_column(
        BigInt, ForeignKey("file_object.id"), nullable=False
    )
    status: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    result: Mapped[dict | None] = mapped_column(JSONType)
