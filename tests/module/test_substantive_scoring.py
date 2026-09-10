"""真实评分闭环（discussion #53）：substantive 与 builtin 隔离、Agent 提交落库、LLM 引擎。"""

from __future__ import annotations

import asyncio
import io
import json
import os
from types import SimpleNamespace

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.agent import AgentArtifact
from app.services import review_service
from app.services.task_service import TerminalTaskError


def _setup(client, email="s@test.com"):
    r = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "Abc12345", "enterprise_name": "评分企业"},
    )
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    pid = client.post("/api/v1/projects", json={"name": "S"}, headers=headers).json()["project_id"]
    return headers, pid


def _score_rule(client, headers, pid, weight=20.0, content="售后服务方案（满分20分）：响应及时性"):
    client.post(
        f"/api/v1/projects/{pid}/requirements/upsert",
        json={
            "requirements": [
                {
                    "req_type": "score_rule",
                    "content": content,
                    "structured": {"score_rule": {"weight": weight, "criterion": "售后服务响应及时"}},
                    "coordinates": [{"file_id": 1}],
                }
            ]
        },
        headers=headers,
    )
    reqs = client.get("/api/v1/requirements", params={"project_id": pid}, headers=headers).json()
    rows = [r for r in reqs if r.get("req_type") == "score_rule"]
    return int(rows[0]["req_id"])


def _docx_bytes(text: str) -> bytes:
    from docx import Document

    buf = io.BytesIO()
    doc = Document()
    doc.add_paragraph(text)
    doc.save(buf)
    return buf.getvalue()


def _seed_artifact(pid: int, content: bytes, version_no: int = 1) -> int:
    async def _run() -> int:
        engine = create_async_engine(os.environ["DATABASE_URL"])
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as s:
            art = AgentArtifact(
                enterprise_id=1,
                project_id=pid,
                task_id=1,
                kind="item_docx",
                name="技术标/技术响应.docx",
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                content=content,
                version_no=version_no,
            )
            s.add(art)
            await s.commit()
            return int(art.id)

    return asyncio.run(_run())


def _session():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    return async_sessionmaker(engine, expire_on_commit=False)()


def test_substantive_items_persist_and_builtin_hidden(client):
    """Agent 提交逐条落库；/scores 只返回 substantive，builtin 记录不冒充真实评分。"""
    h, pid = _setup(client)
    rid = _score_rule(client, h, pid, weight=20.0)

    builtin = client.post(f"/api/v1/projects/{pid}/evaluate", json={}, headers=h)
    assert builtin.status_code == 200

    hidden = client.get(f"/api/v1/projects/{pid}/scores", headers=h)
    assert hidden.status_code == 404
    assert "暂无真实评分" in hidden.json()["detail"]

    submitted = client.post(
        f"/api/v1/projects/{pid}/substantive-items",
        json={
            "items": [
                {
                    "requirement_id": rid,
                    "got": 15.0,
                    "full": 20.0,
                    "deduction_reason": "仅承诺 7×24 响应，未给出响应时限与考核措施",
                    "risk_level": 1,
                    "suggestion": "补充服务响应时限承诺与违约考核条款",
                    "missing_materials": [{"type": "售后承诺", "description": "补响应时限与考核"}],
                    "rule_source": {"quote": "售后服务响应及时性"},
                    "response_source": {"quote": "7×24 小时响应"},
                }
            ]
        },
        headers=h,
    )
    assert submitted.status_code == 200, submitted.text
    body = submitted.json()
    assert body["evaluation_type"] == "substantive"
    assert body["accepted"] == 1

    latest = client.get(f"/api/v1/projects/{pid}/scores", headers=h)
    assert latest.status_code == 200
    score = latest.json()
    assert score["evaluation_type"] == "substantive"
    assert score["score_id"] == body["score_id"]
    assert score["total_score"] == 75.0
    assert score["detail"]["rules"][0]["rule_id"] == rid

    items = client.get(f"/api/v1/projects/{pid}/scores/{body['score_id']}/items", headers=h).json()
    assert len(items) == 1
    item = items[0]
    assert item["verdict"] == "partial"
    assert item["got"] == 15.0 and item["full"] == 20.0
    assert "响应时限" in item["deduction_reason"]
    assert item["missing_materials"][0]["type"] == "售后承诺"
    assert item["rule_source"]["quote"] == "售后服务响应及时性"


