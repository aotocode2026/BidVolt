"""真实 MiniMax 连通性冒烟（本地临时开启门禁，合成非敏感文本）。"""

from __future__ import annotations

import asyncio

from app.config import settings
from app.services.llm import LLMClient, llm_enabled


async def main() -> None:
    settings.data_classification_confirmed = 1
    settings.cloud_llm_enabled = 1
    print("llm_enabled:", llm_enabled())
    reply = await LLMClient().chat(
        system="你是测试助手，用一句话回答。",
        user="请确认你能正常工作，回复：连接成功。",
    )
    print("reply:", reply[:200])


if __name__ == "__main__":
    asyncio.run(main())
