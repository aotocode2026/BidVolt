# 更新记录

本文件是 BidVolt 的更新记录主体，按时间倒序记录每次更新。
新增更新时，请复制 `UPDATE_TEMPLATE.md` 中的模板，并插入到本文件“更新条目”的第一条位置。

<a id="2026-09-09-1153-fix-agent-task-state-consistency"></a>

## 2026-09-09 11:53 · fix · Agent 主会话任务状态一致性（终态/重试/回收）

| 字段 | 值 |
|---|---|
| id | 2026-09-09-1153-fix-agent-task-state-consistency |
| datetime | 2026-09-09T11:53:00+08:00 |
| type | fix |
| status | deployed |
| scope | agent-pipeline, task |
| related | issue #42, discussion #38 |

### 为什么做这次更新

任务 7805/7812 出现 `running` + `done/100` + `result.outcome=incomplete` 并存、重试后旧结果未清理的混合状态。根因：主会话终态结果/进度经独立会话先落库，任务终态后提交；异常/主会话提交失败/租约回收都会重新入队，而旧结果不清理、尝试不区分。

### 具体做了什么

- “未闭环（incomplete）”改为终态业务失败：写完 result 后抛 `TerminalTaskError(code=agent_incomplete)`，`run_task` 直接置 `FAILED_TERMINAL`、不重试，状态/错误/进度与结果一致，不冒充成功。
- `reclaim_stale` 发现 agent_pipeline 任务已写出 `outcome=complete/incomplete` 时按结论收尾（DONE / FAILED_TERMINAL），不整条管线重跑。
- 普通重试入队时清空上一轮 `error`/`finished_at`，避免旧失败被误读为当前状态。
- 结果写入 `attempt`（当前尝试序号）；`GET /projects/{id}/agent-run/{task_id}` 新增 `retry_count`、`generation`。

### 影响范围

- agent_pipeline 收尾/异常路径、任务状态机（run_task/reclaim_stale）、agent-run 详情响应。

### 迁移 / 破坏性变更

- 无数据库迁移。

### 验证方式

- 新增 3 个测试（重试清旧错误、回收按结论收尾 complete/incomplete 不重跑）；相关 13 个用例全绿，ruff 通过；
  全量测试（跳过会真实拉起 hermes 子进程的 `test_bid_generate_api.py`）333 passed，3 个失败为既有环境问题，与本次无关。

### 回滚方式

回退本次提交并重启 app/worker。

<a id="2026-09-09-1033-ops-restore-deepseek-credential"></a>

## 2026-09-09 10:33 · ops · 配置 DeepSeek 凭据并恢复 Agent 主会话生成

| 字段 | 值 |
|---|---|
| id | 2026-09-09-1033-ops-restore-deepseek-credential |
| datetime | 2026-09-09T10:33:00+08:00 |
| type | ops |
| status | released |
| scope | agent-pipeline, hermes |
| related | issue #41, discussion #37 |

### 为什么做这次更新

配置缺失的 DeepSeek 凭据，恢复被 Discussion #37 阻断的 Agent 主会话生成。

### 具体做了什么

- 将 `DEEPSEEK_API_KEY` 写入 `/data/hermes/.env`（与既有 MINIMAX/DASHSCOPE 密钥同处，权限 600；**不进入 Git 仓库**）；
- 重启 `hermes` 服务；模型保持 flash 模式（`config.yaml`：`provider=deepseek`、`default=deepseek-v4-flash`）；
- README 凭据说明改为与实际一致的“HERMES_HOME/.env 或 supervisor environment= 注入，不进仓库 .env”。

### 影响范围

- Agent 主会话生成（agent_pipeline）恢复可用；任务 7805/7812 可由用户重新发起生成。

### 迁移 / 破坏性变更

- 无代码/数据库变更。

### 验证方式

- 后端凭据预检实测 `credential_available=True`；
- 最小真实模型调用 `hermes chat -q "请只回复两个字：OK"` 返回 `OK`（DeepSeek flash 模式）；
- app/worker/hermes RUNNING，`GET /healthz` ok。

### 回滚方式

从 `/data/hermes/.env` 移除 `DEEPSEEK_API_KEY` 并重启 hermes（不影响仓库）。

<a id="2026-09-09-1025-fix-agent-credential-fail-fast"></a>

## 2026-09-09 10:25 · fix · Agent 主会话模型凭据缺失快速失败

| 字段 | 值 |
|---|---|
| id | 2026-09-09-1025-fix-agent-credential-fail-fast |
| datetime | 2026-09-09T10:25:00+08:00 |
| type | fix |
| status | released |
| scope | agent-pipeline, task |
| related | issue #41, discussion #37 |

