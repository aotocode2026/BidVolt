"""任务编排（4.4.3/4.4.6）：创建、领取、执行、状态机、白名单事件。"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import TaskStatus, TaskType
from app.models.task import Task

MAX_RETRIES = 3
# 任务租约（Issue #3：worker 中断后任务不得永久卡在 RUNNING）
LEASE_SECONDS = 600  # 领取后最长独占时间；到期未续期视为 worker 失联（LLM 慢调用留足余量）
HEARTBEAT_INTERVAL = 90  # 执行期间心跳续期间隔（< LEASE_SECONDS 的一半）
HEARTBEAT_LOCK_TIMEOUT_MS = 2000  # 心跳 UPDATE 等行锁上限（长 handler 持锁时放弃本轮，下轮再试）


def _aware(dt: datetime | None) -> datetime:
    """SQLite 读回的 datetime 可能为 naive，统一按 timezone.utc 解释以便比较。"""
    if dt is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


async def _set_rls_context(session: AsyncSession, enterprise_id: int) -> None:
    """PG：为【当前事务】注入租户 RLS 上下文（与 API 依赖一致，is_local=true）。

    事务级设置随 commit/rollback 自动失效，不随连接归还池而泄漏；代价是
    worker 路径上每一次 commit 之后、下一笔业务表写入之前都必须重新调用。
    （生产 Issue #8 根因：曾用会话级 set_config(..., false)，在 asyncpg 连接池下
    上下文随连接漂移/丢失，导致 requirement_revision 等表 INSERT 随机触发
    row-level security 策略违例。）
    """
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        await session.execute(
            text("SELECT set_config('app.enterprise_id', :eid, true)"),
            {"eid": str(enterprise_id)},
        )


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


async def reclaim_stale(session: AsyncSession) -> bool:
    """回收租约过期的 RUNNING 任务（worker 被强杀/卡死后恢复，避免任务永久卡住）。

    过期任务按重试计次：未达上限 → 重新入队（QUEUED，清租约）；达上限 → FAILED_TERMINAL。
    返回是否有任务被回收（有则调用方可立即再领取）。
    """
    now = datetime.now(timezone.utc)
    candidates = (
        await session.scalars(
            select(Task)
            .where(
                Task.status == int(TaskStatus.RUNNING),
                Task.lease_expires_at.is_not(None),
            )
            .order_by(Task.lease_expires_at.asc())
            .limit(10)
        )
    ).all()
    for task in candidates:
        if _aware(task.lease_expires_at) >= now:
            continue  # 租约仍有效（持有 worker 正常执行中）
        task.retry_count += 1
        if task.retry_count >= MAX_RETRIES:
            task.status = int(TaskStatus.FAILED_TERMINAL)
            task.error = {"message": "worker 中断且重试耗尽（租约过期回收）"}
            task.finished_at = now
            task.progress = {
                "phase": task.task_type,
                "status": "failed",
                "percent": 100,
                "hint": "执行进程中断且重试耗尽，请人工处理",
            }
        else:
            task.status = int(TaskStatus.QUEUED)
            task.progress = {
                "phase": task.task_type,
                "status": "retrying",
                "percent": 5,
                "hint": f"执行进程中断，租约过期后重新入队（{task.retry_count}/{MAX_RETRIES}）",
            }
        task.lease_owner = None
        task.lease_expires_at = None
        await session.commit()
        return True
    return False


async def _heartbeat_loop(lease_owner: str, task: Task, session_factory) -> None:
    """独立会话续期租约：不经过 handler 的事务，避免提交半成品写入。"""
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL)
        now = datetime.now(timezone.utc)
        hb = None
        try:
            hb = session_factory()
            async with hb as session:
                # PG 下设置短 lock_timeout：handler 长时间持行锁时放弃本轮，避免心跳排队
                if session.bind is not None and session.bind.dialect.name == "postgresql":
                    await session.execute(text(f"SET LOCAL lock_timeout = '{HEARTBEAT_LOCK_TIMEOUT_MS}ms'"))
                await session.execute(
                    update(Task)
                    .where(
                        Task.id == task.id,
                        Task.lease_owner == lease_owner,
                        Task.status == int(TaskStatus.RUNNING),
                    )
                    .values(
                        lease_expires_at=now + timedelta(seconds=LEASE_SECONDS),
                        last_heartbeat_at=now,
                    )
                )
                await session.commit()
        except Exception:  # noqa: BLE001 心跳失败不致命，下一轮重试（SQLite 锁/PG 锁冲突）
            pass


async def run_task(
    session: AsyncSession,
    task: Task,
    *,
    lease_owner: str | None = None,
    session_factory=None,
) -> Task:
    """执行单个任务（handler 由 HANDLERS 注册）。

    lease_owner 提供时：领取即置租约（防 worker 中断后任务永久卡 RUNNING），
    session_factory 提供时额外启动心跳续期（须为可创建独立会话的工厂，如 SessionLocal）。
    """
    is_pg = session.bind is not None and session.bind.dialect.name == "postgresql"
    if is_pg:
        # RLS：worker 无用户上下文，按任务租户显式设置（事务级，随事务失效）
        await _set_rls_context(session, task.enterprise_id)
    now = datetime.now(timezone.utc)
    task.status = int(TaskStatus.RUNNING)
    task.progress = {"phase": task.task_type, "status": "running", "percent": 10, "current_work": f"开始执行 {task.task_type}"}
    if lease_owner is not None:
        task.lease_owner = lease_owner
        task.lease_expires_at = now + timedelta(seconds=LEASE_SECONDS)
        task.last_heartbeat_at = now
    heartbeat = None
    try:
        await session.commit()
        if is_pg:
            # 上面 commit 结束事务 → 事务级 RLS 上下文随之失效；
            # handler 写业务表（requirement/material_match_result 等）前必须重建
            await _set_rls_context(session, task.enterprise_id)
        if lease_owner is not None and session_factory is not None:
            heartbeat = asyncio.create_task(_heartbeat_loop(lease_owner, task, session_factory))
        handler = HANDLERS.get(task.task_type)
        if handler is None:
            raise NotImplementedError(f"任务类型未实现：{task.task_type}")
        await handler(session, task)
        task.status = int(TaskStatus.DONE)
        task.progress = {"phase": task.task_type, "status": "done", "percent": 100, "summary": "完成"}
        task.finished_at = datetime.now(timezone.utc)
    except Exception as exc:  # noqa: BLE001
        # 回滚 handler 的部分写入，避免失败任务产生副作用（A-4 单事务原子性）
        await session.rollback()
        await session.refresh(task)
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
        if heartbeat is not None:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
        if task.status == int(TaskStatus.RUNNING) and lease_owner is not None:
            # 执行被中断（取消/停机），未走成功/失败路径：按失败计次，避免永久卡 RUNNING
            await session.rollback()
            task.retry_count += 1
            now = datetime.now(timezone.utc)
            if task.retry_count >= MAX_RETRIES:
                task.status = int(TaskStatus.FAILED_TERMINAL)
                task.error = {"message": "执行被中断且重试耗尽"}
                task.finished_at = now
                task.progress = {"phase": task.task_type, "status": "failed", "percent": 100, "hint": "执行被中断且重试耗尽，请人工处理"}
            else:
                task.status = int(TaskStatus.QUEUED)
                task.progress = {"phase": task.task_type, "status": "retrying", "percent": 5, "hint": f"执行被中断，重新入队（{task.retry_count}/{MAX_RETRIES}）"}
        # 终态/重新入队后释放租约，避免过期后被重复回收
        if lease_owner is not None and task.lease_owner == lease_owner:
            task.lease_owner = None
            task.lease_expires_at = None
        # （RLS 上下文为事务级，随事务自动清理，无需复位——复位反而会泄漏到连接池）
        # 最终提交兜底（生产定位 Issue #8）：提交失败时任务状态必须确定性落库，
        # 不能把任务留在 RUNNING 上等租约回收兜底（表现为 15 分钟无意义重试循环）。
        try:
            await session.commit()
        except Exception as commit_exc:  # noqa: BLE001
            await session.rollback()
            try:
                await session.refresh(task)
                task.retry_count += 1
                commit_now = datetime.now(timezone.utc)
                if task.retry_count >= MAX_RETRIES:
                    task.status = int(TaskStatus.FAILED_TERMINAL)
                    task.error = {"message": f"任务状态提交失败且重试耗尽：{commit_exc}"}
                    task.finished_at = commit_now
                    task.progress = {"phase": task.task_type, "status": "failed", "percent": 100, "hint": "任务状态提交失败且重试耗尽，请人工处理"}
                else:
                    task.status = int(TaskStatus.QUEUED)
                    task.progress = {"phase": task.task_type, "status": "retrying", "percent": 5, "hint": f"状态提交失败，重新入队（{task.retry_count}/{MAX_RETRIES}）"}
                task.lease_owner = None
                task.lease_expires_at = None
                try:
                    await session.commit()
                except Exception:  # noqa: BLE001  二次提交仍失败则交还租约回收兜底
                    await session.rollback()
            except Exception:  # noqa: BLE001
                await session.rollback()
    return task


async def run_next_task(session: AsyncSession) -> Task | None:
    task = await claim_next(session)
    if task is None:
        return None
    return await run_task(session, task)


async def _llm_extract_requirements(
    session: AsyncSession,
    *,
    enterprise_id: int,
    project_id: int,
    file_ids: list[int],
    task_id: int,
) -> int:
    """从文件文本块抽取 Requirement（门禁内 LLM）。返回抽取条数；门禁关闭返回 0。"""
    from sqlalchemy import select as sa_select

    from app.models.doc import DocBlock
    from app.services import requirement_service
    from app.services.llm import LLMClient, extract_json, llm_enabled

    if not llm_enabled():
        return 0
    file_ids = [int(i) for i in file_ids]
    if not file_ids:
        return 0
    blocks = (
        await session.scalars(sa_select(DocBlock).where(DocBlock.file_id.in_(file_ids)))
    ).all()
    text = "\n".join(b.text_content or "" for b in blocks)[:30000]
    if not text.strip():
        return 0
    system = (
        "你是投标文件解析助手。从招标材料中抽取资格要求、评分细则、否决条款、技术要求、报价规则、材料清单，"
        "输出 JSON 数组，每项含 req_type/content/structured/coordinates/confidence。"
        "只依据给定材料，禁止编造。"
    )
    reply = await LLMClient().chat(system, f"招标材料：\n{text}")
    parsed = extract_json(reply)
    if isinstance(parsed, dict):
        items = parsed.get("requirements", [])
    elif isinstance(parsed, list):
        items = parsed
    else:
        items = []
    if not items:
        # 抽取为空重试一次（Issue #8：解析 0 条不能静默通过）——更严格的纯 JSON 指令
        reply = await LLMClient().chat(
            system + "只输出 JSON 数组本身，禁止任何解释、Markdown 围栏或额外文字；确实没有可抽取项时输出 []。",
            f"招标材料：\n{text[:20000]}",
        )
        parsed = extract_json(reply)
        if isinstance(parsed, dict):
            items = parsed.get("requirements", [])
        elif isinstance(parsed, list):
            items = parsed
        else:
            items = []
    for item in items:
        await requirement_service.upsert_requirement(
            session,
            enterprise_id=enterprise_id,
            project_id=project_id,
            req_type=item["req_type"],
            content=item["content"],
            structured=item.get("structured"),
            coordinates=item.get("coordinates") or [],
            confidence=item.get("confidence"),
            source_task_id=task_id,
        )
    return len(items)


async def _tender_parse_handler(session: AsyncSession, task: Task) -> None:
    """解析项目材料：doc_block + （门禁内）LLM 语义抽取 requirement。"""
    from app.services.file_service import reparse_file

    file_ids = task.payload.get("file_ids") or []
    if not file_ids:
        raise ValueError("payload.file_ids 为空")
    task.progress = {"phase": task.task_type, "status": "running", "percent": 30, "current_work": f"解析 {len(file_ids)} 个文件"}
    # 进度立即落库：对外可见（SSE/API），并释放 task 行锁，避免阻塞心跳续期
    await session.commit()
    # 关键（生产定位 Issue #8）：RLS 上下文是事务级的，上面 commit 后即被清空；
    # 必须重建，否则后续 requirement_revision 等业务表 INSERT 会违反 RLS 策略
    await _set_rls_context(session, task.enterprise_id)
    for file_id in file_ids:
        fobj = await reparse_file(session, int(file_id))
        if fobj.status != 3:
            raise ValueError(f"文件解析失败：{fobj.original_name}")

    extracted = await _llm_extract_requirements(
        session,
        enterprise_id=task.enterprise_id,
        project_id=task.project_id,
        file_ids=file_ids,
        task_id=task.id,
    )
    result: dict = {"parsed_file_ids": [int(i) for i in file_ids], "requirements_extracted": extracted}
    from app.services.llm import llm_enabled

    if not llm_enabled():
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

    # 关键输入自检（Issue #8）：requirements 为空但项目已有材料时，生成前先行解析抽取，
    # 避免"上传了真实招标文件却未解析"导致成果退化为通用文本。
    from app.models.file import FileObject

    material_file_ids = [
        int(i)
        for i in (
            await session.scalars(
                sa_select(FileObject.id).where(
                    FileObject.project_id == project_id,
                    FileObject.enterprise_id == task.enterprise_id,
                    FileObject.is_deleted.is_(False),
                    FileObject.owner_type == 2,
                )
            )
        ).all()
    ]
    if not requirements and material_file_ids:
        extracted_count = await _llm_extract_requirements(
            session,
            enterprise_id=task.enterprise_id,
            project_id=project_id,
            file_ids=material_file_ids,
            task_id=task.id,
        )
        if extracted_count > 0:
            requirements = (
                await session.scalars(
                    sa_select(Requirement).where(
                        Requirement.enterprise_id == task.enterprise_id,
                        Requirement.project_id == project_id,
                        Requirement.current.is_(True),
                    )
                )
            ).all()
            req_by_type = {}
            for req in requirements:
                req_by_type.setdefault(req.req_type, []).append(req)
        pre_extracted = extracted_count
    else:
        pre_extracted = None

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

    # 当前项目材料文本块（生成依据，逐段响应要求）
    from app.models.doc import DocBlock
    from app.models.file import FileObject

    material_blocks = (
        await session.scalars(
            sa_select(DocBlock)
            .join(FileObject, FileObject.id == DocBlock.file_id)
            .where(
                FileObject.project_id == project_id,
                FileObject.enterprise_id == task.enterprise_id,
                FileObject.is_deleted.is_(False),
            )
            .order_by(DocBlock.file_id, DocBlock.block_index)
        )
    ).all()
    material_text = "\n".join(b.text_content or "" for b in material_blocks)[:30000] or "（项目未上传招标材料）"

    # 历史知识检索（Issue #4）：为技术标提供专业写法素材（来源可追溯，结果仅入任务元数据）
    knowledge_refs: list[dict] = []
    if llm_enabled():
        from app.services import knowledge_service

        kn_query = " ".join(
            [project_name or "", *[r.content for r in req_by_type.get("tech_requirement", [])[:3]]]
        )[:200].strip() or (project_name or "")
        if kn_query:
            try:
                kn = await knowledge_service.search_knowledge(
                    session,
                    enterprise_id=task.enterprise_id,
                    query=kn_query,
                    project_id=project_id,
                    top_k=5,
                )
                knowledge_refs = kn["items"]
            except Exception:  # noqa: BLE001  检索失败不阻塞生成
                knowledge_refs = []
    knowledge_text = "\n".join(
        f"- [{i['source_type']}]{i['file_name']}：{i['snippet']}" for i in knowledge_refs
    ) or "（无历史参考素材）"

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
    enhanced: list[str] = []
    if llm_enabled():
        import re

        client = LLMClient()

        def _clean_md(text: str) -> str:
            """清洗 Markdown 标记（Issue #8）：正式成果节点应为纯文本，不出现 **/###/- 等符号。"""
            cleaned = re.sub(r"[*_`]{1,3}", "", text)
            cleaned = re.sub(r"^#{1,6}\s*", "", cleaned)
            return cleaned.strip()

        def _split_nodes(text: str) -> list[dict]:
            """把 LLM 输出的 Markdown 拆成段落/标题节点（段落按空行合并，正文清洗 Markdown 标记）。"""
            nodes: list[dict] = []
            buf: list[str] = []
            idx = 0

            def flush() -> None:
                nonlocal idx
                if buf:
                    content = _clean_md("\n".join(buf))
                    if content:
                        nodes.append({"id": f"llm-n{idx}", "type": "paragraph", "text": content})
                        idx += 1
                    buf.clear()

            for raw in text.splitlines():
                line = raw.rstrip()
                if re.match(r"^#{1,4}\s", line):
                    flush()
                    title = _clean_md(line.lstrip("#"))
                    if title:
                        nodes.append({"id": f"llm-n{idx}", "type": "heading", "text": title})
                        idx += 1
                elif line.strip():
                    buf.append(line.strip())
                else:
                    flush()
            flush()
            return nodes or [{"id": "llm-n0", "type": "paragraph", "text": _clean_md(text.strip())}]

        # 商务标：正式投标语言改写（禁止新增企业事实）
        business_req_text = "\n".join(
            f"- {r.content}"
            for key in req_by_type
            if key not in ("tech_requirement", "quote_rule")
            for r in req_by_type[key][:10]
        ) or "（未解析到资格/商务要求）"
        business_draft = "\n".join(n.get("text", "") for n in models[1]["nodes"])
        try:
            business_reply = await client.chat(
                "你是投标文件撰写助手。基于给定草稿与要求改写为正式投标语言，"
                "只调整措辞与结构，禁止新增企业事实。直接输出正文。",
                f"项目名称：{project_name}\n招标人：{project.buyer if project else '未提供'}\n"
                f"企业事实：\n{fact_text}\n资格/商务要求：\n{business_req_text}\n草稿：\n{business_draft[:6000]}",
            )
        except Exception:  # noqa: BLE001  LLM 失败回退确定性草稿
            business_reply = ""
        if business_reply.strip():
            models[1] = {
                "nodes": [
                    {"id": "llm-b0", "type": "heading", "text": "商务标"},
                    *_split_nodes(business_reply.strip()),
                ]
            }
            enhanced.append("business")

        # 技术标：全文生成（逐条响应技术要求 + 历史素材作专业写法参考）
        tech_req_text = "\n".join(
            f"- {r.content}" for r in req_by_type.get("tech_requirement", [])[:20]
        ) or "（未解析到技术要求，按当前材料内容组织）"
        try:
            tech_reply = await client.chat(
                "你是投标文件撰写助手，撰写《技术标》正式正文。要求：\n"
                "1. 分章节组织（技术方案总体说明、主要技术参数及响应、生产与供货组织、质量保障、售后服务、进度与交付等，按招标要求取舍），用 ## 分章；\n"
                "2. 逐条响应给定技术要求，禁止遗漏；\n"
                "3. 只依据给定材料与历史参考素材中的通用专业写法，禁止编造企业事实、业绩、人员、证书；\n"
                "4. 禁止沿用历史项目名称、招标人、金额、工期、人员姓名；\n"
                "5. 资料不足处明确标注【待补充】；\n"
                "6. 直接输出 Markdown 正文。",
                f"项目名称：{project_name}\n招标人：{project.buyer if project else '未提供'}\n"
                f"技术要求：\n{tech_req_text}\n"
                f"当前招标材料摘录：\n{material_text[:12000]}\n"
                f"历史参考素材（仅作专业写法参考，不得复制其中项目事实）：\n{knowledge_text[:4000]}\n"
                f"企业产品/能力事实：\n{fact_text[:3000]}",
            )
        except Exception:  # noqa: BLE001  LLM 失败回退确定性草稿
            tech_reply = ""
        if tech_reply.strip():
            models[2] = {
                "nodes": [
                    {"id": "llm-t0", "type": "heading", "text": "技术标"},
                    *_split_nodes(tech_reply.strip()),
                ]
            }
            enhanced.append("technical")
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

    # 评审闭环（Issue #8）：生成后自动执行内置评审 + 要求覆盖检查，评审结果绑定当前成果版本
    from app.services import review_service

    review_info = await review_service.run_evaluation(
        session, enterprise_id=task.enterprise_id, project_id=project_id
    )
    issues = await _review_issues(session, enterprise_id=task.enterprise_id, project_id=project_id)
    error_count = sum(1 for i in issues if i.get("severity") == "error")
    requirement_count = len(requirements)
    deliverables_ready = requirement_count > 0 and error_count == 0
    quality = {
        "requirements_count": requirement_count,
        "pre_extracted": pre_extracted,
        "review_run_id": review_info["run_id"],
        "score_id": review_info["score_id"],
        "issue_count": len(issues),
        "error_count": error_count,
        "deliverables_ready": deliverables_ready,
    }
    if llm_enabled():
        task.result = {
            **versions,
            "note": (
                ("正式成果草稿（待人工校核）" if not deliverables_ready else "确定性草稿 + LLM 全文生成")
                + (f"（增强：{','.join(enhanced)}）" if enhanced else ("（LLM 不可用，草稿回退）" if not deliverables_ready else "") )
            ),
            "quality": quality,
            "issues": issues,
            "knowledge_refs": [
                {"file_name": i["file_name"], "project_id": i["project_id"], "source_type": i["source_type"]}
                for i in knowledge_refs
            ],
        }
    else:
        task.result = {**versions, "note": "确定性草稿（云模型门禁关闭）", "quality": quality, "issues": issues}


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


