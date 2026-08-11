# bidvolt MCP 工具契约

> 后端业务服务暴露给 Hermes 的能力接口。实现为 stdio MCP server（`bidvolt_mcp` 包），
> 内部调用 BidVolt API。租户与授权上下文由服务端按任务级授权注入（产品决策 D-B）。

## 工具分组总览

| 组 | 工具 | 读写 | 说明 |
|---|---|---|---|
| 企业资料 | `search_assets` `list_assets` `get_asset` `classify_enterprise_asset` `upsert_enterprise_facts` | 读/写 | 企业资料查询 + 分类与事实写入（仅企业资料导入任务授权） |
| 招标要求 | `get_requirement` `list_requirements` `get_project_material_blocks` `upsert_requirements` | 读/写 | 招标解析结果读写（含坐标定位） |
| 项目材料 | `list_project_materials` | 读 | 当前招标材料列表 |
| 资料匹配 | `save_material_match_results` | 写 | 资料匹配结果落库 |
| 成果读写 | `get_deliverable_content` `save_deliverable` | 读/写 | 成果结构化内容与版本保存（CAS + 幂等 + source_task_id） |
| 报价 | `calculate_quote` `get_history_price` `get_material_samples` `get_source_metadata` | 读 | 确定性测算 + 外部只读样本/来源元数据（P2：无 apply） |
| 评标 | `get_latest_score` `get_review_items` `submit_score_items` `confirm_review_items` | 读/写 | 评分汇总 + 逐条 review_item 读写 + 批量确认（snapshot + EvidenceRef + CAS） |
| 搜索 | `search_web` `save_source` `link_citation` | 读/写 | AnySearch 与引用追溯（P5，绑定版本） |

**不暴露给 Hermes**：报价应用（apply）、导出、删除、编辑锁管理、权限管理。

> 进度展示说明：不设进度类 MCP 工具。Hermes 流式输出经后端**过滤为白名单事件**（phase/status/percent/当前工作/简短依据/操作提示）后 SSE 推送前端；**禁止透传思维链、工具参数、返回值、内部ID、凭据、错误栈**（产品决策 D-E）。

---

## 1. 企业资料

### search_assets
- 描述：按关键词/类型搜索企业资料（资质、业绩、人员、产品参数、证照等）。**标书生成时企业事实的唯一来源。**
- 参数：`query: string`（关键词）、`category?: string`（资质/业绩/人员/产品/财务/检测报告…）、`project_id?: string`
- 返回：`[{asset_id, name, category, summary, fields:{…}, expires_at?, file_id}]`
- 备注：结果必须带 `asset_id` 引用；过期证照带 `expires_at` 且标注。**不可搜索项目材料域**（产品决策 D-H）。

### list_assets
- 描述：分页列出企业资料（按目录层级）。
- 参数：`category?: string`、`page?`、`size?`

### get_asset
- 描述：读取单个资料详情（含关键字段提取结果）。
- 参数：`asset_id: string`
- 返回：详情 + 字段 + 原始文件定位（file_id/页码）+ revision。

### classify_enterprise_asset
- 描述：**企业资料导入任务专属**：识别资料类型、抽取结构化字段、建议归档目录。
- 参数：`asset_id: string`、`task_id: string`
- 约束：仅企业资料导入任务授权上下文可调用（产品决策 D-B）；结果写入等待用户确认。

### upsert_enterprise_facts
- 描述：**企业资料导入任务专属**：写入/更新企业事实（结构化字段 + 证据引用）。
- 参数：`asset_id: string`、`facts: [{fact_key, value, evidence_ref}]`、`task_id: string`
- 约束：事实必须携带来源文件版本 + 原文定位（EvidenceRef）；低置信字段标记待人工确认。

## 2. 招标要求

### get_requirement
- 描述：读取项目招标解析结果（4.3 requirement），含定位坐标。
- 参数：`project_id: string`、`req_type?: string`（qualification/score_rule/reject_clause/tech_requirement/quote_rule/material_checklist…）
- 返回：`[{req_id, req_type, content, structured, coordinates:[{file_id,page_no,block_index}], confidence, revision}]`

### list_requirements
- 描述：列出项目全部招标要求（按类型分组），生成/评标前先调用。

