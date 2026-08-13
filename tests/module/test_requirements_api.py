from __future__ import annotations

import asyncio
import io

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.services import llm as llm_module
from app.services.task_service import run_next_task

TEST_DB = "./.test_bidvolt.db"


def _setup(client):
    r = client.post(
        "/api/v1/auth/register",
        json={"email": "req@test.com", "password": "Abc12345", "enterprise_name": "测试企业"},
    )
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    pid = client.post("/api/v1/projects", json={"name": "P"}, headers=headers).json()["project_id"]
    return headers, pid


def _upload_txt(client, headers, pid):
    r = client.post(
        "/api/v1/files/upload",
        data={"target": "project", "project_id": str(pid)},
        files=[("files", ("招标.txt", "资质要求：三级。报价规则：上限100万。".encode("utf-8"), "text/plain"))],
        headers=headers,
    )
    return r.json()["files"][0]["file_id"]


def _drain_one_task():
    engine = create_async_engine(f"sqlite+aiosqlite:///{TEST_DB}")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def drain():
        async with factory() as session:
            return await run_next_task(session)

    result = asyncio.run(drain())
    engine.sync_engine.dispose()
    return result


def test_tender_parse_without_llm_only_parses(client):
    h, pid = _setup(client)
    file_id = _upload_txt(client, h, pid)
    client.post(
        f"/api/v1/projects/{pid}/tasks",
        json={"task_type": "tender_parse", "payload": {"file_ids": [file_id]}, "idempotency_key": "parse-1"},
        headers=h,
    )
    task = _drain_one_task()
    assert task.status == 3  # done
    assert task.result["note"]  # 门禁关闭说明
    assert task.result["requirements_extracted"] == 0
    assert client.get(f"/api/v1/requirements?project_id={pid}", headers=h).json() == []


def test_tender_parse_with_llm_extracts_requirements(client, monkeypatch):
    h, pid = _setup(client)
    file_id = _upload_txt(client, h, pid)

    async def fake_chat(self, system, user):
        return (
            '{"requirements": ['
            '{"req_type": "qualification", "content": "电力施工三级", "coordinates": [{"file_id": 1, "page_no": 1, "block_index": 0}], "confidence": 0.9},'
            '{"req_type": "quote_rule", "content": "报价上限100万", "coordinates": [{"file_id": 1, "page_no": 1, "block_index": 0}], "confidence": 0.8}'
            "]}"
        )

    monkeypatch.setattr(settings, "data_classification_confirmed", 1)
    monkeypatch.setattr(settings, "cloud_llm_enabled", 1)
    monkeypatch.setattr(settings, "minimax_api_key", "test-key")
    monkeypatch.setattr(llm_module.LLMClient, "chat", fake_chat)

    client.post(
        f"/api/v1/projects/{pid}/tasks",
        json={"task_type": "tender_parse", "payload": {"file_ids": [file_id]}, "idempotency_key": "parse-2"},
        headers=h,
    )
    task = _drain_one_task()
    assert task.result["requirements_extracted"] == 2

    reqs = client.get(f"/api/v1/requirements?project_id={pid}", headers=h).json()
    assert len(reqs) == 2
    types = {r["req_type"] for r in reqs}
    assert types == {"qualification", "quote_rule"}


def test_upsert_supersedes_previous_revision(client):
    h, pid = _setup(client)
    upsert = client.post(
        f"/api/v1/projects/{pid}/requirements/upsert",
        json={
            "requirements": [
                {"req_type": "qualification", "req_key": "qual", "content": "三级", "coordinates": [{"file_id": 1}]}
            ]
        },
        headers=h,
    )
    assert upsert.status_code == 201
    assert upsert.json()["count"] == 1

    upsert2 = client.post(
        f"/api/v1/projects/{pid}/requirements/upsert",
        json={
            "requirements": [
                {"req_type": "qualification", "req_key": "qual", "content": "二级（补遗）", "coordinates": [{"file_id": 2}]}
            ]
        },
        headers=h,
    )
    assert upsert2.status_code == 201
    reqs = client.get(f"/api/v1/requirements?project_id={pid}", headers=h).json()
    assert len(reqs) == 1
    assert reqs[0]["content"] == "二级（补遗）"
    assert reqs[0]["revision"] == 2


def test_same_type_multiple_requirements(client):
    """同 req_type 多条要求应共存（Issue #2 #11），不再互相覆盖。"""
    h, pid = _setup(client)
    for content in ("电压等级 10kV", "短路电流 30kA"):
        r = client.post(
            f"/api/v1/projects/{pid}/requirements/upsert",
            json={"requirements": [{"req_type": "tech_requirement", "content": content, "coordinates": [{"file_id": 1}]}]},
            headers=h,
        )
        assert r.status_code == 201
    reqs = client.get(f"/api/v1/requirements?project_id={pid}", headers=h).json()
    tech = [x for x in reqs if x["req_type"] == "tech_requirement"]
    assert len(tech) == 2
    assert {x["content"] for x in tech} == {"电压等级 10kV", "短路电流 30kA"}


def test_upsert_requires_coordinates(client):
    h, pid = _setup(client)
    r = client.post(
        f"/api/v1/projects/{pid}/requirements/upsert",
        json={"requirements": [{"req_type": "qualification", "content": "x"}]},
        headers=h,
    )
    assert r.status_code == 422
