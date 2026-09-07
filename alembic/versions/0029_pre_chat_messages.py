"""pre_chat 消息持久化：任务前对话刷新可恢复（issue #20）。

Revision ID: 0029
Revises: 0028
Create Date: 2026-09-07
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None

_GUARD = (
    "(CASE WHEN current_setting('app.enterprise_id', true) ~ '^[0-9]+$' "
    "THEN current_setting('app.enterprise_id', true)::bigint END)"
)


def upgrade() -> None:
    op.create_table(
        "pre_chat_message",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("project_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False, server_default="user"),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("client_message_id", sa.String(length=100), nullable=True, index=True),
        sa.Column("reply_to_seq", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE pre_chat_message FORCE ROW LEVEL SECURITY")
        op.execute(
            "CREATE POLICY tenant_isolation_pre_chat_message ON pre_chat_message "
            f"USING (enterprise_id = {_GUARD}) WITH CHECK (enterprise_id = {_GUARD})"
        )


def downgrade() -> None:
    op.drop_table("pre_chat_message")
