# BidVolt · Agent 主会话端到端新方案 README

> 一句话：**一个任务 = 一个 Hermes 主会话**。服务端只提供会话基础设施（能力令牌、事件流控制台、催办/判定、逐份成文），
> **投标流程完全由主会话自主编排**——用 `todo` 列计划、用 `delegate_task` 派 分析/提取/校验/写作/评审 子 agent，
> 带着验收报告循环修复，直到全部满足才输出交付回执。
> 旧管线（tender_parse / bid_generate / 旧 `/demo`）**完全隔离、原样保留**。

---

## 1. 设计原则

1. **主会话自主**：流程编排（计划、委派、验收、修复循环）全部交给主 agent（skill：`bidvolt-agent-pipeline`），
   服务端不写死任何步骤顺序，不干涉流程。
2. **隔离**：新任务类型 `agent_pipeline`、新接口 `/agent-run` 系列、新页面 `/demo/agent-demo.html`；
   旧任务类型、旧接口、旧 skill、旧页面零改动。
3. **如实判定**：主会话全部验收门通过 → 最后一行输出 `【PIPELINE_COMPLETE】`；
   确有无法闭环项（如企业资料缺失）→ 输出 `【PIPELINE_INCOMPLETE】原因…`。
   **如实标注是最终结论，不冒充完成、不无谓重试**。
4. **全程可见**：服务 ↔ 主会话的每条消息（指令、主会话每次回复、子任务委派与验收结论）
   逐条落入事件流表，网页控制台实时显示；交付包必附 `会话记录/主会话记录.md`。
5. **交付红线不变**：模板原文、章节、编号、顺序、表格、字段及注释不能减少/改写/替换——
   成文全部走底稿式（复制原 docx 整段，填空=修订模式+批注来源，撰写内容=修订插入+批注）。

## 2. 架构总览

```
用户网页端（/demo/agent-demo.html）
        │ ① POST /api/v1/projects/{id}/agent-run
        ▼
API 层（FastAPI） ── 签发任务级 capability token（企业/项目/任务 + MCP 工具白名单）
        │ ② 任务入队（task_type=agent_pipeline）
        ▼
Worker ── run_agent_pipeline ──③ PTY 长驻启动──▶  Hermes 主会话（hermes chat --cli）
   ▲                          -t bidvolt,todo,delegation,file,vision
   │                          -s bidvolt-agent-pipeline
   │  事件流（AgentSessionEvent）          │ ④ 自主编排
   │  ◀──────── 服务指令 / 主会话输出 ─────┤    todo 计划（A分析→B提取→C校验→D撰写→E校验→F评审→G交付）
   │                                     │    delegate_task 派子 agent（后台运行）
   │  ⑤ 30s 轮询 Hermes 会话库             │    结果自动回流（ASYNC DELEGATION BATCH COMPLETE）
   │  （判据=主会话最后一条回复）            │    验收不过 → 带报告回修 → 再派（循环）
   │  三级催办 / 提前退出自动 --resume 续跑  ▼
   │                          ⑧ 结束回执：最后一行
   │                          【PIPELINE_COMPLETE】 / 【PIPELINE_INCOMPLETE】原因
   │  ⑥ 任务结果：outcome / reason / session_id
   ▼
响应文件包（GET response-package）
   价格/商务/技术 三目录逐份成文（条目模板原文 + 填空修订批注 + 撰写内容）
   + 报价单.xlsx + manifest.json + 会话记录/主会话记录.md
```

详细图示：`docs/流程图/Agent主会话端到端新方案流程图.svg`（PNG 同目录）。

## 3. 组件清单

