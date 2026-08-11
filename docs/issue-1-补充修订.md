# Issue #1 补充修订说明与开发子任务清单

> 本文件为 Issue #1（[架构总审](https://github.com/zhangsheng377/BidVolt/issues/1)）的回复草稿与后续开发子任务清单，可在核对后直接粘贴为 Issue 评论。

---

## 一、回复草稿（可粘贴到 Issue #1）

@pilipiliwang 本批按 Issue 清单逐条复查，补齐了此前仅停留在文字约束、尚未可执行/可验证的机制，并将 P0/P1 的遗留交付物落地。修订提交后文档与清单如下。

### 1. 数据模型补全（Issue 二.3/二.4、P0-1）

- 企业资料域补全 DDL：`enterprise_asset` / `enterprise_asset_revision` / `enterprise_fact` / `enterprise_fact_evidence` / `enterprise_asset_category` / `enterprise_ingestion_task`，事实携带来源版本 + 原文定位 + 有效期，支持用户纠正与人工确认
- 项目材料域补全 DDL：`project_material` / `project_material_revision` / `project_event` / `project_snapshot` / `material_match_result`；`requirement` 增加 `revision` / `supersedes` / `current`，新增 `requirement_revision`（补遗/澄清可追溯）
- `task_type` 增加 `enterprise_ingestion`，作为企业资料写工具（classify/upsert）的唯一授权入口
- `citation` 表补 `enterprise_id` / `project_id`，引用类对象完整所属链闭合

### 2. 安全与运维（P0-2、P1）

- 权限点 + 独立权限门禁（架构决策 D18）：规则修改（`review_provider.config`）、报价应用（`quote.apply`）、终稿导出（`deliverable.export`）独立授权；单企业少账号场景不建角色表/角色分配 UI
- 密钥管理统一：所有密钥由 Secret Manager 注入运行环境，Agent 不读源码 `.env`（修正此前文档自相矛盾处）
- 病毒扫描统一为强制：ClamAV 必装，不可选；扫描失败阻塞入库
- 压缩包安全补全：总解压字节上限 + 拒绝绝对路径/`..`/符号链接/硬链接/设备文件；转换器/解压器部署于无网络、非 root、只读根文件系统容器
- 搜索出站安全：出站代理 + DLP/敏感信息脱敏 + 域名策略（P0-2）
- 云模型与搜索的 P1 门禁：数据分级/客户授权/地域/留存/删除规则确认前默认关闭（写入架构第 7 节与 4.3/4.9）
- 配额与运维：每租户存储/并发/模型 token/搜索/导出配额（默认值可配）；RPO ≤ 24h、RTO ≤ 4h；备份保留 30 天；注销数据保留 90 天后物理删除
- 审计动作枚举：文件读取下载、MCP 工具调用、外部调用、模型版本、评分、报价、权限、管理员操作全量覆盖

### 3. MCP 契约与命名统一（Issue 二.11、P0-3）

- MCP 工具 Schema 由同一 IDL（OpenRPC + JSON Schema）生成，客户端/服务端/测试共用；五条 Skill 路径端到端契约测试清单落地
- Skill 名统一为 `bidvolt-*`（连字符）风格，模型配置统一为 deepseek-v4-flash / qwen-vl-max，修正 README 与模块文档不一致处
- HistoryPriceProvider 契约补全：`query_history` / `get_material_samples` / `get_source_metadata` 三类只读能力（含 MCP 工具与 REST 端点）

### 4. Issue 七 交付物

- 新增 `docs/威胁模型与测试清单.md`：数据域与权限边界图、威胁模型（STRIDE，10 项）、数据流图、A-1~A-12 测试清单（跨租户 IDOR / 授权上下文 / Prompt Injection / 幂等并发 / 文件安全 / SSE 白名单 / 出站安全 / 报价边界 / ReviewProvider / 沙箱 / 审计配额 / 评分闭环）
- 新增 `docs/数据分级与授权确认清单.md`：P1 门禁的唯一解锁入口——数据分级表（L1~L4）、七项客户授权、部署配置落点与签署栏，产品/客户填写签署后云模型/搜索方可解锁

