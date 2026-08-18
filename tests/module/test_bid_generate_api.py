from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.services import llm as llm_module
from app.services.task_service import run_next_task

TEST_DB = "./.test_bidvolt.db"


def _setup(client):
    r = client.post(
        "/api/v1/auth/register",
        json={"email": "bg@test.com", "password": "Abc12345", "enterprise_name": "生成企业"},
    )
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    pid = client.post("/api/v1/projects", json={"name": "生成项目"}, headers=headers).json()["project_id"]
    return headers, pid


def _drain_one_task():
    engine = create_async_engine(f"sqlite+aiosqlite:///{TEST_DB}")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def drain():
        async with factory() as session:
            return await run_next_task(session)

    result = asyncio.run(drain())
    engine.sync_engine.dispose()
    return result


def test_bid_generate_creates_three_deliverables(client):
    h, pid = _setup(client)
    client.post(
        f"/api/v1/projects/{pid}/requirements/upsert",
        json={
            "requirements": [
                {"req_type": "basic_info", "content": "项目名称：变电站改造", "coordinates": [{"file_id": 1}]},
                {"req_type": "tech_requirement", "content": "电压等级 10kV", "coordinates": [{"file_id": 1}]},
            ]
        },
        headers=h,
    )
    client.post(
        f"/api/v1/projects/{pid}/tasks",
        json={
            "task_type": "bid_generate",
            "payload": {"material_ref": "CABLE-YJV-3x95", "cost": 100},
            "idempotency_key": "bidgen-1",
        },
        headers=h,
    )
    task = _drain_one_task()
    assert task.status == 3
    assert set(task.result) >= {1, 2, 3}
    assert "门禁关闭" in task.result["note"]

    deliverables = client.get(f"/api/v1/deliverables?project_id={pid}", headers=h).json()
    assert len(deliverables) == 3
    for d in deliverables:
        assert d["current_version_no"] == 1

    quote = next(d for d in deliverables if d["deliverable_type"] == 3)
    content = client.get(f"/api/v1/deliverables/{quote['deliverable_id']}/content", headers=h).json()
    sheet_text = str(content["model"])
    assert "CABLE-YJV-3x95" in sheet_text
    assert "待人工定价" not in sheet_text  # 有样本即可确定测算

    # 幂等：同一任务重复提交不产生新版本
    client.post(
        f"/api/v1/projects/{pid}/tasks",
        json={
            "task_type": "bid_generate",
            "payload": {"material_ref": "CABLE-YJV-3x95", "cost": 100},
            "idempotency_key": "bidgen-1",
        },
        headers=h,
    )
    _drain_one_task()  # 幂等返回原任务（created=False），worker 不重复写
    deliverables = client.get(f"/api/v1/deliverables?project_id={pid}", headers=h).json()
    assert all(d["current_version_no"] == 1 for d in deliverables)


def test_bid_generate_requires_requirements(client):
    """Issue #12 问题三：要求为 0 不得生成并标记完成——任务必须失败并给出明确指引。"""
    h, pid = _setup(client)
    client.post(
        f"/api/v1/projects/{pid}/tasks",
        json={"task_type": "bid_generate", "payload": {}, "idempotency_key": "bg-noreq"},
        headers=h,
    )
    task = None
    for _ in range(3):  # MAX_RETRIES=3：三轮后 FAILED_TERMINAL
        task = _drain_one_task()
    assert task is not None
    assert task.status == 6
    assert "要求" in (task.error or {}).get("message", "")


