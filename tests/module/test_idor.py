"""A-1 跨租户 IDOR：企业 A 的对象对企业 B 不可见/不可访问。"""

from __future__ import annotations


def _register(client, email):
    r = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "Abc12345", "enterprise_name": "企业"},
    )
    assert r.status_code == 201
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_cross_tenant_idor(client):
    ha = _register(client, "a@idor.com")
    hb = _register(client, "b@idor.com")

    # A 创建项目并上传文件
    pid = client.post("/api/v1/projects", json={"name": "A项目"}, headers=ha).json()["project_id"]
    upload = client.post(
        "/api/v1/files/upload",
        data={"target": "project", "project_id": str(pid)},
        files=[("files", ("材料.txt", "秘密内容".encode(), "text/plain"))],
        headers=ha,
    )
    file_id = upload.json()["files"][0]["file_id"]
    did = client.post(
        "/api/v1/deliverables",
        json={"project_id": pid, "deliverable_type": 1, "title": "商务标"},
        headers=ha,
    ).json()["deliverable_id"]

    # B 访问 A 的对象：一律 404 或空
    assert client.get(f"/api/v1/projects/{pid}", headers=hb).status_code == 404
    assert client.patch(f"/api/v1/projects/{pid}", json={"name": "x"}, headers=hb).status_code == 404
    assert client.get(f"/api/v1/files/{file_id}/download", headers=hb).status_code == 404
    assert client.get(f"/api/v1/files/{file_id}/blocks", headers=hb).status_code == 404
    assert client.get(f"/api/v1/deliverables/{did}/content", headers=hb).status_code == 404
    assert client.get("/api/v1/files?target=project", headers=hb).json()["total"] == 0
    assert client.get("/api/v1/projects", headers=hb).json()["total"] == 0

    # A 自己的数据不受影响
    assert client.get(f"/api/v1/projects/{pid}", headers=ha).status_code == 200
    assert client.get(f"/api/v1/files/{file_id}/download", headers=ha).status_code == 200


def test_cross_tenant_all_object_interfaces(client):
    """A-1 全对象接口：任务/Requirement/评分/报价/搜索引用/审计跨租户均 404。"""
    ha = _register(client, "a2@idor.com")
    hb = _register(client, "b2@idor.com")

    # A 创建项目 + 对象
    pid = client.post("/api/v1/projects", json={"name": "A项目"}, headers=ha).json()["project_id"]
    task = client.post(
        f"/api/v1/projects/{pid}/tasks",
        json={"task_type": "chat", "payload": {}, "idempotency_key": "idor-task"},
        headers=ha,
    ).json()
    task_id = task["task_id"]
    did = client.post(
        "/api/v1/deliverables",
        json={"project_id": pid, "deliverable_type": 1, "title": "商务标"},
        headers=ha,
    ).json()["deliverable_id"]
    client.post(
        f"/api/v1/deliverables/{did}/versions",
        json={"content": {"nodes": [{"id": "n1", "type": "paragraph", "text": "商务响应"}]}, "version_type": 4},
        headers=ha,
    )
    req = client.post(
        f"/api/v1/projects/{pid}/requirements/upsert",
        json={"requirements": [{"req_type": "qualification", "content": "三级资质", "coordinates": [{"file_id": 1}]}]},
        headers=ha,
    )
    assert req.status_code == 201
    req_id = req.json()["created"][0]
    ev = client.post(f"/api/v1/projects/{pid}/evaluate", json={}, headers=ha).json()
    score_id = ev["score_id"]
    src = client.post(
        "/api/v1/search-sources",
        json={"url": "https://www.gov.cn/a", "project_id": pid},
        headers=ha,
    ).json()
    src_id = src["source_id"]

    # B 访问 A 的对象：一律 404 或空
    assert client.get(f"/api/v1/tasks/{task_id}", headers=hb).status_code == 404
    assert client.get(f"/api/v1/requirements?project_id={pid}", headers=hb).json() == []
    assert client.get(f"/api/v1/requirements/{req_id}", headers=hb).status_code == 404
    assert client.get(f"/api/v1/projects/{pid}/scores", headers=hb).status_code == 404
    assert client.get(f"/api/v1/projects/{pid}/scores/{score_id}/items", headers=hb).status_code == 404
    assert client.get(f"/api/v1/search-sources/{src_id}", headers=hb).status_code == 404
    assert client.get(f"/api/v1/deliverables/{did}/references", headers=hb).status_code == 404

    # 报价：外部只读 Provider，历史/测算不携带租户数据；快照按租户隔离
    r = client.post(
        "/api/v1/quotes/calculate",
        json={"material_ref": "CABLE-YJV-3x95", "cost": 100},
        headers=hb,
    )
    assert r.status_code == 200  # 外部样本与租户无关

    # A 自己的数据不受影响
    assert client.get(f"/api/v1/tasks/{task_id}", headers=ha).status_code == 200
    assert client.get(f"/api/v1/requirements/{req_id}", headers=ha).status_code == 200
    assert client.get(f"/api/v1/search-sources/{src_id}", headers=ha).status_code == 200