### 为什么做这次更新

项目 215/216 生成失败（任务 7805/7812）：主模型已切 DeepSeek，但 `DEEPSEEK_API_KEY` 未配置到 Hermes 可见位置，生成报 `No usable credentials found for provider 'deepseek'`；任务按普通异常重试 3 次、长时催办后以“未闭环”收尾，状态接口没有可直接处理的失败原因。

### 具体做了什么

- `run_agent_pipeline` 启动前预检 `DEEPSEEK_API_KEY`（进程环境或 `HERMES_HOME/.env`），缺失立即快速失败；REPL 运行中命中凭据错误标记同样终止。
- 新增 `TerminalTaskError`：确定性失败直接 `FAILED_TERMINAL`，不消耗重试、不重新入队、不无效催办。
- 失败原因安全可读：`error={code: model_credentials_unavailable, message}`、进度 `hint` 给出“管理员配置 DEEPSEEK_API_KEY 后重新发起生成”，不含密钥/内部细节。
- 存量任务 7805/7812 资料与历史保留，凭据配置后由用户重新发起生成，后端不静默重复执行。

### 影响范围

- agent_pipeline 启动/运行路径、`run_task` 异常状态机（新增 TerminalTaskError 分支）。

### 迁移 / 破坏性变更

- 无数据库迁移。

### 验证方式

- 新增 4 个测试（凭据来源探测 ×3、TerminalTaskError 终态不重试 ×1）；相关 14 个用例全绿，ruff 通过。
- 服务器已部署（2026-09-09）：HEAD `254eae4`，无数据库迁移；app/worker RUNNING，`GET /healthz` ok；
  生产环境预检实测 `credential_available=False`（凭据缺失将被识别并快速失败）。
- **待外部配置**：`DEEPSEEK_API_KEY` 尚未提供，生成恢复仍需配置密钥并做最小真实模型调用验证（见 issue #41）。
- GitHub 提交：`254eae4`（代码 + 文档）。

### 回滚方式

回退本次提交并重启 app/worker。

<a id="2026-09-08-2243-fix-market-knowledge-platform"></a>

## 2026-09-08 22:43 · fix · 行情库改为平台共享（所有用户可见）

| 字段 | 值 |
|---|---|
| id | 2026-09-08-2243-fix-market-knowledge-platform |
| datetime | 2026-09-08T22:43:00+08:00 |
| type | fix |
| status | released |
| scope | market-knowledge, bid-generate |
| related | issue #34, discussion #26 |

### 为什么做这次更新

口径修正：行情库应为平台共享内容，所有登录用户均可查看，不再按企业分类；上传/删除/重试等管理操作由平台管理员执行。

### 具体做了什么

- 列表/详情/要点接口移除企业过滤：任何登录用户可见全平台行情资料与提炼要点；
- Agent 参考出口（provider 策略 `all`）改为全平台要点，不再按企业过滤；
- 提炼任务与删除操作按资料 ID 直查（不再校验操作者企业），管理仍要求管理员权限 `market_knowledge.manage`；
- 迁移 `0035`：移除 3 张行情库表的企业级 RLS 策略并 `NO FORCE ROW LEVEL SECURITY`（`enterprise_id` 保留为上传者溯源字段）。

### 影响范围

- 行情库可见范围（扩大为全平台）、提炼任务/删除的租户校验、Agent 生成参考内容来源。

### 迁移 / 破坏性变更

- 迁移 `0035`：仅 PG 上移除 3 张表的 RLS 策略；无列/数据变更。

### 验证方式

- 新增跨企业可见性与跨企业管理员删除测试；行情库相关 11 个测试全绿，ruff 通过。
- 服务器已部署（2026-09-08）：HEAD `5831d2c`，迁移 `0035 (head)`（3 张行情库表 RLS 策略已移除、`pg_policies` 计数 0），
  迁移前 DB 备份完成；app/worker RUNNING，`GET /healthz` ok。
- GitHub 提交：`5831d2c`（代码 + 文档）。

### 回滚方式

回退迁移 `0035`（恢复 RLS 策略）并回退本次提交，重启 app/worker。

<a id="2026-09-08-2156-feat-market-knowledge-library"></a>

## 2026-09-08 21:56 · feat · 投标行情内容库管理与 Agent 生成前参考

| 字段 | 值 |
|---|---|
| id | 2026-09-08-2156-feat-market-knowledge-library |
| datetime | 2026-09-08T21:56:00+08:00 |
| type | feat |
| status | released |
| scope | market-knowledge, bid-generate, mcp |
| related | issue #34, discussion #26 |