### get_project_material_blocks
- 描述：按项目读取 doc_block 文本块（解析产物，带坐标），招标解析 Skill 写 requirement 前读取。
- 参数：`project_id: string`、`file_id?: string`、`page?`、`block_index?`
- 返回：`[{block_id, file_id, page_no, block_index, block_type, text_content, extra}]`

### upsert_requirements
- 描述：写入/更新招标要求（招标解析 Skill 产出），每条带坐标与置信度。
- 参数：`project_id: string`、`requirements: [{req_type, content, structured?, coordinates:[{file_id,page_no,block_index}], confidence}]`、`task_id: string`
- 约束：coordinates 为空视为失败；补遗/澄清优先覆盖，记录 supersedes 关系（revision）。

## 4. 资料匹配

### save_material_match_results
- 描述：保存资料匹配结果（material_match Skill 产出），含缺失项关联。
- 参数：`project_id: string`、`results: [{requirement_ref, asset_id?, matched: yes|partial|no, gap_desc?, affected_score_item?, impact_score?, suggestion}]`
- 约束：缺失项必须关联具体招标要求与评分项；不虚构匹配。

## 5. 成果读写

### get_deliverable_content
- 描述：读取成果指定版本的结构化内容（DocModel/SheetModel）。
- 参数：`deliverable_id: string`、`version_no?: int`（缺省当前版本）
- 返回：`{deliverable_id, deliverable_type, version_id, version_no, model}`

### save_deliverable
- 描述：保存成果新版本（version_type=AI生成/AI校核）。**写操作，必须带来源说明。**
- 参数：`deliverable_id: string`、`model: object`、`expected_version_id: string`（CAS 基准版本，冲突 409）、`idempotency_key: string`（幂等）、`source_task_id: string`、`change_note: string`
- 返回：`{version_no, version_id, milestone}`
- 约束：调用前必须已通过 `get_deliverable_content` 读取基准版本；`expected_version_id` 与当前不符返回 409（产品决策 D-C）。

## 6. 报价（只读建议）

### calculate_quote
- 描述：调用确定性 QuoteEngine 计算建议价（三类策略），**只返回建议，不写入**。
- 参数：`material_ref: string`、`cost: number`、`min_profit_rate: number`、`strategy?: win|balance|profit`
- 返回：`{calc_id, suggested_price, score, gross_margin, risk_level, basis, sample_count, engine_version}`
- 备注：sample_count < 阈值（如 5）时提示数据不足，可建议 AI 参考价格；口径不一致样本不参与计算。

### get_history_price
- 描述：查询历史中标记录（外部 Provider 只读）。
- 参数：`material_name?`、`region?`、`year?`、`limit?`
- 返回：`[{material_name, spec, region, win_price, win_date, source_hash}]`

### get_material_samples
- 描述：读取某物料的样本明细（`HistoryPriceProvider.get_material_samples`），用于报价依据解释与审计复算。
- 参数：`material_ref: string`
- 返回：`[{material_name, spec, region, win_price, win_date, source_hash, normalized_params}]`

### get_source_metadata
- 描述：读取外部报价数据来源元数据（`HistoryPriceProvider.get_source_metadata`）：源标识、抓取时间、覆盖范围、更新策略。
- 参数：`provider_id?: string`
- 返回：`[{provider_id, source_name, fetched_at, coverage, update_policy, readonly_verified}]`

## 7. 评标（review_item 主模型，产品决策 D-J）

### get_latest_score
- 描述：读取项目最新评分汇总（由逐条 review_items 计算）。
- 参数：`project_id: string`
- 返回：`{score_id, review_run_id, total_score, biz_score, tech_score, quote_score, reject_count, missing_count, improvable}`

### get_review_items
- 描述：逐条读取评分项（review_item），含状态、EvidenceRef、材料关联。
- 参数：`score_id: string`、`status?: pending_confirm|confirmed|rejected|re_reviewed`
- 返回：`[{item_id, category, problem_description, got, full, improvable, risk_level, suggestion, action_type, evidence:[EvidenceRef], missing_material_types, related_deliverable_node, status, confidence, material_links:[{material_id, match_basis, confidence}]}]`

### submit_score_items
- 描述：提交评分项（ReviewProvider 产出），**evidence 必须为服务端生成的 EvidenceRef**（产品决策 D-G）。
- 参数：`project_id: string`、`snapshot_id: string`、`ruleset_version: string`、`items: [{category, problem_description, got, full, improvable, suggestion, action_type, evidence: EvidenceRef[], risk_level, confidence, requirement_id?, related_deliverable_node?}]`
- 约束：evidence 非空且服务端校验通过，否则丢弃并提示；初始 status = pending_confirm。

