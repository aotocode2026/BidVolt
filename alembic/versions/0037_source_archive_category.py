"""企业资料“源文件”分类与存量源包回填（discussion #55）。

Revision ID: 0037
Revises: 0036
Create Date: 2026-09-10
"""

from __future__ import annotations

from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    # 1) 每个企业补齐“源文件”分类（复用已有，不重复创建）
    op.execute(
        "INSERT INTO enterprise_asset_category (enterprise_id, name, created_at) "
        "SELECT DISTINCT e.id, '源文件', now() FROM enterprise e "
        "WHERE NOT EXISTS (SELECT 1 FROM enterprise_asset_category c "
        "WHERE c.enterprise_id = e.id AND c.name = '源文件')"
    )
    # 2) 历史源压缩包资产回填：归入“源文件”、状态 4（无需业务分类）。
    #    UPDATE 涉及两张 RLS 表：临时关闭 RLS 完成数据回填后恢复。
    op.execute("ALTER TABLE enterprise_asset DISABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE file_object DISABLE ROW LEVEL SECURITY")
    op.execute(
        "UPDATE enterprise_asset a SET asset_type='源文件', status=4, "
        "category_id=(SELECT c.id FROM enterprise_asset_category c "
        "WHERE c.enterprise_id=a.enterprise_id AND c.name='源文件' LIMIT 1) "
        "WHERE a.source_file_id IS NOT NULL AND a.source_file_id IN "
        "(SELECT f.id FROM file_object f WHERE f.ext='.zip')"
    )
    op.execute("ALTER TABLE enterprise_asset FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE file_object FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    # 仅清理未被资产引用的“源文件”分类（资产状态不回退，避免破坏已发布数据）
    op.execute(
        "DELETE FROM enterprise_asset_category c WHERE c.name='源文件' "
        "AND NOT EXISTS (SELECT 1 FROM enterprise_asset a WHERE a.category_id=c.id)"
    )
