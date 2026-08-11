"""由 bidvolt_mcp.tools.TOOL_DEFS 生成 OpenRPC + JSON Schema IDL（单一事实源）。"""

from __future__ import annotations

import json
from pathlib import Path

from bidvolt_mcp import tools


def build_openrpc() -> dict:
    methods = []
    for d in tools.TOOL_DEFS:
        methods.append(
            {
                "name": d["name"],
                "summary": d["description"],
                "params": [
                    {
                        "name": "args",
                        "schema": d["inputSchema"],
                        "required": True,
                    }
                ],
                "result": {
                    "name": "result",
                    "schema": {"type": ["object", "array", "string", "number", "boolean", "null"]},
                },
            }
        )
    return {
        "openrpc": "1.2.6",
        "info": {
            "title": "BidVolt MCP Tools",
            "version": "0.1.0",
            "description": "后端业务服务暴露给 Hermes 的能力接口（契约见 docs/hermes/bidvolt-mcp-tools.md）",
        },
        "servers": [],
        "methods": methods,
    }


def main() -> None:
    schema_dir = Path(__file__).resolve().parent / "schema"
    schema_dir.mkdir(exist_ok=True)
    target = schema_dir / "openrpc.json"
    target.write_text(
        json.dumps(build_openrpc(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"written {target} ({len(tools.TOOL_DEFS)} methods)")


if __name__ == "__main__":
    main()
