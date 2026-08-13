from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.enterprise_domain import EnterpriseAsset, EnterpriseFact

TEST_DB = "./.test_bidvolt.db"


def _seed_fact(enterprise_id: int) -> int:
    engine = create_async_engine(f"sqlite+aiosqlite:///{TEST_DB}")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def run() -> int:
        async with factory() as s:
            asset = EnterpriseAsset(
                id=99001,
                enterprise_id=enterprise_id,
                name="营业执照",
                asset_type="证照",
                status=2,
            )
            s.add(asset)
            await s.flush()
            fact = EnterpriseFact(
                enterprise_id=enterprise_id,
                asset_id=asset.id,
                fact_key="credit_code",
                fact_value={"value": "91110000MOCK"},
                confidence=0.6,
                status=1,
            )
            s.add(fact)
            await s.commit()
            return fact.id

    fact_id = asyncio.run(run())
    engine.sync_engine.dispose()
    return fact_id


def _setup(client):
    r = client.post(
        "/api/v1/auth/register",
        json={"email": "fact@test.com", "password": "Abc12345", "enterprise_name": "资料企业"},
    )
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    return headers, r.json()["enterprise_id"]


def test_fact_confirm_and_correct_with_revisions(client):
    h, eid = _setup(client)
    fid = _seed_fact(eid)

    facts = client.get("/api/v1/enterprise/assets/99001/facts", headers=h)
    assert facts.status_code == 200
    assert facts.json()["items"][0]["fact_id"] == fid

    rev0 = client.get(f"/api/v1/enterprise/facts/{fid}/revisions", headers=h)
    assert rev0.json()["items"] == []

    ok = client.put(f"/api/v1/enterprise/facts/{fid}", json={"confirmed": True}, headers=h)
    assert ok.status_code == 200
    assert ok.json()["status"] == 2
    assert ok.json()["revision_no"] == 1

    fix = client.put(
        f"/api/v1/enterprise/facts/{fid}",
        json={"fact_value": "91110000FIXED", "note": "人工纠正"},
        headers=h,
    )
    assert fix.status_code == 200
    assert fix.json()["status"] == 3
    assert fix.json()["fact_value"] == {"value": "91110000FIXED"}
    assert fix.json()["revision_no"] == 2

    revs = client.get(f"/api/v1/enterprise/facts/{fid}/revisions", headers=h).json()["items"]
    assert len(revs) == 2
    assert revs[0]["revision_no"] == 2
    assert revs[0]["fact_value"] == {"value": "91110000FIXED"}


def test_fact_cross_enterprise_denied(client):
    h, eid = _setup(client)
    fid = _seed_fact(eid)
    r2 = client.post(
        "/api/v1/auth/register",
        json={"email": "fact2@test.com", "password": "Abc12345", "enterprise_name": "其他"},
    )
    h2 = {"Authorization": f"Bearer {r2.json()['access_token']}"}
    assert client.get(f"/api/v1/enterprise/facts/{fid}/revisions", headers=h2).status_code == 404
    assert client.put(f"/api/v1/enterprise/facts/{fid}", json={"confirmed": True}, headers=h2).status_code == 404


def test_enterprise_ingest_queue(client):
    r = client.post(
        "/api/v1/auth/register",
        json={"email": "q@test.com", "password": "Abc12345", "enterprise_name": "队列企业"},
    )
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    up = client.post(
        "/api/v1/files/upload",
        data={"target": "enterprise"},
        files=[("files", ("营业执照.txt", "营业执照".encode(), "text/plain"))],
        headers=h,
    )
    assert up.status_code == 200
    assets = client.get("/api/v1/enterprise/assets", headers=h).json()
    assert assets
    ing = client.post(
        "/api/v1/enterprise/ingest",
        json={"asset_ids": [assets[0]["asset_id"]]},
        headers=h,
    )
    assert ing.status_code == 202
    lst = client.get("/api/v1/enterprise/ingest", headers=h)
    assert lst.status_code == 200
    assert any(i["ingest_id"] == ing.json()["ingest_id"] for i in lst.json()["items"])


def test_review_suggestion_override(client):
    r = client.post(
        "/api/v1/auth/register",
        json={"email": "sug@test.com", "password": "Abc12345", "enterprise_name": "评标企业"},
    )
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    pid = client.post("/api/v1/projects", json={"name": "P"}, headers=h).json()["project_id"]
    did = client.post(
        "/api/v1/deliverables",
        json={"project_id": pid, "deliverable_type": 1, "title": "商务标"},
        headers=h,
    ).json()["deliverable_id"]
    client.post(
        f"/api/v1/deliverables/{did}/versions",
        json={"content": {"nodes": [{"id": "n1", "text": "商务"}]}},
        headers=h,
    )
    ev = client.post(f"/api/v1/projects/{pid}/evaluate", json={}, headers=h)
    assert ev.status_code == 200
    score_id = ev.json()["score_id"]
    items = client.get(f"/api/v1/projects/{pid}/scores/{score_id}/items", headers=h).json()
    item = items[0]
    original = item["suggestion"]

    r2 = client.put(
        f"/api/v1/projects/{pid}/scores/{score_id}/items/{item['item_id']}/suggestion",
        json={"suggestion": "人工修改后的建议：补充报价说明"},
        headers=h,
    )
    assert r2.status_code == 200
    assert r2.json()["effective_suggestion"] == "人工修改后的建议：补充报价说明"

    items2 = client.get(f"/api/v1/projects/{pid}/scores/{score_id}/items", headers=h).json()
    updated = next(i for i in items2 if i["item_id"] == item["item_id"])
    assert updated["suggestion"] == original  # 原始建议保留
    assert updated["suggestion_override"] == "人工修改后的建议：补充报价说明"
    assert updated["effective_suggestion"] == "人工修改后的建议：补充报价说明"
