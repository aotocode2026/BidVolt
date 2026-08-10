from __future__ import annotations

import asyncio

import pytest

from app.config import settings
from app.services import llm as llm_module
from app.services.llm import LLMGateClosed, extract_json


def test_gate_closed_by_default():
    assert llm_module.llm_enabled() is False


def test_chat_raises_when_gated(monkeypatch):
    monkeypatch.setattr(settings, "data_classification_confirmed", 0)
    monkeypatch.setattr(settings, "cloud_llm_enabled", 1)
    monkeypatch.setattr(settings, "minimax_api_key", "k")
    with pytest.raises(LLMGateClosed):
        asyncio.run(llm_module.LLMClient().chat("s", "u"))


def test_chat_sends_payload_when_enabled(monkeypatch):
    monkeypatch.setattr(settings, "data_classification_confirmed", 1)
    monkeypatch.setattr(settings, "cloud_llm_enabled", 1)
    monkeypatch.setattr(settings, "minimax_api_key", "test-key")

    captured: dict = {}

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "{\"requirements\": []}"}}]}

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None, headers=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return _FakeResponse()

    monkeypatch.setattr(llm_module.httpx, "AsyncClient", _FakeClient)

    result = asyncio.run(llm_module.LLMClient().chat("system", "user"))
    assert "requirements" in result
    assert captured["url"].endswith("/text/chatcompletion_v2")
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert captured["json"]["model"] == settings.minimax_model


def test_extract_json():
    assert extract_json('说明：\n{"a": 1}\n结束') == {"a": 1}
    with pytest.raises(ValueError):
        extract_json("没有 JSON")
