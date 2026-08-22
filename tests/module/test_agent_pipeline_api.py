"""新方案接口测试：隔离性——旧接口照常、新接口受开关控制。"""

from __future__ import annotations

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings

TEST_DB = "./.test_bidvolt.db"


def _setup(client):
    r = client.post(
        "/api/v1/auth/register",
        json={"email": "agent@test.com", "password": "Abc12345", "enterprise_name": "Agent测试企业"},
    )
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    pid = client.post("/api/v1/projects", json={"name": "Agent项目"}, headers=headers).json()["project_id"]
    return headers, pid


def test_agent_run_disabled_by_default(client, monkeypatch):
    """开关关闭（默认）：新接口 409 明确提示，旧接口照常可用。"""
    monkeypatch.setattr(settings, "agent_pipeline_enabled", 0)
    h, pid = _setup(client)

    r = client.post(
        f"/api/v1/projects/{pid}/agent-run",
        json={"idempotency_key": "a1", "payload": {}},
        headers=h,
    )
    assert r.status_code == 409
    assert "旧流程" in r.json()["detail"]

    # 旧接口不受影响
    t = client.post(
        f"/api/v1/projects/{pid}/tasks",
        json={"task_type": "chat", "payload": {"message": "hi"}, "idempotency_key": "old-1"},
        headers=h,
    )
    assert t.status_code == 201
    assert t.json()["task_id"]


def test_agent_run_enabled_creates_isolated_task(client, monkeypatch):
    """开关开启：新 task_type=agent_pipeline 建任务；旧任务类型列表照常。"""
    monkeypatch.setattr(settings, "agent_pipeline_enabled", 1)
    h, pid = _setup(client)

    r = client.post(
        f"/api/v1/projects/{pid}/agent-run",
        json={"idempotency_key": "a2", "payload": {"scope": "full"}},
        headers=h,
    )
    assert r.status_code == 201
    task_id = r.json()["task_id"]
    assert r.json()["capability_token"]

    st = client.get(f"/api/v1/projects/{pid}/agent-run/{task_id}", headers=h)
    assert st.status_code == 200
    assert st.json()["task_type"] == "agent_pipeline"

    # 旧任务类型校验仍接受（TaskType.ALL 未破坏）
    t = client.post(
        f"/api/v1/projects/{pid}/tasks",
        json={"task_type": "tender_parse", "payload": {"file_ids": []}, "idempotency_key": "old-2"},
        headers=h,
    )
    assert t.status_code == 201


def test_response_package_branching_for_agent_task(client, monkeypatch):
    """新方案：response-package 只取主会话打包好的 zip；未打包 409 指引；有 zip 直接返回。"""
    monkeypatch.setattr(settings, "agent_pipeline_enabled", 1)
    h, pid = _setup(client)

    r = client.post(
        f"/api/v1/projects/{pid}/agent-run",
        json={"idempotency_key": "a3", "payload": {}},
        headers=h,
    )
    task_id = r.json()["task_id"]

    # 未打包 → 409 指引
    pkg = client.get(f"/api/v1/projects/{pid}/response-package", headers=h)
    assert pkg.status_code == 409
    assert "成文打包" in pkg.json()["detail"]

    # 主会话打包产物落库后 → 200 且内容为 zip 本身
    from app.models.agent import AgentArtifact

    engine = create_async_engine("sqlite+aiosqlite:///" + TEST_DB)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def _insert():
        async with maker() as session:
            # 用任务行反查企业/项目（任务表无 RLS）
            from app.models.task import Task

            task = await session.scalar(select(Task).where(Task.id == task_id))
            session.add(
                AgentArtifact(
                    enterprise_id=task.enterprise_id,
                    project_id=task.project_id,
                    task_id=task.id,
                    kind="zip",
                    name="响应文件包(底稿：采购文件.docx).zip",
                    mime="application/zip",
                    content=b"PK\x03\x04fakezip",
                )
            )
            await session.commit()
        await engine.dispose()

    asyncio.run(_insert())

    pkg2 = client.get(f"/api/v1/projects/{pid}/response-package", headers=h)
    assert pkg2.status_code == 200
    assert pkg2.content.startswith(b"PK\x03\x04")
    from urllib.parse import unquote

    assert "采购文件" in unquote(pkg2.headers["content-disposition"])


def test_response_package_old_path_untouched_without_agent_task(client):
    """旧任务（无 agent_pipeline 任务）仍走原服务端成文路径（有底稿时 200 或 409 底稿提示，
    绝不返回新方案 409 文案）。"""
    h, pid = _setup(client)
    pkg = client.get(f"/api/v1/projects/{pid}/response-package", headers=h)
    assert pkg.status_code == 409
    # 旧路径文案（缺底稿）≠ 新方案文案
    assert "成文打包" not in pkg.json()["detail"]
