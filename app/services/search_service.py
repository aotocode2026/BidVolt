"""搜索服务（4.9）：DLP 脱敏、域名分级、AnySearch 门禁。"""

from __future__ import annotations

import os
import re
import urllib.request
from urllib.parse import urlparse

import httpx

from app.config import settings

TRUST_DOMAIN_L1 = {"gov.cn", "cebpubservice.com", "ndrc.gov.cn"}
TRUST_DOMAIN_L3 = {"zhihu.com", "weibo.com", "toutiao.com"}

# AnySearch 官方接口：JSON-RPC 2.0 tools/call；匿名额度约 50 次/天，注册 Key 约 1000 次/天
ANYSEARCH_DEFAULT_URL = "https://api.anysearch.com/mcp"
ANYSEARCH_CLIENT_HEADER = "mcp/1.0.0"


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
    """AnySearch 出网门禁（P1）：数据分级确认 + 开关。

    未配置 ANYSEARCH_KEY 时走匿名额度（约 50 次/天，仅限开发/测试）；
    生产正式使用建议配置注册 Key（1000 次/天）。出网前一律经 DLP 脱敏。
    """
    return settings.data_classification_confirmed == 1 and settings.search_enabled == 1


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


def _parse_anysearch_markdown(text: str) -> list[dict]:
    """解析 AnySearch MCP 返回的 Markdown 结果（### N. 标题 / - **URL**: ...）。"""
    results: list[dict] = []
    current: dict | None = None
    for line in text.splitlines():
        m = re.match(r"^\s*###\s+\d+\.\s+(.+)$", line)
        if m:
            if current:
                results.append(current)
            current = {"title": m.group(1).strip(), "url": None, "snippet": ""}
            continue
        if current is None:
            continue
        um = re.match(r"^\s*[-*]\s*\*\*URL\*\*:\s*(.+)$", line)
        if um:
            current["url"] = um.group(1).strip()
            continue
        if line.strip():
            current["snippet"] = (current["snippet"] + "\n" if current["snippet"] else "") + line.strip()
    if current:
        results.append(current)
    return [r for r in results if r.get("url")]


class AnySearchProvider:
    """AnySearch HTTP 适配器（JSON-RPC 2.0；出网前 DLP 脱敏；门禁未开拒绝调用）。

    契约：POST https://api.anysearch.com/mcp，method=tools/call，工具 search；
    有 Key 时带 Authorization: Bearer <key>，无 Key 时匿名（保留 X-Anysearch-Client 头）。
    """

    def query(self, query: str, scope: str | None = None, max_results: int = 5) -> list[dict]:
        if not search_gate_open():
            raise ValueError("搜索门禁关闭（P1）：数据分级未确认或未启用")
        sanitized = sanitize_query(query)
        proxy = settings.http_proxy or os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY")
        endpoint = settings.anysearch_base_url or ANYSEARCH_DEFAULT_URL
        client_kwargs: dict = {"timeout": 30}
        if proxy:
            client_kwargs["proxy"] = proxy
        headers = {"X-Anysearch-Client": ANYSEARCH_CLIENT_HEADER}
        if settings.anysearch_key:
            headers["Authorization"] = f"Bearer {settings.anysearch_key}"
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "search",
                "arguments": {"query": sanitized, "max_results": max(max_results, 1)},
            },
        }
        with httpx.Client(**client_kwargs) as client:
            resp = client.post(endpoint, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        if data.get("error"):
            msg = str(data["error"])
            if "rate" in msg.lower() or "quota" in msg.lower():
                raise ValueError(f"AnySearch 频率/额度受限（匿名约 50 次/天，建议配置 Key）：{msg}")
            raise ValueError(f"AnySearch 调用失败：{msg}")
        result = data.get("result") or {}
        if result.get("isError"):
            raise ValueError(f"AnySearch 返回错误：{result.get('content')}")
        texts = [item.get("text", "") for item in (result.get("content") or []) if item.get("type") == "text"]
        parsed = _parse_anysearch_markdown("\n".join(texts))
        return [
            {
                "url": item["url"],
                "title": item.get("title"),
                "snippet": item.get("snippet"),
                "trust_level": trust_level(item["url"]),
            }
            for item in parsed
        ]


def minimax_search(query: str, limit: int = 10) -> list[dict]:
    """MiniMax 原生联网搜索（POST /v1/coding_plan/search，Bearer 服务端 MINIMAX_API_KEY）。
    全领域开放（产品决定）：行业技术方案、商务写作范例、企业公开信息、政策法规等。
    返回 [{title, link, snippet, date, trust_level}]；来源批注仍走 save_source/link_citation。"""
    import json as _json

    key = os.environ.get("MINIMAX_API_KEY", "").strip()
    if not key:
        raise ValueError("服务端未配置 MINIMAX_API_KEY，MiniMax 搜索不可用")
    sanitized = sanitize_query(query)
    payload = _json.dumps({"q": sanitized}).encode("utf-8")
    req = urllib.request.Request(
        "https://api.minimaxi.com/v1/coding_plan/search",
        data=payload,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = _json.loads(resp.read() or b"{}")
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"MiniMax 搜索失败：{exc}") from exc
    if (data.get("base_resp") or {}).get("status_code") not in (0, None):
        raise ValueError(f"MiniMax 搜索失败：{data.get('base_resp')}")
    out = []
    for r in (data.get("organic") or [])[: max(int(limit), 1)]:
        url = str(r.get("link") or "")
        out.append(
            {
                "url": url,
                "title": str(r.get("title") or ""),
                "snippet": (str(r.get("snippet") or ""))[:600],
                "date": str(r.get("date") or ""),
                "trust_level": trust_level(url),
            }
        )
    return out
