"""add agent_artifact.version_no and updated_at

Revision ID: 0027
Revises: 0026
Create Date: 2026-09-05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_artifact",
        sa.Column("version_no", sa.BigInteger(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    op.drop_column("agent_artifact", "version_no")
