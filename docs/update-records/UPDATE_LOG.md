# 更新记录

本文件是 BidVolt 的更新记录主体，按时间倒序记录每次更新。
新增更新时，请复制 `UPDATE_TEMPLATE.md` 中的模板，并插入到本文件“更新条目”的第一条位置。

<a id="2026-09-07-2157-fix-agent-chat-contract"></a>

## 2026-09-07 21:57 · fix · 补齐聊天消息关联、幂等与异常处理

| 字段 | 值 |
|---|---|
| id | 2026-09-07-2157-fix-agent-chat-contract |
| datetime | 2026-09-07T21:57:33+08:00 |
| type | fix |
| status | released |
| scope | agent, chat |
| related | issue #19, discussion #1, discussion #15 |

### 为什么做这次更新

项目 207 用户在任务完成后隔两天重进并发消息，出现长时间等待；最终回复是原始 Hermes 控制台输出（含 Reasoning 思考过程与 resume 横幅），而不是业务答复。数据库核对显示全部聊天事件 `reply_to_seq` / `client_message_id` 均为空，`message_id` 与 `reply_to_message_id` 同值，前端无法可靠关联消息与回复，重试也无法去重。

### 具体做了什么

- `chat_with_session`：user 事件与 hermes 回复事件分别取 seq，返回 `message_id=user_seq`、`reply_to_message_id=reply_seq`；回复事件写入 `reply_to_seq`。
- `POST /agent-run/{task_id}/chat` 与排队消息支持 `client_message_id`（≤100 字符）：同一标识只写入一条 user 事件；重试直接回放已有回复/失败结果，不重复执行（`duplicate=true`）。
- 运行异常不再冒充正常回复：Hermes 退出码非 0 返回 `status=failed`；输出为空或仅剩运行提示返回 `status=no_valid_reply`；超时写入 error 事件（`reply_to_seq` 关联本消息）后仍返回 409。
- `_clean_reply` 统一清洗回复：去 ANSI、Reasoning 框、会话尾注、框线与状态条噪音。
- `deploy/install-hermes.sh` 默认 `display.show_reasoning=false`，从源头关闭 Reasoning 复盘框（仅隐藏思考过程的显示：模型照常推理，思考内容仍结构化保留在 `/data/hermes/state.db` 的 `messages.reasoning_content`，调试可查，见 `docs/hermes/README.md` §6）。
- 新增 `tests/unit/test_agent_chat.py`（7 个用例）：回复清洗、`client_message_id` 去重、结果/失败回放。

### 影响范围

- `POST /projects/{project_id}/agent-run/{task_id}/chat` 请求/响应契约。
- 聊天事件表写入语义（`client_message_id`、`reply_to_seq`）。
- Hermes 配置（`display.show_reasoning`，需执行 `deploy/install-hermes.sh` 或 `hermes config set` 后生效）。

### 迁移 / 破坏性变更

- 无数据库迁移（`0028` 已含 `client_message_id` / `reply_to_seq` 字段）。
- 响应新增 `status`（queued/processing/processed/failed/no_valid_reply）、`error`、`duplicate` 字段；原有 `message`/`reply`/`session_id`/`message_id` 字段保留，前端旧逻辑不受影响。

### 验证方式

- 新增 7 个单元测试通过；全量测试 312 passed（3 个失败为既有环境问题：SQLite 全新迁移链缺 `agent_artifact` 建表、LibreOffice 容器转换失败，原始代码同样失败，与本次改动无关）。
- 服务器已部署（2026-09-07）：HEAD `5bcb15b`，app/worker 重启后 RUNNING，`GET /healthz` 返回 ok，`alembic current=0028 (head)`；Hermes 配置已写入 `display.show_reasoning: false`；部署代码核验通过（`client_message_id` / `_clean_reply` 均已上线）。
- 前端联调项：项目 207 续聊回复不再含 Reasoning/横幅，且 `message_id ≠ reply_to_message_id`。
- GitHub 提交：`731e26e`（行尾规范化）、`1bc8da0`（本修复）、`ee57be8`/`5bcb15b`（文档）。

### 回滚方式

回退提交 `1bc8da0`（若需一并恢复行尾则回退 `731e26e`），重启 app、worker。

<a id="2026-09-07-1640-feat-ai-enterprise-asset-classification"></a>

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

<a id="2026-09-05-2019-fix-office-remote-save"></a>

