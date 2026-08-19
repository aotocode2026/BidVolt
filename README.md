# BidVolt 投标工作台

面向招投标场景的智能辅助平台：招标材料解析（含国标 OFD）、企业资料库、Requirement 管理、
成果（商务标/技术标/报价单）生成与校核、逐条评审闭环、确定性报价测算、AnySearch 联网检索、
Hermes Agent 工具调用，以及用于验证的 Demo 前端。

> 部署形态：**单个 Docker 容器**（平台直接提供，不走 Dockerfile）；数据与可部署产物全部落在
> 平台外挂持久卷 `/data`，容器重建后无需重装依赖即可恢复。

---

## 1. 功能总览

- 登录/会话：JWT 登录、刷新、企业上下文、RLS 租户隔离
- 项目工作台：项目 CRUD/归档、材料上传（txt/docx/xlsx/pdf/**OFD**）、解析状态
- 项目快照：列表 + 详情（不可变 manifest：材料哈希/要求版本/成果版本/规则与模型版本）；活动任务查询
- Requirement：提取/确认/修订/证据定位（文件坐标）；同类型多条要求共存（req_key 稳定身份），补遗用 key 覆盖并递增 revision；
  **用户确认/修正闭环**（confirm/correct，expected_revision CAS 409 + 审计）
- 成果版本链：商务标/技术标/报价单生成、校核、CAS 版本冲突保护、**指定版本下载**（DOCX/XLSX）、**在线编辑会话**（租约/检查点/完成生成新版本）；AI 修改建议走真实 LLM；
  **标书生成默认走 Hermes Agent**（bid-generate skill 经 MCP 真实执行，质量门不达标自动回退内嵌闭环），
  结构取自招标文件（解析落库 doc_structure）、分章深度生成、逐条响应全部要求、**自检闭环**（迭代至闭环，未闭环不交付）、应答函格式页、正文无 Markdown 残留
- 评审闭环：逐条 review_item、材料补充、单条/批量确认、重审、仅改受影响项；用户可修改并保存评审建议（override，保留原始建议）；按 run_id 恢复完整评审上下文；
  **评分细则权重化**：解析出的评分细则（weight/criterion）逐项打分（体现满分/未体现 0 分+建议），权重统计进 evaluate 响应与 ScoreRecord.detail
- **终检**（导出前质量门）：完整性 + 结构合规（招标文件要求章节缺一即拦）+ 逐条要求覆盖（技术要求→技术标、资格要求→商务标）+ 文字质量（占位草稿/Markdown 残留/正文过短/重复段落/【待补充】统计）+ 字数统计
- 企业资料：结构化 fact 确认/纠正（带修订记录）、资产 revision 列表、处理队列列表
- 异步任务：SSE 白名单事件（snapshot/progress/done/cancelled/failed），支持刷新与断线重连取当前状态
- 报价：真实价格数据源优先（AnySearch 检索公开中标公告 + LLM 抽取成交价，来源 URL 可追溯，不足 3 条回退 Mock）、确定性 QuoteEngine、冻结样本可复算、AI 只给参考区间；
  测算历史/详情、单条样本详情、物料趋势（样本统计/分区汇总）；应用报价校验项目与成果类型 + **招标限价拦截**（超限价 422）
- 项目助手：会话 + 消息历史（刷新可恢复），LLM 回答带上下文入库
- 搜索：AnySearch 真实接入（官方 JSON-RPC 端点），DLP 脱敏 + 域名 trust_level 分级，
  无 Key 走匿名额度（约 50 次/天），正式 Key 由运维在 Secret Manager 配置
- **企业知识检索**（Issue #4 第一阶段）：历史项目材料/企业资料/已确认事实的关键词检索，
  来源可追溯（文件/项目/页码/块索引），租户隔离 + 默认排除当前项目，REST + MCP `search_knowledge`
- **招标公告 URL 安全导入**（Issue #6）：逐跳 SSRF/DNS rebinding 防护、重定向/大小/类型限额，
  正文仅进入本项目材料（document_role=招标公告），失败落 error_code 留审计
- 云模型：MiniMax **M3**（文本主推理，2026-08-19 由 Text-01 切换）、百炼 DashScope qwen-vl（视觉），受 P1 门禁控制
- Hermes Agent：NousResearch Hermes（`/data/hermes`），连接 bidvolt MCP（26 个工具）+ 5 个业务 Skill；
  **标书生成默认路径**（质量门兜底内嵌闭环），任务级 capability token 逐调用授权
- 测试客户端：`/demo/`（真实调用后端 API，多环境配置 + 连接测试，`scripts/build_test_client.py` 打包分发）

## 2. 架构

```
浏览器（Demo 前端 /demo/ 或业务前端）
   │ HTTP + SSE
   ▼
FastAPI（app.main，uvicorn :8123）
   ├─ PostgreSQL 14（/data/pgdata，RLS 租户隔离）      ← supervisor 守护
   ├─ Worker（app.services.worker，任务队列）            ← supervisor 守护
   ├─ ClamAV（病毒扫描，fail-closed）                    ← supervisor 守护
   ├─ Hermes Agent（/data/hermes，headless gateway :9119）← supervisor 守护
   │    └─ bidvolt MCP（stdio，26 工具，任务级 capability 授权）→ 后端
   │         └─ Worker 以 hermes chat 无头模式驱动 bid-generate skill（默认路径，质量门兜底）
   └─ AnySearch / MiniMax-M3 / DashScope（出网，DLP 门禁）
```

单容器内由 **supervisord** 统一管理 postgres / app / worker / hermes / clamd / backup-cron，
崩溃自动重启；容器入口 CMD 为 `/usr/sbin/sshd -D`（平台镜像无 systemd）。部署脚本安装两级
自启动自举（均幂等，入口统一为 `/usr/local/bin/bidvolt-boot`）：

1. **sshd 包装器（主）**：`/usr/sbin/sshd` 替换为包装脚本——容器重启时先触发自举
   （supervisord 未运行则 `nohup bidvolt-init` 拉起全部服务），再 `exec /usr/sbin/sshd.real`；
2. **sshrc 兜底（副）**：SSH 登录时若 supervisord 未运行同样触发自举（双保险）。

自举日志：`/var/log/bidvolt/boot.log`。

## 3. 目录结构

```
app/             FastAPI 应用（api/services/models/static demo）
bidvolt_mcp/     MCP stdio server（OpenRPC IDL + 26 个工具）
deploy/          容器部署脚本（install.sh / bidvolt-init.sh / bidvolt-boot.sh / supervisord.conf / install-hermes.sh / backup.sh / upgrade.sh / healthcheck.sh）
docs/            架构/模块/威胁模型/数据分级授权清单/Hermes 契约与 Skill
scripts/         冒烟与工具脚本（真实 OFD 样本、云能力线上冒烟、MCP 契约）
tests/           pytest（unit/module，容器上跑 PG+RLS）
```

## 4. 本地开发（快速开始）

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env        # 填入数据库/密钥/门禁
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8123
.venv/bin/python -m app.services.worker   # 另开终端
```

测试：

```bash
.venv/bin/pytest -q                 # 本地全量（SQLite）
```

## 5. 部署（运维）

### 5.1 路径约定（全部配置化，不依赖符号链接）

部署脚本通过环境变量决定路径，`deploy/supervisord.conf` 中的 `__REPO__`/`__HERMES_HOME__`
由 `install.sh` 用 `sed` 生成到 `/etc/supervisor/conf.d/bidvolt.conf`：

| 变量 | 默认 | 说明 |
|---|---|---|
| `REPO` | `/data/bidvolt` | 代码 + venv（唯一真源，容器重建后仍在） |
| `HERMES_HOME` | `/data/hermes` | Hermes Agent 数据 + venv |
| `PGDATA` | `/data/pgdata` | PostgreSQL 数据目录（init 脚本固定） |
| `STORAGE_ROOT` | `/data/appdata` | 上传/成果文件（租户前缀目录） |
| `BACKUP_ROOT` | `/data/backups` | 每日 pg_dump + WAL 归档 |
| 日志 | `/data/logs/bidvolt` | `/var/log/bidvolt` 符号链接（init 脚本建立） |

### 5.2 首次安装

```bash
# 本地：把仓库与 .env 复制到容器
scp -r . root@<host>:/data/bidvolt
scp .env root@<host>:/data/bidvolt/.env

# 容器内一键安装（apt 系统包 + venv + PG 初始化 + supervisor + Hermes）
ssh root@<host>
bash /data/bidvolt/deploy/install.sh
```

`install.sh` 完成：系统包（PostgreSQL/ClamAV/LibreOffice/supervisor）→ venv 与依赖 →
`/data` 子目录 → 生成 supervisor 配置（路径来自 `REPO`/`HERMES_HOME`）→ 初始化 PG/迁移 →
启动 supervisord → 安装 Hermes Agent（`SKIP_HERMES=1` 可跳过）。

### 5.3 容器重启 / 重建后恢复

- **同实例重启**：容器可写层与 `/data` 都在，服务由 supervisord 守护。平台重启容器后无需 SSH 登录：
  `install.sh` 已安装 **sshd 包装器**（`/usr/sbin/sshd` → 先触发 `bidvolt-boot` 幂等自举再 exec
  `/usr/sbin/sshd.real`，容器入口 CMD 就是 sshd），另保留 `/etc/ssh/sshrc` 登录兜底（双保险）。
  自举日志：`/var/log/bidvolt/boot.log`。
- **容器被销毁重建**：`/data` 全部保留（代码/venv/Hermes/数据库/文件/备份/日志），
  只需重新执行 `bash /data/bidvolt/deploy/install.sh`（apt 重装系统包 + 生成配置 + 启动；
  venv 已存在时跳过创建，PG 检测到数据目录直接迁移启动）。

### 5.4 验证

```bash
bash /usr/local/bin/bidvolt-healthcheck        # PG + API 健康
curl http://127.0.0.1:8123/healthz             # {"status":"ok"}
supervisorctl status                            # postgres/app/worker/hermes/clamd/backup-cron
```

### 5.5 备份与恢复

- 每日 02:00 cron `pg_dump -Fc` 到 `/data/backups/bidvolt_<日期>.dump`（生成后 `pg_restore --list`
  校验可读），同时备份 `/data/appdata` 业务文件（tar.gz），均保留 30 天；
  WAL 归档到 `/data/backups/wal`（PITR，保留 7 天）。注意 `/etc/cron.d` 条目必须带 `root` 用户字段。
- 恢复：`pg_restore -d bidvolt /data/backups/bidvolt_xxx.dump` + 解包 `bidvolt_appdata_*.tar.gz`。

### 5.6 日常运维

- 重启单服务：`supervisorctl restart app|worker|hermes`
- 改配置：`supervisorctl reread && supervisorctl update`
- 日志：`/data/logs/bidvolt/`（supervisord.log、backup.log、boot.log=启动自举、boot-init.log=自举时 init 输出）
- 健康检查：`bidvolt-healthcheck`（PG/API/alembic head/worker/clamd/持久卷/磁盘/读写全覆盖）
- 升级代码：**`bidvolt-upgrade [tag|commit]`**（预检 → 备份 → 新 venv → 停 app/worker → 迁移 →
  原子切换 → 冒烟 → 失败自动回滚；发布记录 `/var/log/bidvolt/releases.log`；不重启 PostgreSQL）。
  已废弃 `git pull && pip install && supervisorctl restart all` 的无保护升级方式。
  注意：当前生产服务器无 GitHub 出网且挂载盘不支持目录 rename，实际升级走
  `git archive` + SFTP 原地解包流程（详见 `docs/issue-3-发布门禁落地.md` 第五节）。
- **云能力开关**：`DATA_CLASSIFICATION_CONFIRMED`/`CLOUD_LLM_ENABLED`/`SEARCH_ENABLED` 修改
  `/data/bidvolt/.env` 后，需重启容器（supervisor 继承启动时的环境变量，优先级高于 .env）；
  或把开关固定进 supervisor `environment=` 后 `update`。
- **任务中断恢复**：worker 带租约领取任务（`task.lease_*`，300s + 心跳续期）；进程被强杀后租约
  过期自动重新入队，重试耗尽进入 FAILED_TERMINAL 并留审计，不再永久卡 RUNNING。

## 6. 环境变量（.env，禁止提交 git）

见 [.env.example](.env.example)。关键项：

| 变量 | 说明 |
|---|---|
| `DATABASE_URL` / `APP_DB_PASSWORD` | PostgreSQL 连接与建库密码 |
| `JWT_SECRET` / `BIDVOLT_INTERNAL_TOKEN` | 签名密钥与内部传输令牌（≥32 位随机串） |
| `MINIMAX_API_KEY` / `MINIMAX_BASE_URL` | MiniMax 文本模型（默认 api.minimax.chat/v1） |
| `DASHSCOPE_API_KEY` | 百炼 qwen-vl 视觉模型 |
| `ANYSEARCH_KEY` / `ANYSEARCH_BASE_URL` | 搜索（空 = 匿名额度；端点默认 api.anysearch.com/mcp） |
| `DATA_CLASSIFICATION_CONFIRMED` | P1 门禁总开关（签署清单后才置 1） |
| `CLOUD_LLM_ENABLED` / `SEARCH_ENABLED` | 云模型/搜索子开关 |
| `SEARCH_MODE` | `mock`（离线合成）/ `anysearch`（真实出网） |
| `STORAGE_ROOT` / `BACKUP_ROOT` | 文件与备份根目录（/data 约定） |
| `VIRUS_SCAN_REQUIRED` | 1 = ClamAV 不可用即拒绝入库（fail-closed） |

## 7. 使用说明（产品侧）

1. 注册/登录 → 进入工作台
2. 新建项目 → 上传招标材料（txt/docx/xlsx/pdf/OFD；真实 OFD 含文本层/签章/扫描版兜底）
3. 解析后生成 Requirement（可逐条确认/修订，带原文坐标证据）
4. 选择企业资料/报价参数 → 生成三份成果（版本链 + CAS 冲突保护）
5. 发起评审：逐条 review_item（问题/得分/可提升分/依据），可单条或批量补充材料并确认，
   重新审核只重跑受影响项；"一键提升"不改分，只发起材料解析+确认流程
6. 报价测算：确定性 QuoteEngine（冻结样本可复算），AI 只给参考区间并标注风险，用户确认后落版本
7. 搜索：工作台搜索框调用 AnySearch（DLP 脱敏，结果按 trust_level 分级）
8. 助手/Agent：Hermes 可通过 bidvolt MCP 读写项目数据、执行 5 个 Skill 流程

## 8. 安全与门禁

- P1 门禁：`docs/数据分级与授权确认清单.md` 签署前，云模型/搜索默认关闭（fail-closed）
- 租户隔离：全部业务对象绑定 enterprise_id（+ project_id），PG RLS 纵深防护
- 任务级授权：capability token 绑定 enterprise/project/task/工具白名单（`X-Bidvolt-Cap`），
  并在调用时校验 token 项目与请求 `project_id` 一致（跨项目访问 403）
- JWT 回退：capability 端点普通用户调用时按工具映射执行对应权限点，不再绕过权限检查
- 文件安全：隔离区 → magic bytes → 强制 ClamAV → 受限进程解析；压缩包限额/符号链接拒绝
- 出站：搜索前 DLP 脱敏（手机号/证件号/银行卡/信用代码）+ 域名白名单
- SSE/日志：白名单事件，不输出思维链/工具参数/凭据/错误栈
- 发布门禁（Issue #3）：gitleaks secret 扫描（pre-commit 暂存区 + pre-push 全历史 + CI，
  全历史扫描 2026-08-14 通过）；生产配置 fail-fast（`BIDVOLT_ENV=production` 拒绝弱口令/
  占位 JWT_SECRET/不足 32 位内部令牌/SQLite/关闭 ClamAV）；`.env` 权限由脚本强制 0600；
  数据库口令不进入进程 argv（临时 SQL 文件 + psql 变量）
- 任务可靠性：worker 租约 + 心跳 + 过期回收（A-4 扩展）；`bidvolt-upgrade` 带备份/迁移/回滚

## 9. Hermes Agent（已部署）

- 位置：`/data/hermes`（venv + 数据 + skills），supervisor `[program:hermes]` 守护
  `hermes serve` headless gateway（127.0.0.1:9119）
- 模型：主推理 MiniMax-Text-01（`minimax` provider，max_tokens=8000）；
  视觉辅助 = 百炼 qwen-vl-max（`auxiliary.vision` custom + DashScope 兼容端点）
- MCP：`bidvolt`（24 工具，stdio），调用后端需 `BIDVOLT_INTERNAL_TOKEN`
  （当前为 Hermes 服务账号 JWT；生产按 P0-2 走任务级 capability token）
- Skills：`bidvolt-tender-parse / material-match / bid-generate / mock-evaluate / targeted-edit`
- 常用命令（容器内，`export HERMES_HOME=/data/hermes`）：

```bash
/data/hermes/venv/bin/hermes -z '调用 bidvolt MCP 的 search_web 工具搜索：政府采购 电缆 中标价' --yolo --cli
/data/hermes/venv/bin/hermes skills list
/data/hermes/venv/bin/hermes mcp list
```

重新安装：`bash /data/bidvolt/deploy/install-hermes.sh`（幂等）。

## 10. 测试与验证

```bash
# 本地（SQLite）
.venv/bin/pytest -q

# lint / 敏感信息扫描（提交前门禁，CI 同套）
.venv/bin/ruff check app bidvolt_mcp tests
gitleaks detect --source . --log-opts=--all --no-banner
pre-commit install && pre-commit install --hook-type pre-push   # 可选：本地钩子

# 容器（PostgreSQL + RLS，加载 .env）
bash /tmp/run_container_tests.sh -q

# 线上冒烟
.venv/bin/python scripts/smoke_cloud_live.py            # AnySearch 匿名 + MiniMax LLM
.venv/bin/python scripts/smoke_ofd_live.py --file <真实OFD>  # 真实 OFD 上传解析
.venv/bin/python scripts/fetch_ofd_samples.py           # 下载真实 OFD 样本（复现用）
.venv/bin/python scripts/smoke_pg_rls.py                # 真实 PG：RLS 隔离 + capability + IDOR
.venv/bin/python scripts/smoke_all.py                   # 统一端到端入口（--skip 可跳过某项）
```

当前基线（2026-08-19）：本地 **247 passed / 1 skipped**（含生产 fail-fast、任务租约、多企业评审回归、
Issue #4/#5/#6 用例与 Issue #12 回归——用产品真实 docx 作测试夹具的解析质量/去重、重解析清旧块、
空要求生成拦截、无 payload 报价单不编造、终检识别占位草稿、分章深度生成、Hermes 默认路径质量门、
评分细则权重化评审、真实价格源兜底等）；
本地浏览器全流程 E2E **26/26 PASS**（含 Issue #11/#12 专项回归：成果正文可视化+状态判断、步骤条证据、
日志无矛盾误报、资料页项目隔离列、校核结果面板、正文无 Markdown 残留、终检统计面板、结构来自招标文件+Agent 生成路径）；
公网生产环境 E2E（`scripts/e2e_browser_demo.py --base http://47.100.182.3:28123`）**26/26 PASS**
（用产品同款真实招标 docx；主模型 MiniMax-M3；**Hermes 默认生成路径 + 质量门兜底**实测：
Hermes 产出未达质量线自动回退内嵌闭环——技术标 11654 字/商务标 3917 字，无占位无 Markdown 残留；
自检闭环本地 2 轮/公网 1 轮 closed=True；终检覆盖完整性/结构合规/逐条要求覆盖/文字质量/字数统计）；
服务器容器（PG+RLS）204 passed（3 个用例为环境交互问题：2 个因生产 .env 与用例 dev 假设冲突——
已由 `run_container_tests.sh` 固定 `BIDVOLT_ENV=dev` 解决，1 个 capability 终态用例与线上 worker
竞争任务队列，建议停 worker 后复跑）；线上冒烟 `smoke_all` 4/4 PASS（真实 OFD / AnySearch /
MiniMax / 在线编辑 / PG+RLS），`live_bid_check`（三份成果生成）/ `live_req_check`（材料解析→
Requirement）PASS；浏览器全流程 E2E 见 `scripts/e2e_browser_demo.py`。

## 11. 已知限制 / 路线图

- Hermes 默认生成路径已落地（任务级 capability token → MCP 逐调用授权 → 质量门兜底内嵌闭环，
  2026-08-19 实测）；遗留：Hermes 单次产出的篇幅受模型/会话预算影响，未达质量线时回退内嵌
  （runtime=hermes-fallback 可追溯），Agent 产出质量随主推理模型升级持续优化
- 评分细则权重化评审为 V1（正文体现判分）；评分细则的细分打分（按细则子项人工评审计分）
  为后续增强
- Issue #3 发布门禁：已全部落地——gitleaks/pre-commit/CI 全历史扫描、配置 fail-fast、worker 租约恢复、
  `bidvolt-upgrade` 升级回滚、备份恢复演练（2026-08-14 服务器实测）、容器重启自启动自举
  （sshd 包装器，2026-08-16 落地，见 §2/§5.3）
  （见 https://github.com/zhangsheng377/BidVolt/issues/3）
- Issue #4 知识检索：历史标书/方案/行业规范的检索能力尚未评估
  （见 https://github.com/zhangsheng377/BidVolt/issues/4）
- AnySearch 中文检索质量受上游服务限制；匿名额度 50 次/天，正式 Key（1000 次/天）由运维配置
- 扫描版 OFD/图片走视觉模型兜底（qwen-vl），需在业务侧确认视觉门禁与授权
- ruff 基线收窄为 E/F/I/UP 安全规则集；B008/RUF012 等风格类为后续债务（见 `.ruff.toml`）
