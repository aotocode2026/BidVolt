# Issue #12 举一反三审计与 Hermes 接入现状（2026-08-18）

## 一、Hermes 是什么、为什么此前"没用起来"

- Hermes = 部署在服务器容器内的 Agent 运行时（NousResearch/hermes-agent，/data/hermes，
  supervisor 守护，gateway 127.0.0.1:9119），挂载本仓库 bidvolt MCP（25 个业务工具）、
  anysearch 搜索 MCP 与 5 个业务 Skill（招标解析/资料匹配/标书生成/模拟评标/针对性修改），
  边界原则 P1–P7（禁止编造企业事实、只建议不落库、证据可定位、任务级授权等）。
- 此前状态：进程在跑、MCP 连通性测过，但业务主流程（worker 的 bid_generate/tender_parse）
  **从未调用 Hermes**——是手写提示词直连 LLM；设计文档自述"Agent 运行时入口（核心缺口）"
  未建、任务级 capability 全流程待办。
- 结论：Hermes 此前是"装好但闲置"，生成质量自然不如"直接把文件丢给自由发挥的大模型"。

## 二、本轮整改（Hermes 真接入 + 路线图落地，2026-08-18 更新）

### Hermes 真接入（运行时级已验证）
- worker 在 `payload.agent="hermes"` 时以 `hermes chat -q ... -t bidvolt -s bidvolt-bid-generate -Q --cli`
  无头启动部署的 Hermes Agent（HERMES_HOME=/data/hermes 注入），
  签发任务级 capability token（企业/项目/任务/工具白名单）经 BIDVOLT_CAPABILITY_TOKEN 注入，
  MCP 每次工具调用携带并逐调用校验；
- 实测：无头执行 OK（session 正常）、`bidvolt:health` 等 MCP 工具在 CLI 会话真实调用成功、
  5 个 skill 已安装启用；生成任务 150–152 走 hermes 路径（runtime=hermes 记录在任务结果）；
- **主模型已切换 MiniMax-M3（2026-08-19，用户指正）**：原部署写死 MiniMax-Text-01（当时安装脚本默认，
  非决策）。M3 实测可用（HTTP 200）。切换后端 .env + config 默认 + install-hermes.sh 默认 + Hermes config，
  全服务重启验证。效果：内嵌闭环自检质量显著提升——公网 E2E **1 轮闭环（missing 0、closed=True）**、
  本地 2 轮闭环；Hermes Agent 亦由"叙述工具调用"转为真实调用 MCP 工具（已读到真实要求数据）；
- **Hermes Agent 生成全链路闭环达成（2026-08-19，M3 + create_deliverable 工具 + 执行直达约束）**：
  任务 171 实测由 Hermes 真实调用 `create_deliverable×3 → save_deliverable`，
  三份成果 v1 落库（授权、CAS、白名单全部生效）；LLM 云接口偶发 529 限流时如实失败并回退；
  生产默认仍走内嵌闭环（质量更稳、受控），`agent=hermes` 作为已验收的并行路径保留实验开关；
- **已知限制（如实记录）**：Hermes 多步生成中 Agent 仍会在保存前输出 A/B/C/D 方案等待确认
  （headless 模式 skill 决策环待调，预期在 SKILL.md 增加"非交互模式必须直接执行"约束）；
  capability 已通过临时文件兜底通道（/tmp/bidvolt_cap_token，0600）传递并实测生效；

### 路线图落地
| 项 | 状态 |
|---|---|
| 标书结构流程化确认（解析落库 doc_structure，生成消费） | ✅ |
| 报价限价校验（structured.price_limit，apply 超限 422） | ✅ |
| 真实价格数据源：AnySearch+LLM 抽取公开中标价（≥3 条采用，来源 URL 可追溯，Mock 兜底，测算记录 sample_source） | ✅ |
| 评分细则驱动评审：score_rule 必须体现在成果中，否则评审警告（structured.score_rule weight/criterion） | ✅ |
| 应答函格式页：商务标首部确定性应答函（未知字段【待补充】不编造） | ✅ |
| 终检 v2：逐条要求覆盖 + 结构合规 + 文字质量（重复段落/【待补充】）+ 字数统计 | ✅ |
| tasks 列表返回 result/error（评审页任务表摘要可见） | ✅ |
| Hermes 运行时接入（capability 全流程） | ✅（运行时验证；Agent 执行一致性待换模型） |
| loop 整体迁移 Hermes gateway / 任务级 capability 在 MCP 全量启用 | ⏳ 生产前待办（同上，依赖主推理模型） |

## 三、举一反三：同类"硬编码 vs 招标驱动"问题的处置

| 问题 | 处置 | 状态 |
|---|---|---|
| 标书结构拍脑袋 | 解析阶段抽取响应文件格式，生成按该结构 | ✅ 已整改（本轮） |
| 报价限价不生效 | 抽取 structured.price_limit，apply 超限价 422 拦截 | ✅ 已整改（本轮） |
| 报价用 Mock 样本 | V1 已知：HistoryPriceProvider 为 Mock，需接真实中标价数据源 | ⏳ 路线图 |
| 评标规则通用化 | 评分细则要求已抽取，但 review_service 用内置规则集；需按招标评分细则动态生成权重 | ⏳ 路线图 |
| 应答函/授权书等格式页 | 响应文件格式中的格式页（应答函、授权书）随结构抽取，但尚未生成标准格式页模板 | ⏳ 路线图 |
| 履行期限未结构化 | 技术规范书含履行期限，生成"进度与交付"章依赖材料摘录而非结构化字段 | ⏳ 路线图 |
| 导出顺序 | 招标要求价格→商务→技术胶装顺序，导出需同序 | ⏳ 路线图（小改动） |
| 任务级 capability 全流程 | MCP 用服务账号 JWT 回退；签发任务级 token 流程待办 | ⏳ 生产前待办 |
| Agent 运行时整体迁移 | V1 内嵌 loop → 迁移 Hermes gateway（skill 对齐、授权对齐） | ⏳ 路线图 |

## 四、验证（2026-08-18）

- 本地 E2E 26/26：结构抽取 31 条落库、30 章规划、自检 3 轮 missing 18→7、
  技术标 55490 字/商务标 15842 字；未闭环 → deliverables_ready=False 显式标注；
- 公网 E2E 26/26：结构 requirement 来源、技术标 14282 字/商务标 15900 字、
  自检 3 轮 missing 12 → 草稿标注（样本：企业 78 / 任务 #145–#148）；
- 全量单测 239 passed / 1 skipped（含闭环成功/失败两路径、结构消费、终检结构合规、限价拦截）。