async def _review_issues(session: AsyncSession, *, enterprise_id: int, project_id: int) -> list[dict]:
    """校核模式（共享）：完整性、项目名称一致性、资格/技术要求覆盖（确定性）。"""
    from sqlalchemy import select as sa_select

    from app.models.deliverable import Deliverable
    from app.models.project import Project
    from app.models.requirement import Requirement
    from app.services import deliverable_service

    project = await session.scalar(
        sa_select(Project).where(
            Project.id == project_id,
            Project.enterprise_id == enterprise_id,
        )
    )
    if project is None:
        raise ValueError("项目不存在")
    deliverables = (
        await session.scalars(
            sa_select(Deliverable).where(
                Deliverable.project_id == project_id,
                Deliverable.enterprise_id == enterprise_id,
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
                Requirement.enterprise_id == enterprise_id,
                Requirement.project_id == project_id,
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
    return issues


async def _bid_review_handler(session: AsyncSession, task: Task) -> None:
    """校核任务：跑覆盖检查并写任务结果（评审闭环也可由 bid_generate 自动触发）。"""
    issues = await _review_issues(session, enterprise_id=task.enterprise_id, project_id=task.project_id)
    task.result = {"issues": issues, "issue_count": len(issues)}


HANDLERS: dict[str, object] = {
    TaskType.TENDER_PARSE: _tender_parse_handler,
    TaskType.BID_GENERATE: _bid_generate_handler,
    TaskType.MATERIAL_MATCH: _material_match_handler,
    TaskType.CHAT: _chat_handler,
    TaskType.BID_REVIEW: _bid_review_handler,
}
