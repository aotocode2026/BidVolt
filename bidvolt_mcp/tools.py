"""MCP 工具注册表与后端调用（契约见 docs/hermes/bidvolt-mcp-tools.md）。"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

import httpx

BIDVOLT_API_BASE = os.environ.get("BIDVOLT_API_BASE", "http://127.0.0.1:8123")
INTERNAL_TOKEN = os.environ.get("BIDVOLT_INTERNAL_TOKEN", "")
CAPABILITY_TOKEN = os.environ.get("BIDVOLT_CAPABILITY_TOKEN", "")


def _read_cap_file() -> str:
    """Hermes 不保证把父进程 env 透传给 MCP 子进程：capability token 经固定临时文件兜底传递
    （worker 单任务串行，写入安全；文件模式 0600）。"""
    cap_file = os.environ.get("BIDVOLT_CAP_FILE", "/tmp/bidvolt_cap_token")
    try:
        with open(cap_file, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


if not CAPABILITY_TOKEN:
    CAPABILITY_TOKEN = _read_cap_file()


def _headers() -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if INTERNAL_TOKEN:
        # 任务级授权上下文由后端校验；内部 token 仅用于传输层认证
        headers["Authorization"] = f"Bearer {INTERNAL_TOKEN}"
        headers["X-Bidvolt-Internal"] = INTERNAL_TOKEN
    if CAPABILITY_TOKEN:
        # 任务级 capability token：绑定 enterprise/project/task/工具白名单
        headers["X-Bidvolt-Cap"] = CAPABILITY_TOKEN
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


def _list_assets(args: dict) -> Any:
    return _get("/api/v1/enterprise/assets")


def _get_asset(args: dict) -> Any:
    return _get(f"/api/v1/enterprise/assets/{args['asset_id']}")


def _classify_enterprise_asset(args: dict) -> Any:
    with httpx.Client(base_url=BIDVOLT_API_BASE, timeout=30) as client:
        resp = client.post(
            f"/api/v1/enterprise/assets/{args['asset_id']}/classify",
            json={"task_id": args.get("task_id")},
            headers=_headers(),
        )
        resp.raise_for_status()
        return resp.json()


def _upsert_enterprise_facts(args: dict) -> Any:
    with httpx.Client(base_url=BIDVOLT_API_BASE, timeout=30) as client:
        resp = client.post(
            f"/api/v1/enterprise/assets/{args['asset_id']}/facts",
            json={
                "task_id": args.get("task_id"),
                "facts": args["facts"],
            },
            headers=_headers(),
        )
        resp.raise_for_status()
        return resp.json()


def _get_project_material_blocks(args: dict) -> Any:
    return _get(
        f"/api/v1/files/{args['file_id']}/blocks",
        {"page": args.get("page", 1), "size": args.get("size", 100)},
    )


def _list_project_materials(args: dict) -> Any:
    return _get(f"/api/v1/files/projects/{args['project_id']}/materials")


def _get_deliverable_content(args: dict) -> Any:
    return _get(f"/api/v1/deliverables/{args['deliverable_id']}/content")


def _create_deliverable(args: dict) -> Any:
    """创建成果记录（Hermes 生成前必须先建三份记录，再 save_deliverable 写版本）。
    capability 路径下 project_id 与任务绑定项目强校验（后端 403 拦截越权）。"""
    body = {
        "project_id": args["project_id"],
        "deliverable_type": args["deliverable_type"],
        "title": args["title"],
    }
    with httpx.Client(base_url=BIDVOLT_API_BASE, timeout=30) as client:
        resp = client.post("/api/v1/deliverables", json=body, headers=_headers())
        resp.raise_for_status()
        return resp.json()


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


def _calculate_quote(args: dict) -> Any:
    body = {
        "material_ref": args["material_ref"],
        "cost": args["cost"],
        "min_profit_rate": args.get("min_profit_rate", 0.05),
        "strategy": args.get("strategy"),
        "unit": args.get("unit", "元"),
    }
    with httpx.Client(base_url=BIDVOLT_API_BASE, timeout=30) as client:
        resp = client.post("/api/v1/quotes/calculate", json=body, headers=_headers())
        resp.raise_for_status()
        return resp.json()


def _get_history_price(args: dict) -> Any:
    params = {k: args[k] for k in ("category", "publisher", "price_mode", "scope", "limit") if args.get(k) is not None}
    return _get("/api/v1/quotes/history", params)


def _get_latest_score(args: dict) -> Any:
    return _get(f"/api/v1/projects/{args['project_id']}/scores")


def _get_review_items(args: dict) -> Any:
    return _get(f"/api/v1/projects/{args['project_id']}/scores/{args['score_id']}/items")


def _submit_score_items(args: dict) -> Any:
    with httpx.Client(base_url=BIDVOLT_API_BASE, timeout=30) as client:
        resp = client.post(
            f"/api/v1/projects/{args['project_id']}/evaluate",
            json=args.get("payload") or {},
            headers=_headers(),
        )
        resp.raise_for_status()
        return resp.json()


def _confirm_review_items(args: dict) -> Any:
    with httpx.Client(base_url=BIDVOLT_API_BASE, timeout=30) as client:
        resp = client.post(
            f"/api/v1/projects/{args['project_id']}/scores/{args['score_id']}/items/confirm",
            json={
                "item_ids": args.get("item_ids") or [],
                "action": args.get("action", "confirm"),
                "expected_version": args.get("expected_version"),
            },
            headers=_headers(),
        )
        resp.raise_for_status()
        return resp.json()


def _list_requirements(args: dict) -> Any:
    return _get("/api/v1/requirements", {"project_id": args["project_id"]})


def _get_requirement(args: dict) -> Any:
    return _get(f"/api/v1/requirements/{args['req_id']}")


def _upsert_requirements(args: dict) -> Any:
    body = {"requirements": args["requirements"]}
    with httpx.Client(base_url=BIDVOLT_API_BASE, timeout=30) as client:
        resp = client.post(
            f"/api/v1/projects/{args['project_id']}/requirements/upsert",
            json=body,
            headers=_headers(),
        )
        resp.raise_for_status()
        return resp.json()


def _save_material_match_results(args: dict) -> Any:
    body = {"results": args["results"]}
    with httpx.Client(base_url=BIDVOLT_API_BASE, timeout=30) as client:
        resp = client.post(
            f"/api/v1/projects/{args['project_id']}/material-matches",
            json=body,
            headers=_headers(),
        )
        resp.raise_for_status()
        return resp.json()


def _list_material_matches(args: dict) -> Any:
    return _get(f"/api/v1/projects/{args['project_id']}/material-matches")


def _search_web(args: dict) -> Any:
    with httpx.Client(base_url=BIDVOLT_API_BASE, timeout=30) as client:
        resp = client.post(
            "/api/v1/searches",
            json={"query": args["query"], "scope": args.get("scope")},
            headers=_headers(),
        )
        resp.raise_for_status()
        return resp.json()


def _search_web_minimax(args: dict) -> Any:
    with httpx.Client(base_url=BIDVOLT_API_BASE, timeout=90) as client:
        resp = client.post(
            "/api/v1/searches/minimax",
            json={"query": args["query"], "limit": args.get("limit", 10)},
            headers=_headers(),
        )
        resp.raise_for_status()
        return resp.json()


def _vision_analyze_minimax(args: dict) -> Any:
    with httpx.Client(base_url=BIDVOLT_API_BASE, timeout=180) as client:
        resp = client.post(
            "/api/v1/vision/minimax",
            json={
                "prompt": args["prompt"],
                "file_id": args.get("file_id"),
                "image_url": args.get("image_url"),
            },
            headers=_headers(),
        )
        resp.raise_for_status()
        return resp.json()


def _save_source(args: dict) -> Any:
    with httpx.Client(base_url=BIDVOLT_API_BASE, timeout=30) as client:
        resp = client.post("/api/v1/search-sources", json=args, headers=_headers())
        resp.raise_for_status()
        return resp.json()


def _search_knowledge(args: dict) -> Any:
    with httpx.Client(base_url=BIDVOLT_API_BASE, timeout=30) as client:
        resp = client.post(
            "/api/v1/knowledge/search",
            json={
                "query": args["query"],
                "project_id": args.get("project_id"),
                "top_k": args.get("top_k", 10),
                "include_assets": args.get("include_assets", True),
            },
            headers=_headers(),
        )
        resp.raise_for_status()
        return resp.json()


def _link_citation(args: dict) -> Any:
    with httpx.Client(base_url=BIDVOLT_API_BASE, timeout=30) as client:
        resp = client.post(
            f"/api/v1/deliverables/{args['deliverable_id']}/citations",
            json={
                "version_no": args["version_no"],
                "node_id": args.get("node_id"),
                "source_id": args["source_id"],
                "quote_text": args.get("quote_text"),
            },
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
        "name": "search_knowledge",
        "description": "检索本企业历史项目材料/企业资料/已确认事实（来源可追溯，默认排除当前项目自身材料），供生成/校核/评审引用",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "project_id": {"type": "integer"},
                "top_k": {"type": "integer"},
                "include_assets": {"type": "boolean"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "handler": _search_knowledge,
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
        "name": "list_assets",
        "description": "浏览企业资料库目录",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "handler": _list_assets,
    },
    {
        "name": "get_asset",
        "description": "读取单个企业资料详情（含关键字段提取结果与来源定位）",
        "inputSchema": {
            "type": "object",
            "properties": {"asset_id": {"type": "integer"}},
            "required": ["asset_id"],
            "additionalProperties": False,
        },
        "handler": _get_asset,
    },
    {
        "name": "classify_enterprise_asset",
        "description": "企业资料导入任务专属：识别资料类型、抽取结构化字段、建议归档目录",
        "inputSchema": {
            "type": "object",
            "properties": {
                "asset_id": {"type": "integer"},
                "task_id": {"type": "integer"},
            },
            "required": ["asset_id", "task_id"],
            "additionalProperties": False,
        },
        "handler": _classify_enterprise_asset,
    },
    {
        "name": "upsert_enterprise_facts",
        "description": "企业资料导入任务专属：写入/更新企业事实（结构化字段 + 证据引用）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "asset_id": {"type": "integer"},
                "task_id": {"type": "integer"},
                "facts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "fact_key": {"type": "string"},
                            "value": {},
                            "confidence": {"type": "number"},
                            "evidence_ref": {"type": "object"},
                        },
                        "required": ["fact_key"],
                    },
                },
            },
            "required": ["asset_id", "task_id", "facts"],
            "additionalProperties": False,
        },
        "handler": _upsert_enterprise_facts,
    },
    {
        "name": "get_project_material_blocks",
        "description": (
            "读取项目材料解析出的 doc_block 文本块（带坐标、分页）。"
            "注意：这是解析层索引，可能不完整——扫描件/图片可能没有文字层、表格/页眉页脚/图片内文字"
            "可能未被提取；block_count=0 时文件内容完全未被提取。"
            "任何引用、切片、填充都必须以逐块通读的原文为准；文字层缺失或存疑时用 vision 工具读原件图片核对，"
            "不得把解析索引当作全文。"
        ),
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
        "description": (
            "列出项目当前招标材料（含文件名、解析状态、可提取文本块数）。"
            "status=3 表示已解析但索引可能不完整；status=4 表示解析失败、只能读原件；"
            "block_count=0 表示无文字层（多为扫描件），必须用 vision 工具读原件图。"
            "解析索引仅用于定位，内容一律以原件为准。"
        ),
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
        "name": "create_deliverable",
        "description": "创建成果记录（生成前必须先创建三份记录；capability 路径下 project_id 与任务绑定项目强校验）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "integer"},
                "deliverable_type": {"type": "integer"},
                "title": {"type": "string"},
            },
            "required": ["project_id", "deliverable_type", "title"],
            "additionalProperties": False,
        },
        "handler": _create_deliverable,
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
    {
        "name": "calculate_quote",
        "description": "调用确定性 QuoteEngine 计算建议价（只建议不写入）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "material_ref": {"type": "string"},
                "cost": {"type": "number"},
                "min_profit_rate": {"type": "number"},
                "unit": {"type": "string", "description": "成本/样本口径单位（元 或 万元；行情库样本为万元，折扣率样本不参与金额测算）"},
                "strategy": {"type": "string", "enum": ["win", "balance", "profit"]},
            },
            "required": ["material_ref", "cost"],
            "additionalProperties": False,
        },
        "handler": _calculate_quote,
    },
    {
        "name": "get_history_price",
        "description": (
            "历史中标价行情库联合查询（公共共建库+本企业私有库，只读信号）："
            "返回逐条原始样本（发布单位/品类/包名/报价方式/限价/中标价/证据原文/官网链接/公告ID）"
            "与按报价方式分组的聚合 stats。聚合仅标注样本口径供快速参考，决策必须基于逐条原始样本；"
            "折扣率类与金额类样本分开看。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "分标/品类关键词（模糊匹配）"},
                "publisher": {"type": "string", "description": "发布单位关键词"},
                "price_mode": {"type": "string", "description": "报价方式（折扣率/固定总价/比例报价/系数报价/其他）"},
                "scope": {"type": "string", "description": "all（公共+私有，默认）/ public / private"},
                "limit": {"type": "integer", "description": "返回条数（默认 50）"},
            },
            "additionalProperties": False,
        },
        "handler": _get_history_price,
    },
    {
        "name": "get_latest_score",
        "description": "读取项目最新评分汇总",
        "inputSchema": {
            "type": "object",
            "properties": {"project_id": {"type": "integer"}},
            "required": ["project_id"],
            "additionalProperties": False,
        },
        "handler": _get_latest_score,
    },
    {
        "name": "get_review_items",
        "description": "读取逐条评审项（含分类/得分/建议/证据/状态）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "integer"},
                "score_id": {"type": "integer"},
            },
            "required": ["project_id", "score_id"],
            "additionalProperties": False,
        },
        "handler": _get_review_items,
    },
    {
        "name": "submit_score_items",
        "description": "提交模拟评标（snapshot + EvidenceRef 服务端校验）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "integer"},
                "payload": {"type": "object"},
            },
            "required": ["project_id"],
            "additionalProperties": False,
        },
        "handler": _submit_score_items,
    },
    {
        "name": "confirm_review_items",
        "description": "批量确认/拒绝评审项（expected_version CAS）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "integer"},
                "score_id": {"type": "integer"},
                "item_ids": {"type": "array", "items": {"type": "integer"}},
                "action": {"type": "string"},
                "expected_version": {"type": "integer"},
            },
            "required": ["project_id", "score_id", "item_ids"],
            "additionalProperties": False,
        },
        "handler": _confirm_review_items,
    },
    {
        "name": "list_requirements",
        "description": "列出项目当前生效的招标要求（含坐标与 revision）",
        "inputSchema": {
            "type": "object",
            "properties": {"project_id": {"type": "integer"}},
            "required": ["project_id"],
            "additionalProperties": False,
        },
        "handler": _list_requirements,
    },
    {
        "name": "get_requirement",
        "description": "读取单条招标要求详情",
        "inputSchema": {
            "type": "object",
            "properties": {"req_id": {"type": "integer"}},
            "required": ["req_id"],
            "additionalProperties": False,
        },
        "handler": _get_requirement,
    },
    {
        "name": "upsert_requirements",
        "description": "写入/更新招标要求（招标解析 Skill 产出；coordinates 必填）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "integer"},
                "requirements": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "req_type": {"type": "string"},
                            "content": {"type": "string"},
                            "structured": {"type": "object"},
                            "coordinates": {"type": "array"},
                            "confidence": {"type": "number"},
                        },
                        "required": ["req_type", "content"],
                    },
                },
            },
            "required": ["project_id", "requirements"],
            "additionalProperties": False,
        },
        "handler": _upsert_requirements,
    },
    {
        "name": "save_material_match_results",
        "description": "保存资料匹配结果（material_match Skill 产出，缺失项关联要求与评分项）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "integer"},
                "results": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "requirement_id": {"type": "integer"},
                            "asset_id": {"type": "integer"},
                            "matched": {"type": "integer", "enum": [1, 2, 3]},
                            "gap_desc": {"type": "string"},
                            "affected_score_item": {"type": "string"},
                            "impact_score": {"type": "number"},
                            "suggestion": {"type": "string"},
                        },
                        "required": ["matched"],
                    },
                },
            },
            "required": ["project_id", "results"],
            "additionalProperties": False,
        },
        "handler": _save_material_match_results,
    },
    {
        "name": "list_material_matches",
        "description": "列出项目资料匹配结果",
        "inputSchema": {
            "type": "object",
            "properties": {"project_id": {"type": "integer"}},
            "required": ["project_id"],
            "additionalProperties": False,
        },
        "handler": _list_material_matches,
    },
    {
        "name": "search_web",
        "description": "AnySearch 网络搜索（出网前经后端 DLP 脱敏 + 域名白名单；门禁关闭时拒绝）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "scope": {"type": "string", "description": "可选：检索意图说明（透传，不约束）"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "handler": _search_web,
    },
    {
        "name": "search_web_minimax",
        "description": "MiniMax 原生联网搜索（全领域开放：行业技术方案、商务标写作范例、企业公开信息、政策法规等都可以搜；"
                        "返回 title/link/snippet，重要来源用 save_source 入库、link_citation 绑定引用）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "handler": _search_web_minimax,
    },
    {
        "name": "vision_analyze_minimax",
        "description": "MiniMax 官方视觉理解（看图，与主模型无关）：材料里的印章印模、表格截图、流程图、证书扫描件等图片，"
                        "传项目材料 file_id（list_project_materials 里的 id）或 http(s)/data 形式的 image_url，"
                        "用 prompt 提问（如「提取图中所有文字」「描述这张流程图」），返回文本结论",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "file_id": {"type": "integer"},
                "image_url": {"type": "string"},
            },
            "required": ["prompt"],
            "additionalProperties": False,
        },
        "handler": _vision_analyze_minimax,
    },
    {
        "name": "save_source",
        "description": "将搜索结果入库并判定 trust_level",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "url": {"type": "string"},
                "title": {"type": "string"},
                "snippet": {"type": "string"},
                "project_id": {"type": "integer"},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
        "handler": _save_source,
    },
    {
        "name": "link_citation",
        "description": "记录成果节点对搜索来源的引用（绑定版本）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "deliverable_id": {"type": "integer"},
                "version_no": {"type": "integer"},
                "node_id": {"type": "string"},
                "source_id": {"type": "integer"},
                "quote_text": {"type": "string"},
            },
            "required": ["deliverable_id", "version_no", "source_id"],
            "additionalProperties": False,
        },
        "handler": _link_citation,
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


# 成文工具链（新方案）：主会话自主成文的机制工具——追加注册进同一注册表。
# 放在文件末尾避免与上方定义互相干扰（assembly_tools 只读 _get/_headers/_post 依赖）。
from bidvolt_mcp import assembly_tools as _assembly_tools  # noqa: E402

_assembly_tools.register_assembly_tools()
