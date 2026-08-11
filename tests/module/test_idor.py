"""A-1 跨租户 IDOR：企业 A 的对象对企业 B 不可见/不可访问。"""

from __future__ import annotations

import io


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
        files=[("files", ("材料.txt", "秘密内容".encode("utf-8"), "text/plain"))],
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
