# 更新记录

本文件是 BidVolt 的更新记录主体，按时间倒序记录每次更新。
新增更新时，请复制 `UPDATE_TEMPLATE.md` 中的模板，并插入到本文件“更新条目”的第一条位置。

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
