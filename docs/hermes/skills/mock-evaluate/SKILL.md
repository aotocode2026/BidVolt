---

name: bidvolt-mock-evaluate

description: 模拟评标（ReviewProvider）：通过 Document/Code Provider 对三份成果确定性打分，LLM 只做带证据的事实判断（允许 unknown/abstain），输出综合/商务/技术/报价分、否决风险数、缺失材料数与预计可提升分值；每个评分项必须携带服务端校验的 EvidenceRef。

version: 1.0.0

author: BidVolt Backend Team

license: proprietary

metadata:

  hermes:

    tags: [bidvolt, evaluate, score, review]

    related_skills: [bidvolt-tender-parse, bidvolt-bid-generate]

---

# 模拟评标

以评审视角评估当前成果版本，产出可解释、可定位的评分结果与提升建议。

## When to Use

- 用户点击"模拟评标"或查看"当前得分"
- 成果生成/校核/修改后重新评分
- 用户问"现在能得多少分 / 怎么提分"

## Quick Reference

| 工具 | 用途 |
|---|---|
| `list_requirements` | 评分细则 score_rule（**优先**） |
| `get_deliverable_content` | 读取三份成果当前版本 |
| `get_latest_score` | 读取上一次评分（对比提升） |
| `get_review_items` | 读取逐条评分项（含状态、材料关联） |
| `submit_score_items` | 写入评分项（snapshot_id + ruleset_version，evidence 为 EvidenceRef） |
| `confirm_review_items` | 批量确认 review_items（触发合并写入 + 受影响项重审） |
| `search_assets` | 核对证明材料 |
| `search_web` / `link_citation` | 外部标准/政策核对（带来源） |

## Procedure

### 首次评标

1. 确认评估对象：三份成果固定版本（`get_deliverable_content`，取 version_id）+ snapshot_id。
2. Provider 与优先级：
   - **Document Provider（第一优先）**：招标评分细则（score_rule）→ 版本化规则 → 确定性规则 DSL 计算
   - **Code/Rule Provider（补充）**：内置评审规则——否决风险、完整性、资料有效性、跨文件一致性、方案完整性、内容针对性、报价合理性、格式规范；沙箱隔离执行
   - 冲突时以招标要求为准，内置规则仅作补充；**内置检查默认只产生风险，不直接叠加到正式总分**
3. 逐评分项评估，每项必须给出：
   - `category` / `problem_description`
   - `got` / `full` / `improvable`（improvable 是"预计可提升分"，不是直接加分）
   - `evidence`：**EvidenceRef**（{source_version_id, content_hash, source_range, exact_quote, claim_id}，服务端生成校验）
   - `suggestion`：优化点与修改建议
   - `action_type`：upload_material / edit_deliverable / manual_review
   - `risk_level` + `confidence`（LLM 无法判断时输出 unknown/abstain，不猜测）
   - `requirement_id` / `related_deliverable_node`（关联招标要求和成果节点）
4. `submit_score_items` 写入（带 snapshot_id + ruleset_version；evidence 非空且服务端校验通过，否则丢弃并提示）。初始 status = pending_confirm。
5. 汇总输出由逐条 items 计算（不直接存）。

### 提升闭环（用户确认后触发，架构简化 D15）

当用户通过 `confirm_review_items` 批量确认了若干 review_items 后：

1. 后端自动合并同一成果的多条修改，生成**一个**新 deliverable_version（CAS 409 防静默覆盖）
2. 后端自动触发受影响项重审（`re-evaluate`），只跑被确认的 review_items 关联的成果/章节
3. 生成新 review_run + 新 review_item revisions（status = re_reviewed）
4. 前端对比新旧得分

**Agent 不直接参与确认/重审流程**——这些由后端服务编排，Agent 只在首次评标和后续说明/建议时介入。

## Pitfalls

- **证据强制**：评分项没有 EvidenceRef（招标原文/成果节点/企业材料）时，不计入可解释总分，并显式提示"该评分项缺乏依据"。
- **数值 vs 语义分工**：公式计算交给 DSL/Provider 确定性执行；LLM 只做带证据的事实判断，判断不了就输出 unknown，**不编造分数**。
- 招标未明确评分的维度，用 Code Provider 评估并标注"内置规则补充"。
- 否决条款（reject_clause）命中任何一项，综合分标注"否决风险"，不得隐藏。
- 模拟评标仅供参考：输出必须带说明"不代表最终评审结果"。
- 提升建议必须可执行：每条建议对应一个具体评分项和一个可定位的成果位置。

## Verification

- 输出评分项总数 = Document Provider 项数 + Code Provider 命中项数。
- 抽样 5 个评分项，evidence 均为服务端生成的 EvidenceRef，可点击定位到原文/节点/材料。
- 否决风险数与 reject_clause 命中数一致。
- LLM 输出的 unknown 项有明确说明，无编造分数。
