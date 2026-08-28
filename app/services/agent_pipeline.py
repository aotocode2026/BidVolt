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

# 主会话最大时长（秒）：端到端包含多轮子任务 + 跑内验收回修循环 + 完成复核，
# 主会话自主收敛需要充足预算（378 实测 2h 不够，验收循环干到超时被切）——给足 3h
PIPELINE_TIMEOUT = 10800
# 事件落库节流：每 5 秒刷一次
EVENT_FLUSH_SECONDS = 5
# 卡顿阈值：无任何新输出超过该时长则温和催办一次
STALL_SECONDS = 600
# 最多催办次数（逐级加强，最后一次只索取结束标记；之后仍未输出则按会话记录判定收尾）
NUDGE_LIMIT = 3
# 主会话进程提前退出（未输出完成标记）时，自动 --resume 续跑的轮数上限
RESUME_ROUNDS = 2
# 主会话输出 COMPLETE 后，系统追加的复核确认轮数（逐份自查交付件，当场修复后重新回执）
CONFIRM_ROUNDS = 2
# 完成标记轮询间隔（读 Hermes 会话库，判据=主会话最后一条回复，不受终端回显干扰）
MARKER_POLL_SECONDS = 30
# 完成协议标记（skill 与提示词同步约定）
MARK_COMPLETE = "【PIPELINE_COMPLETE】"
MARK_INCOMPLETE = "【PIPELINE_INCOMPLETE】"