| 组件 | 位置 | 职责 |
|---|---|---|
| `run_agent_pipeline` | `app/services/agent_pipeline.py` | 主会话执行器：PTY 长驻 REPL、事件流落库、回执轮询、三级催办、续跑、2h 上限 |
| `chat_with_session` | 同上 | 客户在网页与主会话对话（`hermes chat -q --resume <sid>`） |
| `session_record_markdown` | 同上 | 事件流 → `会话记录/主会话记录.md`（随包交付） |
| `_poll_session_marker` | 同上 | 读 Hermes 会话库（`hermes sessions export`），判据=主会话最后一条回复 |
| API 路由 | `app/api/agent.py` | `POST /agent-run`、`GET /agent-run/{task_id}`、`GET /agent-run/{task_id}/stream`（SSE）、`POST /agent-run/{task_id}/chat` |
| 事件模型 | `app/models/agent.py` | `AgentSessionEvent`（enterprise/project/task/seq/kind/content，FORCE RLS） |
| 任务类型 | `app/constants.py` | `TaskType.AGENT_PIPELINE = "agent_pipeline"` |
| 能力白名单 | `app/services/capability.py` | `agent_pipeline` 工具集 = 解析/生成/评审工具的并集 |
| 控制台页面 | `app/static/agent-demo.html` | `/demo/agent-demo.html`：注册/建项目/上传/发起/控制台（全量消息）/对话/下载包 |
| 主会话 skill | `/data/hermes/skills/bidvolt/agent-pipeline/SKILL.md` | 主 agent 流程守则（角色约定、委派与回执协议、save_deliverable 模型约定） |

## 4. 主会话运行机制（框架层契约）

- **启动**：`hermes chat --cli -t bidvolt,todo,delegation,file,vision -s bidvolt-agent-pipeline --no-restore-cwd --max-turns 120`，
  以 PTY 方式长驻（顶层委派一律后台运行，结果作为新消息回流——进程必须活着，所以不能用 `-q` 单轮模式）。
- **喂入**：就绪后（横幅出现 `Activated skills` + 提示符）以 bracketed-paste 方式提交任务书。
- **事件流**：PTY 输出逐行剥 ANSI、去重复行，5s 节流批量落 `AgentSessionEvent`；控制台 SSE 回放+实时。
- **回执判定**：每 30s 读 Hermes 会话库的最后一条 assistant 回复；`【PIPELINE_COMPLETE】` → 成功收尾；
  `【PIPELINE_INCOMPLETE】` → 任务完成但 `outcome=incomplete`（**不重试**，原因如实记录）。
- **兜底**：无新输出 600s → 三级催办（逐级加强，最后一级只索取回执）；催办耗尽仍无回执 → 按会话记录判定收尾；
  主会话进程提前退出 → 自动 `--resume` 续跑（≤2 轮）；总时长 7200s 上限。

## 5. 接口（新方案，全部挂在 `/api/v1/projects` 下）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/{project_id}/agent-run` | 发起主会话任务（`idempotency_key` 幂等；返回 task_id + capability_token） |
| GET | `/{project_id}/agent-run/{task_id}` | 任务状态/进度/结果（`result.outcome ∈ complete|incomplete`、`result.reason`、`result.session_id`） |
| GET | `/{project_id}/agent-run/{task_id}/stream?since=N` | SSE 事件流（`event: message` / `event: end`），回放+实时 |
| POST | `/{project_id}/agent-run/{task_id}/chat` | 客户直接与主会话对话（`--resume` 追加消息，同一任务串行） |
| GET | `/{project_id}/response-package` | 响应文件包（新方案任务自动附 `会话记录/主会话记录.md`） |

开关：`AGENT_PIPELINE_ENABLED=1`（关闭时新接口返回 409，旧功能不受影响）。

## 6. 安全

- 每个任务签发一次性 capability token（HMAC 签名，默认 1h），绑定 企业/项目/任务 + MCP 工具白名单；
- MCP 工具调用经 `verify_capability` 校验（签名/有效期/租户/白名单）；
- 业务表 PostgreSQL FORCE RLS（`current_setting('app.enterprise_id')`），事件表同；
- Hermes 侧仅授予 `bidvolt,todo,delegation,file,vision` 工具集（file 用于读取委派全量报告，vision 用于看图/图像理解；MCP 写操作仍受 token 限制）。

## 7. 部署要点（服务器）

