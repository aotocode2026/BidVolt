"""worker 泵循环健壮性回归：handler 抛 MissingGreenlet 时不崩 worker、任务正确重新入队（issue #25）。"""

from __future__ import annotations

import asyncio

from sqlalchemy.exc import MissingGreenlet
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.constants import TaskStatus
from app.models.task import Task
from app.services.task_service import HANDLERS, run_task
from tests.conftest import make_test_engine


def test_run_task_survives_missing_greenlet_handler(monkeypatch):
    engine = make_test_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _poisoned_handler(session, task):
        raise MissingGreenlet(
            "greenlet_spawn has not been called; can't call await_only() here."
        )

    monkeypatch.setitem(HANDLERS, "missing_greenlet_test", _poisoned_handler)

    async def _run() -> Task:
        async with factory() as s:
            task = Task(
                enterprise_id=1,
                project_id=1,
                task_type="missing_greenlet_test",
                idempotency_key="mg-test",
                priority=5,
                status=int(TaskStatus.QUEUED),
                payload={},
            )
            s.add(task)
            await s.commit()
            await s.refresh(task)
            # handler 抛 MissingGreenlet：run_task 必须自愈（不把异常抛给 worker 主循环）
            await run_task(s, task, lease_owner="w1", session_factory=factory)
        async with factory() as s2:
            return await s2.get(Task, task.id)

    final = asyncio.run(_run())
    assert final.status == int(TaskStatus.QUEUED)
    assert final.retry_count == 1
    asyncio.run(engine.dispose())
