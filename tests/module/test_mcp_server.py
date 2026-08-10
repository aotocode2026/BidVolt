"""MCP server 契约测试：stdio JSON-RPC + mock 后端鉴权头。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


class _MockBackend(BaseHTTPRequestHandler):
    captured_auth: str | None = None
    assets_payload = {"items": [{"asset_id": 1, "name": "营业执照.txt"}], "total": 1}

    def do_GET(self):  # noqa: N802
        type(self).captured_auth = self.headers.get("Authorization")
        if self.path.startswith("/api/v1/enterprise/assets"):
            body = json.dumps(type(self).assets_payload).encode()
            self.send_response(200)
        elif self.path.startswith("/api/v1/files/123/blocks"):
            body = json.dumps({"items": [{"block_id": 1, "text": "招标要求"}], "total": 1}).encode()
            self.send_response(200)
        else:
            self.send_response(404)
            body = b"{}"
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # noqa: D102
        pass


def _run_mcp(server_port: int, token: str, requests: list[dict]) -> str:
    env = {
        **os.environ,
        "BIDVOLT_API_BASE": f"http://127.0.0.1:{server_port}",
        "BIDVOLT_INTERNAL_TOKEN": token,
    }
    payload = "\n".join(json.dumps(r, ensure_ascii=False) for r in requests) + "\n"
    proc = subprocess.run(
        [sys.executable, "-m", "bidvolt_mcp"],
        cwd=REPO_ROOT,
        env=env,
        input=payload,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_mcp_initialize_and_tools():
    with ThreadingHTTPServer(("127.0.0.1", 0), _MockBackend) as httpd:
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            out = _run_mcp(
                port,
                "secret-token",
                [
                    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                    {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "health", "arguments": {}}},
                    {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "nope", "arguments": {}}},
                ],
            )
        finally:
            httpd.shutdown()

    lines = [json.loads(l) for l in out.strip().splitlines()]
    by_id = {r["id"]: r for r in lines}
    assert by_id[1]["result"]["serverInfo"]["name"] == "bidvolt"
    names = [t["name"] for t in by_id[2]["result"]["tools"]]
    assert {"health", "search_assets", "get_project_material_blocks", "list_project_materials"} <= set(names)
    assert json.loads(by_id[3]["result"]["content"][0]["text"])["status"] == "ok"
    assert by_id[4]["error"]["code"] == -32602


def test_mcp_tools_call_backend_with_auth_header():
    with ThreadingHTTPServer(("127.0.0.1", 0), _MockBackend) as httpd:
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            out = _run_mcp(
                port,
                "my-internal-token",
                [
                    {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "search_assets", "arguments": {"query": "营业执照"}}},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "get_project_material_blocks", "arguments": {"file_id": 123}}},
                ],
            )
        finally:
            httpd.shutdown()

    assert _MockBackend.captured_auth == "Bearer my-internal-token"
    lines = [json.loads(l) for l in out.strip().splitlines()]
    assets = json.loads(lines[0]["result"]["content"][0]["text"])
    assert assets["total"] == 1
    blocks = json.loads(lines[1]["result"]["content"][0]["text"])
    assert blocks["items"][0]["text"] == "招标要求"
