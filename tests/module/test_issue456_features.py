"""Issue #4/#5/#6 新增能力测试：确认/修正、evaluate provider、上传响应、知识检索、公告导入、摘要、错误 envelope 等。"""

from __future__ import annotations

import pytest

from app.services.tender_service import TenderImportError, _validate_url


def _register(client, email="feature@test.com", name="特性测试企业"):
    r = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "Abc12345", "enterprise_name": name},
    )
    assert r.status_code == 201
    data = r.json()
    return {"Authorization": f"Bearer {data['access_token']}"}


def _make_project(client, h, name="P", buyer=None):
    body = {"name": name}
    if buyer:
        body["buyer"] = buyer
    r = client.post("/api/v1/projects", json=body, headers=h)
    assert r.status_code == 201
    return r.json()["project_id"]


def _upload_txt(client, h, pid, target="project", role=None, content="资质要求：三级。供货方案：变压器。".encode()):
    data = {"target": target}
    if target == "project":
        data["project_id"] = str(pid)
    if role:
        data["document_role"] = role
    r = client.post(
        "/api/v1/files/upload",
        data=data,
        files=[("files", ("材料.txt", content, "text/plain"))],
        headers=h,
    )
    assert r.status_code == 200, r.text
    return r.json()["files"][0]


# ---------- #6 P0：Requirement 用户确认/修正 ----------


def test_requirement_confirm_and_conflict(client):
    h = _register(client)
    pid = _make_project(client, h)
    upsert = client.post(
        f"/api/v1/projects/{pid}/requirements/upsert",
        json={"requirements": [{"req_type": "qualification", "content": "三级", "coordinates": [{"file_id": 1}]}]},
        headers=h,
    )
    req_id = upsert.json()["created"][0]
    r = client.put(
        f"/api/v1/projects/{pid}/requirements/{req_id}/confirm",
        json={"expected_revision": 1, "confirmed": True},
        headers=h,
    )
    assert r.status_code == 200
    assert r.json()["confirm_status"] == "confirmed"
    # revision CAS 冲突
    conflict = client.put(
        f"/api/v1/projects/{pid}/requirements/{req_id}/confirm",
        json={"expected_revision": 99, "confirmed": False},
        headers=h,
    )
    assert conflict.status_code == 409


