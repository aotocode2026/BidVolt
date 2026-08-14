"""review_provider 唯一约束修正（服务器实测修复）：provider_code 全局唯一 → (enterprise_id, provider_code) 复合唯一

背景：内置 Provider 按企业隔离创建（commit 2c41cd4），但 0005 迁移给 provider_code
建了全局唯一约束，第二个企业评审时 INSERT builtin_completeness 直接撞
UniqueViolation（生产 evaluate 500 根因）。

说明：SQLite 不支持具名唯一约束删除，本迁移仅在 PostgreSQL 生效；
SQLite 测试库走 create_all（模型已声明复合唯一，见 app/models/review.py）。

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-14
"""
from __future__ import annotations

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return  # SQLite 测试库走 create_all，无需迁移
    # PG 自动命名：内联 unique=True → review_provider_provider_code_key
    op.drop_constraint("review_provider_provider_code_key", "review_provider", type_="unique")
    op.create_unique_constraint(
        "uq_review_provider_ent_code", "review_provider", ["enterprise_id", "provider_code"]
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.drop_constraint("uq_review_provider_ent_code", "review_provider", type_="unique")
    op.create_unique_constraint("review_provider_provider_code_key", "review_provider", ["provider_code"])
