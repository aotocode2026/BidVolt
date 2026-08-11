"""A-2 API \u96c6\u6210\uff1a\u4efb\u52a1\u7b7e\u53d1 capability token\uff0cMCP \u8c03\u7528\u6309\u5de5\u5177/\u79df\u6237\u5f3a\u5236\u6821\u9a8c\u3002"""

from __future__ import annotations


def _register(client, email="cap@test.com", ent="\u80fd\u529b\u4f01\u4e1a"):
    r = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "Abc12345", "enterprise_name": ent},
    )
    assert r.status_code == 201
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _setup(client):
    h = _register(client)
    pid = client.post("/api/v1/projects", json={"name": "P"}, headers=h).json()["project_id"]
    task = client.post(
        f"/api/v1/projects/{pid}/tasks",
        json={"task_type": "tender_parse", "payload": {}, "idempotency_key": "cap-1"},
        headers=h,
    ).json()
    return h, pid, task


def test_task_response_contains_capability_token(client):
    _, _, task = _setup(client)
    assert task["capability_token"].startswith("bidvolt-cap.v1.")


def test_mcp_call_with_allowed_tool_works(client):
    h, pid, task = _setup(client)
    cap = task["capability_token"]
    r = client.get(
        "/api/v1/files/projects/%d/materials" % pid,
        headers={**h, "X-Bidvolt-Cap": cap},
    )
    assert r.status_code == 200


def test_mcp_call_with_disallowed_tool_rejected(client):
    h, pid, task = _setup(client)
    cap = task["capability_token"]
    # tender_parse \u767d\u540d\u5355\u4e0d\u542b search_web
    r = client.post(
        "/api/v1/searches",
        json={"query": "x"},
        headers={**h, "X-Bidvolt-Cap": cap},
    )
    assert r.status_code == 403
    assert "\u65e0\u6743\u8c03\u7528\u5de5\u5177" in r.json()["detail"]


def test_other_enterprise_task_token_cannot_read_our_project(client):
    # \u4f01\u4e1a A \u5efa\u9879\u76ee + \u4e0a\u4f20\u6750\u6599
    h, pid, _ = _setup(client)
    r_up = client.post(
        "/api/v1/files/upload",
        data={"target": "project", "project_id": str(pid)},
        files=[("files", ("tender.txt", b"secret-a-material", "text/plain"))],
        headers=h,
    )
    assert r_up.status_code == 200
    # \u4f01\u4e1a B \u7684\u4efb\u52a1 token
    h2 = _register(client, "cap2@test.com", "ent-b")
    pid2 = client.post("/api/v1/projects", json={"name": "P2"}, headers=h2).json()["project_id"]
    task2 = client.post(
        f"/api/v1/projects/{pid2}/tasks",
        json={"task_type": "tender_parse", "payload": {}, "idempotency_key": "cap-2"},
        headers=h2,
    ).json()
    # B \u7684 token \u8bfb A \u7684\u9879\u76ee\uff1a\u6309 token \u79df\u6237(2) \u8fc7\u6ee4 \u2192 \u7a7a\u5217\u8868\uff0c\u4e0d\u6cc4\u6f0f A \u6570\u636e
    r = client.get(
        "/api/v1/files/projects/%d/materials" % pid,
        headers={"X-Bidvolt-Cap": task2["capability_token"]},
    )
    assert r.status_code == 200
    assert r.json() == []
    # B \u7684 token \u8bfb\u81ea\u5df1\u7684\u9879\u76ee\uff1a\u6b63\u5e38
    r2 = client.get(
        "/api/v1/files/projects/%d/materials" % pid2,
        headers={"X-Bidvolt-Cap": task2["capability_token"]},
    )
    assert r2.status_code == 200


def test_user_jwt_without_cap_still_works(client):
    h, pid, _ = _setup(client)
    r = client.get(
        "/api/v1/files/projects/%d/materials" % pid,
        headers=h,
    )
    assert r.status_code == 200


def test_capability_tampered_rejected(client):
    _, _, task = _setup(client)
    r = client.get(
        "/api/v1/files/projects/1/materials",
        headers={"X-Bidvolt-Cap": task["capability_token"] + "x"},
    )
    assert r.status_code in (401, 403)
