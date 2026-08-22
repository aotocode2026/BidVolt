"""任务级授权上下文（P0-2，D-B）：短时 capability token + 工具白名单。

每个任务签发一次 capability token，绑定 enterprise/project/task/允许工具/有效期；
MCP 调用后端时携带该 token，后端校验签名、租户与工具白名单，禁止静态 Token 全权限。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from app.config import settings

CAPABILITY_TTL = 3600  # 秒

# 任务类型 → MCP 工具白名单（最小权限）
TASK_TOOL_WHITELIST: dict[str, set[str]] = {
    "enterprise_ingestion": {
        "search_assets",
        "list_assets",
        "get_asset",
        "classify_enterprise_asset",
        "upsert_enterprise_facts",
    },
    "tender_parse": {
        "get_project_material_blocks",
        "list_project_materials",
        "get_requirement",
        "list_requirements",
        "upsert_requirements",
    },
    "material_match": {
        "search_assets",
        "list_assets",
        "get_asset",
        "list_project_materials",
        "list_requirements",
        "save_material_match_results",
        "list_material_matches",
        "search_knowledge",
    },
    "bid_generate": {
        "search_assets",
        "list_assets",
        "get_asset",
        "get_project_material_blocks",
        "list_project_materials",
        "get_deliverable_content",
        "create_deliverable",
        "save_deliverable",
        "calculate_quote",
        "get_history_price",
        "get_requirement",
        "list_requirements",
        "search_knowledge",
    },
    "bid_review": {
        "get_deliverable_content",
        "list_project_materials",
        "get_requirement",
        "list_requirements",
        "search_knowledge",
    },
    # 新方案：主会话端到端（主 agent 经 delegate_task 起子任务，工具白名单取并集；
    # 子任务的工具收敛由 Hermes delegate_task 的 toolsets 参数控制）
    "agent_pipeline": {
        "search_assets",
        "list_assets",
        "get_asset",
        "get_project_material_blocks",
        "list_project_materials",
        "get_deliverable_content",
        "create_deliverable",
        "save_deliverable",
        "calculate_quote",
        "get_history_price",
        "get_requirement",
        "list_requirements",
        "upsert_requirements",
        "save_material_match_results",
        "list_material_matches",
        "search_knowledge",
        # 成文工具链（主会话自主成文：切片→填空→追加→校验→封存→打包）
        "resolve_template_draft",
        "get_template_outline",
        "slice_template_item",
        "fill_template_slice",
        "append_template_slice",
        "verify_template_slice",
        "seal_template_item",
        "build_quote_xlsx",
        "package_response_zip",
    },
    "mock_evaluate": {
        "get_deliverable_content",
        "list_project_materials",
        "get_requirement",
        "list_requirements",
        "get_latest_score",
        "get_review_items",
        "submit_score_items",
        "confirm_review_items",
    },
    "targeted_edit": {
        "get_deliverable_content",
        "save_deliverable",
        "list_requirements",
        "search_knowledge",
    },
    "chat": {
        "search_assets",
        "list_assets",
        "get_project_material_blocks",
        "list_project_materials",
        "get_deliverable_content",
        "get_requirement",
        "list_requirements",
        "search_web",
        "search_knowledge",
    },
}


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def _sign(payload_b64: str) -> str:
    return _b64url(
        hmac.new(
            settings.jwt_secret.encode("utf-8"),
            payload_b64.encode("ascii"),
            hashlib.sha256,
        ).digest()
    )


def issue_capability(
    *,
    enterprise_id: int,
    project_id: int,
    task_id: int,
    task_type: str,
    ttl: int = CAPABILITY_TTL,
) -> str:
    """签发任务级 capability token（短时，默认 1h）。"""
    payload = {
        "v": 1,
        "eid": enterprise_id,
        "pid": project_id,
        "tid": task_id,
        "tools": sorted(TASK_TOOL_WHITELIST.get(task_type, set())),
        "exp": int(time.time()) + ttl,
    }
    payload_b64 = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return f"bidvolt-cap.v1.{payload_b64}.{_sign(payload_b64)}"


class CapabilityError(Exception):
    """capability token 无效/越权。"""


def verify_capability(token: str, *, tool: str, enterprise_id: int | None = None) -> dict:
    """校验 token 签名、有效期、工具白名单与租户绑定。失败抛 CapabilityError。"""
    parts = token.split(".")
    if len(parts) != 4 or parts[0] != "bidvolt-cap" or parts[1] != "v1":
        raise CapabilityError("capability token 格式错误")
    payload_b64, sig = parts[2], parts[3]
    expected = _sign(payload_b64)
    if not hmac.compare_digest(expected, sig):
        raise CapabilityError("capability token 签名无效")
    try:
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise CapabilityError("capability token 载荷无效") from exc
    if int(payload.get("exp", 0)) < int(time.time()):
        raise CapabilityError("capability token 已过期")
    if enterprise_id is not None and int(payload.get("eid", 0)) != int(enterprise_id):
        raise CapabilityError("capability token 不属于当前企业")
    if tool not in set(payload.get("tools", [])):
        raise CapabilityError(f"当前任务无权调用工具：{tool}")
    return payload
