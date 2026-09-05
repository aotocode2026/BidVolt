"""add upload batches, chat idempotency and version bindings

Revision ID: 0028
Revises: 0027
Create Date: 2026-09-05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None

json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "upload_batch",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=True),
        sa.Column("target", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_table(
        "upload_batch_item",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("batch_id", sa.BigInteger(), sa.ForeignKey("upload_batch.id"), nullable=False, index=True),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("filename", sa.String(length=500), nullable=False),
        sa.Column("file_id", sa.BigInteger(), nullable=True),
        sa.Column("asset_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("document_role", sa.String(length=50), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.add_column("agent_session_event", sa.Column("client_message_id", sa.String(length=100), nullable=True))
    op.create_index("ix_agent_session_event_client_message_id", "agent_session_event", ["client_message_id"])
    op.add_column("agent_session_event", sa.Column("reply_to_seq", sa.BigInteger(), nullable=True))
    op.add_column("score_record", sa.Column("deliverable_versions", json_type, nullable=True))
    op.add_column("quote_calc", sa.Column("deliverable_id", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("quote_calc", "deliverable_id")
    op.drop_column("score_record", "deliverable_versions")
    op.drop_column("agent_session_event", "reply_to_seq")
    op.drop_index("ix_agent_session_event_client_message_id", table_name="agent_session_event")
    op.drop_column("agent_session_event", "client_message_id")
    op.drop_table("upload_batch_item")
    op.drop_table("upload_batch")
