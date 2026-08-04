# bidvolt MCP 工具契约

> 后端业务服务暴露给 Hermes 的能力接口。实现为 stdio MCP server（`bidvolt_mcp` 包），
> 内部调用 BidVolt API。租户上下文（enterprise_id/project_id）由服务端按 Profile/Session 注入。

## 工具分组总览

| 组 | 工具 | 读写 | 说明 |
|---|---|---|---|
| 企业资料 | `search_assets` `list_assets` `get_asset` | 读 | 企业资料库查询（生成/匹配的事实来源） |
| 招标要求 | `get_requirement` `list_requirements` | 读 | 招标解析结果（含坐标定位） |
| 项目材料 | `list_project_materials` | 读 | 当前招标材料列表 |
| 成果读写 | `get_deliverable_content` `save_deliverable` | 读/写 | 成果结构化内容与版本保存（P3 可追溯） |
| 报价 | `calculate_quote` `get_history_price` | 读 | 确定性测算（P2：无 apply） |
| 评标 | `get_latest_score` `submit_score_items` | 读/写 | 评分记录读写 |
| 搜索 | `search_web` `save_source` `link_citation` | 读/写 | AnySearch 与引用追溯（P5） |

**不暴露给 Hermes**：报价应用（apply）、导出、删除、编辑锁管理、权限管理。

> 进度展示说明：不设进度类 MCP 工具。Hermes 流式输出本身包含思考过程与每次工具调用，后端经 SSE **原样透传**前端即为进度（P05 生成中页面 / 底部对话），前端折叠技术细节即可（见 P6，README）。

---

## 1. 企业资料

### search_assets
- 描述：按关键词/类型搜索企业资料（资质、业绩、人员、产品参数、证照等）。**标书生成时企业事实的唯一来源。**
- 参数：`query: string`（关键词）、`category?: string`（资质/业绩/人员/产品/财务/检测报告…）、`project_id?: string`
- 返回：`[{asset_id, name, category, summary, fields:{…}, expires_at?, file_id}]`
- 备注：结果必须带 `asset_id` 引用；过期证照带 `expires_at` 且标注。

### list_assets
- 描述：分页列出企业资料（按目录层级）。
- 参数：`category?: string`、`page?`、`size?`

### get_asset
- 描述：读取单个资料详情（含关键字段提取结果）。
- 参数：`asset_id: string`
- 返回：详情 + 字段 + 原始文件定位（file_id/页码）。

## 2. 招标要求

### get_requirement
- 描述：读取项目招标解析结果（4.3 requirement），含定位坐标。
- 参数：`project_id: string`、`req_type?: string`（qualification/score_rule/reject_clause/tech_requirement/quote_rule/material_checklist…）
- 返回：`[{req_id, req_type, content, structured, coordinates:[{file_id,page_no,block_index}], confidence}]`

### list_requirements
- 描述：列出项目全部招标要求（按类型分组），生成/评标前先调用。

## 3. 项目材料

### list_project_materials
- 描述：当前项目招标材料列表（必传项），含解析状态。
- 参数：`project_id: string`

## 4. 成果读写

### get_deliverable_content
- 描述：读取成果指定版本的结构化内容（DocModel/SheetModel）。
- 参数：`deliverable_id: string`、`version_no?: int`（缺省当前版本）
- 返回：`{deliverable_id, deliverable_type, version_no, model}`

### save_deliverable
- 描述：保存成果新版本（version_type=AI生成/AI校核）。**写操作，必须带来源说明。**
- 参数：`deliverable_id: string`、`model: object`、`change_note: string`（本次改动摘要，写入版本记录）
- 返回：`{version_no, milestone}`
- 约束：调用前必须已通过 `get_deliverable_content` 读取过基准版本；后端校验 base_version_no。

## 5. 报价（只读建议）

### calculate_quote
- 描述：调用确定性算法服务计算建议价（三类策略），**只返回建议，不写入**。
- 参数：`material_ref: string`、`cost: number`、`min_profit_rate: number`、`strategy?: win|balance|profit`
- 返回：`{calc_id, suggested_price, score, gross_margin, risk_level, basis, sample_count}`
- 备注：sample_count < 5 时提示数据不足。

### get_history_price
- 描述：查询历史中标记录（按物料/地区/年份）。
- 参数：`material_name?`、`region?`、`year?`、`limit?`
- 返回：`[{material_name, spec, region, win_price, win_date, supplier}]`

## 6. 评标

### get_latest_score
- 描述：读取项目最新评分结果。
- 参数：`project_id: string`

### submit_score_items
- 描述：提交评分项（模拟评标 Skill 产出），**evidence 必须非空**（P4）。
- 参数：`project_id: string`、`items: [{name, got, full, improvable, suggestion, evidence, risk_level}]`

## 7. 搜索与引用

### search_web
- 描述：AnySearch 网络搜索（默认开启），返回结果带 URL 与摘要。
- 参数：`query: string`、`scope?: market|competitor|policy|standard`
- 返回：`[{url, title, snippet}]`

### save_source
- 描述：将搜索结果入库（search_source），判定 trust_level。
- 参数：`query`、`url`、`title`、`snippet`、`trust_level: 1|2|3`

### link_citation
- 描述：记录成果节点对搜索来源的引用（citation），前端"查看出处"依赖此。
- 参数：`deliverable_id`、`node_id`、`source_id`、`quote_text`

## 8. MCP server 实现约定

- 传输：stdio（本地子进程），`supports_parallel_tool_calls: true`
- 鉴权：server 启动时从环境读取 `BIDVOLT_INTERNAL_TOKEN`，调用后端 API 时带内部头
- 租户注入：MCP server 按 Hermes Profile（企业）与 Session（项目）解析租户，工具参数不接收 enterprise_id
- 错误语义：业务错误返回结构化 `{code, message}`；401 表示内部 token 失效
- 过滤：`hermes mcp configure bidvolt` 安装时按本清单勾选，不暴露未列工具
