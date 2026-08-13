"""A-2 任务级授权上下文：capability token 签名/有效期/工具白名单/租户绑定。"""

from __future__ import annotations

import pytest

from app.services import capability
from app.services.capability import CapabilityError, issue_capability, verify_capability


def _issue(task_type="tender_parse", eid=1):
    return issue_capability(enterprise_id=eid, project_id=10, task_id=99, task_type=task_type)


def test_capability_roundtrip_and_tools():
    token = _issue()
    payload = verify_capability(token, tool="upsert_requirements")
    assert payload["eid"] == 1
    assert payload["pid"] == 10
    assert payload["tid"] == 99
    assert "upsert_requirements" in payload["tools"]


def test_capability_rejects_tool_outside_whitelist():
    token = _issue("tender_parse")
    with pytest.raises(CapabilityError, match="无权调用工具"):
        verify_capability(token, tool="save_deliverable")


def test_capability_rejects_other_enterprise():
    token = _issue(eid=1)
    with pytest.raises(CapabilityError, match="不属于当前企业"):
        verify_capability(token, tool="upsert_requirements", enterprise_id=2)


def test_capability_rejects_tampered_signature():
    token = _issue()
    with pytest.raises(CapabilityError, match="签名无效"):
        verify_capability(token[:-2] + "ab", tool="upsert_requirements")


def test_capability_expired():
    token = issue_capability(enterprise_id=1, project_id=1, task_id=1, task_type="tender_parse", ttl=-10)
    with pytest.raises(CapabilityError, match="已过期"):
        verify_capability(token, tool="upsert_requirements")


def test_enterprise_ingestion_only_enterprise_tools():
    token = _issue("enterprise_ingestion")
    verify_capability(token, tool="classify_enterprise_asset")
    verify_capability(token, tool="upsert_enterprise_facts")
    with pytest.raises(CapabilityError):
        verify_capability(token, tool="save_deliverable")


def test_whitelist_uses_real_tools():
    """白名单中的工具名必须真实存在于 MCP TOOL_DEFS，防止契约漂移。"""
    from bidvolt_mcp.tools import tool_schemas

    real = {t["name"] for t in tool_schemas()}
    for tools in capability.TASK_TOOL_WHITELIST.values():
        assert tools <= real, f"白名单含未实现工具：{tools - real}"
