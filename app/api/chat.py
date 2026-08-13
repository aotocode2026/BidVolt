"""项目助手会话（Issue #2 #47-#49）：会话列表/创建、消息历史、发送消息（LLM 回答入库）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import UserContext, require_permission
from app.constants import Permission
from app.db import get_session
from app.models.chat import Conversation, ConversationMessage

router = APIRouter(tags=["chat"])

RULE_REPLY = "云模型门禁关闭。当前可执行任务：招标解析、资料匹配、标书生成、模拟评标、针对性修改。"
MAX_CONTEXT = 10


async def _owned_conversation(
    session: AsyncSession,
    enterprise_id: int,
    project_id: int,
    conversation_id: int,
) -> Conversation:
    row = await session.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.enterprise_id == enterprise_id,
            Conversation.project_id == project_id,
        )
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")
    return row


@router.get("/projects/{project_id}/conversations")
async def list_conversations(
    project_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.PROJECT_EDIT)),
) -> dict:
    rows = (
        await session.scalars(
            select(Conversation)
            .where(
                Conversation.enterprise_id == user.enterprise_id,
                Conversation.project_id == project_id,
            )
            .order_by(Conversation.id.desc())
            .limit(100)
        )
    ).all()
    return {
        "items": [
            {
                "conversation_id": c.id,
                "title": c.title,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in rows
        ]
    }


@router.post("/projects/{project_id}/conversations", status_code=status.HTTP_201_CREATED)
async def create_conversation(
    project_id: int,
    body: dict,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.PROJECT_EDIT)),
) -> dict:
    conv = Conversation(
        enterprise_id=user.enterprise_id,
        project_id=project_id,
        title=(body.get("title") or "项目助手").strip()[:200] or "项目助手",
        created_by=user.user_id,
    )
    session.add(conv)
    await session.commit()
    return {"conversation_id": conv.id, "title": conv.title}


@router.get("/projects/{project_id}/conversations/{conversation_id}/messages")
async def list_messages(
    project_id: int,
    conversation_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.PROJECT_EDIT)),
) -> dict:
    await _owned_conversation(session, user.enterprise_id, project_id, conversation_id)
    rows = (
        await session.scalars(
            select(ConversationMessage)
            .where(
                ConversationMessage.enterprise_id == user.enterprise_id,
                ConversationMessage.project_id == project_id,
                ConversationMessage.conversation_id == conversation_id,
            )
            .order_by(ConversationMessage.id.asc())
            .limit(500)
        )
    ).all()
    return {
        "items": [
            {
                "message_id": m.id,
                "role": m.role,
                "content": m.content,
                "source_task_id": m.source_task_id,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in rows
        ]
    }


@router.post(
    "/projects/{project_id}/conversations/{conversation_id}/messages",
    status_code=status.HTTP_201_CREATED,
)
async def send_message(
    project_id: int,
    conversation_id: int,
    body: dict,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.PROJECT_EDIT)),
) -> dict:
    await _owned_conversation(session, user.enterprise_id, project_id, conversation_id)
    message = (body.get("message") or "").strip()
    if not message:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="消息为空")

    user_msg = ConversationMessage(
        enterprise_id=user.enterprise_id,
        project_id=project_id,
        conversation_id=conversation_id,
        role="user",
        content=message,
    )
    session.add(user_msg)
    await session.flush()

    history = (
        await session.scalars(
            select(ConversationMessage)
            .where(
                ConversationMessage.enterprise_id == user.enterprise_id,
                ConversationMessage.project_id == project_id,
                ConversationMessage.conversation_id == conversation_id,
            )
            .order_by(ConversationMessage.id.desc())
            .limit(MAX_CONTEXT)
        )
    ).all()
    history = list(reversed(history))

    from app.services.llm import LLMClient, llm_enabled

    if llm_enabled():
        context = "\n".join(
            f"{'用户' if m.role == 'user' else '助手'}: {m.content}" for m in history[:-1]
        )
        user_text = f"{context}\n用户: {message}" if context else message
        reply = await LLMClient().chat("你是 BidVolt 投标助手，用简洁中文回答。", user_text)
        mode = "llm"
    else:
        reply = RULE_REPLY
        mode = "rule"

    assistant_msg = ConversationMessage(
        enterprise_id=user.enterprise_id,
        project_id=project_id,
        conversation_id=conversation_id,
        role="assistant",
        content=reply,
    )
    session.add(assistant_msg)
    await session.commit()
    return {
        "user_message_id": user_msg.id,
        "assistant_message_id": assistant_msg.id,
        "reply": reply,
        "mode": mode,
    }
