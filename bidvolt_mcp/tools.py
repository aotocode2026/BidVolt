"""MCP 工具注册表与后端调用（契约见 docs/hermes/bidvolt-mcp-tools.md）。"""

from __future__ import annotations

import json
import os
from typing import Any, Callable

import httpx

BIDVOLT_API_BASE = os.environ.get("BIDVOLT_API_BASE", "http://127.0.0.1:8123")
INTERNAL_TOKEN = os.environ.get("BIDVOLT_INTERNAL_TOKEN", "")


def _headers() -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if INTERNAL_TOKEN:
        # 任务级授权上下文由后端校验；内部 token 仅用于传输层认证
        headers["Authorization"] = f"Bearer {INTERNAL_TOKEN}"
        headers["X-Bidvolt-Internal"] = INTERNAL_TOKEN
    return headers


def _get(path: str, params: dict | None = None) -> Any:
    with httpx.Client(base_url=BIDVOLT_API_BASE, timeout=30) as client:
        resp = client.get(path, params=params, headers=_headers())
        resp.raise_for_status()
        return resp.json()


def _health(_args: dict) -> dict:
    return {"service": "bidvolt-mcp", "status": "ok", "version": "0.1.0"}


def _search_assets(args: dict) -> Any:
    return _get(
        "/api/v1/enterprise/assets",
        {"query": args.get("query"), "page": args.get("page", 1), "size": args.get("size", 20)},
    )


def _get_project_material_blocks(args: dict) -> Any:
    return _get(
        f"/api/v1/files/{args['file_id']}/blocks",
        {"page": args.get("page", 1), "size": args.get("size", 100)},
    )


def _list_project_materials(args: dict) -> Any:
    return _get(f"/api/v1/files/projects/{args['project_id']}/materials")


def _get_deliverable_content(args: dict) -> Any:
    return _get(f"/api/v1/deliverables/{args['deliverable_id']}/content")


def _save_deliverable(args: dict) -> Any:
    body = {
        "content": args["model"],
        "expected_version_no": args.get("expected_version_no"),
        "idempotency_key": args["idempotency_key"],
        "source_task_id": args.get("source_task_id"),
        "version_type": 2,  # AI 生成
    }
    with httpx.Client(base_url=BIDVOLT_API_BASE, timeout=30) as client:
        resp = client.post(
            f"/api/v1/deliverables/{args['deliverable_id']}/versions",
            json=body,
            headers=_headers(),
        )
        resp.raise_for_status()
        return resp.json()


TOOL_DEFS: list[dict] = [
    {
        "name": "health",
        "description": "MCP server 健康检查",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "handler": _health,
    },
    {
        "name": "search_assets",
        "description": "按关键词/分类搜索企业资料（企业事实唯一来源）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "category": {"type": "string"},
                "page": {"type": "integer"},
                "size": {"type": "integer"},
            },
            "additionalProperties": False,
        },
        "handler": _search_assets,
    },
    {
        "name": "get_project_material_blocks",
        "description": "读取项目材料解析出的 doc_block 文本块（带坐标）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_id": {"type": "integer"},
                "page": {"type": "integer"},
                "size": {"type": "integer"},
            },
            "required": ["file_id"],
            "additionalProperties": False,
        },
        "handler": _get_project_material_blocks,
    },
    {
        "name": "list_project_materials",
        "description": "列出项目当前招标材料",
        "inputSchema": {
            "type": "object",
            "properties": {"project_id": {"type": "integer"}},
            "required": ["project_id"],
            "additionalProperties": False,
        },
        "handler": _list_project_materials,
    },
    {
        "name": "get_deliverable_content",
        "description": "读取成果指定/当前版本的结构化内容（DocModel/SheetModel）",
        "inputSchema": {
            "type": "object",
            "properties": {"deliverable_id": {"type": "integer"}},
            "required": ["deliverable_id"],
            "additionalProperties": False,
        },
        "handler": _get_deliverable_content,
    },
    {
        "name": "save_deliverable",
        "description": "保存成果新版本（expected_version_id CAS + idempotency_key + source_task_id）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "deliverable_id": {"type": "integer"},
                "model": {"type": "object"},
                "expected_version_no": {"type": "integer"},
                "idempotency_key": {"type": "string"},
                "source_task_id": {"type": "integer"},
            },
            "required": ["deliverable_id", "model", "idempotency_key"],
            "additionalProperties": False,
        },
        "handler": _save_deliverable,
    },
]

_HANDLERS: dict[str, Callable[[dict], Any]] = {
    d["name"]: d["handler"] for d in TOOL_DEFS
}


def call_tool(name: str, args: dict) -> Any:
    if name not in _HANDLERS:
        raise KeyError(name)
    return _HANDLERS[name](args)


def tool_schemas() -> list[dict]:
    return [{k: v for k, v in d.items() if k != "handler"} for d in TOOL_DEFS]
