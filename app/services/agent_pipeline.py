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
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import QUESTION_GATE_WINDOW_MINUTES as ASK_WINDOW_MINUTES
from app.constants import TaskType
from app.models.agent import AgentSessionEvent
from app.models.task import Task

logger = logging.getLogger(__name__)

# 主会话最大时长（秒）：端到端包含多轮子任务 + 跑内验收回修循环 + 完成复核，
# 主会话自主收敛需要充足预算（378 实测 2h 不够，验收循环干到超时被切）——给足 3h
PIPELINE_TIMEOUT = 21600
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
# 客户交互协议标记（主会话回复里用结构化块提问/列动作清单，服务端提取后呈现给客户）
ASK_START, ASK_END = "【ASK】", "【ASK_END】"
ACTION_START, ACTION_END = "【ACTION_LIST】", "【ACTION_LIST_END】"
_ASK_RE = re.compile(r"【ASK】(.*?)【ASK_END】", re.S)
_ACTION_RE = re.compile(r"【ACTION_LIST】(.*?)【ACTION_LIST_END】", re.S)


def extract_ask_blocks(text: str) -> list[str]:
    """从会话文本提取【ASK】…【ASK_END】提问块正文（可能多块）。"""
    return [m.group(1).strip() for m in _ASK_RE.finditer(text or "")]


def extract_action_list(text: str) -> list[str]:
    """从会话文本提取【ACTION_LIST】…【ACTION_LIST_END】动作清单（逐行；「无」→[]）。"""
    blocks = [m.group(1).strip() for m in _ACTION_RE.finditer(text or "")]
    if not blocks:
        return []
    lines: list[str] = []
    for b in blocks:
        for ln in b.splitlines():
            ln = ln.strip().lstrip("-•· ").strip()
            if ln and ln not in ("无", "无。", "无；"):
                lines.append(ln)
    return lines


async def _customer_state(session, task_id: int, limit: int = 400) -> dict:
    """客户交互状态（工具口径为主，文本块兜底兼容旧会话）：
    ask_customer/report_customer_actions 工具落库的 agent_customer_ask 行 +
    旧版【ASK】/【ACTION_LIST】文本块扫描。
    返回 {asks: [{ask_id, kind, items, answered, answer, created_at}], action_list: [str]}。"""
    from sqlalchemy import select as _sa_select

    from app.models.agent import AgentCustomerAsk, AgentSessionEvent

    rows = (
        await session.scalars(
            _sa_select(AgentCustomerAsk)
            .where(AgentCustomerAsk.task_id == int(task_id))
            .order_by(AgentCustomerAsk.id.desc())
            .limit(50)
        )
    ).all()
    asks: list[dict] = []
    action_list: list[str] = []
    for r in rows:
        entry = {
            "ask_id": r.id,
            "kind": r.kind,
            "items": r.items or [],
            "answered": bool(r.answered),
            "answer": r.answer,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "window_minutes": int(r.window_minutes or 0),
            "timeout_notified": bool(r.timeout_notified),
        }
        if r.kind == "action":
            action_list = [str(x) for x in (r.items or []) if str(x).strip()] + action_list
        else:
            asks.append(entry)
    # 旧版文本块兜底：工具上线前会话里的【ASK】/【ACTION_LIST】块
    ev_rows = (
        await session.scalars(
            _sa_select(AgentSessionEvent)
            .where(AgentSessionEvent.task_id == int(task_id))
            .order_by(AgentSessionEvent.seq.desc())
            .limit(limit)
        )
    ).all()
    legacy_asks: list[dict] = []
    legacy_actions: list[str] = []
    for r in ev_rows:
        if r.kind in ("hermes", "service"):
            for blk in extract_ask_blocks(r.content or ""):
                legacy_asks.append(
                    {
                        "ask_id": None,
                        "kind": "question",
                        "items": [{"q": ln} for ln in blk.splitlines() if ln.strip()],
                        "answered": False,
                        "answer": None,
                        "created_at": None,
                        "legacy": True,
                    }
                )
            for al in extract_action_list(r.content or ""):
                legacy_actions = al + legacy_actions
    asks = asks + legacy_asks
    action_list = action_list + [a for a in legacy_actions if a not in action_list]
    return {"asks": asks, "action_list": action_list}

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
# 事件泵噪音过滤（比精简版记录更狠：状态条/表情动画/进度碎片不进事件库，控制台与记录都不再被淹没）
_PUMP_NOISE_RES = (
    re.compile(r"^⚕"),                     # 状态条：⚕ deepseek-v4-pro │ 0/1M │ ... ⚠ YOLO / ⚕ ❯ msg=interrupt ...
    re.compile(r"^❯"),                     # 提示符行 / ❯ msg=interrupt ...
    re.compile(r"^\([^()]{1,12}\)\s"),   # (°ロ°) deliberating... / (¬‿¬) pondering... / (◉_◉) ruminating...
    re.compile(r"^ヽ"),                    # ヽ(>∀<☆)☆ reflecting...
    re.compile(r"^🗜️"),
    re.compile(r"^↩"),
    re.compile(r"^\d{1,4}s?\s*│"),        # 状态条碎片：1s │ ⚠ YOLO / 10s │ ⚠ YOLO
    re.compile(r"^\d{1,6}$"),             # 进度条裸数字碎片：8 / 9 / 302
    re.compile(r"^⚠"),
    re.compile(r"^Requesting summary"),
    re.compile(r"^Iteration budget"),
)


def _is_pump_noise(ln: str) -> bool:
    """事件泵行级噪音判定：框线/状态条/表情动画/进度碎片。工具横幅与正文保留。"""
    if _NOISE_RE.match(ln):
        return True
    if any(p.match(ln) for p in _PUMP_NOISE_RES):
        return True
    return False

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
    from sqlalchemy import func as sa_func
    from sqlalchemy import select as sa_select

    from app.services.task_service import _set_rls_context  # noqa: PLC0415

    # 自含 RLS：本函数可能在任意 commit 之后被调用（泵循环的进度块/pending_chat
    # 提交都会清掉事务级 GUC）——入口先设上下文，保证 INSERT 不撞 WITH CHECK
    await _set_rls_context(session, task.enterprise_id)
    # app 与 worker 双进程都会写事件：用任务行锁把「取 max(seq) + 插入」串行化，
    # 避免两进程读到同一个 max 产生重复 seq（控制台顺序错乱）
    await session.execute(sa_select(Task).where(Task.id == task.id).with_for_update())
    cur = await session.scalar(
        sa_select(sa_func.max(AgentSessionEvent.seq)).where(AgentSessionEvent.task_id == task.id)
    )
    seq[0] = max(seq[0], int(cur or 0))
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


async def _append_events_fresh(task: Task, seq: list[int], batch: list[tuple[str, str]]) -> None:
    """泵侧事件写入统一入口：独立短命会话（R9 2057 崩溃链根因修复）。

    主会话连接一旦在某次事件写入中被取消/被锁链破坏器终止，整个泵就会
    在 run_task 的最终 commit 上崩溃（连接已死）→ retry 耗尽。事件写
    全部走独立短命会话后：取消/终止只殃及当次短命会话，主会话永不碰
    task 行锁，破坏器即使误杀也无后果。"""
    from app.db import SessionLocal as _PumpSessionLocal  # noqa: PLC0415

    async with _PumpSessionLocal() as _sf:
        await asyncio.wait_for(_append_events(_sf, task, seq, batch), timeout=30)


