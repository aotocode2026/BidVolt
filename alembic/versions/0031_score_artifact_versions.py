"""评分/报价绑定正式 artifact 版本（issue #22）。

- score_record 增加 artifact_versions（评分时冻结的 artifact_id -> version_no）；
- quote_calc 增加 artifact_id / artifact_version_no。

Revision ID: 0031
Revises: 0030
Create Date: 2026-09-07
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None

json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.add_column("score_record", sa.Column("artifact_versions", json_type, nullable=True))
    op.add_column("quote_calc", sa.Column("artifact_id", sa.BigInteger(), nullable=True))
    op.add_column("quote_calc", sa.Column("artifact_version_no", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("quote_calc", "artifact_version_no")
    op.drop_column("quote_calc", "artifact_id")
    op.drop_column("score_record", "artifact_versions")