### 为什么做这次更新

实现 Discussion #26 确认口径的“投标行情内容库”：管理员上传/抓取投标经验资料，AI 提炼短要点，标书生成时 Agent 自动读取作低优先级辅助参考；同时支持管理员删除资料并级联删除其全部提炼要点。

### 具体做了什么

- 新增模型（迁移 `0034`，含 PG RLS）：`market_knowledge_article`（资料）、`market_knowledge_point`（提炼要点，1:N）、`market_knowledge_image`（图片关联）；资料/要点软删除。
- 新增接口：`POST /market-knowledge/import-url`（公开网页/公众号，同步抓取正文与图片，解析失败即提示“无法解析”）、`POST /market-knowledge/upload`（文档/PDF/图片）、`GET /market-knowledge`（列表+关键词搜索）、`GET /market-knowledge/{id}`（详情+要点+图片）、`GET /market-knowledge/points`（Agent 参考出口）、`POST /market-knowledge/{id}/re-extract`（重试提炼）、`DELETE /market-knowledge/{id}`（管理员删除，级联软删除其全部要点）。
- 权限：新增管理员权限点 `market_knowledge.manage`（上传/删除/重试）；预览与搜索为登录用户；内容全企业共享。
- 抓取：复用 Issue #32 的 `tender_crawler`（任意公开网址、SSRF 逐跳校验），提取标题/正文/懒加载图片；图片经 Pillow 规范化（webp/gif 等转 PNG/JPG）后入库。
- AI 提炼：新增 worker 任务 `market_knowledge_extract`——文本 + 已完成的图片视觉描述（复用 `image_desc`/qwen-vl，受 `vl_enabled` 门禁）→ LLM 提炼短要点（一条资料 → 多条要点）；提示词带版本号 `MARKET_RULES_VERSION` 写入资料与任务；云模型关闭/失败时 `extract_status=failed` 仍可预览、可重试。
- Agent 参考：可替换 provider（当前策略 `all`=全量读取已提炼要点）；Hermes 新增只读 MCP 工具 `search_market_knowledge`（并更新 bid-generate skill 用法），内嵌闭环在生成上下文中注入“行情库提炼要点”低优先级参考块（招标文件要求与企业事实优先，不得虚构事实）；参考数量只记任务元数据/审计，不提示前端。
- 通用文件列表不再展示行情库文件（`owner_type=3` 从无 target 列表过滤）。

### 影响范围

- 行情库接口与文件归属（新增 owner_type=3）、MCP 工具与生成上下文、权限模型。

### 迁移 / 破坏性变更

- 迁移 `0034`：新增 3 张表（含 RLS 策略）；无存量数据影响。
- 新增权限点 `market_knowledge.manage`（管理员专属）：既有管理员需在其用户权限集中具备该点才可管理行情库。

### 验证方式

- 新增 13 个单元/模块测试（解析、分类、图片规范化、管理员门禁、URL 导入、提炼 1:N、重试、级联删除、搜索）；全量 340 passed（3 个失败为既有环境问题，与本次无关）。
- MCP OpenRPC IDL 重新生成（46 个方法）与工具定义一致。
- 服务器已部署（2026-09-08）：HEAD `165026a`，迁移 `0034 (head)`（3 张新表 + RLS 策略核验通过），
  pre-upgrade 备份完成；app/worker/hermes RUNNING，`GET /healthz` ok，
  `/api/v1/market-knowledge` 路由已挂载（未认证 401）；bid-generate skill 已同步至 Hermes。
- GitHub 提交：`165026a`（代码 + 文档）。

### 回滚方式

回退迁移 `0034`（`alembic downgrade 0033`）并回退本次提交，重启 app/worker。

<a id="2026-09-08-2056-fix-tender-import-any-site"></a>

## 2026-09-08 20:56 · fix · 招标公告 URL 导入开放任意网址并明确前端轮询契约

| 字段 | 值 |
|---|---|
| id | 2026-09-08-2056-fix-tender-import-any-site |
| datetime | 2026-09-08T20:56:00+08:00 |
| type | fix |
| status | released |
| scope | tender-notices |
| related | issue #32, discussion #27 |

### 为什么做这次更新

产品确认：招标公告 URL 导入不限于国网 ECP 等已知站点，任意公开网址都允许导入。原实现默认仅放行 `sgccetp.com.cn`、名单外 422 拒绝，与最新口径不符。同时把“异步导入 + 前端轮询”的对接契约在接口文档中固化，便于前端按新契约配合。

### 具体做了什么

