"""task 表豁免 RLS（PG）：worker 跨租户消费任务队列

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-11
"""
from __future__ import annotations

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("DROP POLICY IF EXISTS tenant_isolation_task ON task")
    op.execute("ALTER TABLE task DISABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("ALTER TABLE task ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE task FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation_task ON task "
        "USING (current_setting('app.enterprise_id', true) <> '' "
        "       AND enterprise_id = current_setting('app.enterprise_id', true)::bigint) "
        "WITH CHECK (current_setting('app.enterprise_id', true) <> '' "
        "            AND enterprise_id = current_setting('app.enterprise_id', true)::bigint)"
    )
