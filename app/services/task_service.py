"""任务编排（4.4.3/4.4.6）：创建、领取、执行、状态机、白名单事件。"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import TaskStatus, TaskType
from app.models.task import Task

MAX_RETRIES = 3


async def create_task(
    session: AsyncSession,
    *,
    enterprise_id: int,
    project_id: int,
    task_type: str,
    payload: dict,
    idempotency_key: str,
    priority: int = 5,
) -> tuple[Task, bool]:
    """创建任务；重复 idempotency_key 返回已存在任务（created=False）。"""
    existing = await session.scalar(
        select(Task).where(
            Task.idempotency_key == idempotency_key,
            Task.enterprise_id == enterprise_id,
        )
    )
    if existing is not None:
        return existing, False
    task = Task(
        enterprise_id=enterprise_id,
        project_id=project_id,
        task_type=task_type,
        idempotency_key=idempotency_key,
        priority=priority,
        status=int(TaskStatus.QUEUED),
        payload=payload,
        generation=1,
    )
    session.add(task)
    await session.flush()
    return task, True


async def claim_next(session: AsyncSession) -> Task | None:
    """领取队首任务。PG 下 FOR UPDATE SKIP LOCKED；SQLite 退化为普通取首条。"""
    query = (
        select(Task)
        .where(Task.status == int(TaskStatus.QUEUED))
        .order_by(Task.priority.asc(), Task.id.asc())
        .limit(1)
    )
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True)
    return await session.scalar(query)


def public_event(task: Task) -> dict:
    """SSE 白名单事件（D-E）：只透传 phase/status/percent/当前工作/摘要/提示。"""
    progress = task.progress or {}
    allowed_keys = ("phase", "status", "percent", "current_work", "summary", "hint")
    event = {k: progress.get(k) for k in allowed_keys if k in progress}
    event.setdefault("phase", task.task_type)
    event.setdefault("status", TaskStatus(task.status).name.lower())
    event.setdefault("percent", 100 if task.status == int(TaskStatus.DONE) else 0)
    return event


async def run_task(session: AsyncSession, task: Task) -> Task:
    """执行单个任务（handler 由 HANDLERS 注册）。"""
    is_pg = session.bind is not None and session.bind.dialect.name == "postgresql"
    if is_pg:
        # RLS：worker 无用户上下文，按任务租户显式设置（会话级，跨提交生效）
        await session.execute(
            text("SELECT set_config('app.enterprise_id', :eid, false)"),
            {"eid": str(task.enterprise_id)},
        )
    task.status = int(TaskStatus.RUNNING)
    task.progress = {"phase": task.task_type, "status": "running", "percent": 10, "current_work": f"开始执行 {task.task_type}"}
    try:
        await session.commit()
        handler = HANDLERS.get(task.task_type)
        if handler is None:
            raise NotImplementedError(f"任务类型未实现：{task.task_type}")
        await handler(session, task)
        task.status = int(TaskStatus.DONE)
        task.progress = {"phase": task.task_type, "status": "done", "percent": 100, "summary": "完成"}
        task.finished_at = datetime.now(timezone.utc)
    except Exception as exc:  # noqa: BLE001
        task.retry_count += 1
        if task.retry_count >= MAX_RETRIES:
            task.status = int(TaskStatus.FAILED_TERMINAL)
            task.error = {"message": str(exc)}
            task.finished_at = datetime.now(timezone.utc)
            task.progress = {"phase": task.task_type, "status": "failed", "percent": 100, "hint": "重试耗尽，请人工处理"}
        else:
            task.status = int(TaskStatus.QUEUED)
            task.progress = {"phase": task.task_type, "status": "retrying", "percent": 5, "hint": f"失败，重试 {task.retry_count}/{MAX_RETRIES}"}
    finally:
        if is_pg:
            # 复位租户上下文，避免连接池复用泄漏
            await session.execute(
                text("SELECT set_config('app.enterprise_id', '', false)")
            )
        await session.commit()
    return task


async def run_next_task(session: AsyncSession) -> Task | None:
    task = await claim_next(session)
    if task is None:
        return None
    return await run_task(session, task)


async def _tender_parse_handler(session: AsyncSession, task: Task) -> None:
    """解析项目材料：doc_block + （门禁内）LLM 语义抽取 requirement。"""
    from app.services.file_service import reparse_file

    file_ids = task.payload.get("file_ids") or []
    if not file_ids:
        raise ValueError("payload.file_ids 为空")
    task.progress = {"phase": task.task_type, "status": "running", "percent": 30, "current_work": f"解析 {len(file_ids)} 个文件"}
    for file_id in file_ids:
        fobj = await reparse_file(session, int(file_id))
        if fobj.status != 3:
            raise ValueError(f"文件解析失败：{fobj.original_name}")
    result: dict = {"parsed_file_ids": [int(i) for i in file_ids], "requirements_extracted": 0}

    from sqlalchemy import select as sa_select

    from app.models.doc import DocBlock
    from app.services.llm import LLMClient, extract_json, llm_enabled
    from app.services import requirement_service

    if llm_enabled():
        blocks = (
            await session.scalars(
                sa_select(DocBlock).where(DocBlock.file_id.in_([int(i) for i in file_ids]))
            )
        ).all()
        text = "\n".join(b.text_content or "" for b in blocks)[:30000]
        system = (
            "你是投标文件解析助手。从招标材料中抽取资格要求、评分细则、否决条款、技术要求、报价规则、材料清单，"
            "输出 JSON 数组，每项含 req_type/content/structured/coordinates/confidence。"
            "只依据给定材料，禁止编造。"
        )
        reply = await LLMClient().chat(system, f"招标材料：\n{text}")
        items = extract_json(reply).get("requirements", [])
        for item in items:
            await requirement_service.upsert_requirement(
                session,
                enterprise_id=task.enterprise_id,
                project_id=task.project_id,
                req_type=item["req_type"],
                content=item["content"],
                structured=item.get("structured"),
                coordinates=item.get("coordinates") or [],
                confidence=item.get("confidence"),
                source_task_id=task.id,
            )
        result["requirements_extracted"] = len(items)
    else:
        result["note"] = "云模型门禁关闭，未做语义抽取（仅完成文本解析）"
    task.result = result


async def _bid_generate_handler(session: AsyncSession, task: Task) -> None:
    """生成/补全三份成果：确定性草稿（LLM 门禁内可选增强）。"""
    from sqlalchemy import select as sa_select

    from app.models.deliverable import Deliverable
    from app.models.enterprise_domain import EnterpriseFact
    from app.models.project import Project
    from app.models.requirement import Requirement
    from app.services import deliverable_service, quote_engine
    from app.services.history_provider import MockHistoryPriceProvider
    from app.services.llm import LLMClient, llm_enabled

    project_id = task.project_id
    requirements = (
        await session.scalars(
            sa_select(Requirement).where(
                Requirement.enterprise_id == task.enterprise_id,
                Requirement.project_id == project_id,
                Requirement.current.is_(True),
            )
        )
    ).all()
    req_by_type: dict[str, list[Requirement]] = {}
    for req in requirements:
        req_by_type.setdefault(req.req_type, []).append(req)

    facts = (
        await session.scalars(
            sa_select(EnterpriseFact).where(EnterpriseFact.enterprise_id == task.enterprise_id)
        )
    ).all()
    fact_lines = [f"{f.fact_key}: {f.fact_value.get('value', '')}" for f in facts]
    fact_text = "\n".join(fact_lines) or "（企业资料库暂无结构化事实）"

    project = await session.scalar(
        sa_select(Project).where(
            Project.id == project_id,
            Project.enterprise_id == task.enterprise_id,
        )
    )
    project_name = project.name if project is not None else f"项目{project_id}"

    def _business_model() -> dict:
        nodes = [
            {"id": "n1", "type": "heading", "text": "商务标"},
            {"id": "n2", "type": "paragraph", "text": f"项目名称：{project_name}"},
            {"id": "n3", "type": "paragraph", "text": "一、企业基本情况"},
            {"id": "n4", "type": "paragraph", "text": fact_text},
        ]
        idx = 5
        for req in req_by_type.get("basic_info", [])[:5]:
            nodes.append({"id": f"n{idx}", "type": "paragraph", "text": f"- {req.content}"})
            idx += 1
        for req in req_by_type.get("qualification", [])[:5]:
            nodes.append({"id": f"n{idx}", "type": "paragraph", "text": f"- 资格响应：{req.content}"})
            idx += 1
        nodes.append({"id": f"n{idx}", "type": "paragraph", "text": "（草稿由 BidVolt 确定性生成，待人工校核）"})
        return {"nodes": nodes}

    def _technical_model() -> dict:
        nodes = [
            {"id": "t1", "type": "heading", "text": "技术标"},
            {"id": "t2", "type": "paragraph", "text": f"项目名称：{project_name}"},
            {"id": "t3", "type": "paragraph", "text": "一、技术方案总体说明"},
        ]
        idx = 4
        for req in req_by_type.get("tech_requirement", [])[:10]:
            nodes.append({"id": f"t{idx}", "type": "paragraph", "text": f"- 技术要求响应：{req.content}"})
            idx += 1
        nodes.append({"id": f"t{idx}", "type": "paragraph", "text": "（草稿由 BidVolt 确定性生成，待人工校核）"})
        return {"nodes": nodes}

    async def _quote_model() -> dict:
        material_ref = task.payload.get("material_ref") or "CABLE-YJV-3x95"
        cost = float(task.payload.get("cost", 100))
        params = quote_engine.QuoteParams(
            material_ref=material_ref,
            cost=cost,
            min_profit_rate=float(task.payload.get("min_profit_rate", 0.05)),
        )
        try:
            samples = await MockHistoryPriceProvider().get_material_samples(material_ref)
        except Exception:  # noqa: BLE001
            samples = []
        try:
            result = quote_engine.calculate(params, samples)
            price = result["suggested"]
        except ValueError:
            price = None
        rows = [["材料", "数量", "单价", "小计"]]
        rows.append([material_ref, 1, price if price is not None else "待人工定价", price if price is not None else ""])
        rows.append(["说明", "", "", "确定性测算草稿，用户确认后生效"])
        return {"type": "sheet", "sheets": [{"name": "报价单", "rows": rows}]}

    models = {1: _business_model(), 2: _technical_model(), 3: await _quote_model()}
    if llm_enabled():
        client = LLMClient()
        business_text = "\n".join(n.get("text", "") for n in models[1]["nodes"])
        reply = await client.chat(
            "你是标书撰写助手。基于给定草稿改写为正式投标语言，只调整措辞与结构，禁止新增企业事实。直接输出正文。",
            f"商务标草稿：\n{business_text[:8000]}",
        )
        if reply.strip():
            models[1] = {
                "nodes": [
                    {"id": "llm1", "type": "heading", "text": "商务标"},
                    {"id": "llm2", "type": "paragraph", "text": reply.strip()},
                ]
            }
    versions: dict[int, int] = {}
    for dtype, model in models.items():
        deliverable = await session.scalar(
            sa_select(Deliverable).where(
                Deliverable.project_id == project_id,
                Deliverable.deliverable_type == dtype,
            )
        )
        if deliverable is None:
            deliverable = await deliverable_service.create_deliverable(
                session,
                enterprise_id=task.enterprise_id,
                project_id=project_id,
                deliverable_type=dtype,
                title={1: "商务标", 2: "技术标", 3: "报价单"}[dtype],
            )
        version = await deliverable_service.save_version(
            session,
            deliverable,
            model,
            version_type=2,
            idempotency_key=f"bidgen-{task.id}-{dtype}",
            source_task_id=task.id,
        )
        versions[dtype] = version.version_no

    if llm_enabled():
        task.result = {**versions, "note": "确定性草稿 + LLM 润色（门禁已开）"}
    else:
        task.result = {**versions, "note": "确定性草稿（云模型门禁关闭）"}


async def _material_match_handler(session: AsyncSession, task: Task) -> None:
    """资料匹配：requirements ↔ enterprise_asset（关键词命中），写 material_match_result。"""
    from sqlalchemy import select as sa_select

    from app.models.enterprise_domain import EnterpriseAsset
    from app.models.project_material import MaterialMatchResult
    from app.models.requirement import Requirement

    requirements = (
        await session.scalars(
            sa_select(Requirement).where(
                Requirement.enterprise_id == task.enterprise_id,
                Requirement.project_id == task.project_id,
                Requirement.current.is_(True),
            )
        )
    ).all()
    assets = (
        await session.scalars(
            sa_select(EnterpriseAsset).where(
                EnterpriseAsset.enterprise_id == task.enterprise_id,
                EnterpriseAsset.is_deleted.is_(False),
            )
        )
    ).all()

    results: list[dict] = []
    for req in requirements:
        key_type = next(
            (k for k in ("资质", "业绩", "人员", "检测报告", "证照", "产品参数") if k in req.content),
            None,
        )
        best: EnterpriseAsset | None = None
        if key_type is not None:
            best = next((a for a in assets if a.asset_type == key_type), None)
        matched = 1 if best is not None else 3
        session.add(
            MaterialMatchResult(
                enterprise_id=task.enterprise_id,
                project_id=task.project_id,
                requirement_id=req.id,
                asset_id=best.id if best else None,
                matched=matched,
                gap_desc=None if matched == 1 else "无足够匹配的企业资料",
                affected_score_item=req.req_type,
                suggestion="补充对应资质/业绩材料" if matched != 1 else None,
                source_task_id=task.id,
            )
        )
        results.append({"requirement_id": req.id, "matched": matched})
    task.result = {"matched_count": len(results), "results": results}


async def _chat_handler(session: AsyncSession, task: Task) -> None:
    """对话问答：门禁内走 LLM，门禁外返回规则化可用操作。"""
    from app.services.llm import LLMClient, llm_enabled

    message = task.payload.get("message", "")
    if llm_enabled():
        reply = await LLMClient().chat("你是 BidVolt 投标助手，用简洁中文回答。", message)
        task.result = {"reply": reply, "mode": "llm"}
    else:
        task.result = {
            "reply": "云模型门禁关闭。当前可执行任务：招标解析、资料匹配、标书生成、模拟评标、针对性修改。",
            "mode": "rule",
        }


async def _bid_review_handler(session: AsyncSession, task: Task) -> None:
    """校核模式：完整性、项目名称一致性、资格/技术要求覆盖（确定性）。"""
    from sqlalchemy import select as sa_select

    from app.models.deliverable import Deliverable
    from app.models.project import Project
    from app.models.requirement import Requirement
    from app.services import deliverable_service

    project = await session.scalar(
        sa_select(Project).where(
            Project.id == task.project_id,
            Project.enterprise_id == task.enterprise_id,
        )
    )
    if project is None:
        raise ValueError("项目不存在")
    deliverables = (
        await session.scalars(
            sa_select(Deliverable).where(
                Deliverable.project_id == task.project_id,
                Deliverable.enterprise_id == task.enterprise_id,
            )
        )
    ).all()

    issues: list[dict] = []
    texts: dict[int, str] = {}
    existing = {d.deliverable_type: d for d in deliverables}
    for dtype, name in ((1, "商务标"), (2, "技术标"), (3, "报价单")):
        d = existing.get(dtype)
        if d is None or d.current_version_no == 0:
            issues.append({"severity": "error", "message": f"缺少{name}", "locate": None})
            continue
        _, model = await deliverable_service.get_version_content(
            session, d.id, d.current_version_no
        )
        if d.deliverable_type == 3:
            text = " ".join(str(c) for sheet in model.get("sheets", []) for row in sheet.get("rows", []) for c in row)
        else:
            text = "\n".join(n.get("text", "") for n in model.get("nodes", []))
        texts[dtype] = text
        if project.name not in text:
            issues.append({"severity": "warning", "message": f"{name}未包含项目名称：{project.name}", "locate": d.id})

    requirements = (
        await session.scalars(
            sa_select(Requirement).where(
                Requirement.enterprise_id == task.enterprise_id,
                Requirement.project_id == task.project_id,
                Requirement.current.is_(True),
            )
        )
    ).all()
    for req in requirements:
        target = "技术标" if req.req_type == "tech_requirement" else ("商务标" if req.req_type == "qualification" else None)
        if target is None:
            continue
        dtype = 2 if target == "技术标" else 1
        text = texts.get(dtype, "")
        if not text or req.content[:10] not in text:
            issues.append(
                {
                    "severity": "error",
                    "message": f"{target}未响应要求：{req.content[:40]}",
                    "locate": req.id,
                }
            )

    task.result = {"issues": issues, "issue_count": len(issues)}


HANDLERS: dict[str, object] = {
    TaskType.TENDER_PARSE: _tender_parse_handler,
    TaskType.BID_GENERATE: _bid_generate_handler,
    TaskType.MATERIAL_MATCH: _material_match_handler,
    TaskType.CHAT: _chat_handler,
    TaskType.BID_REVIEW: _bid_review_handler,
}
