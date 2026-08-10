"""M4：成果/版本链/ai-edit diff

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-10
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
bigint_pk = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "deliverable_content",
        sa.Column("id", bigint_pk, primary_key=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("content_json", json_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_table(
        "deliverable",
        sa.Column("id", bigint_pk, primary_key=True),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("deliverable_type", sa.SmallInteger(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("current_version_no", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("stat", json_type, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("project_id", "deliverable_type", name="uq_deliverable_type"),
    )
    op.create_table(
        "deliverable_version",
        sa.Column("id", bigint_pk, primary_key=True),
        sa.Column("deliverable_id", sa.BigInteger(), sa.ForeignKey("deliverable.id"), nullable=False, index=True),
        sa.Column("version_no", sa.BigInteger(), nullable=False),
        sa.Column("version_type", sa.SmallInteger(), nullable=False),
        sa.Column("milestone", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("content_id", sa.BigInteger(), sa.ForeignKey("deliverable_content.id"), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("source_task_id", sa.BigInteger(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("deliverable_id", "version_no", name="uq_dv_no"),
    )
    op.create_table(
        "ai_edit_diff",
        sa.Column("id", bigint_pk, primary_key=True),
        sa.Column("deliverable_id", sa.BigInteger(), sa.ForeignKey("deliverable.id"), nullable=False),
        sa.Column("base_version_no", sa.BigInteger(), nullable=False),
        sa.Column("diff", json_type, nullable=False),
        sa.Column("applied", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("ai_edit_diff")
    op.drop_table("deliverable_version")
    op.drop_table("deliverable")
    op.drop_table("deliverable_content")
