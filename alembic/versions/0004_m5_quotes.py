"""M5：报价域（history_price_snapshot / quote_calc）

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-11
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
bigint_pk = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "history_price_snapshot",
        sa.Column("id", bigint_pk, primary_key=True),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("provider_id", sa.String(length=50), nullable=False),
        sa.Column("material_name", sa.String(length=200), nullable=False),
        sa.Column("material_code", sa.String(length=100), nullable=True),
        sa.Column("spec", sa.String(length=200), nullable=True),
        sa.Column("region", sa.String(length=100), nullable=True),
        sa.Column("win_price", sa.Numeric(18, 2), nullable=False),
        sa.Column("win_date", sa.Date(), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_table(
        "quote_calc",
        sa.Column("id", bigint_pk, primary_key=True),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("params", json_type, nullable=False),
        sa.Column("result", json_type, nullable=False),
        sa.Column("strategy_results", json_type, nullable=True),
        sa.Column("ai_suggest", json_type, nullable=True),
        sa.Column("snapshot_refs", json_type, nullable=True),
        sa.Column("status", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column("applied_version_no", sa.BigInteger(), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("quote_calc")
    op.drop_table("history_price_snapshot")