def test_requirement_correct_supersedes(client):
    h = _register(client)
    pid = _make_project(client, h)
    upsert = client.post(
        f"/api/v1/projects/{pid}/requirements/upsert",
        json={"requirements": [{"req_type": "qualification", "req_key": "qual", "content": "三级", "coordinates": [{"file_id": 1}]}]},
        headers=h,
    )
    req_id = upsert.json()["created"][0]
    r = client.put(
        f"/api/v1/projects/{pid}/requirements/{req_id}/correct",
        json={"expected_revision": 1, "content": "二级（用户修正）", "coordinates": [{"file_id": 1}]},
        headers=h,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["revision"] == 2
    assert body["supersedes"] == req_id
    assert body["confirm_status"] == "unconfirmed"
    reqs = client.get(f"/api/v1/requirements?project_id={pid}", headers=h).json()
    assert len(reqs) == 1
    assert reqs[0]["content"] == "二级（用户修正）"
    # 旧 revision 冲突
    conflict = client.put(
        f"/api/v1/projects/{pid}/requirements/{reqs[0]['req_id']}/correct",
        json={"expected_revision": 1, "content": "x", "coordinates": [{"file_id": 1}]},
        headers=h,
    )
    assert conflict.status_code == 409


# ---------- #6 P0：evaluate provider_id ----------


def test_evaluate_accepts_provider_id_and_rejects_cross_tenant(client):
    h_a = _register(client, email="eva@test.com", name="评审企业A")
    pid_a = _make_project(client, h_a)
    h_b = _register(client, email="evb@test.com", name="评审企业B")
    pid_b = _make_project(client, h_b)

    # B 先评审一次生成自己的内置 Provider
    r = client.post(f"/api/v1/projects/{pid_b}/evaluate", headers=h_b)
    assert r.status_code == 200
    providers = client.get("/api/v1/review-providers", headers=h_b).json()
    provider_b = next(p for p in providers if p["provider_code"] == "builtin_completeness")

    # A 用 B 的 provider_id → 404（跨租户失败关闭）
    r = client.post(f"/api/v1/projects/{pid_a}/evaluate", json={"provider_id": provider_b["provider_id"]}, headers=h_a)
    assert r.status_code == 404

    # 不存在的 id → 404；非法类型 → 422
    assert client.post(f"/api/v1/projects/{pid_a}/evaluate", json={"provider_id": 999999}, headers=h_a).status_code == 404
    assert client.post(f"/api/v1/projects/{pid_a}/evaluate", json={"provider_id": "x"}, headers=h_a).status_code == 422

    # 不传 body 仍然可用（向后兼容）
    r = client.post(f"/api/v1/projects/{pid_a}/evaluate", headers=h_a)
    assert r.status_code == 200
    assert r.json()["run_id"] > 0


# ---------- #6 P0：企业上传响应 file_id + asset_id + auto_ingest ----------


def test_enterprise_upload_returns_asset_id_and_auto_ingest(client):
    h = _register(client, email="asset@test.com")
    item = _upload_txt(client, h, 0, target="enterprise")
    assert item["file_id"] > 0
    assert item["asset_id"] > 0
    assert item["auto_ingest"] is True


def test_project_upload_document_role(client):
    h = _register(client, email="role@test.com")
    pid = _make_project(client, h)
    item = _upload_txt(client, h, pid, target="project", role="招标公告")
    assert item["document_role"] == "招标公告"


# ---------- #6 P0：ai-suggest 停用 recommended ----------


def test_ai_suggest_no_recommended_and_requires_basis(client, monkeypatch):
    import app.api.quotes as quotes_api

    h = _register(client, email="ais@test.com")
    pid = _make_project(client, h)
    monkeypatch.setattr(
        quotes_api,
        "calculate",
        lambda params, samples: {
            "suggested": 120.0,
            "engine_version": "test-1",
            "adjustments": {"region": 1.0},
            "samples": [],
        },
    )
    r = client.post(
        "/api/v1/quotes/calculate",
        json={"project_id": pid, "material_ref": "CABLE-YJV-3x95", "cost": 100},
        headers=h,
    )
    calc_id = r.json()["calc_id"]
    r = client.post("/api/v1/quotes/ai-suggest", json={"calc_id": calc_id}, headers=h)
    assert r.status_code == 200
    assert r.json()["unavailable"] is True
    r = client.post("/api/v1/quotes/ai-suggest", json={"calc_id": calc_id, "basis": "华东样本"}, headers=h)
    body = r.json()
    assert "recommended" not in body
    assert body["price_range"] == ["114.0", "126.0"]  # 金额字符串契约（Issue #6）


# ---------- #4：知识检索 ----------


def test_knowledge_search_hits_and_traceable(client, monkeypatch):
    from app.config import settings as cfg

    monkeypatch.setattr(cfg, "data_classification_confirmed", 1)
    monkeypatch.setattr(cfg, "cloud_llm_enabled", 1)
    monkeypatch.setattr(cfg, "minimax_api_key", "test-key")

    h = _register(client, email="kn@test.com", name="知识库企业")
    # 历史项目材料（含"供货方案"内容）
    pid_old = _make_project(client, h, name="历史变压器项目")
    item = _upload_txt(client, h, pid_old, content="变压器供货方案：生产、检验、包装、运输与交付流程。".encode())
    assert item["status"] == 3  # 已解析

    pid_new = _make_project(client, h, name="新项目")
    r = client.post(
        "/api/v1/knowledge/search",
        json={"query": "变压器 供货方案", "project_id": pid_new},
        headers=h,
    )
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) >= 1
    hit = items[0]
    assert hit["source_type"] == "project_material"
    assert hit["project_id"] == pid_old
    assert hit["file_name"] == "材料.txt"

    # 排除当前项目自身材料
    pid_self = _make_project(client, h, name="自引用项目")
    _upload_txt(client, h, pid_self, content="变压器供货方案（当前项目）。".encode())
    r2 = client.post(
        "/api/v1/knowledge/search",
        json={"query": "变压器 供货方案", "project_id": pid_self},
        headers=h,
    )
    own_ids = {i["project_id"] for i in r2.json()["items"]}
    assert pid_self not in own_ids


