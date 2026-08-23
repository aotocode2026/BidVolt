# -*- coding: utf-8 -*-
"""成文工具链 MCP 工具注册（新方案）：主会话自主成文的机制工具。

这些工具是"机制"：切片=底稿条目区间字节级复制、填空/追加=修订模式+批注、
校验=原文逐字⊂底稿、封存/打包=产物落库。写什么、按什么顺序、何时封存打包，
全部由主会话决定（skill：bidvolt-agent-pipeline 的成文阶段）。
"""
import httpx

from bidvolt_mcp.tools import BIDVOLT_API_BASE, _get, _headers


def _post(path: str, body: dict) -> dict:
    with httpx.Client(base_url=BIDVOLT_API_BASE, timeout=120) as client:
        resp = client.post(path, json=body, headers=_headers())
        resp.raise_for_status()
        return resp.json()


def _resolve_template_draft(args: dict) -> dict:
    return _get(f"/api/v1/projects/{args['project_id']}/assembly/drafts")


def _get_template_outline(args: dict) -> dict:
    return _get(f"/api/v1/projects/{args['project_id']}/assembly/outline")


def _slice_template_item(args: dict) -> dict:
    return _post(
        f"/api/v1/projects/{args['project_id']}/assembly/slices",
        {"file_id": args["file_id"], "req_id": args["req_id"]},
    )


def _fill_template_slice(args: dict) -> dict:
    return _post(
        f"/api/v1/projects/{args['project_id']}/assembly/slices/{args['slice_id']}/fill",
        {"fields": args.get("fields"), "fills": args.get("fills")},
    )


def _append_template_slice(args: dict) -> dict:
    return _post(
        f"/api/v1/projects/{args['project_id']}/assembly/slices/{args['slice_id']}/append",
        {"nodes": args.get("nodes"), "comment": args.get("comment")},
    )


def _verify_template_slice(args: dict) -> dict:
    return _get(f"/api/v1/projects/{args['project_id']}/assembly/slices/{args['slice_id']}/verify")


def _seal_template_item(args: dict) -> dict:
    return _post(
        f"/api/v1/projects/{args['project_id']}/assembly/slices/{args['slice_id']}/seal",
        {"dir": args.get("dir", "商务文件"), "filename": args.get("filename", "")},
    )


def _build_quote_xlsx(args: dict) -> dict:
    return _post(
        f"/api/v1/projects/{args['project_id']}/assembly/xlsx",
        {"sheets": args.get("sheets")},
    )


def _package_response_zip(args: dict) -> dict:
    return _post(
        f"/api/v1/projects/{args['project_id']}/assembly/package",
        {"artifact_ids": args.get("artifact_ids"), "draft_file_id": args.get("draft_file_id")},
    )


def _list_agent_artifacts(args: dict) -> dict:
    return _get(f"/api/v1/projects/{args['project_id']}/assembly/artifacts")


def _inspect_agent_artifact(args: dict) -> dict:
    return _get(f"/api/v1/projects/{args['project_id']}/assembly/artifacts/{args['artifact_id']}/inspect")


