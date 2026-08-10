from __future__ import annotations


def _headers(client):
    r = client.post(
        "/api/v1/auth/register",
        json={"email": "b@test.com", "password": "Abc12345", "enterprise_name": "测试企业"},
    )
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_project_crud(client):
    h = _headers(client)
    create = client.post("/api/v1/projects", json={"name": "测试项目", "tender_no": "TG-2026-001"}, headers=h)
    assert create.status_code == 201
    pid = create.json()["project_id"]

    lst = client.get("/api/v1/projects", headers=h)
    assert lst.status_code == 200
    assert lst.json()["total"] == 1

    got = client.get(f"/api/v1/projects/{pid}", headers=h)
    assert got.status_code == 200
    assert got.json()["name"] == "测试项目"
    assert got.json()["status"] == 1

    upd = client.patch(f"/api/v1/projects/{pid}", json={"name": "改名后"}, headers=h)
    assert upd.status_code == 200
    assert upd.json()["name"] == "改名后"


def test_project_status_machine(client):
    h = _headers(client)
    pid = client.post("/api/v1/projects", json={"name": "P"}, headers=h).json()["project_id"]

    ok = client.put(f"/api/v1/projects/{pid}/status", json={"status": 2}, headers=h)
    assert ok.status_code == 200
    assert ok.json()["status"] == 2

    bad = client.put(f"/api/v1/projects/{pid}/status", json={"status": 4}, headers=h)
    assert bad.status_code == 409


def test_archive_makes_project_readonly(client):
    h = _headers(client)
    pid = client.post("/api/v1/projects", json={"name": "P"}, headers=h).json()["project_id"]
    assert client.post(f"/api/v1/projects/{pid}/archive", headers=h).status_code == 204
    assert client.patch(f"/api/v1/projects/{pid}", json={"name": "x"}, headers=h).status_code == 409
    assert client.get("/api/v1/projects", headers=h).json()["total"] == 0


def test_project_requires_auth(client):
    assert client.post("/api/v1/projects", json={"name": "x"}).status_code == 401
