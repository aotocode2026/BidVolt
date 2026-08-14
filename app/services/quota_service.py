"""每租户配额强制（4.11.1，P1）：存储 / 导出 / 任务并发。"""

from __future__ import annotations

from datetime import datetime, time, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.export import ExportJob
from app.models.file import FileObject
from app.models.quota import TenantQuota


class QuotaExceeded(Exception):
    def __init__(self, message: str, status_code: int = 413) -> None:
        super().__init__(message)
        self.status_code = status_code


async def get_quota(session: AsyncSession, enterprise_id: int) -> TenantQuota:
    quota = await session.get(TenantQuota, enterprise_id)
    if quota is None:
        quota = TenantQuota(enterprise_id=enterprise_id)
        session.add(quota)
        await session.flush()
    return quota


async def check_storage(session: AsyncSession, enterprise_id: int, add_bytes: int) -> int:
    quota = await get_quota(session, enterprise_id)
    used = await session.scalar(
        select(func.coalesce(func.sum(FileObject.size_bytes), 0)).where(
            FileObject.enterprise_id == enterprise_id,
            FileObject.is_deleted.is_(False),
        )
    )
    used = int(used or 0)
    if used + add_bytes > quota.storage_bytes:
        raise QuotaExceeded(
            f"存储配额不足（已用 {used} / 上限 {quota.storage_bytes} 字节）"
        )
    return used


async def check_export_daily(session: AsyncSession, enterprise_id: int) -> int:
    quota = await get_quota(session, enterprise_id)
    day_start = datetime.combine(datetime.now(timezone.utc).date(), time.min, tzinfo=timezone.utc)
    count = await session.scalar(
        select(func.count())
        .select_from(ExportJob)
        .where(
            ExportJob.enterprise_id == enterprise_id,
            ExportJob.created_at >= day_start,
        )
    )
    count = int(count or 0)
    if count >= quota.export_daily:
        raise QuotaExceeded(f"当日导出配额已用尽（{quota.export_daily} 次/天）", status_code=429)
    return count
