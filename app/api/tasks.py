"""任务接口（4.4.3）：提交/查询/中断/SSE 白名单事件流。"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_permission, UserContext
from app.constants import Permission, TaskType
from app.db import get_session
from app.models.task import Task
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

    def generate():
        # V1：返回当前白名单快照；后续接入 worker 事件总线做实时推送
        yield f"event: progress\ndata: {json.dumps(public_event(task), ensure_ascii=False)}\n\n"
        yield f"event: done\ndata: {json.dumps({'task_id': task.id}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
