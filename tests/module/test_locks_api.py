from __future__ import annotations


def _headers(client):
    r = client.post(
        "/api/v1/auth/register",
        json={"email": "c@test.com", "password": "Abc12345", "enterprise_name": "测试企业"},
    )
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_edit_lock_lifecycle(client):
    h = _headers(client)
    pid = client.post("/api/v1/projects", json={"name": "P"}, headers=h).json()["project_id"]

    acq = client.post(f"/api/v1/projects/{pid}/edit-lock", headers=h)
    assert acq.status_code == 200
    assert acq.json()["lock_id"]

    again = client.post(f"/api/v1/projects/{pid}/edit-lock", headers=h)
    assert again.status_code == 409

    hb = client.put(f"/api/v1/projects/{pid}/edit-lock/heartbeat", headers=h)
    assert hb.status_code == 200

    assert client.delete(f"/api/v1/projects/{pid}/edit-lock", headers=h).status_code == 204
    assert client.post(f"/api/v1/projects/{pid}/edit-lock", headers=h).status_code == 200
