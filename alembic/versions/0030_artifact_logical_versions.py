"""正式文件逻辑版本链与覆盖历史（issue #21）。

- agent_artifact 增加 logical_file_id / parent_artifact_id / logical_version_no；
- 新增 agent_artifact_content_version：覆盖保存前归档当前版本内容，覆盖后可回读。

Revision ID: 0030
Revises: 0029
Create Date: 2026-09-07
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None

_GUARD = (
    "(CASE WHEN current_setting('app.enterprise_id', true) ~ '^[0-9]+$' "
    "THEN current_setting('app.enterprise_id', true)::bigint END)"
)


def upgrade() -> None:
    op.add_column("agent_artifact", sa.Column("logical_file_id", sa.BigInteger(), nullable=True))
    op.create_index(
        "ix_agent_artifact_logical_file_id", "agent_artifact", ["logical_file_id"]
    )
    op.add_column("agent_artifact", sa.Column("parent_artifact_id", sa.BigInteger(), nullable=True))
    op.add_column("agent_artifact", sa.Column("logical_version_no", sa.BigInteger(), nullable=True))

    op.create_table(
        "agent_artifact_content_version",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("project_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("artifact_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("version_no", sa.BigInteger(), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False, server_default=sa.text("''")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("artifact_id", "version_no", name="uq_artifact_content_version"),
    )
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE agent_artifact_content_version FORCE ROW LEVEL SECURITY")
        op.execute(
            "CREATE POLICY tenant_isolation_agent_artifact_content_version "
            "ON agent_artifact_content_version "
            f"USING (enterprise_id = {_GUARD}) WITH CHECK (enterprise_id = {_GUARD})"
        )


def downgrade() -> None:
    op.drop_table("agent_artifact_content_version")
    op.drop_column("agent_artifact", "logical_version_no")
    op.drop_column("agent_artifact", "parent_artifact_id")
    op.drop_index("ix_agent_artifact_logical_file_id", table_name="agent_artifact")
    op.drop_column("agent_artifact", "logical_file_id")
