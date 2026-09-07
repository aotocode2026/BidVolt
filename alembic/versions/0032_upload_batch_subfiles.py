"""上传批次 ZIP 子文件关联与逐文件解析状态（issue #23）。

Revision ID: 0032
Revises: 0031
Create Date: 2026-09-07
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "upload_batch_item", sa.Column("source_archive_file_id", sa.BigInteger(), nullable=True)
    )
    op.add_column(
        "upload_batch_item", sa.Column("archive_path", sa.String(length=500), nullable=True)
    )
    op.add_column(
        "upload_batch_item", sa.Column("parse_status", sa.String(length=20), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("upload_batch_item", "parse_status")
    op.drop_column("upload_batch_item", "archive_path")
    op.drop_column("upload_batch_item", "source_archive_file_id")
