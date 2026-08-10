"""任务编排（4.4.3/4.4.6）：创建、领取、执行、状态机、白名单事件。"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import TaskStatus, TaskType
from app.models.task import Task

MAX_RETRIES = 3


async def create_task(
    session: AsyncSession,
    *,
    enterprise_id: int,
    project_id: int,
    task_type: str,
    payload: dict,
    idempotency_key: str,
    priority: int = 5,
) -> tuple[Task, bool]:
    """创建任务；重复 idempotency_key 返回已存在任务（created=False）。"""
    existing = await session.scalar(
        select(Task).where(
            Task.idempotency_key == idempotency_key,
            Task.enterprise_id == enterprise_id,
        )
    )
    if existing is not None:
        return existing, False
    task = Task(
        enterprise_id=enterprise_id,
        project_id=project_id,
        task_type=task_type,
        idempotency_key=idempotency_key,
        priority=priority,
        status=int(TaskStatus.QUEUED),
        payload=payload,
        generation=1,
    )
    session.add(task)
    await session.flush()
    return task, True


async def claim_next(session: AsyncSession) -> Task | None:
    """领取队首任务。PG 下 FOR UPDATE SKIP LOCKED；SQLite 退化为普通取首条。"""
    query = (
        select(Task)
        .where(Task.status == int(TaskStatus.QUEUED))
        .order_by(Task.priority.asc(), Task.id.asc())
        .limit(1)
    )
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True)
    return await session.scalar(query)


def public_event(task: Task) -> dict:
    """SSE 白名单事件（D-E）：只透传 phase/status/percent/当前工作/摘要/提示。"""
    progress = task.progress or {}
    allowed_keys = ("phase", "status", "percent", "current_work", "summary", "hint")
    event = {k: progress.get(k) for k in allowed_keys if k in progress}
    event.setdefault("phase", task.task_type)
    event.setdefault("status", TaskStatus(task.status).name.lower())
    event.setdefault("percent", 100 if task.status == int(TaskStatus.DONE) else 0)
    return event


async def run_task(session: AsyncSession, task: Task) -> Task:
    """执行单个任务（handler 由 HANDLERS 注册）。"""
    task.status = int(TaskStatus.RUNNING)
    task.progress = {"phase": task.task_type, "status": "running", "percent": 10, "current_work": f"开始执行 {task.task_type}"}
    await session.commit()

    handler = HANDLERS.get(task.task_type)
    try:
        if handler is None:
            raise NotImplementedError(f"任务类型未实现：{task.task_type}")
        await handler(session, task)
        task.status = int(TaskStatus.DONE)
        task.progress = {"phase": task.task_type, "status": "done", "percent": 100, "summary": "完成"}
        task.finished_at = datetime.now(timezone.utc)
    except Exception as exc:  # noqa: BLE001
        task.retry_count += 1
        if task.retry_count >= MAX_RETRIES:
            task.status = int(TaskStatus.FAILED_TERMINAL)
            task.error = {"message": str(exc)}
            task.finished_at = datetime.now(timezone.utc)
            task.progress = {"phase": task.task_type, "status": "failed", "percent": 100, "hint": "重试耗尽，请人工处理"}
        else:
            task.status = int(TaskStatus.QUEUED)
            task.progress = {"phase": task.task_type, "status": "retrying", "percent": 5, "hint": f"失败，重试 {task.retry_count}/{MAX_RETRIES}"}
    await session.commit()
    return task


async def run_next_task(session: AsyncSession) -> Task | None:
    task = await claim_next(session)
    if task is None:
        return None
    return await run_task(session, task)


async def _tender_parse_handler(session: AsyncSession, task: Task) -> None:
    """解析项目材料：把 payload.file_ids 逐文件解析为 doc_block。"""
    from app.services.file_service import reparse_file

    file_ids = task.payload.get("file_ids") or []
    if not file_ids:
        raise ValueError("payload.file_ids 为空")
    task.progress = {"phase": task.task_type, "status": "running", "percent": 30, "current_work": f"解析 {len(file_ids)} 个文件"}
    for file_id in file_ids:
        fobj = await reparse_file(session, int(file_id))
        if fobj.status != 3:
            raise ValueError(f"文件解析失败：{fobj.original_name}")
    task.result = {"parsed_file_ids": [int(i) for i in file_ids]}


HANDLERS: dict[str, object] = {
    TaskType.TENDER_PARSE: _tender_parse_handler,
}