# 主会话工具集：全能力开放（产品决定）。bidvolt=全部 39 个业务 MCP 工具（白名单已全放行）；
# 其余为 Hermes 内置工具集（web/browser/terminal/code_execution/file/vision/
# image_gen/tts/skills/todo/memory/session_search/clarify/delegation/cronjob/computer_use）。
# 危险命令确认由 --yolo 跳过（自主批量运行无人点确认）；交付件合规性由
# 主会话 + 验收/评审子 agent 多轮保证（服务端只给信息信号，不设硬性流程代码）。
_HERMES_TOOLSETS = (
    "bidvolt,web,browser,terminal,code_execution,file,vision,image_gen,tts,"
    "skills,todo,memory,session_search,clarify,delegation,cronjob,computer_use"
)
# 网页端客户对话（chat_with_session）：与管线完全一致——全部工具集 + --yolo（产品决定：放开一切限制）
_CHAT_TOOLSETS = _HERMES_TOOLSETS

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
            "写作要求：技术方案/专项响应等方案性条目必须写出完整专业正文（方案性内容大胆写、"
            "事实性数据守据——企业事实用 search_assets，缺数标【待补充】不编造）。"
            "**证据附件纪律**：交付件中的事实条目（资质/证书/业绩/人员/财务/信用），企业资料库有对应扫描件的，"
            "交付件必须携带证据——用整文件直写通道把扫描件实际插入对应位置，"
            "或在条目旁列『附件N：原件在库（扫描件随附）』清单；只写事实不留证据=不合格。"
            "**评分点逐点核验（验收标准）**：对照本项目技术/商务评分标准逐点评分点，"
            "每个评分点都要在交付件中找到实质响应内容与对应证据附件；只有空话没有可核对内容=判不过列清单。"
            "**写作前范式内化（现实口径）**：真实中标标书全文一般不公开（公开的多为成交结果公示与写作规范/模板/教程），"
            "写作子 agent 动笔前先检索可得的公开范式——search_web 查同行业技术响应写法/章节展开惯例/图表表格用法/"
            "国网及电力行业投标文件编制规范与格式范例，能搜到的范文、教程提炼其结构与表达惯例；"
            "搜不到全文范本时不强求，以本项目评分标准+技术规范书+《响应文件格式》模板为纲逐章展开撰写。"
            "若上一轮方案性条目深度不足（只有目录没有实质正文、或声称的内容实际不存在）："
            "用 search_web_minimax/search_web 检索行业标准、同类项目做法与专业规范，"
            "把技术方案/专项响应深化到「可以直接拿去投标」的程度（技术规范书的每项技术要求"
            "都有实质性响应、正文实际写在文件里，篇幅以内容需要为准，不凑字数），"
            "再重新 append→verify→seal→package_response_zip。"
            "回执前逐份 inspect_agent_artifact 核对 pending_items 清单：分标/包信息从报价单/采购文件提取回填"
            "进 fields.values；电话/地址/邮编/法人等从 search_assets 企业资料回填进 fields.values；"
            "待补充标签必须具体（客户照着就能补）；报价明细表/授权委托书等文件里**无标签下划线产生的裸【待补充】**"
            "用 fills 逐处定向替换为带具体标签（如【待补充：被授权人姓名】【待补充：不含税单价】），"
            "含模板表格的条目（业绩表/人员表/报价表等）按 slice 回执的 tables 清册用 table_fills **逐行填表**"
            "（填表头之外的数据行），空表+文末挂文字代替=不合格；**不得另起与模板原表重复的新表**——"
            "数据必须填进模板自身的表格（专项响应文件的模板业绩表必须用 table_fills 填其数据行），"
            "修完重新 seal→package 再回执。"
            "搜索全领域开放：search_web_minimax（MiniMax 原生）/ search_web / Hermes web 均可搜"
            "（标的信息、企业公开信息、行业技术方案、商务写作范例、政策标准），来源经 save_source/link_citation 批注。"
            "成文要点：fill_template_slice 的 fields——buyer/project_name/tender_no/supplier 四个便捷键照填"
            "（supplier 取企业资料库 supplier_name 事实，search_assets 可查；没有则留空标【待补充】）；"
            "其余任何模板词（法人/地址/电话/邮编/分标/包/自定义标签等）用 fields.values={模板词:{value,source}} "
            "通用词表填实，值从哪取由你决定（资料库/采购文件/搜索/推断）；"
            "放不进模板空位的事实（业绩/人员明细等）才放进 append 的撰写内容；"
            "封存前逐份 verify_template_slice（verify 之后又 fill/append 改动过，必须重验）。"
            "打包前必须核对 get_template_outline 里 is_file_item=true 的全部条目都已 seal_template_item 并"
            "已包含在 artifact_ids 中，缺失的先补齐 slice→fill→append→verify→seal 再 package_response_zip。"
            "打包前由验收子 agent 以「能不能直接投标」为目标自行核验（可用信号：matched_title/req_title/"
            "was_verified、package 回执的 audit 清单、inspect_agent_artifact 打开每份文件看实际内容），"
            "发现任何妨碍直接投标的问题就回修、重新封存打包，直到验收子 agent 确认达标。"
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
            "写作要求（客户要的是能直接投标的交付件）：技术方案/专项响应等方案性条目必须写出完整专业的正文"
            "（项目理解→总体方案/架构→分项技术方案→实施组织与进度→质量/安全/进度保障→售后培训），"
            "方案性内容大胆写、事实性数据守据——企业事实用 search_assets，缺数标【待补充】不编造。"
            "方案正文必须实际写在文件里且完整——只有目录没有实质正文=不合格；"
            "用「详见附件X/第X章」交叉引用是正常投标写法，但验收时必须核实所指内容真实存在且有实质内容。"
            "**证据附件纪律**：交付件中的事实条目（资质/证书/业绩/人员/财务/信用），企业资料库有对应扫描件的，"
            "交付件必须携带证据——用整文件直写通道把扫描件实际插入对应位置，"
            "或在条目旁列『附件N：原件在库（扫描件随附）』清单；只写事实不留证据=不合格。"
            "**评分点逐点核验（验收标准）**：对照本项目技术/商务评分标准逐点评分点，"
            "每个评分点都要在交付件中找到实质响应内容与对应证据附件；只有空话没有可核对内容=判不过列清单。"
            "**写作前范式内化（现实口径）**：真实中标标书全文一般不公开（公开的多为成交结果公示与写作规范/模板/教程），"
            "写作子 agent 动笔前先检索可得的公开范式——search_web 查同行业技术响应写法/章节展开惯例/图表表格用法/"
            "国网及电力行业投标文件编制规范与格式范例，能搜到的范文、教程提炼其结构与表达惯例；"
            "搜不到全文范本时不强求，以本项目评分标准+技术规范书+《响应文件格式》模板为纲逐章展开撰写。"
            "搜索全领域开放：search_web_minimax（MiniMax 原生）/ search_web（AnySearch）/ Hermes web 均可搜"
            "——标的信息、企业公开信息、行业技术方案做法、商务标写作范例、政策法规标准；"
            "重要来源 save_source 入库 + link_citation 绑定，与企业资料库冲突时以资料库为准。"
            "成文要求：get_template_outline 里 is_file_item=true 的全部条目都必须"
            "slice→fill→append→verify→seal 后一起 package_response_zip；"
            "build_quote_xlsx 的 sheets[].rows 必须是数组的数组（每行一个数组，不得用对象/带 item 键）。"
            "填空机制边界：fill 的 fields.values 只命中「标签：空位/下划线」形态；无标签下划线"
            "（如「特授权____」）values 打不到——这类空位用 fills 定向替换成带具体标签"
            "（如【待补充：被授权人姓名】），不留裸【待补充】。"
            "商务/技术偏差表若应答无偏差：必须在表格标题旁或表内首行显式标注"
            "「本表空白=无偏差（按采购文件约定，选择无偏差时无需填写本表）」并加批注，不得只留空表。"
            "封存前逐份 verify_template_slice（verify 之后又 fill/append 改动过，必须重验）。"
            "交付前由验收子 agent 以「能不能直接投标」为目标自行核验（可用信号：matched_title/req_title/"
            "was_verified、package 回执的 audit 清单、inspect_agent_artifact 打开每份文件看实际内容），"
            "发现任何妨碍直接投标的问题就回修、重新封存打包，直到验收子 agent 确认达标。"
            "评分步骤：submit_score_items 成功落分即算该步闭环，自动评分分数仅作记录不作验收门。"
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
        "-t", _HERMES_TOOLSETS,
        "-s", "bidvolt-agent-pipeline",
        # 全能力开放：跳过危险命令确认与 shell 钩子确认（自主批量运行无人可点确认，
        # 留着确认流程=终端能力形同虚设；交付件合规性由主会话+验收/评审子 agent 保证）
        "--yolo", "--accept-hooks",
        "--no-restore-cwd", "--max-turns", "120",
    ]
    # 模型可选（A/B 用）：agent-run payload 传 model/provider 时切换主模型，
    # 不传默认 MiniMax-M3/minimax（config.yaml）
    if str(payload.get("model") or "").strip():
        base_args += ["-m", str(payload["model"]).strip()]
    if str(payload.get("provider") or "").strip():
        base_args += ["--provider", str(payload["provider"]).strip()]

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
                # 客户中途对话：复用现有泵循环——取出任务字段里的排队消息，
                # 经与催办/复核提示相同的 PTY 通道注入（hermes CLI 的 /queue 在忙时排队，
                # 主会话当前轮结束后按到达顺序逐条处理）。行锁防 app/worker 双进程丢失更新；
                # 失败下轮再试，不打断主流程。
                try:
                    from sqlalchemy import select as sa_select

                    row = (
                        await session.execute(
                            sa_select(Task).where(Task.id == task.id).with_for_update()
                        )
                    ).scalar_one()
                    pending_chat = list((row.payload or {}).get("pending_chat") or [])
                    if pending_chat:
                        row.payload = {**(row.payload or {}), "pending_chat": []}
                        await session.commit()
                        for msg in pending_chat:
                            _submit("/queue " + " ".join(str(msg).split()))
                except Exception:  # noqa: BLE001 瞬时失败（事务/锁）下轮再取
                    logger.warning("中途对话注入暂未成功（task=%s），下轮重试", task.id, exc_info=True)
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

    # 完成后的系统复核确认（最多 CONFIRM_ROUNDS 轮）：主会话回执后再逐份自查交付件，
    # 发现问题当场修复后重新回执——用信息信号（remaining_blanks/pending_items）驱动收敛
    confirm_rounds = 0
    while marker == MARK_COMPLETE and confirm_rounds < CONFIRM_ROUNDS and sid:
        confirm_rounds += 1
        confirm_prompt = (
            "（系统复核确认）请对交付件做最后确认：用 list_agent_artifacts + inspect_agent_artifact "
            "逐份打开每一份交付文件，核对 remaining_blanks/pending_items——"
            "凡能从采购文件/企业资料库/搜索取得的（如分标/包、电话/地址/邮编/法人）必须填实；"
            "只允许客户独占数据待补充且标签具体（客户照着就能补）；"
            "**裸【待补充】（pending_items 里 kind=bare，或 package 回执 audit.bare_pending 非空）必须清零**——"
            "逐处用 fills 改成具体标签（如【待补充：被授权人姓名】）。"
            "发现问题立即修复（重新 fill/seal/package）后再输出结束标记；"
            "确认无误最后一行输出 " + MARK_COMPLETE + "；确有无法修复项输出 " + MARK_INCOMPLETE + " 原因…。"
        )
        await _append_events(
            session, task, seq,
            [("service", f"系统复核确认（第 {confirm_rounds}/{CONFIRM_ROUNDS} 轮）：主会话逐份自查交付件。")],
        )
        confirm_marker, sid2 = await _run_repl_round(resume_sid=sid, first_message=confirm_prompt)
        _, tail = await _repl_session_state(session, task)
        if confirm_marker is not None:
            marker = confirm_marker
        if sid2:
            sid = sid2
        # 复核确认通过（COMPLETE）即收尾：一轮确认通过就停，不再空转第二轮
        # （第二轮复核曾卡住触发催办，agent 把催办文案回显成 INCOMPLETE 造成假阴性）
        break

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
        if (
            reason in ("+原因", "＋原因", "原因…", "原因", "")
            or "不要其他内容" in reason
            or "并紧接着写出具体原因" in reason
            or "不要写" in reason
            or "写真实原因" in reason
        ):
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

    # 收尾：把最终 zip 附带的会话记录刷新为完整版（含最终回执）+ 附精简版。
    # 打包时的快照早于最终回执，不刷新交付包里的会话记录会戛然而止。
    try:
        await _refresh_zip_record(session, task)
    except Exception:  # noqa: BLE001 记录刷新失败不影响任务结论
        logger.warning("收尾刷新会话记录失败（task=%s）", task.id, exc_info=True)


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
    """从会话导出数据判完成标记（只看 min_index 之后的最后一条 assistant 回复）。
    协议要求标记逐字出现在**最后一行**——正文里转述子 agent 的「判【PIPELINE_INCOMPLETE】」
    等字样不算回执（曾因此把主会话工作摘要误判为结束回执，提前终止任务）。"""
    if not data:
        return None
    messages = data.get("messages") or []
    for idx in range(len(messages) - 1, min_index - 1, -1):
        m = messages[idx]
        if m.get("role") != "assistant":
            continue
        content = str(m.get("content") or "")
        lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
        if not lines:
            continue
        last = lines[-1]
        if last == MARK_COMPLETE or last.startswith(MARK_COMPLETE):
            return MARK_COMPLETE
        if last == MARK_INCOMPLETE or last.startswith(MARK_INCOMPLETE):
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


