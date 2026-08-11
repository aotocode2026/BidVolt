"""MCP IDL 一致性：openrpc.json 由 TOOL_DEFS 生成且保持最新。"""

from __future__ import annotations

import json
from pathlib import Path

from bidvolt_mcp.gen_schema import build_openrpc


def test_openrpc_idl_matches_tool_defs():
    schema_path = Path("bidvolt_mcp/schema/openrpc.json")
    assert schema_path.exists(), "IDL 文件缺失，请运行 python -m bidvolt_mcp.gen_schema"
    on_disk = json.loads(schema_path.read_text(encoding="utf-8"))
    generated = build_openrpc()
    assert on_disk == generated
    names = [m["name"] for m in on_disk["methods"]]
    for required in (
        "get_project_material_blocks",
        "upsert_requirements",
        "save_material_match_results",
        "save_deliverable",
        "link_citation",
    ):
        assert required in names, f"IDL 缺少 P0-3 要求工具：{required}"
