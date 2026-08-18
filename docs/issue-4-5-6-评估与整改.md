# Issue #4 / #5 / #6 架构评估与整改记录（2026-08-16）

> 由后端架构/开发侧对产品 Issue 逐条评估：合理 → 落地实现；与既有产品决策冲突 → 说明并请产品确认。
> 全部改动已通过单元测试 + 本地浏览器全流程 E2E（见文末验证基线）。

---

## 一、Issue #4：历史案例与专业材料检索——评估结论：需求合理，按两阶段落地

### 1.1 架构评估

- **必要性**：合理。专业方案类内容（供货/质量/施工/售后等）仅凭招标文件 + 结构化事实无法生成有企业针对性的正文，产品判断成立。
- **技术选型**：当前数据规模（单企业，解析文本块数千级）无需向量库；第一阶段用**元数据过滤 + 关键词/bigram 重叠打分**即可达到"找到相关章节"的目标，第二阶段（资料规模化后）再引入向量/混合检索。
- **边界约束（必须遵守）**：
  1. 检索只在**本企业租户**内（enterprise_id + PG RLS），跨企业永不互通；
  2. 默认排除**当前项目自身材料**（避免自我引用）；
  3. 检索片段仅作**经验素材**，生成时禁止沿用历史项目名称/招标人/金额/工期/人员（写入生成 Prompt）；
  4. 企业事实类信息只来自已确认 enterprise_fact，**不从历史案例推测**（检索结果与事实查询分开返回）；
  5. 结果来源可追溯（文件/项目/页码/块索引/类型/角色），供校核与评审引用。

### 1.2 第一阶段落地（本记录）

- `app/services/knowledge_service.py`：DocBlock（历史项目材料 + 企业资料）bigram/关键词打分 + 片段摘录 + 来源追溯；已确认企业事实单独返回；
- REST：`POST /api/v1/knowledge/search`（query/project_id/top_k/include_assets）；
- MCP 工具：`search_knowledge`（第 25 个工具，已入 OpenRPC IDL；capability 白名单：bid_generate / material_match / bid_review / targeted_edit / chat）；
- 生成链路接入：`bid_generate` 技术标生成自动检索历史素材并注入 Prompt（引用清单写入任务元数据 knowledge_refs，不混入正式成果——符合 Issue #7"过程信息不进成果"原则）；
- 测试：命中/溯源/排除自身/租户隔离。

### 1.3 验收场景回应（Issue #4 六）

"根据当前招标要求找到相关历史章节"与"分别生成供货/质量/售后方案"两项目前已完成（技术标按技术要求分章生成）；其余四项依赖产品真实资料库积累，见 Issue #4 回复。

---

## 二、Issue #5：轻量测试客户端——评估结论：合理，已落地

- `/demo/` 升级为**测试客户端**：新增"设置/连接"页——多环境（名称+地址）保存/切换（localStorage）、**连接测试**（healthz + OpenAPI，显示状态与耗时）、不携带任何真实账号/密钥；
- 全业务页签真实调用后端 API（认证/项目/资料/成果/任务评标/报价/导出/搜索对话 + 新增知识检索/公告导入）；
- 交付：`scripts/build_test_client.py` 生成版本化静态包 `dist/bidvolt-test-client-<版本>-static.zip`（含 README-TEST-CLIENT.txt，任意静态服务器可分发）；
- 后端配套：CORS 中间件（`CORS_ORIGINS` 可配置，默认全部来源；生产建议同源反代）。

---

## 三、Issue #6：前后端联调总单——逐条评估与处置

### 3.1 P0