async def queue_chat_message(session: AsyncSession, task: Task, message: str) -> None:
    """运行中的任务：把客户消息追加进任务字段 + 事件流（runner 泵循环取出后经 PTY 注入）。

    行锁串行化：app 与 worker 是两个进程，同写 task.payload.pending_chat 必须
    FOR UPDATE 防丢失更新（多条插话按到达顺序排队，逐条注入）。"""
    from sqlalchemy import select as sa_select

    from app.services.task_service import _set_rls_context  # noqa: PLC0415

    await _set_rls_context(session, task.enterprise_id)
    row = (
        await session.execute(
            sa_select(Task).where(Task.id == task.id).with_for_update()
        )
    ).scalar_one()
    payload = dict(row.payload or {})
    pending = list(payload.get("pending_chat") or [])
    pending.append(message)
    payload["pending_chat"] = pending
    row.payload = payload
    seq = [await _next_seq(session, task)]
    await _append_events(session, task, seq, [("user", message)])
    await session.commit()


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
                "-t", _CHAT_TOOLSETS, "--resume", sid,
                "--cli", "-Q", "--yolo", "--accept-hooks",
                "--max-turns", "60", "--no-restore-cwd",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=env, cwd=env["HERMES_HOME"],
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=1800)
        except asyncio.TimeoutError:  # noqa: UP041 服务器 Python 3.10
            raise ValueError("主会话本轮对话超时（1800s）：请稍后再试") from None
        raw = out.decode("utf-8", "replace") + "\n" + err.decode("utf-8", "replace")
        reply = _strip_session_trailer(raw).strip()
        await _append_events(session, task, seq, [("hermes", reply or raw.strip()[-800:])])
        # 纯代码收尾：任务完成后的对话也会把会话记录刷新进最终 zip（完整版+精简版），主会话不感知
        try:
            await _refresh_zip_record(session, task)
        except Exception:  # noqa: BLE001
            logger.warning("chat 后刷新会话记录失败（task=%s）", task.id, exc_info=True)
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


