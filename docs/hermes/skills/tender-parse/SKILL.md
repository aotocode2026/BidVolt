---

name: bidvolt-tender-parse

description: 解析投标项目当前招标材料，识别文件类型（招标公告/技术规范书/评分办法/报价模板/补遗澄清等），抽取资格要求、评分细则、否决条款、技术要求、报价规则与提交材料清单，每条结果绑定原文坐标。

version: 1.0.0

author: BidVolt Backend Team

license: proprietary

metadata:

  hermes:

    tags: [bidvolt, tender, parse, requirement]

    related_skills: [bidvolt-material-match, bidvolt-bid-generate]

---

# 招标材料解析

将项目当前招标材料（文件已由后端完成文本提取，扫描件已由视觉模型识别）解析为结构化招标要求，供匹配、生成、评标使用。

## When to Use

- 项目上传了新的当前招标材料
- 用户要求"解析招标文件 / 梳理招标要求"
- 生成或评标前需要招标要求基线

## Quick Reference

| 工具 | 用途 |
|---|---|
| `list_project_materials` | 列出项目待解析材料（含解析状态） |
| `get_project_material_blocks` | 读取 doc_block 文本块（带坐标） |
| `get_requirement` / `list_requirements` | 读取/检查已解析要求（先查再写，避免重复） |
| `upsert_requirements` | 写入/更新 requirement（带坐标与置信度，记 revision） |
| `save_source` / `link_citation` | 引用网络公开要求时（如政策标准）记录来源 |

## Procedure

1. `list_project_materials` 获取项目材料列表，过滤 `status=已解析` 的文本块。
2. `get_project_material_blocks` 读取各文件 doc_block（带坐标）。
3. 按文件内容判断类型（招标公告、招标主文件、技术规范书、报价模板、评分办法、资格审查文件、补遗、澄清、合同条款、图纸、工程量清单、其他）。
4. 抽取以下要求，经 `upsert_requirements` 写入（保持坐标）：
   - **basic_info**：项目名称、招标编号、招标人、截止时间
   - **qualification**：资格要求（资质等级、业绩、人员、财务状况）
   - **score_rule**：评分细则（商务/技术/报价分权重、打分公式），能结构化的写入 `structured`
   - **reject_clause**：否决条款（必须逐条记录）
   - **tech_requirement**：技术要求（参数、标准、偏离处理）
   - **quote_rule**：报价规则（上限、评分公式、税率要求）
   - **material_checklist**：提交材料清单
   - **attachment**：图纸、清单等附件说明
5. 每条要求必须保留 `coordinates`（原文定位），置信度低于 0.7 的标注 `confidence` 并标记"需人工确认"。
6. 补遗/澄清要求**优先于**主文件要求，覆盖冲突时记录 supersedes 关系（revision）。

## Pitfalls

- **不要编造**：文件里没有的内容绝不写入；扫描件识别不清时标低置信度，不猜测。
- **不重复写入**：先 `list_requirements` 查重，同一文件重传时更新而非新增。
- **坐标必填**：写 requirement 时 coordinates 为空视为失败。
- 识别为"图纸/工程量清单"等非文本材料时，只登记 attachment 类型，不强行抽取要求。

## Verification

- `list_requirements` 按类型核对：八类要求均有产出（无内容的类型可空，但需说明原因）。
- 抽查 3 条 requirement 的 coordinates，能定位到原文文件页码。
