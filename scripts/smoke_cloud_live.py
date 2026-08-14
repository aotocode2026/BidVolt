"""Live check: 云能力解锁后的真实链路（AnySearch 匿名搜索 + MiniMax LLM）。

依赖容器 .env 已置 DATA_CLASSIFICATION_CONFIRMED=1 / CLOUD_LLM_ENABLED=1 /
SEARCH_ENABLED=1 / SEARCH_MODE=anysearch。
用法：python scripts/smoke_cloud_live.py [--base http://47.100.182.3:28123]
"""
from __future__ import annotations

import argparse
import time

import httpx


def _poll_task(client: httpx.Client, headers: dict, task_id: int, timeout: int = 180) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/v1/tasks/{task_id}", headers=headers)
        r.raise_for_status()
        task = r.json()
        if task["status"] in (3, 4):  # 3=成功 4=失败
            return task
        time.sleep(2)
    raise TimeoutError(f"task {task_id} 未在 {timeout}s 内结束")


def run(base: str) -> None:
    with httpx.Client(base_url=base, timeout=60) as c:
        email = f"cloud-live-{int(time.time())}@test.com"
        reg = c.post(
            "/api/v1/auth/register",
            json={"email": email, "password": "Abc12345", "enterprise_name": "CloudSmoke"},
        )
        reg.raise_for_status()
        headers = {"Authorization": f"Bearer {reg.json()['access_token']}"}
        pid = c.post("/api/v1/projects", json={"name": "CloudSmoke"}, headers=headers).json()["project_id"]

        # 1) AnySearch 匿名搜索（真实出网）
        s = c.post("/api/v1/searches", json={"query": "政府采购 电缆 中标价 2026"}, headers=headers)
        s.raise_for_status()
        body = s.json()
        assert body["provider"] == "anysearch", body
        assert body["results"], "AnySearch 无返回结果"
        print(f"ANYSEARCH OK: provider={body['provider']} results={len(body['results'])}")
        print("  sample:", body["results"][0]["title"][:60], "| trust:", body["results"][0]["trust_level"])

        # 2) Chat 任务走真实 MiniMax LLM
        t = c.post(
            f"/api/v1/projects/{pid}/tasks",
            json={
                "task_type": "chat",
                "payload": {"message": "请用一句话解释什么是投标保证金"},
                "idempotency_key": f"chat-live-{int(time.time())}",
            },
            headers=headers,
        )
        t.raise_for_status()
        task_id = t.json()["task_id"]
        task = _poll_task(c, headers, task_id)
        assert task["status"] == 3, task
        mode = (task.get("result") or {}).get("mode")
        reply = (task.get("result") or {}).get("reply", "")
        assert mode == "llm", f"chat 未走真实 LLM: mode={mode}"
        assert reply, "LLM 回复为空"
        print(f"LLM OK: mode={mode} reply_len={len(reply)}")
        print("  reply:", reply[:120].replace("\n", " "))
        print("CLOUD LIVE PASS")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://47.100.182.3:28123")
    run(ap.parse_args().base)
