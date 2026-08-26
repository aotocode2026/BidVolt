"""行情库解析与数据源链回归（无 Mock：样本不足如实报）。"""
from __future__ import annotations

import asyncio

from app.services.history_library import (
    normalize_price_mode,
    parse_amount,
    parse_market_xlsx,
    parse_win_value,
)
from app.services.history_provider import AnySearchHistoryPriceProvider


def test_anysearch_provider_returns_empty_without_llm(monkeypatch):
    async def run():
        return await AnySearchHistoryPriceProvider().get_material_samples("CABLE-YJV-3x95")

    # 门禁关闭（默认测试环境）→ 空样本（不再 Mock 兜底）
    assert asyncio.run(run()) == []


def test_parse_market_xlsx_standard_format():
    import io

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "2026-01"
    header = [
        "序号", "发布时间", "阶段", "项目状态", "公告/项目名称", "发布单位", "分标/品类", "包号",
        "包/项目名称", "限价", "报价方式", "限价证据原文", "限价证据链接", "限价官网链接",
        "中标价", "中标价证据原文", "中标价证据链接", "中标价官网链接", "备注", "公告ID",
    ]
    ws.append(["使用说明（多行占位）"])
    ws.append(header)
    ws.append([
        1, "2026-03-30 00:00:00", "采购公告", "已截止", "国网湖北某批次采购", "国网湖北电力",
        "食堂餐饮运营服务 / CY1526SJJ01-9013003-1001", "1", "某公司2026-2028年食堂餐饮运营服务采购",
        "166 万元", "固定总价", "Word:表2 第2行", "https://sgccetp.com.cn/ecpwcmcore/index/",
        "https://sgccetp.com.cn/portal/#/", "162 万元", "价格字段：成交金额（万元）",
        "CY1526SJJ01-9013", "https://sgccetp.com.cn/portal/#/", "限价口径：明确限价", "ID:2026033099839674",
    ])
    ws.append([
        2, "2026-03-30 00:00:00", "采购公告", "已截止", "国网湖北某批次采购", "国网湖北电力",
        "视频拍摄及制作服务", "1", "某公司2026-2027年视频拍摄及制作服务框架采购",
        "折扣率不大于100%即≤100%（比例）", "折扣率", "Word:表2 第4行", "https://sgccetp.com.cn/ecpwcmcore/index/",
        "https://sgccetp.com.cn/portal/#/", "90%", "价格字段：折扣率（%）",
        "CY1526SJJCK1-9011", "https://sgccetp.com.cn/portal/#/", "限价口径：明确限价", "ID:2026033099841299",
    ])
    ws.append([3, "坏行", "x", "x", "x", "x", "x", "x", "x", "x", "x", "x", "x", "x", None, "x", "x", "x", "x", "ID:bad"])
    buf = io.BytesIO()
    wb.save(buf)

    rows, skipped = parse_market_xlsx(buf.getvalue())
    assert len(rows) == 2, rows
    r1, r2 = rows
    assert r1["price_mode"] == "固定总价" and r1["limit_price"] == 166.0 and r1["win_price"] == 162.0
    assert r1["publisher"] == "国网湖北电力" and r1["notice_id"] == "ID:2026033099839674"
    assert r2["price_mode"] == "折扣率" and r2["limit_price"] is None and r2["win_price"] == 90.0
    assert any("ID:bad" in s for s in skipped)


def test_parse_helpers():
    assert normalize_price_mode("金额总价报价") == "固定总价"
    assert normalize_price_mode("折扣率不大于100%即≤100%（比例）") == "折扣率"
    assert normalize_price_mode("未披露") == "未披露"
    assert parse_amount("163.05 万元") == 163.05
    assert parse_amount("折扣率不大于100%即≤100%（比例）") is None
    assert parse_win_value("90%") == (90.0, True)
    assert parse_win_value("162 万元") == (162.0, False)
