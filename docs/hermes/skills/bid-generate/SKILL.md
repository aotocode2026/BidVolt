---

name: bidvolt-bid-generate

description: 生成或校核商务标、技术标、报价单三份成果。无已有标书时生成，有任意已有标书时校核（完整性、资格响应、技术响应、报价计算、否决风险、评分项覆盖、资料有效性、跨文件一致性）。企业信息只允许引用真实企业资料。

version: 1.0.0

author: BidVolt Backend Team

license: proprietary

metadata:

  hermes:

    tags: [bidvolt, generate, review, bid, deliverable]

    related_skills: [bidvolt-tender-parse, bidvolt-material-match, bidvolt-mock-evaluate]

---

# 标书生成与校核

本 Skill 是投标工作的核心：产出三份成果并保证一致性、可追溯、不虚构。

## When to Use

- 用户点击"开始生成"（无任何已有标书）
- 用户点击"开始校核"（已上传任意已有商务标/技术标/报价单）
- 用户要求重新生成或校核某个成果

## Quick Reference

| 工具 | 用途 |
|---|---|
| `list_requirements` | 招标要求基线 |
| `search_assets` / `get_asset` | 企业事实来源（**必须**用这里的数据） |
| `get_deliverable_content` | 读取已有成果（校核模式）或当前版本（取 version_id） |
| `save_deliverable` | 写新版本（expected_version_id CAS + idempotency_key + source_task_id） |
| `calculate_quote` / `get_history_price` | 报价建议（只建议不落库） |
| `search_web` / `save_source` / `link_citation` | 行情/竞对/政策/标准（带来源） |

## Procedure

### 判定模式
1. 查询项目是否有已有成果：有任意一份 → **校核模式**；一份都没有 → **生成模式**。
2. 校核模式：逐份读取已有成果（可只传部分），先检查完整性（缺失章节/文件），再进入校核项。

### 生成模式（或校核后生成缺失文件）
1. `list_requirements` 建立基线；`search_assets` 汇集企业事实（资质/业绩/人员/产品参数/财务）。
2. 三份成果可**并行**执行（MCP `supports_parallel_tool_calls`）：
   - **商务标**：商务响应、企业介绍、资质证明（只引用真实证照，标注有效期）、业绩表（真实业绩）
   - **技术标**：技术方案、供货范围、参数响应（对 tech_requirement 逐条响应）、质量/进度/服务承诺
   - **报价单**：按 quote_rule 填报价，价格建议来自 `calculate_quote`；仅无公式/无数据时给出 AI 参考区间（标注 is_ai_suggest + 依据/假设/置信度/风险，无追溯依据不输出数字）
3. 内容来源规则（**P1 禁止编造**）：
   - 企业名称、资质、业绩、人员、产品参数、成本：只允许来自 `search_assets`/`get_asset` 结果
   - 招标要求：来自 `list_requirements`
   - 市场/政策/标准：来自 `search_web` 且引用时 `link_citation` 记录来源
4. 写入前交叉一致性检查：企业名称、项目名称、金额、工期、数量、参数、税率，三份必须一致。
5. `save_deliverable` 保存（expected_version_id 取当前版本、idempotency_key、source_task_id、change_note 说明改动）。

### 校核项（校核模式）
文件完整性 → 资格响应（qualification 逐条核对）→ 技术响应（tech_requirement 逐条）→ 报价计算（重算公式）→ 否决风险（reject_clause 逐条扫）→ 评分项覆盖（score_rule）→ 资料有效性（有效期/缺失）→ 跨文件一致性。
校核结果输出问题清单，然后可生成缺失文件/补充章节（不新增第三种模式）。

## Pitfalls

- **绝不虚构企业事实**：无资料支撑的资质/业绩/参数一律不写；宁可留空并列入缺失清单。
- 报价：有评分公式走 `calculate_quote` 确定值；无公式的 AI 参考区间必须标注"AI 建议（区间）"、附依据/假设/置信度/风险，**无可追溯依据不输出数字**。
- 校核模式不得覆盖原始上传版本——`save_deliverable` 永远生成新版本（expected_version_id CAS）。
- 一致性是硬门槛：三份成果任一关键字段不一致，先修复再保存。
- 低可信搜索来源（trust_level=3）不得写入正文，只能作为提示。

## Verification

- `save_deliverable` 返回的新版本可读回，抽查关键字段（企业/项目/金额/工期/参数/税率）三份一致。
- 随机抽查 5 处企业事实描述，能在企业资料中找到来源 asset_id。
- 校核模式下：问题清单逐条有定位（文件/章节/坐标）。
