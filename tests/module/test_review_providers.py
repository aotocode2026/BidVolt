"""A-9 ReviewProvider 契约测试：Document / Code / API 三类 Provider。"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.services.review_providers import ApiProvider, DocumentProvider


class _MockReviewServer(BaseHTTPRequestHandler):
    """API Provider Mock Server：返回固定评审项 + provider 版本。"""

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        assert self.headers.get("Authorization") == "Bearer test-key"
        payload = {
            "provider_version": "mock-1.0",
            "review_run_id": "mock-run-001",
            "items": [
                {
                    "category": "完整性",
                    "problem_description": "缺少技术方案",
                    "got": 0.0,
                    "full": 10.0,
                    "improvable": 10.0,
                    "risk_level": 2,
                    "suggestion": "请补充技术方案",
                    "action_type": "upload_material",
                    "ruleset_version": body.get("provider_version", "mock-1.0"),
                    "evidence": {"claim_id": "mock-api-1"},
                }
            ],
        }
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # noqa: D102
        pass


def test_document_provider_rules_engine():
    provider = DocumentProvider(
        rules=[
            {"type": "field_required", "field": "qualification", "full": 10.0, "claim_id": "doc-q"},
            {"type": "field_required", "field": "performance", "full": 10.0, "claim_id": "doc-p", "risk": 3},
        ]
    )
    result = provider.run({"fields": {"qualification": "三级资质"}})
    assert result.provider_code == "doc_rules"
    assert len(result.items) == 2
    by_claim = {i["evidence"]["claim_id"]: i for i in result.items}
    assert by_claim["doc-q"]["got"] == 10.0  # 已提供
    assert by_claim["doc-p"]["got"] == 0.0  # 缺失
    assert by_claim["doc-p"]["action_type"] == "upload_material"
    assert result.raw_hash  # 原始响应哈希


def test_api_provider_contract_with_mock_server():
    with ThreadingHTTPServer(("127.0.0.1", 0), _MockReviewServer) as httpd:
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            provider = ApiProvider(f"http://127.0.0.1:{port}", api_key="test-key", retries=0)
            result = provider.run({"project_snapshot_id": 1, "deliverable_versions": {1: 3}})
        finally:
            httpd.shutdown()
    assert result.provider_code == "api_external"
    assert result.review_run_id == "mock-run-001"
    assert result.provider_version == "mock-1.0"
    assert result.items[0]["problem_description"] == "缺少技术方案"
    assert result.raw_hash  # provider 原始响应哈希


def test_api_provider_retries_then_fails():
    import pytest

    provider = ApiProvider("http://127.0.0.1:1", api_key="k", timeout=0.2, retries=1)
    with pytest.raises(RuntimeError, match="API Provider 调用失败"):
        provider.run({})


def test_code_provider_builtin_contract(client):
    """Code Provider（内置）：evaluate 返回冻结快照 + provider_raw_hash + 逐条 items。"""
    r = client.post(
        "/api/v1/auth/register",
        json={"email": "prov@test.com", "password": "Abc12345", "enterprise_name": "评审企业"},
    )
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    pid = client.post("/api/v1/projects", json={"name": "P"}, headers=h).json()["project_id"]
    ev = client.post(f"/api/v1/projects/{pid}/evaluate", json={}, headers=h).json()
    assert ev["snapshot_id"]
    items = client.get(f"/api/v1/projects/{pid}/scores/{ev['score_id']}/items", headers=h).json()
    assert len(items) == 3
    assert all(i["status"] == 1 for i in items)  # pending_confirm
    providers = client.get("/api/v1/review-providers", headers=h).json()
    assert providers[0]["provider_type"] == "code"
    assert providers[0]["provider_version"]
