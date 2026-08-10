"""M1 基础表：认证/租户/项目/配额/审计/文件/任务

Revision ID: 0001
Revises:
Create Date: 2026-08-10
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# JSONB（PG）→ JSON（SQLite 开发/测试）
json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "enterprise",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("credit_code", sa.String(length=50), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_table(
        "app_user",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("enterprise_id", sa.BigInteger(), sa.ForeignKey("enterprise.id"), nullable=False, index=True),
        sa.Column("email", sa.String(length=255), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("status", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column("permissions", json_type, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_table(
        "refresh_token",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("token_hash", sa.String(length=255), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_table(
        "enterprise_permission",
        sa.Column("enterprise_id", sa.BigInteger(), sa.ForeignKey("enterprise.id"), primary_key=True),
        sa.Column("permissions", json_type, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_table(
        "project_edit_lock",
        sa.Column("project_id", sa.BigInteger(), primary_key=True),
        sa.Column("lock_token", sa.String(length=64), nullable=False),
        sa.Column("holder_user_id", sa.BigInteger(), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "project",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("tender_no", sa.String(length=100), nullable=True),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("idx_project_ent_status", "project", ["enterprise_id", "status"])
    op.create_table(
        "tenant_quota",
        sa.Column("enterprise_id", sa.BigInteger(), sa.ForeignKey("enterprise.id"), primary_key=True),
        sa.Column("storage_bytes", sa.BigInteger(), nullable=False, server_default=str(10 * 1024**3)),
        sa.Column("concurrent_tasks", sa.BigInteger(), nullable=False, server_default="3"),
        sa.Column("model_tokens_daily", sa.BigInteger(), nullable=False, server_default="2000000"),
        sa.Column("search_daily", sa.BigInteger(), nullable=False, server_default="1000"),
        sa.Column("export_daily", sa.BigInteger(), nullable=False, server_default="50"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("project_id", sa.BigInteger(), nullable=True),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("object_type", sa.String(length=50), nullable=True),
        sa.Column("object_id", sa.BigInteger(), nullable=True),
        sa.Column("payload", json_type, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_table(
        "file_object",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("project_id", sa.BigInteger(), nullable=True),
        sa.Column("owner_type", sa.SmallInteger(), nullable=False),
        sa.Column("bucket", sa.String(length=100), nullable=False),
        sa.Column("object_key", sa.String(length=500), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("original_name", sa.String(length=500), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("mime_type", sa.String(length=100), nullable=True),
        sa.Column("ext", sa.String(length=20), nullable=True),
        sa.Column("category", sa.String(length=50), nullable=True),
        sa.Column("status", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column("parse_status", json_type, nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_table(
        "archive_job",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False),
        sa.Column("archive_file_id", sa.BigInteger(), sa.ForeignKey("file_object.id"), nullable=False),
        sa.Column("status", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column("result", json_type, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_table(
        "task",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("task_type", sa.String(length=50), nullable=False),
        sa.Column("idempotency_key", sa.String(length=100), nullable=False),
        sa.Column("priority", sa.SmallInteger(), nullable=False, server_default="5"),
        sa.Column("status", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column("payload", json_type, nullable=False),
        sa.Column("result", json_type, nullable=True),
        sa.Column("progress", json_type, nullable=True),
        sa.Column("retry_count", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("generation", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("error", json_type, nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("idempotency_key", "enterprise_id", name="uq_task_idem"),
    )


def downgrade() -> None:
    op.drop_table("task")
    op.drop_table("archive_job")
    op.drop_table("file_object")
    op.drop_table("audit_log")
    op.drop_table("tenant_quota")
    op.drop_index("idx_project_ent_status", table_name="project")
    op.drop_table("project")
    op.drop_table("project_edit_lock")
    op.drop_table("enterprise_permission")
    op.drop_table("refresh_token")
    op.drop_table("app_user")
    op.drop_table("enterprise")
