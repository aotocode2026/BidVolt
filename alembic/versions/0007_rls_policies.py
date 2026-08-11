"""PG：租户表启用 RLS + FORCE + 统一策略（P0-1，仅 PostgreSQL 执行）

说明：
- 注册路径需要在无租户上下文时写入 enterprise/app_user/refresh_token/enterprise_permission/tenant_quota/enterprise_asset_category/audit_log，
  这些表由应用层 enterprise_id 过滤 + 复合键防护，不纳入 FORCE RLS；
- 业务数据表全部启用 FORCE RLS，策略统一为 enterprise_id = current_setting('app.enterprise_id')::bigint；
- deliverable_content 为跨租户内容去重共享表，不启用 RLS。
- task 为跨租户任务队列（worker 需跨租户消费），不做 RLS；任务数据按应用层 enterprise_id 过滤（见 0009）。

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-11
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

# 仅在这些业务表启用 RLS（均在已认证会话内写入，携带 enterprise_id）
RLS_TABLES = [
    "project",
    "file_object",
    "deliverable",
    "deliverable_version",
    "review_run",
    "score_record",
    "review_item",
    "review_material_link",
    "quote_calc",
    "history_price_snapshot",
    "enterprise_asset",
    "enterprise_asset_revision",
    "enterprise_fact",
    "enterprise_fact_evidence",
    "project_material",
    "project_material_revision",
    "project_event",
    "material_match_result",
    "project_snapshot",
    "final_check",
    "export_job",
]


def upgrade() -> None:
    # 关联表补 enterprise_id（P0-1 完整所属链；SQLite/PG 均执行）
    op.add_column("deliverable_version", sa.Column("enterprise_id", sa.BigInteger(), nullable=False, server_default="0"))
    op.add_column("enterprise_asset_revision", sa.Column("enterprise_id", sa.BigInteger(), nullable=False, server_default="0"))
    op.add_column("enterprise_fact_evidence", sa.Column("enterprise_id", sa.BigInteger(), nullable=False, server_default="0"))
    op.add_column("project_material_revision", sa.Column("enterprise_id", sa.BigInteger(), nullable=False, server_default="0"))

    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for table in RLS_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation_{table} ON {table} "
            "USING (current_setting('app.enterprise_id', true) <> '' "
            "       AND enterprise_id = current_setting('app.enterprise_id', true)::bigint) "
            "WITH CHECK (current_setting('app.enterprise_id', true) <> '' "
            "            AND enterprise_id = current_setting('app.enterprise_id', true)::bigint)"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for table in RLS_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.drop_column("enterprise_fact_evidence", "enterprise_id")
    op.drop_column("enterprise_asset_revision", "enterprise_id")
    op.drop_column("deliverable_version", "enterprise_id")
    op.drop_column("project_material_revision", "enterprise_id")
