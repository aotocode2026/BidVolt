"""图片描述缓存（入库后台任务）：file_image 登记 + image_description 按 sha256 全局缓存。

Revision ID: 0025
Revises: 0024
Create Date: 2026-08-30
"""
import sqlalchemy as sa

from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "file_image",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("file_id", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_file_image_file", "file_image", ["file_id"])
    op.create_index("ix_file_image_hash", "file_image", ["sha256"])
    op.create_table(
        "image_description",
        sa.Column("sha256", sa.String(64), primary_key=True),
        sa.Column("description", sa.JSON(), nullable=False),
        sa.Column("model", sa.String(50), nullable=True),
        sa.Column("described_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("image_description")
    op.drop_index("ix_file_image_hash", table_name="file_image")
    op.drop_index("ix_file_image_file", table_name="file_image")
    op.drop_table("file_image")
