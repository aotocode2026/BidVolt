from __future__ import annotations

from app.config import settings


def _setup(client):
    r = client.post(
        "/api/v1/auth/register",
        json={"email": "chat@test.com", "password": "Abc12345", "enterprise_name": "助手企业"},
    )
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    pid = client.post("/api/v1/projects", json={"name": "P"}, headers=headers).json()["project_id"]
    return headers, pid


def test_conversation_create_list_and_messages(client):
    h, pid = _setup(client)
    c = client.post(f"/api/v1/projects/{pid}/conversations", json={"title": "招标咨询"}, headers=h)
    assert c.status_code == 201
    cid = c.json()["conversation_id"]

    lst = client.get(f"/api/v1/projects/{pid}/conversations", headers=h)
    assert lst.status_code == 200
    assert any(i["conversation_id"] == cid and i["title"] == "招标咨询" for i in lst.json()["items"])

    empty = client.get(f"/api/v1/projects/{pid}/conversations/{cid}/messages", headers=h)
    assert empty.status_code == 200 and empty.json()["items"] == []

    sent = client.post(
        f"/api/v1/projects/{pid}/conversations/{cid}/messages",
        json={"message": "投标保证金是什么"},
        headers=h,
    )
    assert sent.status_code == 201
    body = sent.json()
    assert body["mode"] == "rule"  # 测试默认门禁关闭
    assert "门禁关闭" in body["reply"]

    msgs = client.get(f"/api/v1/projects/{pid}/conversations/{cid}/messages", headers=h).json()["items"]
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user" and msgs[0]["content"] == "投标保证金是什么"
    assert msgs[1]["role"] == "assistant"


def test_conversation_send_llm_when_enabled(client, monkeypatch):
    from app.services import llm as llm_module

    monkeypatch.setattr(settings, "data_classification_confirmed", 1)
    monkeypatch.setattr(settings, "cloud_llm_enabled", 1)
    monkeypatch.setattr(settings, "minimax_api_key", "test-key")

    async def fake_chat(self, system, user):
        return "投标保证金是投标担保。"

    monkeypatch.setattr(llm_module.LLMClient, "chat", fake_chat)

    h, pid = _setup(client)
    cid = client.post(f"/api/v1/projects/{pid}/conversations", json={}, headers=h).json()["conversation_id"]
    sent = client.post(
        f"/api/v1/projects/{pid}/conversations/{cid}/messages",
        json={"message": "什么是投标保证金"},
        headers=h,
    )
    assert sent.status_code == 201
    assert sent.json()["mode"] == "llm"
    assert sent.json()["reply"] == "投标保证金是投标担保。"
    msgs = client.get(f"/api/v1/projects/{pid}/conversations/{cid}/messages", headers=h).json()["items"]
    assert msgs[-1]["content"] == "投标保证金是投标担保。"


def test_conversation_cross_enterprise_denied(client):
    h, pid = _setup(client)
    cid = client.post(f"/api/v1/projects/{pid}/conversations", json={}, headers=h).json()["conversation_id"]
    r2 = client.post(
        "/api/v1/auth/register",
        json={"email": "other2@test.com", "password": "Abc12345", "enterprise_name": "其他"},
    )
    h2 = {"Authorization": f"Bearer {r2.json()['access_token']}"}
    # 租户隔离：列表按 enterprise 过滤为空，跨租户会话详情 404
    lst = client.get(f"/api/v1/projects/{pid}/conversations", headers=h2)
    assert lst.status_code == 200 and lst.json()["items"] == []
    assert (
        client.get(f"/api/v1/projects/{pid}/conversations/{cid}/messages", headers=h2).status_code
        == 404
    )


def test_conversation_empty_message_rejected(client):
    h, pid = _setup(client)
    cid = client.post(f"/api/v1/projects/{pid}/conversations", json={}, headers=h).json()["conversation_id"]
    r = client.post(
        f"/api/v1/projects/{pid}/conversations/{cid}/messages",
        json={"message": "   "},
        headers=h,
    )
    assert r.status_code == 422
