"""招标要求域：requirement / requirement_revision + RLS（PG）

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-11
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
bigint_pk = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "requirement",
        sa.Column("id", bigint_pk, primary_key=True),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("project_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("req_type", sa.String(length=50), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("structured", json_type, nullable=True),
        sa.Column("source_file_id", sa.BigInteger(), nullable=True),
        sa.Column("coordinates", json_type, nullable=True),
        sa.Column("confidence", sa.Numeric(4, 3), nullable=True),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("supersedes", sa.BigInteger(), nullable=True),
        sa.Column("current", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_table(
        "requirement_revision",
        sa.Column("id", bigint_pk, primary_key=True),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("requirement_id", sa.BigInteger(), sa.ForeignKey("requirement.id"), nullable=False),
        sa.Column("revision_no", sa.BigInteger(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("structured", json_type, nullable=True),
        sa.Column("coordinates", json_type, nullable=True),
        sa.Column("supersedes", sa.BigInteger(), nullable=True),
        sa.Column("source_file_id", sa.BigInteger(), nullable=True),
        sa.Column("source_task_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("requirement_id", "revision_no", name="uq_req_rev"),
    )

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for table in ("requirement", "requirement_revision"):
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
        for table in ("requirement", "requirement_revision"):
            op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON {table}")
            op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.drop_table("requirement_revision")
    op.drop_table("requirement")
