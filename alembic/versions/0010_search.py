"""搜索域：search_source / citation + RLS（PG）

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-11
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
bigint_pk = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "search_source",
        sa.Column("id", bigint_pk, primary_key=True),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("project_id", sa.BigInteger(), nullable=True),
        sa.Column("query", sa.String(length=500), nullable=True),
        sa.Column("url", sa.String(length=1000), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=True),
        sa.Column("snippet", sa.Text(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("trust_level", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("domain", sa.String(length=200), nullable=True),
        sa.Column("extra", json_type, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_table(
        "citation",
        sa.Column("id", bigint_pk, primary_key=True),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("deliverable_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("version_no", sa.BigInteger(), nullable=False),
        sa.Column("node_id", sa.String(length=100), nullable=True),
        sa.Column("source_id", sa.BigInteger(), sa.ForeignKey("search_source.id"), nullable=False),
        sa.Column("quote_text", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for table in ("search_source", "citation"):
            op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
            op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
            op.execute(
                f"CREATE POLICY tenant_isolation_{table} ON {table} "
                "USING (current_setting('app.enterprise_id', true) <> '' "
                "       AND enterprise_id = current_setting('app.enterprise_id', true)::bigint) "
                "WITH CHECK (current_setting('app.enterprise_id', true) <> '' "
                "            AND enterprise_id = current_setting('app.enterprise_id', true)::bigint)"
            )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for table in ("search_source", "citation"):
            op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON {table}")
            op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.drop_table("citation")
    op.drop_table("search_source")