- 代码：`/data/bidvolt`（FastAPI app + worker 两个 supervisor 进程）；
- skill：`/data/hermes/skills/bidvolt/agent-pipeline/SKILL.md`（frontmatter `name: bidvolt-agent-pipeline`）；
- Hermes ≥ v0.19（依赖 `delegation`/`todo` 内建工具集）；
- 环境：`.env` 中 `AGENT_PIPELINE_ENABLED=1`；worker 进程需能访问 `/data/hermes/venv/bin/hermes`；
- ⚠️ Python 3.10：`asyncio.wait_for` 抛 `asyncio.TimeoutError`（≠内建 `TimeoutError`），
  本文件已用 `# noqa: UP041` 保护——**不要对 `agent_pipeline.py` 跑 `ruff --fix`**（会自动改写回去）。

## 8. 与旧方案隔离边界

| | 旧方案 | 新方案（Agent 主会话） |
|---|---|---|
| 任务类型 | tender_parse / bid_generate / material_match / bid_review… | `agent_pipeline` |
| 触发 | `/demo` 旧页面按钮 | `/demo/agent-demo.html` |
| skill | bidvolt-tender-parse / bidvolt-bid-generate | bidvolt-agent-pipeline |
| 编排 | 服务端 handler 分阶段 | 主会话自主（todo + delegate_task） |
| 成文 | 相同底稿式导出（共用 export_service） | 同左 + 会话记录附件 |

两者共用：底稿式成文（`build_response_package`）、capability 签发、存储与 RLS。

## 9. 端到端实测记录（2026-08-21 · 产品材料复测项目 #181）

- 任务 335：**一次会话跑完全部 7 步**（A探查→B分析→C提取→D校验→E撰写→F评审→G交付），
  共派 **6 个子 agent**（分析/提取/校验/撰写/验收/评审），全部回执；
- 事件流 **12,433 条**全程落库（服务指令、主会话每次回复、委派回执与验收结论）；
- 交付件：商务标 v8（22 节通用框架）、技术标 v8（15 节+双模板详评指引）、报价单 v7（4 sheets 56 行）；
- 主会话如实判定：流程闭环 ✅ / 成果闭环 ❌——应答人企业资料库为空 → 企业侧字段全部【待补充】，
  实施地点矛盾（北京/杭州）待采购人书面确认，履约保证金细节待合同落实；
- 交付包校验（脚本 `output/agent-notes/verify_deliverables3.py`）：
  10 份条目文件原文（含修订删除线）**逐字包含于采购文件原文**，0 差异；报价单 4 表可开；会话记录 1.14 MB 完整；
- 会话 id：`20260821_210549_e6d7f1`（可 `hermes --resume` 续跑；补齐企业资料后重新发起 agent-run 即可闭环）。

## 10. 已知限制 / 后续

1. 委派子 agent 的报告摘要会被 Hermes 自动截断（全量存 `/data/hermes/cache/delegation/...`）——
   主会话已带 `file` 工具集可直接读全量日志；
2. 主会话偶尔忘记输出回执 → 服务端三级催办 + 按最后总结判定收尾（已如实标注）；
3. 后续可做：服务端任务/计划/报告 UI 的进一步可视化、按分包批量派写作子 agent、
   企业资料入库后自动触发续跑等。

## 11. 相关文件索引

- 执行器：`app/services/agent_pipeline.py`
- 接口：`app/api/agent.py` · 模型：`app/models/agent.py`
- 控制台页：`app/static/agent-demo.html`
- skill：`output/agent-notes/agent_pipeline_SKILL.md`（部署副本：`/data/hermes/skills/bidvolt/agent-pipeline/SKILL.md`）
- 流程图：`docs/流程图/Agent主会话端到端新方案流程图.svg / .png`
- 交付样例：`output/交付样例/`（输入材料 10 份、响应文件包 zip+解压、流程图、查看说明.md）
- 测试：`tests/module/test_agent_pipeline_api.py`、`tests/unit/test_docx_format.py`
