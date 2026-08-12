from __future__ import annotations

from app.config import settings
from app.services import search_service
from app.services.search_service import AnySearchProvider, sanitize_query, trust_level


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
    monkeypatch.setattr(settings, "anysearch_base_url", "https://search.example.com/v1")
    captured: dict = {}

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"results": [{"url": "https://www.gov.cn/bid/1", "title": "公告"}]}

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
    assert captured["json"]["query"] == "联系人 [电话] 电缆"
    assert captured["headers"]["Authorization"] == "Bearer as-key"
    assert results[0]["trust_level"] == 1


def test_anysearch_gate_closed_raises(monkeypatch):
    monkeypatch.setattr(settings, "data_classification_confirmed", 0)
    import pytest

    with pytest.raises(ValueError, match="门禁关闭"):
        AnySearchProvider().query("x")


def test_anysearch_uses_proxy_when_configured(monkeypatch):
    monkeypatch.setattr(settings, "data_classification_confirmed", 1)
    monkeypatch.setattr(settings, "search_enabled", 1)
    monkeypatch.setattr(settings, "anysearch_key", "as-key")
    monkeypatch.setattr(settings, "anysearch_base_url", "https://search.example.com/v1")
    monkeypatch.setattr(settings, "http_proxy", "http://proxy.internal:3128")
    captured: dict = {}

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"results": [{"url": "https://www.gov.cn/bid/1", "title": "公告"}]}

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