def test_bid_generate_without_payload_quote_is_placeholder(client):
    """Issue #12 问题三：payload 不带物料/成本时，报价单不得编造演示物料（CABLE-YJV）。"""
    h, pid = _setup(client)
    client.post(
        f"/api/v1/projects/{pid}/requirements/upsert",
        json={
            "requirements": [
                {"req_type": "tech_requirement", "content": "电压等级 10kV", "coordinates": [{"file_id": 1}]},
            ]
        },
        headers=h,
    )
    client.post(
        f"/api/v1/projects/{pid}/tasks",
        json={"task_type": "bid_generate", "payload": {}, "idempotency_key": "bg-nopayload"},
        headers=h,
    )
    task = _drain_one_task()
    assert task.status == 3
    deliverables = client.get(f"/api/v1/deliverables?project_id={pid}", headers=h).json()
    quote = next(d for d in deliverables if d["deliverable_type"] == 3)
    content = client.get(f"/api/v1/deliverables/{quote['deliverable_id']}/content", headers=h).json()
    sheet_text = str(content["model"])
    assert "待报价测算" in sheet_text
    assert "CABLE-YJV-3x95" not in sheet_text


def test_material_match_task(client):
    h, pid = _setup(client)
    client.post(
        f"/api/v1/projects/{pid}/requirements/upsert",
        json={
            "requirements": [
                {"req_type": "qualification", "content": "电力施工资质", "coordinates": [{"file_id": 1}]},
                {"req_type": "tech_requirement", "content": "电压等级 10kV", "coordinates": [{"file_id": 1}]},
            ]
        },
        headers=h,
    )
    # 企业资料：资质证书（asset_type=资质）
    client.post(
        "/api/v1/files/upload",
        data={"target": "enterprise"},
        files=[("files", ("资质证书.txt", "电力施工总承包三级".encode(), "text/plain"))],
        headers=h,
    )

    client.post(
        f"/api/v1/projects/{pid}/tasks",
        json={"task_type": "material_match", "payload": {}, "idempotency_key": "mm-1"},
        headers=h,
    )
    task = _drain_one_task()
    assert task.status == 3
    assert task.result["matched_count"] == 2

    matches = client.get(f"/api/v1/projects/{pid}/material-matches", headers=h).json()
    by_req = {m["requirement_id"]: m["matched"] for m in matches}
    reqs = client.get(f"/api/v1/requirements?project_id={pid}", headers=h).json()
    for req in reqs:
        if req["req_type"] == "qualification":
            assert by_req[req["req_id"]] == 1
        else:
            assert by_req[req["req_id"]] == 3


def test_bid_review_reports_missing_deliverables(client):
    h, pid = _setup(client)
    client.post(
        f"/api/v1/projects/{pid}/tasks",
        json={"task_type": "bid_review", "payload": {}, "idempotency_key": "review-1"},
        headers=h,
    )
    task = _drain_one_task()
    assert task.status == 3
    messages = [i["message"] for i in task.result["issues"]]
    assert any("缺少商务标" in m for m in messages)
    assert any("缺少技术标" in m for m in messages)
    assert any("缺少报价单" in m for m in messages)


def test_bid_review_detects_coverage_and_name(client):
    h, pid = _setup(client)
    client.post(
        f"/api/v1/projects/{pid}/requirements/upsert",
        json={
            "requirements": [
                {"req_type": "tech_requirement", "content": "电压等级 10kV", "coordinates": [{"file_id": 1}]},
            ]
        },
        headers=h,
    )
    client.post(
        f"/api/v1/projects/{pid}/tasks",
        json={
            "task_type": "bid_generate",
            "payload": {"material_ref": "CABLE-YJV-3x95", "cost": 100},
            "idempotency_key": "bg-r1",
        },
        headers=h,
    )
    _drain_one_task()
    # 生成后补一条新要求：校核应能发现技术标未响应
    client.post(
        f"/api/v1/projects/{pid}/requirements/upsert",
        json={
            "requirements": [
                {"req_type": "tech_requirement", "content": "抗短路能力 30kA", "coordinates": [{"file_id": 2}]}
            ]
        },
        headers=h,
    )
    client.post(
        f"/api/v1/projects/{pid}/tasks",
        json={"task_type": "bid_review", "payload": {}, "idempotency_key": "review-2"},
        headers=h,
    )
    task = _drain_one_task()
    assert task.status == 3
    messages = [i["message"] for i in task.result["issues"]]
    # 生成的技术标覆盖了"电压等级"，未覆盖"抗短路能力"
    assert any("抗短路能力" in m for m in messages)
    assert not any("电压等级 10kV" in m for m in messages)


