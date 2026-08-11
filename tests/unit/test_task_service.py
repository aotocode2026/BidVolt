from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.constants import TaskStatus, TaskType
from app.models.task import Task
from app.services import task_service

TEST_DB = "./.test_bidvolt.db"


def _task(**overrides) -> Task:
    defaults = dict(
        enterprise_id=1,
        project_id=1,
        task_type=TaskType.TENDER_PARSE,
        idempotency_key="k",
        status=int(TaskStatus.QUEUED),
        payload={},
    )
    defaults.update(overrides)
    return Task(**defaults)


def test_public_event_only_whitelist_fields():
    task = _task(
        progress={
            "phase": "generate",
            "status": "running",
            "percent": 40,
            "current_work": "生成技术标",
            "summary": "ok",
            "hint": None,
            "tool_params": {"secret": 1},
            "internal_id": "task-1",
            "credentials": "abc",
        }
    )
    event = task_service.public_event(task)
    assert set(event.keys()) == {"phase", "status", "percent", "current_work", "summary", "hint"}
    assert "tool_params" not in event
    assert "credentials" not in event
    assert "internal_id" not in event


def test_create_task_idempotent():
    engine = create_async_engine(f"sqlite+aiosqlite:///{TEST_DB}")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def scenario():
        async with factory() as session:
            first, created1 = await task_service.create_task(
                session,
                enterprise_id=1,
                project_id=1,
                task_type=TaskType.TENDER_PARSE,
                payload={},
                idempotency_key="dup",
            )
            await session.commit()
            second, created2 = await task_service.create_task(
                session,
                enterprise_id=1,
                project_id=1,
                task_type=TaskType.TENDER_PARSE,
                payload={},
                idempotency_key="dup",
            )
            return first.id, created1, second.id, created2

    first_id, c1, second_id, c2 = asyncio.run(scenario())
    engine.sync_engine.dispose()
    assert c1 is True
    assert c2 is False
    assert first_id == second_id


def test_run_task_success_and_retry_terminal(monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{TEST_DB}")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    calls = {"n": 0}

    async def failing_handler(session, task):
        calls["n"] += 1
        raise RuntimeError("boom")

    monkeypatch.setitem(task_service.HANDLERS, TaskType.TENDER_PARSE, failing_handler)

    async def scenario():
        async with factory() as session:
            task, _ = await task_service.create_task(
                session,
                enterprise_id=1,
                project_id=1,
                task_type=TaskType.TENDER_PARSE,
                payload={},
                idempotency_key="fail-task",
            )
            await session.commit()
            for _ in range(3):
                await task_service.run_task(session, task)
            return task

    task = asyncio.run(scenario())
    engine.sync_engine.dispose()
    assert calls["n"] == 3
    assert task.status == int(TaskStatus.FAILED_TERMINAL)
    assert task.retry_count == 3
    assert task.error == {"message": "boom"}


def test_handler_partial_write_rolls_back_with_failure(monkeypatch):
    """A-4 单事务：handler 的写入与任务终态同事务提交；失败时部分写入不落库。"""
    from sqlalchemy import select

    from app.models.project import Project

    engine = create_async_engine(f"sqlite+aiosqlite:///{TEST_DB}")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def partial_then_fail(session, task):
        session.add(Project(enterprise_id=task.enterprise_id, name="不应存在", status=1))
        raise RuntimeError("boom-after-write")

    monkeypatch.setitem(task_service.HANDLERS, TaskType.MATERIAL_MATCH, partial_then_fail)

    async def scenario():
        async with factory() as session:
            task, _ = await task_service.create_task(
                session,
                enterprise_id=1,
                project_id=1,
                task_type=TaskType.MATERIAL_MATCH,
                payload={},
                idempotency_key="atomic-fail",
            )
            await session.commit()
            await task_service.run_task(session, task)
            return task.status

    status = asyncio.run(scenario())

    async def check_persisted():
        async with factory() as session:
            return await session.scalar(select(Project).where(Project.name == "不应存在"))

    persisted = asyncio.run(check_persisted())
    engine.sync_engine.dispose()
    assert persisted is None, "handler 失败后部分写入未被回滚"
    assert status == int(TaskStatus.QUEUED)  # 失败后进入重试队列
