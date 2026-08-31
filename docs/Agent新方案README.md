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
| `run_agent_pipeline` | `app/services/agent_pipeline.py` | 主会话执行器：PTY 长驻 REPL、事件流落库、回执轮询、三级催办、续跑、**6h 上限** |
| `chat_with_session` | 同上 | 客户在网页与主会话对话（`hermes chat -q --resume <sid>`；终态任务带 `purpose=chat` 授权） |
| `queue_chat_message` | 同上 | 运行中消息/回答排队（`mode=queue` 排队 / `steer` 插话），经泵循环 PTY 注入 |
| `session_record_markdown` | 同上 | 事件流 → `会话记录/主会话记录.md`（随包交付） |
| `_poll_session_marker` | 同上 | 读 Hermes 会话库（`hermes sessions export`），判据=主会话 assistant 回复（导出为唯一判据） |
| 图片描述后台任务 | `app/services/image_desc.py` | 入库自动入队：图片/文档内嵌图调 qwen-vl 出结构化描述，sha256 全局缓存 |
| API 路由 | `app/api/agent.py` | `POST /agent-run`、`GET /agent-run/{id}`、`GET /agent-run/{id}/stream`（SSE）、`GET /agent-run/{id}/questions`、`POST /agent-run/{id}/asks/{aid}/answer`、`POST /agent-run/{id}/chat`、`POST /pre-chat` |
| 事件模型 | `app/models/agent.py` | `AgentSessionEvent` / `AgentArtifact` / `AgentCustomerAsk`（问答窗口+超时标记） |
| 任务类型 | `app/constants.py` | `TaskType.AGENT_PIPELINE = "agent_pipeline"`、`TaskType.IMAGE_DESCRIBE = "image_describe"` |
| 能力白名单 | `app/services/capability.py` | `agent_pipeline` 工具集（44 工具）+ `pre_chat` 只读工具集 + `purpose` 用途标记 |
| 控制台页面 | `app/static/agent-demo.html` | `/demo/agent-demo.html`：登录状态条/识图进度条/多选上传/资料溯源列表/问卡（多组+倒计时）/对话（排队+插话）/下载包 |
| 主会话 skill | `/data/hermes/skills/bidvolt/agent-pipeline/SKILL.md` | 主 agent 流程守则（角色约定、委派与回执协议、问答窗口、描述找图、成文阶段） |
| 成文工具链服务 | `app/services/assembly_service.py` | 机制原语：候选底稿/模板清单/切片（内存切片仓）/填空/追加/校验/封存/报价xlsx/打包 |
| 成文工具链接口 | `app/api/assembly.py` | `/projects/{id}/assembly/...` 工具端点（capability 逐工具校验）+ 产物下载端点 |
| 成文产物模型 | `app/models/agent.py` → `AgentArtifact` | 条目 docx / 报价单 xlsx / 响应包 zip（RLS） |
| MCP 工具注册 | `bidvolt_mcp/assembly_tools.py` + `tools.py` | 成文工具 + `get_image_descriptions` 等 MCP 定义 |

## 4. 主会话运行机制（框架层契约）

- **启动**：`hermes chat --cli -t bidvolt,todo,delegation,file,vision,... -s bidvolt-agent-pipeline --no-restore-cwd --max-turns 360`，
  以 PTY 方式长驻（顶层委派一律后台运行，结果作为新消息回流——进程必须活着，所以不能用 `-q` 单轮模式）。
- **喂入**：就绪后（横幅出现 `Activated skills` + 提示符）以 bracketed-paste 方式提交任务书。
- **事件流**：PTY 输出逐行剥 ANSI、过滤回显与噪音、去重复行，5s 节流批量落 `AgentSessionEvent`；
  我方提交文本按**子串**过滤（防终端折行把提示词片段漏进事件流），控制台 SSE 回放+实时。
- **回执判定**：每 30s 读 Hermes 会话库的 assistant 回复（**导出为唯一判据**——
  事件流兜底只在卡死出口做整行精确匹配，防止推理/回显折行误判假完成）；主会话最后一行
  `【PIPELINE_COMPLETE】` → 成功收尾；`【PIPELINE_INCOMPLETE】原因…` → 如实未闭环（不重试）。
