"""行情库字段放宽：材料名/规格/发布单位/品类/包名/公告ID 超长值（200 截断崩溃根因）。

Revision ID: 0022
Revises: 0021
Create Date: 2026-08-30
"""
import sqlalchemy as sa

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

_COLS = (
    ("material_name", 500, 200),
    ("material_code", 200, 100),
    ("spec", 500, 200),
    ("region", 300, 100),
    ("publisher", 500, 200),
    ("category", 500, 200),
    ("package_name", 500, 300),
    ("notice_id", 300, 100),
    ("limit_evidence", 500, 300),
    ("win_evidence", 500, 300),
)


def _alter(cols: tuple, direction: int) -> None:
    with op.batch_alter_table("history_price_snapshot") as batch:
        for name, new, old in cols:
            to_len, from_len = (new, old) if direction > 0 else (old, new)
            batch.alter_column(name, type_=sa.String(to_len), existing_type=sa.String(from_len))


def upgrade() -> None:
    _alter(_COLS, 1)


def downgrade() -> None:
    _alter(_COLS, -1)
