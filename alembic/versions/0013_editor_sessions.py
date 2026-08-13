"""在线编辑会话：editor_session + RLS（Issue #2 #41-#43）

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-13
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

bigint_pk = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
json_type = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "editor_session",
        sa.Column("id", bigint_pk, primary_key=True),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "deliverable_id",
            sa.BigInteger(),
            sa.ForeignKey("deliverable.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("base_version_no", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column("lease_token", sa.String(length=128), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("checkpoint", json_type, nullable=True),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_version_no", sa.BigInteger(), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )

    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    table = "editor_session"
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
        op.execute("DROP POLICY IF EXISTS tenant_isolation_editor_session ON editor_session")
        op.execute("ALTER TABLE editor_session DISABLE ROW LEVEL SECURITY")
    op.drop_table("editor_session")
