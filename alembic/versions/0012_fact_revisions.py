"""企业事实修订表 + review_item.suggestion_override（Issue #2 #24/#29）

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-13
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

bigint_pk = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "enterprise_fact_revision",
        sa.Column("id", bigint_pk, primary_key=True),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column(
            "fact_id",
            sa.BigInteger(),
            sa.ForeignKey("enterprise_fact.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("revision_no", sa.BigInteger(), nullable=False),
        sa.Column("fact_value", sa.JSON(), nullable=False),
        sa.Column("confidence", sa.Numeric(4, 3), nullable=True),
        sa.Column("status", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("fact_id", "revision_no", name="uq_efr_rev"),
    )
    op.add_column("review_item", sa.Column("suggestion_override", sa.Text(), nullable=True))

    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    table = "enterprise_fact_revision"
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
        op.execute("DROP POLICY IF EXISTS tenant_isolation_enterprise_fact_revision ON enterprise_fact_revision")
        op.execute("ALTER TABLE enterprise_fact_revision DISABLE ROW LEVEL SECURITY")
    op.drop_column("review_item", "suggestion_override")
    op.drop_table("enterprise_fact_revision")
