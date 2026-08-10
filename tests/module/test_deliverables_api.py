from __future__ import annotations


def _setup(client):
    r = client.post(
        "/api/v1/auth/register",
        json={"email": "d@test.com", "password": "Abc12345", "enterprise_name": "测试企业"},
    )
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    pid = client.post("/api/v1/projects", json={"name": "P"}, headers=headers).json()["project_id"]
    return headers, pid


def test_version_chain_and_cas(client):
    h, pid = _setup(client)
    d = client.post(
        "/api/v1/deliverables",
        json={"project_id": pid, "deliverable_type": 1, "title": "商务标"},
        headers=h,
    )
    assert d.status_code == 201
    did = d.json()["deliverable_id"]

    doc1 = {"nodes": [{"id": "n1", "type": "paragraph", "text": "原始"}]}
    v1 = client.post(
        f"/api/v1/deliverables/{did}/versions",
        json={"content": doc1, "version_type": 1},
        headers=h,
    )
    assert v1.status_code == 201
    assert v1.json()["version_no"] == 1
    assert v1.json()["milestone"] is True

    doc2 = {"nodes": [{"id": "n1", "type": "paragraph", "text": "修改后"}]}
    v2 = client.post(
        f"/api/v1/deliverables/{did}/versions",
        json={"content": doc2, "expected_version_no": 1},
        headers=h,
    )
    assert v2.json()["version_no"] == 2

    conflict = client.post(
        f"/api/v1/deliverables/{did}/versions",
        json={"content": doc2, "expected_version_no": 1},
        headers=h,
    )
    assert conflict.status_code == 409

    content = client.get(f"/api/v1/deliverables/{did}/content", headers=h)
    assert content.json()["version_no"] == 2
    assert content.json()["model"]["nodes"][0]["text"] == "修改后"


def test_restore_and_diff(client):
    h, pid = _setup(client)
    did = client.post(
        "/api/v1/deliverables",
        json={"project_id": pid, "deliverable_type": 2, "title": "技术标"},
        headers=h,
    ).json()["deliverable_id"]
    doc1 = {"nodes": [{"id": "n1", "text": "v1"}]}
    doc2 = {"nodes": [{"id": "n1", "text": "v2"}, {"id": "n2", "text": "新增"}]}
    client.post(f"/api/v1/deliverables/{did}/versions", json={"content": doc1}, headers=h)
    client.post(f"/api/v1/deliverables/{did}/versions", json={"content": doc2}, headers=h)

    diff = client.get(f"/api/v1/deliverables/{did}/diff?from_version=1&to_version=2", headers=h)
    assert diff.status_code == 200
    assert {o["op"] for o in diff.json()["operations"]} == {"replace", "insert"}

    restored = client.post(f"/api/v1/deliverables/{did}/restore/1", headers=h)
    assert restored.status_code == 201
    assert restored.json()["version_no"] == 3
    content = client.get(f"/api/v1/deliverables/{did}/content", headers=h)
    assert content.json()["model"]["nodes"][0]["text"] == "v1"


def test_ai_edit_create_and_apply(client):
    h, pid = _setup(client)
    did = client.post(
        "/api/v1/deliverables",
        json={"project_id": pid, "deliverable_type": 1, "title": "商务标"},
        headers=h,
    ).json()["deliverable_id"]
    doc = {"nodes": [{"id": "n1", "text": "旧文案"}]}
    client.post(f"/api/v1/deliverables/{did}/versions", json={"content": doc}, headers=h)

    edit = client.post(
        f"/api/v1/deliverables/{did}/ai-edit",
        json={"selection": {"type": "text", "refs": ["n1"]}, "instruction": "新文案"},
        headers=h,
    )
    assert edit.status_code == 200
    diff_id = edit.json()["diff_id"]
    assert edit.json()["operations"][0]["after"]["text"] == "新文案"

    applied = client.post(f"/api/v1/deliverables/{did}/ai-edit/{diff_id}/apply", headers=h)
    assert applied.status_code == 201
    content = client.get(f"/api/v1/deliverables/{did}/content", headers=h)
    assert content.json()["model"]["nodes"][0]["text"] == "新文案"

    replay = client.post(f"/api/v1/deliverables/{did}/ai-edit/{diff_id}/apply", headers=h)
    assert replay.status_code == 409  # 已应用


def test_ai_edit_base_version_conflict(client):
    h, pid = _setup(client)
    did = client.post(
        "/api/v1/deliverables",
        json={"project_id": pid, "deliverable_type": 1, "title": "商务标"},
        headers=h,
    ).json()["deliverable_id"]
    client.post(
        f"/api/v1/deliverables/{did}/versions",
        json={"content": {"nodes": [{"id": "n1", "text": "a"}]}},
        headers=h,
    )
    client.post(
        f"/api/v1/deliverables/{did}/versions",
        json={"content": {"nodes": [{"id": "n1", "text": "b"}]}},
        headers=h,
    )
    edit = client.post(
        f"/api/v1/deliverables/{did}/ai-edit",
        json={"selection": {"refs": ["n1"]}, "instruction": "x", "base_version_no": 1},
        headers=h,
    )
    assert edit.status_code == 409


def test_deliverable_requires_auth(client):
    assert client.get("/api/v1/deliverables?project_id=1").status_code == 401
