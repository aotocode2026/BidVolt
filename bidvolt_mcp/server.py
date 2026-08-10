"""stdio JSON-RPC 服务循环。"""

from __future__ import annotations

import json
import sys

from bidvolt_mcp.tools import call_tool, tool_schemas

PROTOCOL_VERSION = "2024-11-05"


def _error(req_id, code: int, message: str) -> str:
    return json.dumps(
        {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}},
        ensure_ascii=False,
    )


def handle_line(line: str) -> str | None:
    try:
        req = json.loads(line)
    except json.JSONDecodeError:
        return None
    if req.get("jsonrpc") != "2.0" or "method" not in req:
        return _error(req.get("id"), -32600, "Invalid Request")

    method = req["method"]
    req_id = req.get("id")
    params = req.get("params") or {}

    if method == "initialize":
        return json.dumps(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "bidvolt", "version": "0.1.0"},
                },
            },
            ensure_ascii=False,
        )
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "tools/list":
        return json.dumps(
            {"jsonrpc": "2.0", "id": req_id, "result": {"tools": tool_schemas()}},
            ensure_ascii=False,
        )
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            result = call_tool(name, args)
            return json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]},
                },
                ensure_ascii=False,
            )
        except KeyError:
            return _error(req_id, -32602, f"未知工具：{name}")
        except Exception as exc:  # noqa: BLE001
            return _error(req_id, -32603, str(exc))
    return _error(req_id, -32601, f"方法不存在：{method}")


def run_stdio() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        response = handle_line(line)
        if response is not None:
            sys.stdout.write(response + "\n")
            sys.stdout.flush()
    return 0
