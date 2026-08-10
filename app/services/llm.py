"""LLM 提供方（P1 门禁：数据分级/客户授权确认前强制关闭）。"""

from __future__ import annotations

import json
import re

import httpx

from app.config import settings


class LLMGateClosed(RuntimeError):
    """云模型未解锁。"""


def llm_enabled() -> bool:
    return (
        settings.data_classification_confirmed == 1
        and settings.cloud_llm_enabled == 1
        and bool(settings.minimax_api_key)
    )


class LLMClient:
    """MiniMax 文本对话客户端（qwen-vl 视觉走 DashScope，另行接入）。"""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> None:
        self.api_key = api_key or settings.minimax_api_key
        self.base_url = (base_url or settings.minimax_base_url).rstrip("/")
        self.model = model or settings.minimax_model

    async def chat(self, system: str, user: str) -> str:
        if not llm_enabled():
            raise LLMGateClosed("数据分级/客户授权未确认，云模型保持关闭（P1 门禁）")
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
        }
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{self.base_url}/text/chatcompletion_v2",
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]


def extract_json(text: str) -> dict:
    """从 LLM 输出中提取第一个 JSON 对象。"""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match is None:
        raise ValueError("LLM 输出中未找到 JSON")
    return json.loads(match.group(0))
