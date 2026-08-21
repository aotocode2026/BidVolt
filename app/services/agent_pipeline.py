"""新方案：Agent 主会话端到端（与旧管线完全隔离）。

一个任务 = 一个 Hermes 主会话；主 agent 自主用 todo 计划 + delegate_task 派
分析/提取/写作/校验/评审子 agent，按验收报告循环修复，满足要求后落库成果。

本模块职责（框架层）：
- 启动/等待主会话，把服务发送的消息与主会话的全部输出逐条落事件表
  （会话控制台数据源）；
- 支持客户在网页上直接与主会话对话（同一 session 追加消息）。
流程编排全部交给主 agent（skill：bidvolt-agent-pipeline）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re

from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import TaskType
from app.models.agent import AgentSessionEvent
from app.models.task import Task

logger = logging.getLogger(__name__)

# 主会话最大时长（秒）：端到端包含多轮子任务，给足预算
PIPELINE_TIMEOUT = 3600
# 事件落库节流：缓冲行数达到阈值或 5 秒到期时刷一次
EVENT_FLUSH_LINES = 40
EVENT_FLUSH_SECONDS = 5

_CHAT_LOCKS: dict[int, asyncio.Lock] = {}


def _hermes_bin() -> str | None:
    import shutil

    return shutil.which("hermes") or "/data/hermes/venv/bin/hermes"


def _hermes_env(cap: str) -> dict:
    env = dict(os.environ)
    env["BIDVOLT_CAPABILITY_TOKEN"] = cap
    env["HERMES_ACCEPT_HOOKS"] = "1"
    env["HERMES_HOME"] = os.environ.get("HERMES_HOME") or "/data/hermes"
    return env


def _write_cap_file(cap: str) -> None:
    try:
        cap_file = os.environ.get("BIDVOLT_CAP_FILE", "/tmp/bidvolt_cap_token")
        with open(cap_file, "w", encoding="utf-8") as _f:
            _f.write(cap)
        os.chmod(cap_file, 0o600)
    except OSError:
        pass


async def _next_seq(session: AsyncSession, task: Task) -> int:
    from sqlalchemy import func as sa_func
    from sqlalchemy import select as sa_select

    cur = await session.scalar(
        sa_select(sa_func.max(AgentSessionEvent.seq)).where(AgentSessionEvent.task_id == task.id)
    )
    return int(cur or 0)


async def _append_events(session: AsyncSession, task: Task, seq: list[int], batch: list[tuple[str, str]]) -> None:
    from app.services.task_service import _set_rls_context  # noqa: PLC0415

    for kind, content in batch:
        seq[0] += 1
        session.add(
            AgentSessionEvent(
                enterprise_id=task.enterprise_id,
                project_id=task.project_id,
                task_id=task.id,
                seq=seq[0],
                kind=kind,
                content=content,
            )
        )
    await session.commit()
    await _set_rls_context(session, task.enterprise_id)


async def run_agent_pipeline(session: AsyncSession, task: Task) -> None:
    """执行 Agent 主会话：启动 hermes chat 会话，逐行把服务消息/主会话输出落事件表，
    完成/失败后回写 session_id 与日志尾。"""
    hermes_bin = _hermes_bin()
    if hermes_bin is None or not os.path.exists(hermes_bin):
        raise ValueError("Hermes 未安装：Agent 主会话流程不可用")

    from app.services.capability import issue_capability

    cap = issue_capability(
        enterprise_id=task.enterprise_id,
        project_id=task.project_id,
        task_id=task.id,
        task_type=TaskType.AGENT_PIPELINE,
    )
    prompt = (
        f"请为项目 {task.project_id} 执行投标工作台端到端流程：解析→撰写→校验→评审→交付。"
        "流程与守则见预载 skill（bidvolt-agent-pipeline）；用 todo 列计划，"
        "用 delegate_task 派子任务，验收不通过带报告修复，全部满足后再输出。"
        f"任务 id={task.id}。"
    )
    env = _hermes_env(cap)
    _write_cap_file(cap)

    task.progress = {
        "phase": "agent_pipeline",
        "status": "running",
        "percent": 5,
        "current_work": "Agent 主会话启动（todo 计划 + 子任务编排，全程自主）…",
    }
    await session.commit()
    from app.services.task_service import _set_rls_context  # noqa: PLC0415

    await _set_rls_context(session, task.enterprise_id)

    seq = [await _next_seq(session, task)]
    await _append_events(session, task, seq, [("service", prompt)])

    try:
        proc = await asyncio.create_subprocess_exec(
            hermes_bin, "chat", "-q", prompt,
            "-t", "bidvolt", "-s", "bidvolt-agent-pipeline",
            "--cli", "--max-turns", "120", "--no-restore-cwd",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=env, cwd=env["HERMES_HOME"],
        )
    except OSError as exc:
        raise ValueError(f"Hermes 启动失败：{exc}") from exc

    buffer: list[tuple[str, str]] = []
    flushed_at = asyncio.get_event_loop().time()

    async def _pump(stream, kind: str) -> None:
        while True:
            line = await stream.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip()
            if text.strip():
                buffer.append((kind, text))

    async def _flush() -> None:
        nonlocal flushed_at
        if buffer:
            batch, buffer[:] = list(buffer), []
            await _append_events(session, task, seq, batch)
        flushed_at = asyncio.get_event_loop().time()

    pump_out = asyncio.create_task(_pump(proc.stdout, "hermes"))
    pump_err = asyncio.create_task(_pump(proc.stderr, "error"))

    try:
        while True:
            try:
                await asyncio.wait_for(proc.wait(), timeout=EVENT_FLUSH_SECONDS)
                break
            except TimeoutError:
                if len(buffer) >= EVENT_FLUSH_LINES or (asyncio.get_event_loop().time() - flushed_at) >= EVENT_FLUSH_SECONDS:
                    await _flush()
    except TimeoutError:
        proc.kill()
        await proc.communicate()
        await _flush()
        raise ValueError("Agent 主会话超时（3600s）：会话可经 session_id 恢复，请稍后重试或缩短范围")
    await asyncio.gather(pump_out, pump_err)
    await _flush()

    full_tail = await _event_tail(session, task, 3000)
    m = re.search(r"Session:\s*([\w-]+)", full_tail)
    session_id = m.group(1) if m else None
    if proc.returncode != 0:
        raise ValueError(f"Agent 主会话未完成（exit={proc.returncode}）：{full_tail[-500:]}")
    task.result = {
        "runtime": "hermes-main-session",
        "session_id": session_id,
        "log_tail": full_tail[-800:],
        "note": "Agent 主会话端到端完成：计划/子任务/验收报告见会话控制台（事件流），session_id 可恢复。",
    }
    task.progress = {
        "phase": "agent_pipeline",
        "status": "done",
        "percent": 100,
        "current_work": "Agent 主会话完成",
    }


async def _event_tail(session: AsyncSession, task: Task, limit: int) -> str:
    from sqlalchemy import select as sa_select

    rows = (
        await session.scalars(
            sa_select(AgentSessionEvent)
            .where(AgentSessionEvent.task_id == task.id)
            .order_by(AgentSessionEvent.seq.desc())
            .limit(200)
        )
    ).all()
    return "\n".join(r.content or "" for r in reversed(rows))[-limit:]


async def chat_with_session(session: AsyncSession, task: Task, message: str) -> dict:
    """客户在网页上直接与主会话对话：向同一 session 追加一条消息，
    返回主会话回复。同一任务串行（一个会话一次只能进行一轮对话）。"""
    from app.services.task_service import _set_rls_context  # noqa: PLC0415

    result = task.result or {}
    sid = result.get("session_id")
    if not sid:
        raise ValueError("主会话尚未建立（任务未完成或未产出会话）：请等待 Agent 主会话完成后再对话")
    hermes_bin = _hermes_bin()
    if hermes_bin is None or not os.path.exists(hermes_bin):
        raise ValueError("Hermes 未安装：无法与主会话对话")

    from app.services.capability import issue_capability

    cap = issue_capability(
        enterprise_id=task.enterprise_id,
        project_id=task.project_id,
        task_id=task.id,
        task_type=TaskType.AGENT_PIPELINE,
    )
    env = _hermes_env(cap)
    _write_cap_file(cap)
    await _set_rls_context(session, task.enterprise_id)

    lock = _CHAT_LOCKS.setdefault(task.id, asyncio.Lock())
    async with lock:
        seq = [await _next_seq(session, task)]
        await _append_events(session, task, seq, [("user", message)])
        try:
            proc = await asyncio.create_subprocess_exec(
                hermes_bin, "chat", "-q", message,
                "-t", "bidvolt", "--resume", sid,
                "--cli", "-Q", "--max-turns", "60", "--no-restore-cwd",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=env, cwd=env["HERMES_HOME"],
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=1800)
        except TimeoutError:
            raise ValueError("主会话本轮对话超时（1800s）：请稍后再试") from None
        raw = out.decode("utf-8", "replace") + "\n" + err.decode("utf-8", "replace")
        reply = _strip_session_trailer(raw).strip()
        await _append_events(session, task, seq, [("hermes", reply or raw.strip()[-800:])])
        return {"reply": reply, "session_id": sid, "returncode": proc.returncode}


def _strip_session_trailer(raw: str) -> str:
    """-Q 模式下输出=最终回复+会话信息尾注；剥离尾注行，保留回复正文。"""
    lines = raw.splitlines()
    out = []
    for ln in lines:
        if re.match(r"^(Resume this session with:|Session:|Duration:|Messages:|$)", ln.strip()):
            continue
        out.append(ln)
    return "\n".join(out).strip()


_KIND_TITLES = {
    "service": "服务 → 主会话",
    "hermes": "主会话输出",
    "tool": "工具/子任务",
    "error": "错误",
    "user": "客户 → 主会话",
}


async def session_record_markdown(session: AsyncSession, task: Task) -> str:
    """把主会话全程事件渲染为 Markdown 记录（交付件附件：会话记录带上）。"""
    from sqlalchemy import select as sa_select

    rows = (
        await session.scalars(
            sa_select(AgentSessionEvent)
            .where(AgentSessionEvent.task_id == task.id)
            .order_by(AgentSessionEvent.seq)
        )
    ).all()
    lines = [
        "# Hermes 主会话 · 全程记录",
        "",
        f"- 任务 id：{task.id}",
        f"- 会话 id：{(task.result or {}).get('session_id') or '（未记录）'}",
        f"- 事件数：{len(rows)}",
        "",
        "> 本记录为系统与 Hermes 主会话的完整消息流：服务发送的指令、主会话的每次回复、"
        "子任务委派（delegate_task）与验收结论均按时间顺序保留。",
        "",
    ]
    for r in rows:
        title = _KIND_TITLES.get(r.kind, r.kind)
        ts = r.created_at.strftime("%H:%M:%S") if r.created_at else ""
        lines.append(f"## [{r.seq}] {title} · {ts}")
        lines.append("")
        lines.append("```text")
        lines.append(r.content or "")
        lines.append("```")
        lines.append("")
    return "\n".join(lines)