| 项 | 评估 | 处置 |
|---|---|---|
| 恢复联调 API 服务 | 合理 | ✅ 服务器 2026-08-16 已恢复（六进程 RUNNING、healthcheck HEALTH_OK、公网 healthz 200）；并落地容器自启动自举（sshd 包装器），避免再因进程停止导致长期空窗 |
| 招标公告 URL 安全导入 | 合理 | ✅ `POST /projects/{id}/tender-notices/import-url` + 列表/详情：仅 http/https、DNS 逐跳校验拒绝内网/保留地址（防 SSRF/DNS rebinding）、手动重定向≤5 跳且逐跳校验、下载≤50MB、内容类型白名单、正文进入本项目材料（document_role=招标公告）绝不写企业资料库、失败落 error_code 留审计 |
| 生产访问方案 | 合理 | ✅ CORS 中间件 + `CORS_ORIGINS` 白名单配置（默认全来源，Bearer 无 cookie 依赖）；同源反代方案保留为生产推荐 |
| 企业身份 enterprise_name | 合理 | ✅ `GET /auth/me` 返回真实企业名 |
| 企业上传返回 file_id+asset_id | 合理 | ✅ enterprise 上传响应含 `asset_id` 与 `auto_ingest: true` |
| Requirement 用户确认/修正 | 合理 | ✅ `PUT .../requirements/{id}/confirm` 与 `/correct`：expected_revision CAS（409）、correct 生成新 revision（supersede + 审计）；前端不再需要拿 Agent upsert 冒充用户确认 |
| 报价数值契约 | 合理（部分现状已满足） | ⚠️ 评估：报价金额当前以 float 序列化。已改为（本次）见 3.2 P1 清单对应项；正式契约为金额/费率一律**字符串**输出 |
| 禁止 AI 猜报价 | **与 Issue #1 产品决策冲突** | ⚠️ 已按 #6 收紧：`/quotes/ai-suggest` **停用 recommended 单点数字**，仅保留参考区间且无依据/无测算结果时不出任何数字（D-F）。完全停用区间与 #1 中"产品决定保留 AI 参考价格区间（带依据/置信度/风险）"冲突，请产品在本 Issue 确认最终口径（回复中 @pilipiliwang） |
| 冻结评审证据 EvidenceRef | 合理 | ✅ 评审输入已冻结到 ProjectSnapshot（deliverable 版本 + ruleset + provider code/version 计入 snapshot 与 raw_hash）；逐条证据含 claim/source_version/content_hash 字段 |
| 评审 Provider 选择生效 | 合理 | ✅ `POST /projects/{id}/evaluate` 接受 `provider_id`：未传=企业内置；传入须属于本企业且启用；跨租户 404、禁用/非内置引擎 422，均失败关闭；provider code/version 冻结进快照与 raw_hash |

### 3.2 P1（后端侧已落地）

- ✅ 项目列表/详情：`buyer` 字段、服务端 `q` 搜索（名称/编号/招标人）、准确分页、**可解释摘要**（材料数/成果数/评审数/最新评分/缺失项，批量计算无 N+1）；
- ✅ 项目文件 `document_role`（上传参数 + 返回 + 公告导入自动标记），刷新不丢；
- ⚠️ Task SSE 鉴权/单调 event ID/Last-Event-ID：当前 SSE 走 `GET /tasks/{id}/stream`（Bearer 鉴权 + 首帧 snapshot 事件支持刷新恢复）；浏览器 EventSource 无法带 Header 属已知平台限制，建议前端用 fetch 流式或短时 token 查询参数，请前端侧确认（不阻塞本后端整改）；
- ⚠️ 写接口统一幂等键/expected revision：任务创建、Requirement、成果保存、评审确认已具备；项目创建/上传批次幂等键列入后续（P2 里程碑）；
- ⚠️ 历史中标只读 Provider 替换 Mock：属外部数据源接入项，需客户提供数据源；V1 按 #2 结论保持 Mock（只读、冻结快照可复算）并显式标注；
- ✅ 编辑器历史版本策略：已有版本链只读 + CAS（expected_version_no 409）+ 指定版本下载；create session 已有租约；
- ⚠️ 核心 API Pydantic 化/OpenAPI 契约 diff：新增接口均已 Pydantic；存量 dict 接口全量替换列入后续；
- ✅ 企业资料列表：已支持关键词/分类过滤与分页（见 enterprise API）。

### 3.3 P2

- ✅ 错误 envelope：HTTPException/422 统一返回 `{detail, code, request_id[, field_errors]}`（兼容原 detail）；
- ⚠️ 全路径 IDOR 测试矩阵：现有 RLS/capability/IDOR 用例 + 本次新增（确认/修正/知识检索/公告导入/评审 provider 跨租户）已覆盖新路径；存量全矩阵按 P2 里程碑推进；
- ✅ 报价金额字符串化：`/quotes/*` 响应金额与费率字段统一输出字符串（见 3.1）。

### 3.4 真实 RAR 验收样本

