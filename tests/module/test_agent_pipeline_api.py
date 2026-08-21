"""新方案接口测试：隔离性——旧接口照常、新接口受开关控制。"""

from __future__ import annotations

from app.config import settings


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
