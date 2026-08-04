---

name: bidvolt-mock-evaluate

description: 模拟评标：组合招标评分细则与系统内置评审规则对三份成果打分，输出综合/商务/技术/报价分、否决风险数、缺失材料数与预计可提升分值；每个评分项必须携带证据定位。

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
| `submit_score_items` | 写入评分项（evidence 必填） |
| `search_assets` | 核对证明材料 |
| `search_web` / `link_citation` | 外部标准/政策核对（带来源） |

## Procedure

1. 确认评估对象：三份成果当前版本号（`get_deliverable_content`）。
2. 规则来源与优先级：
   - **第一优先**：招标评分细则（score_rule）——按结构化公式确定性计算
   - **补充**：内置评审规则——否决风险、文件完整性、资料有效性、跨文件一致性、方案完整性、内容针对性、报价合理性、格式规范
   - 冲突时以招标要求为准，内置规则仅作补充检查
3. 逐评分项打分，每项必须给出：
   - `got` / `full` / `improvable`
   - `evidence`：{requirement:[坐标], deliverable:[节点], material:[asset_id/file_id], search?:[url]}
   - `suggestion`：优化点与修改建议
   - `risk_level`
4. 汇总输出：综合分、商务分、技术分、报价分、否决风险数、缺失材料数、预计可提升分值。
5. `submit_score_items` 写入（evidence 为空的项目丢弃并提示）。
6. 若为"提升建议闭环"场景，输出每条建议的：当前得分、预计提升、关联文件/章节、风险等级、处理方式。

## Pitfalls

- **证据强制**：评分项没有证据定位（招标原文/成果节点/企业材料）时，不计入可解释总分，并显式提示"该评分项缺乏依据"。
- 招标未明确评分的维度，用内置规则评估并标注"内置规则补充"。
- 否决条款（reject_clause）命中任何一项，综合分标注"否决风险"，不得隐藏。
- 模拟评标仅供参考：输出必须带说明"不代表最终评审结果"。
- 提升建议必须可执行：每条建议对应一个具体评分项和一个可定位的成果位置。

## Verification

- 输出评分项总数 = 招标评分细则项数 + 内置规则命中项数。
- 抽样 5 个评分项，evidence 均可点击定位到原文/节点/材料。
- 否决风险数与 reject_clause 命中数一致。