样本文件未上传 GitHub，无法在本仓库复验该具体文件；已用等价压缩包用例（zip 防护 + 归档导入 + 解包入库）覆盖安全链路。联调时请产品提供样本访问方式，后端可远程验收。

---

## 四、附带修复：技术标生成回归（产品反馈）

- **根因**：`bid_generate` 技术标分支只有确定性 stub（"一、技术方案总体说明 + 草稿待校核"），LLM 增强仅作用于商务标；E2E 只断言"成果存在"未断言内容质量，故未暴露。
- **修复**：技术标由 LLM **全文生成**——按技术要求逐条响应、分章节（技术方案/参数响应/生产供货/质量/售后/进度交付）、注入当前材料摘录 + 历史参考素材（Issue #4）+ 企业事实；禁编造/禁沿用历史项目事实、不足处标【待补充】；LLM 失败回退草稿并标注。商务标同链路生成。
- **测试加固**：单元测试断言技术标含要求响应且**不得出现占位草稿标记**；E2E 新增"成果内容质量"步骤（技术标≥200 字、非占位）。

## 五、验证基线（2026-08-17 更新）

- 本地全量 pytest：**233 passed / 1 skipped**（SQLite，含 Issue #12 回归：产品真实 docx 测试夹具、重解析清旧块、抽取去重、空要求生成拦截、终检质量门禁）；
- 本地浏览器全流程 E2E（headless Chromium 模拟人操作）：**25/25 PASS**（含 Issue #11/#12 专项回归：成果正文可视化+状态判断、步骤条证据、日志无矛盾误报、资料页项目隔离列、校核结果面板、正文无 Markdown 残留、终检统计面板），覆盖注册登录→项目→上传解析（真实 LLM 抽取要求）→Requirement 确认/修正→资料匹配→成果生成（技术标/商务标实质正文断言，非占位）→校核→在线编辑→评标闭环→报价闭环→会话（真实 LLM）→真实 AnySearch 搜索→**知识检索**→**公告导入（SSRF 拒绝实测）**→**环境切换与连接测试**→终检导出→快照/任务恢复→刷新恢复；
- **公网生产环境浏览器 E2E**（`--base http://47.100.182.3:28123`）：**25/25 PASS**（2026-08-18 深度生成轮，用产品同款真实招标 docx）——tender_parse 真实 LLM 抽取 10 条（task#124）、material_match（task#125）、bid_generate **技术标 12031 字/商务标 4698 字**（8+3 章分章深度生成，无占位草稿、**无 Markdown 残留**）+ `quality.deliverables_ready=true`（task#126）、bid_review（task#127），ReviewRun/ScoreRecord 绑定成果版本；技术标正文页面渲染 12252 字（非 JSON）；
- 服务器实测（部署后）：alembic 0018 迁移完成，六进程 RUNNING，healthcheck OK；
- 公网复测暴露并已修复的生产问题（详见 Issue #8/#11/#12 回复）：
  1. **worker RLS 租户上下文**：会话级 `set_config` 在 asyncpg 连接池下随连接归还漂移/丢失 → 改为**事务级注入 + 每次提交后重建**（commit `61da09e`），并加任务最终提交兜底；
  2. **新建会话竞态**：新建后自动选中并可立即提问（commit `113b8b9`）；
  3. **Issue #11 测试客户端复测十二条**：步骤条证据化/可点击、成果正文可视化与状态横幅、评审页任务状态表、生成防重复、SSE 健壮化、await 后空安全写入消除 TypeError 误报、日志分级无静默失败（commit `89b6940`/`b6a25b8`）；
  4. **Issue #12 产品复测五问**：重解析清旧块（6 倍重复根因）、抽取去重+碎片过滤、空要求生成拦截、生成 payload 去写死演示物料、报价单不编造、校核结果面板、**终检升级（完整性+要求覆盖+文档质量）**、资料页项目隔离（commit `c551366`/`7ed0e60`）；
  5. **标书篇幅不足（用户反馈）**：参照公开政府采购评审要素（逐项响应/偏离表+实施方案+人员+售后+应急+进度），改为**分章并行深度生成**（技术标 8 章、商务标 3 章、逐条响应全部要求），技术标由 2387 字提升至 **12031 字**（commit `21e4830`）；
- gitleaks：全历史 0 泄漏；ruff 0 error。
