"""模拟评标与提升闭环（4.8）：确定性内置 Code Provider + review_item 主模型。

真实评分闭环（discussion #53）：`substantive` 按招标评分细则对正式成果做实质评审
（服务端 LLM 引擎或 Agent 提交），`builtin` 仅为内部完整性自检，不进入前端评分卡。
"""

from __future__ import annotations

import asyncio
import json
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import AgentArtifact
from app.models.deliverable import Deliverable
from app.models.project_material import ProjectSnapshot
from app.models.requirement import Requirement
from app.models.review import (
    ReviewItem,
    ReviewProvider,
    ReviewRun,
    ScoreRecord,
)
from app.services import deliverable_service
from app.services.task_service import TerminalTaskError

RULESET_VERSION = "builtin-code-1.0"
DELIVERABLE_NAMES = {1: "商务标", 2: "技术标", 3: "报价单"}


async def _project_artifact_versions(
    session: AsyncSession, enterprise_id: int, project_id: int
) -> dict[int, int]:
    """当前正式 artifact 的版本映射（artifact_id -> version_no，issue #22）。"""
    artifacts = (
        await session.scalars(
            select(AgentArtifact).where(
                AgentArtifact.enterprise_id == enterprise_id,
                AgentArtifact.project_id == project_id,
            )
        )
    ).all()
    return {int(a.id): int(a.version_no) for a in artifacts}


async def ensure_builtin_provider(session: AsyncSession, enterprise_id: int) -> ReviewProvider:
    provider = await session.scalar(
        select(ReviewProvider).where(
            ReviewProvider.provider_code == "builtin_completeness",
            ReviewProvider.enterprise_id == enterprise_id,
        )
    )
    if provider is None:
        provider = ReviewProvider(
            enterprise_id=enterprise_id,
            provider_type="code",
            provider_code="builtin_completeness",
            provider_version="1.0.0",
            name="成果完整性检查（内置）",
            category="integrity",
            severity=2,
            enabled=True,
        )
        session.add(provider)
        try:
            # 保存点隔离：并发请求同时创建同企业 Provider 时，唯一约束冲突只回滚本次 INSERT
            async with session.begin_nested():
                await session.flush()
        except IntegrityError:
            # 冲突说明另一并发请求已建好：回滚保存点后重查（租户内 code 唯一，结果必属于本企业）
            provider = await session.scalar(
                select(ReviewProvider).where(
                    ReviewProvider.provider_code == "builtin_completeness",
                    ReviewProvider.enterprise_id == enterprise_id,
                )
            )
            if provider is None:
                raise
    return provider


def _claim_id(rule: str, dtype: int) -> str:
    return f"builtin-{rule}-{dtype}"


