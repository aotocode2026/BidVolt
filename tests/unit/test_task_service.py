from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
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


# ---------- 租约 / 心跳 / 中断恢复（Issue #3） ----------


def test_run_task_sets_and_releases_lease(monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{TEST_DB}")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def ok_handler(session, task):
        task.result = {"ok": True}

    monkeypatch.setitem(task_service.HANDLERS, TaskType.CHAT, ok_handler)

    async def scenario():
        async with factory() as session:
            task, _ = await task_service.create_task(
                session,
                enterprise_id=1,
                project_id=1,
                task_type=TaskType.CHAT,
                payload={},
                idempotency_key="lease-done",
            )
            await session.commit()
            await task_service.run_task(session, task, lease_owner="worker:1")
            return task

    task = asyncio.run(scenario())
    engine.sync_engine.dispose()
    assert task.status == int(TaskStatus.DONE)
    # 终态后租约释放：不会过期误回收
    assert task.lease_owner is None
    assert task.lease_expires_at is None
    assert task.last_heartbeat_at is not None


def test_reclaim_stale_requeues_expired_lease():
    engine = create_async_engine(f"sqlite+aiosqlite:///{TEST_DB}")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def scenario():
        async with factory() as session:
            task, _ = await task_service.create_task(
                session,
                enterprise_id=1,
                project_id=1,
                task_type=TaskType.CHAT,
                payload={},
                idempotency_key="lease-stale",
            )
            # 模拟 worker 领取后进程被强杀：RUNNING + 过期租约
            task.status = int(TaskStatus.RUNNING)
            task.lease_owner = "dead-worker:9"
            task.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()
            reclaimed = await task_service.reclaim_stale(session)
            await session.refresh(task)
            return reclaimed, task

    reclaimed, task = asyncio.run(scenario())
    engine.sync_engine.dispose()
    assert reclaimed is True
    assert task.status == int(TaskStatus.QUEUED)
    assert task.retry_count == 1
    assert task.lease_owner is None
    assert task.lease_expires_at is None


def test_reclaim_stale_ignores_valid_lease():
    engine = create_async_engine(f"sqlite+aiosqlite:///{TEST_DB}")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def scenario():
        async with factory() as session:
            task, _ = await task_service.create_task(
                session,
                enterprise_id=1,
                project_id=1,
                task_type=TaskType.CHAT,
                payload={},
                idempotency_key="lease-valid",
            )
            task.status = int(TaskStatus.RUNNING)
            task.lease_owner = "live-worker:1"
            task.lease_expires_at = datetime.now(UTC) + timedelta(seconds=300)
            await session.commit()
            reclaimed = await task_service.reclaim_stale(session)
            await session.refresh(task)
            return reclaimed, task.status

    reclaimed, status = asyncio.run(scenario())
    engine.sync_engine.dispose()
    assert reclaimed is False
    assert status == int(TaskStatus.RUNNING)  # 正常执行中的任务不受影响


def test_reclaim_stale_terminal_after_retry_exhausted():
    engine = create_async_engine(f"sqlite+aiosqlite:///{TEST_DB}")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def scenario():
        async with factory() as session:
            task, _ = await task_service.create_task(
                session,
                enterprise_id=1,
                project_id=1,
                task_type=TaskType.CHAT,
                payload={},
                idempotency_key="lease-exhausted",
            )
            task.status = int(TaskStatus.RUNNING)
            task.retry_count = task_service.MAX_RETRIES - 1
            task.lease_owner = "dead-worker:9"
            task.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()
            await task_service.reclaim_stale(session)
            await session.refresh(task)
            return task

    task = asyncio.run(scenario())
    engine.sync_engine.dispose()
    assert task.status == int(TaskStatus.FAILED_TERMINAL)
    assert "worker" in task.error["message"]


def test_heartbeat_keeps_lease_fresh_during_handler(monkeypatch):
    """长 handler 期间心跳续期：租约不因执行时长过期。"""
    monkeypatch.setattr(task_service, "HEARTBEAT_INTERVAL", 0.05)
    monkeypatch.setattr(task_service, "LEASE_SECONDS", 1)
    engine = create_async_engine(f"sqlite+aiosqlite:///{TEST_DB}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    observed: dict = {}

    async def slow_handler(session, task):
        claim_hb = task_service._aware(task.last_heartbeat_at)
        await asyncio.sleep(0.2)  # 远大于 LEASE_SECONDS，靠心跳续期撑过去
        async with factory() as check:
            row = await check.scalar(
                select(Task.last_heartbeat_at).where(Task.id == task.id)
            )
        observed["renewed"] = row is not None and task_service._aware(row) > claim_hb
        task.result = {"slow": True}

    monkeypatch.setitem(task_service.HANDLERS, TaskType.CHAT, slow_handler)

    async def scenario():
        async with factory() as session:
            task, _ = await task_service.create_task(
                session,
                enterprise_id=1,
                project_id=1,
                task_type=TaskType.CHAT,
                payload={},
                idempotency_key="lease-heartbeat",
            )
            await session.commit()
            await task_service.run_task(
                session,
                task,
                lease_owner="worker:1",
                session_factory=factory,
            )
            return task

    task = asyncio.run(scenario())
    engine.sync_engine.dispose()
    assert task.status == int(TaskStatus.DONE)
    assert task.result == {"slow": True}
    assert observed.get("renewed") is True, "handler 执行期间心跳未成功续期"
