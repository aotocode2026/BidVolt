from __future__ import annotations


def _setup(client):
    r = client.post(
        "/api/v1/auth/register",
        json={"email": "editor@test.com", "password": "Abc12345", "enterprise_name": "编辑企业"},
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
        json={"content": {"nodes": [{"id": "n1", "type": "paragraph", "text": "原文"}]}},
        headers=headers,
    )
    return headers, did


def test_editor_session_lifecycle(client):
    h, did = _setup(client)
    created = client.post(f"/api/v1/deliverables/{did}/editor-sessions", json={}, headers=h)
    assert created.status_code == 201
    body = created.json()
    assert body["session_id"]
    assert body["lease_token"]
    assert body["base_version_no"] == 1
    assert body["content"]["nodes"][0]["text"] == "原文"

    # 同一成果已有进行中会话，重复创建 409
    dup = client.post(f"/api/v1/deliverables/{did}/editor-sessions", json={}, headers=h)
    assert dup.status_code == 409

    edited = {"nodes": [{"id": "n1", "type": "paragraph", "text": "编辑后的内容"}]}
    ck = client.put(
        f"/api/v1/deliverables/{did}/editor-sessions/{body['session_id']}/checkpoint",
        json={"lease_token": body["lease_token"], "content": edited},
        headers=h,
    )
    assert ck.status_code == 200 and ck.json()["checkpoint_saved"] is True

    got = client.get(
        f"/api/v1/deliverables/{did}/editor-sessions/{body['session_id']}", headers=h
    )
    assert got.status_code == 200
    assert got.json()["checkpoint"]["nodes"][0]["text"] == "编辑后的内容"

    done = client.post(
        f"/api/v1/deliverables/{did}/editor-sessions/{body['session_id']}/complete",
        json={
            "lease_token": body["lease_token"],
            "content": edited,
            "expected_version_no": 1,
        },
        headers=h,
    )
    assert done.status_code == 201
    assert done.json()["version_no"] == 2

    versions = client.get(f"/api/v1/deliverables/{did}/versions", headers=h).json()
    assert any(v["version_no"] == 2 for v in versions)
    v2 = client.get(f"/api/v1/deliverables/{did}/versions/2", headers=h).json()
    assert v2["model"]["nodes"][0]["text"] == "编辑后的内容"

    lst = client.get(f"/api/v1/deliverables/{did}/editor-sessions", headers=h).json()
    assert lst["items"][0]["status"] == 2
    assert lst["items"][0]["completed_version_no"] == 2


def test_editor_session_cas_conflict(client):
    h, did = _setup(client)
    body = client.post(f"/api/v1/deliverables/{did}/editor-sessions", json={}, headers=h).json()
    done = client.post(
        f"/api/v1/deliverables/{did}/editor-sessions/{body['session_id']}/complete",
        json={
            "lease_token": body["lease_token"],
            "content": {"nodes": [{"id": "n1", "type": "paragraph", "text": "x"}]},
            "expected_version_no": 99,
        },
        headers=h,
    )
    assert done.status_code == 409


def test_editor_session_lease_required(client):
    h, did = _setup(client)
    body = client.post(f"/api/v1/deliverables/{did}/editor-sessions", json={}, headers=h).json()
    bad = client.put(
        f"/api/v1/deliverables/{did}/editor-sessions/{body['session_id']}/checkpoint",
        json={"lease_token": "wrong", "content": {"nodes": []}},
        headers=h,
    )
    assert bad.status_code == 403