### confirm_review_items
- 描述：单条或批量确认 review_items（confirm/reject），会触发受影响成果的合并写入。（**批量接口**，body 传 item_ids 列表即可）
- 参数：`score_id: string`、`item_ids: string[]`、`expected_version: string`、`idempotency_key: string`
- 返回：`[{item_id, status: succeeded|conflict|skipped, reason?}]`
- 约束：CAS 校验 expected_version，冲突返回 conflict；幂等防止重复副作用。

## 8. 搜索与引用

### search_web
- 描述：AnySearch 网络搜索（默认开启），返回结果带 URL 与摘要。
- 参数：`query: string`、`scope?: market|competitor|policy|standard`
- 返回：`[{url, title, snippet}]`

### save_source
- 描述：将搜索结果入库（search_source），判定 trust_level。
- 参数：`query`、`url`、`title`、`snippet`、`trust_level: 1|2|3`

### link_citation
- 描述：记录成果节点对搜索来源的引用（citation），前端"查看出处"依赖此。
- 参数：`deliverable_id`、`deliverable_version_id: string`（绑定版本，产品决策）、`node_id`、`source_id`、`quote_text`
- 约束：citation 必须绑定 deliverable_version_id。

## 9. MCP server 实现约定

- 传输：stdio（本地子进程），`supports_parallel_tool_calls: true`
- 鉴权：server 启动时从环境读取 `BIDVOLT_INTERNAL_TOKEN`，调用后端 API 时带内部头
- **授权注入（产品决策 D-B）**：MCP server 按当前任务的**授权上下文**（enterprise/project/task/工具白名单/对象范围）校验；企业资料写工具仅企业资料导入任务可用；工具参数不接收 enterprise_id
- 错误语义：业务错误返回结构化 `{code, message}`；401 表示内部 token 失效；409 表示版本冲突
- 过滤：`hermes mcp configure bidvolt` 安装时按本清单勾选，不暴露未列工具

## 10. MCP IDL 与契约测试（P0-3）

### 10.1 IDL/JSON Schema 约定

- 本清单中每个工具的**参数 Schema**由单一 IDL 定义：`bidvolt_mcp/schema/openrpc.json`（OpenRPC 1.2.6 + JSON Schema），由 `bidvolt_mcp/tools.py` 的 `TOOL_DEFS` 生成（`python -m bidvolt_mcp.gen_schema`），`TOOL_DEFS` 即唯一事实源，**禁止手写两端各自维护**
- 每个工具 Schema 包含：`name`、`summary`（description）、`params`（JSON Schema，含必填与约束）；`auth_scope`（任务级授权范围）与 `idempotent`（写工具必须带 `idempotency_key`）在后端 API 层强制
- `tests/module/test_mcp_idl.py` 校验 IDL 文件与 `TOOL_DEFS` 一致，且包含 P0-3 要求的所有工具，纳入 CI

### 10.2 五条 Skill 路径端到端契约测试

| Skill 路径 | 测试场景（Mock 后端 + 合成材料） | 断言 |
|---|---|---|
| 招标解析（tender-parse） | 上传合成招标文件 → 解析 → `upsert_requirements` 写入 | 每条 requirement 有 coordinates；查重不重复写；补遗 supersedes 记录 |
| 资料匹配（material-match） | `search_assets` 检索 → `save_material_match_results` | 缺失项关联 requirement/评分项；asset_id 真实存在 |
| 标书生成/校核（bid-generate） | 生成三份成果 → `save_deliverable` | expected_version_id CAS；幂等；企业事实可溯源；无公式报价带 is_ai_suggest 标注 |
| 模拟评标（mock-evaluate） | `submit_score_items` → `confirm_review_items` | evidence 服务端校验通过；批量返回逐条结果；重审只跑受影响项 |
| 针对性修改（targeted-edit） | 选区 diff → 应用 | diff 节点存在于基准版本；AI 不调 `save_deliverable`；应用后与 diff 一致 |

> 完整测试清单（含跨租户 IDOR、Prompt Injection、任务幂等、文件安全、SSE 白名单）见 `docs/威胁模型与测试清单.md`。
