"""requirement/requirement_revision 增加 req_key（Issue #2 #11：同类型多条要求共存）

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-14
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("requirement", sa.Column("req_key", sa.String(length=100), nullable=True))
    op.add_column("requirement_revision", sa.Column("req_key", sa.String(length=100), nullable=True))


def downgrade() -> None:
    op.drop_column("requirement_revision", "req_key")
    op.drop_column("requirement", "req_key")
