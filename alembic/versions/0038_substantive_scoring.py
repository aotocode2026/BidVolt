"""真实评分闭环（discussion #53）：评分类型与逐项结论/证据字段。

Revision ID: 0038
Revises: 0037
Create Date: 2026-09-10
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None

json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.add_column(
        "score_record",
        sa.Column("evaluation_type", sa.String(length=20), nullable=False, server_default="builtin"),
    )
    op.add_column("review_item", sa.Column("verdict", sa.String(length=30), nullable=True))
    op.add_column("review_item", sa.Column("deduction_reason", sa.Text(), nullable=True))
    op.add_column("review_item", sa.Column("rule_source", json_type, nullable=True))
    op.add_column("review_item", sa.Column("response_source", json_type, nullable=True))
    op.add_column("review_item", sa.Column("missing_materials", json_type, nullable=True))


def downgrade() -> None:
    op.drop_column("review_item", "missing_materials")
    op.drop_column("review_item", "response_source")
    op.drop_column("review_item", "rule_source")
    op.drop_column("review_item", "deduction_reason")
    op.drop_column("review_item", "verdict")
    op.drop_column("score_record", "evaluation_type")