### 5. 架构侧部署与技术可行性判断（单容器形态，ADR D17）

部署前提是**服务器本身为单个 Docker 容器（平台已确认不支持多容器）**，架构侧据此做如下取舍（产品第 8 条评论已授权"技术选型和具体实现以后端架构判断为准"）：

1. **多进程容器**：FastAPI、Hermes、PostgreSQL、ClamAV、转换器、备份 cron 同容器运行，supervisor 统一管理 + 优雅停机 + 健康检查；PG 镜像内 `apt install postgresql-16`，数据目录挂持久卷，首次启动初始化集群
2. **V1 不引入 Redis**：任务队列用 PG 任务表（`SELECT … FOR UPDATE SKIP LOCKED`），编辑锁用表实现；多 worker 重叠执行时才需要 lease/fencing
3. **V1 不引入容器内 MinIO/pgbouncer**：文件存储抽象为 StorageProvider，本地磁盘适配器（租户前缀目录 + 应用层签名下载）满足 P0-1 隔离语义；S3 适配器保留为演进预留
4. **沙箱即受限进程近似（唯一形态）**：平台不支持多容器，无法做独立沙箱容器；V1 用非 root 子进程 + rlimit + 只读输入目录 + 无外部网络依赖实现近似隔离，强隔离列为演进预留、不纳入验收（已在文档与测试清单中如实标注）
5. **ClamAV 病毒库策略已定（ADR D19）**：镜像构建期打包基线病毒库（离线可扫描）+ 启动时 freshclam 增量更新（失败不阻塞，库超 7 天未更新告警）；clamd 不可用则 fail-closed 阻塞入库。镜像体积与内存成本（约 300MB）接受，由构建期打包规避网络不确定性
6. **搜索出网实现已定**：应用层 DLP 脱敏 + 域名白名单（代码内强制），有 `HTTP_PROXY` 则经代理，否则直连白名单域名——不依赖外部代理设施；真正决定搜索是否上线的仍是数据分级/客户授权（P1 门禁）

### 6. 待产品/环境确认（不阻塞文档，但阻塞对应功能上线）

1. 数据分级与客户授权结论（决定云模型/搜索是否解锁，未确认前按 P1 门禁默认关闭；确认入口 = `docs/数据分级与授权确认清单.md`）
2. 权限默认集：注册用户默认拥有除 `review_provider.config`、`admin.*`、`audit.view` 外的权限点，是否满足产品预期
3. （已定，无需确认）ClamAV 病毒库方案 ADR D19；沙箱强隔离不纳入 V1（平台不支持多容器）；搜索出网实现见第 5 节第 6 条

---

## 二、开发子任务清单

> 按里程碑展开，进入核心开发时逐项勾选；P0 项完成并通过对应验收后再进入生产级开发。

### M1 基础平台

- [x] PostgreSQL + RLS（FORCE）初始化，复合唯一键/同租户复合外键
- [x] 认证（注册/登录/refresh）、编辑锁
- [x] 权限点 + 用户权限集合（enterprise_permission 表）+ 中间件 + 独立权限门禁（规则修改/报价应用/终稿导出）
- [x] tenant_quota 配额表 + 超限拦截
- [x] 单容器部署：supervisor 多进程编排（FastAPI/worker/PG/ClamAV/cron）、PG 首次初始化、持久卷（pgdata/data/backups）、备份 cron（平台不支持多容器，Hermes 进程见 M3 门禁项）
- [x] StorageProvider：V1 本地磁盘适配器（租户前缀目录 + 应用层签名下载），S3 适配器接口预留
- [x] audit_log 按 4.11.3 枚举埋点
- [x] A-1 跨租户 IDOR 用例组

