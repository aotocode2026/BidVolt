"""后台任务消费者（supervisor 运行：python -m app.services.worker）。"""

from __future__ import annotations

import asyncio
import os

from app.db import SessionLocal
from app.services.task_service import run_next_task


async def worker_loop() -> None:
    while True:
        async with SessionLocal() as session:
            try:
                await run_next_task(session)
            except Exception as exc:  # noqa: BLE001
                print(f"[worker] 处理失败：{exc}", flush=True)
        await asyncio.sleep(0.5)


if __name__ == "__main__":
    if os.environ.get("TASK_WORKER_ENABLED", "1") != "1":
        raise SystemExit("TASK_WORKER_ENABLED=0，worker 不启动")
    asyncio.run(worker_loop())
