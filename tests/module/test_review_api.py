from __future__ import annotations

import io


def _setup(client):
    r = client.post(
        "/api/v1/auth/register",
        json={"email": "r@test.com", "password": "Abc12345", "enterprise_name": "测试企业"},
    )
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    pid = client.post("/api/v1/projects", json={"name": "P"}, headers=headers).json()["project_id"]
    return headers, pid


def _create_deliverable(client, headers, pid, dtype, content):
    did = client.post(
        "/api/v1/deliverables",
        json={"project_id": pid, "deliverable_type": dtype, "title": "成果"},
        headers=headers,
    ).json()["deliverable_id"]
    client.post(
        f"/api/v1/deliverables/{did}/versions",
        json={"content": content, "version_type": 2},
        headers=headers,
    )
    return did


def test_evaluate_reports_missing_deliverables(client):
    h, pid = _setup(client)
    r = client.post(f"/api/v1/projects/{pid}/evaluate", json={}, headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["missing_count"] == 3
    assert body["total_score"] == 0
    # Issue #8：无评分细则时显式标注内置规则（scale=builtin），不冒充招标得分
    assert body["scale"] == "builtin"
    assert body["full_marks"] == 30  # 完整性三份各 10 分

    # discussion #53：builtin 完整性检查是内部自检，不进入前端评分卡
    scores = client.get(f"/api/v1/projects/{pid}/scores", headers=h)
    assert scores.status_code == 404
    assert "暂无真实评分" in scores.json()["detail"]
    items = client.get(f"/api/v1/projects/{pid}/scores/{body['score_id']}/items", headers=h)
    assert len(items.json()) == 3
    assert all(i["status"] == 1 for i in items.json())


def test_evaluate_weighted_score_rules(client):
    """评分细则权重化评审引擎：细则体现得满分、未体现 0 分并给建议；统计可追溯。"""
    h, pid = _setup(client)
    long_text = "本公司具备相应资质与业绩，人员设备资金保障到位，质量保证体系健全，售后服务响应及时。" * 3
    client.post(
        f"/api/v1/projects/{pid}/requirements/upsert",
        json={"requirements": [
            {"req_type": "score_rule", "content": "售后服务方案（满分20分）：响应及时性",
             "structured": {"score_rule": {"weight": 20, "criterion": "售后服务响应及时"}},
             "coordinates": [{"file_id": 1}]},
            {"req_type": "score_rule", "content": "进度保障（满分10分）：按期交付",
             "structured": {"score_rule": {"weight": 10, "criterion": "按期交付承诺"}},
             "coordinates": [{"file_id": 1}]},
        ]},
        headers=h,
    )
    # 技术标体现第一条细则（"售后服务响应及时"），未体现第二条
    _create_deliverable(client, h, pid, 2, {
        "nodes": [{"type": "paragraph", "text": long_text + "售后服务响应及时性：7×24 小时响应。"}]})
    _create_deliverable(client, h, pid, 1, {"nodes": [{"type": "paragraph", "text": long_text}]})
    _create_deliverable(client, h, pid, 3, {"type": "sheet", "sheets": [{"name": "报价单", "rows": [["项目", "建议价"]]}]})

    r = client.post(f"/api/v1/projects/{pid}/evaluate", json={}, headers=h)
    assert r.status_code == 200
    body = r.json()
    stats = body["score_rules"]
    assert stats["count"] == 2
    assert stats["weight_total"] == 30
    assert stats["weight_got"] == 20
    assert stats["missed"] == 1
    # Issue #8：评分基准必须显式——有评分细则按细则计算并标注 scale=score_rules
    assert body["scale"] == "score_rules"
    assert body["full_marks"] == 30
    assert body["got_marks"] == 20
    assert abs(body["total_score"] - 20 / 30 * 100) < 0.01

    items = client.get(f"/api/v1/projects/{pid}/scores/{body['score_id']}/items", headers=h).json()
    rule_items = [i for i in items if i["category"] == "评分细则"]
    assert len(rule_items) == 2
    got_values = sorted(i["got"] for i in rule_items)
    assert got_values == [0, 20]  # 一条满分、一条 0 分
    missed_item = next(i for i in rule_items if i["got"] == 0)
    assert "评分细则未在成果中体现" in missed_item["suggestion"]


def test_latest_score_binds_artifact_versions_and_detects_stale(client):
    """评分冻结 artifact 版本；artifact/结构化成果版本变化后旧分判过期（issue #22）。"""
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models.agent import AgentArtifact

    h, pid = _setup(client)
    did1 = _create_deliverable(client, h, pid, 1, {"nodes": [{"type": "paragraph", "text": "商务"}]})
    _create_deliverable(client, h, pid, 2, {"nodes": [{"type": "paragraph", "text": "技术"}]})
    _create_deliverable(client, h, pid, 3, {"type": "sheet", "sheets": [{"name": "报价单", "rows": [["项目", "建议价"]]}]})

    engine = create_async_engine("sqlite+aiosqlite:///./.test_bidvolt.db")
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def _seed() -> int:
        async with maker() as s:
            art = AgentArtifact(
                enterprise_id=1,
                project_id=pid,
                task_id=1,
                kind="item_docx",
                name="价格文件/报价单.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                content=b"x",
                version_no=1,
            )
            s.add(art)
            await s.commit()
            return int(art.id)

    aid = asyncio.run(_seed())
    # discussion #53：/scores 只返回 substantive 真实评分，用 Agent 提交路径落库
    r = client.post(
        f"/api/v1/projects/{pid}/substantive-items",
        json={
            "items": [
                {
                    "requirement_id": None,
                    "category": "评分细则",
                    "problem_description": "售后服务方案",
                    "got": 15.0,
                    "full": 20.0,
                    "verdict": "partial",
                    "risk_level": 1,
                    "suggestion": "补充响应时效承诺",
                }
            ]
        },
        headers=h,
    )
    assert r.status_code == 200
    assert r.json()["evaluation_type"] == "substantive"

    latest = client.get(f"/api/v1/projects/{pid}/scores", headers=h).json()
    assert latest["has_score"] is True
    assert latest["evaluation_type"] == "substantive"
    assert latest["artifact_versions"] == {str(aid): 1}
    assert any(a["artifact_id"] == aid for a in latest["scored_artifacts"])
    assert latest["is_stale"] is False

    # artifact 版本变化 → 旧分过期（issue #22 核心验收）
    async def _bump_artifact():
        async with maker() as s:
            art = await s.get(AgentArtifact, aid)
            art.version_no = 2
            await s.commit()

    asyncio.run(_bump_artifact())
    stale_after_artifact = client.get(f"/api/v1/projects/{pid}/scores", headers=h).json()
    assert stale_after_artifact["is_stale"] is True
    assert any(
        reason.get("artifact_id") == aid and reason.get("current_version") == 2
        for reason in stale_after_artifact["stale_reasons"]
    )

    # 结构化成果版本变化 → 旧分过期（字符串/整数键比对修复）
    client.post(
        f"/api/v1/deliverables/{did1}/versions",
        json={"content": {"nodes": [{"type": "paragraph", "text": "商务第二版"}]}, "version_type": 2},
        headers=h,
    )
    stale_after_deliverable = client.get(f"/api/v1/projects/{pid}/scores", headers=h).json()
    assert any(
        reason.get("deliverable_id") == did1 and reason.get("current_version") == 2
        for reason in stale_after_deliverable["stale_reasons"]
    )
    asyncio.run(engine.dispose())


def test_two_enterprises_both_evaluate(client):
    """服务器实测回归：内置 Provider 按企业隔离创建，
    唯一约束为 (enterprise_id, provider_code)，第二个企业评审不再撞
    review_provider_provider_code_key（曾致生产 evaluate 500）。"""
    for email in ("rev-a@test.com", "rev-b@test.com"):
        r = client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": "Abc12345", "enterprise_name": email},
        )
        h = {"Authorization": f"Bearer {r.json()['access_token']}"}
        pid = client.post("/api/v1/projects", json={"name": "P"}, headers=h).json()["project_id"]
        r2 = client.post(f"/api/v1/projects/{pid}/evaluate", json={}, headers=h)
        assert r2.status_code == 200, f"{email}: {r2.text[:120]}"
        assert r2.json()["missing_count"] == 3


