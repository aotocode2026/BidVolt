"""任务接口（4.4.3）：提交/查询/中断/SSE 白名单事件流。"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import UserContext, require_permission
from app.constants import Permission, TaskType
from app.db import get_session
from app.models.task import Task
from app.services.capability import issue_capability
from app.services.task_service import create_task, public_event

router = APIRouter(tags=["tasks"])


@router.post("/projects/{project_id}/tasks", status_code=status.HTTP_201_CREATED)
async def submit_task(
    project_id: int,
    body: dict,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.PROJECT_EDIT)),
) -> dict:
    task_type = body.get("task_type")
    if task_type not in TaskType.ALL:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"未知任务类型：{task_type}")
    idempotency_key = body.get("idempotency_key")
    if not idempotency_key:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="缺少 idempotency_key")
    task, created = await create_task(
        session,
        enterprise_id=user.enterprise_id,
        project_id=project_id,
        task_type=task_type,
        payload=body.get("payload") or {},
        idempotency_key=idempotency_key,
    )
    await session.commit()
    return {
        "task_id": task.id,
        "status": task.status,
        "created": created,
        "progress": public_event(task),
        "capability_token": issue_capability(
            enterprise_id=user.enterprise_id,
            project_id=project_id,
            task_id=task.id,
            task_type=task_type,
        ),
    }


@router.get("/tasks/{task_id}")
async def task_status(
    task_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.FILE_READ)),
) -> dict:
    task = await session.scalar(
        select(Task).where(Task.id == task_id, Task.enterprise_id == user.enterprise_id)
    )
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
    return {
        "task_id": task.id,
        "task_type": task.task_type,
        "status": task.status,
        "retry_count": task.retry_count,
        "result": task.result,
        "progress": public_event(task),
        "error": task.error,
    }


@router.get("/projects/{project_id}/tasks")
async def list_project_tasks(
    project_id: int,
    status_filter: str | None = None,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.PROJECT_EDIT)),
) -> dict:
    """按项目列出最近任务（Issue #2 #16：刷新后恢复活动任务/状态）。"""
    query = (
        select(Task)
        .where(
            Task.enterprise_id == user.enterprise_id,
            Task.project_id == project_id,
        )
        .order_by(Task.id.desc())
        .limit(50)
    )
    if status_filter:
        try:
            query = query.where(Task.status == int(status_filter))
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="status 必须是数字") from exc
    rows = (await session.scalars(query)).all()
    return {
        "items": [
            {
                "task_id": t.id,
                "task_type": t.task_type,
                "status": t.status,
                "idempotency_key": t.idempotency_key,
                "retry_count": t.retry_count,
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "progress": public_event(t),
            }
            for t in rows
        ]
    }


@router.post("/projects/{project_id}/tasks/{task_id}/interrupt")
async def interrupt_task(
    project_id: int,
    task_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.PROJECT_EDIT)),
) -> dict:
    task = await session.scalar(
        select(Task).where(
            Task.id == task_id,
            Task.enterprise_id == user.enterprise_id,
            Task.project_id == project_id,
        )
    )
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
    messages = task.payload.setdefault("messages", [])
    messages.append({"type": "interrupt", "note": "补充材料，重新规划"})
    task.generation += 1  # 陈旧写入拦截
    await session.commit()
    return {"task_id": task.id, "generation": task.generation}


@router.get("/tasks/{task_id}/stream")
async def task_stream(
    task_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.FILE_READ)),
) -> StreamingResponse:
    task = await session.scalar(
        select(Task).where(Task.id == task_id, Task.enterprise_id == user.enterprise_id)
    )
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")

    terminal_events = {3: "done", 5: "cancelled", 6: "failed"}

    async def generate():
        # 初始快照：支持刷新/断线重连后先拿到当前状态（#18）
        await session.refresh(task)
        snapshot = {"task_id": task.id, "status": task.status, "progress": public_event(task)}
        yield f"event: snapshot\ndata: {json.dumps(snapshot, ensure_ascii=False)}\n\n"
        if task.status in terminal_events:
            yield f"event: {terminal_events[task.status]}\ndata: {json.dumps({'task_id': task.id, 'status': task.status}, ensure_ascii=False)}\n\n"
            return

        last_event = snapshot["progress"]
        while True:
            await session.refresh(task)
            event = public_event(task)
            if event != last_event:
                yield f"event: progress\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                last_event = event
            if task.status in terminal_events:
                yield f"event: {terminal_events[task.status]}\ndata: {json.dumps({'task_id': task.id, 'status': task.status}, ensure_ascii=False)}\n\n"
                break
            await asyncio.sleep(1)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
