"""历史中标价公共/私有行情库：字段扩展 + 公共行 RLS 豁免。

Revision ID: 0019
Revises: 0018
Create Date: 2026-08-26
"""
import sqlalchemy as sa

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("history_price_snapshot", sa.Column("publisher", sa.String(200)))
    op.add_column("history_price_snapshot", sa.Column("category", sa.String(200)))
    op.add_column("history_price_snapshot", sa.Column("package_name", sa.String(300)))
    op.add_column("history_price_snapshot", sa.Column("price_mode", sa.String(30)))
    op.add_column("history_price_snapshot", sa.Column("limit_price", sa.Numeric(18, 4)))
    op.add_column("history_price_snapshot", sa.Column("publish_date", sa.Date()))
    op.add_column("history_price_snapshot", sa.Column("notice_id", sa.String(100)))
    op.add_column("history_price_snapshot", sa.Column("limit_evidence", sa.String(300)))
    op.add_column("history_price_snapshot", sa.Column("win_evidence", sa.String(300)))
    op.add_column("history_price_snapshot", sa.Column("limit_evidence_url", sa.String(500)))
    op.add_column("history_price_snapshot", sa.Column("win_evidence_url", sa.String(500)))

    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    # 公共行情库（enterprise_id=0）对全平台租户可见；企业私有行仍按租户隔离
    op.execute("DROP POLICY IF EXISTS tenant_isolation_history_price_snapshot ON history_price_snapshot")
    op.execute(
        "CREATE POLICY tenant_isolation_history_price_snapshot ON history_price_snapshot "
        "USING (enterprise_id = 0 "
        "       OR (current_setting('app.enterprise_id', true) <> '' "
        "           AND enterprise_id = current_setting('app.enterprise_id', true)::bigint)) "
        "WITH CHECK (enterprise_id = 0 "
        "            OR (current_setting('app.enterprise_id', true) <> '' "
        "                AND enterprise_id = current_setting('app.enterprise_id', true)::bigint))"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP POLICY IF EXISTS tenant_isolation_history_price_snapshot ON history_price_snapshot")
        op.execute(
            "CREATE POLICY tenant_isolation_history_price_snapshot ON history_price_snapshot "
            "USING (current_setting('app.enterprise_id', true) <> '' "
            "       AND enterprise_id = current_setting('app.enterprise_id', true)::bigint) "
            "WITH CHECK (current_setting('app.enterprise_id', true) <> '' "
            "            AND enterprise_id = current_setting('app.enterprise_id', true)::bigint)"
        )
    for col in (
        "publisher", "category", "package_name", "price_mode", "limit_price",
        "publish_date", "notice_id", "limit_evidence", "win_evidence",
        "limit_evidence_url", "win_evidence_url",
    ):
        op.drop_column("history_price_snapshot", col)