- **复核确认轮**：收到 COMPLETE 后服务端追加一轮「系统复核确认」——逐份打开交付文件核对
  空位清零、报价依据随件、**深度与证据三查**（方案正文实质内容/评分装订矩阵图数对齐/依据三路），
  不足回修后重新回执。
- **兜底**：无新输出 600s → 三级催办（逐级加强，最后一级只索取回执）；催办耗尽仍无回执 → 按会话记录判定收尾；
  主会话进程提前退出 → 自动 `--resume` 续跑（≤2 轮）；总时长 **6h** 上限。
- **capability**：管线 cap 有效期 = 管线时长 + 1h（7h，默认 1h 曾在第 60 分钟全线 403 导致自救重签）；
  终态任务对话用 `purpose=chat` 短时授权（完成/取消/失败终态均可对话澄清）。
- **提问关问答窗口**：ask 带 `window_minutes`（默认 20）；超时未答服务端注入「已超时，由你自行决定」
  纯信号（附原问题，不给答案），材料类问题以库内事实为准能装订的全部装订；问卡仍可补答。
- **RLS 防弹化**：全部 FORCE RLS 表策略为「GUC 数字校验守卫」形式（空串/垃圾值静默过滤不报错）；
  泵循环所有 DB 写入自含 RLS 重设、`_flush` 带异常兜底——瞬时 DB 故障不再能杀死泵循环。

## 5. 接口（新方案，全部挂在 `/api/v1/projects` 下）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/{project_id}/agent-run` | 发起主会话任务（`idempotency_key` 幂等；`resume_from_task_id` 续跑；返回 task_id + capability_token） |
| GET | `/{project_id}/agent-run/{task_id}` | 任务状态/进度/结果（`result.outcome ∈ complete|incomplete`、`result.reason`、`result.session_id`；`progress.percent/current_work` 里程碑驱动） |
| GET | `/{project_id}/agent-run/{task_id}/stream?since=N` | SSE 事件流（`event: message` / `event: end`），回放+实时 |
| GET | `/{project_id}/agent-run/{task_id}/questions` | 客户问卡：ask 列表（问答窗口/超时标记/已答状态）+ 提交前动作清单 |
| POST | `/{project_id}/agent-run/{task_id}/asks/{ask_id}/answer` | 客户回答（运行中排队注入；终态直接对话） |
| POST | `/{project_id}/agent-run/{task_id}/chat` | 控制台对话（`mode=queue` 排队 / `steer` 插话；运行中一律经泵注入） |
| POST | `/{project_id}/pre-chat` | 任务前对话（只读工具；会话 id 存项目，开跑自动注入任务书） |
| GET | `/{project_id}/response-package` | 响应文件包：新方案任务只取主会话打包好的 zip（尚未打包 → 409 指引重新发起 agent-run）；旧任务维持原服务端成文逻辑 |

前端对接全量字段与示例见 `docs/前端对接接口文档.md`。

### 5.1 成文工具链（主会话自主成文，服务端只给机制）

主会话在交付阶段（skill 成文阶段 H）自己调用以下工具成文，服务端不再替它做决策：

| 工具 | 作用 |
|---|---|
| `resolve_template_draft` | 列出候选底稿 docx（按是否含《响应文件格式》章分级）+ 推荐 file_id |
| `get_template_outline` | 模板清单（价格/商务/技术分组，每项带 req_id） |
| `slice_template_item(file_id, req_id)` | 底稿条目区间**字节级复制**为切片（保留格式）→ slice_id |
| `fill_template_slice(slice_id, fields, fills)` | 修订模式填空+批注（标准字段+定向替换；未知空位原位【待补充】） |
| `append_template_slice(slice_id, nodes, comment)` | 撰写内容修订插入+批注 |
| `verify_template_slice(slice_id)` | **逐字校验原文⊂底稿**，返回 issues（先 verify 后 seal） |
| `seal_template_item(slice_id, dir, filename)` | 生成条目 docx 落产物库 → artifact_id |
| `build_quote_xlsx(sheets)` | 报价单 xlsx 落产物库 |
| `package_response_zip(artifact_ids, draft_file_id)` | 打包（自动附会话记录+manifest）→ zip 产物 |

