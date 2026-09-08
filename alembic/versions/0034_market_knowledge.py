"""投标行情内容库：资料/提炼要点(1:N)/图片关联（issue #34）。

Revision ID: 0034
Revises: 0033
Create Date: 2026-09-08
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None

_GUARD = (
    "(CASE WHEN current_setting('app.enterprise_id', true) ~ '^[0-9]+$' "
    "THEN current_setting('app.enterprise_id', true)::bigint END)"
)


def upgrade() -> None:
    op.create_table(
        "market_knowledge_article",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("category", sa.String(length=50), nullable=False, server_default="其他"),
        sa.Column("source_type", sa.String(length=20), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("file_id", sa.BigInteger(), nullable=True),
        sa.Column("text_content", sa.Text(), nullable=True),
        sa.Column("extract_status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("extract_error", sa.Text(), nullable=True),
        sa.Column("extract_attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rules_version", sa.String(length=20), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True, index=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_table(
        "market_knowledge_point",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "article_id",
            sa.BigInteger(),
            sa.ForeignKey("market_knowledge_article.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_table(
        "market_knowledge_image",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "article_id",
            sa.BigInteger(),
            sa.ForeignKey("market_knowledge_article.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("file_id", sa.BigInteger(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for table in (
            "market_knowledge_article",
            "market_knowledge_point",
            "market_knowledge_image",
        ):
            op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
            op.execute(
                f"CREATE POLICY tenant_isolation_{table} ON {table} "
                f"USING (enterprise_id = {_GUARD}) WITH CHECK (enterprise_id = {_GUARD})"
            )


def downgrade() -> None:
    op.drop_table("market_knowledge_image")
    op.drop_table("market_knowledge_point")
    op.drop_table("market_knowledge_article")
