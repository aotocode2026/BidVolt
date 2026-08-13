"""成果与版本链服务（4.5/4.6）：CAS、幂等、内容去重、diff。"""

from __future__ import annotations

import copy
import hashlib
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.deliverable import (
    AIEditDiff,
    Deliverable,
    DeliverableContent,
    DeliverableVersion,
)


class VersionConflict(Exception):
    """expected_version_no 与当前版本不一致（409）。"""


def content_hash(content: dict) -> str:
    return hashlib.sha256(
        json.dumps(content, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


async def create_deliverable(
    session: AsyncSession,
    *,
    enterprise_id: int,
    project_id: int,
    deliverable_type: int,
    title: str,
) -> Deliverable:
    existing = await session.scalar(
        select(Deliverable).where(
            Deliverable.project_id == project_id,
            Deliverable.deliverable_type == deliverable_type,
        )
    )
    if existing is not None:
        return existing
    deliverable = Deliverable(
        enterprise_id=enterprise_id,
        project_id=project_id,
        deliverable_type=deliverable_type,
        title=title,
        current_version_no=0,
    )
    session.add(deliverable)
    await session.flush()
    return deliverable


async def _get_or_create_content(session: AsyncSession, content: dict) -> DeliverableContent:
    digest = content_hash(content)
    row = await session.scalar(
        select(DeliverableContent).where(DeliverableContent.content_hash == digest)
    )
    if row is None:
        row = DeliverableContent(content_hash=digest, content_json=content)
        session.add(row)
        await session.flush()
    return row


async def save_version(
    session: AsyncSession,
    deliverable: Deliverable,
    content: dict,
    *,
    version_type: int = 4,
    created_by: int | None = None,
    expected_version_no: int | None = None,
    idempotency_key: str | None = None,
    source_task_id: int | None = None,
    milestone: bool | None = None,
) -> DeliverableVersion:
    if expected_version_no is not None and deliverable.current_version_no != expected_version_no:
        raise VersionConflict(
            f"版本冲突：当前 {deliverable.current_version_no}，期望 {expected_version_no}"
        )
    if idempotency_key:
        existing = await session.scalar(
            select(DeliverableVersion).where(
                DeliverableVersion.deliverable_id == deliverable.id,
                DeliverableVersion.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            return existing

    content_row = await _get_or_create_content(session, content)
    version_no = deliverable.current_version_no + 1
    version = DeliverableVersion(
        enterprise_id=deliverable.enterprise_id,
        deliverable_id=deliverable.id,
        version_no=version_no,
        version_type=version_type,
        milestone=(
            milestone
            if milestone is not None
            else version_type in (1, 2, 3)  # 原始/AI生成/AI校核为里程碑
        ),
        content_id=content_row.id,
        created_by=created_by,
        source_task_id=source_task_id,
        idempotency_key=idempotency_key,
    )
    session.add(version)
    deliverable.current_version_no = version_no
    await session.flush()
    return version


async def get_version_content(
    session: AsyncSession, deliverable_id: int, version_no: int
) -> tuple[DeliverableVersion, dict]:
    version = await session.scalar(
        select(DeliverableVersion).where(
            DeliverableVersion.deliverable_id == deliverable_id,
            DeliverableVersion.version_no == version_no,
        )
    )
    if version is None:
        raise ValueError("版本不存在")
    content = await session.get(DeliverableContent, version.content_id)
    return version, content.content_json


async def restore_version(
    session: AsyncSession,
    deliverable: Deliverable,
    version_no: int,
    created_by: int | None,
) -> DeliverableVersion:
    _, content = await get_version_content(session, deliverable.id, version_no)
    return await save_version(
        session, deliverable, copy.deepcopy(content), version_type=4, created_by=created_by
    )


def compute_diff(prev: dict, curr: dict) -> list[dict]:
    """节点级 diff：replace / insert / remove。"""
    prev_nodes = {n.get("id"): n for n in prev.get("nodes", [])}
    curr_nodes = {n.get("id"): n for n in curr.get("nodes", [])}
    ops: list[dict] = []
    for node_id, node in curr_nodes.items():
        if node_id not in prev_nodes:
            ops.append({"op": "insert", "node": node})
        elif prev_nodes[node_id] != node:
            ops.append({"op": "replace", "node_id": node_id, "before": prev_nodes[node_id], "after": node})
    for node_id in prev_nodes:
        if node_id not in curr_nodes:
            ops.append({"op": "remove", "node_id": node_id})
    return ops


def apply_diff(content: dict, diff: dict) -> dict:
    nodes = {n.get("id"): n for n in content.get("nodes", [])}
    for op in diff.get("operations", []):
        if op["op"] == "replace":
            if op["node_id"] not in nodes:
                raise ValueError(f"节点不存在：{op['node_id']}")
            nodes[op["node_id"]] = op["after"]
        elif op["op"] == "remove":
            nodes.pop(op["node_id"], None)
        elif op["op"] == "insert":
            nodes[op["node"]["id"]] = op["node"]
    return {**content, "nodes": list(nodes.values())}


def create_edit_diff(content: dict, selection: dict, instruction: str) -> dict:
    """AI 针对性修改：占位实现（选区节点文本替换），待 Hermes 接入后替换。"""
    ops: list[dict] = []
    refs = selection.get("refs") or []
    for ref in refs:
        node = next((n for n in content.get("nodes", []) if n.get("id") == ref), None)
        if node is None:
            raise ValueError(f"节点不存在：{ref}")
        new_node = dict(node)
        new_node["text"] = instruction
        ops.append({"op": "replace", "node_id": ref, "before": node, "after": new_node})
    return {"operations": ops, "note": "AI 生成（占位实现，待 Hermes 接入）"}


async def generate_edit_diff(content: dict, selection: dict, instruction: str) -> dict:
    """AI 针对性修改：门禁内走真实 LLM 改写，门禁外走占位替换。"""
    from app.services.llm import LLMClient, llm_enabled

    refs = selection.get("refs") or []
    nodes = {n.get("id"): n for n in content.get("nodes", [])}
    if not llm_enabled():
        return create_edit_diff(content, selection, instruction)

    client = LLMClient()
    ops: list[dict] = []
    for ref in refs:
        node = nodes.get(ref)
        if node is None:
            raise ValueError(f"节点不存在：{ref}")
        user_prompt = f"原文：{node.get('text', '')}\n修改要求：{instruction}\n只输出修改后的文本，不要任何解释。"
        rewritten = (
            await client.chat("你是标书编辑助手，按指令改写选中文本，只返回改写结果。", user_prompt)
        ).strip()
        new_node = dict(node)
        new_node["text"] = rewritten
        ops.append({"op": "replace", "node_id": ref, "before": node, "after": new_node})
    return {"operations": ops, "note": "AI 生成（MiniMax）"}
