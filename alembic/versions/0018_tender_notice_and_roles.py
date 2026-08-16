"""Issue #6：file_object.document_role、project.buyer、tender_notice 表

Revision ID: 0018
Revises: 0017
Create Date: 2026-08-16
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return  # SQLite 测试库走 create_all
    op.add_column("file_object", sa.Column("document_role", sa.String(50), nullable=True))
    op.add_column("project", sa.Column("buyer", sa.String(300), nullable=True))
    op.create_table(
        "tender_notice",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("project_id", sa.BigInteger(), sa.ForeignKey("project.id"), nullable=False, index=True),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("title", sa.String(500), nullable=True),
        sa.Column("status", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column("file_id", sa.BigInteger(), nullable=True),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.drop_table("tender_notice")
    op.drop_column("project", "buyer")
    op.drop_column("file_object", "document_role")
