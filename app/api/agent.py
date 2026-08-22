"""新方案接口（Agent 主会话端到端）：与旧任务接口完全隔离。

- POST /projects/{project_id}/agent-run        提交一次 Agent 主会话任务（新 task_type）
- GET  /projects/{project_id}/agent-run/{id}   查看状态/进度/结果
- GET  /projects/{project_id}/agent-run/{id}/stream  会话控制台事件流（SSE，含服务消息/主会话回复）
- POST /projects/{project_id}/agent-run/{id}/chat    客户直接与主会话对话（同一 session 追加消息）

开关：settings.agent_pipeline_enabled=0 时本接口返回 409，旧流程不受任何影响。
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import UserContext, require_permission
from app.config import settings
from app.constants import Permission, TaskType
from app.db import get_session
from app.models.agent import AgentSessionEvent
from app.models.project import Project
from app.models.task import Task
from app.services.capability import issue_capability
from app.services.task_service import create_task, public_event

router = APIRouter(prefix="/projects", tags=["agent-pipeline"])


async def _get_agent_task(session: AsyncSession, user: UserContext, project_id: int, task_id: int) -> Task:
    task = await session.scalar(
        select(Task).where(
            Task.id == task_id,
            Task.enterprise_id == user.enterprise_id,
            Task.project_id == project_id,
            Task.task_type == TaskType.AGENT_PIPELINE,
        )
    )
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent 主会话任务不存在")
    return task


@router.post("/{project_id}/agent-run", status_code=status.HTTP_201_CREATED)
async def agent_run(
    project_id: int,
    body: dict,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.PROJECT_EDIT)),
) -> dict:
    if not settings.agent_pipeline_enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Agent 主会话流程未启用（AGENT_PIPELINE_ENABLED=0）。旧流程接口不受影响，可继续使用。",
        )
    project = await session.scalar(
        select(Project).where(
            Project.id == project_id,
            Project.enterprise_id == user.enterprise_id,
        )
    )
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="项目不存在")
    idempotency_key = body.get("idempotency_key")
    if not idempotency_key:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="缺少 idempotency_key")
    payload = dict(body.get("payload") or {})
    resume_from_task_id = body.get("resume_from_task_id")
    if resume_from_task_id is not None:
        # 续跑上一单：校验上一任务归属与类型，取其会话 id 注入 payload
        prev = await session.scalar(
            select(Task).where(
                Task.id == int(resume_from_task_id),
                Task.enterprise_id == user.enterprise_id,
                Task.project_id == project_id,
                Task.task_type == TaskType.AGENT_PIPELINE,
            )
        )
        if prev is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="要续跑的任务不存在或不属于本项目")
        prev_sid = (prev.result or {}).get("session_id")
        if not prev_sid:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="要续跑的任务没有可恢复的主会话（可能未完成或未产出会话），请改为普通发起。",
            )
        payload["resume_from_task_id"] = int(resume_from_task_id)
        payload["resume_session_id"] = prev_sid
    task, created = await create_task(
        session,
        enterprise_id=user.enterprise_id,
        project_id=project_id,
        task_type=TaskType.AGENT_PIPELINE,
        payload=payload,
        idempotency_key=idempotency_key,
    )
    await session.commit()
    return {
        "task_id": task.id,
        "status": task.status,
        "created": created,
        "resume_from_task_id": resume_from_task_id,
        "resume_session_id": payload.get("resume_session_id"),
        "progress": public_event(task),
        "capability_token": issue_capability(
            enterprise_id=user.enterprise_id,
            project_id=project_id,
            task_id=task.id,
            task_type=TaskType.AGENT_PIPELINE,
        ),
    }


@router.get("/{project_id}/agent-run/{task_id}")
async def agent_run_status(
    project_id: int,
    task_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.FILE_READ)),
) -> dict:
    task = await _get_agent_task(session, user, project_id, task_id)
    return {
        "task_id": task.id,
        "task_type": task.task_type,
        "status": task.status,
        "progress": task.progress,
        "result": task.result,
        "error": task.error,
    }


@router.get("/{project_id}/agent-run/{task_id}/stream")
async def agent_run_stream(
    project_id: int,
    task_id: int,
    since: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.FILE_READ)),
) -> StreamingResponse:
    """会话控制台事件流：先补发 since 之后的历史事件，再实时推送新事件，任务终态后发 end。"""
    if not settings.agent_pipeline_enabled:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Agent 主会话流程未启用")
    task = await _get_agent_task(session, user, project_id, task_id)

    terminal = {3, 4, 5, 6}

    async def generate():
        last_seq = since
        while True:
            rows = (
                await session.scalars(
                    select(AgentSessionEvent)
                    .where(
                        AgentSessionEvent.task_id == task_id,
                        AgentSessionEvent.seq > last_seq,
                    )
                    .order_by(AgentSessionEvent.seq)
                    .limit(200)
                )
            ).all()
            for r in rows:
                last_seq = max(last_seq, r.seq)
                yield f"event: message\ndata: {json.dumps({'seq': r.seq, 'kind': r.kind, 'content': r.content}, ensure_ascii=False)}\n\n"
            await session.refresh(task)
            if task.status in terminal:
                r = task.result or {}
                yield (
                    "event: end\ndata: "
                    + json.dumps(
                        {
                            "status": task.status,
                            "session_id": r.get("session_id"),
                            "outcome": r.get("outcome"),
                            "reason": r.get("reason"),
                            "error": task.error,
                        },
                        ensure_ascii=False,
                    )
                    + "\n\n"
                )
                break
            await asyncio.sleep(1)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/{project_id}/agent-run/{task_id}/chat")
async def agent_run_chat(
    project_id: int,
    task_id: int,
    body: dict,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.PROJECT_EDIT)),
) -> dict:
    """客户直接与主会话对话：向同一 session 追加一条消息，返回主会话回复。"""
    if not settings.agent_pipeline_enabled:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Agent 主会话流程未启用")
    task = await _get_agent_task(session, user, project_id, task_id)
    message = str(body.get("message") or "").strip()
    if not message:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="message 不能为空")
    from app.services.agent_pipeline import chat_with_session

    try:
        return await chat_with_session(session, task, message)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
