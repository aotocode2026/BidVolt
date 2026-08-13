"""task 增加租约列（Issue #3：worker 中断后任务恢复）

lease_owner / lease_expires_at / last_heartbeat_at：
领取即置租约 + 心跳续期；租约过期的 RUNNING 任务可被回收重新入队。

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-14
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("task", sa.Column("lease_owner", sa.String(length=120), nullable=True))
    op.add_column("task", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("task", sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_task_lease_expires_at", "task", ["lease_expires_at"])


def downgrade() -> None:
    op.drop_index("ix_task_lease_expires_at", table_name="task")
    op.drop_column("task", "last_heartbeat_at")
    op.drop_column("task", "lease_expires_at")
    op.drop_column("task", "lease_owner")