async def _lock_breaker_loop(task_id: int) -> None:
    """独立于泵主循环的锁链破坏器（R9 2057 冻死根因防线）。

    主循环里任何一处无超时的数据库等待（FOR UPDATE 等锁/连接获取）都会让
    整轮永久冻结——若破坏器也写在主循环里，冻结时它永远轮不到执行，锁链
    就成死锁。因此独立成 task：每 60s 自检一次，任务离开 RUNNING 即自退。
    只杀「idle in transaction 超 3 分钟且持有 task 行锁」的后端（挂起事务
    持锁者）；无锁滞留事务仅记录不杀（R9 误杀自身连接六连崩教训）。
    全部 DB 操作带超时，自身故障静默自愈。"""
    from sqlalchemy import text as _sa_text

    from app.db import SessionLocal as _WD  # noqa: PLC0415

    while True:
        await asyncio.sleep(60)
        try:
            async with _WD() as _w:
                # 任务已离开 RUNNING（成功/失败/重排队）→ 破坏器自退
                _status = await asyncio.wait_for(
                    _w.execute(
                        _sa_text("SELECT status FROM task WHERE id = :tid"), {"tid": task_id}
                    ),
                    timeout=20,
                )
                if int(_status.scalar() or 0) != 2:
                    break
                _idle = await asyncio.wait_for(
                    _w.execute(
                        _sa_text(
                            "SELECT pid, application_name, left(query, 80) FROM pg_stat_activity "
                            "WHERE datname = current_database() AND state = 'idle in transaction' "
                            "AND xact_start < now() - interval '5 minutes' AND pid <> pg_backend_pid()"
                        )
                    ),
                    timeout=20,
                )
                _idle_rows = _idle.fetchall()
                if _idle_rows:
                    logger.warning(
                        "悬挂事务观察（task=%s，独立破坏器）：%s",
                        task_id,
                        [(int(p), str(a), str(q)) for (p, a, q) in _idle_rows],
                    )
                _blocked = await asyncio.wait_for(
                    _w.execute(
                        _sa_text(
                            "SELECT DISTINCT l.pid FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
                            "WHERE l.relation = 'task'::regclass AND l.granted "
                            "AND a.state = 'idle in transaction' "
                            "AND a.xact_start < now() - interval '3 minutes' "
                            "AND a.pid <> pg_backend_pid()"
                        )
                    ),
                    timeout=20,
                )
                for (pid,) in _blocked.fetchall():
                    try:
                        await asyncio.wait_for(
                            _w.execute(_sa_text(f"SELECT pg_terminate_backend({int(pid)})")),
                            timeout=20,
                        )
                        logger.warning(
                            "独立锁链破坏器终止持 task 锁的悬挂事务（task=%s）：%s",
                            task_id,
                            int(pid),
                        )
                    except Exception:  # noqa: BLE001 单个终止失败不阻断其余
                        pass
                await _w.rollback()
        except Exception:  # noqa: BLE001 破坏器自身故障静默，下轮再试
            pass


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
        # 管线最长跑 PIPELINE_TIMEOUT（6h），cap 必须覆盖整个运行期：
        # 默认 1h TTL 曾让主会话在第 60 分钟全线 403（capability token 已过期），
        # 靠重签脚本自救才跑完——留 1h 余量，杜绝中途失效
        ttl=PIPELINE_TIMEOUT + 3600,
    )
    payload = task.payload or {}
    resume_session_id = str(payload.get("resume_session_id") or "").strip() or None
    resume_from_task = payload.get("resume_from_task_id")
    pre_chat = bool(payload.get("pre_chat"))
    if resume_session_id and not pre_chat:
        prompt = (
            f"这是对上一轮主会话任务（id={resume_from_task or '上一单'}）的【续跑】，沿用同一会话上下文。"
            "项目数据可能已更新（如新增/更换材料、补录企业资料）：请先用 MCP 工具重新核实项目现状"
            "（list_requirements / list_project_materials / get_deliverable_content / search_assets），"
            "对照上一轮的总结与提交前客户动作清单（report_customer_actions 记录），从断点继续推进解析→撰写→校验→评审→成文→打包。"
            "流程与守则见预载 skill（bidvolt-agent-pipeline）。"
            "写作要求：技术方案/专项响应等方案性条目必须写出完整专业正文（方案性内容大胆写、"
            "事实性数据守据——企业事实用 search_assets，缺数按成品纪律三级处置（填实/无·不适用/客户动作清单）不编造）。"
            "**证据附件纪律**：交付件中的事实条目（资质/证书/业绩/人员/财务/信用），企业资料库有对应扫描件的，"
            "交付件必须携带证据——用整文件直写通道把扫描件实际插入对应位置，"
            "或在条目旁列『附件N：原件在库（扫描件随附）』清单；只写事实不留证据=不合格。"
            "**评分点逐点核验（验收标准）**：对照本项目技术/商务评分标准逐点评分点，"
            "每个评分点都要在交付件中找到实质响应内容与对应证据附件；只有空话没有可核对内容=判不过列清单。"
            "**评分装订矩阵（动笔前必建）**：把评分标准拆成矩阵，每行一个评分点："
            "「评分点|分值|证明材料|模板位置|预计得分|状态」——按矩阵逐行装订证据，每个评分点都要有实质正文+对应证据附件；"
            "只写方案不装证据=漏分（材料在库却漏用、技术卷厚度不足即源于此；装订时逐件核验证据的主体/有效期/关联性，"
            "人工成稿常见的其他主体材料、过期证书、重复业绩不得装订）。"
            "**证据成文形态**：模板固定表按原表容量填报（行数不变），超出容量的业绩/人员/资质在评分支撑章节"
            "**逐组逐人成节**——每组业绩一节（业绩说明+合同封面/关键页/签章页/发票/查验截图整组扫描件实际插入）、"
            "每名人员一节（简历+学历职称证件+社保截图实际插入）、每项资质一节（证书扫描件实际插入）；"
            "金标准目录几百条=业绩组数×证据页数+人员数×证件页数+页码索引，照此形态成文目录自然对齐。"
            "**证据装订可派专职子 agent（独立轮次预算）**：插图重活交给「证据装订」子 agent"
            "（任务书：评分矩阵+资产清单+目标章节+插图规范≤16×23.2cm 等比例不撑页；"
            "用 download_project_material/fitz 提取扫描件+python-docx 插入+覆盖上传+render_qa_docx 复验）；"
            "证据=实际插入的图，「原件在库」清单只在硬阻塞时降级；验收时核对矩阵行与图数对齐，缺图判不过。"
            "**证据页数量化（照金标准页数口径）**：矩阵每行证据写明装订页数——审计/信用报告**整本装订**（三年审计 100+ 页一页不落），"
            "业绩每组=合同全页+发票+查验截图**整组**（不止封面签章页），人员=简历+全部证件+社保，证书=证书页全文；"
            "目录条目/图数/字数的差距都源于证据页数——验收核对每行图数达到写明的页数下限。"
            "**投标有效性核验（先于撰写）**：A 阶段先核验——①响应截止时间 vs 今天（search_web 查本项目是否已出预成交/成交公示、"
            "有无延期/重招，截止已过列入风险清单）；②采购文件「不接受委托中介机构编制」类条款下编制方与投标单位关系；"
            "③公告与技术规范书之间的地点/范围/数量矛盾清单；④最高限价/保证金/税率/多轮报价规则。"
            "**提问关（开编前问）**：A 分析后、开编前，把决策类问题用 ask_customer 一次批量问完"
            "（截止/延期、编制方与投标单位关系、业绩人员齐备、报价策略（争分优先/保毛利优先/无偏好）、合同条款接受度、地点/范围矛盾），"
            "答案回来再开编相应部分；不依赖答案的部分并行推进。"
            "**成品纪律（全面取消修订/批注与待补标签）**：交付件=Hermes 自己写完的终稿——"
            "成文直接干净写入正文（无修订模式、无 Word 批注），不留【待补充】标签、不留"
            "「建议值/请客户确认」元语言。每条信息三级处置：①能取得的填实（采购文件/企业资料/公开搜索/合理推断，"
            "推断值写中性事实）；②按采购文件口径本就不需要的填「无（…）/不适用」（如「无：自成立以来未发生名称变更」、"
            "「本项目免收响应保证金，本表不适用」），空表在标题旁注「本表空白=无偏差/不适用」；"
            "③确需客户实体动作的（签字/盖章/签署日期/提交证照原件）成文处按模板留白（不写标签），"
            "收尾前用 report_customer_actions 工具上报提交前客户动作清单。fill/verify 回执 remaining_blanks 里的【待补充】是"
            "「还没填完」的信号，逐项清零（填实或写无/不适用），裸【待补充】与「具体标签」字样=判不过。"
            "**深度展开方法**：方案性正文的产出载体是**文件**，不是对话回复——写作子 agent 用 write_file 分段/"
            "terminal 追加/python-docx 直写交付文件，一次写不完就继续写，写到深度达标为止（子 agent 也持整文件直写通道）。"
            "「写到什么程度」由技术规范书逐条要求+评分标准定：每条技术要求都要有实质性响应正文，"
            "禁止以「太长/一次写不下」为由只写目录或截断。图由 Hermes 自绘（mermaid/matplotlib 架构图/数据流图/拓扑图/甘特图插入交付文件）。"
            "**技术卷正文必须派专职「技术写作」子 agent（独立轮次预算）**：技术专项响应文件的正文不得由主会话顺手写——"
            "开编后立刻 delegate_task 派专职写作子 agent，任务书逐条写明（缺哪条判不过）："
            "①总体方案：总体设计思路+总体架构分层说明（按本项目技术规范书的架构描述分层，每层一段实质描述）+关键设备/系统清单表+"
            "核心技术与实现方案（适配范围/实现细节/性能保障，以技术规范书要求为准）+可靠性/保障方案；"
            "②分项技术方案按技术规范书/评分标准中的**每个评分对象/分项**逐项成文（现状痛点→本分项内容与范围→技术路线分步→"
            "关键参数与指标→风险与对策→验收标准），每分项正文不少于 500 字，另配作业卡（步骤/保障措施/工时/验收记录）；"
            "③实施组织与进度：职责分工表+进度计划编制说明+里程碑与交付物表+进度网络图说明与关键路径+进度纠偏措施；"
            "④质量安全进度保障：三级质检流程说明+检验批划分与验收标准表+安全技术交底制度+安全文明施工措施清单（≥10项）+风险矩阵表；"
            "⑤售后培训：质保期服务承诺明细表+培训计划表+分级响应流程长文+回访与满意度管理；"
            "⑥自绘图配额：每个分项至少 1 张流程图+总体方案 2-3 张（架构图/数据流图）+关键参数分解图、实施时序图，中文渲染自查。"
            "子 agent 回执后主会话逐章核查（章节齐全/字数达标/图已插入），不足的回修补足再进校验阶段——"
            "正文深度是验收硬指标，不达标不回执。"
            "公开语料没有金标准全文不是障碍：深度来源=技术规范书逐条要求+行业标准/白皮书/论文/同类项目公开资料+企业自身资料，"
            "搜索只作补充参考，以本项目文件为主为准。"
            "**向客户提问（ask_customer 工具，提问权+提问纪律）**：你可以向客户提问，但**先尽己力**——调用前必须完成三查"
            "（①企业资料库 search_assets+vision 读图取证 ②采购文件与技术规范书原件 ③公开搜索+按采购文件口径合理推断），"
            "三查都拿不到、且属「只有客户本人能提供/只能客户实体动作」的信息（如银行账户、真实签署人身份、证照原件）才允许问。"
            "禁止问：资料库里有的、公开可查的、可推断的、可写「无/不适用」的、不影响直接投标的。"
            "用 ask_customer 工具**批量**问（一次列完所有问题，不一条一条问），每条带 q/need/checked"
            "（问题/为什么需要/已自查说明——问得傻会被自己的 checked 暴露）；提问由主会话发起，"
            "子 agent 发现客户独占信息在回执里列给主会话，主会话统一批量 ask_customer。"
            "工具立即返回（ask_id），客户回答后回答会自动回到本会话；提问后不干等："
            "继续推进不依赖答案的工作；答案回来再回填相关字段并重验；"
            "收口时未答复项按三级处置（能推断的填实/写「无/不适用」/用 report_customer_actions 上报实体动作）。"
            f"**提问关问答窗口**：每条提问有 {ASK_WINDOW_MINUTES} 分钟问答窗口，超时未答系统会注入"
            "「问答窗口已过，由你自行决定」信号——收到即不再等待，按三级处置纪律自主拍板"
            "（材料类问题以企业资料库事实为准，能装订的全部装订整本/整组，不保守跳过）；"
            "客户之后补答会自动注入本会话，届时回填并重验。"
            "**报价依据必附（依据随项目而定，缺依据不出数字）**：测算出的每个报价数字必须附带书面依据，且依据写进交付件本身"
            "（报价单「备注/说明」格或价格文件「报价测算说明」页）。依据的**具体构成随本项目而定，由你按实际确定，不照清单硬套**："
            "成本法按本项目真实成本科目（人力/设备/差旅/管理/税费——有什么算什么，没有的不硬凑）；"
            "行情样本列真实找到的同类成交/报价（项目名+金额+日期+公示来源，get_history_price 行情库或 search_web 公开中标价）；"
            "规则面只写本项目真实存在的（限价有无、税率、付款口径、含代理费——无最高限价就写「本项目无最高限价」）。"
            "原则=依据真实、可复核、随件走；只写一句「处于行情区间内」没有任何明细=判不过。"
            "报价即最终报价：不写「建议报价/请客户确认调整」元语言。"
            "**报价目标=得分最优（硬纪律）**：按区间平均价浮动法反算——①从采购文件取公式参数（n1/n2/W1/W2/c）"
            "与最高限价（无最高限价就写明「本项目无最高限价」）；②基准价=历史同类成交中位×规模修正与本次行情样本"
            "（get_history_price/search_web 真实成交）的中位锚点；③最优报价≈基准价（按公式扣分斜率微调），"
            "落在异常低价红线与限价之间；**禁止成本加成定价**——成本科目只作底线校验与随件依据，不是定价公式"
            "（成本测算高于基准价时说明依据并仍按得分最优报）；客户回答「无偏好」=按本条执行。"
            "**报价反算硬验收门（测算工作簿必含、验收逐项核对，缺一项=判不过回修）**："
            "①三步反算表（锚点→基准价→最优报价，每步写数字+依据），报价单最终数字必须**等于**反算表结果；"
            "②**锚点选取纪律（限价优先）**：**本项目有最高限价的 → 锚点=最高限价原值**（限价是采购文件给定的"
            "唯一可靠基准参照，样本口径混杂时一律以限价为锚）；**无最高限价的 → 锚点=同形态最相近样本原值**"
            "（场景一致→口径一致→形态一致；形态相近的候选**金额不参与选择**，取与本项目范围最接近者）；"
            "锚点一经选定不得更换（反算表全文只允许一个锚点），写锚点来源与依据；"
            "禁止用「估计的有效报价均值 A2」或任何估算值充当锚点；禁止「上修」字样（出现=判不过）；"
            "最优报价=锚点×(1-C)=锚点×0.98 附近按扣分斜率微调后取整到万，**报价不得高于锚点原值**；"
            "③**成本底线纪律**：成本科目按本项目真实构成逐项测算——人力=人数×驻场/服务天数×人日单价"
            "（市场行情 1500-2500 元/人日，超出须注明依据），差旅/设备/管理费列实际口径；"
            "成本合计**不得高于锚点金额**——成本超过锚点=科目虚高（重复计费/单价离谱），"
            "必须复核削减；成本只作底线校验，**禁止 成本×系数=报价 的任何写法**；"
            "④成交价公开的同类项目（同形态最相近）报价作为基准价主锚点，行情库 get_history_price 中位辅助校验；"
            "⑤**本轮必须重新生成反算表与测算说明**——沿用上一轮的数字或文本（旧说明含「上修」字样的一律作废重写）。"
            "报价单.xlsx/响应函及报价汇总表/报价明细表三处报价数字必须勾稽一致。"
            "测算表加多轮报价不得上涨校验（后轮≤前轮）与异常低价预警（低于限价/成本比例标出并写明依据）；"
            "逐行税额=不含税×税率四舍五入、尾差在合计行倒算统一。"
            "**统一社会信用代码校验**：全包内统一社会信用代码必须与营业执照原文一致且为 18 位（数字+字母），"
            "verify/fill 后自查一遍位数，17 位=OCR 漏位必须回修。"
            "**编号逐字核对（所有编号类字段通用）**：信用代码/营业执照注册号/证书编号/采购编号/合同编号等，"
            "落盘前用 execute_code 把交付件里的编号与来源原文**并排 diff**（长度+字符逐一比对），"
            "任何长度或字符差异必须回修；vision 读出的编号必须与资料库文本源/可复核文本核对一致才可用，"
            "不允许把 vision 单源读出的编号直接写进交付件。"
            "**diff 必须对最终 zip 内的文件当场执行**（重新解包每个 docx 提取编号再比对），"
            "复用上一轮产物也必须重跑，禁止引用历史轮次的 diff 结论。"
            "**vision 误读的编号变体禁止以任何形式进入交付件**——包括证书/表格正文，也包括说明性文字里的"
            "「曾误读为 xxx」复述；统一社会信用代码 91 开头必须 18 位，出现 17 位变体=判不过（打包服务端会拒绝）。"
            "**已删除的错误内容不得在修复记录/编制记录里复述具体编号**（如混装的第三方专利号、误读变体）——"
            "说明修复时写类别与数量（如「删除 1 组非本企业专利证据」），不写具体号码。"
            "资料库编号若带 numbers_verified/numbers_pass1/numbers_conflict（入库时二次识别比对结果）："
            "优先采用 numbers_verified；numbers_conflict 列出的两轮不一致条目必须并列两个候选"
            "并对照扫描件原文再定，不得任选其一直接入件（打包服务端会拒绝含 conflict 值未处理的包）。"
            "**写作前范式内化（以本项目为主、为准）**：真实中标标书全文一般不公开（公开的多为成交结果公示与写作规范/模板/教程）。"
            "写作子 agent 动笔前：①先吃透本项目评分标准+技术规范书+《响应文件格式》模板，以此为纲；"
            "②再检索公开范式（search_web 查同行业技术响应写法/章节展开惯例/图表表格用法/国网及电力行业投标文件编制规范），"
            "搜到的范文、教程只作表达与结构参考——与本项目文件冲突处一律以本项目文件为准；搜不到全文范本不影响动笔。"
            "若上一轮方案性条目深度不足（只有目录没有实质正文、或声称的内容实际不存在）："
            "用 search_web_minimax/search_web 检索行业标准、同类项目做法与专业规范，"
            "把技术方案/专项响应深化到「可以直接拿去投标」的程度（技术规范书的每项技术要求"
            "都有实质性响应、正文实际写在文件里，篇幅以内容需要为准，不凑字数），"
            "再重新 append→verify→seal→package_response_zip。"
            "回执前逐份 inspect_agent_artifact 核对 pending_items 清单：分标/包信息从报价单/采购文件提取回填"
            "进 fields.values；电话/地址/邮编/法人等从 search_assets 企业资料回填进 fields.values；"
            "pending_items/remaining_blanks 里的【待补充】是「还没填完」的信号——逐项清零："
            "能填的填实（被授权人姓名从企业资料法定代表人/授权代表事实填实、不含税单价按含税价÷(1+税率) 倒算等），"
            "不能填的写「无/不适用」，确需客户实体动作的成文处留白并在总结列动作清单；"
            "裸【待补充】与「具体标签」字样=判不过。"
            "含模板表格的条目（业绩表/人员表/报价表等）按 slice 回执的 tables 清册用 table_fills **逐行填表**"
            "（填表头之外的数据行），空表+文末挂文字代替=不合格；**不得另起与模板原表重复的新表**——"
            "数据必须填进模板自身的表格（专项响应文件的模板业绩表必须用 table_fills 填其数据行），"
            "修完重新 seal→package 再回执。"
            "**表格与字体硬纪律（验收逐项核对，违规=判不过回修）**："
            "①任何需要表格的条目必须用**真实 Word 表格（w:tbl）**成文——禁止用 | 竖线字符在正文里模拟表格；"
            "模板无预置表格的条目（如报价明细表）在响应内容里新建带框线真表（表头+数据行+合计行），"
            "验收时解包计数 w:tbl 与应含表格的条目一致；"
            "②字体纪律（按模板原字体，不强制统一到某一款）：中文 run 必须显式设置 eastAsia 中文字体"
            "（宋体/仿宋/黑体/楷体等，与模板一致），西文与数字用 Times New Roman 或模板原字体——"
            "**禁止用 Calibri/Consolas/Arial 等西文字体充当中文 eastAsia、禁止中文 run 不设 eastAsia**"
            "（Windows 会渲染错乱）；同一文件字号保持统一（正文小四/表格五号，按模板）；"
            "验收时解包检查 rFonts 与 sz 的分布（中文 run 缺 eastAsia 或误用西文字体=判不过），"
            "render_qa_docx 渲染后用 vision 抽查字体观感。"
            "搜索全领域开放：search_web_minimax（MiniMax 原生）/ search_web / Hermes web 均可搜"
            "（标的信息、企业公开信息、行业技术方案、商务写作范例、政策标准），来源经 save_source 入库 + link_citation 绑定。"
            "成文要点：fill_template_slice 的 fields——buyer/project_name/tender_no/supplier 四个便捷键照填"
            "（supplier 取企业资料库 supplier_name 事实，search_assets 可查；没有则留空、成文处按模板留白）；"
            "其余任何模板词（法人/地址/电话/邮编/分标/包/自定义标签等）用 fields.values={模板词:{value,source}} "
            "通用词表填实，值从哪取由你决定（资料库/采购文件/搜索/推断）；"
            "放不进模板空位的事实（业绩/人员明细等）才放进 append 的撰写内容；"
            "封存前逐份 verify_template_slice（verify 之后又 fill/append 改动过，必须重验）；"
            "封版前对正式上传的每份 docx 用 render_qa_docx 逐页渲染检查（空白页必须回修重渲，"
            "表格跨页/图片方向/断页用 vision 抽查 PNG 页）。"
            "打包前必须核对 get_template_outline 里 is_file_item=true 的全部条目都已 seal_template_item 并"
            "已包含在 artifact_ids 中，缺失的先补齐 slice→fill→append→verify→seal 再 package_response_zip。"
            "**打包内容硬验收门（最终 zip 必须含全部 12 个条目，验收时 zipfile 列目录逐项核对，缺任一=判不过回修）**："
            "①9 份 is_file_item 正式文件（商务 4+技术 2+价格 3）；"
            "②内部管理文件/报价测算工作簿.xlsx——三步反算表（样本中位→基准价→最优报价）+成本科目底线校验+行情样本清单+规则面；"
            "③内部管理文件/报价测算说明.docx（Word 段落式详细说明：报价目标与公式参数→行情样本逐条与规模修正→"
            "基准价与最优报价反算过程→成本底线校验结论→多轮报价校验→承诺与风险提示；反算表数字与测算工作簿一致）；"
            "④内部管理文件/编制逻辑与评分响应记录.docx——评分细则逐条+各项得分+评分装订矩阵（评分点|分值|证明材料|模板位置|"
            "预计得分|状态）+报价三处勾稽声明。"
            "另外：价格文件/报价单.xlsx 必须含两个 sheet——「报价明细表」（序号/服务名称/服务内容/单位/数量/含税单价/税率/"
            "含税合价+合计行）与「说明」（仅一行简短口径：最高限价有无/税率/总价承包/三表勾稽一致声明——"
            "详细解释一律写进内部管理文件/报价测算说明.docx，不在 xlsx 里堆长文）。"
            "打包前由验收子 agent 以「能不能直接投标」为目标自行核验（可用信号：matched_title/req_title/"
            "was_verified、package 回执的 audit 清单、inspect_agent_artifact 打开每份文件看实际内容），"
            "发现任何妨碍直接投标的问题就回修、重新封存打包，直到验收子 agent 确认达标。"
            "评分步骤：submit_score_items 成功落分即算该步闭环，自动评分分数仅作记录与风险提示，不作验收门。"
            "收尾前用 report_customer_actions 工具上报「提交前客户动作清单」（只能客户实体完成的事，"
            "每行一条、写明动作+位置，如「盖章：授权委托书第X页加盖公章」；服务端记录并呈现给客户；"
            "没有任何客户动作就不调用）。"
            f"任务 id={task.id}。全部完成后，最后一行单独输出 {MARK_COMPLETE}；"
            f"若确有无法闭环项，最后一行输出 {MARK_INCOMPLETE} 并说明原因。"
        )
    else:
        prompt = (
            (("（会话历史中有客户在任务开始前的交代，先快速回顾一遍再按任务书执行。）" if pre_chat else ""))
            + f"请为项目 {task.project_id} 执行投标工作台端到端流程：解析→撰写→校验→评审→交付。"
            "流程与守则见预载 skill（bidvolt-agent-pipeline）；用 todo 列计划，"
            "用 delegate_task 派子任务（子 agent 结果会自动回到本会话，派完继续推进，不要停下来等），"
            "验收不通过带报告修复，全部满足后再输出。"
            "写作要求（客户要的是能直接投标的交付件）：技术方案/专项响应等方案性条目必须写出完整专业的正文"
            "（项目理解→总体方案/架构→分项技术方案→实施组织与进度→质量/安全/进度保障→售后培训），"
            "方案性内容大胆写、事实性数据守据——企业事实用 search_assets，缺数按成品纪律三级处置（填实/无·不适用/客户动作清单）不编造。"
            "方案正文必须实际写在文件里且完整——只有目录没有实质正文=不合格；"
            "用「详见附件X/第X章」交叉引用是正常投标写法，但验收时必须核实所指内容真实存在且有实质内容。"
            "**方案深度量化纪律（防「能交但薄」，验收逐节核）**：技术专项响应文件按**本项目技术规范书与评分标准的章节结构**成文，"
            "缺哪节补哪节、短哪节扩哪节——总体方案：总体设计思路→总体架构分层说明（每层一段实质描述+关键清单表）→"
            "核心技术与实现方案（适配范围/实现细节/性能保障）→可靠性/保障方案；"
            "分项技术方案按技术规范书/评分标准中的每个评分对象/分项成文"
            "（现状痛点→本分项内容与范围→技术路线分步→关键参数与指标→风险与对策→验收标准），"
            "每分项正文不少于 500 字，另配作业卡（步骤/保障措施/工时/验收记录）；"
            "实施组织与进度：组织架构与职责分工表（岗位/人数/职责/资质要求）→进度计划编制说明（分阶段工期分解）→"
            "里程碑与交付物表→进度网络图说明与关键路径→进度纠偏措施；质量安全进度保障：三级质检流程说明→"
            "检验批划分与验收标准表→安全技术交底制度→安全文明施工措施清单（不少于 10 项）→"
            "风险矩阵表（风险/等级/措施/责任人）；售后培训：质保期服务承诺明细表→培训计划表（批次/对象/内容/学时/考核方式）→"
            "分级响应流程长文（响应时限/备件保障/远程支持/定期巡检）→回访与满意度管理。"
            "**自绘图配额**：每个分项至少 1 张流程图；总体方案 2-3 张（架构图/数据流图）；"
            "另配关键参数分解图、实施时序图各 1 张——图由 Hermes 自绘（matplotlib/mermaid，"
            "中文渲染自查：文字清晰、无箭头重叠），插图规范≤16×23.2cm 等比例不撑页。"
            "**深度硬验收门（打包前用 execute_code 逐项计数，任一项不达标=验收判不过，必须回修重验直到全过）**："
            "技术专项响应文件：①正文字数（解包 word/document.xml 剥离标签后计数）**不少于 100,000 字**；"
            "②大纲条目**不少于 600 条**（业绩每组一节、人员每人一节、资质每项一节——"
            "各组各人各证都要有自己的标题条目，不是合并成一段）；"
            "③media 图片**不少于 400 张**（证据扫描件+自绘图）；④表格**不少于 50 个**。"
            "商务补充文件：正文字数**不少于 35,000 字**、media 图片**不少于 300 张**（审计/信用报告整本装订，一页不落）。"
            "计数脚本示例：zipfile 解包 docx → re.findall(r'<w:p[ >].*?</w:p>', xml, re.S) 数段落并剥离标签数字数、"
            "len(re.findall(r'<w:tbl>', xml)) 数表、docProps/app.xml 或 media 目录数图。"
            "低于门槛就把【差距清单】交给「技术写作」子 agent 扩写（每章字数配额：项目理解≥8,000、总体方案≥12,000、"
            "分项方案每分项≥3,000×全部分项、实施组织与进度≥8,000、质量安全进度保障≥6,000、售后培训≥6,000、"
            "评分装订矩阵支撑章节≥50,000）→重新 seal→重新 package→重新计数，直到全部达标才允许输出完成标记。"
            "这组门槛就是「一次性跑出金标准厚度」的验收定义，不得自行降级。"
            "**证据附件纪律**：交付件中的事实条目（资质/证书/业绩/人员/财务/信用），企业资料库有对应扫描件的，"
            "交付件必须携带证据——用整文件直写通道把扫描件实际插入对应位置，"
            "或在条目旁列『附件N：原件在库（扫描件随附）』清单；只写事实不留证据=不合格。"
            "**评分点逐点核验（验收标准）**：对照本项目技术/商务评分标准逐点评分点，"
            "每个评分点都要在交付件中找到实质响应内容与对应证据附件；只有空话没有可核对内容=判不过列清单。"
            "**评分装订矩阵（动笔前必建）**：把评分标准拆成矩阵，每行一个评分点："
            "「评分点|分值|证明材料|模板位置|预计得分|状态」——按矩阵逐行装订证据，每个评分点都要有实质正文+对应证据附件；"
            "只写方案不装证据=漏分（材料在库却漏用、技术卷厚度不足即源于此；装订时逐件核验证据的主体/有效期/关联性，"
            "人工成稿常见的其他主体材料、过期证书、重复业绩不得装订）。"
            "**证据成文形态**：模板固定表按原表容量填报（行数不变），超出容量的业绩/人员/资质在评分支撑章节"
            "**逐组逐人成节**——每组业绩一节（业绩说明+合同封面/关键页/签章页/发票/查验截图整组扫描件实际插入）、"
            "每名人员一节（简历+学历职称证件+社保截图实际插入）、每项资质一节（证书扫描件实际插入）；"
            "金标准目录几百条=业绩组数×证据页数+人员数×证件页数+页码索引，照此形态成文目录自然对齐。"
            "**证据装订可派专职子 agent（独立轮次预算）**：插图重活交给「证据装订」子 agent"
            "（任务书：评分矩阵+资产清单+目标章节+插图规范≤16×23.2cm 等比例不撑页；"
            "用 download_project_material/fitz 提取扫描件+python-docx 插入+覆盖上传+render_qa_docx 复验）；"
            "证据=实际插入的图，「原件在库」清单只在硬阻塞时降级；验收时核对矩阵行与图数对齐，缺图判不过。"
            "**证据页数量化（照金标准页数口径）**：矩阵每行证据写明装订页数——审计/信用报告**整本装订**（三年审计 100+ 页一页不落），"
            "业绩每组=合同全页+发票+查验截图**整组**（不止封面签章页），人员=简历+全部证件+社保，证书=证书页全文；"
            "目录条目/图数/字数的差距都源于证据页数——验收核对每行图数达到写明的页数下限。"
            "**投标有效性核验（先于撰写）**：A 阶段先核验——①响应截止时间 vs 今天（search_web 查本项目是否已出预成交/成交公示、"
            "有无延期/重招，截止已过列入风险清单）；②采购文件「不接受委托中介机构编制」类条款下编制方与投标单位关系；"
            "③公告与技术规范书之间的地点/范围/数量矛盾清单；④最高限价/保证金/税率/多轮报价规则。"
            "**提问关（开编前问）**：A 分析后、开编前，把决策类问题用 ask_customer 一次批量问完"
            "（截止/延期、编制方与投标单位关系、业绩人员齐备、报价策略（争分优先/保毛利优先/无偏好）、合同条款接受度、地点/范围矛盾），"
            "答案回来再开编相应部分；不依赖答案的部分并行推进。"
            "**成品纪律（全面取消修订/批注与待补标签）**：交付件=Hermes 自己写完的终稿——"
            "成文直接干净写入正文（无修订模式、无 Word 批注），不留【待补充】标签、不留"
            "「建议值/请客户确认」元语言。每条信息三级处置：①能取得的填实（采购文件/企业资料/公开搜索/合理推断，"
            "推断值写中性事实）；②按采购文件口径本就不需要的填「无（…）/不适用」（如「无：自成立以来未发生名称变更」、"
            "「本项目免收响应保证金，本表不适用」），空表在标题旁注「本表空白=无偏差/不适用」；"
            "③确需客户实体动作的（签字/盖章/签署日期/提交证照原件）成文处按模板留白（不写标签），"
            "收尾前用 report_customer_actions 工具上报提交前客户动作清单。fill/verify 回执 remaining_blanks 里的【待补充】是"
            "「还没填完」的信号，逐项清零（填实或写无/不适用），裸【待补充】与「具体标签」字样=判不过。"
            "**深度展开方法**：方案性正文的产出载体是**文件**，不是对话回复——写作子 agent 用 write_file 分段/"
            "terminal 追加/python-docx 直写交付文件，一次写不完就继续写，写到深度达标为止（子 agent 也持整文件直写通道）。"
            "「写到什么程度」由技术规范书逐条要求+评分标准定：每条技术要求都要有实质性响应正文，"
            "禁止以「太长/一次写不下」为由只写目录或截断。图由 Hermes 自绘（mermaid/matplotlib 架构图/数据流图/拓扑图/甘特图插入交付文件）。"
            "**技术卷正文必须派专职「技术写作」子 agent（独立轮次预算）**：技术专项响应文件的正文不得由主会话顺手写——"
            "开编后立刻 delegate_task 派专职写作子 agent，任务书逐条写明（缺哪条判不过）："
            "①总体方案：总体设计思路+总体架构分层说明（按本项目技术规范书的架构描述分层，每层一段实质描述）+关键设备/系统清单表+"
            "核心技术与实现方案（适配范围/实现细节/性能保障，以技术规范书要求为准）+可靠性/保障方案；"
            "②分项技术方案按技术规范书/评分标准中的**每个评分对象/分项**逐项成文（现状痛点→本分项内容与范围→技术路线分步→"
            "关键参数与指标→风险与对策→验收标准），每分项正文不少于 500 字，另配作业卡（步骤/保障措施/工时/验收记录）；"
            "③实施组织与进度：职责分工表+进度计划编制说明+里程碑与交付物表+进度网络图说明与关键路径+进度纠偏措施；"
            "④质量安全进度保障：三级质检流程说明+检验批划分与验收标准表+安全技术交底制度+安全文明施工措施清单（≥10项）+风险矩阵表；"
            "⑤售后培训：质保期服务承诺明细表+培训计划表+分级响应流程长文+回访与满意度管理；"
            "⑥自绘图配额：每个分项至少 1 张流程图+总体方案 2-3 张（架构图/数据流图）+关键参数分解图、实施时序图，中文渲染自查。"
            "子 agent 回执后主会话逐章核查（章节齐全/字数达标/图已插入），不足的回修补足再进校验阶段——"
            "正文深度是验收硬指标，不达标不回执。"
            "公开语料没有金标准全文不是障碍：深度来源=技术规范书逐条要求+行业标准/白皮书/论文/同类项目公开资料+企业自身资料，"
            "搜索只作补充参考，以本项目文件为主为准。"
            "**向客户提问（ask_customer 工具，提问权+提问纪律）**：你可以向客户提问，但**先尽己力**——调用前必须完成三查"
            "（①企业资料库 search_assets+vision 读图取证 ②采购文件与技术规范书原件 ③公开搜索+按采购文件口径合理推断），"
            "三查都拿不到、且属「只有客户本人能提供/只能客户实体动作」的信息（如银行账户、真实签署人身份、证照原件）才允许问。"
            "禁止问：资料库里有的、公开可查的、可推断的、可写「无/不适用」的、不影响直接投标的。"
            "用 ask_customer 工具**批量**问（一次列完所有问题，不一条一条问），每条带 q/need/checked"
            "（问题/为什么需要/已自查说明——问得傻会被自己的 checked 暴露）；提问由主会话发起，"
            "子 agent 发现客户独占信息在回执里列给主会话，主会话统一批量 ask_customer。"
            "工具立即返回（ask_id），客户回答后回答会自动回到本会话；提问后不干等："
            "继续推进不依赖答案的工作；答案回来再回填相关字段并重验；"
            "收口时未答复项按三级处置（能推断的填实/写「无/不适用」/用 report_customer_actions 上报实体动作）。"
            f"**提问关问答窗口**：每条提问有 {ASK_WINDOW_MINUTES} 分钟问答窗口，超时未答系统会注入"
            "「问答窗口已过，由你自行决定」信号——收到即不再等待，按三级处置纪律自主拍板"
            "（材料类问题以企业资料库事实为准，能装订的全部装订整本/整组，不保守跳过）；"
            "客户之后补答会自动注入本会话，届时回填并重验。"
            "**报价依据必附（依据随项目而定，缺依据不出数字）**：测算出的每个报价数字必须附带书面依据，且依据写进交付件本身"
            "（报价单「备注/说明」格或价格文件「报价测算说明」页）。依据的**具体构成随本项目而定，由你按实际确定，不照清单硬套**："
            "成本法按本项目真实成本科目（人力/设备/差旅/管理/税费——有什么算什么，没有的不硬凑）；"
            "行情样本列真实找到的同类成交/报价（项目名+金额+日期+公示来源，get_history_price 行情库或 search_web 公开中标价）；"
            "规则面只写本项目真实存在的（限价有无、税率、付款口径、含代理费——无最高限价就写「本项目无最高限价」）。"
            "原则=依据真实、可复核、随件走；只写一句「处于行情区间内」没有任何明细=判不过。"
            "报价即最终报价：不写「建议报价/请客户确认调整」元语言。"
            "**报价目标=得分最优（硬纪律）**：按区间平均价浮动法反算——①从采购文件取公式参数（n1/n2/W1/W2/c）"
            "与最高限价（无最高限价就写明「本项目无最高限价」）；②基准价=历史同类成交中位×规模修正与本次行情样本"
            "（get_history_price/search_web 真实成交）的中位锚点；③最优报价≈基准价（按公式扣分斜率微调），"
            "落在异常低价红线与限价之间；**禁止成本加成定价**——成本科目只作底线校验与随件依据，不是定价公式"
            "（成本测算高于基准价时说明依据并仍按得分最优报）；客户回答「无偏好」=按本条执行。"
            "**报价反算硬验收门（测算工作簿必含、验收逐项核对，缺一项=判不过回修）**："
            "①三步反算表（锚点→基准价→最优报价，每步写数字+依据），报价单最终数字必须**等于**反算表结果；"
            "②**锚点选取纪律（限价优先）**：**本项目有最高限价的 → 锚点=最高限价原值**（限价是采购文件给定的"
            "唯一可靠基准参照，样本口径混杂时一律以限价为锚）；**无最高限价的 → 锚点=同形态最相近样本原值**"
            "（场景一致→口径一致→形态一致；形态相近的候选**金额不参与选择**，取与本项目范围最接近者）；"
            "锚点一经选定不得更换（反算表全文只允许一个锚点），写锚点来源与依据；"
            "禁止用「估计的有效报价均值 A2」或任何估算值充当锚点；禁止「上修」字样（出现=判不过）；"
            "最优报价=锚点×(1-C)=锚点×0.98 附近按扣分斜率微调后取整到万，**报价不得高于锚点原值**；"
            "③**成本底线纪律**：成本科目按本项目真实构成逐项测算——人力=人数×驻场/服务天数×人日单价"
            "（市场行情 1500-2500 元/人日，超出须注明依据），差旅/设备/管理费列实际口径；"
            "成本合计**不得高于锚点金额**——成本超过锚点=科目虚高（重复计费/单价离谱），"
            "必须复核削减；成本只作底线校验，**禁止 成本×系数=报价 的任何写法**；"
            "④成交价公开的同类项目（同形态最相近）报价作为基准价主锚点，行情库 get_history_price 中位辅助校验；"
            "⑤**本轮必须重新生成反算表与测算说明**——沿用上一轮的数字或文本（旧说明含「上修」字样的一律作废重写）。"
            "报价单.xlsx/响应函及报价汇总表/报价明细表三处报价数字必须勾稽一致。"
            "测算表加多轮报价不得上涨校验（后轮≤前轮）与异常低价预警（低于限价/成本比例标出并写明依据）；"
            "逐行税额=不含税×税率四舍五入、尾差在合计行倒算统一。"
            "**统一社会信用代码校验**：全包内统一社会信用代码必须与营业执照原文一致且为 18 位（数字+字母），"
            "verify/fill 后自查一遍位数，17 位=OCR 漏位必须回修。"
            "**编号逐字核对（所有编号类字段通用）**：信用代码/营业执照注册号/证书编号/采购编号/合同编号等，"
            "落盘前用 execute_code 把交付件里的编号与来源原文**并排 diff**（长度+字符逐一比对），"
            "任何长度或字符差异必须回修；vision 读出的编号必须与资料库文本源/可复核文本核对一致才可用，"
            "不允许把 vision 单源读出的编号直接写进交付件。"
            "**diff 必须对最终 zip 内的文件当场执行**（重新解包每个 docx 提取编号再比对），"
            "复用上一轮产物也必须重跑，禁止引用历史轮次的 diff 结论。"
            "**vision 误读的编号变体禁止以任何形式进入交付件**——包括证书/表格正文，也包括说明性文字里的"
            "「曾误读为 xxx」复述；统一社会信用代码 91 开头必须 18 位，出现 17 位变体=判不过（打包服务端会拒绝）。"
            "**已删除的错误内容不得在修复记录/编制记录里复述具体编号**（如混装的第三方专利号、误读变体）——"
            "说明修复时写类别与数量（如「删除 1 组非本企业专利证据」），不写具体号码。"
            "资料库编号若带 numbers_verified/numbers_pass1/numbers_conflict（入库时二次识别比对结果）："
            "优先采用 numbers_verified；numbers_conflict 列出的两轮不一致条目必须并列两个候选"
            "并对照扫描件原文再定，不得任选其一直接入件（打包服务端会拒绝含 conflict 值未处理的包）。"
            "**写作前范式内化（以本项目为主、为准）**：真实中标标书全文一般不公开（公开的多为成交结果公示与写作规范/模板/教程）。"
            "写作子 agent 动笔前：①先吃透本项目评分标准+技术规范书+《响应文件格式》模板，以此为纲；"
            "②再检索公开范式（search_web 查同行业技术响应写法/章节展开惯例/图表表格用法/国网及电力行业投标文件编制规范），"
            "搜到的范文、教程只作表达与结构参考——与本项目文件冲突处一律以本项目文件为准；搜不到全文范本不影响动笔。"
            "搜索全领域开放：search_web_minimax（MiniMax 原生）/ search_web（AnySearch）/ Hermes web 均可搜"
            "——标的信息、企业公开信息、行业技术方案做法、商务标写作范例、政策法规标准；"
            "重要来源 save_source 入库 + link_citation 绑定，与企业资料库冲突时以资料库为准。"
            "成文要求：get_template_outline 里 is_file_item=true 的全部条目都必须"
            "slice→fill→append→verify→seal 后一起 package_response_zip；"
            "build_quote_xlsx 的 sheets[].rows 必须是数组的数组（每行一个数组，不得用对象/带 item 键）。"
            "填空机制边界：fill 的 fields.values 只命中「标签：空位/下划线」形态；无标签下划线"
            "（如「特授权____」）values 打不到——这类空位按成品纪律三级处置：能填的用 fills 定向替换为实值，"
            "不能填的写「无/不适用」，确需客户动作的留白（不留裸【待补充】）。"
            "商务/技术偏差表若应答无偏差：必须在表格标题旁或表内首行显式标注"
            "「本表空白=无偏差（按采购文件约定，选择无偏差时无需填写本表）」，不得只留空表。"
            "封存前逐份 verify_template_slice（verify 之后又 fill/append 改动过，必须重验）；"
            "封版前对正式上传的每份 docx 用 render_qa_docx 逐页渲染检查（空白页必须回修重渲，"
            "表格跨页/图片方向/断页用 vision 抽查 PNG 页）。"
            "交付前由验收子 agent 以「能不能直接投标」为目标自行核验（可用信号：matched_title/req_title/"
            "was_verified、package 回执的 audit 清单、inspect_agent_artifact 打开每份文件看实际内容），"
            "发现任何妨碍直接投标的问题就回修、重新封存打包，直到验收子 agent 确认达标。"
            "评分步骤：submit_score_items 成功落分即算该步闭环，自动评分分数仅作记录不作验收门。"
            "收尾前用 report_customer_actions 工具上报「提交前客户动作清单」（只能客户实体完成的事，"
            "每行一条、写明动作+位置，如「盖章：授权委托书第X页加盖公章」；服务端记录并呈现给客户；"
            "没有任何客户动作就不调用）。"
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
    await _append_events_fresh(task, seq, [("service", prompt)])
    # 独立锁链破坏器（R9 2057 冻死根因防线）：主循环一旦冻在等锁上也能
    # 继续杀持锁悬挂事务。任务离开 RUNNING 后自退（见 _lock_breaker_loop）。
    asyncio.create_task(_lock_breaker_loop(task.id))

    loop = asyncio.get_running_loop()
    started_at = loop.time()

    base_args = [
        "chat", "--cli",
        "-t", _HERMES_TOOLSETS,
        "-s", "bidvolt-agent-pipeline",
        # 全能力开放：跳过危险命令确认与 shell 钩子确认（自主批量运行无人可点确认，
        # 留着确认流程=终端能力形同虚设；交付件合规性由主会话+验收/评审子 agent 保证）
        "--yolo", "--accept-hooks",
        "--no-restore-cwd", "--max-turns", "360",
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
        ask_timeout_at = 0.0
        progress_at = 0.0
        watchdog_at = 0.0
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
                    if not ln or _is_pump_noise(ln) or ln == last_line:
                        continue
                    # 我方提交在 REPL 里的回显（含提示符前缀）不作为主会话输出
                    t = ln
                    for p in ("❯", ">", "›"):
                        if t.startswith(p):
                            t = t[len(p):].strip()
                            break
                    if ln in echo_texts or t in echo_texts:
                        continue
                    # 终端按列硬折行时，我方提交文本的回显会以「子串行」形式回流
                    # （长提示词/催办/复核提示被折行，恰好折在完成标记前就形成
                    # 以【PIPELINE_COMPLETE】开头的假回执行）——按子串过滤；
                    # 真实回执行（完整标记，或 INCOMPLETE+原因）不在此列，永不丢弃
                    if (
                        ln not in (MARK_COMPLETE, MARK_INCOMPLETE)
                        and not ln.startswith(MARK_INCOMPLETE)
                        and any(ln in e or t in e for e in echo_texts)
                    ):
                        continue
                    last_line = ln
                    pending.append(("hermes", ln))
            except Exception:  # noqa: BLE001 回调内异常不得吞掉事件流
                logger.exception("主会话输出解析失败（task=%s）", task.id)

        loop.add_reader(master_fd, _on_readable)

        async def _flush() -> None:
            if pending:
                batch, pending[:] = list(pending), []
                # 事件冲刷用独立短命会话（R9 架构级根治）：主会话历史累积
                # 后 commit 悬挂是 R7/R8/R9 五次整轮卡死的唯一根因，而
                # 新鲜会话（排水/超时/进度/终态）数百次提交从未挂过。
                # with 自带回滚，task 行锁绝不滞留。
                # 超时兜底：即使冲刷本身被锁链卡住，循环也必须继续走到
                # 锁链破坏器——超时后放弃本次冲刷（batch 丢回 pending 下轮重试）。
                from app.db import SessionLocal as _PumpSessionLocal  # noqa: PLC0415

                try:
                    async with _PumpSessionLocal() as _sf:
                        await asyncio.wait_for(_append_events(_sf, task, seq, batch), timeout=30)
                except asyncio.TimeoutError:  # noqa: UP041 服务器 Python 3.10
                    pending[:0] = batch  # 未确认写入：放回队首下轮重试
                    logger.warning("事件冲刷超时（task=%s），batch 放回重试", task.id)
                    raise

        try:
            while True:
                await asyncio.sleep(EVENT_FLUSH_SECONDS)
                # 事件写入自身含 RLS 重设；任何瞬时 DB 故障都不得杀死泵循环
                # （批量留在 pending，下轮重试）——历史上 INSERT 撞 RLS 曾把整轮打死
                try:
                    await _flush()
                except Exception:  # noqa: BLE001
                    logger.warning("事件冲刷暂未成功（task=%s），下轮重试", task.id, exc_info=True)
                    # 会话自愈（R7/R8 四次整轮卡死根因之一）：异常后事务可能悬挂，
                    # 必须显式 rollback 重置——毒事务不 rollback 会永久持有行锁
                    try:
                        await session.rollback()
                    except Exception:  # noqa: BLE001
                        pass
                # 客户中途对话：复用现有泵循环——取出任务字段里的排队消息，
                # 经与催办/复核提示相同的 PTY 通道注入（hermes CLI 的 /queue 在忙时排队，
                # 主会话当前轮结束后按到达顺序逐条处理）。行锁防 app/worker 双进程丢失更新；
                # 失败下轮再试，不打断主流程。
                # R7 线上定位：本块必须用【独立短命会话】——主泵会话历史上一次 commit
                # 失败后不 rollback，事务悬挂持有 task 行锁，后续 FOR UPDATE 全部等锁
                # （曾致客户回答 40 分钟投不出去）。独立会话自带 with 回滚，不留锁。
                try:
                    from sqlalchemy import select as sa_select

                    from app.db import SessionLocal as _PumpSessionLocal  # noqa: PLC0415
                    from app.services.task_service import _set_rls_context  # noqa: PLC0415

                    async with _PumpSessionLocal() as _s2:
                        await _set_rls_context(_s2, task.enterprise_id)
                        row = (
                            await asyncio.wait_for(
                                _s2.execute(
                                    sa_select(Task).where(Task.id == task.id).with_for_update()
                                ),
                                timeout=30,
                            )
                        ).scalar_one()
                        pending_chat = list((row.payload or {}).get("pending_chat") or [])
                        if pending_chat:
                            if not sent_first:
                                # REPL 尚未就绪（interrupt 模式下就绪前的 PTY 提交会被丢弃，
                                # R7 线上实证）：只释放行锁不取走，等首条消息发出后再投递
                                await _s2.commit()
                                continue
                            row.payload = {**(row.payload or {}), "pending_chat": []}
                            await _s2.commit()
                            for msg in pending_chat:
                                # 排队（/queue，下一轮处理）或插话（/steer，下一个工具调用后注入改方向）
                                if isinstance(msg, dict):
                                    _text = str(msg.get("text") or "")
                                    _cmd = "/steer" if msg.get("mode") == "steer" else "/queue"
                                else:
                                    _text = str(msg)
                                    _cmd = "/queue"
                                _submit(_cmd + " " + " ".join(_text.split()))
                except Exception:  # noqa: BLE001 瞬时失败（事务/锁）下轮再取
                    logger.warning("中途对话注入暂未成功（task=%s），下轮重试", task.id, exc_info=True)
                # 提问关问答窗口：超过窗口仍未回答的 ask 注入「已超时，由你自行决定」
                # 纯信号（原问题原文附上，不给任何默认答案——走向由主会话按三级处置
                # 纪律自己拍板）；一条 ask 只注入一次（timeout_notified 幂等）。
                # 问卡不消失：客户之后补答仍会走 queue_chat_message 回填。
                if loop.time() - ask_timeout_at > 60:
                    ask_timeout_at = loop.time()
                    try:
                        from sqlalchemy import select as _sa_sel

                        from app.db import SessionLocal as _PumpSessionLocal  # noqa: PLC0415
                        from app.models.agent import AgentCustomerAsk
                        from app.services.task_service import _set_rls_context  # noqa: PLC0415

                        # 与 pending_chat 排水同理：读与幂等标记用独立短命会话，
                        # 不依赖主泵会话的事务状态（R7 线上主会话事务悬挂曾卡死注入）
                        _now = datetime.now(timezone.utc)
                        _due: list[tuple[int, int, datetime, list]] = []  # (ask_id, window, created_at, items)
                        async with _PumpSessionLocal() as _s3:
                            await _set_rls_context(_s3, task.enterprise_id)
                            rows = (
                                await _s3.scalars(
                                    _sa_sel(AgentCustomerAsk)
                                    .where(
                                        AgentCustomerAsk.task_id == task.id,
                                        AgentCustomerAsk.kind == "question",
                                        AgentCustomerAsk.answered == 0,
                                        AgentCustomerAsk.timeout_notified == 0,
                                    )
                                )
                            ).all()
                            for a in rows:
                                _window = max(1, int(a.window_minutes or 20))
                                if _now < a.created_at + timedelta(minutes=_window):
                                    continue
                                _due.append((a.id, _window, a.created_at, list(a.items or [])))
                                a.timeout_notified = 1
                            await _s3.commit()
                        for _aid, _window, _created, _items in _due:
                            lines = ["- " + (it.get("q") if isinstance(it, dict) else str(it)).strip() for it in _items]
                            signal = (
                                f"（系统提示·问答窗口已过）提问 ask_id={_aid} 已等待超过 {_window} 分钟，"
                                "客户尚未回答。问卡仍可补答，补答会自动回到本会话。"
                                "不再等待：请按任务书三级处置纪律**由你自行决定**这些问题的走向——"
                                "能核实的核实、能推断的推断、该写「无/不适用」的照写；"
                                "材料类问题以企业资料库事实为准，能装订的全部装订（整本/整组），不保守跳过；"
                                "确需客户实体动作的收口时用 report_customer_actions 列出。原问题：\n"
                                + "\n".join(lines)
                            )
                            await _append_events_fresh(task, seq, [("service", signal)])
                            # _append_events 已 commit + 重设 RLS，无需再提交
                            # 关键：事件库只是控制台展示，主会话读不到——必须经 PTY 队列注入
                            _submit("/queue " + " ".join(signal.split()))
                            logger.info("提问关超时信号已注入 ask=%s（task=%s）", _aid, task.id)
                    except Exception:  # noqa: BLE001 瞬时失败下轮再查
                        logger.warning("提问关超时检查暂未成功（task=%s），下轮重试", task.id, exc_info=True)
                # 进度条（观感 + 页面监控信息量）：按真实里程碑推进 percent/current_work——
                # 提问关发起 15% → 成文产物落库 40-70% → 打包 85%，期间随耗时缓慢爬升
                if loop.time() - progress_at > 60:
                    progress_at = loop.time()
                    try:
                        from sqlalchemy import func as sa_func
                        from sqlalchemy import select as _sa_sel_p

                        from app.db import SessionLocal as _PumpSessionLocal  # noqa: PLC0415
                        from app.models.agent import AgentArtifact, AgentCustomerAsk
                        from app.services.task_service import _set_rls_context  # noqa: PLC0415

                        # 与 pending_chat/提问超时同理：独立短命会话——主泵会话事务
                        # 悬挂曾两次卡死整轮（R7 排水、R8 进度块 21:34 卡死事件）
                        _n_asks = _n_ans = _n_art = _n_zip = 0
                        async with _PumpSessionLocal() as _s4:
                            await _set_rls_context(_s4, task.enterprise_id)
                            _n_asks = int(
                                (await _s4.scalar(
                                    _sa_sel_p(sa_func.count(AgentCustomerAsk.id)).where(
                                        AgentCustomerAsk.task_id == task.id,
                                        AgentCustomerAsk.kind == "question",
                                    )
                                ))
                                or 0
                            )
                            _n_ans = int(
                                (await _s4.scalar(
                                    _sa_sel_p(sa_func.count(AgentCustomerAsk.id)).where(
                                        AgentCustomerAsk.task_id == task.id,
                                        AgentCustomerAsk.kind == "question",
                                        AgentCustomerAsk.answered == 1,
                                    )
                                ))
                                or 0
                            )
                            _n_art = int(
                                (await _s4.scalar(
                                    _sa_sel_p(sa_func.count(AgentArtifact.id)).where(AgentArtifact.task_id == task.id)
                                ))
                                or 0
                            )
                            _n_zip = int(
                                (await _s4.scalar(
                                    _sa_sel_p(sa_func.count(AgentArtifact.id)).where(
                                        AgentArtifact.task_id == task.id,
                                        AgentArtifact.kind == "zip",
                                    )
                                ))
                                or 0
                            )
                        _elapsed = int(loop.time() - started_at)
                        if _n_zip:
                            _pct, _work = 85, f"交付包已生成（第 {_n_zip} 版），主会话复核收尾中…"
                        elif _n_art:
                            _pct, _work = min(70, 40 + _n_art * 2), f"成文产物 {_n_art} 份落库：撰写/校验/评审推进中…"
                        elif _n_asks:
                            _pct, _work = 15, f"提问关已发起（{_n_ans}/{_n_asks} 组已答，超时未答主会话将自行决定），并行推进中…"
                        else:
                            _pct, _work = 5 + min(5, _elapsed // 300), "Agent 主会话启动（todo 计划 + 子任务编排，全程自主）…"
                        async with _PumpSessionLocal() as _s5:
                            await asyncio.wait_for(_set_rls_context(_s5, task.enterprise_id), timeout=30)
                            _row = (
                                await asyncio.wait_for(
                                    _s5.execute(
                                        _sa_sel_p(Task).where(Task.id == task.id).with_for_update()
                                    ),
                                    timeout=30,
                                )
                            ).scalar_one()
                            _cur = dict(_row.progress or {})
                            if _cur.get("percent") != _pct or _cur.get("current_work") != _work:
                                _row.progress = {
                                    "phase": "agent_pipeline",
                                    "status": "running",
                                    "percent": _pct,
                                    "current_work": _work,
                                }
                                await asyncio.wait_for(_s5.commit(), timeout=30)
                    except Exception:  # noqa: BLE001 进度纯观感，失败下轮再试
                        logger.warning("进度更新暂未成功（task=%s），下轮重试", task.id, exc_info=True)
                # 悬挂事务看门狗（R7/R8 四次整轮卡死的根本防线）：主泵会话 commit
                # 一旦悬挂，事务挂着 task 行锁，后续 FOR UPDATE 全部等锁形成死锁链。
                # 每 60s 用独立连接终止本库「idle in transaction 超 5 分钟」的其他
                # 后端——锁释放后本泵挂起操作自然完成或失败，循环自愈。
                if loop.time() - watchdog_at > 60:
                    watchdog_at = loop.time()
                    try:
                        from sqlalchemy import text as _sa_text

                        from app.db import SessionLocal as _PumpSessionLocal  # noqa: PLC0415

                        # 主会话事务卫生由「每轮迭代末 rollback」保证（见循环尾），
                        # 此处只需独立会话 + state_change 守卫：健康泵的连接事务
                        # 从不滞留（每轮 ≤5s 关闭），真悬挂的 commit 连接才会命中
                        # 并被杀（commit 失败→except→rollback→循环自愈）。
                        async with _PumpSessionLocal() as _s8:
                            rows = (
                                await _s8.execute(
                                    _sa_text(
                                        "SELECT pid, application_name, left(query, 80) FROM pg_stat_activity "
                                        "WHERE datname = current_database() "
                                        "AND state = 'idle in transaction' "
                                        "AND xact_start < now() - interval '5 minutes' "
                                        "AND state_change < now() - interval '5 minutes' "
                                        "AND pid <> pg_backend_pid()"
                                    )
                                )
                            ).fetchall()
                            # 观察记录（不杀）：详见下方精准破坏器
                            if rows:
                                logger.warning(
                                    "悬挂事务观察（task=%s）：%s",
                                    task.id,
                                    [(int(p), str(app), str(q)) for (p, app, q) in rows],
                                )
                            # 精准锁链破坏器（只杀真凶）：idle-in-txn 超 5 分钟
                            # 且持有 task 表锁的后端——这正是「冲刷事务悬挂持锁→
                            # 排水 FOR UPDATE 永久等待」死锁链的持有方。
                            # 无锁的遗留事务（如 app 的 set_config 余留）不杀：
                            # 它们不阻塞任何查询，杀掉反而引发 connection closed
                            # 崩溃（R9 六连崩教训）。
                            blocked = (
                                await _s8.execute(
                                    _sa_text(
                                        "SELECT DISTINCT l.pid FROM pg_locks l "
                                        "JOIN pg_stat_activity a ON a.pid = l.pid "
                                        "WHERE l.relation = 'task'::regclass "
                                        "AND l.granted "
                                        "AND a.state = 'idle in transaction' "
                                        "AND a.xact_start < now() - interval '3 minutes' "
                                        "AND a.pid <> pg_backend_pid()"
                                    )
                                )
                            ).fetchall()
                            for (pid,) in blocked:
                                try:
                                    await _s8.execute(_sa_text(f"SELECT pg_terminate_backend({int(pid)})"))
                                    logger.warning(
                                        "锁链破坏器终止持 task 锁的悬挂事务（task=%s）：%s",
                                        task.id, int(pid),
                                    )
                                except Exception:  # noqa: BLE001
                                    pass
                    except Exception:  # noqa: BLE001 看门狗自身故障不影响泵
                        pass
                # 注意：不在循环尾对主会话做 rollback——rollback 会过期 ORM 对象，
                # 下一次 task.id/enterprise_id 访问触发惰性加载 → MissingGreenlet
                # 崩溃（R9 2052 三连崩根因）。主会话事务卫生由各块自含性保证：
                # 写块全部 commit、读块/看门狗全走独立短命会话，主会话不留滞留事务。
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
                    # 完成标记以会话库导出为唯一判据（assistant 消息原文，我方提示词
                    # 注入不可能混入）。事件流兜底通道已从轮询中移除：终端折行会把
                    # 推理中提及标记/我方提示词回显折成「以标记开头」的假回执行，
                    # 曾两次误判提前终止健康会话——只在卡死出口处做精确整行兜底（见下）。
                    if round_marker:
                        break
                # 进程提前退出且没有待刷事件
                if proc.poll() is not None and not pending:
                    break
                # 卡顿催办：逐级加强，最后一级只索取结束标记
                if loop.time() - last_out_at > STALL_SECONDS:
                    if nudges >= NUDGE_LIMIT:
                        # 出口前兜底：事件流里已有主会话输出的结束标记（导出通道偶发失配时误催办）
                        try:
                            if await asyncio.wait_for(_marker_from_events(session, task), timeout=20) == MARK_COMPLETE:
                                round_marker = MARK_COMPLETE
                                break
                        except Exception:  # noqa: BLE001
                            pass
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
            # 冲刷失败不得炸穿轮次（R9 看门狗误杀连接后此处曾把整轮异常抛出，
            # 管线崩溃→reclaim 重跑×3）——失败仅记录，pending 遗留由下一轮/终态兜底
            try:
                await _flush()
            except Exception:  # noqa: BLE001
                logger.warning("轮次收尾冲刷失败（task=%s）", task.id, exc_info=True)
                try:
                    await session.rollback()
                except Exception:  # noqa: BLE001
                    pass
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
        await _append_events_fresh(
            task, seq,
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
            "不能填的写「无/不适用」，确需客户实体动作的成文处留白并在总结列动作清单；"
            "**裸【待补充】（pending_items 里 kind=bare，或 package 回执 audit.bare_pending 非空）必须清零**——"
            "逐处填实或写「无/不适用」，不留待补标签。"
            "同时核对：报价单已随件附测算依据（依据构成随项目而定——真实成本科目+真实找到的行情样本+本项目真实存在的规则面，"
            "缺依据必须补写）；收尾前已用 report_customer_actions 工具上报提交前客户动作清单（没有客户动作则说明未调用）。"
            "**深度与证据核对（防止「能交但薄」）**：①方案性条目（技术专项响应/商务补充等）正文必须是**实质内容**——"
            "项目理解/总体方案与架构/分项技术方案/实施组织与进度/质量安全进度保障/售后培训逐章有货，"
            "对照技术规范书逐条要求每条都有实质性响应正文，只有目录或空话=不足；"
            "②**深度硬验收门逐项计数**：用 execute_code 解包最终 docx 统计——技术专项响应文件正文字数≥100,000、"
            "大纲条目≥600、media 图片≥400、表格≥50；商务补充文件正文字数≥35,000、media 图片≥300。"
            "任一项低于门槛必须回修（把差距清单交技术写作子 agent 扩写对应章节→重新 seal→重新 package→重新计数），"
            "直到全部达标；"
            "③评分装订矩阵每行证据已**实际插入**交付文件（数文件 media 图数与矩阵行对齐：业绩/人员/资质/审计的"
            "扫描件页在文件里，不是只有文字叙述）；④报价反算硬验收门逐项核对：三步反算表（锚点→基准价→最优报价）"
            "齐全、报价单数字=反算表结果、**锚点=最高限价原值（本项目有最高限价时）/同形态最相近样本原值（无最高限价时）**"
            "（禁止换成其他样本或估算值、禁止估计 A2 充当锚点、禁止「上修」字样、"
            "报价=锚点×0.98 附近取整且不高于锚点原值、反算表全文只允许一个锚点）、"
            "成本按真实构成测算且不高于同形态样本金额（高于 70 万档=科目虚高须削减）、成本只出现在底线校验行"
            "（无成本加成写法）、"
            "报价依据三路（成本科目/行情样本/规则面）随件写明；⑤编号逐字 diff（对最终 zip 重新解包当场比对，"
            "禁止引用历史轮次结论）：信用代码/注册号/证书编号/采购编号"
            "与来源原文并排比对无任何长度或字符差异；⑥打包内容硬验收门：最终 zip 逐项核对 12 个条目齐全"
            "（9 份正式文件+内部管理文件/报价测算工作簿.xlsx+内部管理文件/报价测算说明.docx+"
            "内部管理文件/编制逻辑与评分响应记录.docx），报价单.xlsx 含「报价明细表」+「说明」两个 sheet，"
            "Word 说明与测算工作簿的反算数字一致；⑦表格与字体核对：报价明细表等应含表条目 w:tbl≥1 且为真表格"
            "（无竖线假表），中文 run 均设置了 eastAsia 中文字体（无缺省、无西文字体冒充），字号按模板统一。"
            "上述任何一项不足就回修（重写正文/补装证据/补依据，重新 fill/seal/package）后再输出结束标记；"
            "确认无误最后一行输出 " + MARK_COMPLETE + "；确有无法修复项输出 " + MARK_INCOMPLETE + " 原因…。"
        )
        await _append_events_fresh(
            task, seq,
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

    # 终态数据用局部变量构建，绝不直接赋值 ORM 对象（R9 2058 崩溃链根因：
    # task.result/progress 赋值会留在主会话事务里，下一次主会话查询触发
    # autoflush → 主会话在 2 小时长寿事务中持有 task 行锁 → 独立锁链破坏器
    # 按规则杀死主会话 → run_task 终态 commit 崩溃 → retry 耗尽）。
    # 终态一律经下方 _s6 独立短命会话落库，主会话全程不碰 task 行锁。
    _final_result: dict
    _final_progress: dict
    if marker == MARK_COMPLETE:
        _final_result = {
            "runtime": "hermes-main-session",
            "session_id": sid,
            "log_tail": tail[-800:],
            "outcome": "complete",
            "note": "Agent 主会话端到端完成：计划/子任务/验收报告见会话控制台（事件流），session_id 可恢复。",
        }
        _final_progress = {
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
            or "总结正文放在标记之前" in reason
            or "只写" in reason
        ):
            reason = ""
        _final_result = {
            "runtime": "hermes-main-session",
            "session_id": sid,
            "log_tail": tail[-800:],
            "outcome": "incomplete",
            "reason": reason or "主会话判定未闭环（详见会话记录）",
            "note": "Agent 主会话走完全部流程并如实判定未闭环（未冒充完成）：原因见 reason。"
                    "补齐硬约束（如企业资料）后可重新发起 agent-run。会话控制台可回看全程。",
        }
        _final_progress = {
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

    # 收尾：终态写入用独立短命会话——主泵会话 commit 悬挂已三次卡死整轮
    # （R7 排水、R8 进度块、R8 终态写入 23:23 事件）。客户交互提取只读，
    # 与 result/progress 一起经短命会话落库，主会话不再承担终态 commit。
    try:
        customer = await asyncio.wait_for(_customer_state(session, task.id), timeout=30)
        if customer.get("action_list"):
            _final_result["action_list"] = customer["action_list"]
        _final_result["customer_asks"] = customer.get("asks") or []
    except Exception:  # noqa: BLE001 提取失败不影响任务结论
        logger.warning("提取客户交互块失败（task=%s）", task.id, exc_info=True)

    _terminal_written = False
    for _attempt in range(1, 4):
        try:
            from sqlalchemy import select as _sa_sel_f

            from app.db import SessionLocal as _PumpSessionLocal  # noqa: PLC0415
            from app.services.task_service import _set_rls_context  # noqa: PLC0415

            _result = dict(_final_result)
            _progress = dict(_final_progress)
            async with _PumpSessionLocal() as _s6:
                await asyncio.wait_for(_set_rls_context(_s6, task.enterprise_id), timeout=30)
                _row = (
                    await asyncio.wait_for(
                        _s6.execute(_sa_sel_f(Task).where(Task.id == task.id).with_for_update()),
                        timeout=30,
                    )
                ).scalar_one()
                _row.result = _result
                _row.progress = _progress
                await asyncio.wait_for(_s6.commit(), timeout=30)
            _terminal_written = True
            break
        except Exception:  # noqa: BLE001 终态落库失败下轮由 worker 心跳/回收兜底
            logger.warning(
                "终态落库暂未成功（task=%s，第 %s/3 次）", task.id, _attempt, exc_info=True
            )
            await asyncio.sleep(5)

    # 收尾：把最终 zip 附带的会话记录刷新为完整版（含最终回执）+ 附精简版。
    # 打包时的快照早于最终回执，不刷新交付包里的会话记录会戛然而止。
    try:
        from app.db import SessionLocal as _PumpSessionLocal  # noqa: PLC0415
        from app.services.task_service import _set_rls_context  # noqa: PLC0415

        async with _PumpSessionLocal() as _s7:
            await asyncio.wait_for(_set_rls_context(_s7, task.enterprise_id), timeout=30)
            await asyncio.wait_for(_refresh_zip_record(_s7, task), timeout=120)
    except Exception:  # noqa: BLE001 记录刷新失败不影响任务结论
        logger.warning("收尾刷新会话记录失败（task=%s）", task.id, exc_info=True)


async def _export_session_json(hermes_bin: str, env: dict, session_id: str) -> dict | None:
    """导出一个 Hermes 会话的最新 jsonl 记录。失败返回 None。
    导出可能多行（每行一条记录）、也可能单行整包；最后一行可能是会话元信息——
    从后往前找第一条带消息内容（messages 键或 assistant/user 内容）的记录。"""
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
    for ln in reversed(lines):
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        if isinstance(rec, dict) and (
            rec.get("messages")
            or rec.get("role") in ("assistant", "user")
            or rec.get("content") not in (None, "")
        ):
            return rec
    return None


def _marker_from_export(data: dict | None, min_index: int = 0) -> str | None:
    """从会话导出数据判完成标记（只看 min_index 之后的 assistant 回复，多扫最近若干条——
    末尾可能夹着用户催办/工具记录，不能只看最后一条）。
    协议要求标记逐字出现在**最后一行**——正文里转述子 agent 的「判【PIPELINE_INCOMPLETE】」
    等字样不算回执（曾因此把主会话工作摘要误判为结束回执，提前终止任务）。"""
    if not data:
        return None
    messages = data.get("messages") or []
    checked = 0
    for idx in range(len(messages) - 1, min_index - 1, -1):
        m = messages[idx]
        if m.get("role") != "assistant":
            continue
        content = str(m.get("content") or "")
        if isinstance(m.get("content"), list):  # 兼容 content 为分片数组的导出形状
            content = " ".join(
                str(p.get("text") or "") if isinstance(p, dict) else str(p)
                for p in m["content"]
            )
        lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
        if not lines:
            continue
        last = lines[-1]
        if last == MARK_COMPLETE or last.startswith(MARK_COMPLETE):
            return MARK_COMPLETE
        if last == MARK_INCOMPLETE or last.startswith(MARK_INCOMPLETE):
            return MARK_INCOMPLETE
        checked += 1
        if checked >= 10:  # 只回看最近 10 条 assistant 回复，防误读旧回执
            break
    return None


async def _marker_from_events(session: AsyncSession, task: Task, limit: int = 400) -> str | None:
    """兜底通道：直接扫事件库里主会话最近的输出行找结束标记。
    会话库导出偶发失配（压缩/快照时序）时，主会话真实回执仍在事件流里。"""
    from sqlalchemy import select as sa_select

    from app.db import SessionLocal as _PumpSessionLocal  # noqa: PLC0415
    from app.services.task_service import _set_rls_context  # noqa: PLC0415

    # 独立短命会话（同 _flush 根治口径）
    async with _PumpSessionLocal() as _sm:
        await _set_rls_context(_sm, task.enterprise_id)
        rows = (
            await _sm.scalars(
                sa_select(AgentSessionEvent)
                .where(
                    AgentSessionEvent.task_id == task.id,
                    AgentSessionEvent.kind == "hermes",
                )
                .order_by(AgentSessionEvent.seq.desc())
                .limit(limit)
            )
        ).all()
    for r in rows:
        for ln in (r.content or "").splitlines():
            ln = ln.strip()
            # COMPLETE 只认整行等于标记：终端折行会让「模型推理中提及标记」「我方提示词回显」
            # 的片段以标记开头（如「【PIPELINE_COMPLETE】 at the end.」），
            # startswith 会把这些假回执误判成真完成、杀掉健康会话——整行精确匹配才能当真。
            if ln == MARK_COMPLETE:
                return MARK_COMPLETE
            if ln == MARK_INCOMPLETE or ln.startswith(MARK_INCOMPLETE):
                return MARK_INCOMPLETE
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

    from app.db import SessionLocal as _PumpSessionLocal  # noqa: PLC0415
    from app.services.task_service import _set_rls_context  # noqa: PLC0415

    # 独立短命会话（同 _flush 根治口径）
    async with _PumpSessionLocal() as _se:
        await _set_rls_context(_se, task.enterprise_id)
        rows = (
            await _se.scalars(
                sa_select(AgentSessionEvent)
                .where(AgentSessionEvent.task_id == task.id)
                .order_by(AgentSessionEvent.seq.desc())
                .limit(200)
            )
        ).all()
    return "\n".join(r.content or "" for r in reversed(rows))[-limit:]


async def queue_chat_message(session: AsyncSession, task: Task, message: str, mode: str = "queue") -> None:
    """运行中的任务：把客户消息追加进任务字段 + 事件流（runner 泵循环取出后经 PTY 注入）。

    mode：queue=排队（下一轮处理，/queue 注入）；steer=插话（下一个工具调用后注入
    改方向提示，不打断当前步骤，/steer 注入）。
    行锁串行化：app 与 worker 是两个进程，同写 task.payload.pending_chat 必须
    FOR UPDATE 防丢失更新（多条按到达顺序排队，逐条注入）。"""
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
    pending.append({"text": message, "mode": "steer" if mode == "steer" else "queue"})
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
        # 终态任务的对话授权：任务 DONE 后普通 cap 会被 require_capability 拒绝
        # （「任务已结束，授权上下文失效」），带 purpose=chat 放行——见 deps.require_capability
        purpose="chat",
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
        if re.match(r"^(Resume this session with:|[Ss]ession[_ ]?[Ii]?[Dd]?:|Duration:|Messages:|$)", ln.strip()):
            continue
        out.append(ln)
    return "\n".join(out).strip()


_SESSION_ID_RE = re.compile(r"[Ss]ession[_ ]?[Ii]?[Dd]?:\s*([\w-]+)")


async def pre_chat(session: AsyncSession, project, message: str) -> dict:
    """任务前对话：项目尚无主会话任务时，客户先与 Hermes 聊天建立项目会话
    （session id 存 project.pre_chat_session_id）；后续 agent-run 把任务 prompt
    注入该会话，任务前的交代自动成为主会话上下文。"""
    from app.services.task_service import _set_rls_context  # noqa: PLC0415

    hermes_bin = _hermes_bin()
    if hermes_bin is None or not os.path.exists(hermes_bin):
        raise ValueError("Hermes 未安装：无法对话")
    sid = project.pre_chat_session_id
    # 任务前对话：签发只读 capability（能列项目材料/查企业资料库/查需求，
    # 不能写）——客户问「现在都有哪些资料？」模型要能据实回答
    from app.services.capability import issue_capability

    cap = issue_capability(
        enterprise_id=project.enterprise_id,
        project_id=project.id,
        task_id=0,
        task_type="pre_chat",
        purpose="pre_chat",
    )
    env = _hermes_env(cap)
    _write_cap_file(cap)
    await _set_rls_context(session, project.enterprise_id)

    lock = _CHAT_LOCKS.setdefault(f"project-{project.id}", asyncio.Lock())
    async with lock:
        # 只读说明：业务工具已开放（只读白名单），可查资料/材料/需求如实回答客户
        chat_message = (
            "（系统说明：这是任务创建前的对话，你可以调用业务工具查询企业资料库/项目材料/需求清单"
            "（只读，无写入权限）——客户问「现在都有哪些资料/材料齐不齐」时要据实查询回答；"
            f"当前项目 project_id={project.id}（企业 enterprise_id={project.enterprise_id}）。"
            "客户交代的偏好与背景信息要记进会话，任务开始后这些内容会自动成为主会话上下文。）\n"
            + message
        )
        args = [hermes_bin, "chat", "-q", chat_message,
                "-t", _CHAT_TOOLSETS,
                "--cli", "-Q", "--yolo", "--accept-hooks",
                "--max-turns", "60", "--no-restore-cwd"]
        if sid:
            args += ["--resume", sid]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=env, cwd=env["HERMES_HOME"],
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=1800)
        except asyncio.TimeoutError:  # noqa: UP041 服务器 Python 3.10
            raise ValueError("任务前对话超时（1800s）：请稍后再试") from None
        raw = out.decode("utf-8", "replace") + "\n" + err.decode("utf-8", "replace")
        reply = _strip_session_trailer(raw).strip()
        m = _SESSION_ID_RE.search(raw)
        if m:
            sid = m.group(1)
            project.pre_chat_session_id = sid
            await session.commit()
        return {"reply": reply, "session_id": sid, "returncode": proc.returncode}


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
