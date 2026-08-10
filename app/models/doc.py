"""文档解析结果模型（4.3）。"""

from __future__ import annotations

from sqlalchemy import BigInteger, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigInt, JSONType, TimestampMixin


class DocBlock(Base, TimestampMixin):
    __tablename__ = "doc_block"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    file_id: Mapped[int] = mapped_column(
        BigInt, ForeignKey("file_object.id"), nullable=False, index=True
    )
    block_type: Mapped[str] = mapped_column(String(20), nullable=False)
    page_no: Mapped[int | None] = mapped_column(Integer)
    block_index: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_block_id: Mapped[int | None] = mapped_column(BigInt)
    text_content: Mapped[str | None] = mapped_column(Text)
    extra: Mapped[dict | None] = mapped_column(JSONType)