def test_bid_generate_llm_generates_business_and_technical(client, monkeypatch):
    """回归（产品反馈）：技术标必须由 LLM 生成实质正文，不得只剩确定性 stub。
    新流程（用户反馈"标书太短"）：分章并行深度生成，逐条响应全部技术要求。"""
    monkeypatch.setattr(settings, "data_classification_confirmed", 1)
    monkeypatch.setattr(settings, "cloud_llm_enabled", 1)
    monkeypatch.setattr(settings, "minimax_api_key", "test-key")

    async def fake_chat(self, system, user):
        if "技术和服务要求逐项响应" in user:
            return (
                "### 逐项响应明细\n本部分对全部技术要求逐条响应：\n"
                "1. 电压等级 10kV——完全满足招标要求，无偏离。\n"
                "2. 抗短路能力 30kA——完全满足招标要求，无偏离。\n"
                "其余条款均完全响应招标文件要求，无负偏离，并提供出厂试验报告作为证明。"
            )
        if "技术" in user or "实施方案" in user:
            return (
                "### 分节一\n本章为技术标正式正文，针对采购范围给出具体实施步骤、技术措施与进度安排，"
                "内容详实，达到给定字数要求，满足招标文件各项要求。\n"
                "### 分节二\n针对重点难点提出对策，明确质量控制与验收标准，确保项目按期高质量交付。"
            )
        return (
            "### 分节一\n本章为商务标正式投标语言正文，逐条响应商务条款并给出明确承诺，"
            "无偏离声明完整，内容详实，达到给定字数要求。"
        )

    monkeypatch.setattr(llm_module.LLMClient, "chat", fake_chat)

    h, pid = _setup(client)
    client.post(
        f"/api/v1/projects/{pid}/requirements/upsert",
        json={
            "requirements": [
                {"req_type": "tech_requirement", "content": "电压等级 10kV", "coordinates": [{"file_id": 1}]},
                {"req_type": "tech_requirement", "content": "抗短路能力 30kA", "coordinates": [{"file_id": 1}]},
            ]
        },
        headers=h,
    )
    client.post(
        f"/api/v1/projects/{pid}/tasks",
        json={
            "task_type": "bid_generate",
            "payload": {"material_ref": "CABLE-YJV-3x95", "cost": 100},
            "idempotency_key": "bg-llm-2",
        },
        headers=h,
    )
    task = _drain_one_task()
    assert task.status == 3
    assert "LLM 全文生成" in task.result["note"]
    assert "business" in task.result["note"] and "technical" in task.result["note"]

    deliverables = client.get(f"/api/v1/deliverables?project_id={pid}", headers=h).json()
    biz = next(d for d in deliverables if d["deliverable_type"] == 1)
    biz_content = client.get(f"/api/v1/deliverables/{biz['deliverable_id']}/content", headers=h).json()
    biz_text = "\n".join(n.get("text", "") for n in biz_content["model"]["nodes"])
    assert "商务条款逐项响应" in biz_text  # 新流程的章节标题
    assert "无偏离声明" in biz_text

    tech = next(d for d in deliverables if d["deliverable_type"] == 2)
    tech_content = client.get(f"/api/v1/deliverables/{tech['deliverable_id']}/content", headers=h).json()
    tech_text = "\n".join(n.get("text", "") for n in tech_content["model"]["nodes"])
    assert "技术方案总体说明" in tech_text
    assert "技术和服务要求逐项响应" in tech_text
    assert "电压等级 10kV" in tech_text
    assert "抗短路能力 30kA" in tech_text
    assert "草稿由 BidVolt 确定性生成" not in tech_text  # 关键回归：不得退回 stub
    assert len(tech_text) >= 100  # 实质正文，而非占位
    assert "##" not in tech_text and "**" not in tech_text  # Issue #12：无 Markdown 残留
