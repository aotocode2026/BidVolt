---
name: bidvolt-evidence-extract
description: 按 evidence_plan.md 从企业资料库批量提取证据扫描件到本地 evidence/<key>/（download_project_material 批量下载 + PDF 150dpi 渲染 + 跨组 hardlink 复用），用 get_asset 结构化描述为主、vision 为辅复核关键字段（发票方向/合同主体/证件/资质/社保逐人），产出 evidence_check_report.md（逐条可否装订）。触发词：证据提取/装订预提取/evidence_plan/可否装订/发票方向复核/证据扫描件。
---

# bidvolt 证据装订预提取（evidence extract + check）

## 触发条件
BE 子 agent 拿到 evidence_plan.md（marker key → asset_id 分组表），要求把证据扫描件从企业资料库提取到 `/data/hermes/work/本项目/evidence/<key>/`、vision/结构化复核关键字段、产出 evidence_check_report.md（每条可否装订）。位置：盘点摸底（bidvolt-asset-inventory）之后、装配 docx（assemble_docx.py --manifest）之前。

## 输入
- evidence_plan.md：marker key 表（业绩1.x / 人员-xx / 资质-xx / 专利-xx / 商务-xx），每 key 列 asset_id 分组（合同/发票/查验、证件、社保、证书）+ 页数下限 + ⚠️ 不可装订标注
- assets_full_listing.txt + list_assets 持久化输出：asset_id → source_file_id + name

## 标准流程
1. **建组**：按 plan 把 asset_id 分组（含子类：合同/发票/查验/身份证/学历/证书…），写 `evidence/_groups.json` 与 `evidence/_download_plan.json`（含 download/copy 标记）。
2. **映射**：list_assets 持久化输出的 result 字段是**字符串化的 JSON** → `json.loads` 两次拿 asset_id→source_file_id（一次 loads 得到的是 str，直接下标会炸）。
3. **去重**：跨组 asset（同图出现在 2 个 marker key，如资质商务卷复件、名称变更 对应资产、对应人员身份证 对应资产编号）只下载一次（primary），次组用 `os.link` hardlink——省盘且同 inode，装配计数互不影响。
4. **批量下载**：download_project_material(file_id, save_path) 每批 30 个并行；目标目录先 `mkdir -p`（否则 Errno 2）；文件名 `{seq:02d}_{asset_id}_{子类}_{原名}` 保可溯源性；下载前检查目标已存在则跳过（幂等续传）。400+ 文件 ≈ 15 批，节奏可控。
5. **格式**：jpeg/png 直接复制（Word 可插）；PDF 用 `import pymupdf`（fitz 已弃用警告）逐页渲染 PNG 150dpi；html 证据（如创新型公示）保留原件并在报告注明「装配前需转图」，别强转。
6. **跨组复制枚举要全**：_download_plan.json 里 op=copy 的**每一条**都要落地（本类实战漏过 1303→商务-财务说明书，因只盯了「明显」的复件组）。生成复制脚本时直接遍历 plan 的 copy 项，别手写清单。
7. **复核（主通道 = get_asset image_description）**：
   - 发票：逐张 get_asset，text_summary 直接给「购方/销方/金额/发票号」→ 判定方向
   - 合同：首页 + 签署页 get_asset；签署页找 信用代码+银行账号+法人签字 三锚点
   - 人员/资质/专利/主体/审计/社保：get_asset 封面/关键页即可，不必逐页
8. **写 evidence_check_report.md**：按 marker key 逐条结论（✅ 可装订 / ⚠️ 条件装订(附佐证/待人工核) / ❌ 不可装订+原因），附「发票方向逐张判定表」「待人工核原件清单」「提取计数回执」。

## 可装订判定规则
- **发票方向（核心，逐张判）**：销方=投标主体（含旧名）→ 方向正确，可作本业绩收入证据；销方=甲方 → **方向反（进项发票）**，不可作收入证据，逐张列 asset_id；销方=第三方（既非甲方也非我方）→ 错票，不可装订。判定依据 text_summary 购方/销方字段 + numbers 里的双方信用代码交叉。本次 80 张发票约 26 张方向正确、50 张反、3 张错票——**方向反是常态不是个例**，逐张列出。
- **OCR 名称变体一律不信**：国客/国基/国泰/图鉴/酷客/医客/团客/百客/图洛/图森/墨图/爱图客… 全是「图客未来」误读；甲方同样有变体。锚点 = 统一社会信用代码 91110111318157964Q + 银行账号 11001117300052507773。
- **三件套**：合同+发票+查验缺一不装订（1.20 仅合同 → ❌）。
- **旧名证据**：2022-03-16 前签署的合同/发票（北京爱图客科技）须附名称变更通知（76/191/834）才可装订。
- **社保逐人**：get_asset 的 people+numbers 字段直接给姓名+身份证号，按页映射团队成员在册情况（无需逐页 vision）。
- **证件姓名 OCR 变体**（学历证「某OCR变体」vs 身份证「资料库对应人员」）：标「待人工核原件」，不自行定案。
- **有效期**：以项目截止日核证书；过期证书（ISO27001）不装订，写作卷以声明处理。
- **签署页与首页冲突**：以签署页为准并标注待核（1.2 首页 2025.7 vs 签署页 2023-07-03）。

## Pitfalls（2026-08 实战）
- **vision 网关 502 降级**：vision_analyze_minimax 连续 2-3 次失败即停止重试（每次重试重置冷却），改用 get_asset 结构化描述完成复核（入库后台 vision 已产出缓存），报告注明复核通道；工具恢复后再补 vision 抽查存疑件。
- **get_asset OCR 噪声**：text_summary 常把购方/销方读反或读重（「销售方=博瑞森，购买方=博瑞森」实为误读）；金额/发票号/信用代码字段相对可靠。方向判定结合 numbers 双代码交叉，存疑单列 vision/人工核，别在报告里写死。
- **大件批量**：审计整本（24-28 页/年）、gsxt（34 页）逐页下载是必要成本，但复核只做封面 get_asset 确认事务所/报告号（2023/2024 厚立 F02061/F01067、2025 锡轩所——同项目三年可能不同所，勿假设）。
- **社保页数≠计划页数**：plan 写 8 页可能混入证件页；以 listing 文件名 + get_asset 内容为准（本次实际 7 页明细 + 1 页备注说明）。
- **pdf 资产先验 magic bytes**：扫描型 PDF 下载后可能是 JPEG magic（pypdf 报错）→ 按图处理，别反复试解析。

## 与相邻 skill 的分工
- bidvolt-asset-inventory(+pitfalls)：摸底/盘点（有什么可用），产出 analysis_assets.md；其中已含信用代码锚点、名称变更件、合同三页抽样法、OCR 变体库——本 skill 直接引用其结果，不重复取证。
- 本 skill：按 evidence_plan 提取到本地 + 装订前复核（能否装订），产出 evidence_check_report.md + 供 assemble_docx.py --manifest 的图片路径。

## 支持文件
- references/某项目-evidence-extract.md —— 本项目全量执行记录：分组方案、下载计数、发票方向逐张判定表、社保逐人映射、待人工核清单。
