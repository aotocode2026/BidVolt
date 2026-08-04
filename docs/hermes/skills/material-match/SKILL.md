---

name: bidvolt-material-match

description: 将招标要求与企业资料库匹配，产出已匹配资料、可能缺失资料、需要补充内容和预计影响分值；缺失项必须关联具体招标要求与评分项。

version: 1.0.0

author: BidVolt Backend Team

license: proprietary

metadata:

  hermes:

    tags: [bidvolt, material, match, missing]

    related_skills: [bidvolt-tender-parse, bidvolt-bid-generate]

---

# 企业资料匹配与缺失检测

基于招标要求（4.3 requirement）检索企业资料库，给出匹配结果与缺失清单，为生成/校核提供输入。

## When to Use

- 招标要求解析完成后、开始生成/校核前
- 用户补充资料后重新匹配
- 用户问"还缺什么材料"

## Quick Reference

| 工具 | 用途 |
|---|---|
| `list_requirements` | 读取资格要求、评分项、材料清单 |
| `search_assets` | 按关键词/类别检索企业资料 |
| `list_assets` | 浏览企业资料库目录 |

## Procedure

1. `list_requirements` 获取 qualification / score_rule / material_checklist / tech_requirement。
2. 逐项对每个要求执行 `search_assets` 检索：
   - 完全匹配（名称/类别/关键字段命中）→ 已匹配
   - 部分匹配（类别对但关键字段缺失，如业绩数量不足）→ 部分匹配，注明缺口
   - 无匹配 → 缺失
3. 输出匹配清单，格式（经 MCP 写入或返回给任务编排）：
   ```
   {requirement_ref, asset_id?, matched: yes|partial|no,
    gap_desc?, affected_score_item?, impact_score?, suggestion}
   ```
4. 缺失项必须关联：具体招标要求、目标文件（资质/业绩/人员/产品参数）、预计影响分值。
5. 资料不完整时**继续允许生成**（不阻塞），但缺失清单必须随生成结果一并展示。

## Pitfalls

- **只报告真实缺失**：检索不到 ≠ 不存在，先扩大检索词（别名/简称）再判定缺失。
- **不得虚构**：绝不把缺失项标记为已匹配，更不生成虚假的企业资质/业绩/参数。
- 证照类匹配注意 `expires_at`：即将过期（<90 天）标记"需更新"。
- 部分匹配的影响分值按评分细则估算，标"估算"。

## Verification

- 每个缺失项都能回答：缺什么、对应哪条招标要求、影响哪个评分项、预计扣几分。
- 抽查已匹配项的 asset_id 真实存在。
