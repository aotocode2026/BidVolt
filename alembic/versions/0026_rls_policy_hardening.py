"""RLS 策略防弹化：全部启用 RLS 的表改用「永不报错」的守卫形式。

根因（风光 R6 任务 1769 三次重试耗尽）：agent_session_event 的线上策略被
手工改成了裸 current_setting('app.enterprise_id')::bigint（无 missing_ok、
无守卫），事务中 GUC 为空串时直接炸 InvalidTextRepresentation:
invalid input syntax for type bigint: ""，杀掉泵循环。

新策略：CASE WHEN GUC ~ '^[0-9]+$' THEN GUC::bigint END——
GUC 未设置/NULL/空串/垃圾值一律过滤（不报错），正常值正常放行。

Revision ID: 0026
Revises: 0025
Create Date: 2026-08-31
"""
import sqlalchemy as sa

from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

# 与 0007/0008 启用的 FORCE RLS 表保持一致（线上实测 31 张）
RLS_TABLES = [
    "agent_artifact",
    "agent_session_event",
    "citation",
    "conversation",
    "conversation_message",
    "deliverable",
    "deliverable_version",
    "editor_session",
    "enterprise_asset",
    "enterprise_asset_revision",
    "enterprise_fact",
    "enterprise_fact_evidence",
    "enterprise_fact_revision",
    "export_job",
    "file_object",
    "final_check",
    "history_price_snapshot",
    "material_match_result",
    "project",
    "project_event",
    "project_material",
    "project_material_revision",
    "project_snapshot",
    "quote_calc",
    "requirement",
    "requirement_revision",
    "review_item",
    "review_material_link",
    "review_run",
    "score_record",
    "search_source",
]

_GUARD = (
    "(CASE WHEN current_setting('app.enterprise_id', true) ~ '^[0-9]+$' "
    "THEN current_setting('app.enterprise_id', true)::bigint END)"
)


def _rebuild(direction: int) -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for table in RLS_TABLES:
        # 删掉该表全部既有策略（含历史手工策略名，如 ase_rls），再重建标准策略
        op.execute(
            f"DO $$ DECLARE p text; BEGIN "
            f"FOR p IN SELECT polname FROM pg_policy WHERE polrelid = '{table}'::regclass LOOP "
            f"EXECUTE format('DROP POLICY %I ON {table}', p); "
            f"END LOOP; END $$;"
        )
        if direction > 0:
            op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
            op.execute(
                f"CREATE POLICY tenant_isolation_{table} ON {table} "
                f"USING (enterprise_id = {_GUARD}) WITH CHECK (enterprise_id = {_GUARD})"
            )
        else:
            op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")


def upgrade() -> None:
    _rebuild(1)


def downgrade() -> None:
    _rebuild(-1)
