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
