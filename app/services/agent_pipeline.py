"""新方案：Agent 主会话端到端（与旧管线完全隔离）。

一个任务 = 一个 Hermes 主会话（长驻 REPL 进程）；主 agent 自主用 todo 计划 +
delegate_task 派分析/提取/写作/校验/评审子 agent，按验收报告循环修复，
满足要求后输出完成标记（【PIPELINE_COMPLETE】）才交付。

Hermes 的委派机制：顶层会话的 delegate_task 一律后台运行，子 agent 结果
完成后作为新消息自动回到会话——因此主会话进程必须保持存活（不能像 `-q`
单轮模式那样在主 agent 本轮停笔后立刻退出，否则子 agent 随进程一起被终止）。
服务端以 PTY 方式长驻 `hermes chat --cli` REPL：喂入初始任务书，
子 agent 结果自动回流、主 agent 自动继续，直到输出完成标记或超时。

本模块职责（框架层）：
- 启动/维持主会话，把服务发送的消息与主会话的全部输出逐条落事件表
  （会话控制台数据源）；
- 卡顿时温和催办、过早退出时自动 --resume 续跑；
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
PIPELINE_TIMEOUT = 7200
# 事件落库节流：每 5 秒刷一次
EVENT_FLUSH_SECONDS = 5
# 卡顿阈值：无任何新输出超过该时长则温和催办一次
STALL_SECONDS = 600
# 最多催办次数（逐级加强，最后一次只索取结束标记；之后仍未输出则按会话记录判定收尾）
NUDGE_LIMIT = 3
# 主会话进程提前退出（未输出完成标记）时，自动 --resume 续跑的轮数上限
RESUME_ROUNDS = 2
# 完成标记轮询间隔（读 Hermes 会话库，判据=主会话最后一条回复，不受终端回显干扰）
MARKER_POLL_SECONDS = 30
# 完成协议标记（skill 与提示词同步约定）
MARK_COMPLETE = "【PIPELINE_COMPLETE】"
MARK_INCOMPLETE = "【PIPELINE_INCOMPLETE】"

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*(\x07|\x1b\\)")
_NOISE_RE = re.compile(r"^[─═╭╮╰╯│┌└┐┘├┤┊\s]+$")

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


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text).replace("\r", "\n")


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


def _spawn_repl(hermes_bin: str, args: list[str], env: dict):
    """以 PTY 长驻方式启动 hermes chat --cli REPL。返回 (proc, master_fd)。
    仅 Linux 可用（服务器环境）；本地 Windows 开发不执行主会话。"""
    import subprocess  # noqa: PLC0415

    try:
        import fcntl  # noqa: PLC0415
        import pty  # noqa: PLC0415
        import struct  # noqa: PLC0415
        import termios  # noqa: PLC0415
    except ImportError as exc:  # Windows 开发环境
        raise ValueError("Agent 主会话长驻 REPL 仅支持 Linux 服务器") from exc

    master_fd, slave_fd = pty.openpty()
    try:
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 200, 0, 0))
        proc = subprocess.Popen(
            [hermes_bin, *args],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env=env,
            cwd=env["HERMES_HOME"],
            close_fds=True,
        )
    finally:
        os.close(slave_fd)
    os.set_blocking(master_fd, False)
    return proc, master_fd


def _repl_submit(master_fd: int, text: str) -> None:
    """向 REPL 提交一行：bracketed-paste 包裹（REPL 开启粘贴模式时裸回车不提交）。"""
    payload = "\x1b[200~" + text.replace("\r", " ").replace("\n", " ") + "\x1b[201~" + "\r"
    try:
        os.write(master_fd, payload.encode("utf-8"))
    except OSError as exc:
        logger.warning("REPL 提交失败：%s", exc)


async def run_agent_pipeline(session: AsyncSession, task: Task) -> None:
    """执行 Agent 主会话（长驻 REPL）：喂入任务书后由主 agent 自主完成
    解析→撰写→校验→评审→交付循环，输出完成标记后收尾。"""
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
    payload = task.payload or {}
    resume_session_id = str(payload.get("resume_session_id") or "").strip() or None
    resume_from_task = payload.get("resume_from_task_id")
    if resume_session_id:
        prompt = (
            f"这是对上一轮主会话任务（id={resume_from_task or '上一单'}）的【续跑】，沿用同一会话上下文。"
            "项目数据可能已更新（如新增/更换材料、补录企业资料）：请先用 MCP 工具重新核实项目现状"
            "（list_requirements / list_project_materials / get_deliverable_content / search_assets），"
            "对照上一轮的总结与【待补充】清单，从断点继续推进解析→撰写→校验→评审→成文→打包。"
            "流程与守则见预载 skill（bidvolt-agent-pipeline）。"
            "成文要点：fill_template_slice 的 fields 必填四个值——buyer=采购人、project_name=项目名称、"
            "tender_no=采购编号、supplier=响应供应商企业名称（取企业资料库 supplier_name 事实，search_assets 可查；"
            "没有则留空标【待补充】）；企业其他事实（法人/信用代码/地址/电话/业绩/人员）放进 append 的撰写内容；"
            "封存前逐份 verify_template_slice。"
            "打包前必须核对 get_template_outline 里 is_file_item=true 的全部条目都已 seal_template_item 并"
            "已包含在 artifact_ids 中，缺失的先补齐 slice→fill→append→verify→seal 再 package_response_zip。"
            "打包前逐份抽验：价格/商务/技术每部分的各份 docx 内容必须各不相同、每份首段须含各自条目标题"
            "（发现两份内容雷同=切片串区，必须重做 slice→fill→append→verify→seal 再打包）。"
            "评分步骤：submit_score_items 成功落分即算该步闭环，自动评分分数仅作记录与风险提示，不作验收门。"
            f"任务 id={task.id}。全部完成后，最后一行单独输出 {MARK_COMPLETE}；"
            f"若确有无法闭环项，最后一行输出 {MARK_INCOMPLETE} 并说明原因。"
        )
    else:
        prompt = (
            f"请为项目 {task.project_id} 执行投标工作台端到端流程：解析→撰写→校验→评审→交付。"
            "流程与守则见预载 skill（bidvolt-agent-pipeline）；用 todo 列计划，"
            "用 delegate_task 派子任务（子 agent 结果会自动回到本会话，派完继续推进，不要停下来等），"
            "验收不通过带报告修复，全部满足后再输出。"
            "成文要求：get_template_outline 里 is_file_item=true 的全部条目都必须"
            "slice→fill→append→verify→seal 后一起 package_response_zip；"
            "build_quote_xlsx 的 sheets[].rows 必须是数组的数组（每行一个数组，不得用对象/带 item 键）。"
            "商务/技术偏差表若应答无偏差：必须在表格标题旁或表内首行显式标注"
            "「本表空白=无偏差（按采购文件约定，选择无偏差时无需填写本表）」并加批注，不得只留空表。"
            "打包前逐份抽验：价格/商务/技术每部分的各份 docx 内容必须各不相同、每份首段须含各自条目标题"
            "（发现两份内容雷同=切片串区，必须重做 slice→fill→append→verify→seal 再打包）。"
            "评分步骤：submit_score_items 成功落分即算闭环，自动评分分数仅作记录不作验收门。"
            f"任务 id={task.id}。全部完成后，最后一行单独输出 {MARK_COMPLETE}；"
            f"若确有无法闭环项，最后一行输出 {MARK_INCOMPLETE} 并说明原因。"
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

    loop = asyncio.get_running_loop()
    started_at = loop.time()

    base_args = [
        "chat", "--cli",
        "-t", "bidvolt,todo,delegation,file,vision",
        "-s", "bidvolt-agent-pipeline",
        "--no-restore-cwd", "--max-turns", "120",
    ]

    async def _run_repl_round(resume_sid: str | None, first_message: str) -> tuple[str | None, str | None]:
        """启动（或 --resume）一个 REPL 进程，驱动到完成标记或进程退出。
        返回 (完成标记文本, 会话 id)：标记为 COMPLETE/INCOMPLETE，或 None（进程先退出）。"""
        args = list(base_args)
        if resume_sid:
            args += ["--resume", resume_sid]
        proc, master_fd = _spawn_repl(hermes_bin, args, env)

        buf_text = ""  # 已剥离 ANSI 的原始文本
        echo_texts: set[str] = set()
        last_line = ""
        pending: list[tuple[str, str]] = []
        last_out_at = loop.time()
        nudges = 0
        sent_first = False
        session_id: str | None = resume_sid
        round_marker: str | None = None
        marker_poll_at = 0.0
        # 续跑基线：旧会话里已有消息（含上一单回执）不算本轮完成
        marker_min_index = 0
        if resume_sid:
            try:
                marker_min_index = await asyncio.wait_for(
                    _poll_session_message_count(hermes_bin, env, resume_sid), timeout=60
                )
            except Exception:  # noqa: BLE001 基线取不到就按 0（宁可多等一轮催办）
                marker_min_index = 0
        # 卡顿判据基线：会话消息数增长=有进展（工具调用/子代理也都会落消息，
        # 不能只看 PTY 输出——工具执行期间终端可能长时间静默导致误催办）
        last_msg_count = marker_min_index

        def _submit(text: str) -> None:
            echo_texts.add(text)
            _repl_submit(master_fd, text)

        def _on_readable() -> None:
            nonlocal buf_text, last_out_at, sent_first, session_id, last_line
            try:
                chunk = os.read(master_fd, 65536)
            except (BlockingIOError, OSError):
                return
            if not chunk:
                # 子进程已退出（EOF）：摘掉 reader，防止事件循环忙轮询
                try:
                    loop.remove_reader(master_fd)
                except Exception:  # noqa: BLE001
                    pass
                return
            try:
                last_out_at = loop.time()
                text = _strip_ansi(chunk.decode("utf-8", "replace"))
                buf_text += text
                if not sent_first and "Activated skills" in buf_text and "❯" in buf_text:
                    # 就绪后立即提交本轮首条消息
                    sent_first = True
                    _submit(first_message)
                m = re.search(r"Session:\s*([\w-]+)", buf_text)
                if m:
                    session_id = m.group(1)
                for ln in text.split("\n"):
                    ln = ln.strip()
                    if not ln or _NOISE_RE.match(ln) or ln == last_line:
                        continue
                    # 我方提交在 REPL 里的回显（含提示符前缀）不作为主会话输出
                    t = ln
                    for p in ("❯", ">", "›"):
                        if t.startswith(p):
                            t = t[len(p):].strip()
                            break
                    if ln in echo_texts or t in echo_texts:
                        continue
                    last_line = ln
                    pending.append(("hermes", ln))
            except Exception:  # noqa: BLE001 回调内异常不得吞掉事件流
                logger.exception("主会话输出解析失败（task=%s）", task.id)

        loop.add_reader(master_fd, _on_readable)

        async def _flush() -> None:
            if pending:
                batch, pending[:] = list(pending), []
                await _append_events(session, task, seq, batch)

        try:
            while True:
                await asyncio.sleep(EVENT_FLUSH_SECONDS)
                await _flush()
                # 完成标记：读 Hermes 会话库的主会话最后一条回复（权威判据，
                # 不解析终端回显，避免我方提示里的标记文本误判）；
                # 同一份导出顺便看消息总数增长 → 有增长即算有进展（防误催办）
                if session_id and loop.time() - marker_poll_at > MARKER_POLL_SECONDS:
                    marker_poll_at = loop.time()
                    try:
                        data = await asyncio.wait_for(
                            _export_session_json(hermes_bin, env, session_id), timeout=60
                        )
                        round_marker = _marker_from_export(data, marker_min_index)
                        if data:
                            n = int(data.get("message_count") or len(data.get("messages") or []))
                            if n > last_msg_count:
                                last_msg_count = n
                                last_out_at = loop.time()
                    except Exception:  # noqa: BLE001 会话库读取瞬时失败下次再试
                        round_marker = None
                    if round_marker:
                        break
                # 进程提前退出且没有待刷事件
                if proc.poll() is not None and not pending:
                    break
                # 卡顿催办：逐级加强，最后一级只索取结束标记
                if loop.time() - last_out_at > STALL_SECONDS:
                    if nudges >= NUDGE_LIMIT:
                        logger.warning("主会话持续无输出，判定卡死（task=%s）", task.id)
                        round_marker = MARK_INCOMPLETE
                        pending.append(
                            ("error", f"{MARK_INCOMPLETE}主会话未按协议输出结束标记（系统催办 {NUDGE_LIMIT} 次），"
                                      "由服务端按会话记录判定收尾")
                        )
                        break
                    nudges += 1
                    if nudges == 1:
                        nudge = (
                            "（系统提示）请继续推进流程：若子 agent 结果已返回请继续下一步；"
                            f"全部完成后最后一行输出 {MARK_COMPLETE}。"
                        )
                    elif nudges == 2:
                        nudge = (
                            "（系统提示）请继续推进流程。注意：最终回复的**最后一行必须且只能**输出结束标记——"
                            f"全部验收通过输出 {MARK_COMPLETE}；存在无法闭环项输出 {MARK_INCOMPLETE} 原因…。"
                            "总结正文放在标记之前。"
                        )
                    else:
                        nudge = (
                            "（系统提示）请现在输出结束标记作为交付回执：流程已完成就在最后一行只写 "
                            f"{MARK_COMPLETE}；确有无法闭环项就写 {MARK_INCOMPLETE} 并紧接着写出具体原因"
                            "（写真实原因，如「企业资料缺失」「时间不足」，不要写「+原因」等占位字样）。"
                        )
                    _submit(nudge)
                    pending.append(("service", nudge))
                    last_out_at = loop.time()
                # 总时长上限
                if loop.time() - started_at > PIPELINE_TIMEOUT:
                    round_marker = MARK_INCOMPLETE
                    pending.append(
                        ("error", f"{MARK_INCOMPLETE}主会话总时长超过 {PIPELINE_TIMEOUT}s，服务端终止")
                    )
                    break
        finally:
            try:
                loop.remove_reader(master_fd)
            except Exception:  # noqa: BLE001 已因 EOF 摘除时重复摘除
                pass
            await _flush()
            if round_marker is not None or proc.poll() is None:
                _repl_submit(master_fd, "/exit")
            try:
                await asyncio.wait_for(asyncio.to_thread(proc.wait), timeout=60)
            except asyncio.TimeoutError:  # noqa: UP041 服务器 Python 3.10
                proc.kill()
                await asyncio.to_thread(proc.wait)
            try:
                os.close(master_fd)
            except OSError:
                pass

        return round_marker, session_id

    # 第一轮 + 最多 RESUME_ROUNDS 轮自动续跑（主会话提前退出时）
    marker, sid = await _run_repl_round(resume_sid=resume_session_id, first_message=prompt)
    _, tail = await _repl_session_state(session, task)
    resume_rounds = 0
    continuation = ""
    while marker is None and resume_rounds < RESUME_ROUNDS and sid:
        resume_rounds += 1
        continuation = (
            f"（系统续跑）上一个进程提前退出，请继续未完成的任务 {task.id} 流程："
            "检查 todo 计划与已有成果，从断点继续推进（子 agent 结果会自动回到本会话）。"
            f"全部完成后最后一行输出 {MARK_COMPLETE}；"
            f"确实无法闭环则输出 {MARK_INCOMPLETE} 并说明原因。"
        )
        await _append_events(
            session, task, seq,
            [("service", f"主会话提前退出（第 {resume_rounds} 次自动续跑）：--resume {sid} 继续。")],
        )
        marker, sid = await _run_repl_round(resume_sid=sid, first_message=continuation)
        _, tail = await _repl_session_state(session, task)

    if marker == MARK_COMPLETE:
        task.result = {
            "runtime": "hermes-main-session",
            "session_id": sid,
            "log_tail": tail[-800:],
            "outcome": "complete",
            "note": "Agent 主会话端到端完成：计划/子任务/验收报告见会话控制台（事件流），session_id 可恢复。",
        }
        task.progress = {
            "phase": "agent_pipeline",
            "status": "done",
            "percent": 100,
            "current_work": "Agent 主会话完成（全部验收门通过）",
        }
    elif marker == MARK_INCOMPLETE:
        # 主会话自主判定未闭环（如实标注，未冒充完成）：这是流程的最终结论，
        # 不是执行失败——不再重试（重跑也不会改变硬约束），如实交付原因。
        reason = ""
        search_tail = tail.replace(prompt, "")
        if continuation:
            search_tail = search_tail.replace(continuation, "")
        m = re.search(re.escape(MARK_INCOMPLETE) + r"([^\n]*)", search_tail)
        if m:
            reason = m.group(1).strip()
        # 过滤占位回声：agent 照抄提示模板时会把「+原因」「不要其他内容」等字样当正文输出
        if reason in ("+原因", "＋原因", "原因…", "原因", "") or "不要其他内容" in reason:
            reason = ""
        task.result = {
            "runtime": "hermes-main-session",
            "session_id": sid,
            "log_tail": tail[-800:],
            "outcome": "incomplete",
            "reason": reason or "主会话判定未闭环（详见会话记录）",
            "note": "Agent 主会话走完全部流程并如实判定未闭环（未冒充完成）：原因见 reason。"
                    "补齐硬约束（如企业资料）后可重新发起 agent-run。会话控制台可回看全程。",
        }
        task.progress = {
            "phase": "agent_pipeline",
            "status": "done",
            "percent": 100,
            "current_work": "主会话完成（如实判定未闭环，原因见 result.reason）",
        }
    else:
        raise ValueError(
            "Agent 主会话提前退出且未输出完成标记"
            f"（session_id={sid}，可恢复后续跑）"
        )


async def _export_session_json(hermes_bin: str, env: dict, session_id: str) -> dict | None:
    """导出一个 Hermes 会话的最新 jsonl 记录（最后一条）。失败返回 None。"""
    import json  # noqa: PLC0415

    proc = await asyncio.create_subprocess_exec(
        hermes_bin, "sessions", "export", "--session-id", session_id, "--format", "jsonl", "-",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        env=env, cwd=env["HERMES_HOME"],
    )
    out, _ = await proc.communicate()
    lines = [ln for ln in out.decode("utf-8", "replace").splitlines() if ln.strip()]
    if not lines:
        return None
    try:
        return json.loads(lines[-1])
    except ValueError:
        return None


def _marker_from_export(data: dict | None, min_index: int = 0) -> str | None:
    """从会话导出数据判完成标记（只看 min_index 之后的最后一条 assistant 回复）。"""
    if not data:
        return None
    messages = data.get("messages") or []
    for idx in range(len(messages) - 1, min_index - 1, -1):
        m = messages[idx]
        if m.get("role") != "assistant":
            continue
        content = str(m.get("content") or "")
        if MARK_COMPLETE in content:
            return MARK_COMPLETE
        if MARK_INCOMPLETE in content:
            return MARK_INCOMPLETE
        break  # 只看最后一条 assistant 回复
    return None


async def _poll_session_marker(hermes_bin: str, env: dict, session_id: str, min_index: int = 0) -> str | None:
    """读 Hermes 会话库：最后一条 assistant 回复若带完成标记则返回该标记。
    判据来自会话库原文（模型真实回复），与终端显示回显无关。
    min_index：只看该下标之后的 assistant 消息（续跑时跳过上一单的旧回执）。"""
    return _marker_from_export(await _export_session_json(hermes_bin, env, session_id), min_index)


async def _poll_session_message_count(hermes_bin: str, env: dict, session_id: str) -> int:
    """读会话库当前消息总数（续跑基线：旧消息里的回执不算本轮完成）。"""
    data = await _export_session_json(hermes_bin, env, session_id)
    if not data:
        return 0
    try:
        return int(data.get("message_count") or len(data.get("messages") or []))
    except (ValueError, TypeError):
        return 0


async def _repl_session_state(session: AsyncSession, task: Task) -> tuple[str | None, str]:
    """从事件尾里取 session_id 与日志尾（REPL 模式下会话 id 出现在启动横幅中）。"""
    tail = await _event_tail(session, task, 6000)
    m = re.search(r"Session:\s*([\w-]+)", tail)
    return (m.group(1) if m else None), tail


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
                "-t", "bidvolt,todo,delegation,file,vision", "--resume", sid,
                "--cli", "-Q", "--max-turns", "60", "--no-restore-cwd",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=env, cwd=env["HERMES_HOME"],
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=1800)
        except asyncio.TimeoutError:  # noqa: UP041 服务器 Python 3.10
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
