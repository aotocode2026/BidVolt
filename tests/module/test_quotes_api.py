from __future__ import annotations

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.auth import AppUser

TEST_DB = "./.test_bidvolt.db"


def _setup(client):
    r = client.post(
        "/api/v1/auth/register",
        json={"email": "q@test.com", "password": "Abc12345", "enterprise_name": "测试企业"},
    )
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    pid = client.post("/api/v1/projects", json={"name": "P"}, headers=headers).json()["project_id"]
    did = client.post(
        "/api/v1/deliverables",
        json={"project_id": pid, "deliverable_type": 3, "title": "报价单"},
        headers=headers,
    ).json()["deliverable_id"]
    return headers, pid, did


def test_history_readonly_and_snapshot(client):
    h, _, _ = _setup(client)
    r = client.get("/api/v1/quotes/history", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["readonly"] is True
    assert body["sample_count"] >= 5
    assert body["snapshot_ids"]


def test_calculate_strategies_ai_suggest_and_apply(client):
    h, pid, did = _setup(client)
    calc = client.post(
        "/api/v1/quotes/calculate",
        json={
            "material_ref": "CABLE-YJV-3x95",
            "cost": 100,
            "min_profit_rate": 0.1,
            "project_id": pid,
            "adjustments": {"region": 0.01},
        },
        headers=h,
    )
    assert calc.status_code == 200
    calc_id = calc.json()["calc_id"]
    assert calc.json()["result"]["sample_count"] >= 5
    assert calc.json()["result"]["suggested"] >= calc.json()["result"]["min_price"]

    win = client.post("/api/v1/quotes/strategies", json={"calc_id": calc_id, "strategy": "win"}, headers=h)
    assert win.status_code == 200
    assert win.json()["strategy"] == "win"

    no_basis = client.post("/api/v1/quotes/ai-suggest", json={"calc_id": calc_id}, headers=h)
    assert no_basis.json()["unavailable"] is True

    with_basis = client.post(
        "/api/v1/quotes/ai-suggest", json={"calc_id": calc_id, "basis": "华东区电缆中标样本"}, headers=h
    )
    assert with_basis.json()["is_ai_suggest"] is True
    assert len(with_basis.json()["price_range"]) == 2

    applied = client.post(
        "/api/v1/quotes/apply",
        json={"calc_id": calc_id, "deliverable_id": did, "expected_version_no": 0, "note": "确认"},
        headers=h,
    )
    assert applied.status_code == 201
    assert applied.json()["new_version_no"] == 1

    again = client.post(
        "/api/v1/quotes/apply",
        json={"calc_id": calc_id, "deliverable_id": did, "expected_version_no": 1},
        headers=h,
    )
    assert again.status_code == 409

    versions = client.get(f"/api/v1/deliverables/{did}/versions", headers=h)
    assert versions.json()[0]["version_type"] == 5  # 报价应用


def test_apply_requires_quote_apply_permission(client):
    h, pid, did = _setup(client)
    calc = client.post(
        "/api/v1/quotes/calculate",
        json={"material_ref": "CABLE-YJV-3x95", "cost": 100, "min_profit_rate": 0.1, "project_id": pid},
        headers=h,
    ).json()["calc_id"]

    # 直接改库：把用户权限降级为只读（不包含 quote.apply）
    engine = create_async_engine(f"sqlite+aiosqlite:///{TEST_DB}")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def strip_permission():
        async with factory() as session:
            user = await session.scalar(select(AppUser).where(AppUser.email == "q@test.com"))
            user.permissions = ["file.read"]
            await session.commit()

    asyncio.run(strip_permission())
    engine.sync_engine.dispose()

    r = client.post(
        "/api/v1/quotes/apply",
        json={"calc_id": calc, "deliverable_id": did, "expected_version_no": 0},
        headers=h,
    )
    assert r.status_code == 403
