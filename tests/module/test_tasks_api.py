from __future__ import annotations

import io

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services.task_service import run_next_task

TEST_DB = "./.test_bidvolt.db"


def _headers(client):
    r = client.post(
        "/api/v1/auth/register",
        json={"email": "t@test.com", "password": "Abc12345", "enterprise_name": "测试企业"},
    )
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _upload_project_file(client, headers, pid):
    r = client.post(
        "/api/v1/files/upload",
        data={"target": "project", "project_id": str(pid)},
        files=[("files", ("招标.txt", io.BytesIO("招标公告".encode("utf-8")), "text/plain"))],
        headers=headers,
    )
    return r.json()["files"][0]["file_id"]


def test_submit_task_idempotency_and_worker(client):
    h = _headers(client)
    pid = client.post("/api/v1/projects", json={"name": "P"}, headers=h).json()["project_id"]
    file_id = _upload_project_file(client, h, pid)

    payload = {"file_ids": [file_id]}
    r1 = client.post(
        f"/api/v1/projects/{pid}/tasks",
        json={"task_type": "tender_parse", "payload": payload, "idempotency_key": "key-1"},
        headers=h,
    )
    assert r1.status_code == 201
    task_id = r1.json()["task_id"]

    r2 = client.post(
        f"/api/v1/projects/{pid}/tasks",
        json={"task_type": "tender_parse", "payload": payload, "idempotency_key": "key-1"},
        headers=h,
    )
    assert r2.status_code == 201
    assert r2.json()["task_id"] == task_id
    assert r2.json()["created"] is False

    # 跑一次 worker
    engine = create_async_engine(f"sqlite+aiosqlite:///{TEST_DB}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def drain():
        async with session_factory() as session:
            processed = await run_next_task(session)
            assert processed is not None

    import asyncio

    asyncio.run(drain())
    engine.sync_engine.dispose()

    st = client.get(f"/api/v1/tasks/{task_id}", headers=h)
    assert st.json()["status"] == 3  # done
    assert st.json()["result"]["parsed_file_ids"] == [file_id]


def test_submit_unknown_task_type(client):
    h = _headers(client)
    pid = client.post("/api/v1/projects", json={"name": "P"}, headers=h).json()["project_id"]
    r = client.post(
        f"/api/v1/projects/{pid}/tasks",
        json={"task_type": "nope", "payload": {}, "idempotency_key": "k"},
        headers=h,
    )
    assert r.status_code == 422


def test_stream_whitelist_event(client):
    h = _headers(client)
    pid = client.post("/api/v1/projects", json={"name": "P"}, headers=h).json()["project_id"]
    task_id = client.post(
        f"/api/v1/projects/{pid}/tasks",
        json={"task_type": "chat", "payload": {"message": "hi"}, "idempotency_key": "k2"},
        headers=h,
    ).json()["task_id"]

    engine = create_async_engine(f"sqlite+aiosqlite:///{TEST_DB}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    import asyncio

    async def drain():
        async with session_factory() as session:
            await run_next_task(session)

    asyncio.run(drain())
    engine.sync_engine.dispose()

    resp = client.get(f"/api/v1/tasks/{task_id}/stream", headers=h)
    assert "text/event-stream" in resp.headers["content-type"]
    assert "event: progress" in resp.text
    assert "internal_id" not in resp.text


def test_interrupt_bumps_generation(client):
    h = _headers(client)
    pid = client.post("/api/v1/projects", json={"name": "P"}, headers=h).json()["project_id"]
    task_id = client.post(
        f"/api/v1/projects/{pid}/tasks",
        json={"task_type": "chat", "payload": {}, "idempotency_key": "k3"},
        headers=h,
    ).json()["task_id"]

    r = client.post(f"/api/v1/projects/{pid}/tasks/{task_id}/interrupt", headers=h)
    assert r.status_code == 200
    assert r.json()["generation"] == 2


def test_stream_terminal_task_emits_progress_and_done(client):
    h = _headers(client)
    pid = client.post("/api/v1/projects", json={"name": "P"}, headers=h).json()["project_id"]
    task_id = client.post(
        f"/api/v1/projects/{pid}/tasks",
        json={"task_type": "chat", "payload": {}, "idempotency_key": "stream-1"},
        headers=h,
    ).json()["task_id"]

    engine = create_async_engine(f"sqlite+aiosqlite:///{TEST_DB}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    import asyncio

    async def drain():
        async with session_factory() as session:
            await run_next_task(session)

    asyncio.run(drain())
    engine.sync_engine.dispose()

    resp = client.get(f"/api/v1/tasks/{task_id}/stream", headers=h)
    assert "event: progress" in resp.text
    assert "event: done" in resp.text
    assert "internal_id" not in resp.text
