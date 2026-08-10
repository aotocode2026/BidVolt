"""M2：企业资料域 / 项目材料域 / doc_block

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-10
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
bigint_pk = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "enterprise_asset_category",
        sa.Column("id", bigint_pk, primary_key=True),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("parent_id", sa.BigInteger(), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_table(
        "enterprise_asset",
        sa.Column("id", bigint_pk, primary_key=True),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("category_id", sa.BigInteger(), sa.ForeignKey("enterprise_asset_category.id"), nullable=True),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("asset_type", sa.String(length=50), nullable=False, server_default="other"),
        sa.Column("source_file_id", sa.BigInteger(), sa.ForeignKey("file_object.id"), nullable=True),
        sa.Column("status", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_table(
        "enterprise_asset_revision",
        sa.Column("id", bigint_pk, primary_key=True),
        sa.Column("asset_id", sa.BigInteger(), sa.ForeignKey("enterprise_asset.id"), nullable=False),
        sa.Column("revision_no", sa.BigInteger(), nullable=False),
        sa.Column("file_id", sa.BigInteger(), sa.ForeignKey("file_object.id"), nullable=False),
        sa.Column("source_location", json_type, nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("asset_id", "revision_no", name="uq_ear_rev"),
    )
    op.create_table(
        "enterprise_fact",
        sa.Column("id", bigint_pk, primary_key=True),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("asset_id", sa.BigInteger(), sa.ForeignKey("enterprise_asset.id"), nullable=False),
        sa.Column("fact_key", sa.String(length=100), nullable=False),
        sa.Column("fact_value", json_type, nullable=False),
        sa.Column("confidence", sa.Numeric(4, 3), nullable=True),
        sa.Column("status", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_table(
        "enterprise_fact_evidence",
        sa.Column("id", bigint_pk, primary_key=True),
        sa.Column("fact_id", sa.BigInteger(), sa.ForeignKey("enterprise_fact.id"), nullable=False),
        sa.Column(
            "asset_revision_id",
            sa.BigInteger(),
            sa.ForeignKey("enterprise_asset_revision.id"),
            nullable=False,
        ),
        sa.Column("source_range", json_type, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_table(
        "enterprise_ingestion_task",
        sa.Column("id", bigint_pk, primary_key=True),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False),
        sa.Column("task_id", sa.BigInteger(), sa.ForeignKey("task.id"), nullable=False),
        sa.Column("asset_ids", json_type, nullable=False),
        sa.Column("status", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_table(
        "project_material",
        sa.Column("id", bigint_pk, primary_key=True),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("project_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("file_id", sa.BigInteger(), sa.ForeignKey("file_object.id"), nullable=False),
        sa.Column("status", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_table(
        "project_material_revision",
        sa.Column("id", bigint_pk, primary_key=True),
        sa.Column("material_id", sa.BigInteger(), sa.ForeignKey("project_material.id"), nullable=False),
        sa.Column("revision_no", sa.BigInteger(), nullable=False),
        sa.Column("supersedes", sa.BigInteger(), nullable=True),
        sa.Column("conflict", sa.Text(), nullable=True),
        sa.Column("source_file_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("material_id", "revision_no", name="uq_pmr_rev"),
    )
    op.create_table(
        "project_event",
        sa.Column("id", bigint_pk, primary_key=True),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("event_data", json_type, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_table(
        "material_match_result",
        sa.Column("id", bigint_pk, primary_key=True),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("requirement_id", sa.BigInteger(), nullable=True),
        sa.Column("asset_id", sa.BigInteger(), nullable=True),
        sa.Column("matched", sa.SmallInteger(), nullable=False),
        sa.Column("gap_desc", sa.Text(), nullable=True),
        sa.Column("affected_score_item", sa.String(length=100), nullable=True),
        sa.Column("impact_score", sa.Numeric(6, 2), nullable=True),
        sa.Column("suggestion", sa.Text(), nullable=True),
        sa.Column("source_task_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_table(
        "project_snapshot",
        sa.Column("id", bigint_pk, primary_key=True),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("snapshot_type", sa.String(length=30), nullable=False),
        sa.Column("input_refs", json_type, nullable=False),
        sa.Column("external_samples", json_type, nullable=True),
        sa.Column("rules_version", json_type, nullable=True),
        sa.Column("manifest", json_type, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_table(
        "doc_block",
        sa.Column("id", bigint_pk, primary_key=True),
        sa.Column("file_id", sa.BigInteger(), sa.ForeignKey("file_object.id"), nullable=False, index=True),
        sa.Column("block_type", sa.String(length=20), nullable=False),
        sa.Column("page_no", sa.Integer(), nullable=True),
        sa.Column("block_index", sa.Integer(), nullable=False),
        sa.Column("parent_block_id", sa.BigInteger(), nullable=True),
        sa.Column("text_content", sa.Text(), nullable=True),
        sa.Column("extra", json_type, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )


def downgrade() -> None:
    for table in (
        "doc_block",
        "project_snapshot",
        "material_match_result",
        "project_event",
        "project_material_revision",
        "project_material",
        "enterprise_ingestion_task",
        "enterprise_fact_evidence",
        "enterprise_fact",
        "enterprise_asset_revision",
        "enterprise_asset",
        "enterprise_asset_category",
    ):
        op.drop_table(table)