def test_evaluate_partial_score(client):
    h, pid = _setup(client)
    _create_deliverable(client, h, pid, 1, {"nodes": [{"id": "n1", "text": "商务"}]})
    r = client.post(f"/api/v1/projects/{pid}/evaluate", json={}, headers=h).json()
    assert r["missing_count"] == 2
    assert abs(r["total_score"] - 33.33) < 0.01


def test_review_run_list_and_detail(client):
    h, pid = _setup(client)
    _create_deliverable(client, h, pid, 1, {"nodes": [{"id": "n1", "text": "商务"}]})
    ev = client.post(f"/api/v1/projects/{pid}/evaluate", json={}, headers=h).json()
    run_id = ev["run_id"]

    runs = client.get(f"/api/v1/projects/{pid}/reviews", headers=h)
    assert runs.status_code == 200
    assert any(r["run_id"] == run_id and r["snapshot_id"] == ev["snapshot_id"] for r in runs.json()["items"])

    detail = client.get(f"/api/v1/projects/{pid}/reviews/{run_id}", headers=h)
    assert detail.status_code == 200
    body = detail.json()
    assert body["run_id"] == run_id
    assert body["snapshot_id"] == ev["snapshot_id"]
    assert body["provider"] and body["provider"]["provider_id"]
    assert body["score"]["score_id"] == ev["score_id"]
    assert len(body["items"]) == 3


