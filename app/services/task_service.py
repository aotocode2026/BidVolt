"""任务编排（4.4.3/4.4.6）：创建、领取、执行、状态机、白名单事件。"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, text, update
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
    task: Task | None = None,
) -> tuple[int, str | None]:
    """从文件文本块抽取 Requirement（门禁内 LLM）。返回 (抽取条数, 警示文本或 None)；门禁关闭返回 (0, None)。

    task 提供时全程更新进度并落库（Issue #13：LLM 抽取阶段持续数分钟，
    此前任务一直显示 queued 0%，产品以为"解析不出来"——必须让用户看到"AI 抽取中"）。

    模型输出没有可解析 JSON 时：带更明确输出提示重试一次；仍失败不炸任务，
    降级为 0 条并以警示文本显式回报（积极报错，不静默）。"""
    from sqlalchemy import select as sa_select

    from app.models.doc import DocBlock
    from app.services import requirement_service
    from app.services.llm import llm_enabled, try_extract_json

    async def _progress(percent: int, work: str) -> None:
        if task is None:
            return
        task.progress = {"phase": task.task_type, "status": "running", "percent": percent, "current_work": work}
        await session.commit()
        await _set_rls_context(session, task.enterprise_id)  # 事务级 RLS 上下文随 commit 失效，必须重建

    def _items_of(parsed) -> list:
        if isinstance(parsed, dict):
            return parsed.get("requirements") or []
        if isinstance(parsed, list):
            return parsed
        return []

    if not llm_enabled():
        return 0, None
    file_ids = [int(i) for i in file_ids]
    if not file_ids:
        return 0, None
    blocks = (
        await session.scalars(sa_select(DocBlock).where(DocBlock.file_id.in_(file_ids)))
    ).all()
    text = _dedup_block_texts(blocks, limit=60000)
    if not text.strip():
        return 0, None
    system = (
        "阅读下面这份招标材料，把其中对投标人的要求整理出来：资格要求、评分细则、否决条款、"
        "技术要求、报价规则、材料清单。输出 JSON 数组，每项含 req_type/content/structured/coordinates/confidence。"
        "content 写该要求的完整原句。限价要求把 structured 写成 {\"price_limit\": {\"amount\": 数值, \"unit\": \"万元\"}}；"
        "评分细则把 structured 写成 {\"score_rule\": {\"weight\": 分值, \"criterion\": \"评分标准\"}}。"
        "材料里有的才写。"
    )
    json_hint = "请把结果用 JSON 输出：只输出 JSON 本身，开头结尾不加解释文字，不加 Markdown 代码块围栏。"
    await _progress(45, "AI 抽取要求中（大文件需数分钟，请耐心等待）")
    try:
        reply = await _chat_with_retry(system, f"招标材料：\n{text}")
        parsed = try_extract_json(reply)
        if parsed is None:
            reply = await _chat_with_retry(system, f"招标材料：\n{text}\n\n{json_hint}")
            parsed = try_extract_json(reply)
        items = _items_of(parsed)
        if not items and parsed is not None:
            # 抽取为空重试一次（Issue #8：解析 0 条不能静默通过）——更严格的纯 JSON 指令
            await _progress(55, "首轮抽取为空，重试更严格指令…")
            reply = await _chat_with_retry(
                system + "输出 JSON 数组本身（不附加解释文字、Markdown 围栏或额外文字）；确实没有可抽取项时输出 []。",
                f"招标材料：\n{text[:20000]}",
            )
            parsed = try_extract_json(reply)
            items = _items_of(parsed)
    except Exception as exc:  # noqa: BLE001 云模型暂不可用时如实降级，不炸整个解析任务
        await _progress(60, "AI 抽取未完成（云模型暂不可用）…")
        return 0, (
            f"AI 要求抽取未执行（云模型暂不可用：{type(exc).__name__}）："
            "请稍后重新触发【招标解析】，或到要求页人工录入招标要求"
        )
    warning = None
    if parsed is None:
        warning = (
            "AI 要求抽取未获得可解析结果（模型输出中没有 JSON，已重试一次）："
            "请到要求页人工录入招标要求，或重新触发【招标解析】"
        )
    await _progress(60, f"AI 抽取完成（{len(items)} 条候选），正在过滤落库…")
    # 碎片与重复过滤（Issue #12）：历史重复块会诱导 LLM 输出"报价限价(万元)"这类孤立碎片；
    # 丢弃过短内容、纯数字/单位/表头碎片与重复条目，保证落库要求为完整整句。
    import re as _re

    kept: list[dict] = []
    seen_norm: set[str] = set()
    for item in items:
        content = str(item.get("content") or "").strip()
        norm = _re.sub(r"\s+", "", content)
        if len(content) < 5:
            continue
        if _re.fullmatch(r"[\d.，,、（）()元万元%|∶:：·\-—]+", content):
            continue
        if norm in seen_norm:
            continue
        seen_norm.add(norm)
        kept.append(item)
    for item in kept:
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
    return len(kept), warning


def _dedup_block_texts(blocks, limit: int = 30000) -> str:
    """把 DocBlock 列表拼成去重后的文本（Issue #12）：同一文本只保留一次，
    历史重复解析块（旧数据 6 倍重复）不再污染 LLM 提示词。"""
    seen: set[str] = set()
    lines: list[str] = []
    for b in blocks:
        t = (b.text_content or "").strip()
        if t and t not in seen:
            seen.add(t)
            lines.append(t)
    return "\n".join(lines)[:limit]


async def _derive_tender_meta(material_text: str) -> dict:
    """从招标文件开头（封面/采购公告）抽取项目名称与招标人。

    确定性正则优先（封面常按"名称 编号 招标人 采购方式"连排），LLM 小窗口兜底。
    材料里没有的保持 None——系统项目名只是工作台标签，不能写进应答函/委托书等正式字段。"""
    import re as _re

    from app.services.llm import llm_enabled, try_extract_json

    meta: dict = {}
    head = (material_text or "")[:4000]
    if not head.strip():
        return meta
    # 封面连排：项目名 采购编号 招标人 采购方式
    m = _re.search(
        r"([^\s\n]{8,60}?(?:采购|招标|磋商|谈判)[^\s\n]{0,40}?)\s+"
        r"([A-Za-z0-9][A-Za-z0-9\-/]{4,24})\s+([^\s\n]{4,40}?)\s*"
        r"(?:竞争性谈判|公开招标|询价|单一来源|竞争性磋商|竞价)",
        head,
    )
    if m:
        if any(k in m.group(1) for k in ("采购", "招标", "磋商", "谈判", "项目")):
            meta["project_name"] = m.group(1)
        if m.group(3) not in ("采购人", "招标人") and len(m.group(3)) >= 4:
            meta["buyer"] = m.group(3)
    # 显式标注兜底（排除模板自引用"项目名称：（项目名称）"这类占位）
    if not meta.get("project_name"):
        m2 = _re.search(r"项目名称[：:]\s*([^\s\n，,。；;（）()]{6,60})", head)
        if m2:
            meta["project_name"] = m2.group(1)
    if not meta.get("buyer"):
        m3 = _re.search(r"(?:招标人|采购人)[：:]\s*([^\s\n，,。；;（）()]{4,40})", head)
        if m3:
            meta["buyer"] = m3.group(1)
    if llm_enabled():
        try:
            reply = await _chat_with_retry(
                "阅读招标文件开头的封面与采购公告，找出采购项目名称与招标人名称，"
                '输出 JSON：{"project_name": "...", "buyer": "..."}；材料里没有的填 null。输出 JSON 本身。',
                "招标文件开头：\n" + head,
            )
            llm_meta = try_extract_json(reply)
            if isinstance(llm_meta, dict):
                for k in ("project_name", "buyer"):
                    v = str(llm_meta.get(k) or "").strip()
                    if v and v not in ("null", "None") and not meta.get(k):
                        meta[k] = v
        except Exception:  # noqa: BLE001 抽取失败保持确定性结果
            pass
    # 清洗：纯占位符（如"（采购人）"）或过短的值不采纳；含真实文字的括号（如"（第一批）"）保留
    for k in list(meta):
        v = str(meta[k])
        core = v.strip("（）()【】")
        if "_" in v or len(core) < 4 or core in ("采购人", "招标人", "项目名称", "响应供应商名称"):
            meta.pop(k, None)
    return meta


async def _chat_with_retry(system: str, user: str, times: int = 2) -> str:
    """LLM 调用带瞬时故障重试：云模型高峰期常见 529/5xx/限流，
    一次失败不应当让整个解析/生成任务报废。"""
    from app.services.llm import LLMClient

    last: Exception | None = None
    for attempt in range(max(1, int(times))):
        try:
            return await LLMClient().chat(system, user)
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < int(times) - 1:
                await asyncio.sleep(2 * (attempt + 1))
    raise last  # type: ignore[misc]


_STRUCTURE_SYSTEM = (
    "阅读招标文件里'响应文件格式'这一章。这一章分'价格文件''商务文件''技术文件'三部分，"
    "每部分按编号列出了应答人要提交的文件条目。"
    "把三部分里的文件条目整理出来，输出 JSON："
    '{"business": [{"title": "条目名称（用原文编号与名称，如（一）法定代表人（单位负责人）授权委托书）", '
    '"guide": "这一条目的填写说明摘要"}], "technical": [...], "price": [...], '
    '"format": {"font": "...", "size": "...", "line_spacing": "...", "toc_required": true|false, "other": "..."}, '
    '"notes": "..."}。'
    "format 只在招标文件写了字体/字号/版式要求时给出，否则填 null。"
    "招标文件里没有这份组成清单时，对应数组返回 []。"
)


def _parse_structure_reply(reply: str) -> dict[str, list[tuple[str, str]]]:
    """把结构解析的 LLM 输出规整为 {business: [(title, guide)], technical: [...], price: [...]}。"""
    from app.services.llm import extract_json as _extract_json

    if not reply or not reply.strip():
        return {}
    try:
        parsed = _extract_json(reply)
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(parsed, dict):
        return {}

    def _norm(chapters: list) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for c in chapters or []:
            if isinstance(c, dict) and c.get("title"):
                title = str(c["title"]).strip()
                guide = str(c.get("guide") or "按招标文件该部分要求撰写，内容详实").strip()
                out.append((title, guide))
        return out

    return {
        "business": _norm(parsed.get("business") or []),
        "technical": _norm(parsed.get("technical") or []),
        "price": _norm(parsed.get("price") or []),
        "format": parsed.get("format") if isinstance(parsed.get("format"), dict) else None,
    }


async def _llm_extract_structure(
    session: AsyncSession,
    *,
    enterprise_id: int,
    project_id: int,
    file_ids: list[int],
    task: Task | None = None,
) -> dict:
    """解析阶段确认标书结构：从招标材料提取响应文件格式要求，
    落库为 req_type=doc_structure 的要求（structured 携带 role/guide/order）。
    返回 {"count": n, "error": str|None}——失败原因必须向上透出（禁止静默返回 0）。"""
    from sqlalchemy import select as sa_select

    from app.models.doc import DocBlock
    from app.services import requirement_service
    from app.services.llm import llm_enabled

    async def _progress(percent: int, work: str) -> None:
        if task is None:
            return
        task.progress = {"phase": task.task_type, "status": "running", "percent": percent, "current_work": work}
        await session.commit()
        await _set_rls_context(session, task.enterprise_id)

    if not llm_enabled():
        return {"count": 0, "error": "云模型门禁关闭，未做结构解析"}
    file_ids = [int(i) for i in file_ids]
    if not file_ids:
        return {"count": 0, "error": None}
    blocks = (
        await session.scalars(sa_select(DocBlock).where(DocBlock.file_id.in_(file_ids)))
    ).all()
    full_text = _dedup_block_texts(blocks, limit=200000)
    if not full_text.strip():
        return {"count": 0, "error": "材料无可提取文本"}
    # 大文件开窗（Issue #8：412KB 采购文件从头部截 2 万字，漏掉"响应文件格式/模板要求"章节，
    # 导致结构抽取 0 条、成果退化为通用结构）：优先取"响应/应答文件格式"相关窗口，
    # 让模板要求章节进入 LLM 视野。
    text = _structure_window(full_text)
    try:
        await _progress(70, "AI 解析标书结构（响应文件格式要求）…")
        reply = await _chat_with_retry(_STRUCTURE_SYSTEM, f"招标材料：\n{text}")
        parsed = _parse_structure_reply(reply)
        if not parsed.get("business") or not parsed.get("technical"):
            # 结构抽取为空重试一次（更严格的纯 JSON 指令，与要求抽取同策略）
            reply = await _chat_with_retry(
                _STRUCTURE_SYSTEM + "输出 JSON 本身（不附加解释文字或 Markdown 围栏）。",
                f"招标材料：\n{text[:30000]}",
            )
            parsed = _parse_structure_reply(reply)
    except Exception as exc:  # noqa: BLE001
        return {"count": 0, "error": f"结构解析未完成（云模型暂不可用：{type(exc).__name__}），成果将按通用结构生成，请人工核对"}
    count = 0
    order = 0
    for role in ("price", "business", "technical"):
        for title, guide in parsed.get(role, []):
            await requirement_service.upsert_requirement(
                session,
                enterprise_id=enterprise_id,
                project_id=project_id,
                req_type="doc_structure",
                content=title,
                structured={"role": role, "guide": guide, "order": order},
                coordinates=[],
                confidence=None,
            )
            order += 1
            count += 1
    format_spec = parsed.get("format") or None
    if format_spec:
        # 格式要求落库（doc_structure / role=format）：导出时优先按招标要求排版
        await requirement_service.upsert_requirement(
            session,
            enterprise_id=enterprise_id,
            project_id=project_id,
            req_type="doc_structure",
            content=f"响应文件格式要求：{format_spec.get('font') or ''} {format_spec.get('size') or ''}",
            structured={"role": "format", "spec": format_spec},
            coordinates=[],
            confidence=None,
        )
    return {"count": count, "error": None, "format": format_spec}


def _structure_window(full_text: str) -> str:
    """从大文本中定位"响应/应答文件格式与模板要求"相关窗口，供结构抽取使用。"""
    lines = full_text.splitlines()
    kws = (
        "响应文件", "应答文件", "投标文件格式", "文件格式", "响应文件组成", "组成",
        "商务文件", "技术文件", "价格文件", "格式要求", "编制要求", "递交要求", "目录要求",
    )
    hits = [i for i, ln in enumerate(lines) if any(k in ln for k in kws)]
    if hits:
        start = max(0, min(hits) - 20)
        end = min(len(lines), max(hits) + 500)
        window = "\n".join(lines[start:end])
        return window[:40000]
    return full_text[:40000]


def _score_rules_window(full_text: str) -> str:
    """从大文本中定位"评分标准/评审要素/评分权重"表格窗口，供评分细则专项抽取。"""
    lines = full_text.splitlines()
    kws = (
        "评分标准", "评审要素", "评审内容", "评分权重", "商务详评细则", "技术详评",
        "分值权重", "报价评分", "诚信评价", "综合评价", "打分",
    )
    hits = [i for i, ln in enumerate(lines) if any(k in ln for k in kws)]
    if hits:
        start = max(0, min(hits) - 15)
        end = min(len(lines), max(hits) + 900)
        window = "\n".join(lines[start:end])
        return window[:50000]
    return ""


def _chapter_window(full_text: str, title: str, guide: str = "") -> str:
    """按章节标题/写作要求从招标文件原文中取相关段落窗口（Issue #8 复盘：
    正文必须对着原始文档写，抽取仅是骨架，不能丢失原文信息）。"""
    import re as _re

    lines = full_text.splitlines()
    tokens = [t for t in _re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{2,}", title or "")][:4]
    if not tokens:
        tokens = [t for t in _re.findall(r"[\u4e00-\u9fff]{2,}", guide or "")][:3]
    if not tokens:
        return ""
    hits = [i for i, ln in enumerate(lines) if any(k in ln for k in tokens)]
    if not hits:
        return ""
    start = max(0, min(hits) - 15)
    end = min(len(lines), max(hits) + 80)
    return "\n".join(lines[start:end])[:6000]


def _extract_template_blocks(full_text: str) -> dict[str, list[dict]]:
    """模板原文确定性提取（Issue #8 验收铁律：模板原文/章节/编号/顺序/表格/注释
    不能减少、改写或替换）——从'第五章响应文件格式'章节【逐字照搬】组成与模板，
    不经过 LLM 归纳，避免信息丢失。

    返回 {role: [{kind: heading|paragraph|table, text, cols?, order}]}。
    表格行在 doc_block 中以 "cell1 | cell2 | cell3" 形式存在，按 | 重建表格结构。
    """
    import re as _re

    lines = full_text.splitlines()
    role_starts: dict[str, int] = {}
    for i, ln in enumerate(lines):
        s = ln.strip()
        m = _re.match(r"^[一二三]?、?(价格|商务|技术)文件$", s)
        if m:
            role_starts.setdefault({"价格": "price", "商务": "business", "技术": "technical"}[m.group(1)], i)
    if not role_starts:
        return {}

    def _classify_table(rows: list[list[str]]) -> str | None:
        head = " ".join(rows[0]) if rows else ""
        if any(k in head for k in ("采购文件条目号", "层级", "建设规模", "职务", "职称", "拟委任", "工作年限")):
            return "technical"
        if any(k in head for k in ("检查内容", "开户行", "项目名称", "响应总价", "自查", "出资金额", "财务指标", "保证金")):
            return "business"
        return None

    out: dict[str, list[dict]] = {}
    seen_tables: set[str] = set()

    def _emit(role: str, blocks: list[dict]) -> None:
        for b in blocks:
            if b.get("kind") == "table":
                fp = "|".join("|".join(r) for r in b["rows"])
                if fp in seen_tables:
                    continue
                seen_tables.add(fp)
            out.setdefault(role, []).append(b)

    for role, start in sorted(role_starts.items(), key=lambda kv: kv[1]):
        nexts = [v for v in role_starts.values() if v > start]
        # 段落内附录/表格区标记：取该角色起始之后的第一个标记作为本段结束
        seg_end = len(lines)
        for i in range(start, len(lines)):
            if any(k in lines[i] for k in ("投标（应答）人须知前附表附件", "商务评分标准", "技术评分标准", "响应文件编制注意事项")):
                seg_end = i
                break
        nexts.append(seg_end)
        seg = lines[start:min(nexts)]
        blocks: list[dict] = []
        buf: list[str] = []
        table_rows: list[list[str]] = []

        def flush_table():
            nonlocal table_rows
            if len(table_rows) >= 2:
                blocks.append({"kind": "table", "rows": table_rows})
            elif table_rows:
                blocks.append({"kind": "paragraph", "text": " | ".join(table_rows[0])})
            table_rows = []

        def flush_buf():
            nonlocal buf
            if buf:
                blocks.append({"kind": "paragraph", "text": "\n".join(buf)})
                buf = []

        for ln in seg:
            s = ln.strip()
            if not s:
                flush_table()
                flush_buf()
                continue
            if _re.match(r"^（[一二三四五六七八九十]+）", s) or _re.match(r"^\d{1,2}\.", s):
                flush_table()
                flush_buf()
                blocks.append({"kind": "heading", "text": s})
                continue
            if " | " in s and len(s.split(" | ")) >= 2:
                flush_buf()
                cells = [c.strip() for c in s.split(" | ")]
                if cells and any(cells):
                    table_rows.append(cells)
                continue
            flush_table()
            buf.append(s)
        flush_table()
        flush_buf()
        _emit(role, blocks)

    # 后置"格式文件/表格模板区"（Issue #8：技术偏差表等表格模板位于组成说明之后，
    # 如"序号 | 采购文件条目号 | 采购文件条款 | 响应文件条款 | 偏差说明"在 1329 行附近）：
    # 全局扫描残留表格，按表头关键词归入商务/技术模板，避免表格拍平成文字丢失。
    all_rows: list[list[str]] = []
    for ln in lines:
        s = ln.strip()
        if " | " in s and len(s.split(" | ")) >= 2:
            cells = [c.strip() for c in s.split(" | ")]
            if cells and any(cells):
                all_rows.append(cells)
            continue
        if all_rows:
            role = _classify_table(all_rows)
            if role and len(all_rows) >= 2:
                _emit(role, [{"kind": "table", "rows": all_rows}])
            all_rows = []
    if all_rows:
        role = _classify_table(all_rows)
        if role and len(all_rows) >= 2:
            _emit(role, [{"kind": "table", "rows": all_rows}])
    return out


_SCORE_RULES_SYSTEM = (
    "你是评标标准解析助手。从招标文件的评分标准表（商务评分标准/技术评分标准/报价评分标准/"
    "各项目评分权重）中【逐条】提取评分项，输出 JSON 数组："
    '[{"element": "评审要素名（照搬原文，如 绿色低碳评价）", '
    '"content": "评审内容原文（含分值区间，如 0-3分、优：9-10分）", '
    '"weight": 分值上限数字（区间取上限，如 3 或 10）}, ...]。'
    "表格每行对应一条：逐条提取，不把多行归纳合并；分值取区间上限；"
    "只写材料里有的内容；材料没有评分标准表时输出 []。"
)


async def _llm_extract_score_rules(
    session: AsyncSession,
    *,
    enterprise_id: int,
    project_id: int,
    file_ids: list[int],
    task: Task | None = None,
) -> int:
    """评分标准表专项抽取（Issue #8：评标必须按招标评分细则打分，分值权重来自原文表格）。
    落库为 req_type=score_rule（structured.score_rule.weight=分值上限），返回抽取条数。"""
    from sqlalchemy import select as sa_select

    from app.models.doc import DocBlock
    from app.services import requirement_service
    from app.services.llm import llm_enabled, try_extract_json

    if not llm_enabled():
        return 0
    file_ids = [int(i) for i in file_ids]
    if not file_ids:
        return 0
    blocks = (
        await session.scalars(sa_select(DocBlock).where(DocBlock.file_id.in_(file_ids)))
    ).all()
    full_text = _dedup_block_texts(blocks, limit=160000)
    window = _score_rules_window(full_text)
    if not window.strip():
        return 0
    try:
        reply = await _chat_with_retry(_SCORE_RULES_SYSTEM, f"评分标准章节文本：\n{window}")
    except Exception:  # noqa: BLE001 云端暂不可用：评分细则留空，任务结果已如实注明
        return 0
    parsed = try_extract_json(reply)
    if parsed is None:
        try:
            reply = await _chat_with_retry(
                _SCORE_RULES_SYSTEM,
                f"评分标准章节文本：\n{window}\n\n请把结果用 JSON 输出：只输出 JSON 本身，开头结尾不加解释文字或 Markdown 围栏。",
            )
        except Exception:  # noqa: BLE001
            return 0
        parsed = try_extract_json(reply)
    if parsed is None:
        return 0
    if isinstance(parsed, dict):
        items = parsed.get("rules") or parsed.get("items") or []
    elif isinstance(parsed, list):
        items = parsed
    else:
        items = []
    count = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        element = str(item.get("element") or "").strip()
        content = str(item.get("content") or "").strip()
        if not element or not content:
            continue
        try:
            weight = float(item.get("weight") or 0)
        except (TypeError, ValueError):
            weight = 0.0
        if weight <= 0:
            continue
        await requirement_service.upsert_requirement(
            session,
            enterprise_id=enterprise_id,
            project_id=project_id,
            req_type="score_rule",
            content=f"{element}：{content[:160]}",
            structured={"score_rule": {"weight": weight, "criterion": content[:200], "element": element[:80]}},
            coordinates=[],
            confidence=None,
        )
        count += 1
    return count


async def _tender_parse_handler(session: AsyncSession, task: Task) -> None:
    """解析项目材料：doc_block + （门禁内）LLM 语义抽取 requirement + Hermes 双通道复核。"""
    from app.services.file_service import reparse_file
    from app.services.llm import llm_enabled

    file_ids = task.payload.get("file_ids") or []
    if not file_ids:
        raise ValueError("payload.file_ids 为空")
    task.progress = {"phase": task.task_type, "status": "running", "percent": 30, "current_work": f"解析 {len(file_ids)} 个文件"}
    # 进度立即落库：对外可见（SSE/API），并释放 task 行锁，避免阻塞心跳续期
    await session.commit()
    # 关键（生产定位 Issue #8）：RLS 上下文是事务级的，上面 commit 后即被清空；
    # 必须重建，否则后续 requirement_revision 等业务表 INSERT 会违反 RLS 策略
    await _set_rls_context(session, task.enterprise_id)
    parsed_ids: list[int] = []
    parse_failures: list[dict] = []
    for file_id in file_ids:
        fobj = await reparse_file(session, int(file_id))
        if fobj.status != 3:
            # Issue #8 任务 224：单文件失败曾拖垮整个解析任务（其他文件白解析）。
            # 改为逐文件语义：失败文件逐条记录原因，其余文件照常解析与抽取；
            # 仅当全部文件失败时任务才失败。
            ps = fobj.parse_status or {}
            detail = str(ps.get("message") or ps.get("error_code") or "").strip()
            parse_failures.append(
                {"file_id": int(file_id), "file_name": fobj.original_name, "reason": detail or "未知原因"}
            )
        else:
            parsed_ids.append(int(file_id))
    if not parsed_ids and parse_failures:
        msg = "；".join(
            f"{f['file_name']}（原因：{f['reason']}）" for f in parse_failures[:5]
        )
        raise ValueError(f"全部 {len(parse_failures)} 个文件解析失败：{msg}")
    task.progress = {
        "phase": task.task_type,
        "status": "running",
        "percent": 35,
        "current_work": f"{len(parsed_ids)}/{len(file_ids)} 个文件解析成功"
        + (f"，{len(parse_failures)} 个失败（原因已记录）" if parse_failures else ""),
    }
    await session.commit()
    await _set_rls_context(session, task.enterprise_id)

    extracted, req_extract_warning = await _llm_extract_requirements(
        session,
        enterprise_id=task.enterprise_id,
        project_id=task.project_id,
        file_ids=parsed_ids,
        task_id=task.id,
        task=task,
    )
    # 提交代码抽取的待落库要求（Hermes 复核通过 MCP 独立连接读取，必须可见）
    await session.commit()
    await _set_rls_context(session, task.enterprise_id)
    # 模板原文确定性提取（Issue #8 验收铁律：模板原文/编号/顺序/表格不能改写或替换，
    # 生成只能在模板上填写与增加——故模板必须逐字落库，供生成骨架照搬）。
    # 置于 Hermes 复核之前：复核阶段对照原文检查模板清单完整性并补缺。
    from sqlalchemy import select as _sa_select

    from app.models.doc import DocBlock as _DB
    from app.services import requirement_service as _req_svc

    template_blocks = await session.scalars(
        _sa_select(_DB).where(_DB.file_id.in_(parsed_ids))
    )
    full_text = _dedup_block_texts(list(template_blocks), limit=160000)
    templates = _extract_template_blocks(full_text)
    template_count = 0
    for role in ("business", "technical", "price"):
        for order, blk in enumerate(templates.get(role, [])):
            text = blk.get("text") or (
                "\n".join(" | ".join(r) for r in blk.get("rows") or [])
            )
            if not text.strip():
                continue
            await _req_svc.upsert_requirement(
                session,
                enterprise_id=task.enterprise_id,
                project_id=task.project_id,
                req_type="doc_template",
                content=text[:2000],
                structured={
                    "role": role,
                    "order": order,
                    "kind": blk.get("kind") or "paragraph",
                    "rows": blk.get("rows") or [],
                },
                coordinates=[],
                confidence=None,
            )
            template_count += 1
    await session.commit()
    await _set_rls_context(session, task.enterprise_id)
    # —— Hermes 双通道复核（用户要求）：代码+M3 快路径抽取，Hermes 经 tender-parse skill
    # 对照材料文本补抽遗漏要求 + 复核模板清单完整性（缺失模板块补落库）；失败不阻塞，但显式写入任务结果 ——
    hermes_review: dict = {"ok": False, "error": "未执行"}
    if llm_enabled() and parsed_ids:
        task.progress = {
            "phase": task.task_type, "status": "running", "percent": 66,
            "current_work": "Hermes 复核招标要求与模板清单（双通道，对照原文补抽补缺）…",
        }
        await session.commit()
        await _set_rls_context(session, task.enterprise_id)
        try:
            hermes_review = (await _run_hermes_parse_review(task)) or {"ok": False, "error": "hermes 不可用"}
        except Exception as exc:  # noqa: BLE001
            hermes_review = {"ok": False, "error": str(exc)[:200]}
        # 复核后重新统计要求数（Hermes 经独立连接写入，重查数据库）
        from app.models.requirement import Requirement as _Req

        extracted = len(
            (
                await session.scalars(
                    _sa_select(_Req).where(
                        _Req.enterprise_id == task.enterprise_id,
                        _Req.project_id == task.project_id,
                        _Req.current.is_(True),
                        _Req.req_type != "doc_structure",
                    )
                )
            ).all()
        )
        # 复核后重统模板数（Hermes 可能补落 doc_template 块）
        template_count = len(
            (
                await session.scalars(
                    _sa_select(_Req).where(
                        _Req.enterprise_id == task.enterprise_id,
                        _Req.project_id == task.project_id,
                        _Req.current.is_(True),
                        _Req.req_type == "doc_template",
                    )
                )
            ).all()
        )
    # 流程化确认标书结构（用户反馈）：解析阶段即从招标文件提取响应文件格式要求，
    # 落库为 req_type=doc_structure 的要求，生成任务直接消费（不用通用模板拍脑袋）。
    structure_info = await _llm_extract_structure(
        session,
        enterprise_id=task.enterprise_id,
        project_id=task.project_id,
        file_ids=parsed_ids,
        task=task,
    )
    structure_extracted = structure_info.get("count") or 0
    structure_error = structure_info.get("error")
    # 评分标准表专项抽取（Issue #8：评标按招标评分细则打分，权重来自原文表格）
    score_rules_extracted = await _llm_extract_score_rules(
        session,
        enterprise_id=task.enterprise_id,
        project_id=task.project_id,
        file_ids=parsed_ids,
        task=task,
    )
    task.progress = {
        "phase": task.task_type,
        "status": "running",
        "percent": 92,
        "current_work": f"结构 {structure_extracted} 章 / 模板 {template_count} 块 / 评分细则 {score_rules_extracted} 条，汇总…",
    }
    await session.commit()
    await _set_rls_context(session, task.enterprise_id)
    result: dict = {
        "parsed_file_ids": parsed_ids,
        "parse_failures": parse_failures,
        "requirements_extracted": extracted,
        "structure_extracted": structure_extracted,
        "template_blocks_extracted": template_count,
        "score_rules_extracted": score_rules_extracted,
        "hermes_review": {
            "ok": bool(hermes_review.get("ok")),
            "detail": (hermes_review.get("error") or hermes_review.get("log_tail") or "")[-300:],
        },
    }
    from app.services.llm import llm_enabled

    # 积极报错原则（Issue #8 复盘）：每一处降级/异常都必须显式写进任务结果，
    # 前端逐条警示——绝不静默继续。
    notes: list[str] = []
    if parse_failures:
        notes.append(
            f"{len(parse_failures)} 个文件解析失败（已跳过）："
            + "；".join(f"{f['file_name']}（{f['reason'][:60]}）" for f in parse_failures[:5])
        )
    if not hermes_review.get("ok") and llm_enabled() and parsed_ids:
        notes.append(f"Hermes 复核未完成（{hermes_review.get('error', '')[:80]}），要求以代码抽取结果为准")
    if structure_error:
        notes.append(f"未确认招标文件响应格式：{structure_error}（成果可能未按模板结构生成，请人工核对）")
    elif structure_extracted == 0 and llm_enabled():
        notes.append("招标文件未明确响应文件格式要求，成果将按通用投标结构生成")
    if score_rules_extracted == 0 and llm_enabled():
        notes.append("未从材料抽取出评分标准表：评标将以内置完整性规则计分（不代表招标评标得分）")
    if req_extract_warning:
        notes.append(req_extract_warning)
    elif extracted == 0 and llm_enabled():
        notes.append("未从材料抽取出任何招标要求：请检查文件内容（扫描件/加密件需人工处理），或到要求页手动录入")
    if not llm_enabled():
        notes.append("云模型门禁关闭，未做语义抽取（仅完成文本解析）")
    if notes:
        result["note"] = "；".join(notes)
    task.result = result


async def _run_hermes_agent(task: Task) -> dict | None:
    """通过部署的 Hermes Agent（hermes chat 无头模式 + bidvolt-bid-generate skill）生成成果。

    任务级授权：签发 capability token（企业/项目/任务/工具白名单），以
    BIDVOLT_CAPABILITY_TOKEN 注入 Hermes 进程环境，MCP 每次工具调用携带该 token，
    后端逐调用校验（capability.verify_capability）。
    不可用/失败/超时返回 None 或 ok=False（调用方回退内嵌闭环，不影响任务完成）。
    """
    import os
    import shutil

    hermes_bin = shutil.which("hermes") or "/data/hermes/venv/bin/hermes"
    if not os.path.exists(hermes_bin):
        return None
    try:
        from app.services.capability import issue_capability

        cap = issue_capability(
            enterprise_id=task.enterprise_id,
            project_id=task.project_id,
            task_id=task.id,
            task_type=task.task_type,
        )
    except Exception:  # noqa: BLE001
        return None
    prompt = (
        f"你是投标文件撰写 Agent。请使用 bidvolt-bid-generate skill 为项目 {task.project_id} 生成三份投标成果。"
        "本会话是一次非交互批处理任务：直接执行完整流程，每一步都真实调用 MCP 工具"
        "（工具名带 bidvolt: 前缀，如 bidvolt:list_requirements、bidvolt:search_assets、"
        "bidvolt:save_deliverable、bidvolt:calculate_quote），等待工具真实返回后再继续；"
        "输出计划、A/B/C/D 方案或虚构工具结果不算完成，也无需向用户确认——"
        "直接生成并保存，完成后用一句话总结。"
        "流程：1) bidvolt:list_requirements 建立要求基线——其中 req_type=doc_structure 是招标文件"
        "'响应文件格式'章节的文件组成清单，成果章节逐项照搬该清单（标题与顺序与清单一致）；"
        "req_type=score_rule 是评分细则，正文逐条响应；"
        "2) bidvolt:get_project_material_blocks 阅读招标文件【原文】，每章正文以原文相关段落为依据撰写"
        "（抽取信息是骨架，正文依据原文，不脱离原文编造条款）；"
        "3) bidvolt:search_assets 取企业事实；"
        "4) 若项目尚无成果记录，先调用 bidvolt:create_deliverable 创建商务标/技术标/报价单三份记录"
        "（project_id 填 " + str(task.project_id) + "，deliverable_type 分别 1/2/3）；"
        "5) 逐份撰写并调用 bidvolt:save_deliverable 保存新版本"
        "（注意：expected_version_no 必须等于该成果【当前版本号】——新创建的记录当前版本号为 0，"
        "首次保存传 0；后续保存前先用 bidvolt:get_deliverable_content 读回当前版本号再传）；"
        "企业事实以 search_assets 返回为准；无资料处标注【待补充】；报价只建议不落库。任务 id=" + str(task.id) + "。"
    )
    env = dict(os.environ)
    env["BIDVOLT_CAPABILITY_TOKEN"] = cap
    env["HERMES_ACCEPT_HOOKS"] = "1"
    # Hermes 配置/密钥/skills 均位于 HERMES_HOME（安装脚本约定 /data/hermes）；
    # 缺省 HOME 下无配置会触发交互式 setup，必须显式指定。
    hermes_home = os.environ.get("HERMES_HOME") or "/data/hermes"
    env["HERMES_HOME"] = hermes_home
    # capability 兜底通道：hermes 不保证把父进程 env 透传给 MCP 子进程，
    # 写入固定临时文件（MCP 端 tools.py 会读取；单 worker 串行，安全）
    try:
        cap_file = os.environ.get("BIDVOLT_CAP_FILE", "/tmp/bidvolt_cap_token")
        with open(cap_file, "w", encoding="utf-8") as _f:
            _f.write(cap)
        os.chmod(cap_file, 0o600)
    except OSError:
        pass
    try:
        proc = await asyncio.create_subprocess_exec(
            hermes_bin, "chat", "-q", prompt,
            "-t", "bidvolt", "-s", "bidvolt-bid-generate",
            "-Q", "--cli", "--max-turns", "60", "--no-restore-cwd",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=env, cwd=hermes_home,
        )
    except OSError:
        return None
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=900)
    except TimeoutError:
        proc.kill()
        await proc.communicate()
        return {"ok": False, "error": "hermes 执行超时（900s）"}
    log_tail = (out.decode("utf-8", "replace") + "\n" + err.decode("utf-8", "replace"))[-1500:]
    return {"ok": proc.returncode == 0, "log": log_tail}


async def _run_hermes_parse_review(task: Task) -> dict | None:
    """Hermes 双通道复核要求（用户要求"用 hermes 提一遍要求"）：代码+M3 抽取为快路径，
    Hermes 经 bidvolt-tender-parse skill 对照材料文本复核补抽（upsert_requirements 幂等）。
    不可用/失败返回 {"ok": False, "error": ...}（不阻塞解析任务，失败显式写入任务结果）。"""
    import os
    import shutil

    hermes_bin = shutil.which("hermes") or "/data/hermes/venv/bin/hermes"
    if not os.path.exists(hermes_bin):
        return {"ok": False, "error": "hermes 未安装"}
    try:
        from app.services.capability import issue_capability

        cap = issue_capability(
            enterprise_id=task.enterprise_id,
            project_id=task.project_id,
            task_id=task.id,
            task_type=task.task_type,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"capability 签发失败：{exc}"}
    prompt = (
        f"你是招标文件解析复核 Agent。项目 {task.project_id} 已完成一轮要求与模板抽取，请复核并补缺。"
        "本会话是一次非交互批处理任务：直接执行完整流程，每一步调用 MCP 工具"
        "（工具名带 bidvolt: 前缀），等待工具真实返回后再继续；输出计划或虚构的工具结果"
        "不算完成，过程中也无需向用户确认。"
        "流程：1) bidvolt:list_requirements 查看已有要求与 doc_template 模板清单；"
        "2) bidvolt:get_project_material_blocks 读取招标文件原文；"
        "3) 对照材料补抽遗漏的资格要求/评分细则/否决条款/技术要求/报价规则/材料清单，"
        "经 bidvolt:upsert_requirements 落库（坐标必填、先查重不重复写入、不编造）；"
        "4) 【核对模板清单条目——只登记，不写内容】第五章'响应文件格式'（从'一、价格文件'起，"
        "到'响应文件编制注意事项'或'商务评分标准'首次出现处止）逐条列出了响应文件要提交的"
        "文件条目及其表格。请把【该区间内】出现的每个文件条目名称（如'法定代表人授权委托书'、"
        "'技术偏差表'）与表格，和已有 doc_template 清单逐一比对："
        "清单里缺少哪个条目，就用 bidvolt:upsert_requirements 以 req_type='doc_template' "
        "登记【这一条条目本身】——content 只是该条目的名称或该表格本身，与该区间内原文逐字一致；"
        "structured 含 role、order、kind（heading/paragraph/table），表格带 rows。"
        "登记范围只包含该区间内的条目本身：评分标准表（表头含'评审要素''评审内容''评分权重'）、"
        "否决条款表、供应商须知/合同条款中的要求语句不在条目范围；登记时只写条目名称或表格原文，"
        "正文留待生成阶段撰写；同一条目只登记一次。"
        "5) 完成后用一句话总结（补抽要求 N 条、登记缺失条目 M 条）。任务 id=" + str(task.id) + "。"
    )
    env = dict(os.environ)
    env["BIDVOLT_CAPABILITY_TOKEN"] = cap
    env["HERMES_ACCEPT_HOOKS"] = "1"
    hermes_home = os.environ.get("HERMES_HOME") or "/data/hermes"
    env["HERMES_HOME"] = hermes_home
    try:
        cap_file = os.environ.get("BIDVOLT_CAP_FILE", "/tmp/bidvolt_cap_token")
        with open(cap_file, "w", encoding="utf-8") as _f:
            _f.write(cap)
        os.chmod(cap_file, 0o600)
    except OSError:
        pass
    try:
        proc = await asyncio.create_subprocess_exec(
            hermes_bin, "chat", "-q", prompt,
            "-t", "bidvolt", "-s", "bidvolt-tender-parse",
            "-Q", "--cli", "--max-turns", "40", "--no-restore-cwd",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=env, cwd=hermes_home,
        )
    except OSError:
        return {"ok": False, "error": "hermes 启动失败"}
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=600)
    except TimeoutError:
        proc.kill()
        await proc.communicate()
        return {"ok": False, "error": "hermes 复核超时（600s）"}
    log_tail = (out.decode("utf-8", "replace") + "\n" + err.decode("utf-8", "replace"))[-1500:]
    return {"ok": proc.returncode == 0, "log_tail": log_tail}


async def _bid_generate_handler(session: AsyncSession, task: Task) -> None:
    """生成/补全三份成果：确定性草稿（LLM 门禁内可选增强）。"""
    from sqlalchemy import select as sa_select

    from app.models.deliverable import Deliverable
    from app.models.enterprise_domain import EnterpriseFact
    from app.models.project import Project
    from app.models.requirement import Requirement
    from app.services import deliverable_service, quote_engine
    from app.services.history_provider import get_samples_with_fallback
    from app.services.llm import LLMClient, llm_enabled

    project_id = task.project_id
    requirements = (
        await session.scalars(
            sa_select(Requirement).where(
                Requirement.enterprise_id == task.enterprise_id,
                Requirement.project_id == project_id,
                Requirement.current.is_(True),
                Requirement.req_type != "doc_structure",  # 标书结构是生成编排依据，不是招标要求本身
            )
        )
    ).all()
    # 解析阶段确认的标书结构（doc_structure 要求，structured.role ∈ price/business/technical）
    structure_rows = (
        await session.scalars(
            sa_select(Requirement).where(
                Requirement.enterprise_id == task.enterprise_id,
                Requirement.project_id == project_id,
                Requirement.current.is_(True),
                Requirement.req_type == "doc_structure",
            )
        )
    ).all()
    structure_rows = sorted(
        structure_rows, key=lambda r: (r.structured or {}).get("order", 0)
    )
    req_by_type: dict[str, list[Requirement]] = {}
    for req in requirements:
        req_by_type.setdefault(req.req_type, []).append(req)

    # 关键输入自检（Issue #8/#13 验收红线）：解析未成功（要求为 0）时，绝不允许
    # 静默兜底继续生成——必须让任务以明确原因失败，把用户引导回【招标解析】。
    # （原实现曾在生成内先行自动解析抽取；产品验收要求"有错误就爆出来，不要继续"，
    # 自动兜底会让"没解析成功也能生成"的错觉延续，故改为硬失败。）
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
    if not requirements:
        # Issue #12 问题三：要求为 0 仍生成并标记完成 → 用户拿到与本次招标无关的内容。
        # 空要求直接失败（带明确指引），不再产出"完成但不可用"的成果。
        if material_file_ids:
            raise ValueError(
                "生成已拦截：项目材料尚未解析出任何招标要求——请先到资料页触发【招标解析】，"
                "抽取成功后再发起成果生成（或到要求页手动录入要求）"
            )
        raise ValueError(
            "生成已拦截：项目没有任何招标要求且未上传材料——请先上传并解析招标文件，"
            "或手动录入要求后再发起成果生成"
        )
    pre_extracted = None

    async def _gen_progress(percent: int, work: str) -> None:
        """生成全程进度可视化（Issue #8 反馈"一直卡在 10%"）：每阶段落库供 SSE/轮询展示。"""
        task.progress = {"phase": "bid_generate", "status": "running", "percent": percent, "current_work": work}
        await session.commit()
        await _set_rls_context(session, task.enterprise_id)

    await _gen_progress(
        20,
        f"输入检查完成（要求 {len(requirements)} 条 / 材料 {len(material_file_ids)} 个），开始生成",
    )

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
    buyer_name = (project.buyer or "") if project is not None else ""

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
    material_text = "\n".join(b.text_content or "" for b in material_blocks)[:160000] or "（项目未上传招标材料）"

    # 招标文件自述的项目名称/招标人优先（Issue #8：底稿填空与正文引用以招标文件为准，
    # 系统项目名只是工作台标签，不能写进应答函/委托书等正式字段）
    tender_meta = await _derive_tender_meta(material_text)
    if tender_meta.get("project_name"):
        project_name = tender_meta["project_name"]
    if tender_meta.get("buyer"):
        buyer_name = tender_meta["buyer"]


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
        buyer = buyer_name
        nodes = [
            {"id": "n0", "type": "heading", "text": "商务标"},
            {"id": "n1", "type": "heading", "text": "应答函"},
            {"id": "n2", "type": "paragraph", "text": f"致：{buyer or '【招标人名称】'}"},
            {"id": "n3", "type": "paragraph",
             "text": f"我方【供应商名称】已仔细研究《{project_name}》采购文件（含全部澄清与修改），"
                     "自愿参加本项目应答，并作如下承诺与声明："},
            {"id": "n4", "type": "paragraph",
             "text": "1. 应答报价：详见价格文件（报价单）。\n"
                     "2. 本应答函有效期自递交截止日起 90 日历天【以采购文件为准】。\n"
                     "3. 我方完全响应采购文件的资格要求、技术规范书与合同条款，无负偏离（详见商务偏离表）。\n"
                     "4. 我方保证应答内容真实、完整、合法，并承担相应法律责任。"},
            {"id": "n5", "type": "paragraph", "text": f"项目名称：{project_name}"},
            {"id": "n6", "type": "heading", "text": "一、企业基本情况"},
            {"id": "n7", "type": "paragraph", "text": fact_text},
        ]
        idx = 8
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
        material_ref = str(task.payload.get("material_ref") or "").strip()
        cost_raw = task.payload.get("cost")
        # Issue #12：payload 未提供真实物料/成本时不得编造演示物料（此前硬编码 CABLE-YJV-3x95/100，
        # 导致报价单与本次招标无关）。改为"待报价测算"占位，引导用户到报价页基于真实成本测算并应用。
        if not material_ref or cost_raw is None:
            rows = [["材料", "数量", "单价", "小计"]]
            rows.append(["待报价测算", "", "", "请到【报价】页录入真实材料与成本，测算后【应用到报价单】"])
            return {"type": "sheet", "sheets": [{"name": "报价单", "rows": rows}]}
        cost = float(cost_raw)
        params = quote_engine.QuoteParams(
            material_ref=material_ref,
            cost=cost,
            min_profit_rate=float(task.payload.get("min_profit_rate", 0.05)),
        )
        try:
            samples, sample_source = await get_samples_with_fallback(material_ref)
        except Exception:  # noqa: BLE001
            samples, sample_source = [], "none"
        try:
            result = quote_engine.calculate(params, samples)
            price = result["suggested"]
        except ValueError:
            price = None
        rows = [["材料", "数量", "单价", "小计"]]
        rows.append([material_ref, 1, price if price is not None else "待人工定价", price if price is not None else ""])
        rows.append(["说明", "", "", f"确定性测算草稿，用户确认后生效（样本来源：{sample_source}）"])
        return {"type": "sheet", "sheets": [{"name": "报价单", "rows": rows}]}

    # —— 标书结构（流程化：解析阶段已确认并落库 doc_structure；与 LLM 门禁无关）——
    DEFAULT_BUSINESS_CHAPTERS = [
        ("一、企业基本情况", "结合企业事实介绍公司概况、资质能力、经营状况，300-600 字，无企业事实处标【待补充】"),
        ("二、商务条款逐项响应", "对每条资格/商务要求逐条编号响应（要求原文→我方响应承诺→是否偏离），覆盖全部条款，500-1200 字"),
        ("三、商务偏离表与承诺", "无偏离声明或逐项偏离说明；对投标保证金、履约、付款方式、报价有效期等条款给出明确承诺，300-600 字"),
    ]
    DEFAULT_TECH_CHAPTERS = [
        ("一、技术方案总体说明", "项目理解、总体思路、重点难点分析与对策、技术路线，600-1000 字"),
        ("二、技术和服务要求逐项响应", "对每条技术要求逐条编号响应（要求原文→我方响应→是否偏离），覆盖全部条款，无依据处标【待补充】，800-1500 字"),
        ("三、分项实施方案", "按采购范围分项说明工作内容、实施步骤、技术措施、进度安排，600-1200 字"),
        ("四、质量保障措施", "质量管理体系、过程控制、检验与验收标准，400-800 字"),
        ("五、安全与应急预案", "安全措施、应急预案、保密与信息安全措施，300-600 字"),
        ("六、人员配置与组织机构", "项目组织架构、岗位职责、人员投入计划，300-600 字"),
        ("七、售后服务承诺", "服务内容、响应时限、备品备件、培训与技术支持，400-800 字"),
        ("八、进度与交付", "里程碑计划、交付物清单、验收标准与方式，300-600 字"),
    ]

    def _chapters_from_rows(rows: list, role: str) -> list[tuple[str, str]]:
        return [
            (r.content, (r.structured or {}).get("guide") or "按招标文件该部分要求撰写，内容详实")
            for r in rows
            if (r.structured or {}).get("role") == role
        ]

    biz_chapters = _chapters_from_rows(structure_rows, "business")
    tech_chapters = _chapters_from_rows(structure_rows, "technical")
    if biz_chapters and tech_chapters:
        structure_source = "requirement"
        structure_summary = [t for t, _ in biz_chapters] + [t for t, _ in tech_chapters]
    else:
        structure_source = "fallback"
        biz_chapters = list(DEFAULT_BUSINESS_CHAPTERS)
        tech_chapters = list(DEFAULT_TECH_CHAPTERS)
        structure_summary = [t for t, _ in biz_chapters] + [t for t, _ in tech_chapters]
    agent_meta: dict = {
        "plan_sections": len(biz_chapters) + len(tech_chapters),
        "knowledge_refs": 0,
        "rounds": 0,
        "closed": None,
        "self_check": {"parse_ok": False, "missing": 0, "conflicts": 0},
        "refined": [],
    }
    chapter_expansions = {"n": 0}  # 分章字数不达标自动重写次数（闭环）

    # 企业名称（模板填空用：供应商名称字段）
    from app.models.auth import Enterprise as _Ent

    ent_row = await session.get(_Ent, task.enterprise_id)
    ent_name = (ent_row.name if ent_row else "") or ""

    # —— 模板照搬（Issue #8 验收铁律）：成果以招标文件模板原文为骨架，
    # 系统只在模板上填写已知信息与追加补充内容，不改写/替换/删减模板 ——
    template_rows = (
        await session.scalars(
            sa_select(Requirement).where(
                Requirement.enterprise_id == task.enterprise_id,
                Requirement.project_id == project_id,
                Requirement.current.is_(True),
                Requirement.req_type == "doc_template",
            )
        )
    ).all()
    # 模板污染防护 v2（根因：Hermes 补模板任务此前无权威边界，把须知要求文本与评分标准表
    # 贴进了 doc_template）。修法不是禁止 Hermes，而是：
    # 1) 结构性过滤——只剔除“评分标准/权重表”类模板块（表头特征明确，不误伤正常模板条目）；
    # 2) 同内容去重——Hermes 多轮补缺无稳定身份导致累积，按 content 归一去重。
    _SCORE_TBL_HEADERS = ("评审要素", "评审内容", "评分权重", "分包编号")

    def _is_score_table(r) -> bool:
        st = r.structured or {}
        if st.get("kind") != "table":
            return False
        rows = st.get("rows") or []
        head = " ".join(rows[0]) if rows else ""
        return any(k in head for k in _SCORE_TBL_HEADERS)

    template_rows = [r for r in template_rows if not _is_score_table(r)]
    seen_tpl: set[str] = set()
    uniq_rows: list = []
    for r in template_rows:
        key = (r.content or "").strip()
        if key and key not in seen_tpl:
            seen_tpl.add(key)
            uniq_rows.append(r)
    template_rows = uniq_rows
    tpl_by_role: dict[str, list] = {"business": [], "technical": [], "price": []}
    for r in template_rows:
        role = (r.structured or {}).get("role")
        if role in tpl_by_role:
            tpl_by_role[role].append(r)
    for role in tpl_by_role:
        def _order_key(r, _role=role):
            o = (r.structured or {}).get("order", 0)
            try:
                return (0, int(o))
            except (TypeError, ValueError):
                return (1, str(o))

        tpl_by_role[role].sort(key=_order_key)

    def _template_nodes(rows: list) -> list[dict]:
        nodes: list[dict] = []
        for r in rows:
            st = r.structured or {}
            if st.get("kind") == "table" and st.get("rows"):
                nodes.append({"id": f"tpl-{r.id}", "type": "table", "rows": st["rows"]})
            elif st.get("kind") == "heading":
                nodes.append({"id": f"tpl-{r.id}", "type": "heading", "text": r.content})
            else:
                nodes.append({"id": f"tpl-{r.id}", "type": "paragraph", "text": r.content})
        return nodes

    def _fill_known(text: str) -> str:
        """填空（Issue #8 验收铁律：在模板上填写，不删改模板）：
        已知字段回填（招标人/项目名称/供应商名称），未知空位原位标注【待补充：字段名】。"""
        import re as _re

        buyer = buyer_name
        out = text
        # 具名字段优先回填
        out = out.replace("（采购人）", buyer or "【待补充：招标人名称】")
        out = out.replace("（响应供应商名称）", ent_name or "【待补充：供应商名称】")
        out = out.replace("（项目名称）", project_name or "【待补充：项目名称】")
        if "项目名称：" in out:
            out = _re.sub(r"项目名称：\s*[,，、]?", f"项目名称：{project_name}，", out, count=1)
        # 下划线空位 → 原位待补充（保留字段位置，不删除）
        out = _re.sub(r"_{2,}", "【待补充】", out)
        # "法定地址：。" 等空字段尾部补标注
        out = _re.sub(r"(法定地址|注册地址)：\s*[,，。、]?", r"\1：【待补充】", out)
        return out

    def _fill_template_nodes(nodes: list[dict]) -> list[dict]:
        for n in nodes:
            if n.get("type") == "paragraph" and n.get("text"):
                n["text"] = _fill_known(n["text"])
            elif n.get("type") == "table" and n.get("rows"):
                n["rows"] = [[_fill_known(str(c)) for c in row] for row in n["rows"]]
        return nodes

    biz_tpl = _fill_template_nodes(_template_nodes(tpl_by_role.get("business", [])))
    tech_tpl = _fill_template_nodes(_template_nodes(tpl_by_role.get("technical", [])))
    models = {1: _business_model(), 2: _technical_model(), 3: await _quote_model()}
    # 底稿式导出元数据（Issue #8：复制采购文件原 docx 直接改，保留原版式）——
    # 记录模板源文件/裁切区间/填空值，导出时按此在底稿上操作。
    from app.models.doc import DocBlock as _DB2

    block_texts: dict[int, str] = {}
    for f in material_file_ids:
        bs = (await session.scalars(sa_select(_DB2.text_content).where(_DB2.file_id == f))).all()
        block_texts[f] = "\n".join(b or "" for b in bs)[:200000]
    tpl_source_fid = next(
        (
            int(f)
            for f in material_file_ids
            if any(
                k in (block_texts.get(int(f)) or "")
                for k in ("响应文件格式", "商务文件", "技术文件")
            )
        ),
        None,
    )
    tpl_meta = {
        "template_source_file_id": tpl_source_fid,
        "template_start": "响应文件格式",
        "template_end": ["商务评分标准", "技术评分标准", "响应文件编制注意事项"],
        "buyer": buyer_name,
        "project_name": project_name,
        "supplier_name": ent_name,
    }
    if biz_tpl:
        models[1]["nodes"] = [{"id": "tpl-biz-h", "type": "heading", "text": "商务文件"}, *biz_tpl]
        models[1]["template_based"] = True
        models[1].update(tpl_meta)
    if tech_tpl:
        models[2]["nodes"] = [{"id": "tpl-tech-h", "type": "heading", "text": "技术文件"}, *tech_tpl]
        models[2]["template_based"] = True
        models[2].update(tpl_meta)
    enhanced: list[str] = []
    # Hermes 作为默认生成路径（用户决策）：由部署的 Hermes Agent 经 bidvolt MCP
    # （携带任务级 capability token）执行 bid-generate skill 完成生成；
    # 质量门兜底：不可用/失败/产出不达标（三份成果无版本或正文过短）→ 回退内嵌闭环。
    from app.config import settings as _settings

    use_hermes = (task.payload.get("agent") or _settings.bid_generate_agent) == "hermes"
    hermes_ok = False
    if use_hermes and llm_enabled():
        await _gen_progress(30, "启动 Hermes Agent（经 bidvolt MCP 工具）生成成果…")
        hermes_result = await _run_hermes_agent(task)
        if hermes_result is None:
            log_tail = "hermes 不可用（未安装或无法启动）"
        else:
            log_tail = hermes_result.get("error") or hermes_result.get("log", "")
            hermes_ok = bool(hermes_result.get("ok"))
        agent_meta["runtime"] = "hermes"
        agent_meta["hermes"] = {"ok": hermes_ok, "log_tail": (log_tail or "")[-500:]}
        if hermes_ok:
            # 质量门：三份成果都必须有版本，且技术标/商务标正文达到最低篇幅，否则回退内嵌
            gate_ok = True
            try:
                existing_dls = (
                    await session.scalars(
                        sa_select(Deliverable).where(
                            Deliverable.project_id == project_id,
                            Deliverable.enterprise_id == task.enterprise_id,
                        )
                    )
                ).all()
                by_type = {d.deliverable_type: d for d in existing_dls}
                for dtype in (1, 2, 3):
                    d = by_type.get(dtype)
                    if d is None or d.current_version_no == 0:
                        gate_ok = False
                        break
                    if dtype in (1, 2):
                        _, m = await deliverable_service.get_version_content(
                            session, d.id, d.current_version_no
                        )
                        text = "\n".join(n.get("text", "") for n in (m or {}).get("nodes", []))
                        # 质量门与 E2E 质量标准对齐：技术标 >=2000 字、商务标 >=500 字
                        if len(text.strip()) < (2000 if dtype == 2 else 500):
                            gate_ok = False
                            break
            except Exception:  # noqa: BLE001
                gate_ok = False
            agent_meta["hermes"]["gate"] = "pass" if gate_ok else "fallback"
            if gate_ok:
                # Hermes 产出达标：跳过内嵌起草/保存，直接走统一门禁（评审+终检语义）
                agent_meta["plan_sections"] = len(biz_chapters) + len(tech_chapters)
                task.progress = {
                    "phase": "bid_generate", "status": "running", "percent": 80,
                    "current_work": "Hermes Agent 已生成成果，进入评审与质量门禁",
                }
            else:
                hermes_ok = False
    if use_hermes and not hermes_ok:
        agent_meta["runtime"] = "hermes-fallback"  # 实际产出由内嵌闭环完成
    if llm_enabled() and not hermes_ok:
        import re

        await _gen_progress(
            40,
            f"回退内嵌闭环：分章深度生成中（商务 {len(biz_chapters)} 章 / 技术 {len(tech_chapters)} 章，"
            "每章均可能多轮扩充，请耐心等待）",
        )
        client = LLMClient()

        def _clean_md(text: str) -> str:
            """清洗 Markdown 标记（Issue #8/#12）：正式成果节点应为纯文本。
            逐行去除行首 #（LLM 输出常把 ## 标题留在段落缓冲里），并全局去除 **/` 等标记。"""
            lines = [re.sub(r"^#{1,6}\s*", "", ln) for ln in text.splitlines()]
            cleaned = "\n".join(lines)
            cleaned = re.sub(r"[*_`]{1,3}", "", cleaned)
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

        def _dedup_headings(nodes: list[dict]) -> list[dict]:
            """去掉连续重复标题（LLM 输出首章常与预置标题重复，Issue #12 实测出现两次"技术标"）。"""
            out: list[dict] = []
            for n in nodes:
                if (
                    n.get("type") == "heading"
                    and out
                    and out[-1].get("type") == "heading"
                    and out[-1].get("text") == n.get("text")
                ):
                    continue
                out.append(n)
            return out

        # 章节化深度生成（用户反馈"标书太短"）：
        # 按真实服务类投标文件惯例（政府采购评分要素：逐项响应表/偏离表 + 实施方案 + 人员 + 售后 + 应急等），
        # 分章并行生成，每章给出明确字数与写作要求；要求逐条响应【全部】要求，不再只取前 10/20 条。

        tech_req_text = "\n".join(
            f"- {r.content}" for r in req_by_type.get("tech_requirement", [])
        ) or "（未解析到技术要求，按当前材料内容组织）"
        biz_reqs = "\n".join(
            f"- {r.content}"
            for key in req_by_type
            if key != "tech_requirement"
            for r in req_by_type[key]
        ) or "（未解析到资格/商务要求）"

        common_context = (
            f"项目名称：{project_name}\n招标人：{buyer_name or '未提供'}\n"
            f"企业产品/能力事实：\n{fact_text[:3000]}\n"
            f"当前招标材料摘录：\n{material_text[:12000]}\n"
            f"历史参考素材（仅作专业写法参考，项目事实以本项目材料为准）：\n{knowledge_text[:4000]}"
        )

        chapter_sem = asyncio.Semaphore(4)  # 控制并发，避免云模型限流/超时

        async def _gen_chapter(doc_name: str, title: str, guide: str, reqs_text: str) -> str:
            """生成一章正文；字数不达标自动重写（最多 2 轮扩充），失败回退空串。"""
            m = re.search(r"(\d+)\s*[-–]\s*\d+\s*字", guide or "")
            min_len = max(150, int(int(m.group(1)) * 0.6)) if m else 150
            system = (
                f"你在为《{doc_name}》撰写其中一节。"
                "写这一节时：对照下方给定的写作要求和招标文件原文相关段落，把该节应当回应的内容写出来；"
                "企业事实类信息（公司名/资质/业绩/人员等）如果上下文里有就用，上下文里没有的留空，写上【待补充】。"
                "正文用自然段落表达即可。"
            )
            user = (
                f"这一节：{title}\n这一节的写作要求：{guide}\n\n与本项目相关的要求：\n{reqs_text}\n\n"
                f"招标文件原文里和这一节相关的段落：\n"
                f"{_chapter_window(material_text, title, guide) or '（原文没有直接提到这一节，按写作要求写即可）'}\n\n"
                f"{common_context}"
            )
            reply = ""
            async with chapter_sem:
                for attempt in range(3):
                    try:
                        if attempt == 0:
                            reply = await client.chat(system, user)
                        else:
                            reply = await client.chat(
                                system + f"上一版正文仅 {len(reply.strip())} 字，未达要求；"
                                "请在不重复已有内容的前提下扩充到至少 " + str(min_len) + " 字。",
                                user,
                            )
                    except Exception:  # noqa: BLE001
                        return reply
                    if len(reply.strip()) >= min_len:
                        return reply
                    if attempt > 0:
                        chapter_expansions["n"] += 1
                return reply

        # 结构优先级：解析落库（requirement）→ 生成时再解析材料（tender）→ 通用章节（fallback，已在门禁外确定）
        if structure_source == "fallback":
            try:
                structure_reply = await client.chat(
                    _STRUCTURE_SYSTEM,
                    f"招标材料：\n{material_text[:20000]}",
                )
                parsed_structure = _parse_structure_reply(structure_reply)
                nb, nt = parsed_structure.get("business", []), parsed_structure.get("technical", [])
                if nb and nt:
                    biz_chapters, tech_chapters = nb, nt
                    structure_source = "tender"
                    structure_summary = [t for t, _ in biz_chapters] + [t for t, _ in tech_chapters]
            except Exception:  # noqa: BLE001  结构解析失败按通用章节回退
                pass

        async def _gen_doc(doc_name: str, chapters: list[tuple[str, str]], reqs_text: str) -> dict | None:
            replies = await asyncio.gather(
                *[_gen_chapter(doc_name, t, g, reqs_text) for t, g in chapters]
            )
            nodes: list[dict] = [{"id": f"llm-{doc_name}-0", "type": "heading", "text": doc_name}]
            idx = 1
            for (ct, _g), reply in zip(chapters, replies):
                if reply and reply.strip():
                    nodes.append({"id": f"llm-{doc_name}-h{idx}", "type": "heading", "text": ct})
                    idx += 1
                    nodes.extend(_split_nodes(reply.strip()))
            if len(nodes) <= 1:
                return None  # 全部章节失败：回退确定性草稿
            return {"nodes": _dedup_headings(nodes)}

        biz_doc, tech_doc = await asyncio.gather(
            _gen_doc("商务标", biz_chapters, biz_reqs),
            _gen_doc("技术标", tech_chapters, tech_req_text),
        )
        await _gen_progress(60, "分章生成完成，执行 Agent 自检补缺（对照全部要求）…")
        if biz_doc is not None:
            # 应答函格式页（路线图项）：正式商务标首部必须含应答函（字段未知处【待补充】，禁止编造）
            cover_nodes = [
                {"id": "llm-cover-h", "type": "heading", "text": "应答函"},
                {"id": "llm-cover-1", "type": "paragraph", "text": f"致：{buyer_name or '【招标人名称】'}"},
                {"id": "llm-cover-2", "type": "paragraph",
                 "text": f"我方【供应商名称】已仔细研究《{project_name}》采购文件（含全部澄清与修改），"
                         "自愿参加本项目应答，并承诺完全响应采购文件要求（详见商务偏离表），应答报价详见价格文件。"},
            ]
            if models[1].get("template_based"):
                # 模板照搬式（Issue #8 验收铁律）：LLM 只在模板之后【追加】补充响应内容，
                # 绝不改写/替换模板骨架
                models[1]["nodes"].append({"id": "llm-supp-h", "type": "heading", "text": "补充响应内容（在采购文件模板基础上增加）"})
                models[1]["nodes"].extend([*cover_nodes, *biz_doc["nodes"][1:]])
                models[1]["supplement_nodes"] = [*cover_nodes, *biz_doc["nodes"][1:]]
            else:
                models[1] = {"nodes": [biz_doc["nodes"][0], *cover_nodes, *biz_doc["nodes"][1:]]}
            enhanced.append("business")
        if tech_doc is not None:
            if models[2].get("template_based"):
                models[2]["nodes"].append({"id": "llm-supp-t", "type": "heading", "text": "补充响应内容（在采购文件模板基础上增加）"})
                models[2]["nodes"].extend(tech_doc["nodes"][1:] if len(tech_doc["nodes"]) > 1 else tech_doc["nodes"])
                models[2]["supplement_nodes"] = tech_doc["nodes"][1:] if len(tech_doc["nodes"]) > 1 else tech_doc["nodes"]
            else:
                models[2] = tech_doc
            enhanced.append("technical")

        # —— Agent 自检闭环（Hermes bid-generate skill 的 V1 内嵌实现）——
        # 语义（用户要求）：服务内部自检→补缺→再检，迭代直至闭环；
        # 达上限仍未闭环的，不得当作完成交付（deliverables_ready=False + 明确草稿标注）。
        agent_meta["plan_sections"] = len(biz_chapters) + len(tech_chapters)
        MAX_SELF_CHECK_ROUNDS = 3
        self_check_closed: bool | None = None
        try:
            from app.services.llm import extract_json as _extract_json

            for _round in range(1, MAX_SELF_CHECK_ROUNDS + 1):
                biz_text = "\n".join(n.get("text", "") for n in models[1].get("nodes", []))
                tech_text = "\n".join(n.get("text", "") for n in models[2].get("nodes", []))
                check_reply = await client.chat(
                    "你是标书自检助手。对照全部招标要求检查已生成标书正文（含补充响应章节），输出 JSON："
                    '{"missing": [{"req": "未被响应的要求原文", "target": "技术标或商务标"}], '
                    '"conflicts": [{"desc": "矛盾描述", "target": "技术标或商务标"}]}。'
                    "只输出 JSON；无问题输出 {\"missing\": [], \"conflicts\": []}。",
                    f"要求列表：\n{biz_reqs}\n{tech_req_text}\n\n商务标正文：\n{biz_text[:8000]}\n\n技术标正文：\n{tech_text[:12000]}",
                )
                try:
                    _parsed_check = _extract_json(check_reply) if check_reply.strip() else None
                except Exception:  # noqa: BLE001
                    _parsed_check = None
                if not isinstance(_parsed_check, dict):
                    agent_meta["self_check"] = {"parse_ok": False, "missing": 0, "conflicts": 0}
                    self_check_closed = None  # 无法自检：闭环状态未知，交由内置评审兜底
                    break
                missing = [m for m in (_parsed_check.get("missing") or []) if isinstance(m, dict) and m.get("req")]
                conflicts = [c for c in (_parsed_check.get("conflicts") or []) if isinstance(c, dict)]
                agent_meta["self_check"] = {"parse_ok": True, "missing": len(missing), "conflicts": len(conflicts)}
                agent_meta["rounds"] = _round
                if not missing and not conflicts:
                    self_check_closed = True
                    break
                self_check_closed = False  # 本轮未闭环：补缺后进入下一轮
                missing_by_doc: dict[str, list[str]] = {"商务标": [], "技术标": []}
                for m in missing:
                    target = str(m.get("target") or "")
                    doc_name = "技术标" if "技术" in target else "商务标"
                    missing_by_doc[doc_name].append(str(m["req"]))
                for doc_name, miss_reqs in missing_by_doc.items():
                    if not miss_reqs:
                        continue
                    try:
                        refine_reply = await client.chat(
                            f"你是投标文件撰写助手。已生成的《{doc_name}》缺少对以下要求的响应，"
                            "请撰写补充章节逐条覆盖这些要求；内容以给定要求为依据；直接输出 Markdown。",
                            "缺失要求：\n" + "\n".join(f"- {r}" for r in miss_reqs) + f"\n\n{common_context}",
                        )
                    except Exception:  # noqa: BLE001
                        refine_reply = ""
                    if refine_reply and refine_reply.strip():
                        dtype = 2 if doc_name == "技术标" else 1
                        models[dtype]["nodes"].append(
                            {"id": f"llm-{doc_name}-refine-{_round}", "type": "heading",
                             "text": f"补充响应（第{_round}轮自检补缺）"}
                        )
                        models[dtype]["nodes"].extend(_split_nodes(refine_reply.strip()))
                        if doc_name not in agent_meta["refined"]:
                            agent_meta["refined"].append(doc_name)
            # 迭代上限仍未闭环（self_check_closed=False 已由循环置位）→ 交付语义降级
        except Exception:  # noqa: BLE001  自检失败不阻塞生成
            pass
        agent_meta["closed"] = self_check_closed
        await _gen_progress(
            75,
            f"自检完成（closed={self_check_closed}，{agent_meta.get('rounds', 0)} 轮），进入评审与质量门禁…",
        )
    agent_meta["knowledge_refs"] = len(knowledge_refs)
    agent_meta["chapter_expansions"] = chapter_expansions["n"]

    # —— 输入完整性（用户反馈"待补充太多"）：统计三类输入缺口 + 交付件末尾附填写说明 ——
    from app.models.enterprise_domain import EnterpriseAsset

    asset_count = (
        await session.scalar(
            sa_select(func.count()).select_from(EnterpriseAsset).where(
                EnterpriseAsset.enterprise_id == task.enterprise_id,
                EnterpriseAsset.is_deleted.is_(False),
            )
        )
    ) or 0
    input_gaps = {
        "enterprise_assets": int(asset_count),
        "requirements": len(requirements),
        "materials": len(material_file_ids),
    }

    def _pending_notice_nodes(doc_name: str, text: str, gaps: dict) -> list[dict]:
        """交付件末尾的【填写说明】（非投标正文，提交前删除）：向用户解释待补充的由来与补齐路径。
        注意：说明文案本身不写"【待补充】"字样，避免终检待补充统计自增。"""
        pending = text.count("【待补充】") + text.count("待补充")
        lines = [
            "本说明为非投标正文，正式提交前请删除本段。",
            f"本《{doc_name}》中的待补充占位，是系统在“企业事实以资料为准”边界下的结果："
            "未取得真实来源的内容一律如实标注，不虚构资质、业绩、人员或数字。",
            "待补充占位的补齐路径：",
            f"1. 企业事实类（名称/资质/业绩/人员/报价）：当前企业资料库有 {gaps.get('enterprise_assets', 0)} 份资料，"
            "请在资料页导入营业执照/资质证书/业绩合同并做“企业资料导入分类”后重新生成，将自动回填；",
            "2. 招标信息类（技术条款/服务期限/验收标准）：请上传技术规范书及附件并重新触发招标解析；"
            "多标包项目请确认所投标包；",
            "3. 投标承诺类（人员投入/培训课时/保存年限等）：属于投标人自主承诺，请人工填写。",
            f"本稿共 {pending} 处待补充。",
        ]
        return [
            {"id": f"notice-{doc_name}-h", "type": "heading", "text": "填写说明（非投标正文，提交前删除）"},
            *[{"id": f"notice-{doc_name}-{i}", "type": "paragraph", "text": ln} for i, ln in enumerate(lines)],
        ]

    if not hermes_ok:
        for dtype in (1, 2):
            text_now = "\n".join(n.get("text", "") for n in models[dtype].get("nodes", []))
            models[dtype]["nodes"].extend(
                _pending_notice_nodes({1: "商务标", 2: "技术标"}[dtype], text_now, input_gaps)
            )
    versions: dict[int, int] = {}
    if not hermes_ok:  # Hermes 已通过 MCP 保存版本，内嵌保存路径跳过
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

    await _gen_progress(85, "执行内置评审与评分细则核对…")
    review_info = await review_service.run_evaluation(
        session, enterprise_id=task.enterprise_id, project_id=project_id
    )
    issues = await _review_issues(session, enterprise_id=task.enterprise_id, project_id=project_id)
    error_count = sum(1 for i in issues if i.get("severity") == "error")
    requirement_count = len(requirements)
    # 交付语义（用户要求）：Agent 自检未闭环（达到迭代上限仍有缺失/矛盾）时，
    # 绝不能当作完成交付——deliverables_ready 必须为 False 并显式标注草稿。
    self_check = agent_meta.get("self_check") or {}
    sc_missing = int(self_check.get("missing") or 0)
    sc_conflicts = int(self_check.get("conflicts") or 0)
    sc_closed = agent_meta.get("closed")
    deliverables_ready = (
        requirement_count > 0
        and error_count == 0
        and sc_closed is not False
    )
    quality = {
        "requirements_count": requirement_count,
        "pre_extracted": pre_extracted,
        "review_run_id": review_info["run_id"],
        "score_id": review_info["score_id"],
        "issue_count": len(issues),
        "error_count": error_count,
        "self_check": {
            "missing": sc_missing,
            "conflicts": sc_conflicts,
            "closed": sc_closed,
            "rounds": agent_meta.get("rounds", 0),
        },
        "deliverables_ready": deliverables_ready,
        # 输入完整性（用户反馈"待补充太多"）：三类输入缺口统计 + 各成果待补充计数
        "input_gaps": input_gaps,
        "pending": {
            "技术标": "\n".join(n.get("text", "") for n in models[2].get("nodes", [])).count("【待补充】"),
            "商务标": "\n".join(n.get("text", "") for n in models[1].get("nodes", [])).count("【待补充】"),
        },
    }
    not_closed_note = (
        f"自检未闭环：仍有 {sc_missing} 项要求未响应、{sc_conflicts} 项矛盾（已迭代 {agent_meta.get('rounds', 0)} 轮）"
        if sc_closed is False
        else ""
    )
    if llm_enabled():
        base_note = (
            ("正式成果草稿（待人工校核）" if not deliverables_ready else "确定性草稿 + LLM 全文生成")
            + (f"；{not_closed_note}" if not_closed_note else "")
        )
        if agent_meta.get("runtime") == "hermes":
            base_note = ("Hermes Agent 生成（bidvolt-bid-generate skill）"
                         + ("" if deliverables_ready else "（正式成果草稿，待人工校核）"))
        task.result = {
            **versions,
            "note": base_note
                + (f"（增强：{','.join(enhanced)}）" if enhanced else ""),
            "quality": quality,
            "issues": issues,
            "structure_source": structure_source,
            "structure": structure_summary,
            "agent": agent_meta,
            "knowledge_refs": [
                {"file_name": i["file_name"], "project_id": i["project_id"], "source_type": i["source_type"]}
                for i in knowledge_refs
            ],
        }
    else:
        task.result = {
            **versions,
            "note": "确定性草稿（云模型门禁关闭）",
            "quality": quality,
            "issues": issues,
            "structure_source": structure_source,
            "structure": structure_summary,
            "agent": agent_meta,
        }


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
                Requirement.req_type != "doc_structure",  # 标书结构不参与资料匹配
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
    # 积极报错（Issue #8 复盘）：无要求/全未匹配必须显式警示，禁止静默返回 0
    notes: list[str] = []
    if not requirements:
        notes.append("项目没有招标要求，无法做资料匹配——请先完成【招标解析】")
    elif not assets:
        notes.append("企业资料库为空，所有要求均未匹配到资料——请先上传营业执照/资质/业绩等企业资料")
    else:
        unmatched = sum(1 for r in results if r["matched"] != 1)
        if unmatched:
            notes.append(f"{unmatched}/{len(results)} 条要求未匹配到企业资料，评审将标记缺失并给出补充建议")
    if notes:
        task.result["note"] = "；".join(notes)


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
    requirements = (
        await session.scalars(
            sa_select(Requirement).where(
                Requirement.enterprise_id == enterprise_id,
                Requirement.project_id == project_id,
                Requirement.current.is_(True),
            )
        )
    ).all()
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
        # 模板照搬校验（Issue #8 验收铁律：模板章节/表格一个不能少——缺一项报一项）
        if dtype in (1, 2):
            role = "business" if dtype == 1 else "technical"
            tpl_rows = [
                r for r in requirements
                if r.req_type == "doc_template"
                and (r.structured or {}).get("role") == role
            ]
            tpl_rows.sort(key=lambda r: (r.structured or {}).get("order", 0))
            missing_tpl = []
            for r in tpl_rows:
                key = r.content.strip()[:24]
                if key and key not in text:
                    kind = (r.structured or {}).get("kind") or "paragraph"
                    missing_tpl.append(f"{key}（{kind}）")
            if missing_tpl:
                issues.append(
                    {
                        "severity": "error",
                        "message": f"{name}缺少采购文件模板条目 {len(missing_tpl)} 项："
                        + "；".join(missing_tpl[:6])
                        + ("…" if len(missing_tpl) > 6 else ""),
                        "locate": d.id,
                    }
                )
        # 【待补充】逐处列出缺失清单（Issue #8：每一处待补充必须在校核中指出）
        lines = [ln.strip() for ln in text.splitlines() if "【待补充】" in ln]
        for ln in lines[:12]:
            short = ln[:60]
            issues.append(
                {
                    "severity": "warning",
                    "message": f"{name}存在待补充：{short}（对应企业资料/证明材料尚未上传或未进入成果，正式提交前必须补齐）",
                    "locate": d.id,
                }
            )

    for req in requirements:
        target = "技术标" if req.req_type == "tech_requirement" else ("商务标" if req.req_type == "qualification" else None)
        if target is None:
            # 评分细则（路线图项：评分细则驱动评审）：评分项必须在任一成果中体现，否则列入缺失评分点
            if req.req_type == "score_rule":
                if (not texts.get(1) or req.content[:10] not in texts[1]) and (
                    not texts.get(2) or req.content[:10] not in texts[2]
                ):
                    issues.append(
                        {
                            "severity": "warning",
                            "message": f"评分细则未在成果中体现：{req.content[:40]}",
                            "locate": req.id,
                        }
                    )
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
