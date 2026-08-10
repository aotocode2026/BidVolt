"""pytest 公共夹具：SQLite 测试库 + TestClient。"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

# 必须在导入 app 之前设置（env var 优先级高于 .env 文件）
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./.test_bidvolt.db"
os.environ["STORAGE_ROOT"] = os.path.join(tempfile.gettempdir(), "bidvolt_test_storage")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401  注册全部模型
from app.db import get_session
from app.main import app
from app.models.base import Base

TEST_DB = Path(__file__).resolve().parent.parent / ".test_bidvolt.db"


def _sync_schema() -> None:
    engine = create_engine(f"sqlite:///{TEST_DB}")
    Base.metadata.create_all(engine)
    engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def schema() -> None:
    _sync_schema()


@pytest.fixture(scope="session")
def client(schema):
    async_engine = create_async_engine(f"sqlite+aiosqlite:///{TEST_DB}")
    testing_session = async_sessionmaker(async_engine, expire_on_commit=False)

    async def override_get_session():
        async with testing_session() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    async_engine.sync_engine.dispose()


@pytest.fixture(autouse=True)
def clean_db():
    """每个测试前清空全部表，保证用例隔离。"""
    engine = create_engine(f"sqlite:///{TEST_DB}")
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())
    engine.dispose()
    shutil.rmtree(os.environ["STORAGE_ROOT"], ignore_errors=True)
    yield
