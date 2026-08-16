"""后台任务消费者（supervisor 运行：python -m app.services.worker）。"""

from __future__ import annotations

import asyncio
import os
import socket

from app.db import SessionLocal
from app.services.task_service import claim_next, reclaim_stale, run_task


def _worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


async def worker_loop() -> None:
    """单 worker 消费循环：领取 → 带租约执行（心跳续期）→ 回收过期任务。

    V1 单 worker（supervisor [program:worker] 一个实例）；租约保证进程被强杀/
    卡死后任务可被回收重新入队，不再永久卡 RUNNING。
    """
    worker_id = _worker_id()
    while True:
        worked = False
        try:
            async with SessionLocal() as session:
                task = await claim_next(session)
                if task is not None:
                    worked = True
                    await run_task(
                        session,
                        task,
                        lease_owner=worker_id,
                        session_factory=SessionLocal,
                    )
                else:
                    worked = await reclaim_stale(session)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            print(f"[worker] 处理失败：{exc}", flush=True)
            try:
                # 落盘诊断日志（生产定位用；supervisor 子进程 stdout 可能不可见）
                with open("/data/logs/bidvolt/worker.log", "a", encoding="utf-8") as f:
                    f.write(f"[worker] 处理失败：{exc!r}\n")
            except OSError:
                pass
        if not worked:
            await asyncio.sleep(0.5)


if __name__ == "__main__":
    if os.environ.get("TASK_WORKER_ENABLED", "1") != "1":
        raise SystemExit("TASK_WORKER_ENABLED=0，worker 不启动")
    try:
        asyncio.run(worker_loop())
    except KeyboardInterrupt:
        pass  # supervisor 停机信号：run_task 的 finally 已释放租约/重新入队
