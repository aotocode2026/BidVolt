"""容器内检查数据库编码与中文写入。"""

from __future__ import annotations

import asyncio
import os

import asyncpg


async def main() -> None:
    url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(url)
    print("server_encoding:", await conn.fetchval("SHOW server_encoding"))
    print("client_encoding:", await conn.fetchval("SHOW client_encoding"))
    print("chinese:", await conn.fetchval("SELECT '中文写入'::text"))
    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
