"""线上验证 bid_generate：任务生成三份成果。"""

from __future__ import annotations

import time

import httpx


def run(base: str) -> None:
    with httpx.Client(base_url=base, timeout=60) as c:
        reg = c.post(
            "/api/v1/auth/register",
            json={"email": f"bid-{int(time.time())}@test.com", "password": "Abc12345", "enterprise_name": "生成验证"},
        )
        headers = {"Authorization": f"Bearer {reg.json()['access_token']}"}
        pid = c.post("/api/v1/projects", json={"name": "生成验证项目"}, headers=headers).json()["project_id"]
        c.post(
            f"/api/v1/projects/{pid}/requirements/upsert",
            json={
                "requirements": [
                    {"req_type": "tech_requirement", "content": "电压等级 10kV", "coordinates": [{"file_id": 1}]},
                    {"req_type": "quote_rule", "content": "报价上限 120 万", "coordinates": [{"file_id": 1}]},
                ]
            },
            headers=headers,
        )
        task = c.post(
            f"/api/v1/projects/{pid}/tasks",
            json={
                "task_type": "bid_generate",
                "payload": {"material_ref": "CABLE-YJV-3x95", "cost": 100},
                "idempotency_key": f"live-bid-{pid}",
            },
            headers=headers,
        ).json()
        for _ in range(20):
            st = c.get(f"/api/v1/tasks/{task['task_id']}", headers=headers).json()
            if st["status"] in (3, 6):
                break
            time.sleep(1)
        print("task status:", st["status"], "result:", st.get("result"))
        assert st["status"] == 3
        deliverables = c.get(f"/api/v1/deliverables?project_id={pid}", headers=headers).json()
        print("deliverables:", [(d["deliverable_type"], d["current_version_no"]) for d in deliverables])
        assert len(deliverables) == 3
        print("LIVE BID CHECK PASS")


if __name__ == "__main__":
    run("http://47.100.182.3:28123")
