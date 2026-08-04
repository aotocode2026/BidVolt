---

name: bidvolt-targeted-edit

description: 对成果做"AI 针对性修改"：仅针对用户选中的文字、章节、表格、图片、Excel 行列或单元格区域产出修改差异（JSON Patch），用户确认后由前端/后端应用，AI 不直接落版本。

version: 1.0.0

author: BidVolt Backend Team

license: proprietary

metadata:

  hermes:

    tags: [bidvolt, edit, diff, selection]

    related_skills: [bidvolt-bid-generate, bidvolt-mock-evaluate]

---

# 针对性修改（选区级）

根据用户选区与指令，产出**仅作用于选区**的修改差异。AI 只负责生成 diff，不直接写入版本。

## When to Use

- 用户在编辑器选中文字/章节/表格/图片/行列/单元格区域后下达修改指令
- 用户要求"只改这段 / 只优化这个表格 / 调整这几行报价"

## Quick Reference

| 工具 | 用途 |
|---|---|
| `get_deliverable_content` | 读取基准版本内容（含节点 id） |
| `search_assets` | 修改涉及企业事实时核对来源（P1） |
| `list_requirements` | 修改涉及招标要求时核对 |
| `search_web` / `save_source` / `link_citation` | 修改涉及外部信息时引用来源 |
| `save_deliverable` | **不用**——AI 不落版本，应用由用户确认后走后端接口 |

## Procedure

1. 接收输入：`selection`（选区：{type: text/table/image/sheet_range, refs: [node_id/单元格范围]}）+ `instruction`（修改指令）+ `base_version_no`。
2. `get_deliverable_content` 读取 base_version_no 内容，定位选区对应节点。
3. 只修改选区覆盖的内容，产出 JSON Patch 风格 diff：
   ```
   {operations: [
     {op: replace, node_id, before, after},
     {op: insert, after_node_id, node},
     {op: remove, node_id}
   ]}
   ```
4. diff 规则：
   - **只动选区**：选区外任何内容不得修改；如需连带修改，单独列出并说明理由。
   - 企业事实类修改：`after` 内容必须能溯源（search_assets 结果），否则拒绝并提示。
   - 报价类修改：数值改动必须附依据（calculate_quote 或用户提供的成本），并标注风险。
   - 引用外部信息：插入内容若来自搜索，附 citation。
5. 返回 diff（前端展示 before/after 差异，用户确认后应用）。

## Pitfalls

- **不直接应用**：本 Skill 永不调用 `save_deliverable`；应用由用户确认后通过后端 ai-edit apply 接口完成（形成新版本 + 审计）。
- 版本错位防护：若 base_version_no 不是当前版本，提示"版本已更新，请基于最新版本重新选择"。
- 选区为 Excel 区域时，diff 以单元格范围表达（H3:H8），避免整表替换。
- 用户指令模糊时，先输出理解确认（改什么、影响范围），不擅自扩大修改面。

## Verification

- diff 中所有 node_id 存在于 base_version 内容中。
- 抽查 after 内容：企业事实有来源、报价有依据、外部引用有 citation。
- 应用后的结果（前端/后端执行）与 diff 逐条一致。
