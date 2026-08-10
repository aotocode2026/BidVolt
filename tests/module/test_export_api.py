from __future__ import annotations

import asyncio
import io
import zipfile

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.auth import AppUser

TEST_DB = "./.test_bidvolt.db"


def _setup(client):
    r = client.post(
        "/api/v1/auth/register",
        json={"email": "x@test.com", "password": "Abc12345", "enterprise_name": "测试企业"},
    )
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    pid = client.post("/api/v1/projects", json={"name": "P"}, headers=headers).json()["project_id"]
    return headers, pid


def _add_deliverable(client, headers, pid, dtype, model):
    did = client.post(
        "/api/v1/deliverables",
        json={"project_id": pid, "deliverable_type": dtype, "title": "成果"},
        headers=headers,
    ).json()["deliverable_id"]
    client.post(
        f"/api/v1/deliverables/{did}/versions",
        json={"content": model, "version_type": 2},
        headers=headers,
    )
    return did


def test_final_check_detects_missing_deliverable(client):
    h, pid = _setup(client)
    _add_deliverable(client, h, pid, 1, {"nodes": [{"id": "n1", "text": "商务"}]})
    _add_deliverable(client, h, pid, 3, {"type": "sheet", "sheets": [{"name": "报价单", "rows": [["a", "1"]]}]})

    check = client.post(f"/api/v1/projects/{pid}/check", json={}, headers=h)
    assert check.status_code == 200
    assert check.json()["passed"] is False
    assert any("技术标" in i["message"] for i in check.json()["issues"])

    _add_deliverable(client, h, pid, 2, {"nodes": [{"id": "n2", "text": "技术"}]})
    check2 = client.post(f"/api/v1/projects/{pid}/check", json={}, headers=h)
    assert check2.json()["passed"] is True

    got = client.get(f"/api/v1/projects/{pid}/check/{check2.json()['check_id']}", headers=h)
    assert got.json()["passed"] is True


def test_export_and_delivery_package(client):
    h, pid = _setup(client)
    _add_deliverable(client, h, pid, 1, {"nodes": [{"id": "n1", "text": "商务响应"}]})
    _add_deliverable(client, h, pid, 2, {"nodes": [{"id": "n2", "text": "技术方案"}]})
    _add_deliverable(
        client,
        h,
        pid,
        3,
        {"type": "sheet", "sheets": [{"name": "报价单", "rows": [["材料", "价格"], ["电缆", "120"]]}]},
    )

    exp = client.post(
        f"/api/v1/projects/{pid}/export",
        json={"formats": ["docx", "xlsx"], "with_manifest": True},
        headers=h,
    )
    assert exp.status_code == 200
    job_id = exp.json()["job_id"]
    files = exp.json()["files"]
    assert any(f["name"].endswith(".docx") for f in files)
    assert any(f["name"].endswith(".xlsx") for f in files)
    assert any(f["name"] == "manifest.json" for f in files)
    assert all(f["sha256"] for f in files)

    status = client.get(f"/api/v1/projects/{pid}/export/{job_id}", headers=h)
    assert status.json()["status"] == 2

    pkg = client.get(f"/api/v1/projects/{pid}/delivery-package", headers=h)
    assert pkg.status_code == 200
    with zipfile.ZipFile(io.BytesIO(pkg.content)) as zf:
        names = zf.namelist()
        assert "manifest.json" in names
        assert any(n.endswith(".docx") for n in names)
        assert any(n.endswith(".xlsx") for n in names)


def test_export_requires_deliverable_export_permission(client):
    h, pid = _setup(client)
    _add_deliverable(client, h, pid, 1, {"nodes": [{"id": "n1", "text": "商务"}]})

    engine = create_async_engine(f"sqlite+aiosqlite:///{TEST_DB}")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def strip():
        async with factory() as session:
            user = await session.scalar(select(AppUser).where(AppUser.email == "x@test.com"))
            user.permissions = ["file.read"]
            await session.commit()

    asyncio.run(strip())
    engine.sync_engine.dispose()

    r = client.post(f"/api/v1/projects/{pid}/export", json={"formats": ["docx"]}, headers=h)
    assert r.status_code == 403
