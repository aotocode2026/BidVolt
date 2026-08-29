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

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import UserContext, require_capability, require_permission
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
    # 模型可选（A/B）：model/provider 透传给主会话启动参数
    for key in ("model", "provider"):
        if body.get(key):
            payload[key] = str(body[key]).strip()
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
    # 任务前对话：项目已有 pre-chat 会话且未显式续跑时，把任务 prompt 注入该会话
    if not payload.get("resume_session_id") and project.pre_chat_session_id:
        payload["resume_session_id"] = project.pre_chat_session_id
        payload["pre_chat"] = True
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
    # 客户交互状态（主会话提问 / 提交前动作清单）：工具落库为主、旧文本块兜底，前端直接呈现
    customer: dict = {}
    try:
        from app.services.agent_pipeline import _customer_state

        customer = await _customer_state(session, task_id)
    except Exception:  # noqa: BLE001 扫描失败不影响状态查询
        customer = {}
    result = task.result or {}
    result = dict(result)
    if customer.get("action_list") and "action_list" not in result:
        result["action_list"] = customer["action_list"]
    if customer.get("asks") and "customer_asks" not in result:
        result["customer_asks"] = customer["asks"]
    return {
        "task_id": task.id,
        "task_type": task.task_type,
        "status": task.status,
        "progress": task.progress,
        "result": result,
        "error": task.error,
        "customer": customer,
    }


@router.get("/{project_id}/agent-run/{task_id}/questions")
async def agent_run_questions(
    project_id: int,
    task_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.FILE_READ)),
) -> dict:
    """主会话向客户提的问题（ask_customer 工具）与提交前客户动作清单（report_customer_actions 工具）。
    前端渲染问卡；客户答复走 POST /asks/{ask_id}/answer 回到主会话。"""
    task = await _get_agent_task(session, user, project_id, task_id)
    from app.services.agent_pipeline import _customer_state

    return await _customer_state(session, task_id)


@router.post("/{project_id}/agent-run/{task_id}/asks", status_code=status.HTTP_201_CREATED)
async def agent_run_ask(
    project_id: int,
    task_id: int,
    body: dict,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_capability("ask_customer")),
) -> dict:
    """ask_customer / report_customer_actions 工具：主会话向客户提问或上报提交前动作清单。
    kind=question：items=[{q, need, checked}]（也兼容纯字符串）；kind=action：items=[str]。
    MCP 调用 task_id 可传 0，服务端按 capability token 的 tid 解析。"""
    if task_id == 0:
        cap = getattr(request.state, "cap_payload", None)
        if cap is None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="task_id 必填")
        task_id = int(cap["tid"])
    task = await _get_agent_task(session, user, project_id, task_id)
    kind = str(body.get("kind") or "question").strip()
    raw_items = body.get("items") or body.get("questions") or []
    if kind == "question":
        items: list[dict] = []
        for it in raw_items:
            if isinstance(it, str) and it.strip():
                items.append({"q": it.strip(), "need": "", "checked": ""})
            elif isinstance(it, dict) and str(it.get("q") or "").strip():
                items.append(
                    {
                        "q": str(it.get("q")).strip(),
                        "need": str(it.get("need") or "").strip(),
                        "checked": str(it.get("checked") or "").strip(),
                    }
                )
        if not items:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="questions 不能为空")
        answered = 0
    elif kind == "action":
        items = [str(x).strip() for x in raw_items if str(x).strip()]
        if not items:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="actions 不能为空")
        answered = 1
    else:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="kind 只能是 question/action")
    from app.models.agent import AgentCustomerAsk
    from app.services.task_service import _set_rls_context  # noqa: PLC0415

    await _set_rls_context(session, task.enterprise_id)
    row = AgentCustomerAsk(
        enterprise_id=task.enterprise_id,
        project_id=task.project_id,
        task_id=task.id,
        kind=kind,
        items=items,
        answered=answered,
    )
    session.add(row)
    await session.commit()
    return {
        "ask_id": row.id,
        "kind": kind,
        "recorded": len(items),
        "message": (
            "提问已记录并在页面渲染问卡，客户回答后会自动回到本会话；"
            "动作清单已记录并呈现给客户。继续推进不依赖答案的工作。"
        ),
    }