### M2 文件与解析

- [x] 上传/压缩包安全管道：隔离区 → magic bytes → ClamAV 强制 → 受限进程解析（非 root + rlimit）→ 入库
- [x] 压缩包限制与路径穿越/符号链接拒绝
- [x] 解析 SDK 接入 + 坐标体系 + OFD（OFD 文本层内置 zip/XML 解析，easyofd 可选图像转换；合成 OFD 单测 + 线上网页实测通过）
- [x] enterprise_asset/fact/evidence/category/ingestion_task 落库
- [x] project_material/revision/event 落库
- [x] requirement + requirement_revision（revision/supersedes/current）
- [x] A-5 文件安全用例组

### M3 Hermes 接入

- [ ] Hermes 进程部署（同容器 supervisor 编排；`bidvolt_mcp` stdio server 已实现并有契约测试，待 Hermes Agent 进程接入）
- [x] 任务级授权上下文（capability token 落地：签名/有效期/工具白名单/租户绑定）+ enterprise_ingestion 任务类型
- [x] 任务编排：队列 = PG 任务表（SKIP LOCKED，不引入 Redis）；idempotency_key 唯一约束、generation 校验、重试耗尽终态失败、单事务提交
- [x] SSE 白名单事件过滤（禁止思维链/工具参数/内部 ID/凭据/错误栈）
- [x] MCP IDL（OpenRPC + JSON Schema）生成 + 服务端实现（`bidvolt_mcp/schema/openrpc.json` + `test_mcp_idl.py` 一致性校验）
- [x] Secret Manager 注入密钥（DASHSCOPE_API_KEY/ANYSEARCH_KEY/BIDVOLT_INTERNAL_TOKEN，`.env` 不入库）
- [x] A-2/A-3/A-4/A-6 用例组

### M4 成果与评审闭环

- [x] DocModel/SheetModel + 版本链（CAS 409、20 版保留、里程碑、hash 去重）
- [x] 在线编辑后端接口 + ai-edit diff
- [x] 生成/校核（三份成果并行、一致性检查）
- [x] ReviewProvider：Document/Code/API 契约 + 受限进程执行 + EvidenceRef 服务端校验
- [x] review_item 主模型 + review_material_link + review_run + score_record 汇总
- [x] 五步提升闭环（上传→确认→合并写入→重审→更新分）+ 批量逐条结果
- [x] project_snapshot 冻结 + 不可变 manifest
- [x] A-9/A-10/A-12 用例组

### M5 报价与搜索

- [x] HistoryPriceProvider 三类只读能力 + history_price_snapshot（外部不回写）
- [x] QuoteEngine 确定性算法（标准化/异常剔除/口径统一/三类策略/复算）
- [x] AI 参考建议边界（区间 + 依据/假设/置信度/风险，无依据不输出数字）
- [x] quote.apply 双门禁（用户确认 + quote.apply 权限）+ 审计
- [x] 出站代理 + DLP 脱敏 + 域名策略；搜索配额监控
- [x] A-7/A-8 用例组

### M6 导出与打磨

- [x] 终稿检查 + 豁免记录 + 导出转换（DOCX/XLSX/PDF，转换器受限进程运行）
- [x] 交付包 manifest（文件哈希/检查结果/豁免/来源版本）
- [x] 备份（每日 pg_dump + 转存 30 天）、恢复演练（RPO/RTO）
- [x] 删除策略（注销 90 天物理删除、导出产物 7 天、审计 180 天）
- [x] A-11 审计与配额用例组

### 上线门禁（P1）

- [ ] 数据分级与客户授权结论确认（`docs/数据分级与授权确认清单.md` 签署完成，解锁云模型/搜索）
- [ ] 出站代理与 DLP 设施就绪
- [x] 全部 A-1~A-12 测试通过（127 passed, 1 deselected）
