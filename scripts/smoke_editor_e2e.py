# -*- coding: utf-8 -*-
"""真实服务器端到端：在线编辑会话（创建→检查点→完成）+ AI 建议（LLM）→应用。"""
from __future__ import annotations

import argparse
import json
import time

import httpx


def run(base: str) -> None:
    with httpx.Client(base_url=base, timeout=60) as c:
        email = f"editor-e2e-{int(time.time())}@test.com"
        reg = c.post(
            "/api/v1/auth/register",
            json={"email": email, "password": "Abc12345", "enterprise_name": "EditorE2E"},
        )
        reg.raise_for_status()
        headers = {"Authorization": f"Bearer {reg.json()['access_token']}"}
        pid = c.post("/api/v1/projects", json={"name": "EditorE2E"}, headers=headers).json()["project_id"]
        did = c.post(
            "/api/v1/deliverables",
            json={"project_id": pid, "deliverable_type": 1, "title": "商务标"},
            headers=headers,
        ).json()["deliverable_id"]
        c.post(
            f"/api/v1/deliverables/{did}/versions",
            json={"content": {"nodes": [{"id": "n1", "type": "paragraph", "text": "原商务标正文"}]}},
            headers=headers,
        ).raise_for_status()

        # 1) 在线编辑会话：创建 → 检查点 → 完成 → 新版本
        s = c.post(f"/api/v1/deliverables/{did}/editor-sessions", json={}, headers=headers)
        s.raise_for_status()
        session = s.json()
        edited = {"nodes": [{"id": "n1", "type": "paragraph", "text": "人工编辑后的商务标正文"}]}
        c.put(
            f"/api/v1/deliverables/{did}/editor-sessions/{session['session_id']}/checkpoint",
            json={"lease_token": session["lease_token"], "content": edited},
            headers=headers,
        ).raise_for_status()
        done = c.post(
            f"/api/v1/deliverables/{did}/editor-sessions/{session['session_id']}/complete",
            json={
                "lease_token": session["lease_token"],
                "content": edited,
                "expected_version_no": session["base_version_no"],
            },
            headers=headers,
        )
        done.raise_for_status()
        assert done.json()["version_no"] == 2, done.json()
        v2 = c.get(f"/api/v1/deliverables/{did}/versions/2", headers=headers).json()
        assert v2["model"]["nodes"][0]["text"] == "人工编辑后的商务标正文"
        print("EDITOR SESSION E2E PASS: v2 =", v2["model"]["nodes"][0]["text"])

        # 2) AI 建议：真实 LLM 生成 → 应用 → 新版本
        ai = c.post(
            f"/api/v1/deliverables/{did}/ai-edit",
            json={
                "selection": {"type": "text", "refs": ["n1"]},
                "instruction": "请把这段改成更正式、更完整的商务响应说明",
            },
            headers=headers,
        )
        ai.raise_for_status()
        diff_id = ai.json()["diff_id"]
        applied = c.post(f"/api/v1/deliverables/{did}/ai-edit/{diff_id}/apply", headers=headers)
        applied.raise_for_status()
        assert applied.json()["version_no"] == 3, applied.json()
        v3 = c.get(f"/api/v1/deliverables/{did}/versions/3", headers=headers).json()
        text3 = v3["model"]["nodes"][0]["text"]
        assert text3 and text3 != "人工编辑后的商务标正文", text3
        print("AI EDIT E2E PASS: v3 =", text3[:120])
        print("EDITOR E2E ALL PASS")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://47.100.182.3:28123")
    run(ap.parse_args().base)