@router.post("/{project_id}/agent-run/{task_id}/asks/{ask_id}/answer")
async def agent_run_ask_answer(
    project_id: int,
    task_id: int,
    ask_id: int,
    body: dict,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.PROJECT_EDIT)),
) -> dict:
    """客户回答主会话提问：落库 + 注入主会话（运行中排队注入，完成后直接对话）。"""
    task = await _get_agent_task(session, user, project_id, task_id)
    from app.models.agent import AgentCustomerAsk
    from app.services.agent_pipeline import chat_with_session, queue_chat_message
    from app.services.task_service import _set_rls_context  # noqa: PLC0415

    await _set_rls_context(session, task.enterprise_id)
    row = await session.scalar(
        select(AgentCustomerAsk).where(
            AgentCustomerAsk.id == ask_id,
            AgentCustomerAsk.enterprise_id == task.enterprise_id,
            AgentCustomerAsk.task_id == task_id,
        )
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="提问记录不存在")
    if row.kind != "question":
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="该记录是动作清单，不需要回答")
    answer = body.get("answer")
    if isinstance(answer, str):
        answer = [answer]
    if not isinstance(answer, list) or not any(str(x).strip() for x in answer):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="answer 不能为空")
    row.answer = [str(x).strip() for x in answer]
    row.answered = 1
    lines = []
    for i, it in enumerate(row.items or []):
        a = row.answer[i] if i < len(row.answer) else ""
        lines.append(f"{i + 1}. 问题：{it.get('q')} → 客户回答：{a}")
    inject = (
        "客户已回答主会话提问（ask_id=" + str(row.id) + "）：\n" + "\n".join(lines)
        + "\n请据此回填相关字段并重新核验；其余仍缺的信息继续按三级处置。"
    )
    if task.status in (1, 2):
        await queue_chat_message(session, task, inject)
        reply = None
    else:
        try:
            d = await chat_with_session(session, task, inject)
            reply = d.get("reply")
        except ValueError as exc:
            await queue_chat_message(session, task, inject)
            reply = None
            logger_ = __import__("logging").getLogger(__name__)
            logger_.warning("回答注入走排队通道（%s）", exc)
    await session.commit()
    return {"ask_id": row.id, "answered": True, "queued": reply is None, "reply": reply}


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
                            "action_list": r.get("action_list") or [],
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


@router.post("/{project_id}/pre-chat")
async def project_pre_chat(
    project_id: int,
    body: dict,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.PROJECT_EDIT)),
) -> dict:
    """任务前对话：项目尚无主会话任务时，客户先与 Hermes 聊天建立项目会话
    （session 存 project.pre_chat_session_id）；之后 agent-run 会把任务 prompt
    注入该会话，任务前的交代自动成为主会话上下文。"""
    if not settings.agent_pipeline_enabled:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Agent 主会话流程未启用")
    project = await session.scalar(
        select(Project).where(
            Project.id == project_id,
            Project.enterprise_id == user.enterprise_id,
        )
    )
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="项目不存在")
    message = str(body.get("message") or "").strip()
    if not message:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="message 不能为空")
    from app.services.agent_pipeline import pre_chat

    try:
        return await pre_chat(session, project, message)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


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
    from app.services.agent_pipeline import chat_with_session, queue_chat_message

    if task.status in (1, 2) and not (task.result or {}).get("session_id"):
        # 运行中：主会话尚未产出 session_id，但 runner 持有 PTY——把消息写入任务字段，
        # 由 runner 现有泵循环取出并经同一条 PTY 通道注入（与催办/复核提示同一机制）。
        # 客户中途发消息不再 409，而是排队待主会话当前轮结束后处理。
        await queue_chat_message(session, task, message)
        return {
            "queued": True,
            "reply": None,
            "session_id": None,
            "message": "已送达主会话队列：当前轮结束后主会话会读取并回复（回复见会话控制台）。",
        }
    try:
        return await chat_with_session(session, task, message)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