async def run_evaluation(
    session: AsyncSession,
    *,
    enterprise_id: int,
    project_id: int,
    provider_id: int | None = None,
) -> dict:
    """生成 project_snapshot + review_run + score_record + review_items（初始 pending_confirm）。

    provider_id（Issue #6 P0）：未传则使用企业内置 Provider；传入则校验
    （必须属于本企业、已启用、当前仅支持 builtin_completeness 引擎），
    非法/禁用/跨租户/不支持的引擎一律失败关闭（ValueError，由 API 映射 404/422）。
    评审冻结 Provider 版本与配置身份：provider_code/version 计入快照与 raw_hash。
    """
    if provider_id is None:
        provider = await ensure_builtin_provider(session, enterprise_id)
    else:
        provider = await session.scalar(
            select(ReviewProvider).where(
                ReviewProvider.id == provider_id,
                ReviewProvider.enterprise_id == enterprise_id,
            )
        )
        if provider is None:
            raise ValueError("provider_not_found")
        if not provider.enabled:
            raise ValueError("provider_disabled")
        if provider.provider_code != "builtin_completeness":
            raise ValueError("provider_unsupported")
    deliverables = (
        await session.scalars(
            select(Deliverable).where(
                Deliverable.enterprise_id == enterprise_id,
                Deliverable.project_id == project_id,
            )
        )
    ).all()
    existing_types = {d.deliverable_type for d in deliverables}
    artifact_versions = await _project_artifact_versions(session, enterprise_id, project_id)
    input_refs = {
        "deliverable_versions": {d.id: d.current_version_no for d in deliverables},
        "artifact_versions": artifact_versions,
        "ruleset": RULESET_VERSION,
        "provider_code": provider.provider_code,
        "provider_version": provider.provider_version,
    }
    snapshot = ProjectSnapshot(
        enterprise_id=enterprise_id,
        project_id=project_id,
        snapshot_type="review",
        input_refs=input_refs,
        rules_version={"ruleset": RULESET_VERSION, "provider_code": provider.provider_code},
    )
    session.add(snapshot)
    await session.flush()

    items_data: list[dict] = []
    for dtype, name in DELIVERABLE_NAMES.items():
        if dtype in existing_types:
            items_data.append(
                {
                    "category": "完整性",
                    "problem_description": f"{name}已生成",
                    "got": 10.0,
                    "full": 10.0,
                    "improvable": 0.0,
                    "risk_level": 0,
                    "suggestion": None,
                    "action_type": "manual_review",
                    "missing_material_types": None,
                    "evidence": {
                        "claim_id": _claim_id("integrity", dtype),
                        "source_version_id": input_refs["deliverable_versions"].get(dtype),
                        "content_hash": None,
                        "source_range": None,
                        "exact_quote": None,
                    },
                }
            )
        else:
            items_data.append(
                {
                    "category": "完整性",
                    "problem_description": f"缺少{name}",
                    "got": 0.0,
                    "full": 10.0,
                    "improvable": 10.0,
                    "risk_level": 2,
                    "suggestion": f"请上传{name}",
                    "action_type": "upload_material",
                    "missing_material_types": name,
                    "evidence": {
                        "claim_id": _claim_id("integrity", dtype),
                        "source_version_id": None,
                        "content_hash": None,
                        "source_range": None,
                        "exact_quote": None,
                    },
                }
            )

    # 评分细则权重化评审引擎（路线图项）：解析出的评分细则（structured.score_rule.weight/criterion）
    # 逐项打分：细则在技术标/商务标正文中体现 → 得满分；未体现 → 0 分并给出建议。
    score_rule_reqs = (
        await session.scalars(
            select(Requirement).where(
                Requirement.enterprise_id == enterprise_id,
                Requirement.project_id == project_id,
                Requirement.current.is_(True),
                Requirement.req_type == "score_rule",
            )
        )
    ).all()
    score_rule_stats = {"count": 0, "weight_total": 0.0, "weight_got": 0.0, "missed": 0}
    if score_rule_reqs:
        doc_texts: dict[int, str] = {}
        for d in deliverables:
            if d.current_version_no == 0:
                continue
            try:
                _, m = await deliverable_service.get_version_content(session, d.id, d.current_version_no)
            except Exception:  # noqa: BLE001
                m = {}
            doc_texts[d.deliverable_type] = "\n".join(
                n.get("text", "") for n in (m or {}).get("nodes", [])
            )
        for r in score_rule_reqs:
            structured = r.structured or {}
            rule = structured.get("score_rule") or {}
            try:
                weight = float(rule.get("weight") or 10)
            except (TypeError, ValueError):
                weight = 10.0
            criterion = str(rule.get("criterion") or r.content)
            covered = any(
                t and (r.content[:10] in t or criterion[:10] in t)
                for t in doc_texts.values()
            )
            items_data.append(
                {
                    "category": "评分细则",
                    "problem_description": r.content,
                    "got": round(weight, 2) if covered else 0.0,
                    "full": round(weight, 2),
                    "improvable": 0.0 if covered else round(weight, 2),
                    "risk_level": 0 if covered else 1,
                    "suggestion": None if covered else f"评分细则未在成果中体现：{criterion[:40]}",
                    "action_type": "manual_review",
                    "missing_material_types": None,
                    "evidence": {
                        "claim_id": f"score-rule-{r.id}",
                        "criterion": criterion,
                        "weight": weight,
                        "source_requirement_id": r.id,
                    },
                }
            )
            score_rule_stats["count"] += 1
            score_rule_stats["weight_total"] += round(weight, 2)
            if covered:
                score_rule_stats["weight_got"] += round(weight, 2)
            else:
                score_rule_stats["missed"] += 1

    raw_hash = sha256(
        json.dumps(
            {
                "provider_code": provider.provider_code,
                "provider_version": provider.provider_version,
                "items": items_data,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode()
    ).hexdigest()
    run = ReviewRun(
        enterprise_id=enterprise_id,
        project_id=project_id,
        snapshot_id=snapshot.id,
        provider_id=provider.id,
        provider_raw_hash=raw_hash,
        status=2,
    )
    session.add(run)
    await session.flush()

    total_got = sum(d["got"] for d in items_data)
    total_full = sum(d["full"] for d in items_data)
    missing_count = sum(1 for d in items_data if d["got"] == 0)
    # 评分基准（Issue #8 反馈"未获取评审细则却显示 10 分满分"）：
    # 有招标评分细则 → 总分按细则权重计算（weight_got/weight_total×100）；
    # 无细则 → 内置完整性规则得分，并显式标注"未获取评分细则，不代表招标评标得分"。
    has_rules = bool(score_rule_reqs)
    if has_rules:
        scale = "score_rules"
        rule_got = score_rule_stats["weight_got"]
        rule_full = score_rule_stats["weight_total"] or 1.0
        overall = round(rule_got / rule_full * 100, 2)
        full_marks = round(rule_full, 2)
        got_marks = round(rule_got, 2)
    else:
        scale = "builtin"
        overall = round(total_got / total_full * 100, 2) if total_full else 0.0
        full_marks = round(total_full, 2)
        got_marks = round(total_got, 2)
    score = ScoreRecord(
        enterprise_id=enterprise_id,
        project_id=project_id,
        review_run_id=run.id,
        evaluation_type="builtin",
        total_score=overall,
        missing_count=missing_count,
        improvable=round(sum(d["improvable"] for d in items_data), 2),
        deliverable_versions=input_refs["deliverable_versions"],
        artifact_versions=artifact_versions,
        detail={
            "items_count": len(items_data),
            "score_rules": score_rule_stats,
            "scale": scale,
            "full_marks": full_marks,
            "got_marks": got_marks,
        },
    )
    session.add(score)
    await session.flush()

    items: list[ReviewItem] = []
    for data in items_data:
        item = ReviewItem(
            enterprise_id=enterprise_id,
            project_id=project_id,
            review_run_id=run.id,
            score_id=score.id,
            ruleset_version=RULESET_VERSION,
            category=data["category"],
            problem_description=data["problem_description"],
            got=data["got"],
            full=data["full"],
            improvable=data["improvable"],
            risk_level=data["risk_level"],
            suggestion=data["suggestion"],
            action_type=data["action_type"],
            evidence=data["evidence"],
            missing_material_types=data["missing_material_types"],
            status=1,
            expected_version=str(snapshot.id),
        )
        session.add(item)
        items.append(item)
    await session.flush()
    return {
        "run_id": run.id,
        "score_id": score.id,
        "snapshot_id": snapshot.id,
        "total_score": float(score.total_score),
        "missing_count": missing_count,
        "item_ids": [i.id for i in items],
        "score_rules": score_rule_stats,
        "scale": scale,
        "full_marks": full_marks,
        "got_marks": got_marks,
    }


async def confirm_items(
    session: AsyncSession,
    *,
    enterprise_id: int,
    project_id: int,
    score_id: int,
    item_ids: list[int],
    action: str,
    expected_version: str | None = None,
) -> list[dict]:
    if action not in ("confirm", "reject"):
        raise ValueError("action 必须为 confirm 或 reject")
    results: list[dict] = []
    for item_id in item_ids:
        item = await session.scalar(
            select(ReviewItem).where(
                ReviewItem.id == item_id,
                ReviewItem.enterprise_id == enterprise_id,
                ReviewItem.project_id == project_id,
                ReviewItem.score_id == score_id,
            )
        )
        if item is None:
            results.append({"item_id": item_id, "status": "skipped", "reason": "条目不存在"})
            continue
        if item.status != 1:
            results.append({"item_id": item_id, "status": "skipped", "reason": f"当前状态不可确认（{item.status}）"})
            continue
        if expected_version is not None and str(item.expected_version) != str(expected_version):
            results.append({"item_id": item_id, "status": "conflict", "reason": "快照版本不一致"})
            continue
        item.status = 2 if action == "confirm" else 3
        results.append({"item_id": item_id, "status": "succeeded"})
    return results


async def re_evaluate(
    session: AsyncSession,
    *,
    enterprise_id: int,
    project_id: int,
    item_ids: list[int],
) -> dict:
    """仅重审受影响项：确认且可补材料的项，若项目已有材料则提升 got=full。"""
    originals = (
        await session.scalars(
            select(ReviewItem).where(
                ReviewItem.id.in_(item_ids),
                ReviewItem.enterprise_id == enterprise_id,
                ReviewItem.project_id == project_id,
            )
        )
    ).all()
    if not originals:
        raise ValueError("没有可重审的条目")

    provider = await ensure_builtin_provider(session, enterprise_id)
    deliverables = (
        await session.scalars(
            select(Deliverable).where(
                Deliverable.enterprise_id == enterprise_id,
                Deliverable.project_id == project_id,
            )
        )
    ).all()
    deliverable_versions = {d.id: d.current_version_no for d in deliverables}
    artifact_versions = await _project_artifact_versions(session, enterprise_id, project_id)
    snapshot = ProjectSnapshot(
        enterprise_id=enterprise_id,
        project_id=project_id,
        snapshot_type="review",
        input_refs={
            "item_ids": item_ids,
            "ruleset": RULESET_VERSION,
            "deliverable_versions": deliverable_versions,
            "artifact_versions": artifact_versions,
        },
        rules_version={"ruleset": RULESET_VERSION},
    )
    session.add(snapshot)
    await session.flush()

    run = ReviewRun(
        enterprise_id=enterprise_id,
        project_id=project_id,
        snapshot_id=snapshot.id,
        provider_id=provider.id,
        status=2,
    )
    session.add(run)
    await session.flush()

    new_items: list[ReviewItem] = []
    for original in originals:
        # 不再“确认+任意材料→满分”：真实改善只能来自重新实质评审（discussion #53）
        got = float(original.got or 0)
        improvable = float(original.improvable or 0)
        item = ReviewItem(
            enterprise_id=enterprise_id,
            project_id=project_id,
            review_run_id=run.id,
            requirement_id=original.requirement_id,
            criterion_id=original.criterion_id,
            ruleset_version=original.ruleset_version,
            category=original.category,
            problem_description=original.problem_description,
            got=got,
            full=original.full,
            improvable=improvable,
            risk_level=original.risk_level,
            suggestion=original.suggestion,
            action_type=original.action_type,
            evidence=original.evidence,
            missing_material_types=original.missing_material_types,
            verdict=original.verdict,
            deduction_reason=original.deduction_reason,
            rule_source=original.rule_source,
            response_source=original.response_source,
            missing_materials=original.missing_materials,
            status=4,  # re_reviewed
            expected_version=str(snapshot.id),
        )
        session.add(item)
        await session.flush()
        new_items.append(item)

    # 总分 = 原评审已确认项（未被本次重审替换的部分） + 重审新项
    original_score_ids = {o.score_id for o in originals if o.score_id is not None}
    carried: list[ReviewItem] = []
    replaced_ids = {o.id for o in originals}
    carried_evaluation_types: set[str] = set()
    for score_id in original_score_ids:
        score_row = await session.scalar(
            select(ScoreRecord).where(
                ScoreRecord.id == score_id,
                ScoreRecord.enterprise_id == enterprise_id,
                ScoreRecord.project_id == project_id,
            )
        )
        if score_row is None:
            continue
        carried_evaluation_types.add(score_row.evaluation_type or "builtin")
        run_rows = (
            await session.scalars(
                select(ReviewItem).where(
                    ReviewItem.review_run_id == score_row.review_run_id,
                    ReviewItem.enterprise_id == enterprise_id,
                    ReviewItem.project_id == project_id,
                )
            )
        ).all()
        for row in run_rows:
            if row.id in replaced_ids:
                continue  # 本次重审已生成新项
            carried.append(row)  # 未被替换的原项全部保留（含已给分未确认项）
    all_items = carried + new_items
    total_got = sum(float(i.got or 0) for i in all_items)
    total_full = sum(float(i.full or 0) for i in all_items)
    score = ScoreRecord(
        enterprise_id=enterprise_id,
        project_id=project_id,
        review_run_id=run.id,
        evaluation_type="substantive" if "substantive" in carried_evaluation_types else "builtin",
        total_score=round(total_got / total_full * 100, 2) if total_full else 0.0,
        missing_count=sum(1 for i in all_items if i.got == 0),
        improvable=round(sum(float(i.improvable or 0) for i in all_items), 2),
        deliverable_versions=deliverable_versions,
        artifact_versions=artifact_versions,
        detail={"re_evaluated": len(new_items), "carried": len(carried)},
    )
    session.add(score)
    await session.flush()
    return {
        "run_id": run.id,
        "score_id": score.id,
        "total_score": float(score.total_score),
        "new_item_ids": [i.id for i in new_items],
        "improved_count": sum(1 for i in new_items if i.improvable == 0 and i.got == i.full),
    }


# ===================== 真实评分闭环（discussion #53） =====================
# substantive：按招标评分细则对正式成果逐条实质评审（服务端 LLM 引擎 / Agent 提交）。
# builtin：仅内部完整性自检，不进入 /scores 评分卡，也不作为真实评分的降级兜底。

SUBSTANTIVE_RULES_VERSION = "substantive-llm-1.0"
SUBSTANTIVE_PROVIDER_CODE = "substantive_llm"
_LLM_MAX_FILE_CHARS = 12000
_LLM_MAX_TOTAL_CHARS = 30000

_SUBSTANTIVE_SYSTEM = (
    "你是资深招投标评审专家，对投标文件按招标评分标准逐条实质评审。\n"
    "规则：\n"
    "1. 只依据给定的受评文件文本判断；找不到证据必须返回 insufficient_evidence，禁止编造事实或分数。\n"
    "2. 仅凭关键词命中不得给满分；要判断实际响应与满足程度，部分满足按档位给部分分，正文照抄条款不等于已满足。\n"
    "3. got 必须是 0 到满分之间的数字；insufficient_evidence / not_applicable 时 got=null。\n"
    "4. 结论、扣分原因、缺失材料都要有文本依据；没有可行建议时 suggestion=null。\n"
    "5. 输出严格 JSON 对象（不要 Markdown 围栏、不要多余文字），字段：\n"
    '{"verdict":"satisfied|partial|unsatisfied|insufficient_evidence|not_applicable",'
    '"got":<数字或 null>,"deduction_reason":"扣分/未满足原因，可为 null",'
    '"risk_level":0|1|2|3,"suggestion":"可执行的提分建议，可为 null",'
    '"missing_materials":[{"type":"资料类型","description":"补什么、为什么"}],'
    '"rule_quote":"评审所依据的标准原文片段，可为 null",'
    '"response_quote":"成果中对应的原文片段，可为 null"}'
)


def _normalize_verdict(value: object) -> str:
    """把 LLM/Agent 给出的结论归一化到固定枚举；未知结论按“证据不足”处理（不编分）。"""
    aliases = {
        "satisfied": "satisfied",
        "pass": "satisfied",
        "met": "satisfied",
        "full": "satisfied",
        "partial": "partial",
        "partially_met": "partial",
        "unsatisfied": "unsatisfied",
        "fail": "unsatisfied",
        "not_met": "unsatisfied",
        "missing": "unsatisfied",
        "insufficient_evidence": "insufficient_evidence",
        "insufficient": "insufficient_evidence",
        "unknown": "insufficient_evidence",
        "not_applicable": "not_applicable",
        "n/a": "not_applicable",
        "na": "not_applicable",
    }
    return aliases.get(str(value or "").strip().lower(), "insufficient_evidence")


def _coerce_float(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _rule_category(content: str, criterion: str) -> str:
    """评分项分类（商务/技术/价格），用于分项卡统计，不硬造三项分数。"""
    text = f"{content} {criterion}"
    if any(k in text for k in ("价格", "报价", "预算", "折扣", "费率")):
        return "价格"
    if any(k in text for k in ("技术", "方案", "实施", "服务", "售后", "质量", "进度", "工期", "人员", "团队")):
        return "技术"
    if any(k in text for k in ("商务", "资质", "业绩", "资格", "财务", "纳税", "信用", "承诺")):
        return "商务"
    return "综合"


async def ensure_substantive_provider(session: AsyncSession, enterprise_id: int) -> ReviewProvider:
    provider = await session.scalar(
        select(ReviewProvider).where(
            ReviewProvider.provider_code == SUBSTANTIVE_PROVIDER_CODE,
            ReviewProvider.enterprise_id == enterprise_id,
        )
    )
    if provider is None:
        provider = ReviewProvider(
            enterprise_id=enterprise_id,
            provider_type="code",
            provider_code=SUBSTANTIVE_PROVIDER_CODE,
            provider_version="1.0.0",
            name="招标实质评分（LLM 引擎）",
            category="scoring",
            severity=2,
            enabled=True,
        )
        session.add(provider)
        try:
            async with session.begin_nested():
                await session.flush()
        except IntegrityError:
            provider = await session.scalar(
                select(ReviewProvider).where(
                    ReviewProvider.provider_code == SUBSTANTIVE_PROVIDER_CODE,
                    ReviewProvider.enterprise_id == enterprise_id,
                )
            )
            if provider is None:
                raise
    return provider


def _docx_text(data: bytes) -> str:
    import io as _io

    from docx import Document

    doc = Document(_io.BytesIO(data))
    parts: list[str] = []
    for para in doc.paragraphs:
        if para.text.strip():
            parts.append(para.text)
    for table in doc.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text for cell in row.cells))
    return "\n".join(parts)


def _xlsx_text(data: bytes) -> str:
    import io as _io

    from openpyxl import load_workbook

    wb = load_workbook(_io.BytesIO(data), read_only=True, data_only=True)
    parts: list[str] = []
    for ws in wb.worksheets:
        for row in ws.iter_rows(values_only=True):
            vals = ["" if v is None else str(v) for v in row]
            if any(v.strip() for v in vals):
                parts.append(f"{ws.title}: " + " | ".join(vals))
    wb.close()
    return "\n".join(parts)


async def _artifact_texts(
    session: AsyncSession, enterprise_id: int, project_id: int
) -> tuple[list[dict], dict[int, int]]:
    """读取正式成果 artifact（item_docx / xlsx）的文本；不可读的逐条记录原因。"""
    artifacts = (
        await session.scalars(
            select(AgentArtifact).where(
                AgentArtifact.enterprise_id == enterprise_id,
                AgentArtifact.project_id == project_id,
            )
        )
    ).all()
    versions = {int(a.id): int(a.version_no) for a in artifacts}
    summaries: list[dict] = []
    for a in artifacts:
        if a.kind not in ("item_docx", "xlsx"):
            continue
        text = ""
        error = None
        try:
            if a.kind == "xlsx":
                text = _xlsx_text(a.content or b"")
            else:
                text = _docx_text(a.content or b"")
        except Exception as exc:  # noqa: BLE001 损坏/加密文件如实标记不可读，不拖垮整次评审
            error = f"{type(exc).__name__}: {str(exc)[:120]}"
        summaries.append(
            {
                "artifact_id": int(a.id),
                "name": a.name,
                "kind": a.kind,
                "version_no": int(a.version_no),
                "text": text,
                "chars": len(text),
                "error": error,
            }
        )
    return summaries, versions


async def _score_rule_with_llm(rule: dict, file_blocks: list[dict]) -> dict:
    """单条评分细则的 LLM 评审；解析失败两轮后按“证据不足”处理，绝不编分。"""
    from app.services.llm import LLMClient, try_extract_json

    parts: list[str] = []
    budget = _LLM_MAX_TOTAL_CHARS
    for idx, block in enumerate(file_blocks, start=1):
        text = block.get("text") or ""
        truncated = len(text) > _LLM_MAX_FILE_CHARS
        text = text[:_LLM_MAX_FILE_CHARS]
        if len(text) > budget:
            text = text[:budget]
            truncated = True
        if budget <= 0:
            break
        budget -= len(text)
        head = f"【文件 {idx}：{block.get('name') or '未命名'}（版本 {block.get('version_no')}）】\n"
        parts.append(head + text + ("\n…（内容过长已截断）" if truncated else ""))

    user = (
        f"评分标准：{rule.get('content') or ''}\n"
        f"评审要点：{rule.get('criterion') or rule.get('content') or ''}\n"
        f"满分：{rule.get('weight') or 0} 分\n"
        f"分类：{rule.get('category') or '评分细则'}\n\n"
        f"受评正式成果文本：\n{''.join(parts) or '（无可读文本）'}\n\n"
        "请按系统要求只输出一个 JSON 对象作为评审结论。"
    )
    last_reply: str | None = None
    for attempt in range(2):
        try:
            reply = await LLMClient().chat(_SUBSTANTIVE_SYSTEM, user)
        except Exception:  # noqa: BLE001 瞬时云端故障：重试一次，仍失败则由任务层重试
            if attempt == 0:
                await asyncio.sleep(2)
                continue
            raise
        parsed = try_extract_json(reply)
        if isinstance(parsed, dict):
            return parsed
        last_reply = reply
        user += "\n\n上次输出不是合法 JSON。请只输出一个 JSON 对象，不要任何解释文字或 Markdown 围栏。"
    return {
        "verdict": "insufficient_evidence",
        "got": None,
        "parse_error": True,
        "raw": (last_reply or "")[:300],
    }


def _item_from_llm_result(rule: dict, parsed: dict) -> dict:
    verdict = _normalize_verdict(parsed.get("verdict"))
    full = float(rule.get("weight") or 0.0)
    got = _coerce_float(parsed.get("got"))

    if verdict == "not_applicable":
        got, full, improvable = None, None, None
    elif verdict == "insufficient_evidence":
        got, improvable = None, None
    elif verdict == "satisfied":
        got, improvable = full, 0.0
    elif verdict == "unsatisfied":
        got, improvable = 0.0, full
    else:  # partial：有数字才保留；无数字按“证据不足”处理（绝不编分）
        if got is None:
            verdict = "insufficient_evidence"
            got, improvable = None, None
        else:
            got = min(max(got, 0.0), full)
            improvable = round(full - got, 2)

    deduction_reason = parsed.get("deduction_reason")
    deduction_reason = deduction_reason.strip() if isinstance(deduction_reason, str) else None
    deduction_reason = deduction_reason or None
    suggestion = parsed.get("suggestion")
    suggestion = suggestion.strip() if isinstance(suggestion, str) else None
    suggestion = suggestion or None
    try:
        risk_level = int(parsed.get("risk_level"))
    except (TypeError, ValueError):
        risk_level = None
    risk_level = min(max(risk_level or 0, 0), 3)
    missing = parsed.get("missing_materials")
    if not isinstance(missing, list):
        missing = []
    missing_materials = [
        m for m in missing
        if isinstance(m, dict) and (m.get("type") or m.get("description"))
    ]
    missing_types = "、".join(
        str(m.get("type") or "").strip()
        for m in missing_materials
        if str(m.get("type") or "").strip()
    ) or None

    if verdict == "insufficient_evidence" and missing_materials:
        action_type = "upload_material"
    elif missing_materials:
        action_type = "upload_material"
    elif verdict in ("unsatisfied", "partial"):
        action_type = "edit_deliverable"
    else:
        action_type = "manual_review"

    rule_quote = parsed.get("rule_quote")
    response_quote = parsed.get("response_quote")
    return {
        "category": rule.get("category") or "评分细则",
        "problem_description": rule.get("content") or "",
        "requirement_id": rule.get("requirement_id"),
        "criterion_id": rule.get("req_key")
        or (str(rule.get("requirement_id")) if rule.get("requirement_id") else None),
        "got": round(got, 2) if got is not None else None,
        "full": round(full, 2) if full is not None else None,
        "improvable": round(improvable, 2) if improvable is not None else None,
        "risk_level": risk_level,
        "suggestion": suggestion,
        "action_type": action_type,
        "missing_material_types": missing_types,
        "verdict": verdict,
        "deduction_reason": deduction_reason,
        "rule_source": {
            "requirement_id": rule.get("requirement_id"),
            "quote": rule_quote,
            "location": rule.get("coordinates"),
            "file_id": rule.get("source_file_id"),
        },
        "response_source": {
            "quote": response_quote,
            "files": [
                {
                    "artifact_id": b.get("artifact_id"),
                    "name": b.get("name"),
                    "version_no": b.get("version_no"),
                }
                for b in rule.get("_file_blocks", [])
            ],
        },
        "missing_materials": missing_materials,
        "evidence": {
            "engine": "substantive_llm",
            "rule_quote": rule_quote,
            "response_quote": response_quote,
            "parse_error": bool(parsed.get("parse_error")),
            "llm_error": parsed.get("llm_error"),
        },
    }


async def _persist_substantive_score(
    session: AsyncSession,
    *,
    enterprise_id: int,
    project_id: int,
    provider: ReviewProvider,
    snapshot: ProjectSnapshot,
    items_data: list[dict],
    artifact_versions: dict[int, int],
    deliverable_versions: dict[int, int],
    rules_list: list[dict],
    run_hash: str,
) -> dict:
    """原子落库 run + score + items（均为 evaluation_type=substantive）。"""
    run = ReviewRun(
        enterprise_id=enterprise_id,
        project_id=project_id,
        snapshot_id=snapshot.id,
        provider_id=provider.id,
        provider_raw_hash=run_hash,
        status=2,
    )
    session.add(run)
    await session.flush()

    numeric = [
        it for it in items_data
        if it["verdict"] in ("satisfied", "partial", "unsatisfied")
        and it["got"] is not None and it["full"]
    ]
    total_got = sum(float(it["got"]) for it in numeric)
    total_full = sum(float(it["full"]) for it in numeric)
    total_score = round(total_got / total_full * 100, 2) if total_full else None
    missing_count = sum(
        1 for it in items_data if it["verdict"] in ("unsatisfied", "insufficient_evidence")
    )
    unrated_count = sum(
        1 for it in items_data if it["verdict"] in ("insufficient_evidence", "not_applicable")
    )
    known_improvable = sum(
        float(it["improvable"]) for it in items_data if it["improvable"] is not None
    )
    category_scores: dict[str, dict] = {}
    for it in items_data:
        cat = it["category"]
        bucket = category_scores.setdefault(cat, {"got": 0.0, "full": 0.0, "count": 0, "unrated": 0})
        if it["verdict"] in ("insufficient_evidence", "not_applicable"):
            bucket["unrated"] += 1
        elif it["got"] is not None and it["full"]:
            bucket["got"] = round(bucket["got"] + float(it["got"]), 2)
            bucket["full"] = round(bucket["full"] + float(it["full"]), 2)
            bucket["count"] += 1

    score = ScoreRecord(
        enterprise_id=enterprise_id,
        project_id=project_id,
        review_run_id=run.id,
        evaluation_type="substantive",
        total_score=total_score,
        missing_count=missing_count,
        improvable=round(known_improvable, 2),
        deliverable_versions=deliverable_versions,
        artifact_versions=artifact_versions,
        detail={
            "items_count": len(items_data),
            "unrated_count": unrated_count,
            "scale": "score_rules" if total_full else "not_applicable",
            "method": "按招标评分细则逐条实质评审（预评估，不代表采购方最终专家评分）",
            "category_scores": category_scores,
            "rules": rules_list,
        },
    )
    session.add(score)
    await session.flush()

    items: list[ReviewItem] = []
    for data in items_data:
        item = ReviewItem(
            enterprise_id=enterprise_id,
            project_id=project_id,
            review_run_id=run.id,
            score_id=score.id,
            requirement_id=data.get("requirement_id"),
            criterion_id=data.get("criterion_id"),
            ruleset_version=SUBSTANTIVE_RULES_VERSION,
            category=data["category"],
            problem_description=data["problem_description"],
            got=data["got"],
            full=data["full"],
            improvable=data["improvable"],
            risk_level=data["risk_level"],
            suggestion=data["suggestion"],
            action_type=data["action_type"],
            evidence=data["evidence"],
            missing_material_types=data["missing_material_types"],
            verdict=data["verdict"],
            deduction_reason=data["deduction_reason"],
            rule_source=data["rule_source"],
            response_source=data["response_source"],
            missing_materials=data["missing_materials"],
            confidence=data.get("confidence"),
            status=1,
            expected_version=str(snapshot.id),
        )
        session.add(item)
        items.append(item)
    await session.flush()
    return {
        "evaluation_type": "substantive",
        "run_id": run.id,
        "score_id": score.id,
        "snapshot_id": snapshot.id,
        "total_score": total_score,
        "missing_count": missing_count,
        "unrated_count": unrated_count,
        "item_ids": [int(i.id) for i in items],
        "scale": "score_rules" if total_full else "not_applicable",
    }


async def substantive_idempotency_key(
    session: AsyncSession, enterprise_id: int, project_id: int
) -> str:
    """按当前输入（规则修订/成果版本/artifact 版本）生成幂等键：输入不变重复点击返回同一任务。"""
    rules = (
        await session.scalars(
            select(Requirement).where(
                Requirement.enterprise_id == enterprise_id,
                Requirement.project_id == project_id,
                Requirement.current.is_(True),
                Requirement.req_type == "score_rule",
            )
        )
    ).all()
    deliverables = (
        await session.scalars(
            select(Deliverable).where(
                Deliverable.enterprise_id == enterprise_id,
                Deliverable.project_id == project_id,
            )
        )
    ).all()
    artifact_versions = await _project_artifact_versions(session, enterprise_id, project_id)
    digest = sha256(
        json.dumps(
            {
                "rules": [(int(r.id), int(r.revision)) for r in sorted(rules, key=lambda r: r.id)],
                "deliverables": sorted(
                    (int(d.id), int(d.current_version_no)) for d in deliverables
                ),
                "artifacts": sorted(artifact_versions.items()),
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode()
    ).hexdigest()
    return f"substantive-{digest[:32]}"


async def run_substantive_evaluation(session: AsyncSession, task) -> None:
    """Worker 任务：冻结规则与正式成果版本 → 逐条 LLM 实质评审 → 落库 substantive 评分。

    缺规则 / 成果不可读 / LLM 门禁关闭 → TerminalTaskError(code=not_scoreable)：
    明确返回“暂不可评分”及原因，绝不回退 builtin 完整性评分。
    """
    from app.services.llm import llm_enabled
    from app.services.task_service import _set_rls_context

    enterprise_id = int(task.enterprise_id)
    project_id = int(task.project_id)

    async def _progress(percent: int, current_work: str) -> None:
        task.progress = {
            "phase": "substantive_evaluate",
            "status": "running",
            "percent": percent,
            "current_work": current_work,
        }
        await session.commit()
        await _set_rls_context(session, enterprise_id)

    rules = (
        await session.scalars(
            select(Requirement)
            .where(
                Requirement.enterprise_id == enterprise_id,
                Requirement.project_id == project_id,
                Requirement.current.is_(True),
                Requirement.req_type == "score_rule",
            )
            .order_by(Requirement.id)
        )
    ).all()
    if not rules:
        raise TerminalTaskError(
            "暂不可评分：尚未解析到评分标准（score_rule）。请先完成招标解析并确认评分细则。",
            code="not_scoreable",
        )

    art_summaries, artifact_versions = await _artifact_texts(session, enterprise_id, project_id)
    readable = [a for a in art_summaries if (a.get("text") or "").strip()]
    if not readable:
        unreadable = [a for a in art_summaries if a.get("error")]
        reason = "；".join(f"{a['name']}: {a['error']}" for a in unreadable[:3]) or "项目没有正式成果文件"
        raise TerminalTaskError(
            f"暂不可评分：没有可读取的正式成果（{reason}）。",
            code="not_scoreable",
        )
    if not llm_enabled():
        raise TerminalTaskError(
            "暂不可评分：云模型门禁关闭，评审能力未就绪。",
            code="not_scoreable",
        )

    await _progress(20, f"已读取 {len(rules)} 条评分细则与 {len(readable)} 份正式成果，开始逐条评审…")

    frozen_rules: list[dict] = []
    for r in rules:
        structured = (r.structured or {}).get("score_rule") or {}
        weight = _coerce_float(structured.get("weight"))
        weight = weight if weight is not None and weight > 0 else 0.0
        criterion = str(structured.get("criterion") or r.content)
        frozen_rules.append(
            {
                "requirement_id": int(r.id),
                "req_key": r.req_key,
                "revision": int(r.revision),
                "content": r.content,
                "criterion": criterion,
                "weight": round(weight, 2),
                "category": str(structured.get("category") or "").strip()
                or _rule_category(r.content, criterion),
                "coordinates": r.coordinates,
                "source_file_id": r.source_file_id,
            }
        )

    deliverables = (
        await session.scalars(
            select(Deliverable).where(
                Deliverable.enterprise_id == enterprise_id,
                Deliverable.project_id == project_id,
            )
        )
    ).all()
    deliverable_versions = {int(d.id): int(d.current_version_no) for d in deliverables}
    provider = await ensure_substantive_provider(session, enterprise_id)
    snapshot = ProjectSnapshot(
        enterprise_id=enterprise_id,
        project_id=project_id,
        snapshot_type="review",
        input_refs={
            "evaluation_type": "substantive",
            "ruleset": SUBSTANTIVE_RULES_VERSION,
            "rules": frozen_rules,
            "deliverable_versions": deliverable_versions,
            "artifact_versions": artifact_versions,
        },
        rules_version={"ruleset": SUBSTANTIVE_RULES_VERSION},
    )
    session.add(snapshot)
    await session.flush()

    items_data: list[dict] = []
    total = len(frozen_rules)
    for idx, rule in enumerate(frozen_rules, start=1):
        await _progress(
            int(30 + 60 * idx / total),
            f"逐条实质评审 {idx}/{total}：{str(rule['content'])[:40]}",
        )
        rule = {**rule, "_file_blocks": readable}
        parsed = await _score_rule_with_llm(rule, readable)
        items_data.append(_item_from_llm_result(rule, parsed))

    run_hash = sha256(
        json.dumps(
            {
                "ruleset": SUBSTANTIVE_RULES_VERSION,
                "rules": frozen_rules,
                "items": items_data,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode()
    ).hexdigest()
    result = await _persist_substantive_score(
        session,
        enterprise_id=enterprise_id,
        project_id=project_id,
        provider=provider,
        snapshot=snapshot,
        items_data=items_data,
        artifact_versions=artifact_versions,
        deliverable_versions=deliverable_versions,
        rules_list=[
            {
                "rule_id": r["requirement_id"],
                "content": r["content"],
                "criterion": r["criterion"],
                "weight": r["weight"],
                "category": r["category"],
                "revision": r["revision"],
            }
            for r in frozen_rules
        ],
        run_hash=run_hash,
    )
    task.result = result


async def submit_substantive_items(
    session: AsyncSession,
    *,
    enterprise_id: int,
    project_id: int,
    payload: dict,
) -> dict:
    """Agent 提交的真实评分逐条落库（MCP submit_score_items → /substantive-items）。

    校验 requirement 归属与满分上限，逐条回执；相同 payload 幂等（不重复建 run/score）。
    """
    if not isinstance(payload, dict):
        raise ValueError("payload 必须是对象")
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raw_items = payload.get("score_items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("payload.items 必须为非空数组")

    rules_by_id = {
        int(r.id): r
        for r in (
            await session.scalars(
                select(Requirement).where(
                    Requirement.enterprise_id == enterprise_id,
                    Requirement.project_id == project_id,
                    Requirement.current.is_(True),
                    Requirement.req_type == "score_rule",
                )
            )
        ).all()
    }
    provider = await ensure_substantive_provider(session, enterprise_id)
    artifact_versions = await _project_artifact_versions(session, enterprise_id, project_id)
    deliverables = (
        await session.scalars(
            select(Deliverable).where(
                Deliverable.enterprise_id == enterprise_id,
                Deliverable.project_id == project_id,
            )
        )
    ).all()
    deliverable_versions = {int(d.id): int(d.current_version_no) for d in deliverables}

    payload_hash = sha256(
        json.dumps(raw_items, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    existing_run = await session.scalar(
        select(ReviewRun).where(
            ReviewRun.enterprise_id == enterprise_id,
            ReviewRun.project_id == project_id,
            ReviewRun.provider_id == provider.id,
            ReviewRun.provider_raw_hash == payload_hash,
        )
    )
    if existing_run is not None:
        existing_score = await session.scalar(
            select(ScoreRecord).where(
                ScoreRecord.review_run_id == existing_run.id,
                ScoreRecord.enterprise_id == enterprise_id,
                ScoreRecord.project_id == project_id,
            )
        )
        if existing_score is not None:
            return {
                "run_id": existing_run.id,
                "score_id": existing_score.id,
                "duplicate": True,
                "accepted": 0,
                "results": [{"status": "duplicate", "reason": "相同内容已提交过，未重复落库"}],
            }

    items_data: list[dict] = []
    results: list[dict] = []
    for index, entry in enumerate(raw_items):
        if not isinstance(entry, dict):
            results.append({"index": index, "status": "skipped", "reason": "条目必须是对象"})
            continue

        requirement_id: int | None = None
        rule = None
        rule_weight: float | None = None
        if entry.get("requirement_id") is not None:
            try:
                requirement_id = int(entry["requirement_id"])
            except (TypeError, ValueError):
                results.append({"index": index, "status": "skipped", "reason": "requirement_id 必须是整数"})
                continue
            rule = rules_by_id.get(requirement_id)
            if rule is None:
                results.append(
                    {"index": index, "status": "skipped", "reason": "requirement_id 不存在或不属于本项目"}
                )
                continue
            structured = (rule.structured or {}).get("score_rule") or {}
            rule_weight = _coerce_float(structured.get("weight"))

        full = _coerce_float(entry.get("full"))
        if full is None:
            full = rule_weight
        if full is None or full < 0:
            results.append({"index": index, "status": "skipped", "reason": "full 缺失或非法"})
            continue
        if rule_weight is not None:
            full = min(full, rule_weight)
        got = _coerce_float(entry.get("got"))
        if got is not None:
            got = min(max(got, 0.0), full)

        verdict_value = entry.get("verdict")
        verdict = _normalize_verdict(verdict_value) if verdict_value else None
        if verdict is None:
            if got is None:
                verdict = "insufficient_evidence"
            elif got >= full > 0:
                verdict = "satisfied"
            elif got <= 0:
                verdict = "unsatisfied"
            else:
                verdict = "partial"
        verdict = _normalize_verdict(verdict)

        if verdict == "not_applicable":
            got, full, improvable = None, None, None
        elif verdict == "insufficient_evidence":
            got, improvable = None, None
        elif verdict == "satisfied":
            got, improvable = float(full), 0.0
        elif verdict == "unsatisfied":
            got, improvable = 0.0, float(full)
        else:
            if got is None:
                verdict = "insufficient_evidence"
                improvable = None
            else:
                improvable = round(float(full) - got, 2)

        missing = entry.get("missing_materials")
        if not isinstance(missing, list):
            missing = []
        missing_materials = [
            m for m in missing
            if isinstance(m, dict) and (m.get("type") or m.get("description"))
        ]
        missing_types = "、".join(
            str(m.get("type") or "").strip()
            for m in missing_materials
            if str(m.get("type") or "").strip()
        ) or None
        action_type = str(entry.get("action_type") or "").strip()
        if not action_type:
            if missing_materials:
                action_type = "upload_material"
            elif verdict in ("unsatisfied", "partial"):
                action_type = "edit_deliverable"
            else:
                action_type = "manual_review"
        try:
            risk_level = int(entry["risk_level"]) if entry.get("risk_level") is not None else None
        except (TypeError, ValueError):
            risk_level = None
        if risk_level is not None:
            risk_level = min(max(risk_level, 0), 3)
        confidence = _coerce_float(entry.get("confidence"))
        if confidence is not None:
            confidence = min(max(confidence, 0.0), 1.0)

        rule_source = entry.get("rule_source")
        rule_source = rule_source if isinstance(rule_source, dict) and rule_source else None
        response_source = entry.get("response_source")
        response_source = response_source if isinstance(response_source, dict) and response_source else None
        evidence = entry.get("evidence")
        if not isinstance(evidence, dict) or not evidence:
            evidence = {
                "engine": "agent_submit",
                "exact_quote": (response_source or {}).get("quote"),
            }
        suggestion = entry.get("suggestion")
        suggestion = suggestion.strip() if isinstance(suggestion, str) else None
        suggestion = suggestion or None
        deduction_reason = entry.get("deduction_reason")
        deduction_reason = deduction_reason.strip() if isinstance(deduction_reason, str) else None
        deduction_reason = deduction_reason or None
        problem_description = str(entry.get("problem_description") or entry.get("content") or "")
        if not problem_description and rule is not None:
            problem_description = rule.content
        if not problem_description:
            problem_description = "评分项"
        criterion_id = str(
            entry.get("criterion_id")
            or entry.get("rule_id")
            or (str(requirement_id) if requirement_id is not None else "")
            or ""
        ).strip() or None

        items_data.append(
            {
                "category": str(entry.get("category") or "评分细则"),
                "problem_description": problem_description,
                "requirement_id": requirement_id,
                "criterion_id": criterion_id,
                "got": round(got, 2) if got is not None else None,
                "full": round(float(full), 2) if full is not None else None,
                "improvable": round(improvable, 2) if improvable is not None else None,
                "risk_level": risk_level,
                "suggestion": suggestion,
                "action_type": action_type,
                "missing_material_types": missing_types,
                "verdict": verdict,
                "deduction_reason": deduction_reason,
                "rule_source": rule_source,
                "response_source": response_source,
                "missing_materials": missing_materials,
                "evidence": evidence,
                "confidence": confidence,
            }
        )
        results.append({"index": index, "status": "accepted", "requirement_id": requirement_id})

    if not items_data:
        raise ValueError("没有可落库的有效评分条目：" + "；".join(
            r.get("reason", "") for r in results if r.get("status") == "skipped"
        )[:200])

    matched_rules = [rules_by_id[i["requirement_id"]] for i in items_data if i.get("requirement_id") in rules_by_id]
    snapshot = ProjectSnapshot(
        enterprise_id=enterprise_id,
        project_id=project_id,
        snapshot_type="review",
        input_refs={
            "evaluation_type": "substantive",
            "ruleset": SUBSTANTIVE_RULES_VERSION,
            "submitted_by": "agent",
            "deliverable_versions": deliverable_versions,
            "artifact_versions": artifact_versions,
            "payload_hash": payload_hash,
        },
        rules_version={"ruleset": SUBSTANTIVE_RULES_VERSION},
    )
    session.add(snapshot)
    await session.flush()
    result = await _persist_substantive_score(
        session,
        enterprise_id=enterprise_id,
        project_id=project_id,
        provider=provider,
        snapshot=snapshot,
        items_data=items_data,
        artifact_versions=artifact_versions,
        deliverable_versions=deliverable_versions,
        rules_list=[
            {
                "rule_id": int(r.id),
                "content": r.content,
                "criterion": str((r.structured or {}).get("score_rule", {}).get("criterion") or r.content),
                "weight": _coerce_float((r.structured or {}).get("score_rule", {}).get("weight")) or 0.0,
                "category": "评分细则",
                "revision": int(r.revision),
            }
            for r in matched_rules
        ],
        run_hash=payload_hash,
    )
    return {**result, "accepted": len(items_data), "results": results}
