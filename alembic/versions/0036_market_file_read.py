"""行情库文件（owner_type=3）跨企业可读：调整 file_object RLS（issue #51）。

Revision ID: 0036
Revises: 0035
Create Date: 2026-09-10
"""

from __future__ import annotations

from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None

_GUARD = (
    "(CASE WHEN current_setting('app.enterprise_id', true) ~ '^[0-9]+$' "
    "THEN current_setting('app.enterprise_id', true)::bigint END)"
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("DROP POLICY IF EXISTS tenant_isolation_file_object ON file_object")
    op.execute(
        "CREATE POLICY tenant_isolation_file_object ON file_object "
        f"USING ((enterprise_id = {_GUARD}) OR (owner_type = 3)) "
        f"WITH CHECK (enterprise_id = {_GUARD})"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("DROP POLICY IF EXISTS tenant_isolation_file_object ON file_object")
    op.execute(
        "CREATE POLICY tenant_isolation_file_object ON file_object "
        f"USING (enterprise_id = {_GUARD}) WITH CHECK (enterprise_id = {_GUARD})"
    )
