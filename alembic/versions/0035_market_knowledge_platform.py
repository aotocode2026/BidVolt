"""行情库改为平台共享（issue #34 口径修正）：移除企业级 RLS，所有登录用户可见。

Revision ID: 0035
Revises: 0034
Create Date: 2026-09-08
"""

from __future__ import annotations

from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None

_GUARD = (
    "(CASE WHEN current_setting('app.enterprise_id', true) ~ '^[0-9]+$' "
    "THEN current_setting('app.enterprise_id', true)::bigint END)"
)
_TABLES = (
    "market_knowledge_article",
    "market_knowledge_point",
    "market_knowledge_image",
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for table in _TABLES:
            op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON {table}")
            op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for table in _TABLES:
            op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
            op.execute(
                f"CREATE POLICY tenant_isolation_{table} ON {table} "
                f"USING (enterprise_id = {_GUARD}) WITH CHECK (enterprise_id = {_GUARD})"
            )
