"""招标公告 URL 导入逐附件下载：批次/公告关联与附件来源（issue #32）。

Revision ID: 0033
Revises: 0032
Create Date: 2026-09-08
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tender_notice", sa.Column("import_batch_id", sa.BigInteger(), nullable=True)
    )
    op.add_column(
        "upload_batch_item", sa.Column("notice_id", sa.BigInteger(), nullable=True)
    )
    op.create_index(
        "ix_upload_batch_item_notice_id", "upload_batch_item", ["notice_id"]
    )
    op.add_column(
        "upload_batch_item", sa.Column("source_url", sa.Text(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("upload_batch_item", "source_url")
    op.drop_index("ix_upload_batch_item_notice_id", table_name="upload_batch_item")
    op.drop_column("upload_batch_item", "notice_id")
    op.drop_column("tender_notice", "import_batch_id")