ASSEMBLY_TOOL_DEFS = [
    {
        "name": "resolve_template_draft",
        "description": (
            "成文工具：列出候选底稿 docx（按是否含《响应文件格式》章分级排序）与推荐 file_id。"
            "主会话据此选择底稿（slice_template_item 的 file_id 参数）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"project_id": {"type": "integer"}},
            "required": ["project_id"],
            "additionalProperties": False,
        },
        "handler": _resolve_template_draft,
    },
    {
        "name": "get_template_outline",
        "description": (
            "成文工具：返回《响应文件格式》模板清单（doc_template），按 价格/商务/技术 分组，"
            "每项带 req_id（作为 slice_template_item 的 item_ref）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"project_id": {"type": "integer"}},
            "required": ["project_id"],
            "additionalProperties": False,
        },
        "handler": _get_template_outline,
    },
    {
        "name": "slice_template_item",
        "description": (
            "成文工具（机制）：把底稿中该模板条目区间原文整段复制为独立文档切片（保留格式），"
            "返回 slice_id。未定位到时按解析清单内容重建并在返回 warn 中如实标注。"
            "同一切片后续 fill/append/verify/seal。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "integer"},
                "file_id": {"type": "integer", "description": "底稿 docx 的 file_id（见 resolve_template_draft）"},
                "req_id": {"type": "integer", "description": "模板条目 req_id（见 get_template_outline）"},
            },
            "required": ["project_id", "file_id", "req_id"],
            "additionalProperties": False,
        },
        "handler": _slice_template_item,
    },
    {
        "name": "fill_template_slice",
        "description": (
            "成文工具（机制）：对切片做修订模式填空+批注。先按 fields（buyer/project_name/supplier/tender_no）"
            "走标准填空规则（无资料的空位原位标注【待补充：标签】），再按 fills=[{find,value,comment}] 定向替换。"
            "所有改动均带修订标记与来源批注，模板原文保留为删除线。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "integer"},
                "slice_id": {"type": "string"},
                "fields": {
                    "type": "object",
                    "description": "可选：{buyer, project_name, supplier, tender_no}",
                },
                "fills": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "find": {"type": "string", "description": "模板原文片段"},
                            "value": {"type": "string"},
                            "comment": {"type": "string", "description": "批注：来源与依据"},
                        },
                        "required": ["find", "value"],
                    },
                },
            },
            "required": ["project_id", "slice_id"],
            "additionalProperties": False,
        },
        "handler": _fill_template_slice,
    },
    {
        "name": "append_template_slice",
        "description": (
            "成文工具（机制）：把撰写内容追加到切片（修订插入+批注）。"
            "nodes 支持 {type:heading|paragraph|table,text/rows} 或裸字符串。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "integer"},
                "slice_id": {"type": "string"},
                "nodes": {"type": "array"},
                "comment": {"type": "string"},
            },
            "required": ["project_id", "slice_id"],
            "additionalProperties": False,
        },
        "handler": _append_template_slice,
    },
    {
        "name": "verify_template_slice",
        "description": (
            "成文工具（机制）：切片忠实性校验——条目文件原文（含删除线、剔除插入）必须逐字包含于底稿原文，"
            "返回 {ok, issues, original_chars, inserted_chars, deleted_chars}。"
            "不通过时主会话应带报告回修（重新 fill/append 或重新 slice）后再 seal。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "integer"},
                "slice_id": {"type": "string"},
            },
            "required": ["project_id", "slice_id"],
            "additionalProperties": False,
        },
        "handler": _verify_template_slice,
    },
    {
        "name": "seal_template_item",
        "description": (
            "成文工具（机制）：把切片生成为条目 docx 并落产物库（先 verify 通过再 seal），"
            "返回 artifact_id（package_response_zip 用）。切片封存后即失效。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "integer"},
                "slice_id": {"type": "string"},
                "dir": {"type": "string", "description": "价格文件/商务文件/技术文件"},
                "filename": {"type": "string", "description": "包内文件名（如（一）响应函及报价汇总表.docx）"},
            },
            "required": ["project_id", "slice_id"],
            "additionalProperties": False,
        },
        "handler": _seal_template_item,
    },
    {
        "name": "build_quote_xlsx",
        "description": (
            "成文工具（机制）：按主会话给出的 sheets 生成报价单 xlsx 并落产物库，"
            "返回 artifact_id。rows 必须是【数组的数组】（每行是一个字符串/数字数组），"
            "金额未知处标【待补充】，禁止整表省略。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "integer"},
                "sheets": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "rows": {
                                "type": "array",
                                "items": {
                                    "type": "array",
                                    "items": {"type": ["string", "number", "null"]},
                                },
                            },
                        },
                        "required": ["name", "rows"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["project_id", "sheets"],
            "additionalProperties": False,
        },
        "handler": _build_quote_xlsx,
    },
    {
        "name": "list_agent_artifacts",
        "description": (
            "成文工具（产物自检）：列出本任务已封存的全部产物（条目 docx/报价单 xlsx/响应包 zip）"
            "的 artifact_id/kind/name/大小。验收子 agent 用它核对「导出产物」是否齐全。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"project_id": {"type": "integer"}},
            "required": ["project_id"],
            "additionalProperties": False,
        },
        "handler": _list_agent_artifacts,
    },
    {
        "name": "inspect_agent_artifact",
        "description": (
            "成文工具（产物自检）：预览单个封存产物的内容——docx 返回文本头尾/字数/【待补充】计数/修订计数；"
            "xlsx 返回各表行列数与前几行；zip 返回文件清单。验收子 agent 用它核对导出产物与成果模型是否一致"
            "（例如报价单 xlsx 是否有完整表格而不是单格占位）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "integer"},
                "artifact_id": {"type": "integer"},
            },
            "required": ["project_id", "artifact_id"],
            "additionalProperties": False,
        },
        "handler": _inspect_agent_artifact,
    },
    {
        "name": "package_response_zip",
        "description": (
            "成文工具（机制）：把已封存产物（item_docx/xlsx 的 artifact_ids）打包为响应文件包 zip，"
            "自动附 会话记录/主会话记录.md 与 manifest.json，返回 zip 的 artifact_id"
            "（客户下载端点 /response-package 将直接取它）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "integer"},
                "artifact_ids": {"type": "array", "items": {"type": "integer"}},
                "draft_file_id": {"type": "integer"},
            },
            "required": ["project_id"],
            "additionalProperties": False,
        },
        "handler": _package_response_zip,
    },
]


def register_assembly_tools() -> None:
    from bidvolt_mcp import tools as t

    for d in ASSEMBLY_TOOL_DEFS:
        if d["name"] not in t._HANDLERS:
            t.TOOL_DEFS.append(d)
            t._HANDLERS[d["name"]] = d["handler"]
