from __future__ import annotations

from app.config import settings
from app.services import search_service
from app.services.search_service import (
    AnySearchProvider,
    _parse_anysearch_markdown,
    sanitize_query,
    search_gate_open,
    trust_level,
)


def test_sanitize_query_masks_pii():
    dirty = "联系人 13812345678，证件 110101199001011234，卡 6222021234567890"
    clean = sanitize_query(dirty)
    assert "13812345678" not in clean
    assert "110101199001011234" not in clean
    assert "6222021234567890" not in clean
    assert "[电话]" in clean and "[证件号]" in clean and "[银行卡]" in clean


def test_trust_level_domains():
    assert trust_level("https://www.gov.cn/x") == 1
    assert trust_level("https://news.example.com/x") == 2
    assert trust_level("https://zhihu.com/question/1") == 3


def test_anysearch_sanitizes_before_send(monkeypatch):
    monkeypatch.setattr(settings, "data_classification_confirmed", 1)
    monkeypatch.setattr(settings, "search_enabled", 1)
    monkeypatch.setattr(settings, "anysearch_key", "as-key")
    monkeypatch.setattr(settings, "anysearch_base_url", "https://search.example.com/mcp")
    captured: dict = {}

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": "## Search Results\n\n### 1. 公告\n- **URL**: https://www.gov.cn/bid/1\n招标正文",
                        }
                    ]
                },
            }

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, json=None, headers=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return _FakeResponse()

    monkeypatch.setattr(search_service.httpx, "Client", _FakeClient)
    results = AnySearchProvider().query("联系人 13812345678 电缆")
    assert captured["url"] == "https://search.example.com/mcp"
    assert captured["json"]["method"] == "tools/call"
    assert captured["json"]["params"]["name"] == "search"
    assert captured["json"]["params"]["arguments"]["query"] == "联系人 [电话] 电缆"
    assert captured["json"]["params"]["arguments"]["max_results"] == 5
    assert captured["headers"]["Authorization"] == "Bearer as-key"
    assert captured["headers"]["X-Anysearch-Client"] == "mcp/1.0.0"
    assert results[0]["trust_level"] == 1
    assert results[0]["title"] == "公告"


def test_anysearch_gate_closed_raises(monkeypatch):
    monkeypatch.setattr(settings, "data_classification_confirmed", 0)
    import pytest

    with pytest.raises(ValueError, match="门禁关闭"):
        AnySearchProvider().query("x")


def test_anysearch_anonymous_mode_no_auth_header(monkeypatch):
    """无 Key 时门禁仍可开（匿名额度），且不发送 Authorization 头。"""
    monkeypatch.setattr(settings, "data_classification_confirmed", 1)
    monkeypatch.setattr(settings, "search_enabled", 1)
    monkeypatch.setattr(settings, "anysearch_key", "")
    monkeypatch.setattr(settings, "anysearch_base_url", "https://search.example.com/mcp")
    captured: dict = {}

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"result": {"content": []}}

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, json=None, headers=None):
            captured["headers"] = headers
            return _FakeResponse()

    monkeypatch.setattr(search_service.httpx, "Client", _FakeClient)
    assert search_gate_open()
    AnySearchProvider().query("电缆 中标价")
    assert "Authorization" not in captured["headers"]
    assert captured["headers"]["X-Anysearch-Client"] == "mcp/1.0.0"


def test_anysearch_gate_closed_without_enable(monkeypatch):
    monkeypatch.setattr(settings, "data_classification_confirmed", 1)
    monkeypatch.setattr(settings, "search_enabled", 0)
    monkeypatch.setattr(settings, "anysearch_key", "as-key")
    import pytest

    with pytest.raises(ValueError, match="门禁关闭"):
        AnySearchProvider().query("x")


def test_parse_anysearch_markdown():
    text = (
        "## Search Results (3 results)\n\n"
        "### 1. 招标公告标题\n"
        "- **URL**: https://www.gov.cn/bid/1\n"
        "第一条摘要\n"
        "\n"
        "### 2. 行业资讯\n"
        "- **URL**: https://news.example.com/m\n"
        "第二条摘要\n"
    )
    items = _parse_anysearch_markdown(text)
    assert len(items) == 2
    assert items[0]["title"] == "招标公告标题"
    assert items[0]["url"] == "https://www.gov.cn/bid/1"
    assert "第一条摘要" in items[0]["snippet"]


def test_anysearch_uses_proxy_when_configured(monkeypatch):
    monkeypatch.setattr(settings, "data_classification_confirmed", 1)
    monkeypatch.setattr(settings, "search_enabled", 1)
    monkeypatch.setattr(settings, "anysearch_key", "as-key")
    monkeypatch.setattr(settings, "anysearch_base_url", "https://search.example.com/mcp")
    monkeypatch.setattr(settings, "http_proxy", "http://proxy.internal:3128")
    captured: dict = {}

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"result": {"content": []}}

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            captured["proxy"] = kwargs.get("proxy")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, json=None, headers=None):
            return _FakeResponse()

    monkeypatch.setattr(search_service.httpx, "Client", _FakeClient)
    AnySearchProvider().query("电缆")
    assert captured["proxy"] == "http://proxy.internal:3128"
