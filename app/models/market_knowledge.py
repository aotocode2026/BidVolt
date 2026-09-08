"""投标行情内容库模型（Issue #34）：一条资料 → 多条提炼要点（1:N）+ 图片关联。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigInt, TimestampMixin


class MarketKnowledgeArticle(Base, TimestampMixin):
    __tablename__ = "market_knowledge_article"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(BigInt, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    # 公众号文章 / 网页文章 / 文档 / 图片（上传时启发式解析，可人工调整）
    category: Mapped[str] = mapped_column(String(50), nullable=False, default="其他")
    source_type: Mapped[str] = mapped_column(String(20), nullable=False)  # url / upload
    source_url: Mapped[str | None] = mapped_column(Text)
    file_id: Mapped[int | None] = mapped_column(BigInt)  # 原件/正文 FileObject（owner_type=3）
    text_content: Mapped[str | None] = mapped_column(Text)  # 清洗后的正文文本
    extract_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    extract_error: Mapped[str | None] = mapped_column(Text)
    extract_attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rules_version: Mapped[str | None] = mapped_column(String(20))  # 提炼提示词版本
    created_by: Mapped[int | None] = mapped_column(BigInt)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class MarketKnowledgePoint(Base, TimestampMixin):
    __tablename__ = "market_knowledge_point"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    article_id: Mapped[int] = mapped_column(
        BigInt, ForeignKey("market_knowledge_article.id"), nullable=False, index=True
    )
    enterprise_id: Mapped[int] = mapped_column(BigInt, nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MarketKnowledgeImage(Base, TimestampMixin):
    __tablename__ = "market_knowledge_image"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    article_id: Mapped[int] = mapped_column(
        BigInt, ForeignKey("market_knowledge_article.id"), nullable=False, index=True
    )
    enterprise_id: Mapped[int] = mapped_column(BigInt, nullable=False, index=True)
    file_id: Mapped[int] = mapped_column(BigInt, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
