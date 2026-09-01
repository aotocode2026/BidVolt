"""异步数据库引擎与会话。"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

engine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,
    # R9 线上根因：泵循环任何一次连接获取/归还悬挂都会让整轮永久冻结
    # （async with SessionLocal() 的 __aenter__ 无超时=无限等待）。
    # 10s 获取上限让所有泵/API 数据库操作都有界失败→上层 try/except 自愈。
    pool_timeout=10,
    # 30 分钟强制回收长命连接：服务器侧被终止/半死的连接不会在本进程内
    # 无限期滞留（pool_pre_ping 只挡死 socket，挡不住悬挂事务占池）。
    pool_recycle=1800,
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：每个请求一个会话。"""
    async with SessionLocal() as session:
        yield session
