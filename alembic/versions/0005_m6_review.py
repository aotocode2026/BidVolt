"""M6：评审域（review_provider / review_run / score_record / review_item / review_material_link）

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-11
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
bigint_pk = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "review_provider",
        sa.Column("id", bigint_pk, primary_key=True),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False),
        sa.Column("provider_type", sa.String(length=20), nullable=False),
        sa.Column("provider_code", sa.String(length=50), nullable=False, unique=True),
        sa.Column("provider_version", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("category", sa.String(length=50), nullable=True),
        sa.Column("severity", sa.SmallInteger(), nullable=True),
        sa.Column("config", json_type, nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_table(
        "review_run",
        sa.Column("id", bigint_pk, primary_key=True),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("snapshot_id", sa.BigInteger(), nullable=False),
        sa.Column("provider_id", sa.BigInteger(), sa.ForeignKey("review_provider.id"), nullable=False),
        sa.Column("review_run_id", sa.String(length=100), nullable=True),
        sa.Column("provider_raw_hash", sa.String(length=64), nullable=True),
        sa.Column("status", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_table(
        "score_record",
        sa.Column("id", bigint_pk, primary_key=True),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("review_run_id", sa.BigInteger(), sa.ForeignKey("review_run.id"), nullable=True),
        sa.Column("total_score", sa.Numeric(6, 2), nullable=True),
        sa.Column("biz_score", sa.Numeric(6, 2), nullable=True),
        sa.Column("tech_score", sa.Numeric(6, 2), nullable=True),
        sa.Column("quote_score", sa.Numeric(6, 2), nullable=True),
        sa.Column("reject_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("missing_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("improvable", sa.Numeric(6, 2), nullable=True),
        sa.Column("detail", json_type, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_table(
        "review_item",
        sa.Column("id", bigint_pk, primary_key=True),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("review_run_id", sa.BigInteger(), sa.ForeignKey("review_run.id"), nullable=False),
        sa.Column("score_id", sa.BigInteger(), sa.ForeignKey("score_record.id"), nullable=True),
        sa.Column("requirement_id", sa.BigInteger(), nullable=True),
        sa.Column("criterion_id", sa.String(length=100), nullable=True),
        sa.Column("ruleset_version", sa.String(length=50), nullable=False),
        sa.Column("category", sa.String(length=50), nullable=False),
        sa.Column("problem_description", sa.Text(), nullable=False),
        sa.Column("got", sa.Numeric(6, 2), nullable=True),
        sa.Column("full", sa.Numeric(6, 2), nullable=True),
        sa.Column("improvable", sa.Numeric(6, 2), nullable=True),
        sa.Column("risk_level", sa.SmallInteger(), nullable=True),
        sa.Column("suggestion", sa.Text(), nullable=True),
        sa.Column("action_type", sa.String(length=30), nullable=True),
        sa.Column("evidence", json_type, nullable=False),
        sa.Column("missing_material_types", sa.String(length=200), nullable=True),
        sa.Column("related_deliverable_node", sa.String(length=500), nullable=True),
        sa.Column("status", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column("confidence", sa.Numeric(4, 3), nullable=True),
        sa.Column("expected_version", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_table(
        "review_material_link",
        sa.Column("id", bigint_pk, primary_key=True),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("review_item_id", sa.BigInteger(), sa.ForeignKey("review_item.id"), nullable=False),
        sa.Column("material_id", sa.BigInteger(), nullable=False),
        sa.Column("material_type", sa.String(length=20), nullable=False, server_default="project"),
        sa.Column("match_basis", sa.Text(), nullable=True),
        sa.Column("source_location", sa.String(length=500), nullable=True),
        sa.Column("confidence", sa.Numeric(4, 3), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )


def downgrade() -> None:
    for table in (
        "review_material_link",
        "review_item",
        "score_record",
        "review_run",
        "review_provider",
    ):
        op.drop_table(table)
