from __future__ import annotations

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.quota import TenantQuota

TEST_DB = "./.test_bidvolt.db"


def _setup(client):
    r = client.post(
        "/api/v1/auth/register",
        json={"email": "q2@test.com", "password": "Abc12345", "enterprise_name": "配额企业"},
    )
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    return headers


def _set_quota(email: str, **fields):
    engine = create_async_engine(f"sqlite+aiosqlite:///{TEST_DB}")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def update():
        async with factory() as session:
            from app.models.auth import AppUser

            user = await session.scalar(select(AppUser).where(AppUser.email == email))
            quota = await session.get(TenantQuota, user.enterprise_id)
            for key, value in fields.items():
                setattr(quota, key, value)
            await session.commit()

    asyncio.run(update())
    engine.sync_engine.dispose()


def test_upload_respects_storage_quota(client):
    h = _setup(client)
    _set_quota("q2@test.com", storage_bytes=5)
    r = client.post(
        "/api/v1/files/upload",
        data={"target": "enterprise"},
        files=[("files", ("材料.txt", "超过配额的十字节内容".encode(), "text/plain"))],
        headers=h,
    )
    assert r.status_code == 413


def test_export_respects_daily_quota(client):
    h = _setup(client)
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
    _set_quota("q2@test.com", export_daily=0)
    r = client.post(f"/api/v1/projects/{pid}/export", json={"formats": ["docx"]}, headers=h)
    assert r.status_code == 429
