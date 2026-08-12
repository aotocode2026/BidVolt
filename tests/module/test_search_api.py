from __future__ import annotations

import asyncio
import io

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.services.task_service import run_next_task

TEST_DB = "./.test_bidvolt.db"


def _setup(client):
    r = client.post(
        "/api/v1/auth/register",
        json={"email": "s@test.com", "password": "Abc12345", "enterprise_name": "搜索企业"},
    )
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    pid = client.post("/api/v1/projects", json={"name": "P"}, headers=headers).json()["project_id"]
    did = client.post(
        "/api/v1/deliverables",
        json={"project_id": pid, "deliverable_type": 1, "title": "商务标"},
        headers=headers,
    ).json()["deliverable_id"]
    client.post(
        f"/api/v1/deliverables/{did}/versions",
        json={"content": {"nodes": [{"id": "n1", "text": "商务"}]}},
        headers=headers,
    )
    return headers, pid, did


def test_search_mock_mode(client, monkeypatch):
    monkeypatch.setattr(settings, "search_mode", "mock")
    h, _, _ = _setup(client)
    r = client.post("/api/v1/searches", json={"query": "电缆 中标价"}, headers=h)
    assert r.status_code == 200
    results = r.json()["results"]
    assert {x["trust_level"] for x in results} == {1, 2, 3}


def test_search_anysearch_gate_closed(client, monkeypatch):
    monkeypatch.setattr(settings, "search_mode", "anysearch")
    monkeypatch.setattr(settings, "data_classification_confirmed", 0)
    h, _, _ = _setup(client)
    r = client.post("/api/v1/searches", json={"query": "x"}, headers=h)
    assert r.status_code == 403


def test_search_anysearch_gate_open_returns_results(client, monkeypatch):
    monkeypatch.setattr(settings, "search_mode", "anysearch")
    monkeypatch.setattr(settings, "data_classification_confirmed", 1)
    monkeypatch.setattr(settings, "search_enabled", 1)
    monkeypatch.setattr(settings, "anysearch_key", "as-key")

    def fake_query(self, query, scope=None):
        return [{"url": "https://www.gov.cn/bid/1", "title": "公告", "trust_level": 1}]

    from app.services import search_service

    monkeypatch.setattr(search_service.AnySearchProvider, "query", fake_query)
    h, _, _ = _setup(client)
    r = client.post("/api/v1/searches", json={"query": "电缆"}, headers=h)
    assert r.status_code == 200
    assert r.json()["provider"] == "anysearch"
    assert r.json()["results"][0]["trust_level"] == 1


def test_save_source_and_citation(client):
    h, _, did = _setup(client)
    src = client.post(
        "/api/v1/search-sources",
        json={"url": "https://www.gov.cn/bid/1", "title": "招标公告", "query": "电缆"},
        headers=h,
    )
    assert src.status_code == 201
    source_id = src.json()["source_id"]
    assert src.json()["trust_level"] == 1

    cit = client.post(
        f"/api/v1/deliverables/{did}/citations",
        json={"version_no": 1, "node_id": "n1", "source_id": source_id, "quote_text": "政策引用"},
        headers=h,
    )
    assert cit.status_code == 201

    refs = client.get(f"/api/v1/deliverables/{did}/references", headers=h)
    assert len(refs.json()) == 1
    assert refs.json()[0]["source"]["trust_level"] == 1


def test_chat_handler_gate_closed(client, monkeypatch):
    monkeypatch.setattr(settings, "data_classification_confirmed", 0)
    monkeypatch.setattr(settings, "cloud_llm_enabled", 0)
    h, pid, _ = _setup(client)
    client.post(
        f"/api/v1/projects/{pid}/tasks",
        json={"task_type": "chat", "payload": {"message": "你好"}, "idempotency_key": "chat-1"},
        headers=h,
    )
    engine = create_async_engine(f"sqlite+aiosqlite:///{TEST_DB}")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def drain():
        async with factory() as session:
            return await run_next_task(session)

    task = asyncio.run(drain())
    engine.sync_engine.dispose()
    assert task.status == 3
    assert task.result["mode"] == "rule"
    assert "门禁关闭" in task.result["reply"]
