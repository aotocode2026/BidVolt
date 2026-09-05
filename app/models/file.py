"""文件对象模型（4.2）。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, SmallInteger, String, Text, func
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
    # 项目文件角色（Issue #6 P1）：招标公告 / 招标文件 / 补充材料 / 已完成标书 / 其他
    document_role: Mapped[str | None] = mapped_column(String(50))
    status: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    parse_status: Mapped[dict | None] = mapped_column(JSONType)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 解包来源：本文件由哪个压缩包展开而来（压缩包自动解包入库时写入）；
    # archive_path=包内相对路径（如 03_业绩/合同A/合同扫描件.pdf），保留原始层次信息
    source_archive_id: Mapped[int | None] = mapped_column(BigInt)
    archive_path: Mapped[str | None] = mapped_column(String(500))


class ArchiveJob(Base, TimestampMixin):
    __tablename__ = "archive_job"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(BigInt, nullable=False)
    archive_file_id: Mapped[int] = mapped_column(
        BigInt, ForeignKey("file_object.id"), nullable=False
    )
    status: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    result: Mapped[dict | None] = mapped_column(JSONType)


class UploadBatch(Base, TimestampMixin):
    """一次上传/导入的批次：刷新后可查询逐文件结果。"""

    __tablename__ = "upload_batch"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(BigInt, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigInt, nullable=False)
    project_id: Mapped[int | None] = mapped_column(BigInt)
    target: Mapped[str] = mapped_column(String(20), nullable=False)


class UploadBatchItem(Base, TimestampMixin):
    """批次内的单文件处理结果。"""

    __tablename__ = "upload_batch_item"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    batch_id: Mapped[int] = mapped_column(
        BigInt, ForeignKey("upload_batch.id"), nullable=False, index=True
    )
    enterprise_id: Mapped[int] = mapped_column(BigInt, nullable=False, index=True)
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    file_id: Mapped[int | None] = mapped_column(BigInt)
    asset_id: Mapped[int | None] = mapped_column(BigInt)
    status: Mapped[str] = mapped_column(String(30), nullable=False)  # accepted/duplicate/error/expanded
    message: Mapped[str | None] = mapped_column(Text)
    document_role: Mapped[str | None] = mapped_column(String(50))


class FileImage(Base, TimestampMixin):
    """文件内嵌图片登记（docx/pdf 解析层提取）：file_id + sha256 + 页序。
    描述缓存在 image_description（按 sha256 全局共享，跨项目/企业复用）。"""

    __tablename__ = "file_image"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    file_id: Mapped[int] = mapped_column(BigInt, nullable=False, index=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    page: Mapped[int | None] = mapped_column(Integer)


class ImageDescription(Base, TimestampMixin):
    """图片结构化描述缓存（sha256 全局唯一）：入库后台任务产出，
    每张图只调一次视觉模型；结构化字段（编号/日期/金额/主体/印章/摘要）。"""

    __tablename__ = "image_description"

    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    description: Mapped[dict] = mapped_column(JSONType, nullable=False)
    model: Mapped[str | None] = mapped_column(String(50))
    described_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