- 取消站点白名单：移除 `tender_import_allowed_hosts` 配置与 `host_allowed`/`site_not_allowed` 校验，任意公开网址均可导入。
- SSRF 防线保留不变：仅 http/https；逐跳（含重定向）DNS 校验，内网/保留地址一律拒绝；附件下载仍逐跳校验 + 1GB 上限 + 高危可执行扩展名拦截。
- 文档固化**前端轮询契约**：`import-url` 返回 `status=1`（导入中）后，前端轮询
  `GET /projects/{project_id}/tender-notices/{notice_id}`（`status` 与 `attachments`）或
  `GET /files/batches/{batch_id}`（逐附件状态）；`status=2`（已导入）前禁用“确认”按钮，
  `status=3` 为失败（`error_code/error_message` 说明原因）。

### 影响范围

- `import-url` 允许的网址范围（扩到任意公开网址）；前端需按轮询契约配合。

### 迁移 / 破坏性变更

- 无数据库迁移；仅代码与文档。

### 验证方式

- 测试更新：任意公开站点返回 201（导入中），内网/保留地址仍 422 拒绝；相关用例全绿。
- 服务器已部署（2026-09-08）：HEAD `a1a164b`，无数据库迁移；app/worker 重启后 RUNNING，
  `GET /healthz` 返回 ok；生产代码冒烟：`ccgp.gov.cn`、`cebpubservice.com` 等任意公开站点通过校验，
  127.0.0.1/10.0.0.5/169.254.169.254 均被 `blocked_address` 拒绝。
- GitHub 提交：`a1a164b`（代码 + 文档）。

### 回滚方式

回退本次提交并重启 app/worker。

<a id="2026-09-08-1825-feat-tender-import-attachments"></a>

## 2026-09-08 18:25 · feat · 招标公告 URL 导入逐附件下载与预览

| 字段 | 值 |
|---|---|
| id | 2026-09-08-1825-feat-tender-import-attachments |
| datetime | 2026-09-08T18:25:00+08:00 |
| type | feat |
| status | released |
| scope | tender-notices, files, worker |
| related | issue #32, discussion #27 |

### 为什么做这次更新

原 `import-url` 只抓公告正文（50MB 上限 + 内容类型白名单），无法满足“逐附件下载、解压、预览并用于后续标书生成”的需求。产品在 Discussion #27 确认口径：仅已知站点（默认 `sgccetp.com.cn`）、单文件/总量 1GB、不限类型、解压 zip、部分失败保留成功项并逐条说明原因。ECP 示例页是 Angular hash 路由 SPA，正文与“[下载公告文件]”均由前端 JS 渲染、站点启用 SM2/SM4 加密，后端普通 HTTP 无法直接获取附件。

### 具体做了什么

- `import-url` 改为“秒回”：同步创建 `TenderNotice`（导入中）+ `UploadBatch` + worker 任务；附件下载/解包/落库由 worker 后台执行，前端轮询批次/公告状态。
- 新增抓取服务 `tender_crawler.py`：白名单校验；hash 路由 SPA 用无头浏览器（Playwright/Chromium）渲染，发现“下载/附件/获取”类按钮后**浏览器内点击并拦截下载**（复用站点自带加密/验签，不逆向协议）；静态页直连解析 `<a>` 附件链接。
- 逐附件落库：正文 `document_role=招标公告`；附件 `招标文件/公告附件`；zip 复用 `process_archive` 解包，子文件写 `source_archive_file_id/archive_path` 溯源；每个附件（含解包子文件/失败项）各写一条 `UploadBatchItem`，状态 `accepted/duplicate/expanded/error/skipped` 并带可读原因。
- 失败降级：401/403、重定向登录页、登录墙 HTML 判定为 `skipped`（“获取招标文件”类需登录附件跳过）；正文成功但附件全部失败时公告仍置为已导入，逐条原因可查。
- 安全边界：附件通道放开至 1GB、不限类型，但仍保留 ClamAV 病毒扫描、zip 炸弹/嵌套/路径穿越防护、逐跳 SSRF 校验；高风险可执行扩展名（`.exe/.dll/.bat` 等）默认拦截；全局上传通道的 500MB 上限与类型白名单不变。
- `TenderNotice` 新增 `import_batch_id`，`UploadBatchItem` 新增 `notice_id/source_url`（迁移 `0033`）。
- `GET /projects/{project_id}/tender-notices/{notice_id}` 新增逐附件 `attachments`；`GET /files/batches/{batch_id}` items 新增 `notice_id/source_url`。

### 影响范围

- 招标公告导入接口、文件批次查询接口、worker 任务编排、无头浏览器运行时（服务器新增 Playwright + Chromium 依赖）。

### 迁移 / 破坏性变更