def test_substantive_items_ownership_cap_and_idempotency(client):
    h, pid = _setup(client)
    rid = _score_rule(client, h, pid, weight=10.0)
    _, other_pid = _setup(client, email="s2@test.com")

    payload = {
        "items": [
            # 跨项目 requirement → 拒绝
            {"requirement_id": 99999, "got": 9, "full": 10},
            # 满分超过细则权重 → 收敛到权重上限
            {"requirement_id": rid, "got": 50, "full": 50, "verdict": "satisfied"},
        ]
    }
    first = client.post(f"/api/v1/projects/{pid}/substantive-items", json=payload, headers=h)
    assert first.status_code == 200, first.text
    assert first.json()["accepted"] == 1
    assert first.json()["results"][0]["status"] == "skipped"
    items = client.get(
        f"/api/v1/projects/{pid}/scores/{first.json()['score_id']}/items", headers=h
    ).json()
    assert items[0]["full"] == 10.0
    assert items[0]["got"] == 10.0

    replay = client.post(
        f"/api/v1/projects/{pid}/substantive-items",
        json=payload,
        headers=h,
    )
    assert replay.status_code == 200
    assert replay.json()["duplicate"] is True
    assert replay.json()["score_id"] == first.json()["score_id"]
    assert other_pid != pid


def test_substantive_evaluate_not_scoreable_without_rules(client):
    h, pid = _setup(client)
    task = SimpleNamespace(enterprise_id=1, project_id=pid, progress={}, result=None)
    try:
        asyncio.run(_run_not_scoreable(task))
    except TerminalTaskError as exc:
        assert exc.code == "not_scoreable"
        assert "评分标准" in exc.message
    else:
        raise AssertionError("缺评分细则必须 not_scoreable 失败关闭")


async def _run_not_scoreable(task):
    async with _session() as s:
        await review_service.run_substantive_evaluation(s, task)


def test_substantive_evaluate_not_scoreable_when_llm_disabled(client):
    h, pid = _setup(client)
    _score_rule(client, h, pid)
    _seed_artifact(pid, _docx_bytes("售后服务响应及时，7×24 小时响应。"))
    task = SimpleNamespace(enterprise_id=1, project_id=pid, progress={}, result=None)
    try:
        asyncio.run(_run_not_scoreable(task))
    except TerminalTaskError as exc:
        assert exc.code == "not_scoreable"
        assert "门禁" in exc.message or "评审能力" in exc.message
    else:
        raise AssertionError("LLM 门禁关闭必须 not_scoreable，不得回退 builtin")


def test_substantive_evaluate_with_mocked_llm(client, monkeypatch):
    h, pid = _setup(client)
    rid = _score_rule(client, h, pid, weight=20.0)
    aid = _seed_artifact(pid, _docx_bytes("售后服务响应及时：承诺 7×24 小时响应并接受考核。"))

    replies = [
        {
            "verdict": "partial",
            "got": 12,
            "deduction_reason": "响应时限承诺存在，但缺考核细则",
            "risk_level": 1,
            "suggestion": "补充违约考核条款",
            "missing_materials": [{"type": "考核细则", "description": "补响应考核办法"}],
            "rule_quote": "售后服务响应及时性",
            "response_quote": "7×24 小时响应",
        }
    ]
    monkeypatch.setattr("app.services.llm.llm_enabled", lambda: True)

    async def fake_chat(self, system, user):
        return json.dumps(replies.pop(0), ensure_ascii=False)

    monkeypatch.setattr("app.services.llm.LLMClient.chat", fake_chat)

    task = SimpleNamespace(enterprise_id=1, project_id=pid, progress={}, result=None)

    async def _run() -> None:
        async with _session() as s:
            await review_service.run_substantive_evaluation(s, task)
            await s.commit()

    asyncio.run(_run())
    assert task.result["evaluation_type"] == "substantive"
    assert task.result["total_score"] == 60.0

    latest = client.get(f"/api/v1/projects/{pid}/scores", headers=h).json()
    assert latest["evaluation_type"] == "substantive"
    assert latest["artifact_versions"] == {str(aid): 1}
    items = client.get(f"/api/v1/projects/{pid}/scores/{latest['score_id']}/items", headers=h).json()
    assert items[0]["verdict"] == "partial"
    assert items[0]["requirement_id"] == rid
    assert items[0]["rule_source"]["quote"] == "售后服务响应及时性"
    assert items[0]["missing_materials"][0]["type"] == "考核细则"


def test_substantive_evaluate_endpoint_idempotent(client):
    h, pid = _setup(client)
    _score_rule(client, h, pid)
    first = client.post(f"/api/v1/projects/{pid}/substantive-evaluate", json={}, headers=h)
    assert first.status_code == 200
    assert first.json()["created"] is True
    second = client.post(f"/api/v1/projects/{pid}/substantive-evaluate", json={}, headers=h)
    assert second.status_code == 200
    assert second.json()["created"] is False
    assert second.json()["task_id"] == first.json()["task_id"]
