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
        elif self.path.startswith("/api/v1/deliverables/9/content"):
            body = json.dumps({"deliverable_id": 9, "version_no": 1, "model": {"nodes": []}}).encode()
            self.send_response(200)
        elif self.path.startswith("/api/v1/quotes/history"):
            body = json.dumps({"sample_count": 8, "samples": [], "readonly": True}).encode()
            self.send_response(200)
        elif self.path.startswith("/api/v1/requirements"):
            body = json.dumps([{"req_id": 1, "req_type": "qualification", "content": "三级资质"}]).encode()
            self.send_response(200)
        else:
            self.send_response(404)
            body = b"{}"
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        type(self).captured_auth = self.headers.get("Authorization")
        length = int(self.headers.get("Content-Length", 0))
        _ = self.rfile.read(length)
        if self.path.startswith("/api/v1/deliverables/9/versions"):
            body = json.dumps({"version_no": 2, "version_id": 20, "milestone": False}).encode()
            self.send_response(201)
        elif self.path.startswith("/api/v1/quotes/calculate"):
            body = json.dumps({"calc_id": 7, "result": {"suggested": 120.0, "sample_count": 8}}).encode()
            self.send_response(200)
        elif self.path.startswith("/api/v1/projects/5/requirements/upsert"):
            body = json.dumps({"created": [1], "count": 1}).encode()
            self.send_response(201)
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
    assert {
        "health",
        "search_assets",
        "get_project_material_blocks",
        "list_project_materials",
        "get_deliverable_content",
        "save_deliverable",
        "list_requirements",
        "get_requirement",
        "upsert_requirements",
    } <= set(names)
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


def test_mcp_deliverable_tools():
    with ThreadingHTTPServer(("127.0.0.1", 0), _MockBackend) as httpd:
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            out = _run_mcp(
                port,
                "deliverable-token",
                [
                    {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "get_deliverable_content", "arguments": {"deliverable_id": 9}}},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "save_deliverable", "arguments": {"deliverable_id": 9, "model": {"nodes": []}, "idempotency_key": "k", "expected_version_no": 1, "source_task_id": 5}}},
                ],
            )
        finally:
            httpd.shutdown()

    assert _MockBackend.captured_auth == "Bearer deliverable-token"
    lines = [json.loads(l) for l in out.strip().splitlines()]
    content = json.loads(lines[0]["result"]["content"][0]["text"])
    assert content["version_no"] == 1
    saved = json.loads(lines[1]["result"]["content"][0]["text"])
    assert saved["version_no"] == 2


def test_mcp_quote_tools():
    with ThreadingHTTPServer(("127.0.0.1", 0), _MockBackend) as httpd:
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            out = _run_mcp(
                port,
                "quote-token",
                [
                    {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "get_history_price", "arguments": {"material_ref": "CABLE"}}},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "calculate_quote", "arguments": {"material_ref": "CABLE", "cost": 100}}},
                ],
            )
        finally:
            httpd.shutdown()

    lines = [json.loads(l) for l in out.strip().splitlines()]
    history = json.loads(lines[0]["result"]["content"][0]["text"])
    assert history["readonly"] is True
    calc = json.loads(lines[1]["result"]["content"][0]["text"])
    assert calc["result"]["suggested"] == 120.0


def test_mcp_requirement_tools():
    with ThreadingHTTPServer(("127.0.0.1", 0), _MockBackend) as httpd:
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            out = _run_mcp(
                port,
                "req-token",
                [
                    {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "list_requirements", "arguments": {"project_id": 5}}},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "upsert_requirements", "arguments": {"project_id": 5, "requirements": [{"req_type": "qualification", "content": "三级资质", "coordinates": [{"file_id": 1}]}]}}},
                ],
            )
        finally:
            httpd.shutdown()

    assert _MockBackend.captured_auth == "Bearer req-token"
    lines = [json.loads(l) for l in out.strip().splitlines()]
    reqs = json.loads(lines[0]["result"]["content"][0]["text"])
    assert reqs[0]["req_type"] == "qualification"
    created = json.loads(lines[1]["result"]["content"][0]["text"])
    assert created["count"] == 1
