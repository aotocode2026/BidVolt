"""LLM 提供方（P1 门禁：数据分级/客户授权确认前强制关闭）。"""

from __future__ import annotations

import base64
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


def vl_enabled() -> bool:
    """视觉模型（qwen-vl / DashScope）门禁。"""
    return (
        settings.data_classification_confirmed == 1
        and settings.cloud_llm_enabled == 1
        and bool(settings.dashscope_api_key)
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
            # MiniMax-Text-01 上限 40000；设 8000 控制大输入下的生成时长与成本
            "max_tokens": 8000,
        }
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{self.base_url}/text/chatcompletion_v2",
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]


class DashScopeVLClient:
    """百炼 qwen-vl 视觉理解（OpenAI 兼容模式）。"""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> None:
        self.api_key = api_key or settings.dashscope_api_key
        self.base_url = (base_url or settings.dashscope_base_url).rstrip("/")
        self.model = model or settings.dashscope_vl_model

    async def describe(
        self,
        image_bytes: bytes,
        mime: str = "image/png",
        prompt: str = "识别图中全部文字与关键内容。",
        *,
        model: str | None = None,
        high_res: bool = False,
    ) -> str:
        if not vl_enabled():
            raise LLMGateClosed("数据分级/客户授权未确认，视觉模型关闭（P1 门禁）")
        b64 = base64.b64encode(image_bytes).decode("ascii")
        payload = {
            "model": model or self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                    ],
                }
            ],
        }
        # qwen2.5-vl 系列：让模型自行把图切成高分辨率块逐块识别（编号二次识别用）；
        # 旧模型/端点不识别的参数会 400，调用方自行降级重试
        if high_res:
            payload["vl_high_resolution_images"] = True
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]


def _first_json_object(text: str) -> str | None:
    """提取首个完整 JSON 对象（花括号配平，兼容字符串内的花括号）。"""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def extract_json(text: str) -> dict | list:
    """从 LLM 输出中提取 JSON（兼容 ```json 围栏、前后说明文字、多个 JSON 片段）。"""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    first = _first_json_object(text)
    if first is None:
        raise ValueError("LLM 输出中未找到 JSON")
    try:
        return json.loads(first)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM 输出 JSON 解析失败：{exc}") from exc


def try_extract_json(text: str) -> dict | list | None:
    """extract_json 的容错版：输出中没有可解析 JSON 时返回 None，由调用方决定降级策略
    （解析任务各抽取环节不应因模型一次输出格式抖动而整体失败）。"""
    try:
        return extract_json(text)
    except ValueError:
        return None
