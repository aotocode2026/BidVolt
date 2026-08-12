"""搜索服务（4.9）：DLP 脱敏、域名分级、AnySearch 门禁。"""

from __future__ import annotations

import re
import os
from urllib.parse import urlparse

import httpx

from app.config import settings

TRUST_DOMAIN_L1 = {"gov.cn", "cebpubservice.com", "ndrc.gov.cn"}
TRUST_DOMAIN_L3 = {"zhihu.com", "weibo.com", "toutiao.com"}


def sanitize_query(text: str) -> str:
    """出网前脱敏：手机号/证件号/银行卡/统一社会信用代码。"""
    text = re.sub(r"\d{17}[\dXx]", "[证件号]", text)  # 先长后短，避免手机号误命中
    text = re.sub(r"(?<!\d)\d{16,19}(?!\d)", "[银行卡]", text)
    text = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[电话]", text)
    text = re.sub(r"[0-9A-HJ-NPQRTUWXY]{18}", "[信用代码]", text)
    return text


def trust_level(url: str) -> int:
    host = (urlparse(url).hostname or "").lower()
    if any(host.endswith(d) for d in TRUST_DOMAIN_L1):
        return 1
    if any(host.endswith(d) for d in TRUST_DOMAIN_L3):
        return 3
    return 2


def search_gate_open() -> bool:
    """AnySearch 出网门禁（P1）：数据分级确认 + 开关 + Key。"""
    return (
        settings.data_classification_confirmed == 1
        and settings.search_enabled == 1
        and bool(settings.anysearch_key)
    )


class MockSearchProvider:
    """开发/测试用本地合成结果（不出网）。"""

    provider_id = "mock_search"

    def query(self, query: str, scope: str | None = None) -> list[dict]:
        return [
            {
                "url": f"https://www.gov.cn/bid/example-{abs(hash(query)) % 1000}",
                "title": "政府招标公告示例",
                "snippet": f"{query} 相关政策与标准",
                "trust_level": 1,
            },
            {
                "url": "https://news.example.com/market",
                "title": "行业行情资讯",
                "snippet": f"{query} 市场分析",
                "trust_level": 2,
            },
            {
                "url": "https://zhihu.com/question/1",
                "title": "网友讨论",
                "snippet": "低可信来源，需人工核实",
                "trust_level": 3,
            },
        ]


class AnySearchProvider:
    """AnySearch HTTP 适配器（出网前 DLP 脱敏；门禁未开拒绝调用）。"""

    def query(self, query: str, scope: str | None = None) -> list[dict]:
        if not search_gate_open():
            raise ValueError("搜索门禁关闭（P1）：数据分级未确认或未启用")
        sanitized = sanitize_query(query)
        proxy = settings.http_proxy or os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY")
        client_kwargs: dict = {"base_url": settings.anysearch_base_url, "timeout": 30}
        if proxy:
            client_kwargs["proxy"] = proxy
        with httpx.Client(**client_kwargs) as client:
            resp = client.post(
                "/search",
                json={"query": sanitized, "scope": scope},
                headers={"Authorization": f"Bearer {settings.anysearch_key}"},
            )
            resp.raise_for_status()
            data = resp.json()
        return [
            {
                "url": item["url"],
                "title": item.get("title"),
                "snippet": item.get("snippet"),
                "trust_level": trust_level(item["url"]),
            }
            for item in data.get("results", [])
        ]