_NOISE_LINE_RES = (
    re.compile(r"^⚕"),
    re.compile(r"^❯"),
    re.compile(r"^\(°ロ°\)"),
    re.compile(r"^\(⌐■_■\)"),
    re.compile(r"^\(¬_¬\)"),
    re.compile(r"^╭─"),
    re.compile(r"^╰─"),
    re.compile(r"^┌─"),
    re.compile(r"^└"),
    re.compile(r"^[│╭╰╞┌└─═]+$"),
)
_BOX_CHARS = set("█░╭╮╰╯│┤┐┌└┘─═╞╡┴┬├╌╍═")


def _is_noise_line(s: str) -> bool:
    """会话记录噪音行判定：进度条/中断提示/思考动画/banner 框线。工具调用横幅不算噪音。"""
    s = s.strip()
    if not s:
        return False
    if any(p.match(s) for p in _NOISE_LINE_RES):
        return True
    if (
        s.startswith("┊ ⚡ preparing")
        or s.startswith("┊ 🔀 preparing")
        or s.startswith("┊ 💻 preparing")
        or s.startswith("⚡ mcp__")
        or s.startswith("💻")
        or s.startswith("🔀")
    ):
        return False
    body = [ch for ch in s if ch != " "]
    if body and sum(1 for ch in body if ch in _BOX_CHARS) / len(body) > 0.55:
        return True
    return False