def test_knowledge_search_tenant_isolation(client):
    h_a = _register(client, email="ka@test.com", name="知识A")
    pid_a = _make_project(client, h_a)
    _upload_txt(client, h_a, pid_a, content="A企业独家方案内容甲乙丙丁戊己。".encode())

    h_b = _register(client, email="kb@test.com", name="知识B")
    _make_project(client, h_b)
    r = client.post(
        "/api/v1/knowledge/search",
        json={"query": "甲乙丙丁戊己", "project_id": None},
        headers=h_b,
    )
    assert r.status_code == 200
    assert r.json()["items"] == []


# ---------- #6 P0：招标公告 URL 导入 ----------


def test_tender_notice_import_happy_path(client, monkeypatch):
    from app.services import tender_service

    h = _register(client, email="tn@test.com")
    pid = _make_project(client, h)

    async def fake_fetch(url):
        return "招标公告正文内容".encode(), "notice.html", "text/html"

    monkeypatch.setattr(tender_service, "fetch_document", fake_fetch)
    r = client.post(
        f"/api/v1/projects/{pid}/tender-notices/import-url",
        json={"url": "https://example.com/notice.html"},
        headers=h,
    )
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == 2
    assert body["file_id"] > 0
    notices = client.get(f"/api/v1/projects/{pid}/tender-notices", headers=h).json()
    assert len(notices["items"]) == 1


def test_tender_notice_import_blocked_fail_closed(client, monkeypatch):
    from app.services import tender_service

    h = _register(client, email="tn2@test.com")
    pid = _make_project(client, h)

    async def fake_fetch(url):
        raise TenderImportError("blocked_address", "内网地址已拒绝")

    monkeypatch.setattr(tender_service, "fetch_document", fake_fetch)
    r = client.post(
        f"/api/v1/projects/{pid}/tender-notices/import-url",
        json={"url": "http://127.0.0.1/notice"},
        headers=h,
    )
    assert r.status_code == 201
    assert r.json()["status"] == 3
    assert r.json()["error_code"] == "blocked_address"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x",
        "http://10.0.0.5/x",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/x",
        "file:///etc/passwd",
        "ftp://example.com/x",
    ],
)
def test_tender_url_validation_blocks(url):
    with pytest.raises(TenderImportError):
        _validate_url(url)


# ---------- #6 P1：项目摘要与搜索 ----------


def test_project_list_summary_and_q_search(client):
    h = _register(client, email="sum@test.com")
    pid = _make_project(client, h, name="电缆项目", buyer="国家电网")
    _upload_txt(client, h, pid)
    r = client.get("/api/v1/projects?q=国家电网", headers=h)
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["buyer"] == "国家电网"
    assert items[0]["summary"]["material_count"] == 1
    assert client.get("/api/v1/projects?q=不存在关键词", headers=h).json()["total"] == 0


# ---------- #6 P2：错误 envelope ----------


def test_error_envelope_has_code_and_request_id(client):
    h = _register(client, email="err@test.com")
    r = client.get("/api/v1/projects/999999", headers=h)
    assert r.status_code == 404
    body = r.json()
    assert body["detail"]
    assert body["code"] == "not_found"
    assert body["request_id"]


# ---------- #6 P0：auth/me 企业名 ----------


def test_auth_me_returns_enterprise_name(client):
    h = _register(client, email="me@test.com", name="实名企业")
    r = client.get("/api/v1/auth/me", headers=h)
    assert r.status_code == 200
    assert r.json()["enterprise_name"] == "实名企业"
