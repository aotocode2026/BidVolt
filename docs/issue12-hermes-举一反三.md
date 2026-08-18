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

## 二、本轮整改（V1：把 Agent 闭环内嵌进 worker，接口契约保持 MCP 对齐）

按 docs/hermes/README.md 建议的"最小 Python agent loop"方案，将 bid-generate skill 的
Procedure 落进 worker（后续可整体迁移到 Hermes gateway 后）：

1. **结构确认流程化**：招标解析阶段从招标文件提取响应文件格式（第五章响应文件格式等），
   落库 doc_structure 要求（role/guide/order），要求页可见可确认；
2. **规划**：生成时优先消费解析落库结构（requirement）→ 生成时解析材料（tender）→ 通用兜底；
3. **取证**：企业事实（EnterpriseFact）+ 项目材料摘录 + 历史知识检索（knowledge refs 入任务元数据）；
4. **起草**：分章并行生成，逐条响应全部要求；
5. **自检闭环**：起草后对照全部要求自检（missing/conflicts JSON），缺项回注补写，
   **迭代最多 3 轮直至闭环**；达上限仍未闭环 → deliverables_ready=False +
   任务注明"自检未闭环：仍有 N 项要求未响应"（**绝不把中间未闭环的交付件当完成交付**）；
6. **终检结构合规**：招标文件要求的章节缺失 → 终检拦截。

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
