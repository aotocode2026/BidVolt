"""Issue #19：聊天消息关联、幂等与回复清洗的回归测试。"""

from __future__ import annotations

import asyncio

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.agent import AgentSessionEvent
from app.models.task import Task
from app.services.agent_pipeline import (
    _clean_reply,
    _find_client_message_event,
    _replay_chat_result,
    queue_chat_message,
)
from tests.conftest import make_test_engine


def test_clean_reply_strips_reasoning_box_and_session_trailer():
    raw = (
        "┌─ Reasoning ──────────────────────────────────────────────────────────────────┐\n"
        "用户连续发「11」。没有实际内容。我应该保持极简。\n"
        "收到。我在。\n"
        '↻ Resumed session 20260903_104750_965a9c "项目207投标工作台流程执行" (31 user messages, 516 total messages)\n'
    )
    out = _clean_reply(raw)
    assert "收到。我在。" in out
    assert "┌─ Reasoning" not in out
    assert "↻ Resumed session" not in out


def test_clean_reply_strips_ansi_and_trailer_lines():
    raw = "\x1b[1m收到。我在。\x1b[0m\nDuration: 12s\nMessages: 2"
    assert _clean_reply(raw) == "收到。我在。"


def test_clean_reply_keeps_plain_reply():
    assert _clean_reply("收到。我在。") == "收到。我在。"


def test_clean_reply_returns_empty_for_pure_noise():
    raw = "Window too small...\n┌─ Reasoning ───────────┐\n↻ Resumed session x (1 user messages, 1 total messages)"
    assert _clean_reply(raw) == ""


async def _make_task(session) -> Task:
    task = Task(
        enterprise_id=1,
        project_id=1,
        task_type="agent_pipeline",
        idempotency_key="agent-chat-test",
        priority=5,
        status=3,
        payload={},
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


def test_queue_chat_message_dedupe_by_client_message_id():
    async def _run() -> None:
        engine = make_test_engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as s:
            task = await _make_task(s)
            first = await queue_chat_message(s, task, "你好", client_message_id="cm-1")
            second = await queue_chat_message(s, task, "你好", client_message_id="cm-1")
            assert first["message_id"] == second["message_id"]
            assert second["duplicate"] is True
            count = await s.scalar(
                select(func.count()).select_from(AgentSessionEvent).where(
                    AgentSessionEvent.task_id == task.id,
                    AgentSessionEvent.kind == "user",
                )
            )
            assert count == 1
        await engine.dispose()

    asyncio.run(_run())


def test_replay_chat_result_returns_stored_reply():
    async def _run() -> None:
        engine = make_test_engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as s:
            task = await _make_task(s)
            user_event = AgentSessionEvent(
                enterprise_id=1,
                project_id=1,
                task_id=task.id,
                seq=1,
                kind="user",
                content="你好",
                client_message_id="cm-2",
            )
            s.add(user_event)
            await s.flush()
            reply_event = AgentSessionEvent(
                enterprise_id=1,
                project_id=1,
                task_id=task.id,
                seq=2,
                kind="hermes",
                content="收到。我在。",
                reply_to_seq=user_event.seq,
            )
            s.add(reply_event)
            await s.commit()

            found = await _find_client_message_event(s, task, "cm-2")
            assert found is not None and found.seq == 1
            result = await _replay_chat_result(s, task, found, "sid-1")
            assert result["status"] == "processed"
            assert result["reply"] == "收到。我在。"
            assert result["message_id"] == 1
            assert result["reply_to_message_id"] == 2
            assert result["duplicate"] is True
        await engine.dispose()

    asyncio.run(_run())


def test_replay_chat_result_returns_error_state():
    async def _run() -> None:
        engine = make_test_engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as s:
            task = await _make_task(s)
            user_event = AgentSessionEvent(
                enterprise_id=1,
                project_id=1,
                task_id=task.id,
                seq=1,
                kind="user",
                content="你好",
                client_message_id="cm-3",
            )
            s.add(user_event)
            await s.flush()
            error_event = AgentSessionEvent(
                enterprise_id=1,
                project_id=1,
                task_id=task.id,
                seq=2,
                kind="error",
                content="本轮处理失败：Hermes 进程退出码 1，请稍后重试",
                reply_to_seq=user_event.seq,
            )
            s.add(error_event)
            await s.commit()

            result = await _replay_chat_result(s, task, user_event, "sid-1")
            assert result["status"] == "failed"
            assert result["reply"] is None
            assert result["reply_to_message_id"] is None
            assert "退出码 1" in result["error"]
        await engine.dispose()

    asyncio.run(_run())
