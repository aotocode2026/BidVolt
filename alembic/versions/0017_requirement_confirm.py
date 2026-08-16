"""requirement 增加用户确认/修正闭环字段（Issue #6 P0）

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-16
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return  # SQLite 测试库走 create_all，无需迁移
    op.add_column(
        "requirement",
        sa.Column("confirm_status", sa.String(20), nullable=False, server_default="unconfirmed"),
    )
    op.add_column("requirement", sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.drop_column("requirement", "confirmed_at")
    op.drop_column("requirement", "confirm_status")
