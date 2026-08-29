"""项目表加 pre_chat_session_id（任务前对话会话，开跑时注入任务 prompt）。

Revision ID: 0021
Revises: 0020
Create Date: 2026-08-30
"""
import sqlalchemy as sa

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("project", sa.Column("pre_chat_session_id", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("project", "pre_chat_session_id")
