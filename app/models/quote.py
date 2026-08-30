"""报价域模型（4.7.3）。"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Numeric, SmallInteger, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigInt, JSONType, TimestampMixin


class HistoryPriceSnapshot(Base, TimestampMixin):
    """历史中标价快照库（两层）：
    - enterprise_id=0：平台级公共行情库（全平台租户可见，用户上传共建）；
    - enterprise_id=本企业：企业私有历史价（仅本企业可见）。
    不可变快照，不回写外部库。"""

    __tablename__ = "history_price_snapshot"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(BigInt, nullable=False, index=True)
    provider_id: Mapped[str] = mapped_column(String(50), nullable=False)
    material_name: Mapped[str] = mapped_column(String(500), nullable=False)
    material_code: Mapped[str | None] = mapped_column(String(200))
    spec: Mapped[str | None] = mapped_column(String(500))
    region: Mapped[str | None] = mapped_column(String(300))
    win_price: Mapped[float] = mapped_column(Numeric(18, 2), nullable=False)
    win_date: Mapped[date] = mapped_column(Date, nullable=False)
    source_hash: Mapped[str | None] = mapped_column(String(64))
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # —— 公共/私有行情库扩展（限价↔中标价配对，源自公开采集 xlsx 共建）——
    publisher: Mapped[str | None] = mapped_column(String(500))  # 发布单位
    category: Mapped[str | None] = mapped_column(String(500))  # 分标/品类
    package_name: Mapped[str | None] = mapped_column(String(500))  # 包/项目名称
    price_mode: Mapped[str | None] = mapped_column(String(30))  # 报价方式（归一枚举）
    limit_price: Mapped[float | None] = mapped_column(Numeric(18, 4))  # 限价（万元；折扣率类为 None）
    publish_date: Mapped[date | None] = mapped_column(Date)  # 公告发布时间
    notice_id: Mapped[str | None] = mapped_column(String(300))  # 公告ID
    limit_evidence: Mapped[str | None] = mapped_column(String(500))  # 限价证据原文
    win_evidence: Mapped[str | None] = mapped_column(String(500))  # 中标价证据原文
    limit_evidence_url: Mapped[str | None] = mapped_column(String(500))
    win_evidence_url: Mapped[str | None] = mapped_column(String(500))


class QuoteCalc(Base, TimestampMixin):
    __tablename__ = "quote_calc"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(BigInt, nullable=False, index=True)
    project_id: Mapped[int] = mapped_column(BigInt, nullable=False)
    params: Mapped[dict] = mapped_column(JSONType, nullable=False)
    result: Mapped[dict] = mapped_column(JSONType, nullable=False)
    strategy_results: Mapped[dict | None] = mapped_column(JSONType)
    ai_suggest: Mapped[dict | None] = mapped_column(JSONType)
    snapshot_refs: Mapped[list | None] = mapped_column(JSONType)
    status: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)  # 1 待确认 2 已应用 3 已放弃
    applied_version_no: Mapped[int | None] = mapped_column(BigInt)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
