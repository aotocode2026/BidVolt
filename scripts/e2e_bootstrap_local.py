"""本地浏览器 E2E 前置：用 create_all 建 SQLite 测试库。

为什么不用 alembic：迁移脚本使用 PG 方言（server_default=now()、BigInteger 主键），
SQLite 下无法自增/无 now() 函数，与 pytest 测试环境（conftest 的 create_all）同理。
生产 PG 仍走 alembic（0015 任务租约列已含）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine

import app.models  # noqa: F401  注册全部模型
from app.models.base import Base

DB = Path(__file__).resolve().parent.parent / ".e2e_bidvolt.db"
DB.unlink(missing_ok=True)
engine = create_engine(f"sqlite:///{DB}")
Base.metadata.create_all(engine)
engine.dispose()
print(f"E2E 库已创建：{DB}")