## 2026-09-05 20:19 · fix · 实现 Office 正式文件远端保存与版本管理

| 字段 | 值 |
|---|---|
| id | 2026-09-05-2019-fix-office-remote-save |
| datetime | 2026-09-05T20:19:11+08:00 |
| type | fix |
| status | released |
| scope | assembly, artifacts |
| related | issue #4, pr #12 |

### 为什么做这次更新

此前 Office 正式文件编辑仍依赖前端本机 Office bridge，保存只落在本机；用户换浏览器或换设备后无法看到已保存内容。本次更新把正式文件的编辑与保存落到远端，形成“编辑 → 保存 → 新版本/覆盖 → 历史版本可查”的闭环。

### 具体做了什么

- 新增 `POST /projects/{project_id}/assembly/artifacts/{artifact_id}/save`。
- 支持 `mode=overwrite` 覆盖当前文件、`mode=new` 另存新版本。
- 保存后返回新版本号、文件版本 ID、状态和下载入口。

### 影响范围

- 正式 Office 文件的远端保存与版本管理。
- 保存冲突时不再丢失当前编辑内容。

### 迁移 / 破坏性变更

- 随 PR #12 新增 `alembic/versions/0028_remaining_p0.py`。

### 验证方式

- 本机保存与正式保存分离。
- 另存不删除旧版本，覆盖不误改其他版本。
- 刷新或换设备后可下载已保存的新内容。

### 回滚方式

回滚 PR #12 中对应接口和迁移。

<a id="2026-09-05-2019-fix-chat-message-contract"></a>

## 2026-09-05 20:19 · fix · 补齐 Agent 聊天消息状态与回复关联契约

| 字段 | 值 |
|---|---|
| id | 2026-09-05-2019-fix-chat-message-contract |
| datetime | 2026-09-05T20:19:11+08:00 |
| type | fix |
| status | released |
| scope | chat, agent |
| related | issue #5, pr #12 |

### 为什么做这次更新

前端已有发送和流式输出，但缺少稳定的逐消息收件与回复关联。网络中断或请求较慢时，用户可能先看到“未收到”，随后消息又出现。本次更新让每次发送都可追踪、可恢复、可重试，且不产生意外重复。

### 具体做了什么

- `queue_chat_message` 返回 `message_id`、`status`。
- `chat_with_session` 返回 `message_id`、`reply_to_message_id`、`status`。
- `pre_chat` 返回 `message_id`、`status`。

### 影响范围

- Agent 聊天消息的收件、排队、回复关联与状态查询。

### 迁移 / 破坏性变更

- 无独立迁移，随 PR #12 的 `0028_remaining_p0.py` 处理相关结构。

### 验证方式

- 用户消息出现在先前内容之后，回复与该消息对应。
- 慢请求不会先误报“未收到”。
- 刷新后历史记录完整、不重复、不乱序。

### 回滚方式

回滚 PR #12 中对应接口修改。

<a id="2026-09-05-2019-fix-upload-batch"></a>

## 2026-09-05 20:19 · fix · 提供上传批次与逐文件解析状态

| 字段 | 值 |
|---|---|
| id | 2026-09-05-2019-fix-upload-batch |
| datetime | 2026-09-05T20:19:11+08:00 |
| type | fix |
| status | released |
| scope | files, upload |
| related | issue #6, pr #12 |

### 为什么做这次更新

上传、解析和材料确认此前缺少稳定的“本次上传/处理批次”标识，前端刷新或换浏览器后难以恢复真实状态。本次更新让企业资料上传、项目材料上传和 ZIP 解包具备可恢复的逐文件处理状态。

### 具体做了什么

- 新增 `upload_batch` / `upload_batch_item`。
- `POST /files/upload` 返回 `batch_id`。
- 新增 `GET /files/batches/{batch_id}` 查询逐文件处理结果。

### 影响范围

- 文件上传、解析状态与材料确认的可恢复能力。

### 迁移 / 破坏性变更

- 随 PR #12 新增 `alembic/versions/0028_remaining_p0.py`。

### 验证方式

- 成功项不因其他文件失败而丢失。
- 两次上传结果不混淆。
- 刷新后仍可查看批次和逐文件状态。

### 回滚方式

回滚 PR #12 中对应接口和迁移。

<a id="2026-09-05-2019-fix-review-quote-version-binding"></a>

