"""ReviewProvider 注册表与契约实现（A-9，D-G）。

统一契约：输入 provider_version + 冻结快照信息，输出 review_items（含 evidence/raw hash）。
V1 内置 evaluate 使用 code 类型（确定性完整性检查）；document 与 api 类型提供可插拔实现与契约测试。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ProviderResult:
    provider_code: str
    provider_version: str
    review_run_id: str
    items: list[dict] = field(default_factory=list)
    raw: Any = None

    @property
    def raw_hash(self) -> str:
        return hashlib.sha256(
            json.dumps(self.raw, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()


class DocumentProvider:
    """Document Provider：版本化规则文档 → 确定性规则引擎执行。"""

    def __init__(self, rules: list[dict], version: str = "1.0.0"):
        self.rules = rules
        self.version = version

    def run(self, inputs: dict) -> ProviderResult:
        items = []
        for rule in self.rules:
            rule_type = rule.get("type")
            field_name = rule.get("field")
            required = rule.get("required")
            rule_version = rule.get("version", self.version)
            if rule_type == "field_required":
                value = inputs.get("fields", {}).get(field_name)
                ok = value not in (None, "", [])
                items.append(
                    {
                        "category": rule.get("category", "完整性"),
                        "problem_description": (
                            f"缺少{field_name}" if not ok else f"{field_name}已提供"
                        ),
                        "got": rule.get("full", 10.0) if ok else 0.0,
                        "full": rule.get("full", 10.0),
                        "improvable": 0.0 if ok else rule.get("full", 10.0),
                        "risk_level": 0 if ok else rule.get("risk", 2),
                        "suggestion": None if ok else f"请补充{field_name}",
                        "action_type": "upload_material" if not ok else "manual_review",
                        "ruleset_version": rule_version,
                        "evidence": {"claim_id": rule.get("claim_id"), "source_version_id": None},
                    }
                )
        return ProviderResult(
            provider_code="doc_rules",
            provider_version=self.version,
            review_run_id="doc-" + hashlib.sha256(json.dumps(self.rules, sort_keys=True).encode()).hexdigest()[:12],
            items=items,
            raw={"rules": self.rules, "inputs": inputs},
        )


class ApiProvider:
    """API Provider：外部评分服务适配器（认证/超时/重试；V1 用 Mock Server 契约测试）。"""

    def __init__(self, base_url: str, api_key: str = "", timeout: float = 5.0, retries: int = 2):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.retries = retries

    def run(self, inputs: dict) -> ProviderResult:
        import httpx

        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
                resp = httpx.post(
                    f"{self.base_url}/review",
                    json={"provider_version": "1.0.0", "inputs": inputs},
                    headers=headers,
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                payload = resp.json()
                return ProviderResult(
                    provider_code="api_external",
                    provider_version=payload.get("provider_version", "1.0.0"),
                    review_run_id=payload.get("review_run_id", "api-" + str(attempt)),
                    items=payload.get("items", []),
                    raw=payload,
                )
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
        raise RuntimeError(f"API Provider 调用失败（重试 {self.retries} 次）：{last_exc}") from last_exc
