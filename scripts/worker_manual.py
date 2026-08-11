"""容器内手动执行一次任务队列消费，打印真实错误。"""

from __future__ import annotations

import asyncio

from app.db import SessionLocal
from app.services.task_service import run_next_task


async def main() -> None:
    async with SessionLocal() as session:
        task = await run_next_task(session)
        if task is None:
            print("no queued task")
        else:
            print("task:", task.id, "status:", task.status, "error:", task.error)


if __name__ == "__main__":
    asyncio.run(main())
