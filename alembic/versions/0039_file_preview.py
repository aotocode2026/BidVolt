"""浏览器内 Office 预览缓存（issue #63）。

新增 file_preview：docx→PDF 转换结果按内容寻址缓存（source_key = 文件 sha256 或 v<版本号>）。

Revision ID: 0039
Revises: 0038
Create Date: 2026-09-12
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None

_GUARD = (
    "(CASE WHEN current_setting('app.enterprise_id', true) ~ '^[0-9]+$' "
    "THEN current_setting('app.enterprise_id', true)::bigint END)"
)


def upgrade() -> None:
    op.create_table(
        "file_preview",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("source_type", sa.String(length=20), nullable=False),
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column("source_key", sa.String(length=120), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False, server_default="pdf"),
        sa.Column(
            "mime",
            sa.String(length=100),
            nullable=False,
            server_default="application/pdf",
        ),
        sa.Column("content", sa.LargeBinary(), nullable=False, server_default=sa.text("''")),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("byte_size", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "enterprise_id",
            "source_type",
            "source_key",
            "kind",
            name="uq_file_preview_source",
        ),
    )
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE file_preview ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE file_preview FORCE ROW LEVEL SECURITY")
        op.execute(
            "CREATE POLICY tenant_isolation_file_preview ON file_preview "
            f"USING (enterprise_id = {_GUARD}) WITH CHECK (enterprise_id = {_GUARD})"
        )


def downgrade() -> None:
    op.drop_table("file_preview")