def test_confirm_batch_and_replay(client):
    h, pid = _setup(client)
    body = client.post(f"/api/v1/projects/{pid}/evaluate", json={}, headers=h).json()
    score_id, item_ids = body["score_id"], body["item_ids"]

    one = client.put(
        f"/api/v1/projects/{pid}/scores/{score_id}/items/{item_ids[0]}/confirm",
        json={"action": "confirm", "expected_version": body["snapshot_id"]},
        headers=h,
    )
    assert one.json()["status"] == "succeeded"

    conflict = client.put(
        f"/api/v1/projects/{pid}/scores/{score_id}/items/{item_ids[1]}/confirm",
        json={"action": "confirm", "expected_version": "999"},
        headers=h,
    )
    assert conflict.json()["status"] == "conflict"

    batch = client.post(
        f"/api/v1/projects/{pid}/scores/{score_id}/items/confirm",
        json={"item_ids": item_ids[1:], "action": "confirm", "expected_version": body["snapshot_id"]},
        headers=h,
    )
    assert all(x["status"] == "succeeded" for x in batch.json()["results"])

    replay = client.post(
        f"/api/v1/projects/{pid}/scores/{score_id}/items/confirm",
        json={"item_ids": item_ids, "expected_version": body["snapshot_id"]},
        headers=h,
    )
    assert all(x["status"] == "skipped" for x in replay.json()["results"])

def test_re_evaluate_does_not_fake_improve_after_material(client):
    """discussion #53：确认上传建议 + 项目存在任意材料，不得自动判满分。"""
    h, pid = _setup(client)
    body = client.post(f"/api/v1/projects/{pid}/evaluate", json={}, headers=h).json()
    missing_item = body["item_ids"][0]
    client.put(
        f"/api/v1/projects/{pid}/scores/{body['score_id']}/items/{missing_item}/confirm",
        json={"action": "confirm", "expected_version": body["snapshot_id"]},
        headers=h,
    )
    client.post(
        "/api/v1/files/upload",
        data={"target": "project", "project_id": str(pid)},
        files=[("files", ("补充材料.txt", io.BytesIO("资质证书".encode()), "text/plain"))],
        headers=h,
    )
    re = client.post(
        f"/api/v1/projects/{pid}/re-evaluate",
        json={"item_ids": [missing_item]},
        headers=h,
    )
    assert re.status_code == 200
    # 不再“任意材料→满分”：真实改善只能来自重新实质评审，得分保持不变
    assert re.json()["improved_count"] == 0
    assert re.json()["total_score"] == 0.0


def test_unconfirmed_material_does_not_change_score(client):
    """A-12：未确认的材料关联不改变成果版本与实际得分。"""
    h, pid = _setup(client)
    body = client.post(f"/api/v1/projects/{pid}/evaluate", json={}, headers=h).json()
    missing_item = body["item_ids"][0]
    score_id = body["score_id"]

    # 上传补充材料但【不确认】评审项
    client.post(
        "/api/v1/files/upload",
        data={"target": "project", "project_id": str(pid)},
        files=[("files", ("补充材料.txt", io.BytesIO("资质证书".encode()), "text/plain"))],
        headers=h,
    )
    re = client.post(
        f"/api/v1/projects/{pid}/re-evaluate",
        json={"item_ids": [missing_item]},
        headers=h,
    )
    assert re.status_code == 200
    assert re.json()["improved_count"] == 0
    # 未确认材料不改变实际得分：三份成果均缺失 → 0
    assert re.json()["total_score"] == 0.0

    # 评审项状态仍为待确认，未进入 confirmed
    items = client.get(
        f"/api/v1/projects/{pid}/scores/{score_id}/items",
        headers=h,
    ).json()
    original = next(i for i in items if i["item_id"] == missing_item)
    assert original["status"] == 1


def test_provider_list_after_evaluate(client):
    h, pid = _setup(client)
    client.post(f"/api/v1/projects/{pid}/evaluate", json={}, headers=h)
    providers = client.get("/api/v1/review-providers", headers=h)
    assert providers.status_code == 200
    assert any(p["provider_code"] == "builtin_completeness" for p in providers.json())