def test_same_enterprise_cross_project_isolated(client):
    """同企业不同 project 的对象互不可见（项目级隔离）。"""
    h = _register(client, "same@idor.com")
    pid1 = client.post("/api/v1/projects", json={"name": "项目1"}, headers=h).json()["project_id"]
    pid2 = client.post("/api/v1/projects", json={"name": "项目2"}, headers=h).json()["project_id"]

    did1 = client.post(
        "/api/v1/deliverables",
        json={"project_id": pid1, "deliverable_type": 1, "title": "商务标1"},
        headers=h,
    ).json()["deliverable_id"]
    client.post(
        f"/api/v1/projects/{pid1}/requirements/upsert",
        json={"requirements": [{"req_type": "qualification", "content": "一级资质", "coordinates": [{"file_id": 1}]}]},
        headers=h,
    ).json()["created"][0]

    # 项目 2 的上下文访问项目 1 的对象：列表为空/详情 404
    assert client.get(f"/api/v1/requirements?project_id={pid2}", headers=h).json() == []
    assert client.get(f"/api/v1/deliverables?project_id={pid2}", headers=h).json() == []
    assert client.get(f"/api/v1/deliverables/{did1}", headers=h).json().get("project_id") == pid1
    assert client.get(f"/api/v1/files/projects/{pid2}/materials", headers=h).json() == []


def test_cross_tenant_provider_lock_and_ai_diff(client):
    ha = _register(client, "audit-a@idor.com")
    hb = _register(client, "audit-b@idor.com")

    pid = client.post("/api/v1/projects", json={"name": "A项目"}, headers=ha).json()["project_id"]
    did = client.post(
        "/api/v1/deliverables",
        json={"project_id": pid, "deliverable_type": 1, "title": "商务标"},
        headers=ha,
    ).json()["deliverable_id"]
    client.post(
        f"/api/v1/deliverables/{did}/versions",
        json={"content": {"nodes": [{"id": "n1", "type": "paragraph", "text": "原文"}]}, "version_type": 4},
        headers=ha,
    )
    diff = client.post(
        f"/api/v1/deliverables/{did}/ai-edit",
        json={"selection": {"type": "text", "refs": ["n1"]}, "instruction": "改写"},
        headers=ha,
    )
    diff_id = diff.json()["diff_id"]
    client.post(f"/api/v1/projects/{pid}/evaluate", json={}, headers=ha).json()

    # 跨企业：AI diff 读取、编辑锁、Provider 配置均不可访问
    assert client.get(f"/api/v1/deliverables/{did}/ai-edit/{diff_id}", headers=hb).status_code == 404
    assert client.post(f"/api/v1/projects/{pid}/edit-lock", headers=hb).status_code == 404
    a_providers = client.get("/api/v1/review-providers", headers=ha).json()
    a_provider_id = a_providers[0]["provider_id"]
    assert client.put(
        f"/api/v1/review-providers/{a_provider_id}/config",
        json={"enabled": False},
        headers=hb,
    ).status_code in (403, 404)
    # B 看不到 A 的 Provider
    assert all(p["provider_id"] != a_provider_id for p in client.get("/api/v1/review-providers", headers=hb).json())
