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


def test_project_list_with_multiple_projects_having_scores(client):
    """Issue #8 P0 现场：≥2 个项目各有评分记录时 GET /projects 曾 500
    （GROUP BY 聚合子查询被当标量：PostgreSQL "more than one row returned by a
    subquery used as an expression"）。回归：列表 200 且各项目取到自己最新一条评分。"""
    import asyncio

    from sqlalchemy import text

    from app.models.review import ScoreRecord

    h = _headers(client)
    ent_id = client.get("/api/v1/auth/me", headers=h).json()["enterprise_id"]
    pids = [
        client.post("/api/v1/projects", json={"name": f"评分项目{i}"}, headers=h).json()["project_id"]
        for i in (1, 2)
    ]

    async def _seed():
        # 与 conftest 使用同一测试库 URL：BIDVOLT_TEST_DATABASE_URL 指向 PG 时即真 PG 对等
        from tests.conftest import IS_PG, make_test_engine

        engine = make_test_engine()
        async with engine.begin() as conn:
            if IS_PG:
                await conn.execute(text(f"SELECT set_config('app.enterprise_id', '{ent_id}', true)"))
            for pid, base in zip(pids, (80, 90)):
                await conn.execute(
                    ScoreRecord.__table__.insert().values(
                        enterprise_id=ent_id, project_id=pid, total_score=base,
                        reject_count=0, missing_count=0,
                    )
                )
                await conn.execute(
                    ScoreRecord.__table__.insert().values(
                        enterprise_id=ent_id, project_id=pid, total_score=base + 5,
                        reject_count=0, missing_count=1,
                    )
                )
        await engine.dispose()

    asyncio.run(_seed())

    lst = client.get("/api/v1/projects", headers=h)
    assert lst.status_code == 200
    by_id = {p["project_id"]: p for p in lst.json()["items"]}
    assert by_id[pids[0]]["summary"]["latest_total_score"] == 85
    assert by_id[pids[1]]["summary"]["latest_total_score"] == 95
    assert by_id[pids[0]]["summary"]["missing_count"] == 1
    assert by_id[pids[1]]["summary"]["missing_count"] == 1