- 迁移 `0033`：`tender_notice.import_batch_id`、`upload_batch_item.notice_id/source_url`。
- `import-url` 语义变化：由“同步返回结果”改为“秒回导入中 + 后台处理”，前端需轮询；仅白名单站点可导入，名单外 422 拒绝。
- 新增运行时依赖：`playwright>=1.49` 及无头 Chromium（浏览器二进制 + 系统依赖）。

### 验证方式

- 新增单元/模块测试 10 个（分类、发现、异步契约、附件成功/跳过/失败、zip 解包溯源、正文失败）；相关用例全绿。
- 全量测试 329 passed（3 个失败为既有环境问题：迁移链 0027 缺 `agent_artifact` 表、LibreOffice 转换，与本次无关）。
- 真实 ECP 示例页端到端冒烟：成功下载“[下载公告文件]”对应“招标公告.zip”（36,226 字节，ZIP 魔数），
  “[获取招标文件]”无下载判定为需登录跳过。
- 服务器已部署（2026-09-08）：HEAD `ac882a5`，迁移 `0033 (head)`，pre-upgrade 备份完成
  （DB dump + appdata），app/worker 重启后 RUNNING，`GET /healthz` 返回 ok；
  生产代码对真实 ECP 页复跑冒烟通过，worker 日志无新增错误。
- GitHub 提交：`ac882a5`（代码 + 文档）。

### 回滚方式

回退迁移 `0033`（`alembic downgrade 0032`）并回退本次提交，重启 app/worker。

<a id="2026-09-07-2345-fix-worker-greenlet-hardening"></a>

## 2026-09-07 23:45 · fix · worker 泵循环 MissingGreenlet 自愈与悬挂事务加固

| 字段 | 值 |
|---|---|
| id | 2026-09-07-2345-fix-worker-greenlet-hardening |
| datetime | 2026-09-07T23:45:28+08:00 |
| type | fix |
| status | released |
| scope | worker, agent |
| related | issue #25, discussion #15 |

### 为什么做这次更新

服务器日志显示两类问题：worker 循环出现 `MissingGreenlet("greenlet_spawn has not been called…")`（主泵会话被取消/毒化后，错误处理路径继续在同一会话上回滚/读取而二次抛出）；锁链破坏器反复观察到一个 `idle in transaction` 的悬挂事务（最后语句 `SELECT max(agent_session_event.seq)`，即事件冲刷超时取消提交后遗留的悬挂行锁），观察日志每 60 秒刷屏。

### 具体做了什么

- `worker_loop` 捕获 `MissingGreenlet` 时 `engine.dispose()` 重置连接池自愈，避免坏连接逐轮复现。
- `run_task` 错误/中断路径：主会话回滚失败时改用独立短命会话直写失败/重试状态（状态机确定性落库），不再二次抛出 MissingGreenlet。
- 事件冲刷（`_flush`）超时取消后显式 `rollback` + `close`，杜绝 `idle in transaction` 悬挂行锁。
- 锁链破坏器：终止持有 `task` 或 `agent_session_event` 行锁且 `idle in transaction` 超 3 分钟的会话；悬挂观察日志按变化去重，不再每轮刷屏。
- 新增回归测试：handler 抛 `MissingGreenlet` 时任务正确重新入队、worker 不崩。

### 影响范围

- worker 主循环、任务执行错误路径、事件冲刷与会话锁链自愈。

### 迁移 / 破坏性变更

- 无数据库迁移；纯运行时加固。

### 验证方式

- 新增 1 个回归测试通过；全量测试 324 passed（3 个失败为既有环境问题，与本次无关）。
- 服务器已部署（2026-09-07）：HEAD `4c0f32f`，无迁移（`alembic current=0032 (head)`），app/worker 重启后 RUNNING，`GET /healthz` 返回 ok；worker 日志无新 MissingGreenlet，锁链观察日志不再刷屏。
- GitHub 提交：`4f0df2e`（代码）、`4c0f32f`（文档）。

### 回滚方式

回退提交 `4f0df2e`，重启 worker。

<a id="2026-09-07-2332-feat-artifact-file-health"></a>

## 2026-09-07 23:32 · feat · 产物详情增加文件健康信号并优雅降级损坏文件

| 字段 | 值 |
|---|---|
| id | 2026-09-07-2332-feat-artifact-file-health |
| datetime | 2026-09-07T23:32:03+08:00 |
| type | feat |
| status | released |
| scope | assembly, artifacts |
| related | issue #24, discussion #1 |

### 为什么做这次更新

