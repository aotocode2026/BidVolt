from __future__ import annotations


def _setup(client):
    r = client.post(
        "/api/v1/auth/register",
        json={"email": "snap@test.com", "password": "Abc12345", "enterprise_name": "快照企业"},
    )
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    pid = client.post("/api/v1/projects", json={"name": "P"}, headers=headers).json()["project_id"]
    did = client.post(
        "/api/v1/deliverables",
        json={"project_id": pid, "deliverable_type": 1, "title": "商务标"},
        headers=headers,
    ).json()["deliverable_id"]
    client.post(
        f"/api/v1/deliverables/{did}/versions",
        json={"content": {"nodes": [{"id": "n1", "type": "paragraph", "text": "商务响应"}]}},
        headers=headers,
    )
    return headers, pid, did


def test_snapshot_list_and_manifest(client):
    h, pid, did = _setup(client)
    ev = client.post(f"/api/v1/projects/{pid}/evaluate", json={}, headers=h)
    assert ev.status_code == 200
    sid = ev.json()["snapshot_id"]

    lst = client.get(f"/api/v1/projects/{pid}/snapshots", headers=h)
    assert lst.status_code == 200
    items = lst.json()["items"]
    assert any(i["snapshot_id"] == sid and i["snapshot_type"] == "review" for i in items)

    det = client.get(f"/api/v1/projects/{pid}/snapshots/{sid}", headers=h)
    assert det.status_code == 200
    body = det.json()
    assert body["snapshot_id"] == sid
    manifest = body["manifest"]
    assert manifest["project_id"] == pid
    assert any(dl["deliverable_id"] == did and dl["current_version_no"] == 1 for dl in manifest["deliverables"])
    assert "rules" in manifest and manifest["rules"]["ruleset"]


def test_snapshot_cross_enterprise_denied(client):
    h, pid, _ = _setup(client)
    ev = client.post(f"/api/v1/projects/{pid}/evaluate", json={}, headers=h)
    sid = ev.json()["snapshot_id"]
    r2 = client.post(
        "/api/v1/auth/register",
        json={"email": "other@test.com", "password": "Abc12345", "enterprise_name": "其他企业"},
    )
    h2 = {"Authorization": f"Bearer {r2.json()['access_token']}"}
    assert client.get(f"/api/v1/projects/{pid}/snapshots/{sid}", headers=h2).status_code in (403, 404)


def test_project_tasks_list(client):
    h, pid, _ = _setup(client)
    t = client.post(
        f"/api/v1/projects/{pid}/tasks",
        json={"task_type": "chat", "payload": {"message": "hi"}, "idempotency_key": "snap-t1"},
        headers=h,
    )
    assert t.status_code == 201
    task_id = t.json()["task_id"]

    lst = client.get(f"/api/v1/projects/{pid}/tasks", headers=h)
    assert lst.status_code == 200
    assert any(i["task_id"] == task_id for i in lst.json()["items"])

    queued = client.get(f"/api/v1/projects/{pid}/tasks?status_filter=1", headers=h)
    assert queued.status_code == 200
    assert all(i["status"] == 1 for i in queued.json()["items"])


def test_deliverable_version_download(client):
    h, _, did = _setup(client)
    r = client.get(f"/api/v1/deliverables/{did}/versions/1/download", headers=h)
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    assert "docx" in r.headers["content-disposition"]
    assert r.content[:2] == b"PK"  # docx = zip
