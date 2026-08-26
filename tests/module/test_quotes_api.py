from __future__ import annotations

import asyncio
import io
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.auth import AppUser
from app.models.quote import HistoryPriceSnapshot

TEST_DB = "./.test_bidvolt.db"


def _setup(client, email: str = "q@test.com", enterprise: str = "测试企业"):
    r = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "Abc12345", "enterprise_name": enterprise},
    )
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    eid = client.get("/api/v1/auth/me", headers=headers).json()["enterprise_id"]
    pid = client.post("/api/v1/projects", json={"name": "P"}, headers=headers).json()["project_id"]
    did = client.post(
        "/api/v1/deliverables",
        json={"project_id": pid, "deliverable_type": 3, "title": "报价单"},
        headers=headers,
    ).json()["deliverable_id"]
    return headers, pid, did, eid


def _seed_library(enterprise_id: int, n: int = 8, name: str = "CABLE-YJV-3x95 电力电缆"):
    """向行情库播种（enterprise_id=0 → 公共库；否则私有库）。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{TEST_DB}")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def run():
        async with factory() as session:
            for i in range(n):
                session.add(
                    HistoryPriceSnapshot(
                        enterprise_id=enterprise_id,
                        provider_id="history_price_library",
                        material_name=name,
                        category="电缆 / 物资",
                        package_name=f"{name}框架采购",
                        price_mode="固定总价",
                        limit_price=130.0,
                        win_price=round(115.0 + i * 1.2, 2),
                        win_date=date(2026, 6, 1),
                        publish_date=date(2026, 5, 1),
                        notice_id=f"ID:{enterprise_id}:{i}",
                        publisher="国网测试电力",
                        source_hash=f"h{i}",
                    )
                )
            await session.commit()

    asyncio.run(run())
    asyncio.run(engine.dispose())


def _seed_public():
    _seed_library(0)


def _market_xlsx_bytes() -> bytes:
    """构造「标黄提取」格式的限价↔中标价 xlsx（表头自定位）。"""
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(
        [
            "公告ID", "公告/项目名称", "发布单位", "分标/品类", "包/项目名称",
            "限价", "报价方式", "中标价", "发布时间", "限价证据原文", "中标价证据原文",
        ]
    )
    ws.append(
        ["N-1001", "某批物资采购", "国网华东测试", "物资/电缆", "CABLE-YJV-3x95 框架采购",
         "120 万元", "固定总价", "110.5 万元", "2026-07-01", "限价原文A", "中标原文A"]
    )
    ws.append(
        ["N-1002", "某批物资采购", "国网华东测试", "服务/运维", "运维框架",
         "200 万元", "固定总价", "189 万元", "2026-07-02", "限价原文B", "中标原文B"]
    )
    ws.append(
        ["N-1003", "某批折扣采购", "国网华东测试", "服务/运维", "折扣框架",
         "100", "折扣率", "92%", "2026-07-03", "限价原文C", "中标原文C"]
    )
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_apply_rejects_price_over_tender_limit(client):
    """举一反三：招标限价（报价规则 structured.price_limit）必须约束报价应用。"""
    h, pid, did, _ = _setup(client)
    _seed_public()
    client.post(
        f"/api/v1/projects/{pid}/requirements/upsert",
        json={"requirements": [
            {"req_type": "quote_rule", "content": "报价限价 50 万元",
             "structured": {"price_limit": {"amount": 50, "unit": "万元"}},
             "coordinates": [{"file_id": 1}]},
        ]},
        headers=h,
    )
    calc = client.post(
        "/api/v1/quotes/calculate",
        json={"material_ref": "CABLE-YJV-3x95", "cost": 100, "min_profit_rate": 0.1,
              "unit": "万元", "project_id": pid},
        headers=h,
    )
    calc_id = calc.json()["calc_id"]
    applied = client.post(
        "/api/v1/quotes/apply",
        json={"calc_id": calc_id, "deliverable_id": did, "expected_version_no": 0},
        headers=h,
    )
    assert applied.status_code == 422
    assert "限价" in applied.json()["detail"]


def test_history_library_query_contract(client):
    """行情库联合查询：公共+私有可见、逐条样本+分组聚合、口径标注、非只读阻塞（可共建导入）。"""
    h, _, _, eid = _setup(client)
    _seed_public()
    _seed_library(eid, n=3, name="企业私有样本")

    r = client.get("/api/v1/quotes/history", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["readonly"] is False  # 支持共建导入，不再只读
    assert body["sample_count"] >= 5
    assert "public" in [s["source"] for s in body["samples"]]
    assert any(s["source"] == "private" for s in body["samples"])
    assert body["stats"]  # 按报价方式分组聚合
    for s in body["samples"]:
        assert "limit_price" in s and "win_price" in s and "price_mode" in s

    # scope 过滤
    pub = client.get("/api/v1/quotes/history", params={"scope": "public"}, headers=h).json()
    assert pub["sample_count"] >= 5
    assert all(s["source"] == "public" for s in pub["samples"])
    priv = client.get("/api/v1/quotes/history", params={"scope": "private"}, headers=h).json()
    assert priv["sample_count"] == 3
    assert all(s["source"] == "private" for s in priv["samples"])

    # price_mode 过滤
    fixed = client.get("/api/v1/quotes/history", params={"price_mode": "固定总价"}, headers=h).json()
    assert fixed["sample_count"] >= 1
    assert all(s["price_mode"] == "固定总价" for s in fixed["samples"])


def test_history_import_public_builds_shared_library(client):
    """共建导入：上传「标黄提取」格式 xlsx 入公共库，随后可查询到（含折扣率行正确归一）。"""
    h, _, _, _ = _setup(client)
    r = client.post(
        "/api/v1/quotes/history/import",
        files={"file": ("market.xlsx", _market_xlsx_bytes(),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"target": "public"},
        headers=h,
    )
    assert r.status_code == 201
    body = r.json()
    assert body["imported"] == 3
    assert body["scope"] == "public"

    q = client.get("/api/v1/quotes/history", params={"scope": "public"}, headers=h).json()
    assert q["sample_count"] == 3
    assert all(s["source"] == "public" for s in q["samples"])

    # 重复导入同一文件：按 source_hash 去重，不产生重复行
    r2 = client.post(
        "/api/v1/quotes/history/import",
        files={"file": ("market.xlsx", _market_xlsx_bytes(),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"target": "public"},
        headers=h,
    )
    assert r2.status_code == 201
    assert r2.json()["imported"] == 0
    assert r2.json()["skipped"] == 3
    assert client.get("/api/v1/quotes/history", params={"scope": "public"}, headers=h).json()["sample_count"] == 3

    by_notice = {s["notice_id"]: s for s in q["samples"]}
    assert by_notice["N-1001"]["win_price"] == "110.5"
    assert by_notice["N-1001"]["limit_price"] == "120.0"
    assert by_notice["N-1001"]["win_ratio"] is not None
    assert by_notice["N-1003"]["price_mode"] == "折扣率"
    assert by_notice["N-1003"]["win_price"] == "92.0"
    assert by_notice["N-1003"]["limit_price"] is None  # 折扣率行限价不落金额
    assert by_notice["N-1003"]["win_ratio"] is None  # 折扣率不参与金额比值


def test_history_private_import_tenant_isolated(client):
    """私有库租户隔离：A 导入私有样本，B 查不到；B 导入只入 B 私有库；公共库双方可见。"""
    h_a, _, _, _ = _setup(client)
    r = client.post(
        "/api/v1/quotes/history/import",
        files={"file": ("market.xlsx", _market_xlsx_bytes(),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"target": "private"},
        headers=h_a,
    )
    assert r.status_code == 201

    h_b, _, _, _ = _setup(client, email="q2@test.com", enterprise="测试企业B")  # 第二个企业
    q_b = client.get("/api/v1/quotes/history", params={"scope": "all"}, headers=h_b).json()
    assert q_b["sample_count"] == 0  # A 的私有样本对 B 不可见

    r_b = client.post(
        "/api/v1/quotes/history/import",
        files={"file": ("market2.xlsx", _market_xlsx_bytes(),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"target": "public"},
        headers=h_b,
    )
    assert r_b.status_code == 201
    q_b2 = client.get("/api/v1/quotes/history", params={"scope": "public"}, headers=h_b).json()
    assert q_b2["sample_count"] == 3  # 公共库共建，全平台可见
    q_a = client.get("/api/v1/quotes/history", params={"scope": "public"}, headers=h_a).json()
    assert q_a["sample_count"] == 3  # A 同样能看到 B 共建的公共库


def test_calculate_strategies_ai_suggest_and_apply(client):
    h, pid, did, _ = _setup(client)
    _seed_public()
    calc = client.post(
        "/api/v1/quotes/calculate",
        json={
            "material_ref": "CABLE-YJV-3x95",
            "cost": 100,
            "min_profit_rate": 0.1,
            "unit": "万元",
            "project_id": pid,
            "adjustments": {"region": 0.01},
        },
        headers=h,
    )
    assert calc.status_code == 200
    calc_id = calc.json()["calc_id"]
    assert calc.json()["result"]["sample_count"] >= 5
    assert calc.json()["result"]["suggested"] >= calc.json()["result"]["min_price"]

    win = client.post("/api/v1/quotes/strategies", json={"calc_id": calc_id, "strategy": "win"}, headers=h)
    assert win.status_code == 200
    assert win.json()["strategy"] == "win"

    no_basis = client.post("/api/v1/quotes/ai-suggest", json={"calc_id": calc_id}, headers=h)
    assert no_basis.json()["unavailable"] is True

    with_basis = client.post(
        "/api/v1/quotes/ai-suggest", json={"calc_id": calc_id, "basis": "华东区电缆中标样本"}, headers=h
    )
    assert with_basis.json()["is_ai_suggest"] is True
    assert len(with_basis.json()["price_range"]) == 2

    applied = client.post(
        "/api/v1/quotes/apply",
        json={"calc_id": calc_id, "deliverable_id": did, "expected_version_no": 0, "note": "确认"},
        headers=h,
    )
    assert applied.status_code == 201
    assert applied.json()["new_version_no"] == 1

    again = client.post(
        "/api/v1/quotes/apply",
        json={"calc_id": calc_id, "deliverable_id": did, "expected_version_no": 1},
        headers=h,
    )
    assert again.status_code == 409

    versions = client.get(f"/api/v1/deliverables/{did}/versions", headers=h)
    assert versions.json()[0]["version_type"] == 5  # 报价应用


def test_apply_validates_project_and_deliverable_type(client):
    h, pid, _, _ = _setup(client)
    _seed_public()
    calc_id = client.post(
        "/api/v1/quotes/calculate",
        json={"material_ref": "CABLE-YJV-3x95", "cost": 100, "min_profit_rate": 0.1,
              "unit": "万元", "project_id": pid},
        headers=h,
    ).json()["calc_id"]

    # 非报价单成果 → 409
    biz = client.post(
        "/api/v1/deliverables",
        json={"project_id": pid, "deliverable_type": 1, "title": "商务标"},
        headers=h,
    ).json()["deliverable_id"]
    wrong_type = client.post(
        "/api/v1/quotes/apply",
        json={"calc_id": calc_id, "deliverable_id": biz, "expected_version_no": 0},
        headers=h,
    )
    assert wrong_type.status_code == 409

    # 另一项目的报价单 → 409
    pid2 = client.post("/api/v1/projects", json={"name": "P2"}, headers=h).json()["project_id"]
    other_quote = client.post(
        "/api/v1/deliverables",
        json={"project_id": pid2, "deliverable_type": 3, "title": "报价单"},
        headers=h,
    ).json()["deliverable_id"]
    wrong_project = client.post(
        "/api/v1/quotes/apply",
        json={"calc_id": calc_id, "deliverable_id": other_quote, "expected_version_no": 0},
        headers=h,
    )
    assert wrong_project.status_code == 409


def test_apply_requires_quote_apply_permission(client):
    h, pid, did, _ = _setup(client)
    _seed_public()
    calc = client.post(
        "/api/v1/quotes/calculate",
        json={"material_ref": "CABLE-YJV-3x95", "cost": 100, "min_profit_rate": 0.1,
              "unit": "万元", "project_id": pid},
        headers=h,
    ).json()["calc_id"]

    # 直接改库：把用户权限降级为只读（不包含 quote.apply）
    engine = create_async_engine(f"sqlite+aiosqlite:///{TEST_DB}")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def strip_permission():
        async with factory() as session:
            user = await session.scalar(select(AppUser).where(AppUser.email == "q@test.com"))
            user.permissions = ["file.read"]
            await session.commit()

    asyncio.run(strip_permission())
    engine.sync_engine.dispose()

    r = client.post(
        "/api/v1/quotes/apply",
        json={"calc_id": calc, "deliverable_id": did, "expected_version_no": 0},
        headers=h,
    )
    assert r.status_code == 403


def test_recalc_from_frozen_snapshot(client):
    """A-8：任一建议价可按冻结样本 + 参数 + 算法版本复算，结果一致。"""
    h, pid, did, _ = _setup(client)
    _seed_public()
    calc = client.post(
        "/api/v1/quotes/calculate",
        json={"material_ref": "CABLE-YJV-3x95", "cost": 100, "min_profit_rate": 0.1,
              "unit": "万元", "project_id": pid},
        headers=h,
    ).json()
    calc_id = calc["calc_id"]
    original = calc["result"]["suggested"]

    recalc = client.post("/api/v1/quotes/recalc", json={"calc_id": calc_id}, headers=h)
    assert recalc.status_code == 200
    body = recalc.json()
    assert body["matches_original"] is True
    assert body["recalc"]["suggested"] == original
    assert body["engine_version"]