机制保真红线：切片=复制原件；改动=修订+批注；校验=原文逐字⊂底稿。写什么、按什么顺序、封存哪些条目，全部由主会话决定。

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
- 模型：`/data/hermes/config.yaml`（当前 `deepseek-v4-flash`/`deepseek`，max_tokens=32768，
  `display.busy_input_mode: queue`；视觉 `auxiliary.vision` = qwen-vl-max）；
- 环境：`.env` 中 `AGENT_PIPELINE_ENABLED=1`；worker 进程需能访问 `/data/hermes/venv/bin/hermes`；
  `DEEPSEEK_API_KEY` 在 supervisor `environment=` 注入（不进 .env）；
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

## 9. 端到端实测记录（持续更新）

### 2026-08-21 · 产品材料复测项目 #181（早期形态）
- 任务 335：一次会话跑完全部 7 步，共派 6 个子 agent；事件流 12,433 条；
- 交付件：商务标 v8 / 技术标 v8 / 报价单 v7；主会话如实判定成果未闭环（企业资料库为空）；
- 交付包校验：10 份条目文件原文逐字包含于采购文件原文，0 差异。

### 2026-08-30 · 风光场站真题 R5（deepseek-v4-pro，项目 #193）
- 会话 `20260830_063245_307e95`，多轮续跑；交付包 14.5MB（价格 3+商务 4+技术 2+内部 1+会话记录+manifest）；
- 报价 48.50 万（得分最优反算：区间平均价浮动法+12 条行情样本+成本科目+规则面勾稽）；
- 技术卷 189 页/219 图（16 组业绩 207 图+5 人证件社保+7 资质）；商务补充 53 页/35 图；
- 验收门 B→C→C2→E→F→G 全过；未闭环项如实列明（08-25 已成交=演练交付、信用报告时点）；
- 教训闭环：cap 1h 过期自救重签 → 改为管线时长+1h；假回执折行误判 → 导出唯一判据+整行精确兜底；
  下载端点 cap 401 → require_capability 统一。

### 2026-08-31 · 风光场站真题 R6（页面真实浏览器驱动，项目 #198）
- 识图后台任务 1368 个全部完成 0 失败，图片描述缓存 1944 张（sha256 全局复用）；
- 首轮 MiniMax M3 浅交付（22 分钟/0.5MB/0 图）→ 定位两层服务端缺陷（RLS 空串崩溃、泵循环 GUC 丢失）修复后；
- MiniMax 配额耗尽（429 Token Plan 上限）→ 切 deepseek-v4-flash 续跑：15 分钟出包 10.7MB，
  技术卷 51 图/1.34 万字（证据装订恢复），报价 48.50 万三路依据勾稽；
- 结论：flash 适合日常低成本跑；对标金标准深度（技术 584 图/12.5 万字）仍建议 deepseek-v4-pro。

## 10. 已知限制 / 后续

1. 委派子 agent 的报告摘要会被 Hermes 自动截断（全量存 `/data/hermes/cache/delegation/...`）——
   主会话已带 `file` 工具集可直接读全量日志；
2. 主会话偶尔忘记输出回执 → 服务端三级催办 + 按最后总结判定收尾（已如实标注）；
3. 交付深度受主推理模型能力约束：flash 快而浅、pro 深而慢/贵——模型选择=成本/质量权衡（产品侧拍板）；
4. 续跑式推进是"增量修补"：深度不达标时更有效的是换强模型重跑整单，而非无限续跑；
5. 后续可做：服务端任务/计划/报告 UI 进一步可视化、按分包批量派写作子 agent、协议门信号
   （检测提问关/委派未发生时的提醒，进一步收紧"一次性达标"）。

## 11. 相关文件索引

- 执行器：`app/services/agent_pipeline.py`
- 接口：`app/api/agent.py` · 模型：`app/models/agent.py`
- 控制台页：`app/static/agent-demo.html`
- skill：`output/agent-notes/agent_pipeline_SKILL.md`（部署副本：`/data/hermes/skills/bidvolt/agent-pipeline/SKILL.md`）
- 流程图：`docs/流程图/Agent主会话端到端新方案流程图.svg / .png`
- 交付样例：`output/交付样例/`（输入材料 10 份、响应文件包 zip+解压、流程图、查看说明.md）
- 测试：`tests/module/test_agent_pipeline_api.py`、`tests/unit/test_docx_format.py`