用户反馈“编制逻辑与评分响应记录.docx”在成果目录中打开报错。核验确认该文件（项目 207 artifact 938）本身完全有效：正确 MIME、zip 17 条目无损、`word/document.xml` 正常、3017 字符正文，LibreOffice 可直接渲染为 PDF——预览失败属于前端预览层问题，不是后端文件问题。为了让这类问题今后可自证并让前端能区分“文件损坏”与“预览失败”，产物详情接口增加文件健康信号，损坏文件优雅降级。

### 具体做了什么

- `inspect_agent_artifact` 返回 `file_health`：`bytes/mime/kind/readable/zip_ok/document_xml_ok/text_chars/sheets/entries/error`。
- 文件损坏时如实返回 `readable=false` 与错误原因，不再抛 500；前端可据此决定“下载原文件兜底”还是“预览层问题”。
- 新增 2 个回归测试：正常 docx 健康信号、损坏 docx 优雅降级。

### 影响范围

- `GET /projects/{project_id}/assembly/artifacts/{artifact_id}/inspect` 响应契约（新增 `file_health`）。

### 迁移 / 破坏性变更

- 无数据库迁移；纯响应增量字段。

### 验证方式

- 新增 2 个回归测试通过；全量测试 323 passed（3 个失败为既有环境问题，与本次无关）。
- 服务器已部署（2026-09-07）：HEAD `8e6bef2`，无迁移（`alembic current=0032 (head)`），app/worker 重启后 RUNNING，`GET /healthz` 返回 ok；项目 207 artifact 938 详情返回 `file_health.readable=true`。
- GitHub 提交：`5fa1061`（代码）、`8e6bef2`（文档）。

### 回滚方式

回退提交 `5fa1061`，重启 app、worker。

<a id="2026-09-07-2322-fix-upload-batch-subfiles"></a>

## 2026-09-07 23:22 · fix · 上传批次补齐 ZIP 子文件关联与逐文件解析状态

| 字段 | 值 |
|---|---|
| id | 2026-09-07-2322-fix-upload-batch-subfiles |
| datetime | 2026-09-07T23:22:44+08:00 |
| type | fix |
| status | released |
| scope | files, upload |
| related | issue #23, discussion #1, discussion #16 |

### 为什么做这次更新

`GET /files/batches/{batch_id}` 只有“本次上传的原件”条目，ZIP 自动解包出的子文件没有对应批次条目，前端无法逐个看到子文件的来源压缩包、包内路径与解析状态；失败项只有汇总数量，没有可读原因。

### 具体做了什么

- `upload_batch_item` 增加 `source_archive_file_id`（来源压缩包）、`archive_path`（包内相对路径）、`parse_status`（parsing/done/failed），迁移 `0032`。
- 上传 zip 自动解包后，为每个成功子文件、失败项、重复项各写一条批次条目；子文件条目带来源压缩包与包内路径，失败项带可读原因。
- 普通上传文件条目也记录解析状态；`GET /files/batches/{batch_id}` 按当前 `FileObject.status` 实时回读解析状态（刷新后反映最新进度）。
- 新增回归测试：zip 解包批次含原件与子文件条目，来源与解析状态正确。

### 影响范围

- `GET /files/batches/{batch_id}` 响应契约（items 新增 `source_archive_file_id`/`archive_path`/`parse_status`）。
- `upload_batch_item` 表与迁移 `0032`。

### 迁移 / 破坏性变更

- 新增迁移 `0032`（3 个字段）；全部为增量字段，旧条目无值即为空，前端可兼容。

### 验证方式

- 新增 1 个回归测试通过；全量测试 321 passed（3 个失败为既有环境问题，与本次无关）。
- 服务器已部署（2026-09-07）：HEAD `4e2762b`，迁移已执行 `0031 → 0032`（`upload_batch_item` 3 个字段落库），app/worker 重启后 RUNNING，`GET /healthz` 返回 ok，`alembic current=0032 (head)`。
- 前端联调项：上传含多个文件的 zip 后，批次查询可逐个看到子文件的来源压缩包、包内路径与解析状态。
- GitHub 提交：`362fae4`（代码）、`4e2762b`（文档）。

### 回滚方式

回退提交 `362fae4`，`alembic downgrade 0031`（如需移除字段），重启 app、worker。

<a id="2026-09-07-2312-fix-score-artifact-binding"></a>

## 2026-09-07 23:12 · fix · 评分与报价绑定正式 artifact 版本并修复键类型比对

| 字段 | 值 |
|---|---|
| id | 2026-09-07-2312-fix-score-artifact-binding |
| datetime | 2026-09-07T23:12:15+08:00 |
| type | fix |
| status | released |
| scope | review, quotes |
| related | issue #22, discussion #1, discussion #14, discussion #16 |

