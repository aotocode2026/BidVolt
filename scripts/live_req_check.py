"""线上验证招标要求域（UTF-8 文件方式，避免管道编码问题）。"""

from __future__ import annotations

import time

import httpx


def run(base: str) -> None:
    with httpx.Client(base_url=base, timeout=60) as c:
        reg = c.post(
            "/api/v1/auth/register",
            json={"email": f"req-{int(time.time())}@test.com", "password": "Abc12345", "enterprise_name": "要求验证企业"},
        )
        reg.raise_for_status()
        headers = {"Authorization": f"Bearer {reg.json()['access_token']}"}
        pid = c.post("/api/v1/projects", json={"name": "中文项目名"}, headers=headers).json()["project_id"]
        print("project name:", c.get(f"/api/v1/projects/{pid}", headers=headers).json()["name"])

        upsert = c.post(
            f"/api/v1/projects/{pid}/requirements/upsert",
            json={
                "requirements": [
                    {
                        "req_type": "qualification",
                        "content": "电力施工总承包三级资质（中文内容）",
                        "coordinates": [{"file_id": 1, "page_no": 1, "block_index": 0}],
                        "confidence": 0.9,
                    }
                ]
            },
            headers=headers,
        )
        assert upsert.status_code == 201, upsert.text
        reqs = c.get(f"/api/v1/requirements?project_id={pid}", headers=headers).json()
        print("requirement content:", reqs[0]["content"])
        assert reqs[0]["content"] == "电力施工总承包三级资质（中文内容）"

        upload = c.post(
            "/api/v1/files/upload",
            data={"target": "project", "project_id": str(pid)},
            files=[("files", ("招标.txt", "资质要求：三级。".encode("utf-8"), "text/plain"))],
            headers=headers,
        )
        file_id = upload.json()["files"][0]["file_id"]
        task = c.post(
            f"/api/v1/projects/{pid}/tasks",
            json={"task_type": "tender_parse", "payload": {"file_ids": [file_id]}, "idempotency_key": f"live-req-{pid}"},
            headers=headers,
        ).json()
        for _ in range(20):
            st = c.get(f"/api/v1/tasks/{task['task_id']}", headers=headers).json()
            if st["status"] in (3, 6):
                break
            time.sleep(1)
        print("task status:", st["status"], "result:", st.get("result"))
        assert st["status"] == 3
        print("LIVE REQ CHECK PASS")


if __name__ == "__main__":
    run("http://47.100.182.3:28123")
