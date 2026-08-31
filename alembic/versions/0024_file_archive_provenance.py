"""解包溯源：file_object 增加 source_archive_id（来源压缩包）与 archive_path（包内相对路径）。

压缩包自动解包入库时写入，材料列表据此展示「来源压缩包名 › 包内路径层次」。

Revision ID: 0024
Revises: 0023
Create Date: 2026-08-30
"""
import sqlalchemy as sa

from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("file_object") as batch:
        batch.add_column(sa.Column("source_archive_id", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("archive_path", sa.String(500), nullable=True))
    op.create_index("ix_file_object_source_archive", "file_object", ["source_archive_id"])


def downgrade() -> None:
    op.drop_index("ix_file_object_source_archive", table_name="file_object")
    with op.batch_alter_table("file_object") as batch:
        batch.drop_column("archive_path")
        batch.drop_column("source_archive_id")