### 为什么做这次更新

`deliverable_versions` 经 JSON 序列化后键变成字符串，`stale_reasons` 用字符串键与整数 `Deliverable.id` 比较，版本变化后旧评分永远不会被判过期；且该映射只指向结构化 deliverable，无法证明评分对应用户打开的那个正式 artifact 版本。报价同样只绑定结构化 deliverable，缺正式文件绑定。

### 具体做了什么

- `evaluate` / `re_evaluate` 冻结 `artifact_versions`（`artifact_id -> version_no`）入评分快照与 `ScoreRecord`。
- `GET /projects/{project_id}/scores`：键归一化为整数后比较，`deliverable_versions` 与 `artifact_versions` 任一变化都正确判定过期；新增返回 `has_score`、`artifact_versions`、`scored_artifacts`（含名称/版本）。
- `quote_calc` 增加 `artifact_id` / `artifact_version_no`：`calculate`/`apply` 可写入，详情与列表返回。
- 迁移 `0031`：`score_record.artifact_versions` + `quote_calc` 两个绑定字段。
- 新增 2 个回归测试：artifact/结构化成果版本变化过期判定、报价 artifact 绑定读回。

### 影响范围

- `GET /projects/{project_id}/scores` 响应契约（新增 `has_score`/`artifact_versions`/`scored_artifacts`，`stale_reasons` 现在会包含 artifact 维度）。
- 报价测算 `calculate`/`apply`/详情/列表契约（新增 `artifact_id`/`artifact_version_no`）。
- `score_record`、`quote_calc` 表与迁移 `0031`。

### 迁移 / 破坏性变更

- 新增迁移 `0031`（3 个字段）；存量评分 `artifact_versions` 为空，此时只按 `deliverable_versions` 判过期，行为向后兼容。
- 所有新增字段/返回为增量，旧字段保持不变。

### 验证方式

- 新增 2 个回归测试通过；全量测试 320 passed（3 个失败为既有环境问题，与本次无关）。
- 服务器已部署（2026-09-07）：HEAD `fb9e235`，迁移已执行 `0030 → 0031`（`score_record.artifact_versions` + `quote_calc.artifact_id/artifact_version_no` 落库），app/worker 重启后 RUNNING，`GET /healthz` 返回 ok，`alembic current=0031 (head)`。
- 前端联调项：项目 207 最新评分返回 `scored_artifacts`；文件版本变化后 `is_stale=true` 且 `stale_reasons` 给出对应 artifact/成果。
- GitHub 提交：`1084841`（代码）、`fb9e235`（文档）。

### 回滚方式

回退提交 `1084841`，`alembic downgrade 0030`（如需移除字段），重启 app、worker。

<a id="2026-09-07-2300-feat-artifact-logical-versions"></a>

## 2026-09-07 23:00 · feat · 正式文件逻辑版本链与覆盖历史

| 字段 | 值 |
|---|---|
| id | 2026-09-07-2300-feat-artifact-logical-versions |
| datetime | 2026-09-07T23:00:42+08:00 |
| type | feat |
| status | released |
| scope | assembly, artifacts |
| related | issue #21, discussion #13, discussion #16 |

### 为什么做这次更新

`POST /assembly/artifacts/{id}/save` 的 `mode=new` 只新建一个独立 artifact 并从 V1 开始，无法表达“同一逻辑文件的历史版本链”；`mode=overwrite` 直接替换内容，覆盖前内容没有任何接口可读。前端无法确认“另存后的旧版在哪、覆盖前的内容能否找回”。

### 具体做了什么

- `agent_artifact` 增加 `logical_file_id`（同一逻辑文件不变）、`parent_artifact_id`（另存自哪个 artifact）、`logical_version_no`（逻辑文件下递增）。
- 新增 `agent_artifact_content_version` 归档表（迁移 `0030`，含 RLS）：覆盖保存前把当前版本内容落历史，覆盖后可回读。
- `save` 接口：`mode=new` 继承逻辑文件身份（`logical_version_no=原版本+1`，`parent_artifact_id=原 artifact`），旧版保留可下载；`mode=overwrite` 归档当前内容后递增 `version_no`，并初始化逻辑文件根身份。
- 新增 `GET /assembly/artifacts/{id}/versions`（同一逻辑文件版本链列表）与 `GET /agent-artifact/{id}/versions/{n}/download`（指定版本内容下载）。
- 产物清单/详情响应增加 `logical_file_id`、`logical_version_no`、`parent_artifact_id`。
- 下载响应头改用 RFC 5987 `filename*`（修复中文文件名撞 latin-1 头编码导致 500 的问题，`download_artifact` 同步修复）。

