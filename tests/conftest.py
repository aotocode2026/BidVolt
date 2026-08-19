"""pytest 公共夹具：SQLite 测试库 + TestClient。

PG 对等模式（Issue #13 复盘缺口④）：
    BIDVOLT_TEST_DATABASE_URL=postgresql+asyncpg://... 时，schema/client/clean_db 全部
    使用该真实 PostgreSQL 测试库（会禁用其 RLS 策略——仅测试库，生产库不动）。
    SQLite 对"多行标量子查询"等 PG 严格性错误静默放行，关键回归必须在 PG 上复跑。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path

# 必须在导入 app 之前设置（env var 优先级高于 .env 文件）。
# BIDVOLT_KEEP_DATABASE_URL=1 时保留外部 DATABASE_URL（容器 RLS 测试用真实 PG）。
if os.environ.get("BIDVOLT_KEEP_DATABASE_URL") != "1":
    os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./.test_bidvolt.db"
os.environ["STORAGE_ROOT"] = os.path.join(tempfile.gettempdir(), "bidvolt_test_storage")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401  注册全部模型
from app.config import settings
from app.db import get_session
from app.main import app
from app.models.base import Base

TEST_DB = Path(__file__).resolve().parent.parent / ".test_bidvolt.db"
TEST_DB_URL = os.environ.get("BIDVOLT_TEST_DATABASE_URL") or "sqlite+aiosqlite:///./.test_bidvolt.db"
IS_PG = TEST_DB_URL.startswith("postgresql")


def make_test_engine():
    return create_async_engine(TEST_DB_URL, pool_pre_ping=True)


def _sync_schema() -> None:
    async def _run() -> None:
        engine = make_test_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
            if IS_PG:
                # 仅测试库：禁用 RLS，让 clean_db 能跨租户清表；
                # API 依赖里的 set_config 注入路径照常执行（无害），RLS 语义由生产验证。
                for table in Base.metadata.sorted_tables:
                    await conn.execute(text(f'ALTER TABLE "{table.name}" DISABLE ROW LEVEL SECURITY'))
        await engine.dispose()

    asyncio.run(_run())


@pytest.fixture(scope="session", autouse=True)
def schema() -> None:
    _sync_schema()


@pytest.fixture(scope="session")
def client(schema):
    async_engine = make_test_engine()
    testing_session = async_sessionmaker(async_engine, expire_on_commit=False)

    async def override_get_session():
        async with testing_session() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()

    async def _dispose():
        await async_engine.dispose()

    asyncio.run(_dispose())


@pytest.fixture(autouse=True)
def clean_db():
    """每个测试前清空全部表，保证用例隔离。"""
    async def _run() -> None:
        engine = make_test_engine()
        async with engine.begin() as conn:
            for table in reversed(Base.metadata.sorted_tables):
                await conn.execute(table.delete())
        await engine.dispose()

    asyncio.run(_run())
    shutil.rmtree(os.environ["STORAGE_ROOT"], ignore_errors=True)
    yield


@pytest.fixture(autouse=True)
def cloud_gate_closed_by_default(monkeypatch):
    """测试默认门禁关闭：不依赖开发机 .env 的实际解锁状态。

    需要云能力（LLM/VL/搜索）的用例必须显式 monkeypatch 打开门禁并注入测试 Key；
    防止测试集在 .env 解锁后意外调用真实外部 API 或产生非确定性结果。
    """
    monkeypatch.setattr(settings, "data_classification_confirmed", 0)
    monkeypatch.setattr(settings, "cloud_llm_enabled", 0)
    monkeypatch.setattr(settings, "search_enabled", 0)
    monkeypatch.setattr(settings, "minimax_api_key", "")
    monkeypatch.setattr(settings, "dashscope_api_key", "")
    monkeypatch.setattr(settings, "anysearch_key", "")
    yield
