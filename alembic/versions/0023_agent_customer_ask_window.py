"""提问关问答窗口：agent_customer_ask 增加 window_minutes 与 timeout_notified。

超时信号设计（纯信号版）：服务端不替主会话决定任何答案，只在其超过问答窗口
仍未收到客户回答时注入一条「已超时，由你自行决定」提示；幂等注入（timeout_notified）。

Revision ID: 0023
Revises: 0022
Create Date: 2026-08-30
"""
import sqlalchemy as sa

from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("agent_customer_ask") as batch:
        batch.add_column(sa.Column("window_minutes", sa.SmallInteger(), nullable=False, server_default="20"))
        batch.add_column(sa.Column("timeout_notified", sa.SmallInteger(), nullable=False, server_default="0"))


def downgrade() -> None:
    with op.batch_alter_table("agent_customer_ask") as batch:
        batch.drop_column("timeout_notified")
        batch.drop_column("window_minutes")