### 影响范围

- `POST /assembly/artifacts/{id}/save` 响应契约（新增逻辑版本字段）。
- 产物清单/详情契约；新增两个版本查询接口。
- `agent_artifact`、`agent_artifact_content_version` 表与迁移 `0030`。

### 迁移 / 破坏性变更

- 新增迁移 `0030`（3 个字段 + 归档表 + RLS）；存量产物逻辑字段为空，代码按“自身为根、逻辑版本 1”解释，无需回填。
- 所有新增字段/接口为增量，旧字段保持不变。

### 验证方式

- 新增 3 个回归测试通过；全量测试 318 passed（3 个失败为既有环境问题，与本次无关）。
- 服务器已部署（2026-09-07）：HEAD `76eb625`，迁移已执行 `0029 → 0030`（3 个字段 + `agent_artifact_content_version` 表 + RLS 落库），app/worker 重启后 RUNNING，`GET /healthz` 返回 ok，`alembic current=0030 (head)`。
- 前端联调项：另存后新旧版本都可下载；覆盖后历史版本可回读；清单/详情可见 `logical_*` 字段。
- GitHub 提交：`aae2676`（代码）、`76eb625`（文档）。

### 回滚方式

回退提交 `aae2676`，`alembic downgrade 0029`（如需移除归档表与字段），重启 app、worker。

<a id="2026-09-07-2246-fix-stream-replay-prechat"></a>

## 2026-09-07 22:46 · fix · 修复长历史补读截断并持久化 pre_chat 消息

| 字段 | 值 |
|---|---|
| id | 2026-09-07-2246-fix-stream-replay-prechat |
| datetime | 2026-09-07T22:46:26+08:00 |
| type | fix |
| status | released |
| scope | agent, chat, pre_chat |
| related | issue #20, discussion #1, discussion #15 |

### 为什么做这次更新

终态任务的事件流每批最多补读 200 条历史后即发 `end`，长会话（如项目 207 任务 3499 共 3600+ 条事件）会漏发尾部消息；任务前对话（pre-chat）只返回当次回复、不落任何持久记录，刷新后无法恢复，也无法关联消息与回复。

### 具体做了什么

- `agent_run_stream`：终态任务按 `seq` 游标持续分页补读，直到某一批为空才发 `end`，保证 `end` 表示历史已全部补发；`end` 事件附带 `last_seq` 供前端续传。
- 新增 `pre_chat_message` 表（迁移 `0029`，含租户 RLS 策略）：任务前对话的 user/hermes/error 事件逐条落库，刷新可恢复。
- `pre_chat` 重写：写入 user 事件与 hermes 回复事件（`reply_to_seq` 关联），返回 `message_id=user_seq`、`reply_to_message_id=reply_seq`；支持 `client_message_id` 幂等去重与结果回放；退出码非 0 返回 `failed`、输出为空返回 `no_valid_reply`，超时落 error 事件。
- 新增 `GET /projects/{project_id}/pre-chat/messages?since=&limit=` 历史恢复接口。
- 新增 3 个回归测试：长历史（450 条）完整补发、pre_chat 持久化与幂等、失败事件落库。

### 影响范围

- `GET /projects/{project_id}/agent-run/{task_id}/stream` 的 `end` 事件契约（新增 `last_seq`）。
- `POST /projects/{project_id}/pre-chat` 请求/响应契约。
- 新增 `pre_chat_message` 表与迁移。

### 迁移 / 破坏性变更

- 新增迁移 `0029`（`pre_chat_message` + RLS）；无既有表结构变更。
- 响应新增 `status`/`error`/`duplicate`/`reply_to_message_id` 字段，原有 `reply`/`session_id`/`message_id` 保留。

### 验证方式

- 新增 3 个用例通过；全量测试 315 passed（3 个失败为既有环境问题：SQLite 全新迁移链缺 `agent_artifact` 建表、LibreOffice 容器转换，与本次无关）。
- 服务器已部署（2026-09-07）：HEAD `09cfcbe`，`alembic upgrade` 已执行 `0028 → 0029`（`pre_chat_message` 表 + RLS 落库），app/worker 重启后 RUNNING，`GET /healthz` 返回 ok，`alembic current=0029 (head)`。
- 前端联调项：长历史事件流不漏尾部消息（`end` 带 `last_seq`）；pre_chat 消息刷新可恢复（`GET /pre-chat/messages`）。
- GitHub 提交：`819796d`（代码）、`09cfcbe`（文档）。

### 回滚方式

回退提交 `819796d`，`alembic downgrade 0028`（如需移除表），重启 app、worker。

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