## 2026-09-05 20:19 · fix · 让评审与报价绑定成果版本

| 字段 | 值 |
|---|---|
| id | 2026-09-05-2019-fix-review-quote-version-binding |
| datetime | 2026-09-05T20:19:11+08:00 |
| type | fix |
| status | released |
| scope | review, quotes |
| related | issue #7, pr #12 |

### 为什么做这次更新

评审和报价需要稳定对应成果版本。此前前端无法确认某次评分、某次报价是对哪一版文件生效，文件更新后旧结果可能被误当当前有效结果。

### 具体做了什么

- `ScoreRecord` 增加 `deliverable_versions`。
- 最新评分返回 `deliverable_versions`、`is_stale`、`stale_reasons`。
- `QuoteCalc` 增加 `deliverable_id`，calculate/apply/detail/list 返回该字段。

### 影响范围

- 评审、评分、报价计算与结果查询的版本绑定。

### 迁移 / 破坏性变更

- 随 PR #12 新增 `alembic/versions/0028_remaining_p0.py`。

### 验证方式

- 文件更新后，旧分数不会冒充当前有效分数。
- 下载的报价与用户确认的数值和版本一致。

### 回滚方式

回滚 PR #12 中对应接口和迁移。

<a id="2026-09-05-1955-fix-artifact-manifest"></a>

## 2026-09-05 19:55 · fix · 打通成果文件 manifest 哈希与版本映射

| 字段 | 值 |
|---|---|
| id | 2026-09-05-1955-fix-artifact-manifest |
| datetime | 2026-09-05T19:55:42+08:00 |
| type | fix |
| status | released |
| scope | agent, artifacts, response-package |
| related | issue #2, discussion #1, pr #11 |

### 为什么做这次更新

成果文件存在多套表示，前端无法稳定确认“单文件下载的内容”和“整包 ZIP 中同版本文件”是否一致，也无法从已有 ID 定位用户选中的真实文件版本。

### 具体做了什么

- Agent 打包 manifest 中的每个文件增加 `sha256`、`source_artifact_id`、`source_kind`、`version_no`、`mime`。
- `inspect_artifact` 对 zip 产物返回包内 `manifest.json` 内容，便于前端核对单文件与整包一致性。
- artifact 列表和详情增加 `status` 字段：普通产物 `ready`，响应包 zip `packaged`。
- 旧 `build_response_package` 的 manifest 文件清单也补充 `sha256`。

### 影响范围

- artifact manifest、版本映射与状态契约。

### 迁移 / 破坏性变更

- 无独立数据库迁移，主要为字段与响应结构补充。

### 验证方式

- 本地 `py_compile` 通过。
- 不改变 MCP capability 调用方式，保持既有兼容。

### 回滚方式

回滚 PR #11 对应的字段与响应结构补充。

<a id="2026-09-05-1929-fix-artifact-list-detail"></a>

## 2026-09-05 19:29 · fix · 冻结 assembly/artifacts 清单与详情契约

| 字段 | 值 |
|---|---|
| id | 2026-09-05-1929-fix-artifact-list-detail |
| datetime | 2026-09-05T19:29:07+08:00 |
| type | fix |
| status | released |
| scope | assembly, artifacts |
| related | issue #3, discussion #1, pr #8 |

### 为什么做这次更新

`GET /api/v1/projects/{project_id}/assembly/artifacts` 和 `.../artifacts/{artifact_id}/inspect` 在 OpenAPI 中返回宽泛 `object`，前端无法可靠渲染成果目录。

### 具体做了什么

- 为 `AgentArtifact` 增加 `version_no` 和 `updated_at`，覆盖修改时版本号递增。
- 新增 `app/schemas/agent.py`，冻结 artifact 列表和详情响应字段。
- 列表接口支持 `task_id`、`page`、`size` 查询参数，并返回 `group`、`filename`、`mime`、`version_no`、`is_internal`、`updated_at`、`download_url` 等字段。
- 详情接口返回与列表一致的元数据，预览字段保持原有结构。

### 影响范围

- assembly/artifacts 清单与详情接口的响应契约。

### 迁移 / 破坏性变更

- 新增 `alembic/versions/0027_agent_artifact_metadata`。

### 验证方式

- 本地 `py_compile` 通过。
- 普通 JWT 用户按项目权限访问，MCP capability 调用按任务过滤。

### 回滚方式

回滚 PR #8 对应的接口修改与迁移。