def condense_session_markdown(md: str) -> str:
    """精简版会话记录：逐块过滤状态噪音，保留全部内容块（块内只要有一条内容行就整块保留，
    绝不丢正文）。文件头部（任务 id/会话 id/事件数等元信息）原样保留。供交付包附件与网页「精简视图」使用。"""
    out: list[str] = []
    preamble: list[str] = []
    head = ""
    buf: list[str] = []
    in_code = False
    started = False

    def _flush() -> None:
        if not buf:
            return
        body = [ln for ln in buf if ln.strip()]
        if not body or not all(_is_noise_line(ln) for ln in body):
            out.append(head)
            out.append("```text")
            out.extend(buf)
            out.append("```")
            out.append("")

    for line in md.splitlines():
        m = re.match(r"^## \[\d+\] (.+)$", line)
        if m:
            started = True
            _flush()
            head = line
            buf = []
            in_code = False
            continue
        if not started:
            preamble.append(line)
            continue
        if line.strip() == "```text":
            in_code = True
            continue
        if line.strip() == "```":
            in_code = False
            continue
        if in_code:
            buf.append(line)
    _flush()
    return "\n".join(preamble + out)


async def _refresh_zip_record(session: AsyncSession, task: Task) -> None:
    """任务收尾：把最终 zip 附带的会话记录刷新为完整版（含最终回执）并附精简版。
    打包时的记录快照必然早于主会话最终回执（回执在最后一次 package 之后才输出），
    不刷新的话交付包里的会话记录会戛然而止（任务 380 教训）。"""
    import io as _io
    import json as _json
    import zipfile as _zip

    from sqlalchemy import select as sa_select

    from app.models.agent import AgentArtifact
    from app.services.task_service import _set_rls_context  # noqa: PLC0415

    await _set_rls_context(session, task.enterprise_id)
    art = await session.scalar(
        sa_select(AgentArtifact)
        .where(AgentArtifact.task_id == task.id, AgentArtifact.kind == "zip")
        .order_by(AgentArtifact.id.desc())
        .limit(1)
    )
    if art is None:
        return
    full = await session_record_markdown(session, task)
    condensed = condense_session_markdown(full)
    try:
        with _zip.ZipFile(_io.BytesIO(art.content or b""), "r") as zin:
            entries = {n: zin.read(n) for n in zin.namelist()}
    except Exception as exc:  # noqa: BLE001
        logger.warning("收尾刷新会话记录失败（zip 无法读取，task=%s）：%s", task.id, exc)
        return
    full_b = full.encode("utf-8")
    cond_b = condensed.encode("utf-8")
    entries["会话记录/主会话记录.md"] = full_b
    entries["会话记录/主会话记录-精简版.md"] = cond_b
    try:
        man = _json.loads(entries.get("manifest.json") or b"{}")
        files = [
            f for f in man.get("files", [])
            if f.get("name") not in ("会话记录/主会话记录.md", "会话记录/主会话记录-精简版.md")
        ]
        files.append({"name": "会话记录/主会话记录.md", "bytes": len(full_b)})
        files.append({"name": "会话记录/主会话记录-精简版.md", "bytes": len(cond_b)})
        man["files"] = files
        entries["manifest.json"] = _json.dumps(man, ensure_ascii=False, indent=2).encode("utf-8")
    except Exception:  # noqa: BLE001 manifest 刷新失败不影响记录本体
        pass
    buf = _io.BytesIO()
    with _zip.ZipFile(buf, "w", _zip.ZIP_DEFLATED) as zout:
        for name, data in entries.items():
            zout.writestr(name, data)
    art.content = buf.getvalue()
    await session.commit()
