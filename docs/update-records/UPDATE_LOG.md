# 更新记录

本文件是 BidVolt 的更新记录主体，按时间倒序记录每次更新。
新增更新时，请复制 `UPDATE_TEMPLATE.md` 中的模板，并插入到本文件“更新条目”的第一条位置。

## 2026-09-07 16:40 · feat · AI 企业资产分类与人工确认闭环

| 字段 | 值 |
|---|---|
| id | 2026-09-07-1640-feat-ai-enterprise-asset-classification |
| datetime | 2026-09-07T16:40:35+08:00 |
| type | feat |
| status | released |
| scope | enterprise_asset, enterprise_api, frontend |
| related | issue #18 |

### 为什么做这次更新

原有企业资产分类仅依赖文件名关键词，用户上传文件不会按固定规则命名，导致大量资产落入“其他”，影响后续标书生成的证据检索与装订质量。

### 具体做了什么

- 新增 `app/services/asset_classification_service.py`：
  - 图片资产复用现有 `qwen-vl` 图片描述结果，按 `doc_type` 映射到企业资产分类；
  - 文档资产读取正文，调用 MiniMax 文本模型分类；
  - 压缩包/其他资产回退到文件名规则作为兜底。
- 修改 `app/api/enterprise.py`：
  - `POST /enterprise/ingest` 与 `POST /enterprise/assets/{asset_id}/classify` 改为调用 AI 分类；
  - 完善 `PATCH /enterprise/assets/{asset_id}/category`，人工修改时同步更新 `category_id`、`asset_type` 和状态；
  - 新增 `POST /enterprise/assets/{asset_id}/confirm-category`，用于确认 AI/人工分类。
- 修改前端 `app/static/app.js`：
  - 企业资料列表新增“确认”和“改分类”操作；
  - AI 分类结果直接展示，前端人工修改后以前端结果为准。

### 影响范围

- 企业资料导入与单条分类接口。
- 企业资料前端列表。
- AI 分类结果在未确认时即可生效，人工修改后置为已确认。

### 迁移 / 破坏性变更

无数据库迁移。分类字段继续使用现有 `enterprise_asset.category_id / asset_type / status`。

### 验证方式

- 服务器部署后 `app`、`worker` 重启成功；
- `GET /healthz` 返回 `{"status":"ok"}`；
- GitHub 提交：`dbbb030`、`26265d5`；
- 前端可调用确认与改分类接口。

### 回滚方式

回退提交 `dbbb030` 与 `26265d5`，并重启 `app`、`worker`。

## 2026-09-07 15:38 · docs · 建立更新记录文档体系

| 字段 | 值 |
|---|---|
| id | 2026-09-07-1538-docs-create-update-records |
| datetime | 2026-09-07T15:38:49+08:00 |
| type | docs |
| status | released |
| scope | docs |
| related | 无 |

### 为什么做这次更新

此前项目缺少统一的更新记录，无法按时间、issue 或 discussion 快速追溯每次变更。本次新增一套轻量的更新记录文档，方便开发者和其他 agent 阅读与检索。

### 具体做了什么

- 新增 `UPDATE_LOG.md`：主体更新记录，按时间倒序维护。
- 新增 `UPDATE_TEMPLATE.md`：单条更新模板，统一字段与格式。
- 新增 `REFERENCE_INDEX.md`：反向索引，按 issue / discussion / PR 反查更新。

### 影响范围

- 仅新增 `docs/update-records/` 文档目录，不影响业务代码。

### 迁移 / 破坏性变更

无。

### 验证方式

- 确认三个文件可正常阅读。
- 后续新增更新时，同步更新 `UPDATE_LOG.md` 与 `REFERENCE_INDEX.md`。

### 回滚方式

删除 `docs/update-records/` 目录即可。
