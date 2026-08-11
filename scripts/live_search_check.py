"""线上验证搜索（mock 模式）与 chat 任务。"""

from __future__ import annotations

import time

import httpx


def run(base: str) -> None:
    with httpx.Client(base_url=base, timeout=60) as c:
        reg = c.post(
            "/api/v1/auth/register",
            json={"email": f"sch-{int(time.time())}@test.com", "password": "Abc12345", "enterprise_name": "搜索验证"},
        )
        headers = {"Authorization": f"Bearer {reg.json()['access_token']}"}
        pid = c.post("/api/v1/projects", json={"name": "搜索项目"}, headers=headers).json()["project_id"]

        search = c.post("/api/v1/searches", json={"query": "cable price"}, headers=headers)
        print("search status:", search.status_code, "provider:", search.json().get("provider"))
        assert search.status_code == 200
        assert search.json()["results"][0]["trust_level"] == 1

        src = c.post(
            "/api/v1/search-sources",
            json={"url": "https://www.gov.cn/bid/1", "title": "公告", "query": "cable", "project_id": pid},
            headers=headers,
        )
        print("save_source:", src.status_code, "trust:", src.json()["trust_level"])
        assert src.status_code == 201

        task = c.post(
            f"/api/v1/projects/{pid}/tasks",
            json={"task_type": "chat", "payload": {"message": "hello"}, "idempotency_key": f"live-chat-{pid}"},
            headers=headers,
        ).json()
        for _ in range(20):
            st = c.get(f"/api/v1/tasks/{task['task_id']}", headers=headers).json()
            if st["status"] in (3, 6):
                break
            time.sleep(1)
        print("chat task:", st["status"], st["result"]["mode"])
        assert st["status"] == 3 and st["result"]["mode"] == "rule"
        print("LIVE SEARCH/CHAT CHECK PASS")


if __name__ == "__main__":
    run("http://47.100.182.3:28123")
