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

## 补全模式（Complement Pass）

当项目已有 requirements（首轮解析已入库），用户提出"补全 / 校核 / 查漏"任务时，按以下步骤操作：

1. **基线盘点**：`list_requirements` 输出通常超大（300KB+），会被后端落盘到 `/tmp/hermes-results/call_<hash>.txt`。**不要直接 read_file**，用 Python 解析（`json.loads(obj['result'])`），按 req_type 分桶、按 req_id 排序，定位已知覆盖与空白。
2. **扫漏维度**：用户给出的补全目标通常落在以下几类——
   - **资格条件**：具体年份（如"近三年"业绩）、注册资金、合同金额阈值、证据文件类型（合同封面/签字盖章页等）。
   - **评分细则**：价格 30 / 技术 60 / 商务 10 内的二级项目分值与阈值；区间平均价浮动法的 C/W1/W2/n 参数全集。
   - **商务文件格式**：模板章节标题（如"上传投标工具路径：…-选择分标-按钮'编辑'-…"）、采购人/代理机构全称字面与 metadata 校验。
   - **技术规范书**：模块接口清单、感知层/数据层/聚合层/应用层细节、典型场景的实施要求（多智能体协同、动态博弈、边缘仿真等）。
3. **不要覆盖**：upsert_requirements 同 content+不同 coordinates 会创建新 req（不去重），与"保留两版"语义天然契合。原版不动，新增坐标修正版即满足"如发现冲突内容，新增而不是更新"。

## Verification

- `list_requirements` 按类型核对：八类要求均有产出（无内容的类型可空，但需说明原因）。
- 抽查 3 条 requirement 的 coordinates，能定位到原文文件页码。
- **补全任务额外校验**：本次新增 req 覆盖用户要求补全的维度（逐项对照报告列出）；不与已有 req 内容完全重复（结构化字段允许微调，如补充 `evidence` / `range_summary` / `package_code` 等）。

## block_id 定位陷阱（重要）

`get_project_material_blocks(file_id, page, size)` 的 `page` 与 `size` 是 **doc_block 的分页索引**（不是 PDF 页码，也不是 block_id 范围）。例如：

- `page=2, size=100` → 拿到 block_index 100–199，对应 block_id 144567–144666（评审 / 监督 / 谈判流程）。
- `page=3, size=120` → 拿到 block_index 240–359，对应 block_id 144707–144825（响应文件格式章节）。

**绝对不要** 凭"page 3 应该继续 page 2 的 block_id"做线性外推。每次写入坐标前，先用 `get_project_material_blocks` 真实命中，验证 text 内容匹配后再引用该 block_id。

如果错引了一个 block_id（内容对不上），修正方法是**再写一条带正确 block_id 的新 req**，不要删旧版（满足"保留两版"），并在 `structured.correction_note` 字段里写明"坐标修正版"。

## Verification

- `list_requirements` 按类型核对：八类要求均有产出（无内容的类型可空，但需说明原因）。
- 抽查 3 条 requirement 的 coordinates，能定位到原文文件页码。
