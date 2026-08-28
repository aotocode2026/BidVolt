"""客户交互表：ask_customer 工具（提问/提交前动作清单）落库，前端问卡与回答闭环。

Revision ID: 0020
Revises: 0019
Create Date: 2026-08-29
"""
import sqlalchemy as sa

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_customer_ask",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("project_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("task_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("kind", sa.String(20), nullable=False, server_default="question"),
        sa.Column("items", sa.JSON(), nullable=False),
        sa.Column("answered", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("answer", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("DROP POLICY IF EXISTS tenant_isolation_agent_customer_ask ON agent_customer_ask")
    op.execute(
        "CREATE POLICY tenant_isolation_agent_customer_ask ON agent_customer_ask "
        "USING (current_setting('app.enterprise_id', true) <> '' "
        "       AND enterprise_id = current_setting('app.enterprise_id', true)::bigint) "
        "WITH CHECK (current_setting('app.enterprise_id', true) <> '' "
        "            AND enterprise_id = current_setting('app.enterprise_id', true)::bigint)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP POLICY IF EXISTS tenant_isolation_agent_customer_ask ON agent_customer_ask")
    op.drop_table("agent_customer_ask")
